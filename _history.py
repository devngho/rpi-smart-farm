"""Time-range queries over the JSONL sensor history, with server-side downsampling.

Segment files are written by `_store.add_segment`:
  * `store_<ts>.jsonl` - rotated, immutable; `<ts>` is int(last timestamp) in the file.
  * `store.jsonl`      - the active file, appended to every ~5 s.

Parsed files are kept as compact columnar `array('d')` chunks (NaN = no valid value)
in a small LRU, so a query only parses files it has not seen and never re-reads
immutable files. The active file is tailed incrementally from the last offset.

Each chunk also lazily keeps 1-minute and 15-minute rollups (sum/count/min/max per
column), so wide ranges are bucketed from rollups instead of every raw row.
"""
import json
import math
import os
import threading
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict

DATA_DIR = './data'
ACTIVE_NAME = 'store.jsonl'

SENSOR_COLUMNS = ('moisture', 'temp_inner', 'humd_inner', 'temp_outer', 'humd_outer', 'illumination')
COMMAND_COLUMNS = ('pump_level', 'peltier_level', 'peltier_forward', 'fan_level', 'temp_estimated')
COLUMNS = SENSOR_COLUMNS + COMMAND_COLUMNS
DIGITS = {'moisture': 1, 'temp_inner': 2, 'temp_outer': 2, 'humd_inner': 2, 'humd_outer': 2,
          'illumination': 1, 'pump_level': 1, 'peltier_level': 1, 'peltier_forward': 2, 'fan_level': 1,
          'temp_estimated': 2}

ROLLUP_LEVELS = (60, 900)
# Bucket sizes are multiples of the rollup levels and aligned to the epoch, so
# rollup buckets nest exactly inside query buckets.
NICE_STEPS = (5, 10, 15, 30, 60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 10800,
              21600, 43200, 86400, 172800, 604800)

MAX_RAW_ROWS = 500_000
NAN = math.nan
_isnan = math.isnan


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else NAN


def _row(line: str):
    """Parse one JSONL record into (timestamp, values) or None. Validity is applied here."""
    try:
        rec = json.loads(line)
        ts = float(rec['timestamp'])
        rep = rec['sensor_report']
    except (ValueError, KeyError, TypeError):
        return None
    if not math.isfinite(ts) or not isinstance(rep, dict):
        return None
    valid = rep.get('valid')
    legacy = not isinstance(valid, dict) or 'received_at' not in rep  # unknown moisture unit
    vals = []
    for c in SENSOR_COLUMNS[:5]:
        if (c == 'moisture' and legacy) or (isinstance(valid, dict) and valid.get(c) is False):
            vals.append(NAN)
        else:
            vals.append(_num(rep.get(c)))
    lux = _num(rep.get('illumination'))
    vals.append(lux if lux >= 0 else NAN)  # -1.0 is the "no I2C bus" fallback
    cmd = rec.get('reconciler_command')
    if isinstance(cmd, dict):
        peltier = _num(cmd.get('peltier_level'))
        fan = _num(cmd.get('fan_level'))
        if _isnan(fan) and not _isnan(peltier):
            fan = 1023.0 if peltier >= 64 else 0.0  # ReconcilerCommand.fan_level is derived
        # inner temperature estimated from the outer sensor while the inner sensor was down
        est = _num(cmd.get('control_temp')) if cmd.get('temp_source') == 'estimated' else NAN
        vals += [_num(cmd.get('pump_level')), peltier, _num(cmd.get('peltier_forward')), fan, est]
    else:
        vals += [NAN] * len(COMMAND_COLUMNS)
    return ts, vals


class Rollup:
    __slots__ = ('t', 'n', 'tsum', 'sum', 'cnt', 'mn', 'mx')

    def __init__(self):
        self.t = array('d')      # bucket start
        self.n = array('d')      # rows in bucket
        self.tsum = array('d')   # sum of row timestamps
        self.sum = [array('d') for _ in COLUMNS]
        self.cnt = [array('d') for _ in COLUMNS]
        self.mn = [array('d') for _ in COLUMNS]
        self.mx = [array('d') for _ in COLUMNS]


