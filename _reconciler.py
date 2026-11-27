import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from datetime import time as dt_time
from math import exp, isfinite
from pathlib import Path

from _packet import SensorReport


@dataclass
class ReconcilerConfig:
    moisture_range: tuple[int, int] = (205, 614)  # raw ADC units
    target_inner_temp: int = 20                 # target inner temperature (Celsius)
    # Pump disable time ranges: list of (start_hour, start_minute, end_hour, end_minute)
    # Example: [(22, 0, 6, 0)] means disable pump from 22:00 to 06:00
    pump_disable_times: list[tuple[int, int, int, int]] = field(default_factory=list)

    cooling_direction: int = 1  # confirm on the actual Peltier wiring

    sensor_timeout_s: float = 15.0

    # Inner temperature sensor dropouts:
    # 1) up to `inner_hold_s` after the last valid inner reading, keep controlling on that reading;
    # 2) after that, estimate the inner temperature from the outer sensor for at most
    #    `fallback_max_s` (0 disables) with the Peltier capped at `fallback_max_level`;
    # 3) then switch the Peltier off until the inner sensor recovers.
    inner_hold_s: float = 60.0
    fallback_max_s: float = 4 * 3600.0
    fallback_max_level: int = 512

    def __post_init__(self):
        if not isfinite(self.sensor_timeout_s) or self.sensor_timeout_s <= 0:
            raise ValueError('sensor_timeout_s must be finite and positive')
        if type(self.cooling_direction) is not int or self.cooling_direction not in (0, 1):
            raise ValueError('cooling_direction must be 0 or 1')
        if not (isfinite(self.inner_hold_s) and self.inner_hold_s >= 0
                and isfinite(self.fallback_max_s) and self.fallback_max_s >= 0):
            raise ValueError('inner_hold_s and fallback_max_s must be finite and >= 0')
        if not 0 <= self.fallback_max_level <= 1023:
            raise ValueError('fallback_max_level must be within 0..1023')

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
    moisture_deadband: float = 10.24  # raw ADC units

    cutoff: int = 64                    # actuator cutoff level

    # Derivative LPF and anti-windup
    der_tau: float = 1.0              # s (low-pass on derivative)
    aw_limit: float = 2.0             # limit on integral contribution (Ki*I) in output units

    # Inner temperature estimate from the outer sensor:
    #   inner ~= outer + bias + heater_gain * lagged signed Peltier duty (+1 = full heating)
    # Defaults fitted on 4.6 days of farm history: heater off -> inner ~0.7 C above outer,
    # full heating adds ~4.5 C with a ~6 min lag. `bias` is re-learned online while the inner
    # sensor works (time constant fallback_bias_tau); the prior is used until then.
    fallback_heater_gain: float = 4.5   # °C per full-scale duty
    fallback_heater_tau: float = 360.0  # s
    fallback_bias_tau: float = 300.0    # s
    fallback_prior_bias: float = 0.7    # °C
    # The learned bias is kept within prior ± this (history: 5th-95th percentile -0.5..2.6 °C),
    # so readings from a failing sensor (e.g. condensation right before it dies) can't skew it far.
    fallback_bias_limit: float = 2.0    # °C

def load_settings[T: (ReconcilerConfig, ReconcilerTune)](filename: str, defaults: T, *, create=False) -> T:
    path = Path(filename)
    try:
        if not path.exists():
            if create:
                path.write_text(json.dumps(asdict(defaults), indent=4), encoding='utf-8')
            return defaults
        return type(defaults)(**json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f'Invalid settings in {path}: {exc}') from exc


@dataclass
class ReconcilerCommand:
    pump_level: int      # 0-1023
    peltier_level: int   # 0-1023
    peltier_forward: int # 0 or 1
    # Which temperature the Peltier command was based on:
    # 'inner' | 'held' (last valid inner, short dropout) | 'estimated' (outer sensor) | 'none' (off)
    temp_source: str = 'none'
    control_temp: float | None = None  # °C value the controller used (unfiltered)

    @property
    def fan_level(self) -> int:
        return 1023 if self.peltier_level >= 64 else 0

