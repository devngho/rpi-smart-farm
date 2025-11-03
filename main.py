# file: example.py
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import os
import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn
from _packet import PacketConnection, KIND_SENSOR_REPORT, Heartbeat, SensorReport
from _reconciler import ReconcilerCommand, ReconcilerConfig, ReconcilerState, ReconcilerTune, reconcile_sensor_data
from _store import list_segments

last_report: SensorReport | None = None
last_command: ReconcilerCommand | None = None
last_time = 0.0
reconciler_state = ReconcilerState()

# initialize reconciler config and tune as needed
reconciler_config = ReconcilerConfig()
reconciler_tune = ReconcilerTune()

# if files don't exist, create them with default values
if not os.path.exists('./config.json'):
    with open('./config.json', 'w', encoding='utf-8') as f:
        f.write(json.dumps(asdict(reconciler_config), indent=4, ensure_ascii=False))

if not os.path.exists('./tune.json'):
    with open('./tune.json', 'w', encoding='utf-8') as f:
        f.write(json.dumps(asdict(reconciler_tune), indent=4, ensure_ascii=False))

# from file, init them
with open('./config.json', 'r', encoding='utf-8') as f:
    reconciler_config = ReconcilerConfig(**json.load(f))

with open('./tune.json', 'r', encoding='utf-8') as f:
    reconciler_tune = ReconcilerTune(**json.load(f))

async def read_task(): 
    global last_report, last_command
    PORT = "/dev/ttyAMA3"  # Windows면 "COM3", macOS는 "/dev/tty.usbmodemXXXX"

    async with PacketConnection(PORT) as link:
       while True:
            v = await link.read_packet()
            print(v)
            if v is None:
                continue
            kind, packet = v
            if kind == KIND_SENSOR_REPORT:
                if last_report is not None:
                    last_command = process_report(packet)
                    if last_command is not None:
                        print(f"Sending Command: {last_command}")
                        await link.send_pump(last_command.pump_level)
                        await link.send_peltier(last_command.peltier_level, last_command.peltier_forward)
                        if last_command.peltier_level >= 64:
                            await link.send_fans(1023)
                        else:
                            await link.send_fans(0)
                        # if pump is enabled, we should disable it for a second concurrently
                        if last_command.pump_level > 0:
                            await asyncio.sleep(1.0)
                            await link.send_pump(0)
                last_report = packet
                print(f"Sensor Report: {packet}")
            elif kind == Heartbeat.KIND:
                print("Heartbeat received")
            else:
                print(f"Unknown packet kind: {kind}")

def process_report(new_report: SensorReport) -> ReconcilerCommand:
    # reconcile
    global last_time, reconciler_state
    current_time = time.time()
    dt = current_time - last_time if last_time > 0 else 1e-3
    last_time = current_time
    reconciler_state, command = reconcile_sensor_data(
        reconciler_state,
        config=ReconcilerConfig(),
        tune=ReconcilerTune(),
        report=new_report,
        dt=dt
    )

    return command

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 시작 시 백그라운드 태스크 시작
    task = asyncio.create_task(read_task())
    yield
    # 종료 시 태스크 취소
    task.cancel()
    # send pump and peltier to 0
    if last_command is not None:
        print("Shutting down: sending zero commands")
        async with PacketConnection("/dev/ttyAMA3") as link:
            await link.send_pump(0)
            await link.send_peltier(0, 0)
            await link.send_fans(0)
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 모든 origin 허용 (프로덕션에서는 특정 도메인만 허용)
    allow_credentials=True,
    allow_methods=["*"],  # 모든 HTTP 메서드 허용
    allow_headers=["*"],  # 모든 헤더 허용
)

@app.get("/sensor/latest")
async def get_latest_sensor():
    """최근 센서 데이터 반환"""
    if last_report is None:
        return JSONResponse(
            status_code=404,
            content={"error": "No sensor data available yet"}
        )
    
    return asdict(last_report)

@app.get("/reconciler/state")
async def get_reconciler_state():
    """Reconciler 상태 반환"""
    return {
        "state": asdict(reconciler_state),
        "config": asdict(reconciler_config),
        "tune": asdict(reconciler_tune)
    }

@app.get("/segments")
async def list_data_segments(n: int = 50):
    """저장된 데이터 세그먼트 목록 반환"""
    # read n from url parameter
    segments = list_segments()

    return {"segments": segments[:n]}

@app.websocket("/live")
async def websocket_live_sensor(websocket):
    """웹소켓을 통한 실시간 센서 데이터 스트리밍"""
    await websocket.accept()
    previous_report = None
    previous_command = None
    try:
        while True:
            if last_report is not None and last_report != previous_report:
                await websocket.send_json({'report': asdict(last_report)})
                previous_report = last_report
            if last_command is not None and last_command != previous_command:
                await websocket.send_json({'command': asdict(last_command)})
                previous_command = last_command
            await asyncio.sleep(1.0)  # 1초 간격으로 체크
    except Exception as e:
        print(f"WebSocket connection closed: {e}")
    finally:
        await websocket.close()

if __name__ == "__main__":
    uvicorn.run(app, port=8000)
