#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/kline_adapter.py  →  现位置: crypto/kline_training/kline_adapter.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
K线训练数据适配器（OKX API 版）
=======================
从 OKX 公开API 分页拉取加密币K线数据，复用 crypto/market_strategy/indicators.py
计算技术指标（与期货/股票监控台参数一致），输出 lightweight-charts
所需的 JSON 格式。
"""

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# 复用策略中心的公共指标库 + 品类无关的指标/序列化层（chart_serialize）

# 指标计算与序列化已抽取到品类无关的 chart_serialize（加密币/期货/股票共用）。
# 此处 re-export，保持既有 `from kline_adapter import calc_all_indicators/df_to_chart_data`
# 等调用向后兼容。
from crypto.kline_training.chart_serialize import (ADX_PERIOD, ATR_PERIOD, RSI_PERIOD, WARMUP_BARS,
                             calc_rsi, calc_bollinger, calc_ma,
                             calc_all_indicators, df_to_chart_data)

logger = logging.getLogger(__name__)

# ====================================================================
#  OKX 公开 API（无需 API Key）
# ====================================================================

_OKX_BASE = "https://www.okx.com"
_OKX_CANDLES = "/api/v5/market/candles"                   # 最近约1440根，单页上限300
_OKX_HISTORY_CANDLES = "/api/v5/market/history-candles"   # 深历史，单页上限100
_OKX_INSTRUMENTS = "/api/v5/public/instruments"           # 交易对元信息（全量品种列表）
_OKX_TICKERS = "/api/v5/market/tickers"                   # 24h行情，仅用于按成交额排序
_OKX_UA = "Mozilla/5.0"

# 单次请求超时与每通道重试次数。取较小值以便网络异常时快速失败并切换通道，
# 最坏耗时 ≈ (超时 + 退避) × 重试 × 通道数，避免用户长时间空等
_OKX_TIMEOUT = 8
_OKX_RETRIES = 2

_OKX_BAR_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1H", "2H": "2H", "4H": "4H", "6H": "6H", "12H": "12H",
    "1D": "1D", "2D": "2D", "3D": "3D", "1W": "1W", "1M": "1M",
}

# ── 品种注册表 ──────────────────────────────────────────────
# 品种分两层来源，两层都可用，互不冲突：
#   1) CRYPTO_SYMBOLS：本地精选品种，首屏展示、离线也可用（不走网络）；
#      URL 里用短代码即可，如 /kline/crypto/NEAR
#   2) OKX /public/instruments：全量上线品种，由 get_all_symbols() 动态拉取
#      并缓存，无需在此维护；现货用短代码（BTC），永续用完整 instId
#      （BTC-USDT-SWAP），两种写法 resolve_inst_id() 都能解析。
CRYPTO_SYMBOLS = {
    "NEAR": {"display_name": "NEAR/USDT", "inst_id": "NEAR-USDT", "exchange": "OKX"},
    "BTC":  {"display_name": "BTC/USDT",  "inst_id": "BTC-USDT",  "exchange": "OKX"},
    "ETH":  {"display_name": "ETH/USDT",  "inst_id": "ETH-USDT",  "exchange": "OKX"},
    "SOL":  {"display_name": "SOL/USDT",  "inst_id": "SOL-USDT",  "exchange": "OKX"},
}

# 全量品种缓存：品种清单变动极慢，但请求本身要跨代理，故缓存并支持强制刷新
_ALL_SYMBOLS_TTL = 6 * 3600                      # 秒
_ALL_SYMBOLS_QUOTES = ("USDT",)                  # 只收 USDT 本位，避免列表冗长
_ALL_SYMBOLS_TYPES = ("SPOT", "SWAP")            # 现货 + 永续合约
_PROBE_INST_ID = "NEAR-USDT"                    # 连通性自检用的探针交易对

# instId / 币种代码的合法字符形状（同时充当 URL 参数的注入防线）
_INST_ID_RE = re.compile(r"^[A-Z0-9]{1,15}(?:-[A-Z0-9]{1,15}){1,3}$")

_all_symbols_cache = {"ts": 0.0, "data": [], "updated_at": None, "total": 0}
_all_symbols_lock = threading.Lock()

# 指标周期参数（ADX_PERIOD/ATR_PERIOD/RSI_PERIOD/WARMUP_BARS）已移至 chart_serialize

# ── 网络传输层 ────────────────────────────────────────────────
# 本机能否访问 OKX 完全取决于系统代理是否开启（实测：走代理 0.3s 可达，
# 直连 100% 超时）。而 urllib 的 ProxyHandler() 会在【构造时一次性】调用
# getproxies() 并缓存结果，因此绝不能在模块导入时创建全局 opener——
# 那样进程会永久冻结启动那一刻的代理状态，用户中途开启代理客户端也不会
# 生效，表现为"代码没问题却持续 10060 超时，必须重启 Flask 才好"。
# 这里改为每次请求动态构建 opener，并在代理/直连两条通道间自动回退。
_TRANSPORTS = ['proxy', 'direct']   # 尝试顺序，最近成功的通道置顶复用


def _explicit_proxy() -> str:
    """OKX_PROXY 环境变量可显式指定代理，优先级最高（如 http://127.0.0.1:7892）"""
    return os.environ.get('OKX_PROXY') or os.environ.get('okx_proxy') or ''


