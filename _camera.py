"""Periodic webcam snapshots: one JPEG every `INTERVAL_S`, served under /camera.

Capture runs in an ffmpeg subprocess, so it never blocks the control loop, and no
capture failure is allowed to propagate: `main.py` runs this next to serial control.
Files are `<unix seconds>.jpg`; ones older than the retention period are deleted.
"""
import asyncio
import fcntl
import json
import math
import os
import re
import struct
import time
from contextlib import suppress

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

SNAPSHOT_DIR = './data/camera'
DEVICE = os.environ.get('SMART_FARM_CAMERA',
                        '/dev/v4l/by-id/usb-046d_C270_HD_WEBCAM_200901010001-video-index0')
INTERVAL_S = 600
RETENTION_S = float(os.environ.get('SMART_FARM_CAMERA_RETENTION_DAYS', '30')) * 86400  # 0 keeps everything
TIMEOUT_S = 40
WARMUP_FRAMES = 30  # auto exposure / white balance need a moment after the stream starts

# Night mode: when a normal snapshot is darker than NIGHT_LUMA (mean luma, 0-255), take
# prod(NIGHT_STAGES) more frames and average them at 16 bits per channel; random sensor noise
# drops ~sqrt(frames) (measured: 48 frames 6.8x, 192 frames 13x). The camera and scene are
# static, so more frames only cost capture time (~5-6 fps at night: 192 frames ~35 s).
# Then white-balance, stretch and sharpen.
# The camera stays in auto exposure: the C270 accepts manual exposure values but only honours a
# few discrete ones, and its manual mode came out far darker than auto (21:33 test: luma 0.4 vs
# 7.2). It may only lower its frame rate so auto exposure can run longer.
# Daylight snapshots measure ~50-100, night ones ~7-13. What happened is saved next to the JPEG
# as <ts>.json (GET .../info).
NIGHT_LUMA = float(os.environ.get('SMART_FARM_CAMERA_NIGHT_LUMA', '35'))  # 0 disables night mode
NIGHT_WARMUP_FRAMES = 15  # auto exposure settles after the frame-rate change
# Averaged in stages (means of 8, then means of 8 of those, then of 3): tmix re-mixes its
# whole window for every frame, so one 192-frame window would cost ~25x more CPU and hold
# 192 16-bit frames (~1 GB) in memory; staged, at most ~19 frames (~100 MB).
NIGHT_STAGES = tuple(int(n) for n in os.environ.get('SMART_FARM_CAMERA_NIGHT_STAGES', '8,8,3').split(','))
NIGHT_TIMEOUT_S = 180     # 207 frames at the low frame rates auto exposure picks in the dark
NIGHT_MAX_GAIN = 12.0     # cap on the level stretch so the remaining noise isn't blown up
NIGHT_FORMATS = ('yuyv422', 'mjpeg')  # first one the camera accepts is used

_NAME = re.compile(r'(\d+)\.jpg')


