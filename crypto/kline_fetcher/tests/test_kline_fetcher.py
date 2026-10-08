# -*- coding: utf-8 -*-
"""
kline_fetcher 功能测试（离线，使用假交易所，不联网、不触碰真实 data 目录）
==========================================================================

覆盖需求中的四大场景：
  1. 首次从头抓取      test_first_fetch_from_scratch
  2. 断点续传          test_resume_from_existing
  3. Gap 自动回填      test_gap_detection_and_backfill
  4. 多交易所合并去重  test_multi_exchange_merge_and_dedup

另含纯函数与端到端（fetcher.run）组合测试。
运行： pytest -q  或  python -m pytest kline_fetcher/tests -q
"""

import os
import sys
import tempfile
from types import SimpleNamespace

# 把 strategyAI 目录加入 sys.path，使 `import kline_fetcher` 可用
_STRATEGY_AI = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _STRATEGY_AI not in sys.path:
    sys.path.insert(0, _STRATEGY_AI)

from kline_fetcher import fetcher  # noqa: E402
from kline_fetcher.csv_store import (  # noqa: E402
    append_rows_dedup,
    load_existing_timestamps,
    load_rows,
    write_header_if_needed,
)
from kline_fetcher.gap_backfill import backfill_gaps, detect_gaps  # noqa: E402

STEP = 60_000  # 1m 毫秒步长
NO_SLEEP = lambda *_: None  # noqa: E731  测试中禁用真实 sleep


class FakeExchange:
    """
    离线假交易所：只提供一段预先构造的 K 线，模拟 ccxt 接口。

    bars: dict[ts_ms] -> (open, high, low, close, volume)
    """

    def __init__(self, exchange_id, bars, now_ms, tf_seconds=60, rate=0):
        self.id = exchange_id
        self._bars = bars
        self._now = now_ms
        self._tf = tf_seconds
        self.rateLimit = rate  # 设 0 避免测试中触发真实等待

    def parse_timeframe(self, timeframe):
        return self._tf

    def milliseconds(self):
        return self._now

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None):
        ts_list = sorted(t for t in self._bars if since is None or t >= since)
        if limit:
            ts_list = ts_list[:limit]
        return [[t, *self._bars[t]] for t in ts_list]


def make_bars(n, start_ms, missing=()):
    """生成 n 根连续 K 线；missing 为要跳过（制造缺口）的下标集合。"""
    bars = {}
    for i in range(n):
        if i in missing:
            continue
        ts = start_ms + i * STEP
        px = 100 + i
        bars[ts] = (px, px + 1, px - 1, px + 0.5, 10 + i)
    return bars


def temp_csv_path(name):
    d = tempfile.mkdtemp(prefix="kline_test_")
    return os.path.join(d, name)


# ---------------------------------------------------------------------------
# 场景 1：首次从头抓取
# ---------------------------------------------------------------------------
def test_first_fetch_from_scratch():
    start = 1_700_000_000_000 - (1_700_000_000_000 % STEP)  # 对齐步长
    n = 250
    bars = make_bars(n, start)
    now = start + n * STEP
    ex = FakeExchange("okx", bars, now)
    path = temp_csv_path("first.csv")
    write_header_if_needed(path)  # 真实 run() 会先建表头，单测同样先建
    seen = set()

    total = fetcher.fetch_incremental(
        ex, "okx", "BTC/USDT", "1m", STEP, path, start, seen,
        max_candles=None, sleep_fn=NO_SLEEP,
    )

    assert total == n, f"应抓取 {n} 根，实际 {total}"
    ts_set, ts_list = load_existing_timestamps(path)
    assert len(ts_list) == n
    assert ts_list == sorted(ts_list)
    # 时间戳严格连续无缺口
    assert detect_gaps(ts_list, STEP) == []


# ---------------------------------------------------------------------------
# 场景 2：断点续传
# ---------------------------------------------------------------------------
def test_resume_from_existing():
    start = 1_700_000_000_000 - (1_700_000_000_000 % STEP)
    n = 300
    bars = make_bars(n, start)
    now = start + n * STEP
    ex = FakeExchange("okx", bars, now)
    path = temp_csv_path("resume.csv")

    # 预写入前 120 根，模拟「已有部分数据」
    write_header_if_needed(path)
    seed_ts = sorted(bars)[:120]
    seed_rows = [[t, *bars[t]] for t in seed_ts]
    appended = append_rows_dedup(path, "okx", seed_rows, set())
    assert appended == 120

    # 续传：起始点应为最后一根的下一周期
    seen, ordered = load_existing_timestamps(path)
    start_ms = fetcher.resolve_start_ms(path, STEP, start, True, ex)
    assert start_ms == seed_ts[-1] + STEP

    total = fetcher.fetch_incremental(
        ex, "okx", "BTC/USDT", "1m", STEP, path, start_ms, seen,
        sleep_fn=NO_SLEEP,
    )

    assert total == n - 120, f"续传应新增 {n - 120} 根，实际 {total}"
    _, ts_list = load_existing_timestamps(path)
    assert len(ts_list) == n                 # 无丢失
    assert len(ts_list) == len(set(ts_list))  # 无重复
    assert detect_gaps(ts_list, STEP) == []


