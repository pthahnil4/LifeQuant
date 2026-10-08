#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
keltner_boll —— Keltner×BOLL 区间策略通用工具类（项目级）
==========================================================

由研究脚本 ``trend_strategy_boll_limit_scheduler_like copy 7.py`` 重构而来，
遵循 ``boll_limit_dualtimeframe.py`` 的适配器架构规范，供以下调用方使用：

1. 实盘调度器 ``crypto/task/trend_range_trader.py``
   —— 作为 B 仓位（区间波动仓）的默认执行引擎：
   · ATR 外扩 BOLL 边界（开仓轨外扩 entry_atr_mult×ATR、平仓轨外扩 exit_atr_mult×ATR）
   · 可切换的长周期趋势方向方法库（keltner / rsi_mid / vote3 …… 共 20 种）
   · 方向确认过滤（趋势方法方向与持仓方向冲突时暂停开仓，不拦平仓）
2. 回测/模拟盘 —— :func:`simulate` 纯 pandas 事件循环（不依赖 backtrader），
   与 copy 7 的 backtrader 回测语义一致（触边即入/触边即出/长周期反转剔除亏损单）。
3. 详情页扩展 —— :class:`KeltnerBollAdapter` 输出标准化 dict，可由
   ``strategy_util.get_strategy_detail`` 分发（接线方式见迁移文档）。

设计约束（与既有架构契约对齐）：
- 本模块**无模块级可变全局状态**：所有配置经 :class:`KeltnerBollConfig` 传入，
  多实例并发互不串扰（A/B 仓位策略切换状态隔离）；因此实盘路径**不需要**
  占用 pro3_locked 策略计算全局锁（该锁保护的是 pro3 引擎的模块级全局参数）。
- 取数复用 ``pro3_singletimeframe._fetch_kline_data``（自带分级 TTL 缓存 +
  run 级批量预热缓存 + 弱网降级），不另起 API 通道。
- 指标函数一律无副作用：不修改调用方传入的 DataFrame。

术语（首次出现附全称）：
- BOLL = Bollinger Bands（布林带，均值±N倍标准差通道）
- Keltner = Keltner Channel（肯特纳通道，EMA±N倍ATR通道）
- ATR = Average True Range（平均真实波幅，衡量单边波动大小的指标）
- EMA = Exponential Moving Average（指数移动平均）
- RSI = Relative Strength Index（相对强弱指标）
- B 仓位 = 双仓位架构中的区间波动仓（BUCKET_RANGE）

作者：AI Assistant
创建时间：2026-09-27
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time as _time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 供裸模块导入（import keltner_boll）与包导入（crypto.strategy.keltner_boll）两种方式
_STRATEGY_DIR = os.path.dirname(os.path.abspath(__file__))
_CRYPTO_DIR = os.path.dirname(_STRATEGY_DIR)
for _d in (_STRATEGY_DIR, _CRYPTO_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)


# ============================================================================
# 一、基础指标工具（纯函数，无副作用）
# ============================================================================

def ema(s: pd.Series, n: int) -> pd.Series:
    """指数移动平均（Exponential Moving Average）"""
    return s.ewm(span=n, adjust=False).mean()


