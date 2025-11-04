from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, time as dt_time
from _packet import SensorReport

@dataclass
class ReconcilerConfig:
    moisture_range: tuple[int, int] = (20, 60)  # target moisture range (percent)
    target_inner_temp: int = 20                 # target inner temperature (Celsius)
    # Pump disable time ranges: list of (start_hour, start_minute, end_hour, end_minute)
    # Example: [(22, 0, 6, 0)] means disable pump from 22:00 to 06:00
    pump_disable_times: list[tuple[int, int, int, int]] = field(default_factory=list)

@dataclass
class ReconcilerTune:
    pump_Kp: float = 10.0
    pump_Ki: float = 0.1
    pump_Kd: float = 0.1
    peltier_Kp: float = 50.0
    peltier_Ki: float = 0.2
    peltier_Kd: float = 0.001

    # Smoothing (EMA) + warmup length (SMA)
    temp_ema_alpha: float = 0.1
    temp_ema_count: int = 1
    moisture_ema_alpha: float = 0.1
    moisture_ema_count: int = 1

    # Deadbands
    temp_deadband: float = 1.0        # °C
    moisture_deadband: float = 1.0    # %

    cutoff: int = 64                    # actuator cutoff level

    # Derivative LPF and anti-windup
    der_tau: float = 1.0              # s (low-pass on derivative)
    aw_limit: float = 2.0             # limit on integral contribution (Ki*I) in output units

@dataclass
class ReconcilerCommand:
    pump_level: int      # 0-1023
    peltier_level: int   # 0-1023
    peltier_forward: int # 0 or 1

@dataclass
class ReconcilerState:
    # Warmup buffers for SMA (used only for first N samples)
    temp_ema: list[float] = field(default_factory=list)
    moisture_ema: list[float] = field(default_factory=list)

    # PID states
    integral_moisture: float = 0.0
    last_moisture_error: float = 0.0
    integral_temp: float = 0.0
    last_temp_error: float = 0.0

    # Filtered signals and derivative filters
    filt_moist: Optional[float] = None
    filt_temp: Optional[float] = None
    der_moist: float = 0.0
    der_temp: float = 0.0

# ---------- helpers ----------
def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v

def _ema_update(prev: Optional[float], x: float, alpha: float) -> float:
    alpha = _clamp(alpha, 0.0, 1.0)
    return x if prev is None else (1 - alpha) * prev + alpha * x

def _is_pump_disabled(config: ReconcilerConfig) -> bool:
    """현재 시간이 펌프 비활성화 시간대에 속하는지 확인"""
    if not config.pump_disable_times:
        return False
    
    now = datetime.now()
    current_time = now.time()
    
    for start_h, start_m, end_h, end_m in config.pump_disable_times:
        start_time = dt_time(start_h, start_m)
        end_time = dt_time(end_h, end_m)
        
        # 시간대가 자정을 넘어가는 경우 (예: 22:00 - 06:00)
        if start_time > end_time:
            if current_time >= start_time or current_time < end_time:
                return True
        # 일반적인 경우 (예: 09:00 - 17:00)
        else:
            if start_time <= current_time < end_time:
                return True
    
    return False

# ---------- main ----------
def reconcile_sensor_data(
    prev_state: ReconcilerState,
    config: ReconcilerConfig,
    tune: ReconcilerTune,
    report: SensorReport,
    dt: float
) -> tuple[ReconcilerState, ReconcilerCommand]:
    state = prev_state
    if dt <= 0:
        dt = 1e-3

    # ---- 0) Measurements: warmup SMA then EMA smoothing ----
    # Warmup buffers (cap length to *_ema_count)
    if tune.temp_ema_count > 0:
        state.temp_ema.append(report.temp_inner)
        if len(state.temp_ema) > tune.temp_ema_count:
            state.temp_ema.pop(0)
        temp_sma = sum(state.temp_ema) / len(state.temp_ema)
    else:
        temp_sma = report.temp_inner

    if tune.moisture_ema_count > 0:
        state.moisture_ema.append(report.moisture)
        if len(state.moisture_ema) > tune.moisture_ema_count:
            state.moisture_ema.pop(0)
        moist_sma = sum(state.moisture_ema) / len(state.moisture_ema)
    else:
        moist_sma = report.moisture

    # EMA on top of SMA (more 안정적 초기화)
    state.filt_temp  = _ema_update(state.filt_temp,  temp_sma,  tune.temp_ema_alpha)
    state.filt_moist = _ema_update(state.filt_moist, moist_sma, tune.moisture_ema_alpha)

    # ---- 1) Moisture (Pump) - Simple On/Off Control (No PID) ----
    lo, hi = config.moisture_range
    
    # 시간대 체크: 비활성화 시간대면 펌프 끄기
    if _is_pump_disabled(config):
        pump_level = 0
    else:
        # 간단한 on/off 제어 (데드밴드 적용)
        moist_db_lo = lo - tune.moisture_deadband
        moist_db_hi = hi + tune.moisture_deadband
        
        if state.filt_moist < moist_db_lo:
            # 너무 건조 -> 펌프 켜기
            pump_level = 1023
        elif state.filt_moist > moist_db_hi:
            # 너무 습함 -> 펌프 끄기
            pump_level = 0
        else:
            pump_level = 0
    
    # 컷오프 적용
    if pump_level < tune.cutoff:
        pump_level = 0

    # ---- 2) Temperature (Peltier) PID ----
    # Derivative with 1st-order LPF
    a_der = tune.der_tau / (tune.der_tau + dt) if tune.der_tau > 0 else 0.0
    
    temp_error = config.target_inner_temp - state.filt_temp
    if abs(temp_error) < tune.temp_deadband:
        temp_error = 0.0

    raw_d_t = (temp_error - state.last_temp_error) / dt
    state.der_temp = a_der * state.der_temp + (1 - a_der) * raw_d_t

    # Conditional integration for symmetric actuator
    u_noI_temp = tune.peltier_Kp * temp_error + tune.peltier_Kd * state.der_temp
    u_tmp_t = u_noI_temp + tune.peltier_Ki * state.integral_temp
    u_tmp_t_sat = _clamp(u_tmp_t, -1023.0, 1023.0)
    worsen_t = (u_tmp_t_sat >= 1023.0 and temp_error > 0) or (u_tmp_t_sat <= -1023.0 and temp_error < 0)
    if not worsen_t and temp_error != 0.0:
        state.integral_temp += temp_error * dt

    I_temp = tune.peltier_Ki * state.integral_temp
    if tune.peltier_Ki > 0:
        I_temp = _clamp(I_temp, -tune.aw_limit, tune.aw_limit)
        state.integral_temp = I_temp / tune.peltier_Ki

    peltier_u = u_noI_temp + I_temp
    peltier_u = _clamp(peltier_u, -1023.0, 1023.0)
    state.last_temp_error = temp_error

    peltier_forward = 0 if peltier_u >= 0 else 1
    peltier_level = int(round(abs(peltier_u)))
    if peltier_level < tune.cutoff:
        peltier_level = 0

    return state, ReconcilerCommand(
        pump_level=pump_level,
        peltier_level=peltier_level,
        peltier_forward=peltier_forward
    )
