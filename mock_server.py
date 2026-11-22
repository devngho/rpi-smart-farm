"""
Mock Server for Arduino Sensor System
실제 Arduino 없이 센서 데이터를 시뮬레이션하여 시스템을 테스트
"""
import asyncio
import os
import random
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from _packet import SensorReport
from _reconciler import (
    ReconcilerCommand,
    ReconcilerConfig,
    ReconcilerState,
    ReconcilerTune,
    load_settings,
    reconcile_sensor_data,
)

# 전역 상태
last_report: SensorReport | None = None
last_command: ReconcilerCommand | None = None
last_time = 0.0
reconciler_state = ReconcilerState()

# Mock 환경 상태 (시뮬레이션된 물리적 환경)
mock_environment = {
    "moisture": 2.0,      # 현재 토양 습도 (%)
    "temp_inner": 2.0,    # 내부 온도 (°C)
    "humd_inner": 6.0,    # 내부 습도 (%)
    "temp_outer": 2.0,    # 외부 온도 (°C)
    "humd_outer": 7.0,    # 외부 습도 (%)
    "illumination": 500.0, # 조도 (lux)
}

# 제어 상태
control_state = {
    "pump_level": 0,
    "peltier_level": 0,
    "peltier_forward": 0,
    "fan_level": 0,
}

reconciler_config = load_settings('config.json', ReconcilerConfig())
reconciler_tune = load_settings('tune.json', ReconcilerTune())


def simulate_environment_physics(dt: float):
    """
    물리 법칙을 시뮬레이션하여 환경 상태 업데이트
    - 펌프: 토양 습도 증가
    - 펠티어: 내부 온도 조절
    - 팬: 온도 변화 가속
    - 자연 변화: 습도 감소, 온도는 외부 온도로 수렴
    """
    global mock_environment, control_state

    # 펌프 효과: 습도 증가 (0-1023 레벨)
    pump_effect = control_state["pump_level"] / 1023.0 * 5.0  # 최대 5%/초
    mock_environment["moisture"] += pump_effect * dt

    # 자연 증발: 습도 감소
    evaporation = 0.5  # 0.5%/초
    mock_environment["moisture"] -= evaporation * dt
    mock_environment["moisture"] = max(0, min(100, mock_environment["moisture"]))

    # 펠티어 효과: 온도 조절
    peltier_power = control_state["peltier_level"] / 1023.0
    if control_state["peltier_forward"] != reconciler_config.cooling_direction:
        # 가열
        cooling_effect = peltier_power * 3.0  # 최대 3°C/초
        mock_environment["temp_inner"] += cooling_effect * dt
    else:
        # 냉각
        heating_effect = peltier_power * 3.0
        mock_environment["temp_inner"] -= heating_effect * dt

    # 자연 온도 수렴 (단열 손실)
    natural_convergence = 0.01  # 0.1/초
    temp_diff = mock_environment["temp_outer"] - mock_environment["temp_inner"]
    mock_environment["temp_inner"] += temp_diff * natural_convergence * dt

    # 내부 습도: 온도에 따라 변화 (고온이면 건조)
    temp_humidity_effect = (mock_environment["temp_inner"] - 20) * 0.01
    mock_environment["humd_inner"] -= temp_humidity_effect * dt
    mock_environment["humd_inner"] = max(0, min(100, mock_environment["humd_inner"]))

    # 조도: 시간에 따라 변화 (간단한 사인파)
    time_of_day = (time.time() % 86400) / 86400  # 0-1
    mock_environment["illumination"] = 1000 * max(0, abs(0.5 - time_of_day) * 2 - 0.1)

    # 노이즈 추가
    mock_environment["moisture"] += random.uniform(-0.5, 0.5)
    mock_environment["temp_inner"] += random.uniform(-0.1, 0.1)
    mock_environment["humd_inner"] += random.uniform(-0.5, 0.5)

    # clip
    mock_environment["moisture"] = max(0, min(100, mock_environment["moisture"]))
    mock_environment["humd_inner"] = max(0, min(100, mock_environment["humd_inner"]))
    mock_environment["temp_inner"] = max(0, min(100, mock_environment["temp_inner"]))