def wma(s: pd.Series, n: int) -> pd.Series:
    """加权移动平均（Weighted Moving Average，线性权重）"""
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n, min_periods=n).apply(
        lambda x: float(np.dot(x, w) / w.sum()), raw=True)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """平均真实波幅（Average True Range），Wilder 指数平滑"""
    pc = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'],
                    (df['high'] - pc).abs(),
                    (df['low'] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """相对强弱指标（Relative Strength Index），Wilder 平滑"""
    d = s.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.ewm(alpha=1.0 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    # ad==0 的边界：整段无下跌（如单边上涨）→ RSI=100（超买），而非中性 50；
    # 涨跌皆无（au==0 且 ad==0，恒定价格）→ 50 中性。缺此处理会让纯趋势被误判中性。
    fallback = pd.Series(np.where(au.values > 0, 100.0, 50.0), index=out.index)
    return out.where(ad.values != 0, fallback).fillna(50.0)


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ADX（Average Directional Index，平均方向指数）计算。

    与 pro3_singletimeframe.calculate_adx 同口径；**返回副本**，
    不修改传入 df（区别于 copy 7 原脚本的原地写入）。
    """
    d = df.copy()
    d['prev_close'] = d['close'].shift(1)
    d['tr1'] = d['high'] - d['low']
    d['tr2'] = (d['high'] - d['prev_close']).abs()
    d['tr3'] = (d['low'] - d['prev_close']).abs()
    d['TR'] = d[['tr1', 'tr2', 'tr3']].max(axis=1)
    d['high_diff'] = d['high'] - d['high'].shift(1)
    d['low_diff'] = d['low'].shift(1) - d['low']
    d['+DM'] = np.where((d['high_diff'] > d['low_diff']) & (d['high_diff'] > 0),
                        d['high_diff'], 0)
    d['-DM'] = np.where((d['low_diff'] > d['high_diff']) & (d['low_diff'] > 0),
                        d['low_diff'], 0)
    alpha = 1 / period
    d['ATR_ADX'] = d['TR'].ewm(alpha=alpha, adjust=False).mean()
    d['+DM_smooth'] = d['+DM'].ewm(alpha=alpha, adjust=False).mean()
    d['-DM_smooth'] = d['-DM'].ewm(alpha=alpha, adjust=False).mean()
    d['+DI'] = 100 * (d['+DM_smooth'] / d['ATR_ADX'])
    d['-DI'] = 100 * (d['-DM_smooth'] / d['ATR_ADX'])
    d['DI_sum'] = d['+DI'] + d['-DI']
    d['DI_diff'] = (d['+DI'] - d['-DI']).abs()
    d['DX'] = np.where(d['DI_sum'] != 0, 100 * (d['DI_diff'] / d['DI_sum']), 0)
    d['ADX'] = d['DX'].ewm(alpha=alpha, adjust=False).mean()
    return d


def _hold(arr, allow_zero: bool = False) -> np.ndarray:
    """把 0(中性) 用上一个非零方向填充，让趋势段连续。
    allow_zero=True 时保留 0（用于需要明确"无方向"的方法）。"""
    out = np.zeros(len(arr), dtype=float)
    last = 0.0
    for i, v in enumerate(arr):
        v = 0.0 if not np.isfinite(v) else float(v)
        if v != 0.0:
            last = v
        out[i] = last if not allow_zero else v
    return out


def _sign(x, thr: float = 0.0) -> np.ndarray:
    """带死区的符号函数：>thr → +1，<-thr → -1，其余 0（NaN 按 0 处理）"""
    x = np.asarray(x, dtype=float)
    out = np.zeros(len(x))
    out[np.nan_to_num(x) > thr] = 1.0
    out[np.nan_to_num(x) < -thr] = -1.0
    return out


def value_to_dir(v) -> Optional[str]:
    """方向数值 → 'rise'/'fall'/None（|v|≥0.5 才算有方向）"""
    try:
        if v >= 0.5:
            return 'rise'
        if v <= -0.5:
            return 'fall'
    except Exception:
        return None
    return None


def dir_to_value(d) -> float:
    """'rise'/'fall'/其他 → +1/-1/0"""
    s = str(d)
    if s in ('rise', '多', 'long'):
        return 1.0
    if s in ('fall', '空', 'short'):
        return -1.0
    return 0.0


# ============================================================================
# 二、长周期趋势方向方法库（20 种，自 copy 7 移植，全部纯函数）
#     键名即 KeltnerBollConfig.trend_indicator 的合法取值
# ============================================================================

def _macd_adx_direction(df: pd.DataFrame) -> np.ndarray:
    """原版基线：ADX 自适应加权的平滑 MACD 柱状态机。
    权重 hist_weight = min(0.7 + ADX/200, 1)；用「柱 vs 平滑柱」和 MACD 零轴
    共同决定 rise/fall，未确认时进入 histogram 等待态。"""
    d = df if 'ADX' in df.columns else calculate_adx(df, period=14)
    macd_line = ema(d['close'], 12) - ema(d['close'], 26)
    signal_line = ema(macd_line, 9)
    hist = 2 * (macd_line - signal_line)
    adx = d['ADX'].values
    m = macd_line.values
    h = np.nan_to_num(hist.values)
    n = len(d)
    out = np.zeros(n)
    smoothed = None
    justice = "smoothed_histogram"
    deal = None
    modify = 0.0
    for i in range(n):
        adx_v = adx[i] if np.isfinite(adx[i]) else 0.0
        hw = min(0.7 + (adx_v / 200.0), 1.0)
        cw = 1.0 - hw
        smoothed = h[i] if smoothed is None else (smoothed * hw + h[i] * cw)
        if justice == "smoothed_histogram":
            if h[i] - smoothed > 0:
                if i > 0 and m[i - 1] > 0:
                    modify = 1.0
                else:
                    justice, deal = "histogram", "waitRise"
            else:
                if i > 0 and m[i - 1] < 0:
                    modify = -1.0
                else:
                    justice, deal = "histogram", "waitFall"
        elif justice == "histogram":
            if deal == "waitRise":
                if h[i] > 0:
                    modify, justice = 1.0, "smoothed_histogram"
                else:
                    modify = -1.0
            elif deal == "waitFall":
                if h[i] < 0:
                    modify, justice = -1.0, "smoothed_histogram"
                else:
                    modify = 1.0
        out[i] = modify
    return out


def _rsi_mid_direction(df: pd.DataFrame, period: int = 14,
                       hi: float = 55.0, lo: float = 45.0) -> np.ndarray:
    """RSI 中轴方向（带迟滞）：RSI>hi 多，RSI<lo 空，中间保持。
    用的不是超买超卖反转，而是力度中轴。"""
    r = rsi(df['close'], period).values
    raw = np.zeros(len(r))
    raw[r > hi] = 1.0
    raw[r < lo] = -1.0
    return _hold(raw)


def _cmo_direction(df: pd.DataFrame, period: int = 20, thr: float = 0.0) -> np.ndarray:
    """Chande 动量摆动（Chande Momentum Oscillator）：
    CMO = 100*(上涨和 − 下跌和)/(上涨和 + 下跌和)。"""
    d = df['close'].diff()
    up = d.clip(lower=0.0).rolling(period, min_periods=period).sum()
    dn = (-d).clip(lower=0.0).rolling(period, min_periods=period).sum()
    cmo = 100 * (up - dn) / (up + dn).replace(0.0, np.nan)
    return _hold(_sign(cmo.values, thr))


def _keltner_direction(df: pd.DataFrame, period: int = 20, mult: float = 2.0,
                       atr_n: int = 20) -> np.ndarray:
    """Keltner 通道突破方向：EMA ± mult×ATR。破上轨多、破下轨空，回归中轨保持。"""
    mid = ema(df['close'], period)
    a = atr(df, atr_n)
    up, lo = mid + mult * a, mid - mult * a
    raw = np.zeros(len(df))
    raw[(df['close'] > up).values] = 1.0
    raw[(df['close'] < lo).values] = -1.0
    return _hold(raw)


def _ichimoku_direction(df: pd.DataFrame) -> np.ndarray:
    """一目均衡表云图方向：价格在云上方为多、下方为空、云内保持。
    云图用 shift(先行跨) 保证可用信息只到当前 bar（严格因果）。"""
    h, l, c = df['high'], df['low'], df['close']
    tenkan = (h.rolling(9, min_periods=9).max() + l.rolling(9, min_periods=9).min()) / 2
    kijun = (h.rolling(26, min_periods=26).max() + l.rolling(26, min_periods=26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((h.rolling(52, min_periods=52).max()
               + l.rolling(52, min_periods=52).min()) / 2).shift(26)
    top = pd.concat([span_a, span_b], axis=1).max(axis=1)
    bot = pd.concat([span_a, span_b], axis=1).min(axis=1)
    raw = np.zeros(len(df))
    raw[(c > top).values] = 1.0
    raw[(c < bot).values] = -1.0
    return _hold(raw)


def _kama_direction(df: pd.DataFrame, period: int = 10, fast: int = 2,
                    slow: int = 30, k: int = 3) -> np.ndarray:
    """Kaufman 自适应均线（KAMA）方向：效率比调节平滑速度，方向取 KAMA 差分。"""
    c = df['close']
    change = (c - c.shift(period)).abs()
    vol = c.diff().abs().rolling(period, min_periods=period).sum()
    er = (change / vol.replace(0.0, np.nan)).fillna(0.0).clip(0, 1)
    fs, ss = 2.0 / (fast + 1), 2.0 / (slow + 1)
    sc = (er * (fs - ss) + ss) ** 2
    vals, scv = c.values.astype(float), sc.values.astype(float)
    out = np.full(len(vals), np.nan)
    prev = np.nan
    for i in range(len(vals)):
        if not np.isfinite(vals[i]):
            continue
        prev = vals[i] if not np.isfinite(prev) else prev + scv[i] * (vals[i] - prev)
        out[i] = prev
    kama = pd.Series(out, index=c.index)
    return _hold(_sign((kama - kama.shift(k)).values))


def _hma_direction(df: pd.DataFrame, period: int = 55, k: int = 3) -> np.ndarray:
    """Hull 低滞后均线（HMA）方向：方向取 HMA 差分，再要求价格站对边。"""
    c = df['close']
    half, root = max(1, period // 2), max(1, int(np.sqrt(period)))
    hma_line = wma(2 * wma(c, half) - wma(c, period), root)
    slope = _sign((hma_line - hma_line.shift(k)).values)
    side = np.where((c > hma_line).values, 1.0, -1.0)
    both = np.where(slope * side > 0, slope, 0.0)
    return _hold(both)


def _triple_ma_direction(df: pd.DataFrame, fast: int = 10, mid: int = 30,
                         slow: int = 60) -> np.ndarray:
    """三均线排列：快>中>慢 为多，反向为空，纠缠时保持上一个方向。"""
    f, m, s = ema(df['close'], fast), ema(df['close'], mid), ema(df['close'], slow)
    raw = np.zeros(len(df))
    raw[(f > m) & (m > s)] = 1.0
    raw[(f < m) & (m < s)] = -1.0
    return _hold(raw)


def _ma_spread_direction(df: pd.DataFrame, fast: int = 12, slow: int = 26,
                         atr_n: int = 14, thr: float = 0.10) -> np.ndarray:
    """均线分离度方向：把 (快均线−慢均线)/ATR 当作标准化的趋势强度。"""
    spread = ((ema(df['close'], fast) - ema(df['close'], slow))
              / atr(df, atr_n).replace(0.0, np.nan))
    return _hold(_sign(spread.values, thr))


def _psar_direction(df: pd.DataFrame, af0: float = 0.02, step: float = 0.02,
                    max_af: float = 0.2) -> np.ndarray:
    """抛物线 SAR（Parabolic Stop And Reverse）方向：SAR 在价格下方为多，上方为空。"""
    h = df['high'].values.astype(float)
    l = df['low'].values.astype(float)
    n = len(h)
    if n < 3:
        return np.zeros(n)
    d, af, ep = 1, af0, h[0]
    sar = l[0]
    out = np.ones(n)
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if d > 0:
            if l[i] < sar:
                d, sar, ep, af = -1, ep, l[i], af0
            elif h[i] > ep:
                ep, af = h[i], min(af + step, max_af)
        else:
            if h[i] > sar:
                d, sar, ep, af = 1, ep, h[i], af0
            elif l[i] < ep:
                ep, af = l[i], min(af + step, max_af)
        out[i] = 1.0 if d > 0 else -1.0
    return out


def _vortex_direction(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """Vortex 指标：VI+ 与 VI− 的相对大小定义趋势方向。"""
    h, l, c = df['high'], df['low'], df['close']
    vm_p = (h - l.shift(1)).abs()
    vm_m = (l - h.shift(1)).abs()
    tr = pd.concat([h - l, (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()], axis=1).max(axis=1)
    vi_p = (vm_p.rolling(period, min_periods=period).sum()
            / tr.rolling(period, min_periods=period).sum())
    vi_m = (vm_m.rolling(period, min_periods=period).sum()
            / tr.rolling(period, min_periods=period).sum())
    return _hold(_sign((vi_p - vi_m).values))


def _aroon_direction(df: pd.DataFrame, period: int = 25, band: float = 50.0) -> np.ndarray:
    """Aroon 方向：距离「最近最高/最低」的远近决定趋势，带中性带。"""
    n = period
    up = df['high'].rolling(n + 1, min_periods=n + 1).apply(
        lambda x: 100.0 * float(np.argmax(x)) / n, raw=True)
    dn = df['low'].rolling(n + 1, min_periods=n + 1).apply(
        lambda x: 100.0 * float(np.argmin(x)) / n, raw=True)
    raw = np.zeros(len(df))
    raw[(up >= band) & (dn <= 100.0 - band)] = 1.0
    raw[(dn >= band) & (up <= 100.0 - band)] = -1.0
    return _hold(raw)


def _trix_direction(df: pd.DataFrame, period: int = 12, k: int = 1) -> np.ndarray:
    """TRIX 三重指数平滑：对趋势噪声极不敏感，方向取 TRIX 差分。"""
    e1 = ema(df['close'], period)
    e2 = ema(e1, period)
    e3 = ema(e2, period)
    trix = 100 * (e3 / e3.shift(1) - 1)
    return _hold(_sign((trix - trix.shift(k)).values))


def _elder_ray_direction(df: pd.DataFrame, period: int = 13) -> np.ndarray:
    """Elder 多空力度（Elder-ray）：high/low 相对 EMA 的偏离，看谁在掌控。"""
    e = ema(df['close'], period)
    bull = df['high'] - e
    bear = df['low'] - e
    raw = np.zeros(len(df))
    raw[(bull > 0) & (bear > 0)] = 1.0
    raw[(bull < 0) & (bear < 0)] = -1.0
    return _hold(raw)


def _tsmom_direction(df: pd.DataFrame, lookback: int = 30) -> np.ndarray:
    """时间序列动量（Time-Series Momentum）：过去 lookback 根的累计收益方向。"""
    r = df['close'] / df['close'].shift(lookback) - 1.0
    return _hold(_sign(r.values))


def _bb_mid_slope_direction(df: pd.DataFrame, period: int = 20, k: int = 3) -> np.ndarray:
    """布林中轨斜率方向：中轨差分正负，反映中枢位置的移动方向。"""
    mid = df['close'].rolling(period, min_periods=period).mean()
    return _hold(_sign((mid - mid.shift(k)).values))


def _dual_scale_direction(df: pd.DataFrame, fast: int = 10, slow: int = 40,
                          k: int = 3) -> np.ndarray:
    """双尺度一致：快尺度与慢尺度方向都看多才给多，否则 0（不强给方向）。"""
    c = df['close']
    f = ema(c, fast) - ema(c, fast).shift(k)
    s = ema(c, slow) - ema(c, slow).shift(k)
    fs, ss = _sign(f.values), _sign(s.values)
    agree = np.where(fs == ss, fs, 0.0)
    return _hold(agree)


def _htf_weekly_direction(df: pd.DataFrame, base_period: int = 10,
                          factor: int = 5, k: int = 3) -> np.ndarray:
    """高周期确认（真·多周期）：把长周期数据再合成 factor 倍的大周期，
    只有大周期方向与当前周期方向一致时才给出方向，否则 0。
    大周期方向在该大周期走完之前不可用 → 只向前填充已完成的组（严格因果）。"""
    base = _sign((ema(df['close'], base_period)
                  - ema(df['close'], base_period).shift(k)).values)
    n = len(df)
    grp = np.arange(n) // factor
    out = np.zeros(n)
    prev_dir = 0.0
    for g in range(grp.max() + 1 if n else 0):
        idx = np.where(grp == g)[0]
        out[idx] = prev_dir
        last = idx[-1]
        if np.isfinite(df['close'].values[last]):
            prev_dir = float(base[last])
    both = np.where(base * out > 0, base, 0.0)
    return _hold(both)


def _vote_direction(df: pd.DataFrame, methods, need: int) -> np.ndarray:
    """多方法投票：统计看多/看空票数，达到 need 票才给方向，否则保持。"""
    votes = np.zeros(len(df))
    for fn in methods:
        v = np.asarray(fn(df), dtype=float)
        votes += np.nan_to_num(v)
    raw = np.zeros(len(df))
    raw[votes >= need] = 1.0
    raw[votes <= -need] = -1.0
    return _hold(raw)


def _vote3_direction(df: pd.DataFrame) -> np.ndarray:
    """三方法共识：RSI中轴 + Aroon + Vortex，三取二。"""
    return _vote_direction(df, [_rsi_mid_direction, _aroon_direction,
                                _vortex_direction], need=2)


def _vote5_direction(df: pd.DataFrame) -> np.ndarray:
    """五方法共识：RSI中轴 + Aroon + Vortex + KAMA + HMA，五取三。"""
    return _vote_direction(df, [_rsi_mid_direction, _aroon_direction,
                                _vortex_direction, _kama_direction,
                                _hma_direction], need=3)


# 方法注册表：键名即 trend_indicator 配置取值（工厂模式的注册中心）
TREND_INDICATORS: Dict[str, callable] = {
    'macd_adx':     _macd_adx_direction,     # 原版基线（ADX 自适应平滑 MACD 状态机）
    'rsi_mid':      _rsi_mid_direction,      # RSI 中轴力度
    'cmo':          _cmo_direction,          # Chande 动量摆动
    'keltner':      _keltner_direction,      # Keltner 通道突破（默认）
    'ichimoku':     _ichimoku_direction,     # 一目均衡表云图
    'kama':         _kama_direction,         # Kaufman 自适应均线
    'hma':          _hma_direction,          # Hull 低滞后均线
    'triple_ma':    _triple_ma_direction,    # 三均线排列
    'ma_spread':    _ma_spread_direction,    # 均线分离度
    'psar':         _psar_direction,         # 抛物线 SAR
    'vortex':       _vortex_direction,       # Vortex 指标
    'aroon':        _aroon_direction,        # Aroon 指标
    'trix':         _trix_direction,         # TRIX 三重指数平滑
    'elder_ray':    _elder_ray_direction,    # Elder 多空力度
    'tsmom':        _tsmom_direction,        # 时间序列动量
    'bb_mid_slope': _bb_mid_slope_direction, # 布林中轨斜率
    'dual_scale':   _dual_scale_direction,   # 双尺度一致
    'htf_weekly':   _htf_weekly_direction,   # 高周期确认
    'vote3':        _vote3_direction,        # 三方法共识投票
    'vote5':        _vote5_direction,        # 五方法共识投票
}

# 默认长周期趋势方法
DEFAULT_TREND_INDICATOR = 'keltner'


def list_indicators() -> List[str]:
    """返回全部可用的长周期趋势方法名（供配置校验与前端下拉）"""
    return list(TREND_INDICATORS.keys())


def compute_direction_series(df: pd.DataFrame,
                             indicator: str = DEFAULT_TREND_INDICATOR) -> np.ndarray:
    """按指定方法计算长周期方向序列（+1/-1/0），返回与 df 等长的 numpy 数组。

    未知方法名直接抛 ValueError（fail-fast，绝不静默回退成别的方法——
    静默回退会让配置写错时实盘悄悄换掉方向判断逻辑）。
    """
    key = str(indicator or '').strip().lower()
    if key not in TREND_INDICATORS:
        raise ValueError(
            f"未知趋势方法 '{indicator}'，可选: {list(TREND_INDICATORS.keys())}")
    dirs = np.asarray(TREND_INDICATORS[key](df), dtype=float)
    dirs = np.nan_to_num(dirs, nan=0.0, posinf=0.0, neginf=0.0)
    if len(dirs) != len(df):
        tmp = np.zeros(len(df), dtype=float)
        m = min(len(dirs), len(df))
        tmp[:m] = dirs[:m]
        dirs = tmp
    return dirs


# ============================================================================
# 三、统一配置接口（不可变 dataclass，多实例天然隔离）
# ============================================================================

@dataclass(frozen=True)
class KeltnerBollConfig:
    """keltner_boll 策略统一配置。

    Attributes:
        trend_indicator:  长周期趋势方向方法名（TREND_INDICATORS 键）
        boll_period:      短周期 BOLL 均线周期
        boll_dev:         短周期 BOLL 标准差倍数
        entry_atr_mult:   A —— 开仓轨在 BOLL 外扩的 ATR 倍数
                          （多：下轨−A×ATR 开仓；空：上轨+A×ATR 开仓）
        exit_atr_mult:    B —— 平仓轨在 BOLL 外扩的 ATR 倍数
                          （多：上轨+B×ATR 平仓；空：下轨−B×ATR 平仓）
        atr_period:       外扩用 ATR 周期（Wilder）
        direction_filter: 方向确认过滤开关 —— 趋势方法方向与持仓方向明确冲突
                          （rise vs short / fall vs long）时暂停开仓；
                          中性(None)不拦。只拦开仓，平仓/撤单照常。
        conf_window:      长周期方向确认窗口（连续 N 根同向且非零才置 conf=1）
        leverage_times:   回测杠杆倍数（仅 simulate 的盈亏%折算使用）
    """
    trend_indicator: str = DEFAULT_TREND_INDICATOR
    boll_period: int = 20
    boll_dev: float = 2.0
    entry_atr_mult: float = 0.0
    exit_atr_mult: float = 0.0
    atr_period: int = 14
    direction_filter: bool = True
    conf_window: int = 3
    leverage_times: float = 10.0

    def __post_init__(self):
        key = str(self.trend_indicator or '').strip().lower()
        if key not in TREND_INDICATORS:
            raise ValueError(
                f"未知趋势方法 '{self.trend_indicator}'，可选: {list_indicators()}")
        # frozen dataclass 内规范化字段须走 object.__setattr__
        object.__setattr__(self, 'trend_indicator', key)
        if int(self.boll_period) <= 1:
            raise ValueError('boll_period 必须为大于 1 的整数')
        if float(self.boll_dev) <= 0:
            raise ValueError('boll_dev 必须为正数')
        if float(self.entry_atr_mult) < 0 or float(self.exit_atr_mult) < 0:
            raise ValueError('entry_atr_mult / exit_atr_mult 不能为负数')
        if int(self.atr_period) <= 0:
            raise ValueError('atr_period 必须为正整数')

    @classmethod
    def from_range_cfg(cls, rcfg: Optional[Dict]) -> 'KeltnerBollConfig':
        """从调度器 range_position 配置 dict 构建（缺省字段用类默认值）。

        对应配置键（均可缺省）：
            strategy / trend_indicator / entry_atr_mult / exit_atr_mult /
            atr_period / direction_filter / boll_period / boll_dev
        """
        rcfg = rcfg or {}
        return cls(
            trend_indicator=str(rcfg.get('trend_indicator')
                                or DEFAULT_TREND_INDICATOR),
            boll_period=int(rcfg.get('boll_period', 20) or 20),
            boll_dev=float(rcfg.get('boll_dev', 2.0) or 2.0),
            entry_atr_mult=float(rcfg.get('entry_atr_mult', 0.0) or 0.0),
            exit_atr_mult=float(rcfg.get('exit_atr_mult', 0.0) or 0.0),
            atr_period=int(rcfg.get('atr_period', 14) or 14),
            direction_filter=bool(rcfg.get('direction_filter', True)),
        )


def resolve_range_strategy(rcfg: Optional[Dict]) -> str:
    """归一化 B 仓位策略名：'keltner_boll'（默认）/ 'boll'（存量纯 BOLL）。

    未知取值回退 'boll' 并告警——策略名写错时保持存量行为最安全，
    绝不因配置笔误悄悄切换到别的交易逻辑。
    """
    s = str((rcfg or {}).get('strategy', 'keltner_boll') or 'keltner_boll').strip().lower()
    if s in ('keltner_boll', 'boll'):
        return s
    logger.warning(f"[keltner_boll] 未知 range_position.strategy '{s}'，回退存量 'boll'")
    return 'boll'


# ============================================================================
# 四、策略类（工厂 + 纯计算，无全局状态）
# ============================================================================

class KeltnerBollStrategy:
    """Keltner×BOLL 区间策略：长周期趋势方向方法库 + BOLL 边界 ATR 外扩。

    实例只持有不可变配置，所有计算均为纯函数式（入参 df → 出参），
    多线程/多实例并发安全，A/B 仓位切换互不串扰。

    典型用法::

        strat = KeltnerBollStrategy(KeltnerBollConfig(trend_indicator='vote3'))
        state = strat.long_state(df_long)            # 长周期方向 + 确认度
        bands = strat.expand_bands(2080.0, 2140.0, 3.5)  # ATR 外扩边界
        if strat.entry_blocked(state['direction'], 'long'):
            ...  # 方向冲突，本轮暂停开仓
    """

    def __init__(self, config: Optional[KeltnerBollConfig] = None):
        self.config = config or KeltnerBollConfig()

    # ---- 长周期方向 ----

    def direction_series(self, df_long: pd.DataFrame) -> np.ndarray:
        """长周期方向序列（+1/-1/0），方法由 config.trend_indicator 决定"""
        return compute_direction_series(df_long, self.config.trend_indicator)

    def long_state(self, df_long: pd.DataFrame) -> Dict:
        """长周期最新方向与确认度。

        Returns:
            {'direction': 'rise'/'fall'/None, 'value': float(+1/-1/0),
             'conf': 0.0/1.0（连续 conf_window 根同向非零）,
             'indicator': 方法名, 'bars': 参与计算的长周期K线数}
        """
        dirs = self.direction_series(df_long)
        n = len(dirs)
        if n == 0:
            return {'direction': None, 'value': 0.0, 'conf': 0.0,
                    'indicator': self.config.trend_indicator, 'bars': 0}
        conf = 0.0
        w = int(self.config.conf_window)
        if n >= w:
            window = dirs[n - w:n]
            if np.all(window == window[0]) and window[0] != 0.0:
                conf = 1.0
        return {
            'direction': value_to_dir(dirs[-1]),
            'value': float(dirs[-1]),
            'conf': conf,
            'indicator': self.config.trend_indicator,
            'bars': n,
        }

    # ---- BOLL 边界 ATR 外扩 ----

    def expand_bands(self, boll_upper: float, boll_lower: float,
                     atr_value: float) -> Dict[str, float]:
        """按 copy 7 口径外扩 BOLL 边界：

        - 多头开仓价 = 下轨 − entry_atr_mult×ATR；多头平仓价 = 上轨 + exit_atr_mult×ATR
        - 空头开仓价 = 上轨 + entry_atr_mult×ATR；空头平仓价 = 下轨 − exit_atr_mult×ATR

        即 ``range_upper = boll_upper + exit_mult×ATR``、
        ``range_lower = boll_lower − entry_mult×ATR``，多空对称共用同一对边界。
        ATR 无效(NaN/≤0)时按 0 外扩（退化为纯 BOLL 边界，fail-safe）。
        """
        a = float(atr_value) if atr_value is not None and np.isfinite(float(atr_value)) else 0.0
        if a < 0:
            a = 0.0
        return {
            'range_upper': float(boll_upper) + self.config.exit_atr_mult * a,
            'range_lower': float(boll_lower) - self.config.entry_atr_mult * a,
            'atr': a,
        }

    # ---- 方向确认过滤 ----

    def entry_blocked(self, indicator_dir: Optional[str], desired: str) -> bool:
        """方向确认过滤：趋势方法方向与目标持仓方向**明确冲突**时拦开仓。

        - indicator_dir 为 None（中性/数据不足）→ 不拦（fail-open，
          过滤器的职责是"确认冲突才拦"，不是"没确认就拦"）；
        - 只应由调用方用于开仓闸门，平仓/撤单路径不得调用本方法。
        """
        if not self.config.direction_filter:
            return False
        if indicator_dir not in ('rise', 'fall'):
            return False
        want = 'rise' if desired == 'long' else 'fall'
        return indicator_dir != want


# ============================================================================
# 五、实时适配器（供 trend_range_trader / 详情页调用，带结果缓存）
# ============================================================================

class KeltnerBollAdapter:
    """实时长周期方向适配器：取数 → 方向计算 → 结果缓存。

    - 取数复用 ``pro3_singletimeframe._fetch_kline_data``（自带 TTL 缓存与
      弱网降级，联网失败抛异常由调用方兜底）；
    - 结果按 ``(inst_id, long_bar, indicator, conf_window, 最新K线时间戳)`` 缓存：
      同一根长周期 K 线内多轮调度（80s/轮）**零重复方向计算**；取数仍每轮调用
      fetcher（需读最新 K 线时间戳判定是否换 bar），网络请求由 fetcher 自带的
      分级 TTL 缓存去重，本类不额外承担网络层去重职责。新 K 线到达自动重算；
      缓存只存不可变结果 tuple，无脏状态；
    - 本类线程安全（内部锁保护缓存 dict），不依赖也不触碰 pro3 引擎的
      模块级全局参数，故无需占用 pro3_locked 策略计算锁。
    """

    _CACHE_MAX = 512  # 简单容量上限：超限整体清空（币种数×周期数远小于此值）

    def __init__(self, fetcher=None):
        """fetcher: 可注入的取数函数 ``fn(inst_id, bar) -> DataFrame``，
        缺省用 pro3_singletimeframe._fetch_kline_data（测试可注入假数据）。"""
        self._fetcher = fetcher
        self._cache: Dict[tuple, tuple] = {}
        self._lock = threading.Lock()
        self._cache_hits = 0     # 命中缓存（跳过方向计算）的次数，供可观测/测试
        self._cache_misses = 0   # 实际执行方向计算并回填缓存的次数

    def _get_fetcher(self):
        if self._fetcher is None:
            from pro3_singletimeframe import _fetch_kline_data
            self._fetcher = _fetch_kline_data
        return self._fetcher

    def long_state(self, inst_id: str, long_bar: str,
                   indicator: str = DEFAULT_TREND_INDICATOR,
                   conf_window: int = 3) -> Dict:
        """计算某合约长周期最新方向（带缓存）。

        Returns:
            与 :meth:`KeltnerBollStrategy.long_state` 相同结构，另含
            'last_ts'（参与计算的最新长周期 K 线时间戳，缓存键成分）。

        Raises:
            取数失败/数据为空时抛异常，由调用方决定兜底（调度器：本轮
            退化为纯 BOLL 边界且不启用方向过滤，绝不因增强模块故障停摆）。
        """
        key_ind = str(indicator or DEFAULT_TREND_INDICATOR).strip().lower()
        if key_ind not in TREND_INDICATORS:
            raise ValueError(f"未知趋势方法 '{indicator}'，可选: {list_indicators()}")
        df = self._get_fetcher()(inst_id, long_bar)
        if df is None or len(df) == 0:
            raise RuntimeError(f'{inst_id} {long_bar} 长周期K线为空')
        last_ts = df.index[-1]
        ck = (inst_id, long_bar, key_ind, conf_window, last_ts)
        with self._lock:
            hit = self._cache.get(ck)
            if hit is not None:
                self._cache_hits += 1
        if hit is not None:
            return dict(hit[0])
        strat = KeltnerBollStrategy(KeltnerBollConfig(
            trend_indicator=key_ind, conf_window=conf_window))
        state = strat.long_state(df)
        state['last_ts'] = last_ts
        with self._lock:
            if len(self._cache) >= self._CACHE_MAX:
                self._cache.clear()
            self._cache[ck] = (state, _time.time())
            self._cache_misses += 1
        return dict(state)

    def stats(self) -> Dict[str, int]:
        """缓存命中统计（供心跳日志/测试观测）：hits=跳过计算次数，
        misses=实际计算次数，size=当前缓存条目数。"""
        with self._lock:
            return {'hits': self._cache_hits, 'misses': self._cache_misses,
                    'size': len(self._cache)}

    def clear_cache(self):
        with self._lock:
            self._cache.clear()
            self._cache_hits = 0
            self._cache_misses = 0


# 进程级共享适配器（缓存跨轮复用；无状态计算，多调用方共享安全）
_SHARED_ADAPTER: Optional[KeltnerBollAdapter] = None
_SHARED_ADAPTER_LOCK = threading.Lock()


def get_shared_adapter() -> KeltnerBollAdapter:
    """返回进程级共享的 KeltnerBollAdapter（懒初始化，线程安全）"""
    global _SHARED_ADAPTER
    with _SHARED_ADAPTER_LOCK:
        if _SHARED_ADAPTER is None:
            _SHARED_ADAPTER = KeltnerBollAdapter()
        return _SHARED_ADAPTER


# ============================================================================
# 六、调度器接线助手（纯函数：供 trend_range_trader 调用与单元测试）
# ============================================================================

def apply_keltner_boll_boundaries(range_cfg: Optional[Dict], analysis: Dict,
                                  adapter: KeltnerBollAdapter, inst_id: str,
                                  long_period: str, long_dir: str) -> Dict:
    """把 keltner_boll 引擎的增强边界写入本轮 analysis（B 仓位专用）。

    仅当 ``range_cfg.strategy == 'keltner_boll'`` 时生效；成功时：
    - ``analysis['boll_upper'/'boll_lower']`` 替换为 ATR 外扩后的区间边界
      （区间仓挂单/追价/观察提醒/独立风险退出全部消费同一对边界，口径一致；
      趋势仓 A 的止盈引擎在**本函数调用之前**已消费完原始 BOLL 值，不受影响）；
    - 原始值备份到 ``analysis['boll_upper_raw'/'boll_lower_raw']``；
    - 方向过滤元数据写入 ``analysis['kb_direction_filter']``。

    任何失败（K线取不到/指标异常）→ 返回 applied=False，analysis 保持原样，
    本轮退化为存量纯 BOLL 行为（fail-safe：增强模块故障绝不停摆主调度）。

    Args:
        range_cfg:   币种级 range_position 配置
        analysis:    DualPeriodStrategyAdapter.analyze() 的结果 dict（原地更新）
        adapter:     KeltnerBollAdapter 实例（通常取 get_shared_adapter()）
        inst_id:     合约ID
        long_period: 长周期（如 '4H'）
        long_dir:    本轮决议后的长周期方向 'long'/'short'（已含人工锁定语义）

    Returns:
        {'applied': bool, 'blocked': bool（方向过滤是否拦本轮开仓）,
         'text': 心跳日志文案}
    """
    out = {'applied': False, 'blocked': False, 'text': ''}
    if resolve_range_strategy(range_cfg) != 'keltner_boll':
        return out
    upper = float((analysis or {}).get('boll_upper', 0) or 0)
    lower = float((analysis or {}).get('boll_lower', 0) or 0)
    atr_v = float((analysis or {}).get('atr_value', 0) or 0)
    if upper <= 0 or lower <= 0:
        # BOLL 边界本来就不可用：主循环会走"区间跳过"分支，这里不重复告警
        return out
    cfg = KeltnerBollConfig.from_range_cfg(range_cfg)
    strat = KeltnerBollStrategy(cfg)
    try:
        state = adapter.long_state(inst_id, long_period, cfg.trend_indicator,
                                   conf_window=cfg.conf_window)
    except Exception as e:
        out['text'] = f'keltner_boll方向计算失败({type(e).__name__})，本轮退化纯BOLL'
        logger.warning(f"[keltner_boll] {inst_id} {long_period} "
                       f"{cfg.trend_indicator} 方向计算失败: {e}")
        return out
    bands = strat.expand_bands(upper, lower, atr_v)
    analysis['boll_upper_raw'], analysis['boll_lower_raw'] = upper, lower
    analysis['boll_upper'], analysis['boll_lower'] = \
        bands['range_upper'], bands['range_lower']
    blocked = strat.entry_blocked(state['direction'], long_dir)
    analysis['kb_direction_filter'] = {
        'indicator': cfg.trend_indicator,
        'indicator_dir': state['direction'],
        'conf': state['conf'],
        'blocked': blocked,
        'entry_atr_mult': cfg.entry_atr_mult,
        'exit_atr_mult': cfg.exit_atr_mult,
    }
    out['applied'] = True
    out['blocked'] = blocked
    dir_cn = {'rise': '看多', 'fall': '看空'}.get(state['direction'], '中性')
    out['text'] = (f"KB({cfg.trend_indicator})={dir_cn}"
                   f"{'·拦截开仓' if blocked else ''}"
                   f" 轨[{bands['range_lower']:.6g},{bands['range_upper']:.6g}]"
                   f"(ATR外扩 开{cfg.entry_atr_mult:g}/平{cfg.exit_atr_mult:g})")
    return out


def current_range_signal(inst_id: str, short_bar: str, long_bar: str,
                         config: Optional[KeltnerBollConfig] = None,
                         fetcher=None) -> Dict:
    """B 仓位独立信号引擎：产出与 :func:`simulate` **最后一根短 K 完全同源**
    的方向与开/平仓目标价，供实盘调度器逐轮消费。

    设计意图：让定时任务的区间仓（direction_source='keltner' 独立模式）与详情页
    回测跑同一套信号口径——方向、BOLL 轨、ATR 外扩、多空触发价全部由本模块
    计算，不再依赖 Pro3 长周期方向，也不再走 expand_bands 的对称边界（后者在
    空头侧会把 entry/exit 倍数用反）。

    与 simulate 的对齐口径逐字一致：
    - 方向：``compute_direction_series(df_long, trend_indicator)`` 经
      ``merge_asof(direction='backward')`` 对齐到短周期，取**最后一根短 K** 的
      对齐方向；``value_to_dir`` → 'rise'/'fall'/None → 'long'/'short'/None。
    - 价位：``boll_bands(df_short, boll_period, boll_dev)`` 与
      ``atr(df_short, atr_period)`` 取 ``.iloc[-1]``，经 :func:`_bar_levels`
      按方向得 entry_px/exit_px。

    Args:
        inst_id / short_bar / long_bar: 合约与短/长周期
        config:   :class:`KeltnerBollConfig`（缺省用默认配置）
        fetcher:  可注入取数函数 ``fn(inst_id, bar) -> DataFrame``（测试用），
                  缺省用 pro3_singletimeframe._fetch_kline_data

    Returns:
        dict：{'direction': 'long'/'short'/None, 'entry_px': float|None,
        'exit_px': float|None, 'band_upper'/'band_lower': float|None（触发价的
        上/下包络，供风险退出的冻结轨与破位判定使用）, 'top'/'bot'/'atr': float,
        'indicator': str, 'conf': 0.0/1.0, 'last_short_ts'/'last_long_ts'}。
        方向为 None 或 BOLL 轨不可用时 entry_px/exit_px/band_* 均为 None。

    Raises:
        取数失败 / 数据为空时抛异常，由调用方（调度器）兜底 fail-safe。
    """
    cfg = config or KeltnerBollConfig()
    if fetcher is None:
        from pro3_singletimeframe import _fetch_kline_data as fetcher
    df_short = fetcher(inst_id, short_bar)
    df_long = fetcher(inst_id, long_bar)
    if df_short is None or len(df_short) == 0:
        raise RuntimeError(f'{inst_id} {short_bar} 短周期K线为空')
    if df_long is None or len(df_long) == 0:
        raise RuntimeError(f'{inst_id} {long_bar} 长周期K线为空')
    df_short = df_short.copy()
    df_long = df_long.copy()

    # 长周期方向序列 → 向后对齐到短周期时间轴（与 simulate 同口径）
    dirs = compute_direction_series(df_long, cfg.trend_indicator)
    for d in (df_short, df_long):
        if hasattr(d.index, 'tz') and d.index.tz is not None:
            d.index = d.index.tz_localize(None)
    long_ts = pd.DataFrame({'ts': pd.to_datetime(df_long.index), 'dir': dirs})
    short_ts = pd.DataFrame({'ts': pd.to_datetime(df_short.index)})
    aligned = pd.merge_asof(short_ts.sort_values('ts'), long_ts.sort_values('ts'),
                            on='ts', direction='backward')
    cur_val = float(aligned['dir'].values[-1]) if len(aligned) else 0.0
    long_dir = value_to_dir(cur_val)                      # 'rise'/'fall'/None
    direction = {'rise': 'long', 'fall': 'short'}.get(long_dir)  # 'long'/'short'/None

    # 方向确认度（与 long_state 同口径：连续 conf_window 根同向非零）
    conf = 0.0
    w = int(cfg.conf_window)
    if len(dirs) >= w:
        window = dirs[len(dirs) - w:]
        if np.all(window == window[0]) and window[0] != 0.0:
            conf = 1.0

    bands = boll_bands(df_short, cfg.boll_period, cfg.boll_dev)
    atr_series = atr(df_short, cfg.atr_period)
    top = float(bands['top'].values[-1])
    bot = float(bands['bot'].values[-1])
    a = float(atr_series.values[-1]) if np.isfinite(atr_series.values[-1]) else 0.0

    out: Dict = {
        'direction': direction,
        'entry_px': None, 'exit_px': None,
        'band_upper': None, 'band_lower': None,
        'top': top if np.isfinite(top) else None,
        'bot': bot if np.isfinite(bot) else None,
        'atr': a,
        'indicator': cfg.trend_indicator, 'conf': conf,
        'last_short_ts': df_short.index[-1], 'last_long_ts': df_long.index[-1],
    }
    if direction is None or not (np.isfinite(top) and np.isfinite(bot)):
        return out
    lv = _bar_levels(top, bot, a, cfg.entry_atr_mult, cfg.exit_atr_mult)
    if direction == 'long':
        entry, exit_ = lv['long_entry'], lv['long_exit']
    else:
        entry, exit_ = lv['short_entry'], lv['short_exit']
    out['entry_px'] = float(entry)
    out['exit_px'] = float(exit_)
    out['band_upper'] = float(max(entry, exit_))
    out['band_lower'] = float(min(entry, exit_))
    return out


# ============================================================================
# 七、纯 pandas 回测/模拟盘引擎（不依赖 backtrader，事件语义与 copy 7 一致）
# ============================================================================

def boll_bands(df_short: pd.DataFrame, period: int = 20,
               dev: float = 2.0) -> pd.DataFrame:
    """短周期 BOLL 三轨（mid/top/bot），总体标准差口径与 backtrader 一致(ddof=0)"""
    c = df_short['close'].astype(float)
    mid = c.rolling(period, min_periods=period).mean()
    std = c.rolling(period, min_periods=period).std(ddof=0)
    return pd.DataFrame({'mid': mid, 'top': mid + dev * std, 'bot': mid - dev * std},
                        index=df_short.index)


def _bar_levels(top: float, bot: float, a, entry_mult: float,
                exit_mult: float) -> Dict[str, float]:
    """单根短 K 的四个开/平仓触发价（多空各自的入场轨与平仓轨）。

    多空**非对称**（与 :func:`simulate` 逐字一致，是回测与实盘独立模式的唯一
    价位口径来源）：
    - 多头：入场 = 下轨 − entry×ATR；平仓 = 上轨 + exit×ATR
    - 空头：入场 = 上轨 + entry×ATR；平仓 = 下轨 − exit×ATR

    注意：这与 :meth:`KeltnerBollStrategy.expand_bands` 的"一对对称边界"
    口径**不同**——对称边界在空头侧会把 entry/exit 倍数用反（当 entry≠exit
    时不等价）。回测与独立实盘模式一律走本函数，杜绝该偏差。
    ATR 无效(NaN/<0)时按 0 外扩（fail-safe，退化为纯 BOLL 触发价）。
    """
    av = float(a) if a is not None and np.isfinite(float(a)) else 0.0
    if av < 0:
        av = 0.0
    return {
        'long_entry': float(bot) - entry_mult * av,
        'long_exit': float(top) + exit_mult * av,
        'short_entry': float(top) + entry_mult * av,
        'short_exit': float(bot) - exit_mult * av,
    }


def simulate(df_short: pd.DataFrame, df_long: pd.DataFrame,
             config: Optional[KeltnerBollConfig] = None,
             order_size: float = 1.0) -> Dict:
    """Keltner×BOLL 区间策略纯 pandas 事件回测（触边即入/触边即出）。

    语义与 copy 7 的 backtrader DualTimeframeBollLimitStrategy 完全一致：
    - 长周期方向经 merge_asof 向后对齐到短周期（每根短K线取最近已收盘长K线方向）；
    - 多头：长周期 rise 且 bar 范围触及 (下轨−A×ATR) → 该价开多；
      触及 (上轨+B×ATR) → 该价平多；空头镜像；
    - 持仓期间长周期方向与入场时相反 → position_flip；平仓时 flip 且亏损
      → 该笔标记 excluded（"长周期反转导致的短周期亏损不计入统计"）；
    - 盈亏% = 价差比 × 100 × leverage_times。

    Args:
        df_short: 短周期 OHLC DataFrame（须含 open/high/low/close）
        df_long:  长周期 OHLC DataFrame（同上）
        config:   策略配置（trend_indicator / ATR 外扩倍数等）
        order_size: 名义下单量（当前盈亏按比例折算，不影响%口径）

    Returns:
        {'trade_records': [...每笔 {...}], 'excluded_count': int,
         'win_rate': float, 'total_profit_pct': float, 'total_loss_pct': float,
         'final_equity': float（定投口径，基数100）, 'max_drawdown_pct': float}
    """
    cfg = config or KeltnerBollConfig()
    df_short = df_short.copy()
    df_long = df_long.copy()

    # 长周期方向序列 → 向后对齐到短周期时间轴
    dirs = compute_direction_series(df_long, cfg.trend_indicator)
    for d in (df_short, df_long):
        if hasattr(d.index, 'tz') and d.index.tz is not None:
            d.index = d.index.tz_localize(None)
    long_ts = pd.DataFrame({'ts': pd.to_datetime(df_long.index), 'dir': dirs})
    short_ts = pd.DataFrame({'ts': pd.to_datetime(df_short.index)})
    aligned = pd.merge_asof(short_ts.sort_values('ts'), long_ts.sort_values('ts'),
                            on='ts', direction='backward')
    df_short['LONG_DIR'] = aligned['dir'].values

    bands = boll_bands(df_short, cfg.boll_period, cfg.boll_dev)
    atr_series = atr(df_short, cfg.atr_period)

    lev = float(cfg.leverage_times)
    entry_mult, exit_mult = float(cfg.entry_atr_mult), float(cfg.exit_atr_mult)
    pos_size = 0.0
    entry_price = None
    entry_time = None
    entry_dir = None
    entry_band = None      # 开仓时刻的原始 BOLL 轨道（多头=下轨/空头=上轨，外扩前）
    entry_atr = None       # 开仓时刻的 ATR（Average True Range，平均真实波幅）值
    position_flip = False
    max_px = min_px = None
    trade_records: List[Dict] = []

    highs = df_short['high'].values.astype(float)
    lows = df_short['low'].values.astype(float)
    long_dirs = df_short['LONG_DIR'].values
    tops = bands['top'].values
    bots = bands['bot'].values
    atrs = atr_series.values
    times = df_short.index

    def _close_record(now_time, exit_price, direction):
        if direction == 'rise':
            profit = (exit_price / entry_price - 1) * 100 * lev
            mw = (max_px / entry_price - 1) * 100 * lev
            ml = (min_px / entry_price - 1) * 100 * lev
        else:
            profit = (1 - exit_price / entry_price) * 100 * lev
            mw = (1 - min_px / entry_price) * 100 * lev
            ml = (1 - max_px / entry_price) * 100 * lev
        excluded = bool(position_flip and profit < 0)
        trade_records.append({
            'dir': direction, 'open_price': float(entry_price),
            'close_price': float(exit_price), 'entry_time': entry_time,
            'exit_time': now_time, 'profit': float(profit),
            'max_win': float(mw), 'max_loss': float(ml), 'excluded': excluded,
            # 挂单价格推导链：原始 BOLL 轨道 → ATR 外扩 → 实际开仓挂单价
            'boll_band': float(entry_band) if entry_band is not None else None,
            'atr_value': float(entry_atr) if (entry_atr is not None
                                              and np.isfinite(float(entry_atr))) else 0.0,
            'order_price': float(entry_price),
        })

    for i in range(len(df_short)):
        top, bot = tops[i], bots[i]
        if not (np.isfinite(top) and np.isfinite(bot)):
            continue
        long_dir = value_to_dir(long_dirs[i])
        if long_dir is None:
            continue
        high, low, now_time = highs[i], lows[i], times[i]
        lv = _bar_levels(tops[i], bots[i], atrs[i], entry_mult, exit_mult)
        long_entry = lv['long_entry']
        short_entry = lv['short_entry']
        long_exit = lv['long_exit']
        short_exit = lv['short_exit']

        if pos_size != 0 and entry_price is not None:
            max_px = high if max_px is None or high > max_px else max_px
            min_px = low if min_px is None or low < min_px else min_px
            if entry_dir and long_dir != entry_dir:
                position_flip = True

        if pos_size == 0:
            if long_dir == 'rise' and low <= long_entry <= high:
                pos_size, entry_price, entry_time = order_size, long_entry, now_time
                entry_dir, position_flip = 'rise', False
                entry_band, entry_atr = bot, atrs[i]
                max_px = min_px = long_entry
            elif long_dir == 'fall' and low <= short_entry <= high:
                pos_size, entry_price, entry_time = -order_size, short_entry, now_time
                entry_dir, position_flip = 'fall', False
                entry_band, entry_atr = top, atrs[i]
                max_px = min_px = short_entry
        elif pos_size > 0 and low <= long_exit <= high:
            _close_record(now_time, long_exit, 'rise')
            pos_size, entry_price, entry_time, entry_dir = 0.0, None, None, None
            entry_band, entry_atr = None, None
            position_flip, max_px, min_px = False, None, None
        elif pos_size < 0 and low <= short_exit <= high:
            _close_record(now_time, short_exit, 'fall')
            pos_size, entry_price, entry_time, entry_dir = 0.0, None, None, None
            entry_band, entry_atr = None, None
            position_flip, max_px, min_px = False, None, None

    # ---- 统计（定投口径资金曲线，基数 100，与 copy 7 fix_money 一致）----
    base_money = 100.0
    equity = base_money
    peak = base_money
    max_dd = 0.0
    included = [r for r in trade_records if not r['excluded']]
    for r in included:
        equity += base_money * r['profit'] * 0.01
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
    profits = [r['profit'] for r in included]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    return {
        'trade_records': trade_records,
        'total_trades': len(included),
        'excluded_count': len(trade_records) - len(included),
        'win_rate': len(wins) / len(included) if included else 0.0,
        'total_profit_pct': sum(wins),
        'total_loss_pct': sum(losses),
        'avg_profit_pct': sum(wins) / len(wins) if wins else 0.0,
        'avg_loss_pct': sum(losses) / len(losses) if losses else 0.0,
        'final_equity': equity,
        'max_drawdown_pct': max_dd,
    }


# ============================================================================
# 七b、详情页回测适配（产出与 BOLL 限价详情同构的响应，供 detail.html 消费）
# ============================================================================

def get_keltner_boll_full_data(instId: str, short_bar: str = '1H',
                               long_bar: str = '4H',
                               trend_indicator: str = DEFAULT_TREND_INDICATOR,
                               entry_atr_mult: float = 0.0,
                               exit_atr_mult: float = 0.0, atr_period: int = 14,
                               boll_period: int = 20, boll_dev: float = 2.0,
                               fetcher=None) -> Dict:
    """运行 keltner_boll 回测并返回详情页所需的完整字典（结构与 BOLL 限价一致）。

    与 :func:`boll_limit_dualtimeframe.get_boll_strategy_full_data` 对齐：同样返回
    ``market / current_position / trade_records / stats / 资金曲线`` 结构，故
    detail.html 的渲染逻辑（含收益曲线、剔除标记、ADX 统计）无需分支即可复用。

    数据流：``_fetch_kline_data`` 取真实短/长周期 K 线 → :func:`simulate` 事件回测
    → 复用 boll_limit_dualtimeframe.build_range_detail_response 构建统一响应。
    本函数**不触碰 pro3 引擎全局参数**，与 boll 详情一样无需占用 pro3_locked。

    Args:
        instId:     合约ID，如 'BTC-USDT-SWAP'
        short_bar:  短周期（BOLL 挂单周期）
        long_bar:   长周期（趋势方向判断周期）
        trend_indicator / entry_atr_mult / exit_atr_mult / atr_period /
        boll_period / boll_dev: 见 :class:`KeltnerBollConfig`
        fetcher:    可注入取数函数 ``fn(inst_id, bar) -> DataFrame``（测试用），
                    缺省用 pro3_singletimeframe._fetch_kline_data

    Returns:
        dict：与 boll 详情同构；额外在 ``dual_period`` 内附 keltner_boll 参数
        （trend_indicator/entry_atr_mult/exit_atr_mult），供前端回显。

    Raises:
        取数失败 / 数据为空时抛异常，由 strategy_util 分发层统一转成详情页错误提示。
    """
    if fetcher is None:
        from pro3_singletimeframe import _fetch_kline_data as fetcher
    df_short = fetcher(instId, short_bar)
    df_long = fetcher(instId, long_bar)
    if df_short is None or len(df_short) == 0:
        raise RuntimeError(f'{instId} {short_bar} 短周期K线为空')
    if df_long is None or len(df_long) == 0:
        raise RuntimeError(f'{instId} {long_bar} 长周期K线为空')

    cfg = KeltnerBollConfig(
        trend_indicator=trend_indicator, boll_period=boll_period,
        boll_dev=boll_dev, entry_atr_mult=entry_atr_mult,
        exit_atr_mult=exit_atr_mult, atr_period=atr_period)

    # simulate 的 trade_records 与 boll 记录同构；额外给 df_short 补 ADX 列，
    # 让共享构建器能算入场 ADX 统计（口径与 boll 详情一致）。
    df_short_adx = calculate_adx(df_short, atr_period)
    res = simulate(df_short_adx, df_long, cfg)

    strat = KeltnerBollStrategy(cfg)
    long_dir = strat.long_state(df_long)['direction']  # 'rise'/'fall'/None
    price = float(df_short['close'].iloc[-1])
    atr_series = atr(df_short, atr_period)
    atr_value = float(atr_series.iloc[-1]) if np.isfinite(atr_series.iloc[-1]) else 0.0
    adx_value = float(df_short_adx['ADX'].iloc[-1]) \
        if np.isfinite(df_short_adx['ADX'].iloc[-1]) else 0.0

    from boll_limit_dualtimeframe import build_range_detail_response
    out = build_range_detail_response(
        res['trade_records'], df_short_adx, price, adx_value, atr_value,
        long_dir, short_bar, long_bar,
        strategy_name=f'Keltner×BOLL({trend_indicator} {short_bar}/{long_bar})',
        entry_mult=cfg.entry_atr_mult, boll_period=cfg.boll_period,
        boll_dev=cfg.boll_dev)
    # 附加 keltner_boll 专有参数，便于前端回显当前回测配置
    out.setdefault('dual_period', {})
    out['dual_period'].update({
        'trend_indicator': cfg.trend_indicator,
        'entry_atr_mult': cfg.entry_atr_mult,
        'exit_atr_mult': cfg.exit_atr_mult,
        'atr_period': cfg.atr_period,
    })
    return out


# ============================================================================
# 八、诊断工具（趋势段统计与横向对比，自 copy 7 移植）
# ============================================================================

def bar_minutes(bar: str) -> int:
    """把 '3m'/'1H'/'4H'/'1D' 等周期字符串换算成分钟数"""
    s = str(bar).strip().lower()
    n = ''.join(ch for ch in s if ch.isdigit())
    n = int(n) if n else 1
    if s.endswith('m'):
        return n
    if s.endswith('h'):
        return n * 60
    if s.endswith('d'):
        return n * 60 * 24
    if s.endswith('w'):
        return n * 60 * 24 * 7
    return n


def analyze_trend_segments(df_long: pd.DataFrame, indicator: str,
                           long_bar: str) -> Dict:
    """统计某方法在长周期数据上的趋势段特征（用于挑选方向判断方法）。

    趋势段 = 一段连续、同向且非零的长周期 bar。返回：
      num_segments/reversals 段数与反转次数；avg|median|max_dur_hours 单段持续
      时间；coverage 有方向 bar 占比；reversals_per_day 日均反转次数
      （越高=翻转越频繁=越不稳定）。
    """
    dirs = compute_direction_series(df_long, indicator)
    bm = bar_minutes(long_bar)
    n = len(dirs)
    segments = []
    i = 0
    while i < n:
        if dirs[i] != 0.0:
            j = i
            while j + 1 < n and dirs[j + 1] == dirs[i]:
                j += 1
            segments.append((i, j, dirs[i]))
            i = j + 1
        else:
            i += 1
    num_segments = len(segments)
    dur_bars = [(e - s + 1) for (s, e, d) in segments]
    dur_hours = [b * bm / 60.0 for b in dur_bars]
    nonzero = int(np.sum(dirs != 0.0))
    total_days = n * bm / 60.0 / 24.0
    return {
        'indicator': indicator,
        'num_segments': num_segments,
        'reversals': max(num_segments - 1, 0),
        'avg_dur_bars': float(np.mean(dur_bars)) if dur_bars else 0.0,
        'avg_dur_hours': float(np.mean(dur_hours)) if dur_hours else 0.0,
        'median_dur_hours': float(np.median(dur_hours)) if dur_hours else 0.0,
        'max_dur_hours': float(np.max(dur_hours)) if dur_hours else 0.0,
        'coverage': nonzero / n if n > 0 else 0.0,
        'reversals_per_day': (max(num_segments - 1, 0) / total_days) if total_days > 0 else 0.0,
        'total_days': total_days,
    }


def compare_trend_indicators(df_long: pd.DataFrame, long_bar: str,
                             indicators: Optional[List[str]] = None,
                             verbose: bool = True) -> List[Dict]:
    """对全部方法跑一遍趋势段统计，按「日均反转次数」升序排序输出。
    反转越少 = 方向越稳 = 越适合当长周期过滤；反转过多等于没过滤。"""
    keys = indicators or list_indicators()
    rows = []
    for k in keys:
        try:
            rows.append(analyze_trend_segments(df_long, k, long_bar))
        except Exception as e:
            rows.append({'indicator': k, 'error': f'{type(e).__name__}: {e}'})
    ok = [r for r in rows if 'error' not in r]
    ok.sort(key=lambda r: r['reversals_per_day'])
    if verbose:
        print('\n' + '=' * 108)
        print('  长周期趋势方向方法对比（keltner_boll）—— 按日均反转次数升序，反转越少方向越稳')
        print('=' * 108)
        print(f"{'方法':<14}{'方向段':>8}{'反转':>7}{'平均时长(h)':>13}{'中位(h)':>10}"
              f"{'最长(h)':>10}{'有向占比':>10}{'日均反转':>10}")
        print('-' * 108)
        for r in ok:
            print(f"{r['indicator']:<14}{r['num_segments']:>8}{r['reversals']:>7}"
                  f"{r['avg_dur_hours']:>13.1f}{r['median_dur_hours']:>10.1f}"
                  f"{r['max_dur_hours']:>10.1f}{r['coverage']*100:>9.1f}%"
                  f"{r['reversals_per_day']:>10.3f}")
        print('-' * 108)
        for r in rows:
            if 'error' in r:
                print(f"  [X] {r['indicator']}: {r['error']}")
    return ok
