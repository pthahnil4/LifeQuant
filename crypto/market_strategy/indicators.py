#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/indicators.py  →  现位置: crypto/market_strategy/indicators.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
"""
公共技术指标函数库
==================
从 futures/pro3_strategy.py 和 stocks/stock_pro3_strategy.py 中提取的
共享指标计算函数，供所有策略复用。

包含指标:
  - calculate_adx:    ADX 平均趋向指数（含 +DI / -DI / ATR）
  - calc_sar:         Parabolic SAR 抛物线转向
  - calc_atr:         ATR 平均真实波幅（Wilder 标准）
  - get_adaptive_smooth_weight: ADX 自适应平滑权重
  - calculate_ema:    EMA 指数移动平均线
  - calculate_macd:   MACD 指标
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from typing import Tuple

import numpy as np
import pandas as pd


# ====================================================================
#  ADX — 平均趋向指数
# ====================================================================

def calculate_adx(df: pd.DataFrame, period: int = 9) -> pd.DataFrame:
    """计算 ADX 指标（平均趋向指数）

    Parameters
    ----------
    df : pd.DataFrame
        必须包含 'high', 'low', 'close' 列。
    period : int
        ADX 计算周期，默认 9。

    Returns
    -------
    pd.DataFrame
        附加以下列：
        - ADX: 平均趋向指数
        - +DI: 正向趋向指标
        - -DI: 负向趋向指标
        - ATR: 平均真实波幅（ADX 版本）
    """
    data = df.copy()

    # 真实波幅 TR
    data['prev_close'] = data['close'].shift(1)
    data['tr1'] = data['high'] - data['low']
    data['tr2'] = abs(data['high'] - data['prev_close'])
    data['tr3'] = abs(data['low'] - data['prev_close'])
    data['TR'] = data[['tr1', 'tr2', 'tr3']].max(axis=1)

    # 趋向变动 DM
    data['high_diff'] = data['high'] - data['high'].shift(1)
    data['low_diff'] = data['low'].shift(1) - data['low']

    data['+DM'] = np.where(
        (data['high_diff'] > data['low_diff']) & (data['high_diff'] > 0),
        data['high_diff'], 0)
    data['-DM'] = np.where(
        (data['low_diff'] > data['high_diff']) & (data['low_diff'] > 0),
        data['low_diff'], 0)

    # 指数加权平滑
    alpha = 1.0 / period
    data['ATR'] = data['TR'].ewm(alpha=alpha, adjust=False).mean()
    data['+DM_smooth'] = data['+DM'].ewm(alpha=alpha, adjust=False).mean()
    data['-DM_smooth'] = data['-DM'].ewm(alpha=alpha, adjust=False).mean()

    # DI
    data['+DI'] = 100 * (data['+DM_smooth'] / data['ATR'].replace(0, np.nan))
    data['-DI'] = 100 * (data['-DM_smooth'] / data['ATR'].replace(0, np.nan))

    # DX → ADX
    data['DI_sum'] = data['+DI'] + data['-DI']
    data['DI_diff'] = abs(data['+DI'] - data['-DI'])
    data['DX'] = np.where(
        data['DI_sum'] != 0,
        100 * (data['DI_diff'] / data['DI_sum']), 0)
    data['ADX'] = data['DX'].ewm(alpha=alpha, adjust=False).mean()

    # 清理中间列
    drop_cols = ['prev_close', 'tr1', 'tr2', 'tr3', 'TR',
                 'high_diff', 'low_diff', '+DM', '-DM',
                 '+DM_smooth', '-DM_smooth', 'DI_sum', 'DI_diff', 'DX']
    data = data.drop(columns=[c for c in drop_cols if c in data.columns])

    return data


# ====================================================================
#  Parabolic SAR — 抛物线转向
# ====================================================================

def calc_sar(df: pd.DataFrame,
             af_start: float = 0.02,
             af_step: float = 0.02,
             af_max: float = 0.20) -> pd.Series:
    """计算 Parabolic SAR（抛物线转向）— Wilder 标准实现

    Parameters
    ----------
    df : pd.DataFrame
        必须包含 'high', 'low', 'close' 列。
    af_start : float
        初始加速因子，默认 0.02。
    af_step : float
        加速因子步长，默认 0.02。
    af_max : float
        最大加速因子，默认 0.20。

    Returns
    -------
    pd.Series
        SAR 值序列。
    """
    n = len(df)
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values

    sar = np.full(n, np.nan)
    trend = np.zeros(n, dtype=int)  # 1=上升, -1=下降
    ep = np.full(n, np.nan)
    af = np.full(n, np.nan)

    if n < 5:
        return pd.Series(sar, index=df.index)

    # 初始趋势判断（前5根K线）
    if close[4] >= close[0]:
        trend[4] = 1
        sar[4] = low[:5].min()
        ep[4] = high[:5].max()
    else:
        trend[4] = -1
        sar[4] = high[:5].max()
        ep[4] = low[:5].min()
    af[4] = af_start

    for i in range(5, n):
        prev_trend = trend[i - 1]
        prev_sar = sar[i - 1]
        prev_ep = ep[i - 1]
        prev_af = af[i - 1]

        # 当前 SAR
        sar[i] = prev_sar + prev_af * (prev_ep - prev_sar)

        # 限制 SAR 不超过前两根K线的最低价(上升)/最高价(下降)
        if prev_trend == 1:
            sar[i] = min(sar[i], low[i - 1],
                         low[i - 2] if i >= 2 else low[i - 1])
        else:
            sar[i] = max(sar[i], high[i - 1],
                         high[i - 2] if i >= 2 else high[i - 1])

        # 判断是否反转
        if prev_trend == 1:
            if low[i] < sar[i]:
                trend[i] = -1
                sar[i] = prev_ep
                ep[i] = low[i]
                af[i] = af_start
            else:
                trend[i] = 1
                if high[i] > prev_ep:
                    ep[i] = high[i]
                    af[i] = min(prev_af + af_step, af_max)
                else:
                    ep[i] = prev_ep
                    af[i] = prev_af
        else:
            if high[i] > sar[i]:
                trend[i] = 1
                sar[i] = prev_ep
                ep[i] = high[i]
                af[i] = af_start
            else:
                trend[i] = -1
                if low[i] < prev_ep:
                    ep[i] = low[i]
                    af[i] = min(prev_af + af_step, af_max)
                else:
                    ep[i] = prev_ep
                    af[i] = prev_af

    return pd.Series(sar, index=df.index)


# ====================================================================
#  ATR — 平均真实波幅
# ====================================================================

def calc_atr(df: pd.DataFrame, period: int = 9) -> pd.Series:
    """计算 ATR（平均真实波幅），标准 Wilder 方法

    Parameters
    ----------
    df : pd.DataFrame
        必须包含 'high', 'low', 'close' 列。
    period : int
        周期，默认 9。

    Returns
    -------
    pd.Series
        ATR 值序列。
    """
    high, low, close = df['high'], df['low'], df['close']
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


# ====================================================================
#  ADX 自适应平滑权重
# ====================================================================

def get_adaptive_smooth_weight(adx_value: float,
                                base_weight: float = 0.7) -> Tuple[float, float]:
    """根据 ADX 计算自适应平滑权重

    Parameters
    ----------
    adx_value : float
        当前 ADX 值。
    base_weight : float
        基础权重（默认 0.7）。

    Returns
    -------
    hist_weight : float
        历史权重（平滑系数）。
    curr_weight : float
        当前权重（1 - hist_weight）。

    权重映射:
        ADX=0  → 0.70 (更敏感)
        ADX=20 → 0.80 (均衡)
        ADX=40 → 0.90 (更保守)
        ADX=60 → 1.00 (极保守)
    """
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    curr_weight = 1.0 - hist_weight
    return hist_weight, curr_weight


# ====================================================================
#  EMA — 指数移动平均线
# ====================================================================

def calculate_ema(data: pd.Series, period: int) -> pd.Series:
    """计算指数移动平均线（EMA）"""
    return data.ewm(span=period, adjust=False).mean()


# ====================================================================
#  MACD — 指标
# ====================================================================

def calculate_macd(df: pd.DataFrame,
                   fast: int = 12,
                   slow: int = 26,
                   signal: int = 9) -> pd.DataFrame:
    """计算 MACD 指标

    Parameters
    ----------
    df : pd.DataFrame
        必须包含 'close' 列。
    fast, slow, signal : int
        MACD 参数。

    Returns
    -------
    pd.DataFrame
        附加列: 'EMA12', 'EMA26', 'DIF', 'DEA', 'MACD'。
    """
    data = df.copy()
    data['EMA12'] = calculate_ema(data['close'], fast)
    data['EMA26'] = calculate_ema(data['close'], slow)
    data['DIF'] = data['EMA12'] - data['EMA26']
    data['DEA'] = calculate_ema(data['DIF'], signal)
    data['MACD'] = 2.0 * (data['DIF'] - data['DEA'])
    return data
