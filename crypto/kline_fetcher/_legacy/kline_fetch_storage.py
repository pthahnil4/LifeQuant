# -*- coding: utf-8 -*-
"""
K线本地存储工具（获取 + 落盘 + 续传 + 去重 + 缺口回填）
==========================================================

本模块把「从 ccxt 交易所拉取 OHLCV K 线并持续存储到本地 CSV」的完整流程
整理为一套可复用代码，核心能力：

  1. 增量抓取    —— 自动从本地 CSV 最后一根 K 线的时间戳续传
  2. 去重写入    —— 按 timestamp 去重，避免重复行
  3. 多交易所容错 —— 主交易所抓取，失败/缺口时按顺序用备用交易所回填
  4. 稳健重试    —— 网络错误指数退避、限流等待、5xx 服务器错误重试
  5. 连续性校验  —— 抓取结束后检测时间缺口并自动 backfill

依赖：pip install ccxt
运行：python kline_fetch_storage.py --symbol BTC/USDT --timeframe 1m
"""

import argparse
import csv
import datetime
import os
import time
from typing import List, Tuple, Set

import ccxt


# ---------------------------------------------------------------------------
# 命令行参数
# ---------------------------------------------------------------------------
def parse_args():
    """解析命令行参数，控制抓取哪个交易对、时间周期、存储路径等。"""
    parser = argparse.ArgumentParser(
        description="Robust OHLCV fetcher with resume, dedup and gap backfill"
    )
    parser.add_argument(
        "--exchanges", default="okx,bybit,bitget",
        help="逗号分隔的交易所 id，按顺序用于抓取/回填缺口"
    )
    parser.add_argument("--symbol", default="BTC/USDT", help="交易对，如 BTC/USDT")
    parser.add_argument("--timeframe", default="1m", help="K 线周期，默认 1m")
    # 默认起始时间：2022-01-01T00:00:00Z（毫秒）
    default_since_ms = int(
        datetime.datetime(2022, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000
    )
    parser.add_argument(
        "--since", type=int, default=default_since_ms,
        help="起始时间戳（毫秒），默认 2022-01-01 UTC"
    )
    parser.add_argument(
        "--save", default=None,
        help="输出 CSV 路径（默认存放在本模块同级的 data/ 目录）"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="若 CSV 已存在，则从最后时间戳续传"
    )
    parser.add_argument(
        "--no-backfill", action="store_true",
        help="跳过连续性检测与自动缺口回填"
    )
    parser.add_argument(
        "--max-candles", type=int, default=None,
        help="最多抓取多少根 K 线后停止（用于首轮爬取）"
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 存储路径与 CSV 读写
# ---------------------------------------------------------------------------
def default_save_path() -> str:
    """默认存储路径：模块同级 data/ 目录下的 CSV 文件。"""
    base_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "kline_ohlcv_default.csv")


def write_header_if_needed(path: str):
    """文件不存在或为空时写入 CSV 表头。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                ["timestamp", "datetime", "open", "high", "low", "close", "volume", "exchange"]
            )


def tail_last_timestamp(path: str):
    """读取文件末尾，快速返回最后一行的 timestamp（毫秒），无则 None。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            offset = max(end - 8192, 0)
            f.seek(offset)
            chunk = f.read().decode("utf-8", errors="ignore")
            lines = [ln for ln in chunk.splitlines() if ln.strip()]
            for line in reversed(lines):
                parts = line.split(",")
                if len(parts) >= 1:
                    try:
                        return int(parts[0])
                    except Exception:
                        continue
    except FileNotFoundError:
        return None
    return None


def load_existing_timestamps(path: str) -> Tuple[Set[int], List[int]]:
    """
    加载 CSV 中已有的所有 timestamp。

    返回:
        seen    : set，用于 O(1) 去重判断
        ordered : list，升序排列的所有 timestamp，用于缺口检测
    """
    seen: Set[int] = set()
    ordered: List[int] = []
    if not os.path.exists(path):
        return seen, ordered
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # 跳过表头
        for row in reader:
            if not row:
                continue
            try:
                ts = int(row[0])
            except Exception:
                continue
            if ts not in seen:
                seen.add(ts)
                ordered.append(ts)
    ordered.sort()
    return seen, ordered


def append_rows_dedup(path: str, exchange_id: str, rows: List[List], seen_ts: Set[int]) -> int:
    """
    追加写入 K 线行，按 timestamp 去重。

    参数 rows 中每个元素结构：[ts, open, high, low, close, volume]
    返回实际追加写入的行数。
    """
    appended = 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            ts = r[0]
            if ts in seen_ts:
                continue
            dt = datetime.datetime.fromtimestamp(
                ts / 1000, datetime.timezone.utc
            ).isoformat()
            w.writerow([ts, dt, r[1], r[2], r[3], r[4], r[5], exchange_id])
            seen_ts.add(ts)
            appended += 1
    return appended


# ---------------------------------------------------------------------------
# 交易所连接与抓取
# ---------------------------------------------------------------------------
def get_exchange(exchange_id: str):
    """根据 id 创建 ccxt 交易所实例并加载市场信息。"""
    if not hasattr(ccxt, exchange_id):
        raise RuntimeError(f"Exchange '{exchange_id}' 不存在于 ccxt")
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True, "requests_trust_env": True})
    exchange.load_markets()
    return exchange


