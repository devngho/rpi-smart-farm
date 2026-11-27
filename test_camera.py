"""Camera night mode, with ffmpeg reading synthetic frames instead of a webcam."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import _camera

INPUTS = {
    'day': ['-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=10'],
    # dim scene + strong per-frame sensor noise, like the C270 at night
    'night': ['-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=10,eq=brightness=-0.42,noise=alls=20:allf=t'],
}


class CameraTests(unittest.TestCase):
    def grab(self, scene, controls=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, '1.jpg.tmp')
        patches = [patch.object(_camera, 'camera_input', lambda device=None: INPUTS[scene]),
                   patch.object(_camera, 'WARMUP_FRAMES', 1)]
        if controls is not None:
            patches.append(patch.object(_camera, 'Controls', controls))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with patch('builtins.print') as log:
            asyncio.run(_camera.grab(path, device='/nonexistent'))
        self.assertEqual(sorted(os.listdir(tmp.name)), ['1.jpg.tmp'])  # no temp files left behind
        return path, ' '.join(str(c) for c in log.call_args_list)

    def luma(self, path):
        return asyncio.run(_camera.mean_luma(path))

    def test_day_snapshot_untouched(self):
        path, log = self.grab('day')
        self.assertGreater(self.luma(path), _camera.NIGHT_LUMA)
        self.assertNotIn('night capture', log)

    def test_dark_snapshot_is_retaken_brighter(self):
        path, log = self.grab('night')
        self.assertIn('Camera night capture (mean luma', log)
        self.assertIn('no controls', log)  # no webcam here: stacking + levels still apply
        self.assertGreater(self.luma(path), 60)

    def test_controls_set_and_restored(self):
        calls = []

        class FakeControls(_camera.Controls):
            def __init__(self, device=None):
                self.fd = None

            def query(self, cid):
                return {_camera.CID_EXPOSURE_ABSOLUTE: (3, 2047, 250), _camera.CID_GAIN: (0, 255, 0),
                        _camera.CID_EXPOSURE_AUTO: (0, 3, 3), _camera.CID_EXPOSURE_AUTO_PRIORITY: (0, 1, 0)}.get(cid)

            def set(self, cid, value):
                calls.append((cid, value))
                return True

        path, log = self.grab('night', FakeControls)
        self.assertIn('exposure 204.7 ms, gain 255', log)
        night = calls.index((_camera.CID_EXPOSURE_AUTO, _camera.EXPOSURE_MANUAL))
        self.assertIn((_camera.CID_EXPOSURE_ABSOLUTE, 2047), calls[night:])
        self.assertIn((_camera.CID_GAIN, 255), calls[night:])
        self.assertEqual(calls[-4:], [(_camera.CID_EXPOSURE_AUTO, 3), (_camera.CID_EXPOSURE_AUTO_PRIORITY, 0),
                                      (_camera.CID_GAIN, 0), (_camera.CID_EXPOSURE_ABSOLUTE, 250)])  # back to auto

    def test_night_failure_keeps_normal_snapshot(self):
        with patch.object(_camera, 'night_filter', lambda: 'nonexistent_filter'):
            path, log = self.grab('night')
        self.assertIn('night capture failed', log)
        self.assertLess(self.luma(path), _camera.NIGHT_LUMA)

    def test_controls_without_device(self):
        with patch('builtins.print'), _camera.Controls('/nonexistent') as ctl:
            self.assertIsNone(ctl.query(_camera.CID_GAIN))
            self.assertEqual(ctl.night(), 'no controls')
            ctl.reset()


if __name__ == '__main__':
    unittest.main()