# ---------------------------------------------------------------------------
# 场景 3：Gap 自动回填
# ---------------------------------------------------------------------------
def test_gap_detection_and_backfill():
    start = 1_700_000_000_000 - (1_700_000_000_000 % STEP)
    n = 100
    # 主交易所缺第 40~49 根（制造缺口），备用交易所数据完整
    missing = set(range(40, 50))
    primary_bars = make_bars(n, start, missing=missing)
    full_bars = make_bars(n, start)
    path = temp_csv_path("gap.csv")

    # 用主交易所数据先落盘（此时含缺口）
    write_header_if_needed(path)
    rows = [[t, *primary_bars[t]] for t in sorted(primary_bars)]
    append_rows_dedup(path, "okx", rows, set())

    _, before = load_existing_timestamps(path)
    gaps_before = detect_gaps(before, STEP)
    assert len(gaps_before) == 1, f"应检测到 1 段缺口，实际 {len(gaps_before)}"
    assert len(before) == n - len(missing)

    # 回填：主交易所仍缺，备用交易所可补齐
    primary = FakeExchange("okx", primary_bars, start + n * STEP)
    backup = FakeExchange("bybit", full_bars, start + n * STEP)
    seen, _ = load_existing_timestamps(path)
    backfill_gaps(
        [primary, backup], ["okx", "bybit"], "BTC/USDT", "1m", STEP, path, seen
    )

    _, after = load_existing_timestamps(path)
    assert detect_gaps(after, STEP) == []          # 缺口已补全
    assert len(after) == n                          # 总数完整
    assert len(after) == len(set(after))            # 无重复
    # 补缺的那几根来源应标注为备用交易所
    rows_map = {int(r["timestamp"]): r["exchange"] for r in load_rows(path)}
    gap_ts = start + 45 * STEP
    assert gap_ts in rows_map
    assert rows_map[gap_ts] in ("okx", "bybit")


def test_detect_gaps_pure():
    ts = [0, STEP, 2 * STEP, 5 * STEP, 6 * STEP]
    gaps = detect_gaps(ts, STEP)
    assert gaps == [(3 * STEP, 4 * STEP)]


# ---------------------------------------------------------------------------
# 场景 4：多交易所数据合并与去重
# ---------------------------------------------------------------------------
def test_multi_exchange_merge_and_dedup():
    start = 1_700_000_000_000 - (1_700_000_000_000 % STEP)
    path = temp_csv_path("merge.csv")
    write_header_if_needed(path)
    seen = set()

    # 交易所 A 提供第 0~9 根
    bars_a = make_bars(10, start)
    rows_a = [[t, *bars_a[t]] for t in sorted(bars_a)]
    assert append_rows_dedup(path, "okx", rows_a, seen) == 10

    # 交易所 B 提供第 5~14 根：5~9 与 A 重叠（应去重），10~14 为新增
    bars_b = make_bars(15, start)
    rows_b = [[t, *bars_b[t]] for t in sorted(bars_b) if 5 <= (t - start) // STEP <= 14]
    appended_b = append_rows_dedup(path, "bybit", rows_b, seen)
    assert appended_b == 5, f"重叠部分不应重复写入，新增应为 5，实际 {appended_b}"

    # 再次写入完全重叠的数据：应 0 新增
    assert append_rows_dedup(path, "bitget", rows_a, seen) == 0

    _, ts_list = load_existing_timestamps(path)
    assert len(ts_list) == 15
    assert len(ts_list) == len(set(ts_list))
    exchanges_used = {r["exchange"] for r in load_rows(path)}
    assert exchanges_used == {"okx", "bybit"}


# ---------------------------------------------------------------------------
# 端到端：fetcher.run（注入假交易所，走完整流程含回填）
# ---------------------------------------------------------------------------
def test_run_end_to_end_with_injected_exchanges():
    start = 1_700_000_000_000 - (1_700_000_000_000 % STEP)
    n = 60
    missing = {30, 31}
    primary_bars = make_bars(n, start, missing=missing)
    full_bars = make_bars(n, start)
    now = start + n * STEP
    path = temp_csv_path("e2e.csv")

    config = SimpleNamespace(
        exchanges=["okx", "bybit"],
        exchanges_objs=[
            FakeExchange("okx", primary_bars, now),
            FakeExchange("bybit", full_bars, now),
        ],
        symbol="BTC/USDT",
        timeframe="1m",
        since_ms=start,
        save_path=path,
        max_candles=None,
        no_backfill=False,
    )

    saved = fetcher.run(config, sleep_fn=NO_SLEEP)
    assert saved == path
    _, ts_list = load_existing_timestamps(path)
    assert len(ts_list) == n
    assert detect_gaps(ts_list, STEP) == []       # 端到端后无缺口
    assert len(ts_list) == len(set(ts_list))      # 无重复


if __name__ == "__main__":
    # 支持不装 pytest 时直接运行本文件
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
