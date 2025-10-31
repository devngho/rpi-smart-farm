from __future__ import annotations
import asyncio
import aioserial
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Union, Callable

from _illumination import read_light

# kinds
KIND_SENSOR_REPORT = 0   # Arduino->RPi
KIND_HEARTBEAT = 9       # both
KIND_CMD_PUMP = 0        # RPi->Arduino
KIND_CMD_PELTIER = 1
KIND_CMD_FANS = 2

@dataclass
class SensorReport:
    moisture: int
    temp_inner: int
    humd_inner: int
    temp_outer: int
    humd_outer: int
    illumination: Optional[float] = None

@dataclass
class Heartbeat:
    pass

ParsedPayload = Union[SensorReport, Heartbeat, tuple]

class PacketConnection:
    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 5.0, encoding: str = "ascii", 
                 heartbeat_timeout: float = 10.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.encoding = encoding
        self.heartbeat_timeout = heartbeat_timeout  # 하트비트 없이 이 시간이 지나면 연결 끊김으로 간주
        self.ser: Optional[aioserial.AioSerial] = None
        
        # 내부 상태
        self._reader_task: Optional[asyncio.Task] = None
        self._packet_queue: asyncio.Queue = asyncio.Queue()
        self._last_heartbeat_time: float = 0
        self._is_alive: bool = False

        self._heartbeat_task: Optional[asyncio.Task] = None

    # ---- lifecycle ----
    async def open(self):
        if self.ser:
            return
        print(f"Opening serial port: {self.port} at {self.baudrate} baud")
        self.ser = aioserial.AioSerial(
            port=self.port, 
            baudrate=self.baudrate,
            timeout=1,
            write_timeout=1
        )
        print(f"Serial port opened: {self.ser.is_open}")
        await asyncio.sleep(0.5)  # 초기화 대기 시간 증가
        
        # 버퍼 비우기 (중요!)
        if hasattr(self.ser, 'reset_input_buffer'):
            self.ser.reset_input_buffer()
        if hasattr(self.ser, 'reset_output_buffer'):
            self.ser.reset_output_buffer()
        
        print("Serial buffers cleared")
        
        # 백그라운드 리더 시작
        self._last_heartbeat_time = time.monotonic()
        self._is_alive = True
        self._reader_task = asyncio.create_task(self._background_reader())

        self._heartbeat_task = asyncio.create_task(self._heartbeat_sender())

    async def close(self):
        # 백그라운드 태스크 정리
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        
        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None
        
        self._is_alive = False

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    def is_alive(self) -> bool:
        """연결이 살아있는지 확인 (하트비트 기반)"""
        if not self.ser or not self._is_alive:
            return False
        
        # 마지막 하트비트 이후 시간 확인
        elapsed = time.monotonic() - self._last_heartbeat_time
        return elapsed < self.heartbeat_timeout

    async def _background_reader(self):
        """백그라운드에서 계속 패킷을 읽어서 큐에 저장"""
        print("Background reader started")
        read_count = 0
        
        while True:
            try:
                if not self.ser:
                    print("Serial port not available, stopping reader")
                    break
                
                # readline_async 대신 read 시도
                raw = await self.ser.readline_async()
                
                if not raw:
                    await asyncio.sleep(0.01)
                    continue
                
                read_count += 1
                print(f"[{read_count}] Raw data received ({len(raw)} bytes): {raw}")
                
                try:
                    line = raw.decode(self.encoding, errors="ignore").strip()
                    print(f"[{read_count}] Decoded line: '{line}'")
                except UnicodeError as e:
                    print(f"[{read_count}] Decode error: {e}")
                    continue
                
                if not line:
                    print(f"[{read_count}] Empty line after strip")
                    continue
                
                parsed = self._parse_line(line)
                if parsed is None:
                    print(f"[{read_count}] Failed to parse line: '{line}'")
                    continue
                
                kind, payload = parsed
                print(f"[{read_count}] Parsed - kind: {kind}, payload: {payload}")
                
                # 하트비트는 타임스탬프만 업데이트하고 큐에는 넣지 않음 (선택사항)
                if kind == KIND_HEARTBEAT:
                    self._last_heartbeat_time = time.monotonic()
                    print(f"[{read_count}] Heartbeat received")
                    continue
                
                # 센서 리포트나 다른 패킷은 큐에 저장
                if kind == KIND_SENSOR_REPORT:
                    report = self._decode_sensor_report(payload)
                    print(f"[{read_count}] Sensor report decoded: {report}")
                    await self._packet_queue.put((kind, report))
                else:
                    print(f"[{read_count}] Other packet type: {kind}")
                    await self._packet_queue.put((kind, tuple(payload)))
                    
            except asyncio.CancelledError:
                print("Background reader cancelled")
                break
            except Exception as e:
                print(f"[{read_count}] Exception in background reader: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(0.1)
                continue
        
        print("Background reader stopped")
        
    async def _heartbeat_sender(self):
        """주기적으로 하트비트 전송"""
        while True:
            try:
                await self.send_heartbeat()
                await asyncio.sleep(self.heartbeat_timeout / 2)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1.0)
                continue

    # ---- send helpers (RPi -> Arduino) ----
    async def send_heartbeat(self):
        await self._send_fields([str(KIND_HEARTBEAT)])

    async def send_pump(self, level: int):
        if not (0 <= level <= 1023):
            raise ValueError("pump level must be 0..1023")
        fields = [str(KIND_CMD_PUMP), str(level)]
        await self._send_fields(fields)

    async def send_peltier(self, level: int, is_forward: int):
        if not (0 <= level <= 1023):
            raise ValueError("peltier level must be 0..1023")
        if is_forward not in (0, 1):
            raise ValueError("is_forward must be 0 or 1")
        fields = [str(KIND_CMD_PELTIER), str(level), str(is_forward)]
        await self._send_fields(fields)

    async def send_fans(self, level: int):
        if not (0 <= level <= 1023):
            raise ValueError("fans level must be 0..1023")
        fields = [str(KIND_CMD_FANS), str(level)]
        await self._send_fields(fields)

    async def _send_fields(self, fields):
        if not self.ser:
            raise RuntimeError("serial not open")
        line = ",".join(fields) + "\n"
        await self.ser.write_async(line.encode(self.encoding, errors="strict"))

    # ---- receive (Arduino -> RPi) ----
    async def read_packet(self, deadline_sec: float = 1.0) -> Optional[Tuple[int, ParsedPayload]]:
        """
        큐에서 패킷을 가져옴 (백그라운드 리더가 채움)
        반환: (kind, SensorReport/tuple_payload) 또는 None(타임아웃)
        """
        if not self.ser:
            raise RuntimeError("serial not open")

        try:
            return await asyncio.wait_for(
                self._packet_queue.get(),
                timeout=deadline_sec
            )
        except asyncio.TimeoutError:
            return None

    def _parse_line(self, line: str) -> Optional[tuple[str, int, list[str]]]:
        parts = [p.strip() for p in line.split(",")]
        # 첫 필드는 kind
        try:
            kind = int(parts[0], 10)
        except (IndexError, ValueError):
            return None
        return kind, parts[1:]

    def _decode_sensor_report(self, fields: list[str]) -> SensorReport:
        """
        기대 형태:
        moisture, temp_inner, humd_inner, temp_outer, humd_outer
        (모두 정수)
        """
        if len(fields) != 5:
            raise ValueError(f"sensor report needs 5 ints, got {len(fields)}: {fields}")
        try:
            vals = [int(x, 10) for x in fields]
            vals.append(read_light())
        except ValueError as e:
            raise ValueError(f"sensor report contains non-integer: {fields}") from e
        return SensorReport(*vals)
