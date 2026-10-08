import argparse
import csv
import datetime
import os
import time
from typing import List, Tuple, Set

import ccxt


def parse_args():
    parser = argparse.ArgumentParser(description="Robust BTC OHLCV 1h fetch with dedup and gap backfill")
    parser.add_argument("--exchanges", default="okx,bybit,bitget", help="Comma-separated exchange ids used in order for fetching/backfill")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair symbol")
    parser.add_argument("--timeframe", default="1h", help="Timeframe (default 1h)")
    # 2022-01-01T00:00:00Z in ms
    default_since_ms = int(datetime.datetime(2022, 1, 1, tzinfo=datetime.UTC).timestamp() * 1000)
    parser.add_argument("--since", type=int, default=default_since_ms, help="Start timestamp (ms), default is 2022-01-01 UTC")
    parser.add_argument("--save", default=None, help="Output CSV path (default under strategyAI/data)")
    parser.add_argument("--resume", action="store_true", help="Resume from last timestamp in CSV if exists")
    parser.add_argument("--no-backfill", action="store_true", help="Skip continuity check and auto backfill")
    parser.add_argument("--max-candles", type=int, default=None, help="Stop after fetching this many candles (for initial crawl)")
    return parser.parse_args()


def default_save_path() -> str:
    base_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(base_dir, exist_ok=True)
    filename = "btc_ohlcv_robust_1h_since_2022.csv"
    return os.path.join(base_dir, filename)


def write_header_if_needed(path: str):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "datetime", "open", "high", "low", "close", "volume", "exchange"])


def tail_last_timestamp(path: str):
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
    seen: Set[int] = set()
    ordered: List[int] = []
    if not os.path.exists(path):
        return seen, ordered
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
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
    appended = 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            ts = r[0]
            if ts in seen_ts:
                continue
            dt = datetime.datetime.fromtimestamp(ts / 1000, datetime.UTC).isoformat()
            w.writerow([ts, dt, r[1], r[2], r[3], r[4], r[5], exchange_id])
            seen_ts.add(ts)
            appended += 1
    return appended


def get_exchange(exchange_id: str):
    if not hasattr(ccxt, exchange_id):
        raise RuntimeError(f"Exchange '{exchange_id}' 不存在于 ccxt")
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})
    exchange.load_markets()
    return exchange


def limit_for_exchange(exchange_id: str) -> int:
    if exchange_id in ("okx", "kraken"):
        return 100
    elif exchange_id in ("bybit", "bitget"):
        return 200
    return 1000


def safe_fetch_ohlcv(exchange, symbol: str, timeframe: str, since_ms: int, limit: int):
    backoff = 1
    while True:
        try:
            return exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)
        except ccxt.NetworkError:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except ccxt.ExchangeError as e:
            msg = str(e).lower()
            if "rate limit" in msg or "throttle" in msg or "too many requests" in msg or "429" in msg:
                time.sleep(max(exchange.rateLimit / 1000.0, 1.0))
                continue
            raise


def detect_gaps(sorted_ts: List[int], step_ms: int) -> List[Tuple[int, int]]:
    gaps: List[Tuple[int, int]] = []
    if not sorted_ts:
        return gaps
    prev = sorted_ts[0]
    for ts in sorted_ts[1:]:
        diff = ts - prev
        if diff > step_ms:
            # gap from prev+step to ts-step
            gaps.append((prev + step_ms, ts - step_ms))
        prev = ts
    return gaps


def backfill_gaps(exchanges: List, exchange_ids: List[str], symbol: str, timeframe: str, step_ms: int, path: str, seen_ts: Set[int]):
    _, ordered = load_existing_timestamps(path)
    gaps = detect_gaps(ordered, step_ms)
    if not gaps:
        print("Continuity check: no gaps detected")
        return
    print(f"Continuity check: detected {len(gaps)} gap segment(s)")
    for (gap_start, gap_end) in gaps:
        # number of missing candles in this gap
        n_missing = int((gap_end - gap_start) / step_ms) + 1
        print(f"Backfilling gap {gap_start}->{gap_end} ({n_missing} candles)")
        filled = 0
        for ex, ex_id in zip(exchanges, exchange_ids):
            limit = max(limit_for_exchange(ex_id), n_missing + 5)
            batch = safe_fetch_ohlcv(ex, symbol, timeframe, gap_start - step_ms, limit)
            if not batch:
                continue
            # keep only candles within [gap_start, gap_end]
            filtered = [r for r in batch if (r[0] >= gap_start and r[0] <= gap_end)]
            appended = append_rows_dedup(path, ex_id, filtered, seen_ts)
            filled += appended
            if filled >= n_missing:
                print(f"Gap {gap_start}->{gap_end} filled by {ex_id} (added {appended})")
                break
        if filled < n_missing:
            print(f"Warning: gap {gap_start}->{gap_end} partially filled ({filled}/{n_missing})")
    # Re-check
    _, ordered2 = load_existing_timestamps(path)
    gaps2 = detect_gaps(ordered2, step_ms)
    if not gaps2:
        print("Continuity check: all gaps resolved")
    else:
        print(f"Continuity check: {len(gaps2)} gap segment(s) remain")


def main():
    args = parse_args()
    save_path = args.save or default_save_path()
    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    write_header_if_needed(save_path)

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

    seen_ts, ordered_ts = load_existing_timestamps(save_path)
    last_ts = ordered_ts[-1] if ordered_ts else None

    if args.resume and last_ts is not None:
        start_ms = last_ts + step_ms
    else:
        start_ms = int(args.since)

    now_ms = primary.milliseconds()
    limit = limit_for_exchange(exchange_ids[0])
    total_appended = 0
    while start_ms < now_ms - step_ms:
        batch = safe_fetch_ohlcv(primary, args.symbol, args.timeframe, start_ms, limit)
        if not batch:
            break
        appended = append_rows_dedup(save_path, exchange_ids[0], batch, seen_ts)
        total_appended += appended
        last_dt = datetime.datetime.fromtimestamp(batch[-1][0] / 1000, datetime.UTC).isoformat()
        print(f"Fetched {len(batch)} (appended {appended}), last dt={last_dt}, total={total_appended}")
        start_ms = batch[-1][0] + step_ms
        time.sleep(max(primary.rateLimit / 1000.0, 0.5))
        if args.max_candles and total_appended >= args.max_candles:
            break

    print(f"Initial fetch appended={total_appended}. Running continuity check...")
    if not args.no_backfill:
        backfill_gaps(exchanges, exchange_ids, args.symbol, args.timeframe, step_ms, save_path, seen_ts)

    # final summary
    _, ordered_final = load_existing_timestamps(save_path)
    print(f"Final total candles={len(ordered_final)} saved to {save_path}")


if __name__ == "__main__":
    main()