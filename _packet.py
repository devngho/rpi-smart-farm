import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from math import isfinite

import aioserial

KIND_SENSOR_REPORT = 0
UNO_FIELDS = ('moisture', 'temp_inner', 'humd_inner', 'temp_outer', 'humd_outer')
log = logging.getLogger(__name__)


@dataclass
class SensorReport:
    moisture: int
    temp_inner: float | None
    humd_inner: float | None
    temp_outer: float | None
    humd_outer: float | None
    illumination: float | None = None
    received_at: float = field(default_factory=time.time, init=False)
    last_valid_at: dict[str, float | None] = field(init=False)

    def __post_init__(self):
        self.received_monotonic = time.monotonic()
        self.last_valid_at = {key: self.received_at if getattr(self, key) is not None else None
                              for key in UNO_FIELDS}

    def keep_history(self, previous: 'SensorReport'):
        for key in UNO_FIELDS:
            if self.last_valid_at[key] is None:
                self.last_valid_at[key] = previous.last_valid_at[key]

    def snapshot(self, stale_after: float | None = None) -> dict:
        # With no deadline this describes validity *at reception* (history).
        stale = stale_after is not None and time.monotonic() - self.received_monotonic >= stale_after
        return {**asdict(self), 'stale': stale,
                'valid': {key: not stale and getattr(self, key) is not None for key in UNO_FIELDS}}


def parse_report(line: bytes) -> SensorReport | None:
    fields = line.strip().split(b',')
    if len(fields) != 6 or fields[0] != b'0':
        return None
    try:
        soil = int(fields[1])
        if not 0 <= soil <= 1023:
            return None
        inner_t, inner_h, outer_t, outer_h = (
            value if isfinite(value) else None for value in map(float, fields[2:])
        )
    except ValueError:
        return None
    return SensorReport(soil, inner_t, inner_h, outer_t, outer_h)


class PacketConnection:
    def __init__(self, ser):
        self.ser = ser
        self.write_lock = asyncio.Lock()
        self.opened_at = time.monotonic()
        self.last_heartbeat: float | None = None
        self.reports: asyncio.Queue[SensorReport] = asyncio.Queue(maxsize=8)

    async def send(self, *fields: int):
        frame = (','.join(map(str, fields)) + '\n').encode('ascii')
        async with self.write_lock:
            if len(fields) > 1 and fields[1] and self.heartbeat_expired():
                raise ConnectionError('Refusing output after UNO heartbeat timeout')
            if await self.ser.write_async(frame) != len(frame):
                raise OSError('incomplete serial write')

    async def stop(self):
        await self.send(0, 0)
        await self.send(1, 0, 1)
        await self.send(2, 0)

    def heartbeat_expired(self) -> bool:
        return time.monotonic() - (self.last_heartbeat or self.opened_at) >= 5

    async def heartbeat(self):
        next_send = self.opened_at + 2
        while True:
            now = time.monotonic()
            deadline = (self.last_heartbeat or self.opened_at) + 5
            if now >= deadline:
                raise ConnectionError('UNO heartbeat timed out')
            if now >= next_send:
                await self.send(9)
                next_send = now + 2
            await asyncio.sleep(max(0, min(next_send, deadline) - time.monotonic()))

    async def receive(self):
        previous = None
        async for line in self.lines():
            if line is None:
                continue
            report = parse_report(line)
            if report is None:
                log.warning('Ignoring malformed UNO frame: %r', line)
                continue
            if previous is not None:
                report.keep_history(previous)
            previous = report
            try:
                self.reports.put_nowait(report)
            except asyncio.QueueFull as exc:
                raise ConnectionError('Control is not keeping up with sensor frames') from exc

    async def lines(self):
        # A short USB read may end mid-frame. Discard oversized lines through LF.
        partial = b''
        dropping = False
        while True:
            chunk = await self.ser.read_async(64)
            parts = (partial + chunk).split(b'\n')
            for line in parts[:-1]:
                if not dropping and len(line) <= 256:
                    if line.strip() == b'9':
                        if self.heartbeat_expired():
                            raise ConnectionError('Late UNO heartbeat')
                        self.last_heartbeat = time.monotonic()
                    else:
                        yield line
                elif not dropping:
                    log.warning('Discarding UNO frame longer than 256 bytes')
                dropping = False
            partial = parts[-1]
            if not dropping and len(partial) > 256:
                log.warning('Discarding UNO frame longer than 256 bytes')
                dropping = True
            if dropping:
                dropping, partial = True, b''
            yield None  # tick safety deadlines even on an idle port


@asynccontextmanager
async def open_link(port: str):
    ser = aioserial.AioSerial(port=port, baudrate=9600, timeout=0.1,
                              write_timeout=0.5, exclusive=True)
    link = PacketConnection(ser)
    try:
        await link.send(9)
        ser.reset_input_buffer()  # never act on a pre-session sensor frame
        await link.stop()
        yield link
    finally:
        try:
            with suppress(OSError):  # unplugged: preserve the original failure/cancellation
                await link.stop()
        finally:
            ser.close()
