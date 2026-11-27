"""Inner temperature dropouts: short hold, outer-sensor estimate, then fail-safe off."""
import unittest
from dataclasses import asdict
from unittest.mock import patch

from _packet import SensorReport
from _reconciler import ReconcilerConfig, ReconcilerState, ReconcilerTune, reconcile_sensor_data

CONFIG = ReconcilerConfig(moisture_range=(100, 614), target_inner_temp=20, cooling_direction=0)
TUNE = ReconcilerTune(peltier_Kp=500.0, peltier_Ki=0.0, peltier_Kd=0.0, temp_ema_alpha=1.0)


class Farm:
    def __init__(self, config=CONFIG, tune=TUNE):
        self.config, self.tune = config, tune
        self.state = ReconcilerState()
        self.now = 1_000_000.0

    def step(self, inner, outer, dt=5.0):
        self.now += dt
        report = SensorReport(500, inner, 60.0, outer, 60.0)
        report.received_at = self.now
        self.state, command = reconcile_sensor_data(self.state, self.config, self.tune, report, dt)
        return command


class FallbackTests(unittest.TestCase):
    def warm(self, farm, minutes=60, inner=17.0, outer=11.8):
        for _ in range(minutes * 12):
            command = farm.step(inner, outer)
        self.assertEqual(command.temp_source, 'inner')
        self.assertEqual(command.peltier_level, 1023)  # heating full: 17 < 20
        self.assertEqual(command.peltier_forward, 1 - CONFIG.cooling_direction)
        return command

    def test_short_dropout_holds_last_inner(self):
        farm = Farm()
        self.warm(farm)
        for _ in range(12):  # 60 s
            command = farm.step(None, 11.8)
            self.assertEqual((command.temp_source, command.control_temp, command.peltier_level), ('held', 17.0, 1023))
        self.assertEqual(farm.step(None, 11.8).temp_source, 'estimated')

    def test_long_dropout_uses_outer_estimate_with_cap(self):
        farm = Farm()
        self.warm(farm)  # learned: inner - outer = 5.2 at full heating
        command = None
        for _ in range(13):
            command = farm.step(None, 11.8)
        self.assertEqual(command.temp_source, 'estimated')
        self.assertAlmostEqual(command.control_temp, 17.0, delta=0.05)
        self.assertEqual(command.peltier_level, CONFIG.fallback_max_level)
        # capped heating: the estimate follows the lower duty (gain 4.5 C per full duty)
        for _ in range(12 * 60):
            command = farm.step(None, 11.8)
        expected = 17.0 - TUNE.fallback_heater_gain * (1 - CONFIG.fallback_max_level / 1023)
        self.assertAlmostEqual(command.control_temp, expected, delta=0.05)
        # outer temperature changes carry through
        self.assertAlmostEqual(farm.step(None, 14.8).control_temp, expected + 3, delta=0.05)

    def test_estimate_regulates_on_warm_day(self):
        farm = Farm()
        self.warm(farm, inner=17.5, outer=12.5)
        levels, temps = [], []
        for _ in range(13):
            farm.step(None, 18.0)  # hold first
        for _ in range(12 * 60):
            command = farm.step(None, 18.0)  # warm outside: capped heating is enough
            self.assertEqual(command.temp_source, 'estimated')
            self.assertLessEqual(command.peltier_level, CONFIG.fallback_max_level)
            levels.append(command.peltier_level)
            temps.append(command.control_temp)
        last = slice(-12 * 10, None)
        # regulated to the target within the deadband, never pushed beyond it
        self.assertLessEqual(max(temps[last]), CONFIG.target_inner_temp + TUNE.temp_deadband)
        self.assertGreaterEqual(min(temps[last]), CONFIG.target_inner_temp - TUNE.temp_deadband - 0.1)
        self.assertLess(sum(levels[last]) / 120, CONFIG.fallback_max_level)

    def test_time_limit_then_off_and_recovery(self):
        config = ReconcilerConfig(**{**asdict(CONFIG), 'fallback_max_s': 600.0})
        farm = Farm(config)
        self.warm(farm)
        sources = [farm.step(None, 11.8).temp_source for _ in range(12 * 15)]
        self.assertEqual(sources[:12], ['held'] * 12)
        self.assertEqual(sources[-1], 'none')
        self.assertEqual(sources.count('estimated'), 120 - 13)  # until 600 s after the last valid reading
        self.assertEqual(farm.step(None, 11.8).peltier_level, 0)
        self.assertEqual(farm.step(17.0, 11.8).temp_source, 'inner')

    def test_no_outer_no_history(self):
        farm = Farm()
        command = farm.step(None, None)
        self.assertEqual((command.temp_source, command.peltier_level), ('none', 0))
        # never saw the inner sensor: prior bias, but still bounded by the cap
        command = farm.step(None, 11.0)
        self.assertEqual(command.temp_source, 'estimated')
        self.assertAlmostEqual(command.control_temp, 11.0 + TUNE.fallback_prior_bias, delta=0.01)
        self.assertEqual(command.peltier_level, CONFIG.fallback_max_level)

    def test_disabled(self):
        config = ReconcilerConfig(**{**asdict(CONFIG), 'inner_hold_s': 0.0, 'fallback_max_s': 0.0})
        farm = Farm(config)
        self.warm(farm)
        command = farm.step(None, 11.8)
        self.assertEqual((command.temp_source, command.peltier_level), ('none', 0))

    def test_reset_keeps_estimator(self):
        farm = Farm()
        self.warm(farm)
        bias = farm.state.estimator.bias
        farm.state = farm.state.reset()
        self.assertIsNone(farm.state.filt_temp)
        self.assertEqual(farm.state.estimator.bias, bias)
        # outputs were stopped across a long gap: lagged duty decays to off
        command = farm.step(None, 11.8, dt=3600)
        self.assertAlmostEqual(command.control_temp, 11.8 + bias, delta=0.05)

    def test_bias_from_failing_sensor_is_bounded(self):
        farm = Farm()
        self.warm(farm)
        for _ in range(12 * 30):
            farm.step(13.0, 12.5)  # implausible: full heating but inside ~= outside
        lo = TUNE.fallback_prior_bias - TUNE.fallback_bias_limit
        self.assertAlmostEqual(farm.state.estimator.bias, lo)

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            ReconcilerConfig(fallback_max_level=2000)
        with self.assertRaises(ValueError):
            ReconcilerConfig(inner_hold_s=-1)


if __name__ == '__main__':
    with patch('builtins.print'):
        unittest.main()
