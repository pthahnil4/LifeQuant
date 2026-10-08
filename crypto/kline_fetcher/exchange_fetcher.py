# -*- coding: utf-8 -*-
"""
交易所抓取模块
==============

封装与 ccxt 交易所交互的所有逻辑：

  - 建立交易所实例        get_exchange
  - 单批次抓取上限计算    limit_for_exchange
  - 带重试的安全抓取      safe_fetch_ohlcv

重试策略（取原 1m 脚本中最健壮的版本）：
  - NetworkError          : 指数退避重试
  - 限流 429/throttle     : 按 exchange.rateLimit 等待后重试
  - 5xx 服务器错误        : 更长等待时间后重试
  - 超过最大重试次数       : 跳过本批次（返回空列表），由上层决定后续处理
"""

import time

import ccxt


def get_exchange(exchange_id: str, trust_env: bool = True):
    """
    根据 id 创建 ccxt 交易所实例并加载市场信息。

    trust_env=True 时会读取系统代理环境变量（HTTP(S)_PROXY），
    便于在需要代理的网络环境下访问交易所 API。
    """
    if not hasattr(ccxt, exchange_id):
        raise RuntimeError(f"Exchange '{exchange_id}' 不存在于 ccxt")
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True, "requests_trust_env": trust_env})
    exchange.load_markets()
    return exchange


def limit_for_exchange(exchange_id: str, timeframe: str = "1m") -> int:
    """
    返回单批次抓取的 K 线根数上限。

    - 1m 周期统一使用 300（原 1m 脚本行为）
    - 其余周期按交易所保守限制：okx/kraken=100, bybit/bitget=200, 其他=1000
    """
    if timeframe == "1m":
        return 300
    if exchange_id in ("okx", "kraken"):
        return 100
    if exchange_id in ("bybit", "bitget"):
        return 200
    return 1000


def safe_fetch_ohlcv(exchange, symbol: str, timeframe: str, since_ms: int, limit: int):
    """
    带重试机制的安全抓取，返回 ccxt OHLCV 列表；超过重试上限返回 []。
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
