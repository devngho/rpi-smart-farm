import asyncio
import json
import os
from typing import Any

from dataclasses import asdict, dataclass

from _packet import SensorReport
from _reconciler import ReconcilerCommand

current_target_path = './data/store.jsonl'

if not os.path.exists('./data'):
    os.makedirs('./data')

def add_segment(data: tuple[SensorReport, ReconcilerCommand], path: str = current_target_path) -> None:
    # first, read the len
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        if len(lines) > 12 * 30: # 30 minutes at 5s interval
            # seperate to new file
            timestamp = int(json.loads(lines[-1])['sensor_report']['timestamp'])
            path = f'./data/store_{timestamp}.jsonl'

            # rename
            os.rename(path, f'./data/store_{timestamp}.jsonl')

    with open(path, 'a', encoding='utf-8') as f:
        json_line = json.dumps({
            'sensor_report': data[0].dict(),
            'reconciler_command': data[1].dict()
        }, ensure_ascii=False)
        f.write(json_line + '\n')
    
def list_segments(directory: str = './data') -> list[str]:
    files = []
    for filename in os.listdir(directory):
        if filename.startswith('store_') and filename.endswith('.jsonl'):
            files.append(os.path.join(directory, filename))
    return sorted(files)

def last_segments(n: int, directory: str = './data') -> list[str]:
    files = list_segments(directory)

    segments = []

    for file in reversed(files):
        segments.append(file)
        if len(segments) >= n:
            break
    
    if len(segments) < n:
        return segments

    return segments[:n]