@dataclass
class InnerEstimator:
    """Tracks what is needed to stand in for the inner sensor. Uses wall-clock seconds so it
    can survive ReconcilerState resets (sensor timeouts, USB reconnects)."""
    last_inner: float | None = None
    last_inner_at: float | None = None
    bias: float | None = None          # learned inner - outer - gain*duty_lag
    duty_lag: float = 0.0              # first-order lag of the applied signed Peltier duty
    last_duty: float = 0.0             # signed duty currently applied (+ heating)
    last_at: float | None = None
    outage_since: float | None = None  # first report without a valid inner reading
    estimate: float | None = None

    def observe(self, report: SensorReport, config: 'ReconcilerConfig', tune: 'ReconcilerTune') -> None:
        now = report.received_at
        if self.last_at is not None and now > self.last_at:
            gap = now - self.last_at
            duty = self.last_duty if gap <= config.sensor_timeout_s else 0.0  # outputs were stopped
            tau = max(tune.fallback_heater_tau, 1e-6)
            self.duty_lag += (1 - exp(-gap / tau)) * (duty - self.duty_lag)
        else:
            gap = 0.0
        self.last_at = now
        if report.temp_inner is not None:
            self.last_inner, self.last_inner_at, self.outage_since = report.temp_inner, now, None
            if report.temp_outer is not None:
                x = report.temp_inner - report.temp_outer - tune.fallback_heater_gain * self.duty_lag
                if self.bias is None:
                    self.bias = x
                else:
                    self.bias += (1 - exp(-gap / max(tune.fallback_bias_tau, 1e-6))) * (x - self.bias)
                lo = tune.fallback_prior_bias - tune.fallback_bias_limit
                self.bias = min(max(self.bias, lo), lo + 2 * tune.fallback_bias_limit)
        elif self.outage_since is None:
            self.outage_since = now if self.last_inner_at is None else self.last_inner_at
        self.estimate = None
        if report.temp_outer is not None:
            bias = self.bias if self.bias is not None else tune.fallback_prior_bias
            self.estimate = report.temp_outer + bias + tune.fallback_heater_gain * self.duty_lag

    def applied(self, peltier_level: int, peltier_forward: int, cooling_direction: int) -> None:
        """Record the Peltier output actually sent (heating positive)."""
        sign = 1.0 if peltier_forward == 1 - cooling_direction else -1.0
        self.last_duty = sign * peltier_level / 1023

    def source(self, report: SensorReport, config: 'ReconcilerConfig') -> tuple[str, float | None]:
        if report.temp_inner is not None:
            return 'inner', report.temp_inner
        now = report.received_at
        if self.last_inner_at is not None and now - self.last_inner_at <= config.inner_hold_s:
            return 'held', self.last_inner
        since = self.outage_since if self.outage_since is not None else now
        if self.estimate is not None and now - since < config.fallback_max_s:
            return 'estimated', self.estimate
        return 'none', None


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
    filt_moist: float | None = None
    filt_temp: float | None = None
    der_moist: float = 0.0
    der_temp: float = 0.0

    temp_source: str = 'none'
    estimator: InnerEstimator = field(default_factory=InnerEstimator)

    def reset(self) -> 'ReconcilerState':
        """Fresh controller state that keeps what the inner-temperature estimator has learned."""
        return ReconcilerState(estimator=self.estimator)

# ---------- helpers ----------
def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v

def _ema_update(prev: float | None, x: float, alpha: float) -> float:
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

    if tune.moisture_ema_count > 0:
        state.moisture_ema.append(report.moisture)
        if len(state.moisture_ema) > tune.moisture_ema_count:
            state.moisture_ema.pop(0)
        moist_sma = sum(state.moisture_ema) / len(state.moisture_ema)
    else:
        moist_sma = report.moisture

    # EMA on top of SMA (more 안정적 초기화)
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
    est = state.estimator
    est.observe(report, config, tune)
    source, temp = est.source(report, config)
    state.temp_source = source
    if temp is None:
        state.temp_ema.clear()
        state.filt_temp = None
        state.integral_temp = state.last_temp_error = state.der_temp = 0.0
        est.applied(0, 0, config.cooling_direction)
        return state, ReconcilerCommand(pump_level, 0, 0, 'none', None)

    temp_sma = temp
    if tune.temp_ema_count > 0:
        state.temp_ema.append(temp_sma)
        if len(state.temp_ema) > tune.temp_ema_count:
            state.temp_ema.pop(0)
        temp_sma = sum(state.temp_ema) / len(state.temp_ema)
    state.filt_temp = _ema_update(state.filt_temp, temp_sma, tune.temp_ema_alpha)

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

    peltier_forward = 1 - config.cooling_direction if peltier_u >= 0 else config.cooling_direction
    peltier_level = int(round(abs(peltier_u)))
    if source == 'estimated':
        peltier_level = min(peltier_level, config.fallback_max_level)
    if peltier_level < tune.cutoff:
        peltier_level = 0

    est.applied(peltier_level, peltier_forward, config.cooling_direction)
    return state, ReconcilerCommand(
        pump_level=pump_level,
        peltier_level=peltier_level,
        peltier_forward=peltier_forward,
        temp_source=source,
        control_temp=round(temp, 2),
    )