def _build_opener(transport: str):
    """按通道构建 opener；不做任何全局缓存，代理配置每次重新读取"""
    if transport == 'proxy':
        proxy = _explicit_proxy()
        if proxy:
            handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy})
        else:
            # 无参 ProxyHandler → 动态读取当前环境变量 / 注册表系统代理
            handler = urllib.request.ProxyHandler()
    else:
        handler = urllib.request.ProxyHandler({})   # 强制直连
    return urllib.request.build_opener(handler)


def _active_proxy() -> str:
    """当前生效的代理地址（供健康检查与错误提示使用）"""
    proxy = _explicit_proxy()
    if proxy:
        return proxy
    found = urllib.request.getproxies()
    return found.get('https') or found.get('http') or ''


def _promote(transport: str):
    """把本次成功的通道置顶，避免后续请求再走一遍已失败的通道"""
    if _TRANSPORTS[0] != transport:
        _TRANSPORTS.remove(transport)
        _TRANSPORTS.insert(0, transport)


def okx_request(url: str, timeout: int = None, retries: int = None) -> dict:
    """向 OKX 公开 API 发送 GET 请求（动态代理 + 双通道回退 + 退避重试）"""
    timeout = timeout or _OKX_TIMEOUT
    retries = retries or _OKX_RETRIES
    last_err = 'unknown'

    for transport in list(_TRANSPORTS):
        opener = _build_opener(transport)
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _OKX_UA})
                with opener.open(req, timeout=timeout) as resp:
                    result = json.loads(resp.read().decode())
                _promote(transport)
                return result
            except urllib.error.HTTPError as e:
                # 4xx 多为参数错误或风控拦截，换通道/重试都无意义，立即抛出
                if 400 <= e.code < 500 and e.code not in (408, 429):
                    raise RuntimeError(f"OKX API HTTP {e.code}: {e.reason}")
                last_err = f"HTTP {e.code}: {e.reason}"
            except Exception as e:   # URLError / timeout / JSON 解析失败
                last_err = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            time.sleep(0.4 * (attempt + 1))
        logger.warning("OKX 通道 %s 不可用（%s），尝试下一通道", transport, last_err)

    proxy = _active_proxy()
    hint = (f"系统代理指向 {proxy} 但不可用，请检查代理客户端是否正常运行/节点是否可连通境外"
            if proxy else
            "未检测到任何可用代理，而本机无法直连 OKX；请开启系统代理"
            "（Clash/V2Ray 等）或设置 OKX_PROXY 环境变量后重试")
    raise ConnectionError(f"OKX 行情接口连接失败（{last_err}）。{hint}")


def okx_healthcheck() -> Dict:
    """逐个探测代理/直连通道，返回可用性与延迟，用于前端故障提示"""
    url = f"{_OKX_BASE}{_OKX_CANDLES}?instId=NEAR-USDT&bar=1H&limit=1"
    checks = []
    for transport in ('proxy', 'direct'):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _OKX_UA})
            with _build_opener(transport).open(req, timeout=8) as resp:
                resp.read()
            checks.append({"transport": transport, "ok": True,
                           "proxy": _active_proxy() if transport == 'proxy' else '',
                           "latency_ms": round((time.time() - t0) * 1000)})
        except Exception as e:
            checks.append({"transport": transport, "ok": False,
                           "proxy": _active_proxy() if transport == 'proxy' else '',
                           "error": f"{type(e).__name__}: {getattr(e, 'reason', e)}"})
    return {
        "usable": any(c["ok"] for c in checks),
        "current_channel": _TRANSPORTS[0],
        "proxies": _active_proxy(),
        "checks": checks,
    }


# ====================================================================
#  从 OKX 获取 K 线数据（自动分页）
# ====================================================================

def _fetch_page(inst_id: str, bar: str, limit: int,
                after: int = None, history: bool = False) -> list:
    """拉取单页K线，返回 OKX 原始行（ts 降序）"""
    endpoint = _OKX_HISTORY_CANDLES if history else _OKX_CANDLES
    params = f"instId={inst_id}&bar={bar}&limit={limit}"
    if after:
        params += f"&after={after}"
    url = f"{_OKX_BASE}{endpoint}?{params}"
    result = okx_request(url)
    if result.get("code") != "0":
        raise RuntimeError(f"OKX API Error: {result.get('msg', 'unknown')}")
    return result.get("data", [])


