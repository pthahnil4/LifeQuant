# -*- coding: utf-8 -*-
"""
缺口（Gap）检测与回填模块
==========================

K 线数据在长时间抓取过程中，可能因交易所限流、网络抖动、历史数据缺失
等原因产生时间断点。本模块负责：

  - detect_gaps    : 在升序时间戳序列中检测缺失区间
  - backfill_gaps  : 按交易所优先级依次回填缺口，边补边去重

回填时以「主交易所 → 备用交易所」顺序尝试，某个交易所补齐当前缺口后即
进入下一个缺口；全部处理完再复查一次连续性并打印剩余缺口数。
"""

from typing import List, Tuple

from .csv_store import append_rows_dedup, load_existing_timestamps
from .exchange_fetcher import limit_for_exchange, safe_fetch_ohlcv


def detect_gaps(sorted_ts: List[int], step_ms: int) -> List[Tuple[int, int]]:
    """
    在升序时间戳序列中检测缺口。

    返回缺口区间列表 [(gap_start_ts, gap_end_ts), ...]，
    每个区间表示 [prev+step, cur-step] 之间缺失的 K 线时间范围（首尾均为
    实际缺失列的 timestamp）。
    """
    gaps: List[Tuple[int, int]] = []
    if not sorted_ts:
        return gaps
    prev = sorted_ts[0]
    for ts in sorted_ts[1:]:
        diff = ts - prev
        if diff > step_ms:
            gaps.append((prev + step_ms, ts - step_ms))
        prev = ts
    return gaps


def backfill_gaps(
    exchanges: List,
    exchange_ids: List[str],
    symbol: str,
    timeframe: str,
    step_ms: int,
    path: str,
    seen_ts: set,
) -> None:
    """检测并回填 CSV 中的时间缺口。"""
    _, ordered = load_existing_timestamps(path)
    gaps = detect_gaps(ordered, step_ms)
    if not gaps:
        print("Continuity check: no gaps detected")
        return
    print(f"Continuity check: detected {len(gaps)} gap segment(s)")
    for (gap_start, gap_end) in gaps:
        n_missing = int((gap_end - gap_start) / step_ms) + 1
        print(f"Backfilling gap {gap_start}->{gap_end} ({n_missing} candles)")
        filled = 0
        for ex, ex_id in zip(exchanges, exchange_ids):
            limit = max(limit_for_exchange(ex_id, timeframe), n_missing + 5)
            batch = safe_fetch_ohlcv(ex, symbol, timeframe, gap_start - step_ms, limit)
            if not batch:
                continue
            # 仅保留落在 [gap_start, gap_end] 区间内的 K 线
            filtered = [r for r in batch if gap_start <= r[0] <= gap_end]
            appended = append_rows_dedup(path, ex_id, filtered, seen_ts)
            filled += appended
            if filled >= n_missing:
                print(f"Gap {gap_start}->{gap_end} filled by {ex_id} (added {appended})")
                break
        if filled < n_missing:
            print(f"Warning: gap {gap_start}->{gap_end} partially filled ({filled}/{n_missing})")
    # 复查
    _, ordered2 = load_existing_timestamps(path)
    gaps2 = detect_gaps(ordered2, step_ms)
    if not gaps2:
        print("Continuity check: all gaps resolved")
    else:
        print(f"Continuity check: {len(gaps2)} gap segment(s) remain")
