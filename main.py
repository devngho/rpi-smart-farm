# file: example.py
import asyncio
import json
import math
import os
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, replace

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response

from _history import store as history_store
from _illumination import read_light
from _packet import PacketConnection, SensorReport, open_link
from _reconciler import (
    ReconcilerCommand,
    ReconcilerConfig,
    ReconcilerState,
    ReconcilerTune,
    load_settings,
    reconcile_sensor_data,
)
from _store import add_segment, last_segments

last_report: SensorReport | None = None
last_command: ReconcilerCommand | None = None
reconciler_state = ReconcilerState()

reconciler_config = load_settings('config.json', ReconcilerConfig(), create=True)
reconciler_tune = load_settings('tune.json', ReconcilerTune(), create=True)

print("Reconciler Config:", reconciler_config)
print("Reconciler Tune:", reconciler_tune)

async def record_reports(records: asyncio.Queue):
    while True:
        report, command = await records.get()
        try:
            report.illumination = await asyncio.to_thread(read_light)
        except OSError:
            report.illumination = None
        try:
            await asyncio.to_thread(add_segment, (report, command))
        except (OSError, ValueError) as exc:
            print(f'Sensor history unavailable: {exc}')


async def control(link: PacketConnection, records: asyncio.Queue):
    global last_command, last_report, reconciler_state
    previous = None
    pump_until = None
    while True:
        try:
            report = await asyncio.wait_for(link.reports.get(), timeout=0.1)
        except TimeoutError:
            report = None
        now = time.monotonic()
        if pump_until is not None and now >= pump_until:
            await link.send(0, 0)
            pump_until = None
            if last_command:
                last_command = replace(last_command, pump_level=0)
        if previous is not None and now - previous >= reconciler_config.sensor_timeout_s:
            if last_command and (last_command.peltier_level or last_command.pump_level):
                await link.stop()
                last_command = ReconcilerCommand(0, 0, 1)
            pump_until = previous = None
            # don't blend stale inputs into recovery (the inner-temperature estimator is kept)
            reconciler_state = reconciler_state.reset()
        if report is None:
            continue
        deadline = report.received_monotonic + reconciler_config.sensor_timeout_s
        if now >= deadline:
            continue  # never apply an expired sample from the receive queue
        if pump_until is not None:
            deadline = min(deadline, pump_until)
        dt = report.received_monotonic - previous if previous is not None else 0.001
        previous = report.received_monotonic
        # RX and heartbeat keep running during calculation. If the controller itself
        # stalls past an output safety deadline, discard its result and stop the link.
        async with asyncio.timeout(deadline - now):
            reconciler_state, command = await asyncio.to_thread(
                reconcile_sensor_data, reconciler_state, reconciler_config, reconciler_tune, report, dt)
        if not link.reports.empty():
            # A newer sample (possibly a sensor fault) arrived during calculation.
            # Record this sample but only energize outputs from the newest one.
            command = ReconcilerCommand(0, 0, 1)
            reconciler_state.estimator.applied(0, 1, reconciler_config.cooling_direction)
        await link.send(0, command.pump_level)
        if not command.pump_level:
            pump_until = None
        elif pump_until is None:
            pump_until = time.monotonic() + 1
        await link.send(1, command.peltier_level, command.peltier_forward)
        await link.send(2, command.fan_level)
        last_report, last_command = report, command
        # A stalled history disk must never stall serial safety checks.
        if records.full():
            records.get_nowait()
        records.put_nowait((report, command))


async def read_task(port: str, records: asyncio.Queue):
    global last_report, last_command, reconciler_state
    while True:
        try:
            async with open_link(port) as link, asyncio.TaskGroup() as tasks:
                tasks.create_task(link.heartbeat())
                tasks.create_task(link.receive())
                tasks.create_task(control(link, records))
        except* OSError as exc:
            print(f'USB serial connection lost: {exc.exceptions}')
        finally:
            last_report = last_command = None
            reconciler_state = reconciler_state.reset()
        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    port = os.environ.get('SMART_FARM_SERIAL_PORT')
    if not port:
        raise RuntimeError('Set SMART_FARM_SERIAL_PORT to the UNO /dev/serial/by-id/... path')
    records = asyncio.Queue(maxsize=64)
    async with asyncio.TaskGroup() as tasks:
        serial_task = tasks.create_task(read_task(port, records))
        history_task = tasks.create_task(record_reports(records))
        # Parse recent history once up front so the dashboard's first chart load is fast.
        warm_task = tasks.create_task(asyncio.to_thread(history_store.query, None, None, 1000))
        try:
            yield
        finally:
            serial_task.cancel()
            history_task.cancel()
            warm_task.cancel()

app = FastAPI(lifespan=lifespan)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get('SMART_FARM_CORS_ORIGINS', '*').split(','),
    allow_credentials=bool(os.environ.get('SMART_FARM_CORS_ORIGINS')),
    allow_methods=["*"],  # 모든 HTTP 메서드 허용
    allow_headers=["*"],  # 모든 헤더 허용
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

@app.get("/sensor/latest")
async def get_latest_sensor():
    """최근 센서 데이터 반환"""
    if last_report is None:
        return JSONResponse(
            status_code=404,
            content={"error": "No sensor data available yet"}
        )

    return last_report.snapshot(reconciler_config.sensor_timeout_s)

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
    segments = await asyncio.to_thread(last_segments, n)

    return {"segments": segments}

@app.get("/history")
async def get_history(start: float | None = None, end: float | None = None, points: int = 1000):
    """Columnar sensor/command history for [start, end] (Unix seconds).

    Defaults to the 6 hours up to the newest record. When the range holds more than
    `points` rows it is bucketed into aligned time buckets (mean + min/max per bucket);
    `points=0` returns raw rows (for export).
    """
    start = start if start is not None and math.isfinite(start) else None
    end = end if end is not None and math.isfinite(end) else None
    points = min(points, 20000)
    body = await asyncio.to_thread(
        lambda: json.dumps(history_store.query(start, end, points), separators=(',', ':'), allow_nan=False))
    return Response(body, media_type='application/json', headers={'Cache-Control': 'no-cache'})

@app.websocket("/live")
async def websocket_live_sensor(websocket: WebSocket):
    """웹소켓을 통한 실시간 센서 데이터 스트리밍"""
    # WebSocket은 origin 체크를 하지 않도록 설정
    try:
        await websocket.accept()
    except Exception as e:
        print(f"WebSocket accept failed: {e}")
        return

    previous_report = previous_command = object()  # send initial nulls while waiting for USB
    try:
        while True:
            report = last_report.snapshot(reconciler_config.sensor_timeout_s) if last_report else None
            command = {**asdict(last_command), 'fan_level': last_command.fan_level} if last_command else None
            if report != previous_report:
                await websocket.send_json({'report': report})
                previous_report = report
            if command != previous_command:
                await websocket.send_json({'command': command})
                previous_command = command
            await asyncio.sleep(1.0)  # 1초 간격으로 체크
    except Exception as e:
        print(f"WebSocket connection closed: {e}")
    finally:
        with suppress(RuntimeError):
            await websocket.close()

if __name__ == "__main__":
    # Preserve LAN access for the separately hosted dashboard; deployments can narrow the bind.
    host = os.getenv('SMART_FARM_HOST', '0.0.0.0')  # noqa: S104
    uvicorn.run(app, port=8000, host=host)
