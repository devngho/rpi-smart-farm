"""Periodic webcam snapshots: one JPEG every `INTERVAL_S`, served under /camera.

Capture runs in an ffmpeg subprocess, so it never blocks the control loop, and no
capture failure is allowed to propagate: `main.py` runs this next to serial control.
Files are `<unix seconds>.jpg`; ones older than the retention period are deleted.
"""
import asyncio
import os
import re
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


async def grab(path: str) -> None:
    """Write one JPEG from the webcam to `path`."""
    proc = await asyncio.create_subprocess_exec(
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-f', 'v4l2', '-input_format', 'mjpeg', '-video_size', '1280x720', '-i', DEVICE,
        '-vf', f'select=gte(n\\,{WARMUP_FRAMES})', '-frames:v', '1', '-q:v', '4', '-f', 'image2', '-y', path,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
    finally:
        if proc.returncode is None:  # timed out or cancelled: release the camera
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise RuntimeError(f'ffmpeg exited {proc.returncode}: {err.decode(errors="replace").strip()}')


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
