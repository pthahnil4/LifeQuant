import argparse
import csv
import datetime
import os
import time

import ccxt


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch BTC OHLCV data via CCXT")
    parser.add_argument("--exchange", default="okx", help="Exchange id, e.g., okx, bybit, bitget; binance 可能受地域限制")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair symbol")
    parser.add_argument("--timeframe", default="1m", help="Timeframe like 1m, 5m, 1h, 1d")
    parser.add_argument("--days", type=int, default=7, help="Fetch data from now - days (default 7 for quick run)")
    parser.add_argument("--since", type=int, default=None, help="Start timestamp (ms). Overrides --days if set")
    parser.add_argument("--save", default=None, help="Output CSV path")
    parser.add_argument("--resume", action="store_true", help="Resume from last timestamp in CSV if exists")
    parser.add_argument("--max-candles", type=int, default=3000, help="Stop after fetching this many candles (default 3000)")
    return parser.parse_args()


def get_exchange(exchange_id: str):
    if not hasattr(ccxt, exchange_id):
        raise RuntimeError(f"Exchange '{exchange_id}' 不存在于 ccxt")
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})
    try:
        exchange.load_markets()
    except Exception as e:
        msg = str(e).lower()
        if exchange_id == "binance" and ("restricted location" in msg or "451" in msg):
            print("Binance 受地域限制，自动切换到 okx")
            exchange = ccxt.okx({"enableRateLimit": True})
            exchange.load_markets()
        else:
            raise
    return exchange


def default_save_path(exchange_id: str, timeframe: str) -> str:
    base_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(base_dir, exist_ok=True)
    filename = f"btc_ohlcv_{exchange_id}_{timeframe}.csv"
    return os.path.join(base_dir, filename)


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


def write_header_if_needed(path: str):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "datetime", "open", "high", "low", "close", "volume", "exchange"])


def append_rows(path: str, exchange_id: str, rows):
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            ts = r[0]
            dt = datetime.datetime.fromtimestamp(ts / 1000, datetime.UTC).isoformat()
            w.writerow([ts, dt, r[1], r[2], r[3], r[4], r[5], exchange_id])


def fetch_iter(exchange, symbol: str, timeframe: str, since_ms: int, limit: int):
    backoff = 1
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)
            return ohlcv
        except ccxt.NetworkError:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except ccxt.ExchangeError as e:
            msg = str(e).lower()
            if "rate limit" in msg or "throttle" in msg or "too many requests" in msg or "429" in msg:
                time.sleep(max(exchange.rateLimit / 1000.0, 1.0))
                continue
            raise


def main():
    args = parse_args()
    exchange = get_exchange(args.exchange)
    if args.symbol not in exchange.symbols:
        raise RuntimeError(f"交易所 {args.exchange} 不支持交易对 {args.symbol}")
    timeframe_ms = exchange.parse_timeframe(args.timeframe) * 1000
    if args.save is None:
        save_path = default_save_path(exchange.id, args.timeframe)
    else:
        save_path = args.save
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    write_header_if_needed(save_path)

    last_ts = tail_last_timestamp(save_path) if args.resume else None
    if args.since is not None:
        start_ms = int(args.since)
    elif last_ts is not None:
        start_ms = last_ts + timeframe_ms
    else:
        start_ms = int((datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=args.days)).timestamp() * 1000)

    # conservative per-exchange limits
    limit = 1000
    if exchange.id in ("okx", "kraken"):
        limit = 100
    elif exchange.id in ("bybit", "bitget"):
        limit = 200

    total = 0
    while True:
        now_ms = exchange.milliseconds()
        if start_ms >= now_ms - timeframe_ms:
            break
        batch = fetch_iter(exchange, args.symbol, args.timeframe, start_ms, limit)
        if not batch:
            break
        append_rows(save_path, exchange.id, batch)
        total += len(batch)
        last_dt = datetime.datetime.fromtimestamp(batch[-1][0] / 1000, datetime.UTC).isoformat()
        print(f"Fetched {len(batch)} candles, last dt={last_dt}, total={total}")
        start_ms = batch[-1][0] + timeframe_ms
        time.sleep(max(exchange.rateLimit / 1000.0, 0.5))
        if args.max_candles and total >= args.max_candles:
            break

    print(f"Saved {total} candles to {save_path}")


if __name__ == "__main__":
    main()