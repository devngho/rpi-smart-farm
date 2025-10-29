from dataclasses import dataclass, field
from typing import Optional
from _packet import SensorReport

@dataclass
class ReconcilerConfig:
    moisture_range: tuple[int, int] = (20, 60)  # target moisture range (percent)
    target_inner_temp: int = 20                 # target inner temperature (Celsius)

@dataclass
class ReconcilerTune:
    pump_Kp: float = 10.0
    pump_Ki: float = 0.1
    pump_Kd: float = 0.1
    peltier_Kp: float = 30.0
    peltier_Ki: float = 0.2
    peltier_Kd: float = 0.001

    # Smoothing (EMA) + warmup length (SMA)
    temp_ema_alpha: float = 0.1
    temp_ema_count: int = 13
    moisture_ema_alpha: float = 0.1
    moisture_ema_count: int = 13

    # Deadbands
    temp_deadband: float = 1.0        # °C
    moisture_deadband: float = 1.0    # %

    cutoff: int = 64                  # actuator cutoff level

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

    # ---- 1) Moisture (Pump) PID with deadband, derivative LPF, anti-windup ----
    lo, hi = config.moisture_range
    target_moist = (lo + hi) / 2
    # Deadband: within [lo - db, hi + db] → error=0
    moist_db_lo = lo - tune.moisture_deadband
    moist_db_hi = hi + tune.moisture_deadband
    moist_error = 0.0 if (moist_db_lo <= state.filt_moist <= moist_db_hi) else (target_moist - state.filt_moist)

    # Derivative with 1st-order LPF
    a_der = tune.der_tau / (tune.der_tau + dt) if tune.der_tau > 0 else 0.0
    raw_d_m = (moist_error - state.last_moisture_error) / dt
    state.der_moist = a_der * state.der_moist + (1 - a_der) * raw_d_m

    # Conditional integration (freeze when saturated in worsening direction)
    u_noI_pump = tune.pump_Kp * moist_error + tune.pump_Kd * state.der_moist
    u_tmp = u_noI_pump + tune.pump_Ki * state.integral_moisture
    u_tmp_sat = _clamp(u_tmp, 0.0, 1023.0)
    worsen = (u_tmp_sat >= 1023.0 and moist_error > 0) or (u_tmp_sat <= 0.0 and moist_error < 0)
    if not worsen and moist_error != 0.0:
        state.integral_moisture += moist_error * dt

    # Integral contribution clamp (anti-windup)
    I_pump = tune.pump_Ki * state.integral_moisture
    if tune.pump_Ki > 0:
        I_pump = _clamp(I_pump, -tune.aw_limit, tune.aw_limit)
        state.integral_moisture = I_pump / tune.pump_Ki  # keep state consistent

    pump_u = u_noI_pump + I_pump
    pump_u = _clamp(pump_u, 0.0, 1023.0)
    state.last_moisture_error = moist_error
    pump_level = int(round(pump_u))

    if pump_level < tune.cutoff:
        pump_level = 0

    # ---- 2) Temperature (Peltier) PID ----
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

    peltier_forward = 1 if peltier_u >= 0 else 0
    peltier_level = int(round(abs(peltier_u)))
    if peltier_level < tune.cutoff:
        peltier_level = 0

    return state, ReconcilerCommand(
        pump_level=pump_level,
        peltier_level=peltier_level,
        peltier_forward=peltier_forward
    )
