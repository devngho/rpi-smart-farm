"""Periodic webcam snapshots: one JPEG every `INTERVAL_S`, served under /camera.

Capture runs in an ffmpeg subprocess, so it never blocks the control loop, and no
capture failure is allowed to propagate: `main.py` runs this next to serial control.
Files are `<unix seconds>.jpg`; ones older than the retention period are deleted.
"""
import asyncio
import fcntl
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

# Night mode: when a normal snapshot is darker than NIGHT_LUMA (mean luma, 0-255), retake it
# with manual long exposure + max gain, average NIGHT_FRAMES frames to cut sensor noise,
# denoise and stretch the levels. Daylight snapshots measure ~50-100, night ones ~15.
NIGHT_LUMA = float(os.environ.get('SMART_FARM_CAMERA_NIGHT_LUMA', '35'))  # 0 disables night mode
# Exposure in 100 µs units (V4L2 exposure_time_absolute); default: the camera's maximum.
NIGHT_EXPOSURE = int(os.environ.get('SMART_FARM_CAMERA_NIGHT_EXPOSURE', '0'))
NIGHT_WARMUP_FRAMES = 3  # manual exposure needs no convergence, just a couple of frames to apply
NIGHT_FRAMES = 6
NIGHT_TIMEOUT_S = 90  # long exposures run at ~1 fps

_NAME = re.compile(r'(\d+)\.jpg')


def timestamps(directory: str = SNAPSHOT_DIR) -> list[int]:
    """Stored snapshot times, oldest first."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    return sorted(int(m[1]) for m in map(_NAME.fullmatch, names) if m)


def prune(now: float, directory: str = SNAPSHOT_DIR, retention_s: float = RETENTION_S) -> None:
    if retention_s <= 0:
        return
    for ts in timestamps(directory):
        if ts < now - retention_s:
            with suppress(FileNotFoundError):
                os.remove(os.path.join(directory, f'{ts}.jpg'))


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

    def night(self, exposure: int = NIGHT_EXPOSURE) -> str:
        """Manual long exposure and maximum gain. Returns a short description for the log."""
        done = []
        if self.set(CID_EXPOSURE_AUTO, EXPOSURE_MANUAL):
            q = self.query(CID_EXPOSURE_ABSOLUTE)
            if q is not None:
                value = min(max(exposure or q[1], q[0]), q[1])
                if self.set(CID_EXPOSURE_ABSOLUTE, value):
                    done.append(f'exposure {value / 10:g} ms')
        elif self.set(CID_EXPOSURE_AUTO_PRIORITY, 1):
            done.append('auto exposure may lower the frame rate')
        q = self.query(CID_GAIN)
        if q is not None and self.set(CID_GAIN, q[1]):
            done.append(f'gain {q[1]}')
        return ', '.join(done) or 'no controls'


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


def camera_input(device: str = DEVICE) -> list[str]:
    return ['-f', 'v4l2', '-input_format', 'mjpeg', '-video_size', '1280x720', '-i', device]


def night_filter(warmup: int = NIGHT_WARMUP_FRAMES, frames: int = NIGHT_FRAMES) -> str:
    """Average `frames` frames after `warmup` (sensor noise drops ~sqrt(frames)), then denoise."""
    return (f'select=gte(n\\,{warmup}),tmix=frames={frames},select=gte(n\\,{frames - 1}),'
            'hqdn3d=3:2:3:2')


async def _gray(path: str, size: str = '160x90') -> bytes:
    """Downscaled 8-bit grey pixels of a JPEG (via ffmpeg; no image libraries needed)."""
    return await _ffmpeg(['-i', path, '-vf', f'scale={size},format=gray', '-f', 'rawvideo', '-'], 15)


async def mean_luma(path: str) -> float:
    """Average brightness 0-255."""
    raw = await _gray(path, '64x36')
    return sum(raw) / len(raw) if raw else 0.0


async def stretch(src: str, dst: str, low_pct: float = 1.0, high_pct: float = 99.5, gamma: float = 0.8) -> None:
    """Map the low/high brightness percentiles to black/white (same for R, G, B), with mild gamma.
    Percentiles rather than min/max, so a few bright LEDs in view don't cancel the stretch."""
    raw = sorted(await _gray(src))
    lo = raw[int(len(raw) * low_pct / 100)]
    hi = max(raw[min(len(raw) - 1, int(len(raw) * high_pct / 100))], lo + 8)
    expr = f"'255*pow(clip((val-{lo})/{hi - lo}\\,0\\,1)\\,{gamma})'"
    await _ffmpeg(['-i', src, '-vf', f'lutrgb=r={expr}:g={expr}:b={expr}',
                   '-q:v', '4', '-f', 'image2', '-y', dst], 30)


async def grab(path: str, device: str = DEVICE) -> None:
    """Write one JPEG from the webcam to `path`; retake dark (night) frames with long exposure."""
    with Controls(device) as ctl:
        ctl.reset()  # never start from a manual exposure left behind by an interrupted night capture
    await _ffmpeg([*camera_input(device), '-vf', f'select=gte(n\\,{WARMUP_FRAMES})',
                   '-frames:v', '1', '-q:v', '4', '-f', 'image2', '-y', path], TIMEOUT_S)
    if NIGHT_LUMA <= 0:
        return
    luma = await mean_luma(path)
    if luma >= NIGHT_LUMA:
        return
    stacked, night = path + '.stack.jpg', path + '.night.jpg'
    try:
        with Controls(device) as ctl:
            try:
                how = ctl.night()
                await _ffmpeg([*camera_input(device), '-vf', night_filter(),
                               '-frames:v', '1', '-q:v', '2', '-f', 'image2', '-y', stacked], NIGHT_TIMEOUT_S)
            finally:
                ctl.reset()
        await stretch(stacked, night)
        os.replace(night, path)
        print(f'Camera night capture (mean luma {luma:.0f}): {how}')
    except Exception as exc:  # keep the normal (dark) snapshot
        print(f'Camera night capture failed, keeping the normal snapshot: {exc!r}')
    finally:
        for tmp in (stacked, night):
            with suppress(FileNotFoundError):
                os.remove(tmp)


async def take_snapshot(directory: str = SNAPSHOT_DIR, capture=grab) -> int:
    os.makedirs(directory, exist_ok=True)
    ts = int(time.time())
    path = os.path.join(directory, f'{ts}.jpg')
    tmp = path + '.tmp'
    try:
        await capture(tmp)
        if not os.path.getsize(tmp):
            raise RuntimeError('empty snapshot')
        os.replace(tmp, path)  # the API never sees a half-written file
    finally:
        with suppress(FileNotFoundError):
            os.remove(tmp)
    return ts


async def capture_task(directory: str = SNAPSHOT_DIR, interval_s: float = INTERVAL_S, capture=grab):
    """Take a snapshot now (unless a recent one exists), then on every interval boundary."""
    delay = 0.0
    try:
        stored = timestamps(directory)
        if stored and 0 <= time.time() - stored[-1] < interval_s:
            delay = interval_s - time.time() % interval_s
    except OSError as exc:
        print(f'Camera snapshot directory unavailable: {exc}')
    while True:
        await asyncio.sleep(delay)
        try:
            await take_snapshot(directory, capture)
            await asyncio.to_thread(prune, time.time(), directory)
        except Exception as exc:  # unplugged camera, busy device, full disk... retry next time
            print(f'Camera snapshot failed: {exc!r}')
        delay = interval_s - time.time() % interval_s


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
