"""Camera night mode, with ffmpeg reading synthetic frames instead of a webcam."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import _camera

INPUTS = {
    'day': ['-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=10'],
    # dim scene + strong per-frame sensor noise, like the C270 at night
    'night': ['-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=10,eq=brightness=-0.42,noise=alls=20:allf=t'],
    # dim scene with a strong blue cast (blue lifted, green crushed), like the first night-mode result
    'night_cast': ['-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=10,eq=brightness=-0.42,'
                   'lutrgb=r=val*0.6:g=val*0.3:b=val*0.9+25,noise=alls=10:allf=t'],
}


class CameraTests(unittest.TestCase):
    def grab(self, scene, controls=None, night_scene=None, failing_formats=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, '1.jpg.tmp')
        self.formats = []

        def camera_input(device=None, input_format='mjpeg'):
            self.formats.append(input_format)
            if input_format in failing_formats:
                return ['-f', 'lavfi', '-i', 'nonexistent_source']
            return INPUTS[night_scene if night_scene and len(self.formats) > 1 else scene]

        patches = [patch.object(_camera, 'camera_input', camera_input),
                   patch.object(_camera, 'WARMUP_FRAMES', 1),
                   patch.object(_camera, 'NIGHT_STAGES', (8, 6))]  # fewer frames keep these tests fast
        if controls is not None:
            patches.append(patch.object(_camera, 'Controls', controls))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with patch('builtins.print') as log:
            self.info = asyncio.run(_camera.grab(path, device='/nonexistent'))
        self.assertEqual(sorted(os.listdir(tmp.name)), ['1.jpg.tmp'])  # no temp files left behind
        return path, ' '.join(str(c) for c in log.call_args_list)

    def channel_means(self, path):
        raw = asyncio.run(_camera._pixels(path, '64x36', 'rgb24'))
        return [sum(raw[i::3]) / (len(raw) / 3) for i in range(3)]

    def luma(self, path):
        return asyncio.run(_camera.mean_luma(path))

    def test_day_snapshot_untouched(self):
        path, log = self.grab('day')
        self.assertGreater(self.luma(path), _camera.NIGHT_LUMA)
        self.assertNotIn('night capture', log)
        self.assertEqual(self.info['night'], False)
        self.assertGreater(self.info['luma'], _camera.NIGHT_LUMA)

    def test_dark_snapshot_is_retaken_brighter(self):
        path, log = self.grab('night')
        self.assertTrue(self.info['night'])
        self.assertEqual(self.info['controls'], 'no controls')  # no webcam here: stacking + levels still apply
        self.assertEqual(self.info['input_format'], 'yuyv422')
        self.assertEqual(self.info['frames'], _camera.NIGHT_WARMUP_FRAMES + _camera.night_frames())
        self.assertGreaterEqual(self.info['capture_s'], 0)
        self.assertIn(self.info['source'], ('stacked', 'normal'))
        self.assertLess(self.info['luma'], _camera.NIGHT_LUMA)
        self.assertGreater(self.luma(path), 60)
        self.assertAlmostEqual(self.info['luma_after'], self.luma(path), delta=1)

    def test_colour_cast_removed(self):
        # grey box with orange/yellow "wires", dark, with per-frame noise; then the same under a strong blue cast
        scene = ('color=c=0x808080:s=1280x720:r=10,'
                 'drawbox=x=100:y=0:w=80:h=720:color=0xff8000:t=fill,'
                 'drawbox=x=400:y=0:w=60:h=720:color=0xffd000:t=fill,'
                 'drawbox=x=900:y=0:w=40:h=720:color=0xd02020:t=fill,'
                 'drawbox=x=0:y=620:w=1280:h=100:color=0x000000:t=fill,')  # shadows / black breadboard
        INPUTS['grey_scene'] = ['-f', 'lavfi', '-i', scene + 'lutrgb=r=val*0.1:g=val*0.1:b=val*0.1,noise=alls=6:allf=t']
        INPUTS['grey_scene_cast'] = ['-f', 'lavfi', '-i',
                                     scene + 'lutrgb=r=val*0.06:g=val*0.05:b=val*0.12+6,noise=alls=6:allf=t']
        self.addCleanup(INPUTS.pop, 'grey_scene')
        self.addCleanup(INPUTS.pop, 'grey_scene_cast')
        results = []
        for name in ('grey_scene', 'grey_scene_cast'):
            path, _ = self.grab(name)
            self.assertTrue(self.info['night'])
            raw = asyncio.run(_camera._pixels(path, '128x72', 'rgb24'))
            # background column band x = 70%..85% of the width is plain grey in the scene
            bg = [raw[(y * 128 + x) * 3:(y * 128 + x) * 3 + 3] for y in range(55) for x in range(96, 108)]
            results.append([sum(p[i] for p in bg) / len(bg) for i in range(3)])
        for r, g, b in results:
            self.assertLess(max(r, g, b) - min(r, g, b), 20, results)  # background stays neutral
        for a, b in zip(*results):
            self.assertAlmostEqual(a, b, delta=20, msg=results)  # and the cast makes little difference

    def test_darker_long_exposure_falls_back_to_normal_frame(self):
        # night retake darker than the auto-exposure shot, like the first real YUYV attempt
        INPUTS['darker'] = ['-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=10,lutrgb=r=val*0.05:g=val*0.05:b=val*0.05']
        self.addCleanup(INPUTS.pop, 'darker')
        path, _ = self.grab('night', night_scene='darker')
        self.assertTrue(self.info['night'])
        self.assertLess(self.info['luma_stacked'], self.info['luma'])
        self.assertEqual(self.info['source'], 'normal')
        self.assertGreater(self.luma(path), self.info['luma'])

    def test_stacking_reduces_noise(self):
        # same dim scene, heavy per-frame noise: noise must fall with the number of frames stacked
        INPUTS['noisy'] = ['-f', 'lavfi', '-i', 'color=c=0x303030:s=1280x720:r=30,noise=alls=40:allf=t']
        self.addCleanup(INPUTS.pop, 'noisy')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        async def spread(vf, name):
            out = os.path.join(tmp.name, name)
            await _camera._ffmpeg([*INPUTS['noisy'], '-vf', vf, '-frames:v', '1', '-y', out], 60)
            # full resolution, 16-bit grey: no downscaling or 8-bit rounding hiding the noise
            raw = await _camera._ffmpeg(['-i', out, '-vf', 'format=gray16le', '-f', 'rawvideo', '-'], 30)
            px = [int.from_bytes(raw[i:i + 2], 'little') / 257 for i in range(0, len(raw), 2 * 7)]
            mean = sum(px) / len(px)
            return (sum((v - mean) ** 2 for v in px) / len(px)) ** 0.5

        async def run():
            return (await spread('select=gte(n\\,15),format=gbrp16le', 'one.png'),
                    await spread(_camera.night_filter(stages=(8, 6)), 'n48.png'),
                    await spread(_camera.night_filter(), 'n192.png'))

        one, n48, n192 = asyncio.run(run())
        self.assertEqual(_camera.night_frames(), 192)
        self.assertLess(n192, one / 8, (one, n48, n192))   # ~13x in theory
        self.assertLess(n192, n48 * 0.8, (one, n48, n192))  # more frames still help

    def test_white_balance_ignores_coloured_wires(self):
        # night frame: shadows at the sensor's black level (red offset 6), grey soil/walls under
        # a reddish cast (R:G:B = 4:2:1), and as many saturated orange/yellow wire pixels
        shadows = [(6, 0, 0)] * 100
        grey = [(6 + 4 * v, 2 * v, v) for v in range(2, 22)] * 30
        wires = [(96, 40, 8), (90, 80, 6)] * 300
        black, gains = _camera.white_balance(shadows + grey + wires)
        self.assertEqual(black, [6, 0, 0])
        for p in grey[:20]:  # every grey level comes out neutral
            q = [(p[i] - black[i]) * gains[i] for i in range(3)]
            self.assertLess(max(q) - min(q), 0.1 * sum(q) / 3 + 0.5, (q, gains))

    def test_falls_back_to_mjpeg(self):
        path, _ = self.grab('night', failing_formats=('yuyv422',))
        self.assertTrue(self.info['night'])
        self.assertEqual(self.info['input_format'], 'mjpeg')
        self.assertEqual(self.formats, ['mjpeg', 'yuyv422', 'mjpeg'])

    def test_snapshot_info_saved_and_pruned(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        async def capture(path):
            with open(path, 'wb') as f:
                f.write(b'jpeg')
            return {'night': True, 'luma': 12.5}

        ts = asyncio.run(_camera.take_snapshot(tmp.name, capture))
        with open(os.path.join(tmp.name, f'{ts}.json')) as f:
            self.assertEqual(json.load(f), {'night': True, 'luma': 12.5})
        _camera.prune(ts + 10, tmp.name, retention_s=5)
        self.assertEqual(os.listdir(tmp.name), [])

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
        self.assertEqual(self.info['controls'], 'frame rate may drop')
        self.assertIn('exposure_time_absolute', self.info['readback'])
        night = calls.index((_camera.CID_EXPOSURE_AUTO_PRIORITY, 1))
        # auto exposure stays on: no manual exposure or gain (the C270's manual mode is darker)
        self.assertNotIn((_camera.CID_EXPOSURE_AUTO, _camera.EXPOSURE_MANUAL), calls)
        self.assertEqual([c for c, _ in calls[night:]].count(_camera.CID_GAIN), 1)  # only the reset
        self.assertEqual(calls[-4:], [(_camera.CID_EXPOSURE_AUTO, 3), (_camera.CID_EXPOSURE_AUTO_PRIORITY, 0),
                                      (_camera.CID_GAIN, 0), (_camera.CID_EXPOSURE_ABSOLUTE, 250)])  # back to auto

    def test_night_failure_keeps_normal_snapshot(self):
        with patch.object(_camera, 'night_filter', lambda: 'nonexistent_filter'):
            path, log = self.grab('night')
        self.assertIn('night capture failed', log)
        self.assertFalse(self.info['night'])
        self.assertIn('night capture', self.info['error'])
        self.assertLess(self.luma(path), _camera.NIGHT_LUMA)

    def test_brightness_check_failure_keeps_normal_snapshot(self):
        async def broken(path):
            raise TimeoutError

        with patch.object(_camera, 'mean_luma', broken):
            path, log = self.grab('day')
        self.assertIn('brightness check failed', log)
        self.assertGreater(os.path.getsize(path), 0)

    def test_snapshot_taken_right_after_launch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, f'{int(time.time()) - 5}.jpg'), 'wb') as f:
            f.write(b'recent')  # a recent snapshot no longer delays the first one
        shots = []

        async def capture(path):
            shots.append(time.time())
            with open(path, 'wb') as f:
                f.write(b'jpeg')

        async def run():
            task = asyncio.create_task(_camera.capture_task(tmp.name, 600, capture))
            await asyncio.sleep(0.5)
            task.cancel()

        asyncio.run(run())
        self.assertEqual(len(shots), 1)
        self.assertEqual(len(_camera.timestamps(tmp.name)), 2)

    def test_next_delay_skips_boundary_right_after_startup_shot(self):
        self.assertEqual(_camera._next_delay(600, 1200 + 100), 500)
        self.assertEqual(_camera._next_delay(600, 1200 + 570), 630)  # 30 s to the boundary: skip it

    def test_controls_without_device(self):
        with patch('builtins.print'), _camera.Controls('/nonexistent') as ctl:
            self.assertIsNone(ctl.query(_camera.CID_GAIN))
            self.assertEqual(ctl.night(), 'no controls')
            ctl.reset()


if __name__ == '__main__':
    unittest.main()
