"""History store: range selection, rotation handling, and bucket aggregation."""
import json
import math
import os
import tempfile
import unittest
from collections import defaultdict

from _history import COLUMNS, HistoryStore


def record(ts, moisture=500, temp=20.0, valid=True, peltier=100):
    return json.dumps({
        'sensor_report': {'moisture': moisture, 'temp_inner': temp if valid else None, 'humd_inner': 50.0,
                          'temp_outer': 10.0, 'humd_outer': 60.0, 'illumination': -1.0, 'received_at': ts,
                          'stale': False, 'valid': {'moisture': True, 'temp_inner': valid, 'humd_inner': True,
                                                    'temp_outer': True, 'humd_outer': True}},
        'reconciler_command': {'pump_level': 0, 'peltier_level': peltier, 'peltier_forward': 1},
        'timestamp': ts})


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.rows = [(1_000_000 + i * 5.0 + 0.3, 400 + i % 50, 15 + (i % 17) * 0.5, i % 11 != 0) for i in range(3000)]
        for n in range(0, 2527, 361):
            part = self.rows[n:n + 361]
            self.write(f'store_{int(part[-1][0])}.jsonl', part)
        self.write('store.jsonl', self.rows[2527:])
        self.store = HistoryStore(self.dir)

    def write(self, name, rows, mode='w'):
        with open(os.path.join(self.dir, name), mode) as f:
            f.writelines(record(t, m, v, ok) + '\n' for t, m, v, ok in rows)

    def test_raw_range_and_validity(self):
        r = self.store.query(self.rows[100][0], self.rows[199][0], 0)
        self.assertEqual(r['step'], 0)
        self.assertEqual(r['count'], 100)
        self.assertEqual(len(r['t']), 100)
        self.assertEqual(r['first'], self.rows[0][0])
        self.assertEqual(r['last'], self.rows[-1][0])
        self.assertIsNone(r['series']['temp_inner'][10])  # i=110 invalid at reception
        self.assertIsNone(r['series']['illumination'][0])  # -1.0 = no sensor
        self.assertEqual(r['series']['fan_level'][0], 1023)  # derived from peltier_level

    def test_buckets_match_brute_force(self):
        for start, end, points in ((None, None, 200), (0, 2e9, 100), (1_003_001.7, 1_012_345.1, 50)):
            r = self.store.query(start, end, points)
            step = r['step']
            self.assertGreater(step, 0)
            self.assertLessEqual(len(r['t']), points + 2)
            for k, col in enumerate(('moisture', 'temp_inner')):
                buckets = defaultdict(list)
                for t, m, v, ok in self.rows:
                    if r['start'] <= t <= r['end'] and (col == 'moisture' or ok):
                        buckets[math.floor(t / step)].append(m if col == 'moisture' else v)
                got = {math.floor(t / step): (a, lo, hi) for t, a, lo, hi in
                       zip(r['t'], r['series'][col], r['min'][col], r['max'][col])}
                self.assertEqual(set(got), set(buckets))
                for b, vs in buckets.items():
                    a, lo, hi = got[b]
                    self.assertAlmostEqual(a, sum(vs) / len(vs), delta=0.051)
                    self.assertEqual((lo, hi), (min(vs), max(vs)))

    def test_active_file_tail_and_rotation(self):
        before = self.store.query(0, 2e9, 0)['count']
        self.write('store.jsonl', [(2_000_000.0, 1, 1.0, True)], 'a')
        self.assertEqual(self.store.query(0, 3e9, 0)['count'], before + 1)
        os.rename(os.path.join(self.dir, 'store.jsonl'), os.path.join(self.dir, 'store_2000000.jsonl'))
        self.write('store.jsonl', [(2_000_005.0, 2, 2.0, True)])
        r = self.store.query(0, 3e9, 0)
        self.assertEqual(r['count'], before + 2)
        self.assertEqual(r['last'], 2_000_005.0)
        self.assertEqual(sorted(r['series']), sorted(COLUMNS))


if __name__ == '__main__':
    unittest.main()