class Chunk:
    """Columnar rows of one segment file, sorted by timestamp."""

    def __init__(self):
        self.t = array('d')
        self.cols = [array('d') for _ in COLUMNS]
        self._rollups: dict[int, Rollup] = {}

    def extend(self, lines):
        rows = [r for r in map(_row, lines) if r is not None]
        if not rows:
            return
        if len(self.t) and rows[0][0] < self.t[-1] or any(a[0] > b[0] for a, b in zip(rows, rows[1:])):
            rows = sorted([*self.rows(), *rows], key=lambda r: r[0])
            self.t = array('d')
            self.cols = [array('d') for _ in COLUMNS]
        for ts, vals in rows:
            self.t.append(ts)
            for col, v in zip(self.cols, vals):
                col.append(v)
        self._rollups.clear()

    def rows(self):
        return [(self.t[i], [c[i] for c in self.cols]) for i in range(len(self.t))]

    def rollup(self, level: int) -> Rollup:
        r = self._rollups.get(level)
        if r is None:
            r = self._rollups[level] = self._build_rollup(level)
        return r

    def _build_rollup(self, level: int) -> Rollup:
        r = Rollup()
        t = self.t
        n = len(t)
        i = 0
        while i < n:
            b = math.floor(t[i] / level) * level
            j = bisect_left(t, b + level, i)
            r.t.append(b)
            r.n.append(j - i)
            r.tsum.append(math.fsum(t[i:j]))
            for k, col in enumerate(self.cols):
                vs = [v for v in col[i:j] if not _isnan(v)]
                if vs:
                    r.sum[k].append(math.fsum(vs))
                    r.cnt[k].append(len(vs))
                    r.mn[k].append(min(vs))
                    r.mx[k].append(max(vs))
                else:
                    r.sum[k].append(0.0)
                    r.cnt[k].append(0.0)
                    r.mn[k].append(NAN)
                    r.mx[k].append(NAN)
            i = j
        return r


