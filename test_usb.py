"""USB protocol integration tests using a Linux pseudo-terminal in place of the UNO."""
import asyncio
import json
import os
import pty
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import main
from _packet import parse_report
from _store import add_segment, last_segments

STOP = b'0,0\n1,0,1\n2,0\n'
BOOT = b'9\n' + STOP


class USBTests(unittest.IsolatedAsyncioTestCase):
    def test_parser(self):
        report = parse_report(b'0,737,32.5,nan,11.25,inf\r')
        assert report is not None
        self.assertEqual((report.moisture, report.temp_inner, report.humd_inner,
                          report.temp_outer, report.humd_outer), (737, 32.5, None, 11.25, None))
        for malformed in (b'0,1024,1,2,3,4', b'0,12,1', b'0,2.5,1,2,3,4'):
            self.assertIsNone(parse_report(malformed))

    async def test_heartbeat_during_slow_control(self):
        reconcile = main.reconcile_sensor_data

        def slow(*args):
            time.sleep(5.3)  # longer than the UNO heartbeat deadline
            return reconcile(*args)

        master, slave = pty.openpty()
        self.addCleanup(os.close, slave)
        self.addCleanup(os.close, master)
        os.set_blocking(master, False)
        records = asyncio.Queue()
        with patch.object(main, 'reconcile_sensor_data', slow):
            task = asyncio.create_task(main.read_task(os.ttyname(slave), records))
            beats = []
            partial = b''
            started = False
            try:
                async with asyncio.timeout(7):
                    while records.empty():
                        await asyncio.sleep(0.02)
                        with suppress(BlockingIOError):
                            partial += os.read(master, 4096)
                        *lines, partial = partial.split(b'\n')
                        for line in lines:
                            if line == b'9':
                                beats.append(time.monotonic())
                                os.write(master, b'9\r\n')
                            elif not started and line == b'2,0':
                                os.write(master, b'0,737,32.5,50.5,19.2,61.0\r\n')
                                started = True
                self.assertEqual(len(beats), 3)
                for a, b in zip(beats, beats[1:], strict=False):
                    self.assertAlmostEqual(b - a, 2.0, delta=0.3)
                report, command = records.get_nowait()
                self.assertEqual((report.moisture, report.temp_inner, report.humd_inner,
                                  report.temp_outer, report.humd_outer), (737, 32.5, 50.5, 19.2, 61.0))
                self.assertGreater(command.peltier_level, 0)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def test_unplug(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = Path(tmp) / 'uno-by-id'
            master, slave = pty.openpty()
            self.addCleanup(os.close, slave)
            port.symlink_to(os.ttyname(slave))
            records = asyncio.Queue(maxsize=8)
            task = asyncio.create_task(main.read_task(str(port), records))
            try:
                # The LF-terminated stop sequence indicates the session is ready.
                os.set_blocking(master, False)
                wire = bytearray()
                async with asyncio.timeout(2):
                    while b'2,0\n' not in wire:
                        await asyncio.sleep(0.01)
                        with suppress(BlockingIOError):
                            wire.extend(os.read(master, 4096))
                os.write(master, b'9\n0,800,40,20,5,70\n')
                _, old = await asyncio.wait_for(records.get(), 2)
                self.assertGreater(old.peltier_level, 0)
                port.unlink()
                os.close(master)
                master = -1
                async with asyncio.timeout(2):
                    while main.last_command is not None:
                        await asyncio.sleep(0.01)
                replacement, slave2 = pty.openpty()
                self.addCleanup(os.close, replacement)
                self.addCleanup(os.close, slave2)
                port.symlink_to(os.ttyname(slave2))
                os.set_blocking(replacement, False)
                wire.clear()
                async with asyncio.timeout(4):
                    while b'2,0\n' not in wire:
                        await asyncio.sleep(0.01)
                        with suppress(BlockingIOError):
                            wire.extend(os.read(replacement, 4096))
                self.assertEqual(wire, BOOT)
                self.assertIsNone(main.last_command)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                if master >= 0:
                    os.close(master)

    async def test_session(self):
        # This test covers the fail-safe path: no hold, no outer-sensor fallback.
        for key in ('inner_hold_s', 'fallback_max_s'):
            patcher = patch.object(main.reconciler_config, key, 0.0)
            patcher.start()
            self.addCleanup(patcher.stop)
        master, slave = pty.openpty()
        self.addCleanup(os.close, slave)
        self.addCleanup(os.close, master)
        os.set_blocking(master, False)
        wire = bytearray()

        async def until(marker, timeout=2):
            async with asyncio.timeout(timeout):
                while marker not in wire:
                    await asyncio.sleep(0.01)
                    with suppress(BlockingIOError):
                        wire.extend(os.read(master, 4096))
            del wire[:wire.index(marker) + len(marker)]

        with tempfile.TemporaryDirectory() as tmp:
            port = Path(tmp) / 'uno-by-id'
            port.symlink_to(os.ttyname(slave))
            records = asyncio.Queue(maxsize=8)
            task = asyncio.create_task(main.read_task(str(port), records))
            try:
                await until(BOOT)
                with self.assertLogs(level='WARNING') as logs:
                    os.write(master, b'X' * 300 + b'0,1,1,1,1,1\n0,1,no,2,3,4\r\n9\r\n0,100,32.')
                    await asyncio.sleep(0.25)
                self.assertIn('longer than', ' '.join(logs.output))
                self.assertIn('malformed', ' '.join(logs.output))
                self.assertTrue(records.empty())
                await asyncio.sleep(0.15)  # fragment across read timeouts
                os.write(master, b'5,41.5,8.25,77.5\n')
                await until(b'2,1023\n')
                report, command = records.get_nowait()
                self.assertEqual(report.temp_outer, 8.25)
                self.assertEqual(command.peltier_forward, main.reconciler_config.cooling_direction)
                self.assertEqual(command.pump_level, 1023)
                await until(b'0,0\n')  # bounded pump pulse without another report
                os.write(master, b'0,800,nan,41.5,8.25,77.5\n')
                await until(b'1,0,0\n2,0\n')
                invalid, _ = records.get_nowait()
                self.assertIsNone(invalid.temp_inner)
                self.assertEqual(invalid.last_valid_at['temp_inner'], report.received_at)
                os.write(master, b'0,800,nan,nan,nan,nan\n0,800,32.5,41.5,nan,-inf\r\n')
                await until(b'2,1023\n')
                both, off = records.get_nowait()
                self.assertFalse(any(both.snapshot()['valid'][k] for k in ('temp_inner', 'humd_inner', 'temp_outer', 'humd_outer')))
                self.assertEqual(off.peltier_level, 0)
                outer_fault, cooling = records.get_nowait()
                self.assertGreater(cooling.peltier_level, 0)
                add_segment((outer_fault, cooling), str(Path(tmp) / 'store.jsonl'))
                saved = json.loads(last_segments(1, tmp)[0])['sensor_report']
                self.assertEqual(saved, await main.get_latest_sensor())
                self.assertFalse(saved['valid']['temp_outer'])
                self.assertEqual(saved['humd_inner'], 41.5)
                with patch.object(main.reconciler_config, 'sensor_timeout_s', 0.25):
                    await until(STOP)
                    self.assertTrue(outer_fault.snapshot(0.25)['stale'])
                    os.write(master, b'0,800,8.5,40,25,70\n')
                    await until(b'2,1023\n')
                    _, heating = records.get_nowait()
                    self.assertNotEqual(heating.peltier_forward, cooling.peltier_forward)
                await until(b'9\n', timeout=3)  # independent of incoming data
                await until(STOP, timeout=6)  # failed session shutdown
                await until(BOOT, timeout=4)  # missing UNO heartbeat → close and reopen
                self.assertIsNone(main.last_command)
                os.write(master, b'9\n0,800,20,40,5,70\n')
                await until(b'0,0\n1,0,0\n2,0\n')
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await until(STOP)
