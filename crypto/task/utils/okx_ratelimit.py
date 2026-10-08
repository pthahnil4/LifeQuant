#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
OKX 只读接口限频 / 退避（问题#8）
================================

为什么要有这个文件
------------------
改前全项目对 OKX 的调用是「想到就调」：没有任何节流，也没有对 50011 的退避。
实盘调度是多线程长驻进程（主交易循环 + 告警监控 + 行情扫描 + 前端刷新页面），
同一批只读接口（ticker / 订单查询 / 持仓 / 余额）会被多个线程同时高频打出。
OKX 对每个接口都有「N 次/2 秒」的限频，超限回 code=50011，客户端把它当
"查询失败"处理 —— 表现就是持仓查不到、价格取不到、下一轮又去查，
越失败越勤查，形成自我放大的请求风暴。

本模块只做两件事：
  1. **节流**：按接口分组做令牌桶，取不到令牌时先等待（最多 acquire_timeout 秒），
     等待超时才抛 RateLimited；调用方原有的 except 分支按"这次查询失败"降级，
     交易循环跳过本轮，而不是把 50011 打成雪崩。
  2. **退避**：命中限频特征（code=50011 / HTTP 429 / Too Many Requests）或
     传输层抖动（httpx 超时、连接被重置）时，指数退避 + 抖动后重试。

边界（重要）
------------
- 只给**只读**接口用。下单/撤单/改单/设杠杆一律**不许**走 limited()，
  因为自动重发可能造成重复开仓——那类风险要靠 clOrdId 反查定性
  （见 trade_executor.gen_cl_ord_id），不能靠重试。
- 令牌桶是**进程内**共享。OKX 的限频按 API key 计，当前实盘只有一个账号，
  所以进程级分桶够用；若将来同进程并发跑多账号，需要把 bucket key 细分到账号。
- 已接线范围：trade_executor 的只读调用点、api_routes 全部只读路由、
  market_scanner / star_market / batch_trend_updater / instrument_spec /
  plan_routes 余额代理，以及 2026-09-10 补上的两个盲区：alert_monitor（K线与
  持仓查询）与 crypto/app.py 里四处直调的只读接口。接线清单以
  `_smoke_okx_ratelimit.py` 的 `_TARGETS` 为准，新增调用 OKX 的模块要跟着加。
  **实盘 K 线抓取路径 pro3_singletimeframe._request_candles 有意未接线**：
  那是交易核心的数据源，按用户决定保持原样直连，不给调度主链路新增
  任何可能卡住取数的环节。后来者若看到「K 线没限频」，那是明确决定，
  不是漏改；要接请先与用户确认。