def fetch_kline_okx(symbol: str = "NEAR", period: str = "1H",
                    limit: int = 300, after: int = None) -> pd.DataFrame:
    """分页拉取K线数据直至满足 limit 根，返回按时间升序的 OHLCV DataFrame

    列: ['ts', 'open', 'high', 'low', 'close', 'volume']，ts 为 UTC Timestamp。

    说明:
      - /market/candles 单页上限300且只覆盖最近约1440根；
        更早的数据自动切换 /market/history-candles（单页上限100）继续翻页。
      - after: 毫秒时间戳，只返回早于该时间的数据（用于惰性加载）。
    """
    cfg = CRYPTO_SYMBOLS.get(symbol.upper())
    if not cfg:
        raise ValueError(f"不支持的品种: {symbol}")

    inst_id = cfg["inst_id"]
    bar = _OKX_BAR_MAP.get(period, "1H")

    rows_all = []
    cursor = after
    use_history = False  # 先用 candles 端点，取空后自动降级到 history-candles
    max_pages = 40  # 防御性翻页上限

    for _ in range(max_pages):
        if len(rows_all) >= limit:
            break
        page_limit = 100 if use_history else 300
        rows = _fetch_page(inst_id, bar, page_limit, after=cursor, history=use_history)
        if not rows:
            if not use_history:
                # candles 端点已到边界，切换深历史端点继续
                use_history = True
                continue
            break
        rows_all.extend(rows)
        # 每页按 ts 降序排列，末行为本页最早的一根，作为下一页 after 游标
        cursor = rows[-1][0]

    if not rows_all:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

    # 去重（两个端点交界处可能重叠）+ 截取最近 limit 根 + 升序
    seen = set()
    records = []
    for r in rows_all:
        ts_ms = int(r[0])
        if ts_ms in seen:
            continue
        seen.add(ts_ms)
        records.append({
            "ts": pd.Timestamp(ts_ms, unit="ms", tz="UTC"),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        })

    records = records[:limit]
    records.reverse()
    return pd.DataFrame(records)


# ====================================================================
#  主查询函数
# ====================================================================

def get_kline_chart_data(symbol: str = "NEAR",
                         period: str = "1H",
                         limit: int = 500,
                         full_data: bool = False,
                         target_bars: int = 350) -> Dict:
    """从 OKX 分页获取K线数据 + 计算指标 + 序列化

    多拉取 WARMUP_BARS 根参与指标计算后裁剪，保证可见区段指标收敛。
    """
    want = target_bars if full_data else limit
    df = fetch_kline_okx(symbol, period, limit=want + WARMUP_BARS)

    if df.empty:
        return {"candles": [], "volumes": [], "indicators": {},
                "error": "无法获取K线数据"}

    df = calc_all_indicators(df)

    # 裁剪预热段，只保留最近 want 根
    if len(df) > want:
        df = df.tail(want).reset_index(drop=True)

    chart_data = df_to_chart_data(df, period=period)

    cfg = CRYPTO_SYMBOLS.get(symbol.upper(), {})
    chart_data["meta"] = {
        "symbol": symbol.upper(),
        "display_name": cfg.get("display_name", symbol),
        "period": period,
        "exchange": cfg.get("exchange", ""),
        "bar_count": len(chart_data["candles"]),
        "date_range": {
            "start": chart_data["candles"][0]["time"] if chart_data["candles"] else None,
            "end": chart_data["candles"][-1]["time"] if chart_data["candles"] else None,
        } if chart_data["candles"] else None,
    }

    return chart_data


def load_more_kline(symbol: str = "NEAR",
                    period: str = "1H",
                    before_ts: int = None,
                    target_bars: int = 200) -> Dict:
    """加载指定时间点之前的更多历史K线数据（惰性加载）

    同样多拉取 WARMUP_BARS 根做指标预热后裁剪，避免分段计算导致的
    指标接缝跳变；并过滤掉 >= before_ts 的行防止与已有数据重叠。
    """
    if not before_ts:
        return {"candles": [], "volumes": [], "indicators": {},
                "meta": {"bar_count": 0}}

    # before_ts 是 lightweight-charts 的秒级时间戳，转毫秒
    after_ms = int(before_ts) * 1000
    df = fetch_kline_okx(symbol, period, limit=target_bars + WARMUP_BARS,
                         after=after_ms)

    if df.empty:
        return {"candles": [], "volumes": [], "indicators": {},
                "meta": {"bar_count": 0}}

    df = calc_all_indicators(df)

    # 防御性去重 + 裁剪预热段（tail 保留紧邻 before_ts 的最近 target_bars 根）
    df = df[df["ts"] < pd.Timestamp(after_ms, unit="ms", tz="UTC")]
    if len(df) > target_bars:
        df = df.tail(target_bars).reset_index(drop=True)

    chart_data = df_to_chart_data(df, period=period)
    chart_data["meta"] = {
        "symbol": symbol.upper(),
        "period": period,
        "bar_count": len(chart_data["candles"]),
    }
    return chart_data


def get_available_symbols() -> List[Dict]:
    result = []
    for code, cfg in CRYPTO_SYMBOLS.items():
        result.append({
            "code": code,
            "display_name": cfg["display_name"],
            "exchange": cfg["exchange"],
        })
    return result