def limit_for_exchange(exchange_id: str) -> int:
    """单次抓取的最大 K 线根数（1 分钟数据每次 300 根）。"""
    return 300


def safe_fetch_ohlcv(exchange, symbol: str, timeframe: str, since_ms: int, limit: int):
    """
    带重试机制的安全抓取。

    处理三类异常：
      - NetworkError  : 指数退避重试
      - 限流(429)     : 按 exchange.rateLimit 等待后重试
      - 5xx 服务器错误 : 更长的等待时间后重试
    超过最大重试次数则跳过本批次（返回空列表）。
    """
    backoff = 1
    max_retries = 10
    retry_count = 0
    while retry_count < max_retries:
        try:
            return exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=since_ms, limit=limit
            )
        except ccxt.NetworkError as e:
            retry_count += 1
            print(f"Network error (retry {retry_count}/{max_retries}): {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except ccxt.ExchangeError as e:
            msg = str(e).lower()
            if "rate limit" in msg or "throttle" in msg or "too many requests" in msg or "429" in msg:
                print(f"Rate limit hit, waiting {exchange.rateLimit / 1000.0}s...")
                time.sleep(max(exchange.rateLimit / 1000.0, 1.0))
                continue
            if any(code in msg for code in ("500", "502", "503", "504", "internal server error")):
                retry_count += 1
                wait_time = min(backoff * 5, 60)  # 服务器错误等待更久
                print(f"Server error (retry {retry_count}/{max_retries}): {e}")
                print(f"Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                backoff = min(backoff * 2, 30)
                continue
            raise
        except Exception as e:
            # 捕获其他异常（如底层 HTTPError）中的 5xx
            error_msg = str(e).lower()
            if any(code in error_msg for code in ("500", "502", "503", "504")):
                retry_count += 1
                wait_time = min(backoff * 5, 60)
                print(f"HTTP error (retry {retry_count}/{max_retries}): {e}")
                print(f"Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                backoff = min(backoff * 2, 30)
                continue
            raise
    print(f"Failed after {max_retries} retries, skipping this batch")
    return []


# ---------------------------------------------------------------------------
# 缺口检测与回填
# ---------------------------------------------------------------------------
def detect_gaps(sorted_ts: List[int], step_ms: int) -> List[Tuple[int, int]]:
    """
    在升序时间戳序列中检测缺口。

    返回缺口区间列表 [(gap_start_ts, gap_end_ts), ...]，
    每个区间表示 [prev+step, cur-step] 之间缺失的 K 线时间范围。
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
    exchanges: List, exchange_ids: List[str], symbol: str,
    timeframe: str, step_ms: int, path: str, seen_ts: Set[int]
):
    """
    检测本地 CSV 的时间缺口，并按交易所顺序依次回填缺失 K 线。

    对每个缺口：从 gap_start 之前的位置开始抓取，仅保留落在缺口范围内的
    数据去重写入；某个交易所补够后即切换到下一个缺口。全部完成后重新校验。
    """
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
            limit = max(limit_for_exchange(ex_id), n_missing + 5)
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
    # 重新校验
    _, ordered2 = load_existing_timestamps(path)
    gaps2 = detect_gaps(ordered2, step_ms)
    if not gaps2:
        print("Continuity check: all gaps resolved")
    else:
        print(f"Continuity check: {len(gaps2)} gap segment(s) remain")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    args = parse_args()
    save_path = args.save or default_save_path()
    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    write_header_if_needed(save_path)

    # 初始化交易所列表（第一个为主交易所，其余用于缺口回填）
    exchange_ids = [x.strip() for x in args.exchanges.split(",") if x.strip()]
    exchanges = []
    for ex_id in exchange_ids:
        try:
            exchanges.append(get_exchange(ex_id))
        except Exception as e:
            print(f"Skip exchange {ex_id} due to error: {e}")
    if not exchanges:
        raise RuntimeError("No valid exchanges available")

    primary = exchanges[0]
    step_ms = primary.parse_timeframe(args.timeframe) * 1000

    # 加载已有数据，确定起始抓取点
    seen_ts, ordered_ts = load_existing_timestamps(save_path)
    last_ts = ordered_ts[-1] if ordered_ts else None
    if last_ts is not None:
        # 自动从断点续传
        start_ms = last_ts + step_ms
        last_dt = datetime.datetime.fromtimestamp(last_ts / 1000, datetime.timezone.utc).isoformat()
        print(f"检测到已有数据 {len(ordered_ts)} 条，最后时间: {last_dt}")
        print(f"从断点续传: {datetime.datetime.fromtimestamp(start_ms / 1000, datetime.timezone.utc).isoformat()}")
    else:
        start_ms = int(args.since)
        print(f"从头开始获取: {datetime.datetime.fromtimestamp(start_ms / 1000, datetime.timezone.utc).isoformat()}")

    # 主循环：按批次增量抓取，直到当前时间
    now_ms = primary.milliseconds()
    limit = limit_for_exchange(exchange_ids[0])
    total_appended = 0
    consecutive_failures = 0
    max_consecutive_failures = 5

    while start_ms < now_ms - step_ms:
        batch = safe_fetch_ohlcv(primary, args.symbol, args.timeframe, start_ms, limit)
        if not batch:
            consecutive_failures += 1
            print(f"获取失败 ({consecutive_failures}/{max_consecutive_failures})，跳过当前批次")
            if consecutive_failures >= max_consecutive_failures:
                print(f"连续失败{max_consecutive_failures}次，停止获取")
                break
            # 跳过当前时间段，继续下一个
            start_ms += limit * step_ms
            time.sleep(5)
            continue

        consecutive_failures = 0  # 重置失败计数
        appended = append_rows_dedup(save_path, exchange_ids[0], batch, seen_ts)
        total_appended += appended
        last_dt = datetime.datetime.fromtimestamp(batch[-1][0] / 1000, datetime.timezone.utc).isoformat()
        print(f"Fetched {len(batch)} (appended {appended}), last dt={last_dt}, total={total_appended}")
        start_ms = batch[-1][0] + step_ms
        time.sleep(max(primary.rateLimit / 1000.0, 0.5))
        if args.max_candles and total_appended >= args.max_candles:
            break

    # 抓取结束后做连续性检测与缺口回填
    print(f"Initial fetch appended={total_appended}. Running continuity check...")
    if not args.no_backfill:
        backfill_gaps(exchanges, exchange_ids, args.symbol, args.timeframe, step_ms, save_path, seen_ts)

    # 最终汇总
    _, ordered_final = load_existing_timestamps(save_path)
    print(f"Final total candles={len(ordered_final)} saved to {save_path}")


if __name__ == "__main__":
    main()