限频数值来源
------------
没有逐一对照官方限频表（本次编写时 OKX 文档站不可达），下面的默认值只取估算
上限的一半左右：本项目同时交易的币种只有几个，常规用量根本碰不到桶，只在
循环失控 / 多线程叠加 / 重试风暴时才卡住。保守的意义是「宁可自己限速，
也不要被交易所限速」。需要调整时用环境变量覆盖，
格式 `OKX_RL_<组名大写>=每秒速率[:桶容量]`，例如 `OKX_RL_MARKET_TICKER=8:16`。
总开关 `OKX_RATELIMIT_ENABLED=0` 时完全直通（不节流、不重试），用于排查问题。
"""

import logging
import os
import random
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# 分组默认速率（次/秒），桶容量默认 = 速率 × 2 秒（对齐 OKX 的「每 2 秒」窗口）
#
# 取值口径：官方限频大多在「每秒 5~10 次」量级，这里一律只用到它的
# 一半左右。目的是「不干扰正常使用」：前端轮询 + 交易主循环 + 扫描手动的
# 常规用量根本碰不到桶，只有在循环失控、多线程叠加、重试风暴时才会被卡住。
# ---------------------------------------------------------------------
DEFAULT_RATES: Dict[str, float] = {
    'market_ticker': 5.0,    # GET /market/ticker、/market/tickers、/market/orderbook
    'market_candles': 5.0,   # GET /market/candles（K 线，扫全市场时最密集）
    'order_query': 5.0,      # GET /trade/orders-pending、/trade/order
    'algo_query': 3.0,       # GET /trade/algo-orders、/trade/algo-order
    'positions': 5.0,        # GET /account/positions
    'balance': 3.0,          # GET /account/balance、/account/max-order-size
    'account_risk': 3.0,     # GET /account/account-position-risk（维持保证金/账户等级）
    'instruments': 3.0,      # GET /public/instruments
    'leverage': 3.0,         # GET /account/leverage-info
}

_ACQUIRE_TIMEOUT = float(os.getenv('OKX_RL_ACQUIRE_TIMEOUT', '2.0'))  # 等令牌上限(秒)
_DEFAULT_RETRIES = int(os.getenv('OKX_RL_RETRIES', '2'))              # 退避重试次数
_DEFAULT_BASE_DELAY = float(os.getenv('OKX_RL_BASE_DELAY', '0.6'))    # 首次退避(秒)


def _enabled() -> bool:
    return os.getenv('OKX_RATELIMIT_ENABLED', '1').strip().lower() not in ('0', 'false', 'no', 'off')


class RateLimited(Exception):
    """等待令牌超时：本轮只读查询放弃，交由调用方按查询失败降级。"""

    def __init__(self, group: str, wait: float):
        self.group = group
        self.wait = wait
        super().__init__(f"OKX 只读接口限频等待超时 [{group}]（已等 {wait:.2f}s）")


# ---------------------------------------------------------------------
# 令牌桶
# ---------------------------------------------------------------------
class _Bucket:
    __slots__ = ('rate', 'capacity', 'tokens', 'ts', 'lock')

    def __init__(self, rate: float, capacity: float):
        self.rate = max(0.05, float(rate))
        self.capacity = max(1.0, float(capacity))
        self.tokens = self.capacity
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, timeout: float) -> Tuple[bool, float]:
        """取一个令牌；最多等 timeout 秒。返回 (是否取到, 实际等待秒数)。"""
        deadline = time.monotonic() + max(0.0, timeout)
        waited = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                dt = now - self.ts
                if dt > 0:
                    self.tokens = min(self.capacity, self.tokens + dt * self.rate)
                    self.ts = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True, waited
                need = (1.0 - self.tokens) / self.rate
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, waited
            nap = min(need, remaining)
            time.sleep(nap)
            waited += nap


_buckets: Dict[str, _Bucket] = {}
_buckets_lock = threading.Lock()
_stats: Dict[str, Dict[str, int]] = {}
_stats_lock = threading.Lock()
_warned_groups: set = set()


def _bucket_for(group: str) -> Tuple[_Bucket, bool]:
    """取（并按需创建）某分组的令牌桶，返回 (桶, 是否为未知分组)。"""
    bucket = _buckets.get(group)
    known = group in DEFAULT_RATES
    if bucket is not None:
        return bucket, known
    with _buckets_lock:
        bucket = _buckets.get(group)
        if bucket is None:
            rate, capacity = _parse_limit(group)
            bucket = _Bucket(rate, capacity)
            _buckets[group] = bucket
            _stats[group] = {'granted': 0, 'throttled': 0, 'refused': 0}
    return bucket, known


def _parse_limit(group: str) -> Tuple[float, float]:
    """环境变量覆盖：OKX_RL_<GROUP>=速率[:容量]。解析失败回落默认值并告警一次。"""
    rate = DEFAULT_RATES.get(group, 2.0)
    capacity = rate * 2
    raw = os.getenv(f"OKX_RL_{group.upper()}")
    if raw:
        try:
            parts = raw.replace('：', ':').split(':')
            rate = float(parts[0])
            capacity = float(parts[1]) if len(parts) > 1 and parts[1] else rate * 2
        except (ValueError, IndexError):
            if group not in _warned_groups:
                _warned_groups.add(group)
                logger.warning(f"[限频] 环境变量 OKX_RL_{group.upper()}={raw!r} 无法解析，使用默认 {rate}/s")
    return rate, capacity


def _stat(group: str, key: str):
    with _stats_lock:
        s = _stats.setdefault(group, {'granted': 0, 'throttled': 0, 'refused': 0})
        s[key] = s.get(key, 0) + 1


def get_stats() -> Dict[str, Dict[str, int]]:
    """各组计数：granted 放行次数 / throttled 命中限频或退避重试 / refused 等不到令牌。"""
    with _stats_lock:
        return {g: dict(v) for g, v in _stats.items()}


def reset_state():
    """清空桶与计数（冒烟测试用；生产代码不要调）。"""
    with _buckets_lock:
        _buckets.clear()
    with _stats_lock:
        _stats.clear()
    _warned_groups.clear()


# ---------------------------------------------------------------------
# 限频特征识别
# ---------------------------------------------------------------------
# OKX 业务层限频固定回 50011；极端情况下网关直接回 HTTP 429，
# SDK 不做 raise_for_status，仍会把响应体 json 出来，所以报文措辞也一并认。
_THROTTLE_CODES = {'50011', '429'}
_THROTTLE_HINTS = ('too many requests', 'rate limit', 'requests limit')

# 传输层抖动特征：这类异常重试是安全的（只读幂等）
_NET_HINTS = ('timeout', 'timed out', 'connection', 'connect', 'reset',
              'broken pipe', 'remote protocol', 'ssl', 'unavailable',
              'temporarily', 'eof occurred', 'read error')


def is_throttle_response(result: Any) -> bool:
    """响应体是不是「被限频」。"""
    if not isinstance(result, dict):
        return False
    code = str(result.get('code', '') or '')
    if code in _THROTTLE_CODES:
        return True
    msg = str(result.get('msg', '') or '').lower()
    return any(h in msg for h in _THROTTLE_HINTS)


def is_retryable_error(exc: BaseException) -> bool:
    """异常是不是可重试的传输层抖动（而不是代码 bug）。"""
    mod = str(getattr(type(exc), '__module__', '') or '')
    name = type(exc).__name__.lower()
    text = f"{name} {str(exc)}".lower()
    if mod.startswith(('httpx', 'requests', 'urllib3', 'socket', 'ssl')):
        return True
    return any(h in text for h in _NET_HINTS)


def _backoff_sleep(attempt: int, base_delay: float):
    time.sleep(base_delay * (2 ** attempt) * (0.8 + random.random() * 0.4))


# ---------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------
def limited(group: str, func: Callable, *args,
            retries: Optional[int] = None,
            base_delay: Optional[float] = None,
            acquire_timeout: Optional[float] = None,
            retry_on_error: bool = True,
            **kwargs) -> Any:
    """节流 + 退避地调用一个**只读** OKX 接口。

    Args:
        group: 分组名，见 DEFAULT_RATES；未知分组按 2/s 保守处理并告警一次
        func: 目标函数（SDK 方法）
        retries: 退避重试次数，None 用默认 2；**0 表示完全不重试**
        base_delay: 首次退避秒数
        acquire_timeout: 等令牌上限秒数
        retry_on_error: 传输层异常是否也在这里重试。调用方自己已有「异常 +
            重建连接」重试循环的地方（如 _get_positions_raw、_robust_market_call、
            clOrdId 反查）必须传 False，否则两层网络重试互相放大单轮耗时；
            传 False 后仍保留对交易所限频响应（50011）的退避，两者不重叠。

    Returns:
        func 的返回值（原样透传，包括 code!=0 的业务失败响应）

    Raises:
        RateLimited: 等不到令牌
        Exception:   重试耗尽后仍失败的原始异常
    """
    if not _enabled():
        return func(*args, **kwargs)

    bucket, known = _bucket_for(group)
    if not known and group not in _warned_groups:
        _warned_groups.add(group)
        logger.warning(f"[限频] 未登记分组 {group!r}，按默认 2/s 处理")

    n_retry = _DEFAULT_RETRIES if retries is None else int(retries)
    delay = _DEFAULT_BASE_DELAY if base_delay is None else float(base_delay)
    a_timeout = _ACQUIRE_TIMEOUT if acquire_timeout is None else float(acquire_timeout)

    attempt = 0
    while True:
        ok, waited = bucket.acquire(a_timeout)
        if not ok:
            _stat(group, 'refused')
            raise RateLimited(group, a_timeout)
        if waited > 0.05:
            logger.debug(f"[限频] [{group}] 等待令牌 {waited:.2f}s")

        try:
            result = func(*args, **kwargs)
        except Exception as e:
            retriable = retry_on_error and is_retryable_error(e)
            if retriable and attempt < n_retry:
                _stat(group, 'throttled')
                attempt += 1
                logger.warning(
                    f"[限频退避] [{group}] 传输层异常 {type(e).__name__}: {e!r}，"
                    f"第 {attempt}/{n_retry} 次退避重试")
                _backoff_sleep(attempt - 1, delay)
                continue
            raise

        if is_throttle_response(result) and attempt < n_retry:
            _stat(group, 'throttled')
            attempt += 1
            logger.warning(
                f"[限频退避] [{group}] 交易所回限频（code={result.get('code')}），"
                f"第 {attempt}/{n_retry} 次退避重试")
            _backoff_sleep(attempt - 1, delay)
            continue

        _stat(group, 'granted')
        return result
