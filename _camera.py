"""Periodic webcam snapshots: one JPEG every `INTERVAL_S`, served under /camera.

Capture runs in an ffmpeg subprocess, so it never blocks the control loop, and no
capture failure is allowed to propagate: `main.py` runs this next to serial control.
Files are `<unix seconds>.jpg`; ones older than the retention period are deleted.
"""
import asyncio
import fcntl
import json
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
# with manual long exposure + max gain from uncompressed YUYV frames (MJPEG's 8x8 blocks turn
# into posterized tiles once brightened), average NIGHT_FRAMES frames to cut sensor noise,
# denoise and stretch each colour channel's levels. Daylight snapshots measure ~50-100,
# night ones ~13. What happened is saved next to the JPEG as <ts>.json (GET .../info).
NIGHT_LUMA = float(os.environ.get('SMART_FARM_CAMERA_NIGHT_LUMA', '35'))  # 0 disables night mode
# Exposure in 100 µs units (V4L2 exposure_time_absolute); default: the camera's maximum.
NIGHT_EXPOSURE = int(os.environ.get('SMART_FARM_CAMERA_NIGHT_EXPOSURE', '0'))
NIGHT_WARMUP_FRAMES = 3  # manual exposure needs no convergence, just a couple of frames to apply
NIGHT_FRAMES = 6
NIGHT_TIMEOUT_S = 90  # long exposures run at ~1 fps
NIGHT_MAX_GAIN = 10.0  # cap on the level stretch so pure noise isn't blown up to full contrast
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


def night_filter(warmup: int = NIGHT_WARMUP_FRAMES, frames: int = NIGHT_FRAMES) -> str:
    """Average `frames` frames after `warmup` (sensor noise drops ~sqrt(frames)), then denoise."""
    return (f'select=gte(n\\,{warmup}),tmix=frames={frames},select=gte(n\\,{frames - 1}),'
            'hqdn3d=3:2:3:2')


async def _pixels(path: str, size: str, fmt: str) -> bytes:
    """Downscaled 8-bit pixels of an image (via ffmpeg; no image libraries needed)."""
    return await _ffmpeg(['-i', path, '-vf', f'scale={size},format={fmt}', '-f', 'rawvideo', '-'], 15)


async def mean_luma(path: str) -> float:
    """Average brightness 0-255."""
    raw = await _pixels(path, '64x36', 'gray')
    return sum(raw) / len(raw) if raw else 0.0


async def stretch(src: str, dst: str, low_pct: float = 1.0, high_pct: float = 99.5, gamma: float = 0.8,
                  max_gain: float = NIGHT_MAX_GAIN) -> dict:
    """Map each channel's low/high percentiles to black/white, with mild gamma.

    Per channel, because in the dark the channels' black levels and sensitivities differ a
    lot (one shared stretch gave a strong blue cast); percentiles rather than min/max, so a
    few bright LEDs in view don't cancel the stretch. Returns the levels used."""
    raw = await _pixels(src, '160x90', 'rgb24')
    levels = {}
    for i, name in enumerate('rgb'):
        ch = sorted(raw[i::3])
        lo = ch[int(len(ch) * low_pct / 100)]
        hi = ch[min(len(ch) - 1, int(len(ch) * high_pct / 100))]
        levels[name] = (lo, max(hi, lo + 255 / max_gain))
    exprs = ':'.join(f"{c}='255*pow(clip((val-{lo})/{hi - lo:.2f}\\,0\\,1)\\,{gamma})'"
                     for c, (lo, hi) in levels.items())
    await _ffmpeg(['-i', src, '-vf', f'lutrgb={exprs}', '-q:v', '3', '-f', 'image2', '-y', dst], 30)
    return {c: [lo, round(hi, 1)] for c, (lo, hi) in levels.items()}


async def _night_capture(device: str, out: str) -> dict:
    """Long-exposure, frame-averaged, losslessly stored capture; the camera is set back to auto after."""
    with Controls(device) as ctl:
        try:
            info = {'controls': ctl.night()}
            info['readback'] = ctl.readback()
            errors = []
            for fmt in NIGHT_FORMATS:
                try:
                    await _ffmpeg([*camera_input(device, fmt), '-vf', night_filter(),
                                   '-frames:v', '1', '-f', 'image2', '-y', out], NIGHT_TIMEOUT_S)
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
    stacked, night = path + '.stack.png', path + '.night.jpg'  # PNG: no extra compression before the stretch
    try:
        info.update(await _night_capture(device, stacked))
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


@router.get('/snapshots/{ts}/info')
async def get_snapshot_info(ts: int):
    """스냅샷 촬영 정보 (밝기, 야간 모드 적용 여부, 노출/게인 값 등)"""
    try:
        with open(_info_path(ts), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return JSONResponse(status_code=404, content={'error': 'No info for this snapshot'})