def timestamps(directory: str = SNAPSHOT_DIR) -> list[int]:
    """Stored snapshot times, oldest first."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    return sorted(int(m[1]) for m in map(_NAME.fullmatch, names) if m)


def _info_path(ts: int, directory: str = SNAPSHOT_DIR) -> str:
    return os.path.join(directory, f'{ts}.json')


def prune(now: float, directory: str = SNAPSHOT_DIR, retention_s: float = RETENTION_S) -> None:
    if retention_s <= 0:
        return
    for ts in timestamps(directory):
        if ts < now - retention_s:
            for path in (os.path.join(directory, f'{ts}.jpg'), _info_path(ts, directory)):
                with suppress(FileNotFoundError):
                    os.remove(path)


# ---- V4L2 camera controls (plain ioctls, no v4l-utils needed) ----
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_QUERYCTRL = 0xC0445624
CID_GAIN = 0x00980913
CID_EXPOSURE_AUTO = 0x009A0901       # 1 = manual, 3 = aperture priority (UVC auto)
CID_EXPOSURE_ABSOLUTE = 0x009A0902   # 100 µs units
CID_EXPOSURE_AUTO_PRIORITY = 0x009A0903  # 1 = auto exposure may lower the frame rate
EXPOSURE_MANUAL = 1
_QUERYCTRL = struct.Struct('II32siiiiI8x')
_CTRL = struct.Struct('Ii')


class Controls:
    """Best-effort access to a few camera controls; unsupported ones are skipped."""

    def __init__(self, device: str = DEVICE):
        try:
            self.fd: int | None = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            print(f'Camera controls unavailable: {exc}')
            self.fd = None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def query(self, cid: int) -> tuple[int, int, int] | None:
        """(minimum, maximum, default) or None when the camera lacks the control."""
        if self.fd is None:
            return None
        buf = bytearray(_QUERYCTRL.pack(cid, 0, b'', 0, 0, 0, 0, 0))
        try:
            fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, buf)
        except OSError:
            return None
        _, _, _, lo, hi, _, default, _ = _QUERYCTRL.unpack(buf)
        return lo, hi, default

    def get(self, cid: int) -> int | None:
        if self.fd is None:
            return None
        buf = bytearray(_CTRL.pack(cid, 0))
        try:
            fcntl.ioctl(self.fd, VIDIOC_G_CTRL, buf)
        except OSError:
            return None
        return _CTRL.unpack(buf)[1]

    def set(self, cid: int, value: int) -> bool:
        if self.fd is None:
            return False
        try:
            fcntl.ioctl(self.fd, VIDIOC_S_CTRL, bytearray(_CTRL.pack(cid, value)))
            return True
        except OSError:
            return False

    def reset(self, cids=(CID_EXPOSURE_AUTO, CID_EXPOSURE_AUTO_PRIORITY, CID_GAIN, CID_EXPOSURE_ABSOLUTE)):
        """Back to the camera's defaults (auto exposure), e.g. after an interrupted night capture."""
        for cid in cids:
            q = self.query(cid)
            if q is not None:
                self.set(cid, q[2])

    def night(self) -> str:
        """Let auto exposure lower the frame rate (longer exposures). Returns a short description."""
        return 'frame rate may drop' if self.set(CID_EXPOSURE_AUTO_PRIORITY, 1) else 'no controls'

    def readback(self) -> dict:
        """Current values as the camera reports them (None = unavailable), for diagnostics."""
        return {name: self.get(cid) for name, cid in (
            ('auto_exposure', CID_EXPOSURE_AUTO), ('exposure_time_absolute', CID_EXPOSURE_ABSOLUTE),
            ('exposure_dynamic_framerate', CID_EXPOSURE_AUTO_PRIORITY), ('gain', CID_GAIN))}


# ---- capture ----
async def _ffmpeg(args: list[str], timeout: float) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        if proc.returncode is None:  # timed out or cancelled: release the camera
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise RuntimeError(f'ffmpeg exited {proc.returncode}: {err.decode(errors="replace").strip()}')
    return out


def camera_input(device: str = DEVICE, input_format: str = 'mjpeg') -> list[str]:
    return ['-f', 'v4l2', '-input_format', input_format, '-video_size', '1280x720', '-i', device]


def night_frames(stages: tuple[int, ...] | None = None) -> int:
    return math.prod(stages or NIGHT_STAGES)


def night_filter(warmup: int = NIGHT_WARMUP_FRAMES, stages: tuple[int, ...] | None = None) -> str:
    """Average prod(stages) frames after `warmup` at 16 bits per channel, then denoise lightly.

    Each stage averages `n` of the previous stage's outputs and keeps every n-th result, so
    CPU stays linear in the frame count and memory bounded by sum(stages) frames."""
    stages = stages or NIGHT_STAGES
    chain = [f'select=gte(n\\,{warmup})', 'format=gbrp16le']
    for i, n in enumerate(stages):
        chain.append(f'tmix=frames={n}')
        # keep only complete windows; after the last stage, the first complete one is the result
        chain.append(f'select=gte(n\\,{n - 1})' if i == len(stages) - 1
                     else f'select=eq(mod(n\\,{n})\\,{n - 1})')
    chain.append('hqdn3d=2:1.5:3:3')  # light: stacking already removed most noise, keep detail
    return ','.join(chain)


async def _pixels(path: str, size: str, fmt: str) -> bytes:
    """Downscaled 8-bit pixels of an image (via ffmpeg; no image libraries needed)."""
    return await _ffmpeg(['-i', path, '-vf', f'scale={size},format={fmt}', '-f', 'rawvideo', '-'], 15)


async def mean_luma(path: str) -> float:
    """Average brightness 0-255."""
    raw = await _pixels(path, '64x36', 'gray')
    return sum(raw) / len(raw) if raw else 0.0


def _pct(values, pct: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * pct / 100))]


