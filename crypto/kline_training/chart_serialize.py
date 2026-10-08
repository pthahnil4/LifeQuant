#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/chart_serialize.py  →  现位置: crypto/kline_training/chart_serialize.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
图表指标计算与序列化（品类无关）
================================
从 ``kline_adapter.py`` 抽取，供加密币 / 期货 / 股票三品类共用，保证训练页
指标口径与监控台 / Pro3 策略完全一致（复用 ``crypto/market_strategy/indicators.py``）。

约定
----
- 输入：标准化 OHLCV DataFrame，至少含列 ``ts``（pandas Timestamp）、
  ``open`` / ``high`` / ``low`` / ``close``，可选 ``volume``。
- 输出：lightweight-charts 所需的 ``candles`` / ``volumes`` / ``indicators`` JSON。

时间戳处理
----------
``_ts_to_seconds`` 统一把 Timestamp 转为 POSIX 秒。对期货/股票的「北京时间
naive」时间戳，providers 会先按「伪 UTC」方式转换（详见 providers.base），
使图表横轴显示的日期与交易所本地墙钟一致，且与服务器时区无关、可复现。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import sys
from typing import Dict, List

import numpy as np
import pandas as pd

# 复用策略中心的公共指标库（保证训练页指标与监控台/Pro3策略一致）

from crypto.market_strategy.indicators import (calc_atr as _calc_atr_wilder,
                                   calc_sar as _calc_sar_std,
                                   calculate_adx, calculate_macd)

# 指标周期参数（与 strategies/pro3 策略保持一致）
ADX_PERIOD = 9
ATR_PERIOD = 9
RSI_PERIOD = 14

# 指标预热K线数：EMA/RSI/ADX 等递推指标需要足够前置历史才能收敛，
# 多拉取该数量的K线参与计算后再裁剪，避免可见区段首部指标失真
WARMUP_BARS = 150


# ====================================================================
#  技术指标计算（复用 crypto/market_strategy/indicators.py）
# ====================================================================

def calc_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> pd.DataFrame:
    """RSI（标准 Wilder 平滑，alpha=1/period）"""
    df = df.copy()
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))
    return df


def calc_bollinger(df: pd.DataFrame, period: int = 20,
                   std_dev: float = 2.0) -> pd.DataFrame:
    """布林带（BOLL_MID / BOLL_UP / BOLL_DN）"""
    df = df.copy()
    df["BOLL_MID"] = df["close"].rolling(window=period).mean()
    boll_std = df["close"].rolling(window=period).std()
    df["BOLL_UP"] = df["BOLL_MID"] + std_dev * boll_std
    df["BOLL_DN"] = df["BOLL_MID"] - std_dev * boll_std
    return df


def calc_ma(df: pd.DataFrame, periods: List[int] = None) -> pd.DataFrame:
    """移动平均线（默认 MA5/10/20/60）"""
    if periods is None:
        periods = [5, 10, 20, 60]
    df = df.copy()
    for p in periods:
        df[f"MA{p}"] = df["close"].rolling(window=p).mean()
    return df


def calc_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """统一指标入口：MACD/ADX/ATR/SAR 复用策略中心实现，参数与监控台一致"""
    df = calculate_macd(df)                       # DIF / DEA / MACD (12,26,9)
    df = calc_rsi(df)                             # RSI (Wilder 14)
    df = calculate_adx(df, period=ADX_PERIOD)     # ADX / +DI / -DI (9)
    df["ATR"] = _calc_atr_wilder(df, period=ATR_PERIOD)  # ATR (Wilder 9)
    df = calc_bollinger(df)
    df = calc_ma(df, periods=[5, 10, 20, 60])
    df["SAR"] = _calc_sar_std(df)                 # Parabolic SAR (Wilder 标准)
    return df


# ====================================================================
#  数据序列化（lightweight-charts JSON 格式）
# ====================================================================

def _ts_to_seconds(ts) -> int:
    """把 Timestamp / 数值时间统一转为 POSIX 秒（int）"""
    if isinstance(ts, pd.Timestamp):
        return int(ts.timestamp())
    return int(ts)


def df_to_chart_data(df: pd.DataFrame, period: str = "1H") -> Dict:
    """把含指标列的 OHLCV DataFrame 序列化为 lightweight-charts JSON

    读取每行 ``ts`` 列作为时间戳，输出 candles / volumes / indicators 三段。
    """
    candles = []
    volumes = []
    indicators = {
        "macd": {"dif": [], "dea": [], "histogram": []},
        "rsi": [],
        "adx": [],
        "plus_di": [], "minus_di": [],
        "atr": [],
        "boll": {"mid": [], "upper": [], "lower": []},
        "ma": {"ma5": [], "ma10": [], "ma20": [], "ma60": []},
        "sar": [],
    }

    for _, row in df.iterrows():
        t = _ts_to_seconds(row["ts"])
        o = round(float(row["open"]), 6)
        h = round(float(row["high"]), 6)
        l = round(float(row["low"]), 6)
        c = round(float(row["close"]), 6)
        v = round(float(row.get("volume", 0)), 4)

        candles.append({"time": t, "open": o, "high": h, "low": l, "close": c})
        volumes.append({
            "time": t, "value": v,
            "color": "#26a69a80" if c >= o else "#ef535080",
        })

        dif = row.get("DIF")
        dea = row.get("DEA")
        macd = row.get("MACD")
        if pd.notna(dif) and pd.notna(dea) and pd.notna(macd):
            indicators["macd"]["dif"].append({"time": t, "value": round(float(dif), 6)})
            indicators["macd"]["dea"].append({"time": t, "value": round(float(dea), 6)})
            indicators["macd"]["histogram"].append({
                "time": t, "value": round(float(macd), 6),
                "color": "#26a69a" if float(macd) >= 0 else "#ef5350",
            })

        rsi = row.get("RSI")
        if pd.notna(rsi):
            indicators["rsi"].append({"time": t, "value": round(float(rsi), 2)})

        adx = row.get("ADX")
        if pd.notna(adx):
            indicators["adx"].append({"time": t, "value": round(float(adx), 2)})
        plus_di = row.get("+DI")
        if pd.notna(plus_di):
            indicators["plus_di"].append({"time": t, "value": round(float(plus_di), 2)})
        minus_di = row.get("-DI")
        if pd.notna(minus_di):
            indicators["minus_di"].append({"time": t, "value": round(float(minus_di), 2)})

        atr = row.get("ATR")
        if pd.notna(atr):
            indicators["atr"].append({"time": t, "value": round(float(atr), 6)})

        for key, lbl in [("BOLL_MID", "mid"), ("BOLL_UP", "upper"), ("BOLL_DN", "lower")]:
            val = row.get(key)
            if pd.notna(val):
                indicators["boll"][lbl].append({"time": t, "value": round(float(val), 6)})

        for p, key in [(5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")]:
            ma_val = row.get(f"MA{p}")
            if pd.notna(ma_val):
                indicators["ma"][key].append({"time": t, "value": round(float(ma_val), 6)})

        sar = row.get("SAR")
        if pd.notna(sar):
            indicators["sar"].append({"time": t, "value": round(float(sar), 6)})

    return {
        "candles": candles,
        "volumes": volumes,
        "indicators": indicators,
    }