class HistoryStore:
    def __init__(self, directory: str = DATA_DIR, cache_files: int = 256):
        self.directory = directory
        self.cache_files = cache_files
        self._lock = threading.Lock()
        self._chunks: OrderedDict[str, tuple[tuple, Chunk]] = OrderedDict()
        self._listing_key = None
        self._rotated: list[tuple[int, str]] = []
        self._active = Chunk()
        self._active_state = None  # (inode, offset)

    # ---- file access -------------------------------------------------------
    def _list(self) -> list[tuple[int, str]]:
        try:
            st = os.stat(self.directory)
        except FileNotFoundError:
            return []
        key = (st.st_mtime_ns, st.st_ino)
        if key != self._listing_key:
            rotated = []
            for name in os.listdir(self.directory):
                if name.startswith('store_') and name.endswith('.jsonl'):
                    ts = name[len('store_'):-len('.jsonl')]
                    if ts.isdigit():
                        rotated.append((int(ts), os.path.join(self.directory, name)))
            rotated.sort()
            self._rotated, self._listing_key = rotated, key
        return self._rotated

    def _rotated_chunk(self, path: str) -> Chunk | None:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        hit = self._chunks.get(path)
        if hit and hit[0] == key:
            self._chunks.move_to_end(path)
            return hit[1]
        chunk = Chunk()
        with open(path, encoding='utf-8') as f:
            chunk.extend(f.readlines())
        self._chunks[path] = (key, chunk)
        while len(self._chunks) > self.cache_files:
            self._chunks.popitem(last=False)
        return chunk

    def _active_chunk(self) -> Chunk:
        path = os.path.join(self.directory, ACTIVE_NAME)
        try:
            f = open(path, 'rb')
        except FileNotFoundError:
            self._active, self._active_state = Chunk(), None
            return self._active
        with f:
            st = os.fstat(f.fileno())
            inode, offset = self._active_state or (None, 0)
            if inode != st.st_ino or st.st_size < offset:
                self._active, offset = Chunk(), 0  # rotated / truncated
            if st.st_size > offset:
                f.seek(offset)
                data = f.read(st.st_size - offset)
                end = data.rfind(b'\n') + 1  # leave a half-written last line for later
                if end:
                    self._active.extend(data[:end].decode('utf-8', 'replace').splitlines())
                    offset += end
            self._active_state = (st.st_ino, offset)
        return self._active

    def _chunks_for(self, start: float, end: float) -> list[Chunk]:
        rotated = self._list()
        out = []
        prev = -math.inf
        for ts, path in rotated:
            # rows of this file lie in (prev name ts, ts + 1)
            if ts + 1 >= start and prev <= end:
                c = self._rotated_chunk(path)
                if c is not None and len(c.t):
                    out.append(c)
            prev = ts
            if prev > end:
                break
        if prev <= end:
            c = self._active_chunk()
            if len(c.t):
                out.append(c)
        return out

    def bounds(self) -> tuple[float | None, float | None]:
        rotated = self._list()
        first = last = None
        if rotated:
            c = self._rotated_chunk(rotated[0][1])
            if c is not None and len(c.t):
                first = c.t[0]
        a = self._active_chunk()
        if len(a.t):
            last = a.t[-1]
            first = a.t[0] if first is None else first
        elif rotated:
            c = self._rotated_chunk(rotated[-1][1])
            if c is not None and len(c.t):
                last = c.t[-1]
        return first, last

    # ---- queries -----------------------------------------------------------
    def query(self, start: float | None, end: float | None, points: int) -> dict:
        """Rows in [start, end]. `points` <= 0 means raw rows; otherwise at most ~points buckets."""
        with self._lock:
            first, last = self.bounds()
            if end is None:
                end = last if last is not None else 0.0
            if start is None:
                start = end - 6 * 3600
            if start > end:
                start, end = end, start
            chunks = self._chunks_for(start, end)
            spans = [(c, bisect_left(c.t, start), bisect_right(c.t, end)) for c in chunks]
            count = sum(j - i for _, i, j in spans)
            if points <= 0 or count <= points:
                step = 0
                t, series = self._raw(spans)
                lo = hi = None
            else:
                lo_t = max(start, first) if first is not None else start
                hi_t = min(end, last) if last is not None else end
                span = max(hi_t - lo_t, 0)
                step = next((s for s in NICE_STEPS if span / s <= points), NICE_STEPS[-1])
                t, series, lo, hi = self._bucketed(spans, max(start, lo_t), min(end, hi_t), step)

        def pack(arr, digits):
            return [None if _isnan(v) else round(v, digits) for v in arr]

        out = {
            'start': start, 'end': end, 'first': first, 'last': last,
            'step': step, 'count': count,
            't': [round(v, 1) for v in t],
            'series': {c: pack(series[k], DIGITS[c]) for k, c in enumerate(COLUMNS)},
        }
        if lo is not None:
            out['min'] = {c: pack(lo[k], DIGITS[c]) for k, c in enumerate(COLUMNS)}
            out['max'] = {c: pack(hi[k], DIGITS[c]) for k, c in enumerate(COLUMNS)}
        return out

    @staticmethod
    def _raw(spans):
        t = array('d')
        cols = [array('d') for _ in COLUMNS]
        for c, i, j in spans:
            j = min(j, i + MAX_RAW_ROWS - len(t))
            t.extend(c.t[i:j])
            for dst, src in zip(cols, c.cols):
                dst.extend(src[i:j])
            if len(t) >= MAX_RAW_ROWS:
                break
        return t, cols

    @staticmethod
    def _bucketed(spans, start: float, end: float, step: int):
        b0 = math.floor(start / step) * step
        nb = int((end - b0) // step) + 1
        ncol = len(COLUMNS)
        n = [0.0] * nb
        tsum = [0.0] * nb
        s = [[0.0] * nb for _ in range(ncol)]
        cnt = [[0.0] * nb for _ in range(ncol)]
        mn = [[math.inf] * nb for _ in range(ncol)]
        mx = [[-math.inf] * nb for _ in range(ncol)]
        level = next((lv for lv in reversed(ROLLUP_LEVELS) if step % lv == 0), None)

        for c, i, j in spans:
            if i >= j:
                continue
            if level is not None:
                r = c.rollup(level)
                # rollup buckets fully inside [start, end]; edges fall back to raw rows
                ri = bisect_left(r.t, math.ceil(start / level) * level)
                rj = bisect_right(r.t, end - level)
                if ri < rj:
                    raw_lo_end = bisect_left(c.t, r.t[ri], i, j)
                    raw_hi_start = bisect_left(c.t, r.t[rj - 1] + level, i, j)
                    HistoryStore._acc_rollup(r, ri, rj, b0, step, n, tsum, s, cnt, mn, mx)
                    HistoryStore._acc_raw(c, i, raw_lo_end, b0, step, n, tsum, s, cnt, mn, mx)
                    HistoryStore._acc_raw(c, raw_hi_start, j, b0, step, n, tsum, s, cnt, mn, mx)
                    continue
            HistoryStore._acc_raw(c, i, j, b0, step, n, tsum, s, cnt, mn, mx)

        keep = [b for b in range(nb) if n[b]]
        t = [tsum[b] / n[b] for b in keep]
        avg = [[s[k][b] / cnt[k][b] if cnt[k][b] else NAN for b in keep] for k in range(ncol)]
        lo = [[mn[k][b] if cnt[k][b] else NAN for b in keep] for k in range(ncol)]
        hi = [[mx[k][b] if cnt[k][b] else NAN for b in keep] for k in range(ncol)]
        return t, avg, lo, hi

    @staticmethod
    def _acc_raw(c: Chunk, i, j, b0, step, n, tsum, s, cnt, mn, mx):
        if i >= j:
            return
        t = c.t
        idx = [int((t[x] - b0) // step) for x in range(i, j)]
        for x, b in zip(range(i, j), idx):
            n[b] += 1
            tsum[b] += t[x]
        for k, col in enumerate(c.cols):
            sk, ck, mnk, mxk = s[k], cnt[k], mn[k], mx[k]
            for x, b in zip(range(i, j), idx):
                v = col[x]
                if v == v:  # not NaN
                    sk[b] += v
                    ck[b] += 1
                    if v < mnk[b]:
                        mnk[b] = v
                    if v > mxk[b]:
                        mxk[b] = v

    @staticmethod
    def _acc_rollup(r: Rollup, i, j, b0, step, n, tsum, s, cnt, mn, mx):
        idx = [int((r.t[x] - b0) // step) for x in range(i, j)]
        for x, b in zip(range(i, j), idx):
            n[b] += r.n[x]
            tsum[b] += r.tsum[x]
        for k in range(len(COLUMNS)):
            rs, rc, rmn, rmx = r.sum[k], r.cnt[k], r.mn[k], r.mx[k]
            sk, ck, mnk, mxk = s[k], cnt[k], mn[k], mx[k]
            for x, b in zip(range(i, j), idx):
                if rc[x]:
                    sk[b] += rs[x]
                    ck[b] += rc[x]
                    if rmn[x] < mnk[b]:
                        mnk[b] = rmn[x]
                    if rmx[x] > mxk[b]:
                        mxk[b] = rmx[x]


store = HistoryStore(cache_files=int(os.environ.get('SMART_FARM_HISTORY_CACHE_FILES', '512')))