def white_balance(px: list[tuple[int, int, int]], grey_tol: float = 0.3, rounds: int = 8,
                  bin_size: float = 0.025):
    """(black, gains) for 8-bit RGB pixels: per-channel black points (the darkest shadows),
    then gains that make the scene's dominant colour (soil, perlite, walls) neutral.

    Plain grey-world (all pixels) is pulled off by the many orange/yellow wires, and stretching
    each channel to its own white point boosted blue's midtones (blue cast). So: find the most
    common chromaticity among lit pixels (2-D histogram of r/sum, g/sum, smoothed 3x3), take it
    as grey, then refine on pixels whose balanced colour is within `grey_tol`."""
    black = [_pct([p[i] for p in px], 0.5) for i in range(3)]
    x = [[max(0.0, p[i] - black[i]) for i in range(3)] for p in px]
    lit = [q for q in x if sum(q) > 6]
    if len(lit) < 20:
        return black, [1.0, 1.0, 1.0]
    hist: dict[tuple[int, int], list] = {}
    for q in lit:
        t = sum(q)
        hist.setdefault((int(q[0] / t / bin_size), int(q[1] / t / bin_size)), []).append(q)
    density = {k: sum(len(hist.get((k[0] + i, k[1] + j), ())) for i in (-1, 0, 1) for j in (-1, 0, 1))
               for k in hist}
    peak = max(density, key=density.get)
    mode = [q for i in (-1, 0, 1) for j in (-1, 0, 1) for q in hist.get((peak[0] + i, peak[1] + j), ())]

    def neutralise(sample):
        means = [max(sum(q[i] for q in sample) / len(sample), 1e-3) for i in range(3)]
        return [sum(means) / 3 / m for m in means]

    gains = neutralise(mode)
    for _ in range(rounds):
        grey = []
        for q in lit:
            bal = [q[i] * gains[i] for i in range(3)]
            if max(bal) - min(bal) < grey_tol * sum(bal) / 3:
                grey.append(q)
        if len(grey) < len(lit) // 50:  # too few neutral pixels to trust: keep the current gains
            break
        gains = neutralise(grey)
    return black, [min(max(g, 1 / 3), 3.0) for g in gains]


async def stretch(src: str, dst: str, high_pct: float = 99.5, gamma: float = 0.85,
                  max_gain: float = NIGHT_MAX_GAIN, saturation: float = 1.15, sharpen: float = 0.8) -> dict:
    """Colour-balance and brighten a dark frame, then sharpen it.

    Black point per channel, gains from `white_balance`, one shared white point (the
    `high_pct` brightness percentile, so a few bright LEDs in view don't cancel the stretch),
    mild gamma; then a saturation boost and a luma-only unsharp mask (chroma noise untouched).
    Measured in 8-bit units; `maxval` applies the same curve to 16-bit stacks (no banding)."""
    raw = await _pixels(src, '160x90', 'rgb24')
    px = [tuple(raw[i:i + 3]) for i in range(0, len(raw) - 2, 3)]
    black, gains = white_balance(px)
    balanced = [sum(max(0.0, p[i] - black[i]) * gains[i] for i in range(3)) / 3 for p in px]
    white = max(_pct(balanced, high_pct), 255 / max_gain)
    exprs = ':'.join(
        f"{c}='maxval*pow(clip((val*255/maxval-{black[i]})*{gains[i] / white:.5f}\\,0\\,1)\\,{gamma})'"
        for i, c in enumerate('rgb'))
    vf = f'lutrgb={exprs},eq=saturation={saturation},unsharp=5:5:{sharpen}:5:5:0'
    await _ffmpeg(['-i', src, '-vf', vf, '-q:v', '3', '-f', 'image2', '-y', dst], 30)
    return {'black': black, 'gains': [round(g, 3) for g in gains], 'white': round(white, 1)}


async def _night_capture(device: str, out: str) -> dict:
    """Long-exposure, frame-averaged, losslessly stored capture; the camera is set back to auto after."""
    with Controls(device) as ctl:
        try:
            info = {'controls': ctl.night()}
            info['readback'] = ctl.readback()
            errors = []
            for fmt in NIGHT_FORMATS:
                try:
                    started = time.monotonic()
                    await _ffmpeg([*camera_input(device, fmt), '-vf', night_filter(),
                                   '-frames:v', '1', '-f', 'image2', '-y', out], NIGHT_TIMEOUT_S)
                    # longer per frame when auto exposure lowers the frame rate
                    info['capture_s'] = round(time.monotonic() - started, 1)
                    info['frames'] = NIGHT_WARMUP_FRAMES + night_frames()
                    info['input_format'] = fmt
                    return info
                except RuntimeError as exc:  # e.g. format unsupported: try the next one
                    errors.append(f'{fmt}: {exc}')
            raise RuntimeError('; '.join(errors))
        finally:
            ctl.reset()


