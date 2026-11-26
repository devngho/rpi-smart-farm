import json
import os
from dataclasses import asdict

from _packet import SensorReport
from _reconciler import ReconcilerCommand

current_target_path = './data/store.jsonl'

if not os.path.exists('./data'):
    os.makedirs('./data')

ROTATE_AFTER_LINES = 12 * 30  # 30 minutes at 5s interval

# Lines in the active file, counted once and then tracked in memory so that an
# append no longer re-reads the whole file every 5 seconds.
_active_lines: dict[str, int] = {}


def _count_lines(path: str) -> int:
    with open(path, 'rb') as f:
        return sum(chunk.count(b'\n') for chunk in iter(lambda: f.read(1 << 16), b''))


def add_segment(data: tuple[SensorReport, ReconcilerCommand], path: str = current_target_path) -> None:
    if path not in _active_lines:
        _active_lines[path] = _count_lines(path) if os.path.exists(path) else 0

    timestamp = data[0].received_at
    if _active_lines[path] > ROTATE_AFTER_LINES:
        # Rotated name = int(last timestamp in the file); the new record is always later.
        with open(path, 'rb') as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - 4096))
            last = f.read().splitlines()[-1]
        os.rename(path, os.path.join(os.path.dirname(path), f'store_{int(json.loads(last)["timestamp"])}.jsonl'))
        _active_lines[path] = 0

    json_line = json.dumps({
        'sensor_report': data[0].snapshot(),
        'reconciler_command': asdict(data[1]),
        'timestamp': timestamp
    }, ensure_ascii=False, allow_nan=False)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json_line + '\n')
    _active_lines[path] += 1

def list_segments(directory: str = './data') -> list[str]:
    """Return segment files ordered oldest -> newest.

    Rotated files are `store_<timestamp>.jsonl`; the active file is
    `store.jsonl` and is always the newest.
    """
    rotated = []
    active = []
    for filename in os.listdir(directory):
        if filename == 'store.jsonl':
            active.append(os.path.join(directory, filename))
        elif filename.startswith('store_') and filename.endswith('.jsonl'):
            ts = filename[len('store_'):-len('.jsonl')]
            if ts.isdigit():
                rotated.append((int(ts), os.path.join(directory, filename)))
    rotated.sort()
    return [p for _, p in rotated] + active

def last_segments(n: int, directory: str = './data') -> list[str]:
    if n <= 0:
        return []

    files = list_segments(directory)
    segments = []

    # Process files in reverse order (newest first)
    for file in reversed(files):
        with open(file, encoding='utf-8') as f:
            lines = f.readlines()
            # Add lines in reverse order (newest first)
            for line in reversed(lines):
                if not line.strip():
                    continue
                segments.append(line.strip())
                if len(segments) == n:
                    # Reverse to get oldest first and return immediately
                    segments.reverse()
                    return segments

    # If we have fewer than n segments, reverse and return what we have
    segments.reverse()
    return segments