import json
import os
from dataclasses import asdict

from _packet import SensorReport
from _reconciler import ReconcilerCommand

current_target_path = './data/store.jsonl'

if not os.path.exists('./data'):
    os.makedirs('./data')

def add_segment(data: tuple[SensorReport, ReconcilerCommand], path: str = current_target_path) -> None:
    # if file is not exist, create it
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            pass

    # first, read the len
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
        if len(lines) > 12 * 30: # 30 minutes at 5s interval
            # seperate to new file
            timestamp = int(json.loads(lines[-1])['timestamp'])

            # rename
            os.rename(path, f'./data/store_{timestamp}.jsonl')

            # recreate
            with open(path, 'w', encoding='utf-8') as f:
                pass

    with open(path, 'a', encoding='utf-8') as f:
        json_line = json.dumps({
            'sensor_report': data[0].snapshot(),
            'reconciler_command': asdict(data[1]),
            'timestamp': data[0].received_at
        }, ensure_ascii=False, allow_nan=False)
        f.write(json_line + '\n')

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