async def grab(path: str, device: str = DEVICE) -> dict:
    """Write one JPEG from the webcam to `path`; retake dark (night) frames with long exposure.
    Returns what happened (saved as the snapshot's info)."""
    with Controls(device) as ctl:
        ctl.reset()  # never start from a manual exposure left behind by an interrupted night capture
    await _ffmpeg([*camera_input(device), '-vf', f'select=gte(n\\,{WARMUP_FRAMES})',
                   '-frames:v', '1', '-q:v', '4', '-f', 'image2', '-y', path], TIMEOUT_S)
    info: dict = {'night': False}
    if NIGHT_LUMA <= 0:
        return info
    try:
        info['luma'] = round(await mean_luma(path), 1)
    except Exception as exc:  # night mode is best effort: never lose the normal snapshot
        print(f'Camera brightness check failed, keeping the normal snapshot: {exc!r}')
        info['error'] = f'brightness check: {exc!r}'
        return info
    if info['luma'] >= NIGHT_LUMA:
        return info
    stacked, night = path + '.stack.png', path + '.night.jpg'  # 16-bit PNG: no compression or banding
    try:
        info.update(await _night_capture(device, stacked))
        info['luma_stacked'] = round(await mean_luma(stacked), 1)
        if info['luma_stacked'] < 0.7 * info['luma']:
            # the stack came out clearly darker than the normal shot: brighten that instead
            info['source'] = 'normal'
            info['levels'] = await stretch(path, night)
        else:
            info['source'] = 'stacked'
            info['levels'] = await stretch(stacked, night)
        info['luma_after'] = round(await mean_luma(night), 1)
        os.replace(night, path)
        info['night'] = True
        print(f'Camera night capture: {info}')
    except Exception as exc:  # keep the normal (dark) snapshot
        print(f'Camera night capture failed, keeping the normal snapshot: {exc!r}')
        info['error'] = f'night capture: {exc!r}'
    finally:
        for tmp in (stacked, night):
            with suppress(FileNotFoundError):
                os.remove(tmp)
    return info


async def take_snapshot(directory: str = SNAPSHOT_DIR, capture=grab) -> int:
    os.makedirs(directory, exist_ok=True)
    ts = int(time.time())
    path = os.path.join(directory, f'{ts}.jpg')
    tmp = path + '.tmp'
    try:
        info = await capture(tmp)
        if not os.path.getsize(tmp):
            raise RuntimeError('empty snapshot')
        if isinstance(info, dict):
            with suppress(OSError, TypeError, ValueError), open(_info_path(ts, directory), 'w') as f:
                json.dump(info, f)
        os.replace(tmp, path)  # the API never sees a half-written file
    finally:
        with suppress(FileNotFoundError):
            os.remove(tmp)
    return ts


MIN_GAP_S = 60  # don't take a boundary snapshot right after the startup one


def _next_delay(interval_s: float, now: float) -> float:
    """Seconds until the next interval boundary, skipping one that is less than MIN_GAP_S away."""
    delay = interval_s - now % interval_s
    return delay + interval_s if delay < min(MIN_GAP_S, interval_s / 2) else delay


async def capture_task(directory: str = SNAPSHOT_DIR, interval_s: float = INTERVAL_S, capture=grab):
    """Take a snapshot right away (so a restart shows its effect at once), then on every interval boundary."""
    while True:
        try:
            await take_snapshot(directory, capture)
            await asyncio.to_thread(prune, time.time(), directory)
        except Exception as exc:  # unplugged camera, busy device, full disk... retry next time
            print(f'Camera snapshot failed: {exc!r}')
        await asyncio.sleep(_next_delay(interval_s, time.time()))


router = APIRouter(prefix='/camera')


def _jpeg(ts: int, cache: str):
    path = os.path.join(SNAPSHOT_DIR, f'{ts}.jpg')
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={'error': 'Snapshot not found'})
    return FileResponse(path, media_type='image/jpeg',
                        headers={'Cache-Control': cache, 'X-Snapshot-Timestamp': str(ts)})


@router.get('/snapshots')
async def list_snapshots():
    """저장된 스냅샷 시각(Unix 초) 목록, 오래된 순"""
    return {'interval': INTERVAL_S, 'timestamps': await asyncio.to_thread(timestamps)}


@router.get('/latest')
async def latest_snapshot():
    """가장 최근 스냅샷 JPEG"""
    stored = await asyncio.to_thread(timestamps)
    if not stored:
        return JSONResponse(status_code=404, content={'error': 'No snapshot yet'})
    return _jpeg(stored[-1], 'no-cache')


@router.get('/snapshots/{ts}')
async def get_snapshot(ts: int):
    """지정한 시각의 스냅샷 JPEG"""
    return _jpeg(ts, 'max-age=86400')


@router.get('/snapshots/{ts}/info')
async def get_snapshot_info(ts: int):
    """스냅샷 촬영 정보 (밝기, 야간 모드 적용 여부, 노출/게인 값 등)"""
    try:
        with open(_info_path(ts), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return JSONResponse(status_code=404, content={'error': 'No info for this snapshot'})