def create_mock_sensor_report() -> SensorReport:
    """현재 mock 환경 상태에서 센서 리포트 생성"""
    return SensorReport(
        moisture=round(mock_environment["moisture"] * 1023 / 100),
        temp_inner=mock_environment["temp_inner"],
        humd_inner=mock_environment["humd_inner"],
        temp_outer=mock_environment["temp_outer"],
        humd_outer=mock_environment["humd_outer"],
        illumination=mock_environment["illumination"],
    )


def apply_control_command(command: ReconcilerCommand):
    """제어 명령을 mock 환경에 적용"""
    global control_state
    control_state["pump_level"] = command.pump_level
    control_state["peltier_level"] = command.peltier_level
    control_state["peltier_forward"] = command.peltier_forward

    # 팬은 펠티어 레벨에 따라 자동 조절
    if command.peltier_level >= 64:
        control_state["fan_level"] = 1023
    else:
        control_state["fan_level"] = 0


def process_report(new_report: SensorReport) -> ReconcilerCommand:
    """센서 리포트를 처리하고 제어 명령 생성"""
    global last_time, reconciler_state
    current_time = time.time()
    # if 5s not elapsed, skip
    if current_time - last_time < 5.0:
        return last_command if last_command is not None else ReconcilerCommand(
            pump_level=0,
            peltier_level=0,
            peltier_forward=0
        )
    dt = current_time - last_time if last_time > 0 else 1e-3
    last_time = current_time

    reconciler_state, command = reconcile_sensor_data(
        reconciler_state,
        config=reconciler_config,
        tune=reconciler_tune,
        report=new_report,
        dt=dt
    )
    return command


async def simulation_loop():
    """메인 시뮬레이션 루프"""
    global last_report, last_command, last_time

    last_time = time.time()
    last_sim_time = time.time()

    print("🚀 Mock simulation started")
    print(f"Initial environment: {mock_environment}")

    while True:
        await asyncio.sleep(1.0)  # 1초마다 업데이트

        # 물리 시뮬레이션 업데이트
        current_time = time.time()
        dt = current_time - last_sim_time
        last_sim_time = current_time
        simulate_environment_physics(dt)

        # 센서 리포트 생성
        report = create_mock_sensor_report()

        # 첫 리포트가 아니면 제어 로직 실행
        if last_report is not None:
            command = process_report(report)
            apply_control_command(command)
            last_command = command
            print(f"📊 Report: M={report.moisture} ADC T_in={report.temp_inner}°C | "
                  f"Command: Pump={command.pump_level} Peltier={command.peltier_level}(fw={command.peltier_forward})")

        if last_report is not None:
            report.keep_history(last_report)
        last_report = report


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 앱 라이프사이클 관리"""
    # 시작 시 시뮬레이션 태스크 시작
    task = asyncio.create_task(simulation_loop())
    print("✅ Mock server started on port 8000")
    yield
    # 종료 시 태스크 취소
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    print("🛑 Mock server stopped")


app = FastAPI(
    title="Arduino Sensor System Mock Server",
    description="실제 Arduino 없이 센서 시스템을 시뮬레이션",
    lifespan=lifespan
)

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

@app.websocket("/live")
async def websocket_live_sensor(websocket: WebSocket):
    """웹소켓을 통한 실시간 센서 데이터 스트리밍"""
    await websocket.accept()
    previous_report = None
    previous_command = None
    try:
        while True:
            if last_report is not None and last_report != previous_report:
                await websocket.send_json({'report': last_report.snapshot(reconciler_config.sensor_timeout_s)})
                previous_report = last_report
            if last_command is not None and last_command != previous_command:
                await websocket.send_json({'command': {**asdict(last_command), 'fan_level': last_command.fan_level}})
                previous_command = last_command
            await asyncio.sleep(1.0)  # 1초 간격으로 체크
    except Exception as e:
        print(f"WebSocket connection closed: {e}")
    finally:
        await websocket.close()

if __name__ == "__main__":
    print("=" * 60)
    print("🔧 Arduino Sensor System - Mock Server")
    print("=" * 60)
    print("실제 Arduino 없이 시스템을 시뮬레이션합니다.")
    print("환경 물리를 시뮬레이션하여 제어 로직을 테스트할 수 있습니다.")
    print("=" * 60)
    uvicorn.run(app, host=os.getenv('SMART_FARM_MOCK_HOST', '127.0.0.1'), port=8000)
