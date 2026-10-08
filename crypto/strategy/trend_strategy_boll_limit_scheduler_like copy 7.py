import datetime
import math
import os
import sys

import backtrader as bt
import numpy as np
import pandas as pd

'''
    这里的boll去除了长周期带来的干扰，只关注短周期的趋势
    如果长周期方向反转导致短周期亏损，那忽略这笔交易
'''

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

CURRENT_INSTID = ""
CURRENT_SHORT_BAR = ""
CURRENT_LONG_BAR = ""
CURRENT_TREND_INDICATOR = ""      # copy 7：当前使用的长周期趋势方法

PRINT_MARKET = 0
PRINT_TRADE_OPS = 0
PRINT_TRADE_RECORDS = 0
PRINT_ORIGINAL_OUTPUT = 1

GLOBAL_DF_1M = None

def _csv_path_for_symbol(symbol):
    symbol = str(symbol).lower()
    fname = f"{symbol}_ohlcv_robust_1m_since_2022.csv"
    base = os.path.dirname(__file__)
    p1 = os.path.abspath(os.path.join(base, '..', 'strategyAI', 'data', fname))
    p2 = os.path.abspath(os.path.join(base, '..', '..', 'strategyAI', 'data', fname))
    return p1 if os.path.exists(p1) else p2

def _drop_scale_contamination(df, lo=0.2, hi=5.0, ref_days=90):
    """剔除「价格量级污染块」：部分数据文件会把整段时间写成错误量级的价格
    （实测 BTC 2024-08-25~09-24 一个月被写成 ~$67，而真实约 $60000）。
    方法：以 ref_days 天窗口的滚动中位数作为「局部真实量级」基准（对渐变行情
    自适应、对短期波动稳健），凡 close 偏离基准 <lo× 或 >hi× 的行判为污染并整段剔除。
    返回 (清洗后df, 剔除行数)。对正常波动不误伤（实测 NEAR 剔除 0 行）。"""
    if len(df) < 10 or 'close' not in df.columns:
        return df, 0
    close = df['close']
    # 降到小时再算长窗口滚动中位数，兼顾稳健与速度；窗口需大于污染块跨度
    hourly = close.resample('1h').last().dropna()
    if len(hourly) < 24:
        return df, 0
    win = 24 * int(ref_days)
    ref_h = hourly.rolling(window=win, min_periods=24, center=True).median()
    ref = ref_h.reindex(close.index, method='ffill').bfill()
    ratio = close / ref
    bad = (ratio < lo) | (ratio > hi) | ref.isna()
    n = int(bad.sum())
    if n:
        df = df[~bad]
    return df, n


def load_symbol_csv(symbol, calc_full_flag='Y'):
    path = _csv_path_for_symbol(symbol)
    # 全列按字符串读入，避免混合类型告警，再逐列容错转换
    df = pd.read_csv(path, dtype=str, low_memory=False)
    # datetime 容错解析：损坏/错位行（如价格串进了时间列）会得到 NaT
    ts = pd.to_datetime(df['datetime'], utc=True, errors='coerce')
    df['timestamp'] = ts.dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    # OHLC 强制转数值，无法解析的置 NaN
    for col in ('open', 'high', 'low', 'close'):
        df[col] = pd.to_numeric(df.get(col), errors='coerce')
    # 丢弃时间或 OHLC 缺失/损坏的行（零星坏行，不影响整体序列）
    before = len(df)
    df = df.dropna(subset=['timestamp', 'open', 'high', 'low', 'close'])
    dropped = before - len(df)
    if dropped:
        print(f"[load_symbol_csv] {symbol}: 丢弃 {dropped} 行损坏数据（时间/OHLC 无法解析）")
    df.set_index('timestamp', inplace=True)
    df = df[['open', 'high', 'low', 'close']].astype(float)
    # 时间戳去重，保留首次出现，避免坏行造成的重复索引
    df = df[~df.index.duplicated(keep='first')]
    # 剔除价格量级污染块（错误量级的整段数据，会制造假跳变污染回测）
    df, n_contam = _drop_scale_contamination(df)
    if n_contam:
        print(f"[load_symbol_csv] {symbol}: 剔除 {n_contam} 行价格量级污染数据"
              f"（偏离90天滚动中位数 <0.2× 或 >5×）")
    if str(calc_full_flag).upper() == 'N' and len(df) > 0:
        end = pd.to_datetime(df.index.max())
        start = end - pd.Timedelta(days=300)
        df = df.loc[df.index >= start]
    return df.sort_index()

def _normalize_dir(v):
    s = str(v)
    if s == 'rise' or s == '多':
        return 'rise'
    if s == 'fall' or s == '空':
        return 'fall'
    return None

def _dir_to_value(v):
    d = _normalize_dir(v)
    if d == 'rise':
        return 1.0
    if d == 'fall':
        return -1.0
    return 0.0

def _value_to_dir(v):
    try:
        if v >= 0.5:
            return 'rise'
        if v <= -0.5:
            return 'fall'
    except Exception:
        return None
    return None

def _to_pandas_freq(bar):
    s = str(bar).strip()
    sl = s.lower()
    if sl.endswith('m'):
        n = ''.join(ch for ch in sl if ch.isdigit()) or '1'
        return f"{n}min"
    if sl.endswith('h') or s.endswith('H'):
        n = ''.join(ch for ch in sl if ch.isdigit()) or '1'
        return f"{n}h"
    if sl.endswith('d') or s.endswith('D'):
        n = ''.join(ch for ch in sl if ch.isdigit()) or '1'
        return f"{n}D"
    if sl.endswith('w') or s.endswith('W'):
        n = ''.join(ch for ch in sl if ch.isdigit()) or '1'
        return f"{n}W"
    return s

def _resample_ohlc(df_1m, bar):
    freq = _to_pandas_freq(bar)
    ohlc = df_1m.resample(freq, label='right', closed='right').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
    try:
        ohlc.index = ohlc.index.tz_localize(None)
    except Exception:
        pass
    ohlc['confirm'] = 1.0
    ohlc = calculate_adx(ohlc, period=14)
    return ohlc

def calculate_adx(df, period=14):
    df['prev_close'] = df['close'].shift(1)
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['prev_close'])
    df['tr3'] = abs(df['low'] - df['prev_close'])
    df['TR'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    df['high_diff'] = df['high'] - df['high'].shift(1)
    df['low_diff'] = df['low'].shift(1) - df['low']
    df['+DM'] = np.where((df['high_diff'] > df['low_diff']) & (df['high_diff'] > 0), df['high_diff'], 0)
    df['-DM'] = np.where((df['low_diff'] > df['high_diff']) & (df['low_diff'] > 0), df['low_diff'], 0)
    alpha = 1 / period
    df['ATR_ADX'] = df['TR'].ewm(alpha=alpha, adjust=False).mean()
    df['+DM_smooth'] = df['+DM'].ewm(alpha=alpha, adjust=False).mean()
    df['-DM_smooth'] = df['-DM'].ewm(alpha=alpha, adjust=False).mean()
    df['+DI'] = 100 * (df['+DM_smooth'] / df['ATR_ADX'])
    df['-DI'] = 100 * (df['-DM_smooth'] / df['ATR_ADX'])
    df['DI_sum'] = df['+DI'] + df['-DI']
    df['DI_diff'] = abs(df['+DI'] - df['-DI'])
    df['DX'] = np.where(df['DI_sum'] != 0, 100 * (df['DI_diff'] / df['DI_sum']), 0)
    df['ADX'] = df['DX'].ewm(alpha=alpha, adjust=False).mean()
    return df

def get_adaptive_smooth_weight(adx_value, base_weight=0.7):
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    curr_weight = 1.0 - hist_weight
    return hist_weight, curr_weight

def get_period_data(instId, bar):
    if GLOBAL_DF_1M is None:
        raise RuntimeError('GLOBAL_DF_1M is not initialized')
    return _resample_ohlc(GLOBAL_DF_1M, bar)

# ============================================================================
# 【copy 7 修改说明】
# 原文件 trend_strategy_boll_limit_scheduler_like.py 保持不变。
# 本副本只改动【长周期趋势方向判断】，把它从单一方法扩展为可切换的方法库。
#
# copy 4 已经用过的 9 种（本副本【不重复】）：
#   macd_adx / macd_hist / ema_cross / ma_slope / adx_di /
#   supertrend / donchian / linreg / quad_dif
#
# 本副本【新增 19 种】，全部与 copy 4 无重叠：
#   振荡类   : rsi_mid · cmo
#   通道类   : keltner · ichimoku
#   均线类   : kama · hma · triple_ma · ma_spread
#   突破/翻转: psar · vortex
#   强度类   : aroon · trix · elder_ray
#   动量类   : tsmom
#   斜率类   : bb_mid_slope
#   多尺度   : dual_scale · htf_weekly        ← 高周期确认
#   组合投票 : vote3 · vote5                  ← 多方法共识
#
# 原版 macd_adx（ADX 自适应平滑 MACD 柱状态机）保留为基线参照。
# 【当前默认】长周期趋势指标已切换为 keltner（EMA20 ± 2×ATR20 通道突破，
#   破上轨多、破下轨空、回归中轨保持），get_latest_data / run_with_csv /
#   main / --run 四处默认值均已同步；兼容层分发器签名保持 macd_adx 不变。
#
# 另附 analyze_trend_segments / compare_trend_indicators 两个诊断工具，
# 可直接对比 20 种方法在同一段长周期数据上的趋势段特征。
# ============================================================================

def _bar_minutes(bar):
    """把 '3m'/'1H'/'4H'/'1D' 等周期字符串换算成分钟数。"""
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


def _v7_ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _v7_wma(s, n):
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n, min_periods=n).apply(lambda x: float(np.dot(x, w) / w.sum()), raw=True)


def _v7_atr(df, n=14):
    pc = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'],
                    (df['high'] - pc).abs(),
                    (df['low'] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def _v7_rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.ewm(alpha=1.0 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = au / ad.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _v7_hold(arr, allow_zero=False):
    """把 0(中性) 用上一个非零方向填充，让趋势段连续。
    allow_zero=True 时保留 0（用于需要明确"无方向"的方法）。"""
    out = np.zeros(len(arr), dtype=float)
    last = 0.0
    for i, v in enumerate(arr):
        v = 0.0 if not np.isfinite(v) else float(v)
        if v != 0.0:
            last = v
        out[i] = last
    return out


def _v7_sign(x, thr=0.0):
    x = np.asarray(x, dtype=float)
    out = np.zeros(len(x))
    out[np.nan_to_num(x) > thr] = 1.0
    out[np.nan_to_num(x) < -thr] = -1.0
    return out


# ============================ 原版基线：MACD + ADX 自适应平滑状态机 ============
def _macd_adx_direction(df):
    """原版方法（基线参照）：ADX 自适应加权的平滑 MACD 柱状态机。
    权重 hist_weight = min(0.7 + ADX/200, 1)；用「柱 vs 平滑柱」和 MACD 零轴
    共同决定 rise/fall，未确认时进入 histogram 等待态。"""
    d = df.copy()
    if 'ADX' not in d.columns:
        d = calculate_adx(d, period=14)
    macd_line = _v7_ema(d['close'], 12) - _v7_ema(d['close'], 26)
    signal_line = _v7_ema(macd_line, 9)
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
                    justice = "histogram"
                    deal = "waitRise"
            else:
                if i > 0 and m[i - 1] < 0:
                    modify = -1.0
                else:
                    justice = "histogram"
                    deal = "waitFall"
        elif justice == "histogram":
            if deal == "waitRise":
                if h[i] > 0:
                    modify = 1.0
                    justice = "smoothed_histogram"
                else:
                    modify = -1.0
            elif deal == "waitFall":
                if h[i] < 0:
                    modify = -1.0
                    justice = "smoothed_histogram"
                else:
                    modify = 1.0
        out[i] = modify
    return out


# ============================ 新增 19 种 ====================================
def _rsi_mid_direction(df, period=14, hi=55.0, lo=45.0):
    """RSI 中轴方向（带迟滞）：RSI>hi 多，RSI<lo 空，中间保持。
    用的不是超买超卖反转，而是**力度中轴**——与 copy 4 的动量类均不重叠。"""
    r = _v7_rsi(df['close'], period).values
    raw = np.zeros(len(r))
    raw[r > hi] = 1.0
    raw[r < lo] = -1.0
    return _v7_hold(raw)


def _cmo_direction(df, period=20, thr=0.0):
    """Chande 动量摆动：CMO = 100*(上涨和 − 下跌和)/(上涨和 + 下跌和)。"""
    d = df['close'].diff()
    up = d.clip(lower=0.0).rolling(period, min_periods=period).sum()
    dn = (-d).clip(lower=0.0).rolling(period, min_periods=period).sum()
    cmo = 100 * (up - dn) / (up + dn).replace(0.0, np.nan)
    return _v7_hold(_v7_sign(cmo.values, thr))


def _keltner_direction(df, period=20, mult=2.0, atr_n=20):
    """Keltner 通道突破方向：EMA ± mult×ATR。破上轨多、破下轨空，回归中轨保持。"""
    mid = _v7_ema(df['close'], period)
    a = _v7_atr(df, atr_n)
    up, lo = mid + mult * a, mid - mult * a
    raw = np.zeros(len(df))
    raw[(df['close'] > up).values] = 1.0
    raw[(df['close'] < lo).values] = -1.0
    return _v7_hold(raw)


def _ichimoku_direction(df):
    """一目均衡表云图方向：价格在云上方为多、下方为空、云内保持。
    云图用 shift(先行跨) 保证可用信息只到当前 bar。"""
    h, l, c = df['high'], df['low'], df['close']
    tenkan = (h.rolling(9, min_periods=9).max() + l.rolling(9, min_periods=9).min()) / 2
    kijun = (h.rolling(26, min_periods=26).max() + l.rolling(26, min_periods=26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((h.rolling(52, min_periods=52).max() + l.rolling(52, min_periods=52).min()) / 2).shift(26)
    top = pd.concat([span_a, span_b], axis=1).max(axis=1)
    bot = pd.concat([span_a, span_b], axis=1).min(axis=1)
    raw = np.zeros(len(df))
    raw[(c > top).values] = 1.0
    raw[(c < bot).values] = -1.0
    return _v7_hold(raw)


def _kama_direction(df, period=10, fast=2, slow=30, k=3):
    """Kaufman 自适应均线方向：效率比调节平滑速度，方向取 KAMA 差分。"""
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
    return _v7_hold(_v7_sign((kama - kama.shift(k)).values))


def _hma_direction(df, period=55, k=3):
    """Hull 低滞后均线方向：方向取 HMA 差分，再要求价格站对边。"""
    c = df['close']
    half, root = max(1, period // 2), max(1, int(np.sqrt(period)))
    hma_line = _v7_wma(2 * _v7_wma(c, half) - _v7_wma(c, period), root)
    slope = _v7_sign((hma_line - hma_line.shift(k)).values)
    side = np.where((c > hma_line).values, 1.0, -1.0)
    both = np.where(slope * side > 0, slope, 0.0)
    return _v7_hold(both)


def _triple_ma_direction(df, fast=10, mid=30, slow=60):
    """三均线排列：快>中>慢 为多，反向为空，纠缠时保持上一个方向。"""
    f = _v7_ema(df['close'], fast)
    m = _v7_ema(df['close'], mid)
    s = _v7_ema(df['close'], slow)
    raw = np.zeros(len(df))
    raw[(f > m) & (m > s)] = 1.0
    raw[(f < m) & (m < s)] = -1.0
    return _v7_hold(raw)


def _ma_spread_direction(df, fast=12, slow=26, atr_n=14, thr=0.10):
    """均线分离度方向：把 (快均线−慢均线)/ATR 当作标准化的趋势强度。"""
    spread = (_v7_ema(df['close'], fast) - _v7_ema(df['close'], slow)) / _v7_atr(df, atr_n).replace(0.0, np.nan)
    return _v7_hold(_v7_sign(spread.values, thr))


def _psar_direction(df, af0=0.02, step=0.02, max_af=0.2):
    """抛物线 SAR 方向：SAR 在价格下方为多，上方为空（递归，天然因果）。"""
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


def _vortex_direction(df, period=14):
    """Vortex 指标：VI+ 与 VI− 的相对大小定义趋势方向。"""
    h, l, c = df['high'], df['low'], df['close']
    vm_p = (h - l.shift(1)).abs()
    vm_m = (l - h.shift(1)).abs()
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    vi_p = vm_p.rolling(period, min_periods=period).sum() / tr.rolling(period, min_periods=period).sum()
    vi_m = vm_m.rolling(period, min_periods=period).sum() / tr.rolling(period, min_periods=period).sum()
    return _v7_hold(_v7_sign((vi_p - vi_m).values))


def _aroon_direction(df, period=25, band=50.0):
    """Aroon 方向：距离「最近最高/最低」的远近决定趋势，带中性带。"""
    n = period
    up = df['high'].rolling(n + 1, min_periods=n + 1).apply(
        lambda x: 100.0 * float(np.argmax(x)) / n, raw=True)
    dn = df['low'].rolling(n + 1, min_periods=n + 1).apply(
        lambda x: 100.0 * float(np.argmin(x)) / n, raw=True)
    raw = np.zeros(len(df))
    raw[(up >= band) & (dn <= 100.0 - band)] = 1.0
    raw[(dn >= band) & (up <= 100.0 - band)] = -1.0
    return _v7_hold(raw)


def _trix_direction(df, period=12, k=1):
    """TRIX 三重指数平滑：对趋势噪声极不敏感，方向取 TRIX 差分。"""
    e1 = _v7_ema(df['close'], period)
    e2 = _v7_ema(e1, period)
    e3 = _v7_ema(e2, period)
    trix = 100 * (e3 / e3.shift(1) - 1)
    return _v7_hold(_v7_sign((trix - trix.shift(k)).values))


def _elder_ray_direction(df, period=13):
    """Elder 多空力度：high/low 相对 EMA 的偏离，看谁在掌控。"""
    e = _v7_ema(df['close'], period)
    bull = df['high'] - e
    bear = df['low'] - e
    raw = np.zeros(len(df))
    raw[(bull > 0) & (bear > 0)] = 1.0
    raw[(bull < 0) & (bear < 0)] = -1.0
    return _v7_hold(raw)


def _tsmom_direction(df, lookback=30):
    """时间序列动量：自身过去 lookback 根的累计收益方向。"""
    r = df['close'] / df['close'].shift(lookback) - 1.0
    return _v7_hold(_v7_sign(r.values))


def _bb_mid_slope_direction(df, period=20, k=3):
    """布林中轨斜率方向：中轨差分正负，反映中枢位置的移动方向。"""
    mid = df['close'].rolling(period, min_periods=period).mean()
    return _v7_hold(_v7_sign((mid - mid.shift(k)).values))


def _dual_scale_direction(df, fast=10, slow=40, k=3):
    """双尺度一致：快尺度与慢尺度方向**都**看多才给多，否则 0（不强给方向）。"""
    c = df['close']
    f = _v7_ema(c, fast) - _v7_ema(c, fast).shift(k)
    s = _v7_ema(c, slow) - _v7_ema(c, slow).shift(k)
    fs, ss = _v7_sign(f.values), _v7_sign(s.values)
    agree = np.where(fs == ss, fs, 0.0)
    return _v7_hold(agree)


def _htf_weekly_direction(df, base_period=10, factor=5, k=3):
    """高周期确认（真·多周期）：把长周期数据再合成 factor 倍的大周期，
    只有大周期方向与当前周期方向一致时才给出方向，否则 0。
    大周期方向在该大周期**走完**之前不可用 → 只向前填充已完成的组。"""
    base = _v7_sign((_v7_ema(df['close'], base_period) - _v7_ema(df['close'], base_period).shift(k)).values)
    n = len(df)
    grp = np.arange(n) // factor
    htf_dir = np.zeros(n)
    out = np.zeros(n)
    prev_dir = 0.0
    # 逐组：组内不加方向，整组结束后才把该组收盘方向用于下一组
    for g in range(grp.max() + 1 if n else 0):
        idx = np.where(grp == g)[0]
        out[idx] = prev_dir
        last = idx[-1]
        if np.isfinite(df['close'].values[last]):
            prev_dir = float(base[last])
    both = np.where(base * out > 0, base, 0.0)
    return _v7_hold(both)


def _vote_direction(df, methods, need):
    """多方法投票：统计看多/看空票数，达到 need 票才给方向，否则保持。"""
    votes = np.zeros(len(df))
    for fn in methods:
        v = np.asarray(fn(df), dtype=float)
        votes += np.nan_to_num(v)
    raw = np.zeros(len(df))
    raw[votes >= need] = 1.0
    raw[votes <= -need] = -1.0
    return _v7_hold(raw)


def _vote3_direction(df):
    """三方法共识：RSI中轴 + Aroon + Vortex，三取二。"""
    return _vote_direction(df, [
        lambda d: _rsi_mid_direction(d),
        lambda d: _aroon_direction(d),
        lambda d: _vortex_direction(d),
    ], need=2)


def _vote5_direction(df):
    """五方法共识：RSI中轴 + Aroon + Vortex + KAMA + HMA，五取三。"""
    return _vote_direction(df, [
        lambda d: _rsi_mid_direction(d),
        lambda d: _aroon_direction(d),
        lambda d: _vortex_direction(d),
        lambda d: _kama_direction(d),
        lambda d: _hma_direction(d),
    ], need=3)


# ============================ 指标导航页补充 6 种 ==============================
# 为 Web「指标导航」可视化回测页补充的常见趋势方法（2026-09 新增）。
# 只往 TREND_INDICATORS 注册表追加新键，不改动上方任何既有方法与默认值，
# 因此不影响原文件回测/调度链路的稳定性。命名与实现口径对齐 copy 4 家族，
# 其中 supertrend/donchian/ema_cross/ma_slope/linreg 与 COPY4_ONLY 同名方法
# 语义一致（本副本内自含实现，调用仍走注册表，不触发 COPY4_ONLY 报错）。

def _supertrend_direction(df, period=10, mult=3.0):
    """SuperTrend 方向：HL2 ± mult×ATR 带状轨道，收盘破上带转多、破下带转空，
    带内保持（标准 SuperTrend 状态机的因果实现）。"""
    a = _v7_atr(df, period)
    hl2 = (df['high'] + df['low']) / 2.0
    up_band = (hl2 - mult * a).values
    dn_band = (hl2 + mult * a).values
    c = df['close'].values
    n = len(c)
    out = np.zeros(n)
    trend = 1.0
    final_up, final_dn = np.nan, np.nan
    for i in range(n):
        if not np.isfinite(up_band[i]) or not np.isfinite(c[i]):
            out[i] = trend
            continue
        # 轨道棘轮：多头时下轨只升不降，空头时上轨只降不升
        final_up = up_band[i] if (not np.isfinite(final_up) or up_band[i] > final_up
                                  or c[i - 1] < final_up) else final_up
        final_dn = dn_band[i] if (not np.isfinite(final_dn) or dn_band[i] < final_dn
                                  or c[i - 1] > final_dn) else final_dn
        if trend > 0 and c[i] < final_up:
            trend = -1.0
        elif trend < 0 and c[i] > final_dn:
            trend = 1.0
        out[i] = trend
    return out


def _donchian_direction(df, period=55):
    """唐奇安通道突破：收盘创 period 根新高为多、新低为空，通道内保持。
    通道取 shift(1)（上一根为止的极值），避免用当根自身极值自我确认。"""
    hh = df['high'].rolling(period, min_periods=period).max().shift(1)
    ll = df['low'].rolling(period, min_periods=period).min().shift(1)
    raw = np.zeros(len(df))
    raw[(df['close'] > hh).values] = 1.0
    raw[(df['close'] < ll).values] = -1.0
    return _v7_hold(raw)


def _ema_cross_direction(df, fast=12, slow=26):
    """双 EMA 金叉/死叉：快线在慢线上方为多，下方为空。"""
    f = _v7_ema(df['close'], fast)
    s = _v7_ema(df['close'], slow)
    return _v7_hold(_v7_sign((f - s).values))


def _ma_slope_direction(df, period=20, k=5):
    """简单均线斜率：SMA(period) 的 k 根差分正负定方向。"""
    ma = df['close'].rolling(period, min_periods=period).mean()
    return _v7_hold(_v7_sign((ma - ma.shift(k)).values))


def _linreg_direction(df, period=14):
    """线性回归斜率：对最近 period 根收盘做最小二乘拟合，斜率正负定方向。"""
    x = np.arange(period, dtype=float)
    x = x - x.mean()
    denom = float((x * x).sum())

    def _slope(win):
        return float(((win - win.mean()) * x).sum() / denom)

    slope = df['close'].rolling(period, min_periods=period).apply(_slope, raw=True)
    return _v7_hold(_v7_sign(slope.values))


def _trix_ma_direction(df, period=12, signal=9):
    """TRIX + 信号线：TRIX 上穿其 signal 根 EMA 为多、下穿为空（MACD 式用法，
    与 _trix_direction 的「TRIX 自身差分」口径互补）。"""
    e1 = _v7_ema(df['close'], period)
    e2 = _v7_ema(e1, period)
    e3 = _v7_ema(e2, period)
    trix = 100 * (e3 / e3.shift(1) - 1)
    sig = _v7_ema(trix, signal)
    return _v7_hold(_v7_sign((trix - sig).values))


# ============================ 方法注册表 ====================================
# 键名即 trend_indicator 参数取值。'macd_adx' 为原版基线，其余为本副本新增。
TREND_INDICATORS = {
    # --- 基线（与 copy 4 同名同义，便于横向对照）---
    'macd_adx':     lambda df: _macd_adx_direction(df),
    # --- 本副本新增 19 种 ---
    'rsi_mid':      lambda df: _rsi_mid_direction(df),
    'cmo':          lambda df: _cmo_direction(df),
    'keltner':      lambda df: _keltner_direction(df),
    'ichimoku':     lambda df: _ichimoku_direction(df),
    'kama':         lambda df: _kama_direction(df),
    'hma':          lambda df: _hma_direction(df),
    'triple_ma':    lambda df: _triple_ma_direction(df),
    'ma_spread':    lambda df: _ma_spread_direction(df),
    'psar':         lambda df: _psar_direction(df),
    'vortex':       lambda df: _vortex_direction(df),
    'aroon':        lambda df: _aroon_direction(df),
    'trix':         lambda df: _trix_direction(df),
    'elder_ray':    lambda df: _elder_ray_direction(df),
    'tsmom':        lambda df: _tsmom_direction(df),
    'bb_mid_slope': lambda df: _bb_mid_slope_direction(df),
    'dual_scale':   lambda df: _dual_scale_direction(df),
    'htf_weekly':   lambda df: _htf_weekly_direction(df),
    'vote3':        lambda df: _vote3_direction(df),
    'vote5':        lambda df: _vote5_direction(df),
    # --- 指标导航页补充 6 种（Web 可视化回测新增）---
    'supertrend':   lambda df: _supertrend_direction(df),
    'donchian':     lambda df: _donchian_direction(df),
    'ema_cross':    lambda df: _ema_cross_direction(df),
    'ma_slope':     lambda df: _ma_slope_direction(df),
    'linreg':       lambda df: _linreg_direction(df),
    'trix_ma':      lambda df: _trix_ma_direction(df),
}

# copy 4 用过、本副本刻意不重复的方法名（调用时报错并提示）
COPY4_ONLY = ['macd_hist', 'ema_cross', 'ma_slope', 'adx_di',
              'supertrend', 'donchian', 'linreg', 'quad_dif']


def compute_direction_series(df, indicator='macd_adx'):
    """按指定方法计算长周期方向序列（+1/-1/0），返回与 df 等长的 numpy 数组。"""
    key = str(indicator).strip().lower()
    # 注册表优先：指标导航页补充后，部分 COPY4_ONLY 同名方法已在本副本自含实现，
    # 只要注册表里有就直接用；仅当两处都没有时才报「copy 4 专属」提示。
    if key not in TREND_INDICATORS and key in COPY4_ONLY:
        raise ValueError(
            f"'{key}' 属于 copy 4 已实现的方法，本副本（copy 7）刻意不重复。"
            f"请从以下选择：{list(TREND_INDICATORS.keys())}")
    if key not in TREND_INDICATORS:
        raise ValueError(f"未知趋势方法 '{indicator}'，可选: {list(TREND_INDICATORS.keys())}")
    dirs = np.asarray(TREND_INDICATORS[key](df), dtype=float)
    dirs = np.nan_to_num(dirs, nan=0.0, posinf=0.0, neginf=0.0)
    if len(dirs) != len(df):
        tmp = np.zeros(len(df), dtype=float)
        m = min(len(dirs), len(df))
        tmp[:m] = dirs[:m]
        dirs = tmp
    return dirs


def calculate_period_direction(df, indicator='macd_adx'):
    """计算并返回长周期"最新"方向（'rise'/'fall'/None），基于指定方法。"""
    dirs = compute_direction_series(df, indicator)
    if len(dirs) == 0:
        return None
    return _value_to_dir(dirs[-1])


def fill_long_direction_series(df, indicator='macd_adx'):
    """按指定方法填充 df['LONG_DIRECTION'] 列（+1/-1/0）。"""
    dirs = compute_direction_series(df, indicator)
    df['LONG_DIRECTION'] = pd.Series(dirs, index=df.index).astype(float).fillna(0.0)


def analyze_trend_segments(df_long, indicator, long_bar):
    """统计某方法在长周期数据上的趋势段特征（用于挑选方向判断方法）。
    趋势段 = 一段连续、同向且非零的长周期 bar。返回：
      num_segments/reversals 段数与反转次数
      avg|median|max_dur_hours 单段持续时间
      coverage 有方向 bar 占比
      reversals_per_day 日均反转次数（越高=翻转越频繁=越不稳定）
    """
    dirs = compute_direction_series(df_long, indicator)
    bar_min = _bar_minutes(long_bar)
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
    dur_hours = [b * bar_min / 60.0 for b in dur_bars]
    nonzero = int(np.sum(dirs != 0.0))
    total_days = n * bar_min / 60.0 / 24.0
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


def compare_trend_indicators(df_long, long_bar, indicators=None, verbose=True):
    """对全部方法跑一遍趋势段统计，按「日均反转次数」从低到高排序输出。
    反转越少 = 方向越稳 = 越适合当长周期过滤；反转过多等于没过滤。"""
    keys = indicators or list(TREND_INDICATORS.keys())
    rows = []
    for k in keys:
        try:
            rows.append(analyze_trend_segments(df_long, k, long_bar))
        except Exception as e:
            rows.append({'indicator': k, 'error': f"{type(e).__name__}: {e}"})
    ok = [r for r in rows if 'error' not in r]
    ok.sort(key=lambda r: r['reversals_per_day'])
    if verbose:
        print("\n" + "=" * 108)
        print("  长周期趋势方向方法对比（copy 7）  —— 按日均反转次数升序，反转越少方向越稳")
        print("=" * 108)
        print(f"{'方法':<14}{'方向段':>8}{'反转':>7}{'平均时长(h)':>13}{'中位(h)':>10}"
              f"{'最长(h)':>10}{'有向占比':>10}{'日均反转':>10}")
        print("-" * 108)
        for r in ok:
            print(f"{r['indicator']:<14}{r['num_segments']:>8}{r['reversals']:>7}"
                  f"{r['avg_dur_hours']:>13.1f}{r['median_dur_hours']:>10.1f}"
                  f"{r['max_dur_hours']:>10.1f}{r['coverage']*100:>9.1f}%"
                  f"{r['reversals_per_day']:>10.3f}")
        print("-" * 108)
        for r in rows:
            if 'error' in r:
                print(f"  ❌ {r['indicator']}: {r['error']}")
    return ok

class PandasDataWithLongDirection(bt.feeds.PandasData):
    lines = ('LONG_DIRECTION', 'LONG_CONF')
    params = (('LONG_DIRECTION', -1), ('LONG_CONF', -1))

def get_latest_data(instId, short_bar="1H", long_bar="1D", order_size=1.0,
                    trend_indicator="keltner", leverage_times=10.0,
                    entry_atr_mult=0.0, exit_atr_mult=0.0):
    global CURRENT_INSTID, CURRENT_SHORT_BAR, CURRENT_LONG_BAR, CURRENT_TREND_INDICATOR
    CURRENT_INSTID = instId
    CURRENT_SHORT_BAR = short_bar
    CURRENT_LONG_BAR = long_bar
    CURRENT_TREND_INDICATOR = trend_indicator
    if PRINT_MARKET:
        print(f"正在获取短周期数据 ({short_bar})...")
    df_short = get_period_data(instId, short_bar)
    if PRINT_MARKET:
        print(f"正在获取长周期数据 ({long_bar})...")
    df_long = get_period_data(instId, long_bar)
    if PRINT_MARKET:
        print(f"计算长周期方向 (方法: {trend_indicator})...")
    long_direction = calculate_period_direction(df_long, trend_indicator)
    fill_long_direction_series(df_long, trend_indicator)
    if 'LONG_DIRECTION' not in df_long.columns:
        df_long['LONG_DIRECTION'] = 0.0
    df_long['LONG_DIRECTION'] = df_long['LONG_DIRECTION'].fillna(0.0)
    conf_window = 3
    long_dir_values = df_long['LONG_DIRECTION'].values
    long_conf = np.zeros(len(long_dir_values), dtype=float)
    for i in range(len(long_dir_values)):
        if i + 1 < conf_window:
            continue
        window = long_dir_values[i + 1 - conf_window:i + 1]
        if np.all(window == window[0]) and window[0] != 0.0:
            long_conf[i] = 1.0
    df_long['LONG_CONF'] = long_conf
    if PRINT_MARKET:
        print(f"长周期方向: {long_direction}")
        print(f"使用的方法: {trend_indicator}")
    df = df_short
    try:
        if hasattr(df.index, 'tz') and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        if hasattr(df_long.index, 'tz') and df_long.index.tz is not None:
            df_long.index = df_long.index.tz_localize(None)
        short_times = pd.DataFrame({'ts': pd.to_datetime(df.index)})
        long_times = pd.DataFrame({
            'ts': pd.to_datetime(df_long.index),
            'LONG_DIRECTION': df_long['LONG_DIRECTION'].values,
            'LONG_CONF': df_long['LONG_CONF'].values
        })
        short_times = short_times.sort_values('ts')
        long_times = long_times.sort_values('ts')
        aligned = pd.merge_asof(short_times, long_times, on='ts', direction='backward')
        df['LONG_DIRECTION'] = aligned['LONG_DIRECTION'].values
        df['LONG_CONF'] = aligned['LONG_CONF'].values
    except Exception:
        df['LONG_DIRECTION'] = df_long['LONG_DIRECTION'].reindex(df.index, method='ffill')
        df['LONG_CONF'] = df_long['LONG_CONF'].reindex(df.index, method='ffill')
    df['LONG_DIRECTION'] = df['LONG_DIRECTION'].fillna(0.0)
    df['LONG_CONF'] = df['LONG_CONF'].fillna(0.0)
    first_valid = df['LONG_DIRECTION'].first_valid_index() if 'LONG_DIRECTION' in df.columns else None
    if first_valid is not None:
        df = df.loc[df.index >= first_valid]

    # 短周期 ATR 序列（Wilder 14），用于 BOLL 轨道外扩：
    # 多头开仓价 = 下轨 - entry_atr_mult×ATR，多头平仓价 = 上轨 + exit_atr_mult×ATR
    # 空头开仓价 = 上轨 + entry_atr_mult×ATR，空头平仓价 = 下轨 - exit_atr_mult×ATR
    atr_series_short = df['TR'].ewm(alpha=1/14, adjust=False).mean()
    atr_vals = atr_series_short.to_numpy(dtype=float)

    class DualTimeframeBollLimitStrategy(bt.Strategy):
        params = (
            ('boll_period', 20),
            ('boll_dev', 2.0),
            ('order_size', 1.0),
            ('leverage_times', 10.0),
            ('entry_atr_mult', 0.0),
            ('exit_atr_mult', 0.0),
        )
        def __init__(self):
            self.boll = bt.indicators.BollingerBands(self.data.close, period=self.p.boll_period, devfactor=self.p.boll_dev)
            self.atr_vals = atr_vals
            self.k = 0
            self.pos_size = 0.0
            self.entry_price = None
            self.entry_time = None
            self.entry_long_dir = None
            self.position_flip = False
            self.max_price_since_entry = None
            self.min_price_since_entry = None
            self.trades = []
            self.trade_times = []
            self.trade_dirs = []
            self.trade_records = []
            self.profits = []
            self.excluded_profits = []
            self.max_wins = []
            self.max_losses = []
            self.last_trade_index = None

        def next(self):
            long_dir_value = float(self.data.LONG_DIRECTION[0])
            if math.isnan(long_dir_value):
                long_dir_value = 0.0
            long_dir = _value_to_dir(long_dir_value)
            boll_top = float(self.boll.top[0]) if not math.isnan(self.boll.top[0]) else None
            boll_mid = float(self.boll.mid[0]) if not math.isnan(self.boll.mid[0]) else None
            boll_bot = float(self.boll.bot[0]) if not math.isnan(self.boll.bot[0]) else None
            if boll_top is None or boll_bot is None or boll_mid is None:
                return
            if long_dir is None:
                return
            leverage_times = float(self.p.leverage_times)
            low = float(self.data.low[0])
            high = float(self.data.high[0])
            close_price = float(self.data.close[0])
            now_time = self.data.datetime.datetime(0)
            if self.pos_size != 0 and self.entry_price is not None:
                if self.max_price_since_entry is None or high > self.max_price_since_entry:
                    self.max_price_since_entry = high
                if self.min_price_since_entry is None or low < self.min_price_since_entry:
                    self.min_price_since_entry = low
                if self.entry_long_dir and long_dir != self.entry_long_dir:
                    self.position_flip = True
            # ATR 外扩后的实际挂单价位：开仓轨向外扩 entry_atr_mult 倍，平仓轨向外扩 exit_atr_mult 倍
            idx = len(self) - 1
            atr_now = float(self.atr_vals[idx]) if idx < len(self.atr_vals) and np.isfinite(self.atr_vals[idx]) else 0.0
            entry_mult = float(self.p.entry_atr_mult)
            exit_mult = float(self.p.exit_atr_mult)
            long_entry_price = boll_bot - entry_mult * atr_now   # 多头开仓：下轨 - A×ATR
            short_entry_price = boll_top + entry_mult * atr_now  # 空头开仓：上轨 + A×ATR
            long_exit_price = boll_top + exit_mult * atr_now     # 多头平仓：上轨 + B×ATR
            short_exit_price = boll_bot - exit_mult * atr_now    # 空头平仓：下轨 - B×ATR
            touch_long_entry = low <= long_entry_price <= high
            touch_short_entry = low <= short_entry_price <= high
            touch_long_exit = low <= long_exit_price <= high
            touch_short_exit = low <= short_exit_price <= high
            if self.pos_size == 0:
                if long_dir == 'rise' and touch_long_entry:
                    self.k += 1
                    self.pos_size = self.p.order_size
                    self.entry_price = long_entry_price
                    self.entry_time = now_time
                    self.entry_long_dir = long_dir
                    self.position_flip = False
                    self.max_price_since_entry = long_entry_price
                    self.min_price_since_entry = long_entry_price
                    self.trades.append(long_entry_price)
                    self.trade_times.append(now_time)
                    self.trade_dirs.append('rise')
                    if PRINT_TRADE_OPS:
                        print(f"开多 第{self.k}次 (方向:{long_dir}) 触边即入 BOLL下轨-{entry_mult:.2f}×ATR 限价{long_entry_price:.4f} {now_time}")
                elif long_dir == 'fall' and touch_short_entry:
                    self.k += 1
                    self.pos_size = -self.p.order_size
                    self.entry_price = short_entry_price
                    self.entry_time = now_time
                    self.entry_long_dir = long_dir
                    self.position_flip = False
                    self.max_price_since_entry = short_entry_price
                    self.min_price_since_entry = short_entry_price
                    self.trades.append(short_entry_price)
                    self.trade_times.append(now_time)
                    self.trade_dirs.append('fall')
                    if PRINT_TRADE_OPS:
                        print(f"开空 第{self.k}次 (方向:{long_dir}) 触边即入 BOLL上轨+{entry_mult:.2f}×ATR 限价{short_entry_price:.4f} {now_time}")
            elif self.pos_size > 0 and touch_long_exit:
                exit_price = long_exit_price
                profit = (exit_price / self.entry_price - 1) * 100 * leverage_times
                max_win = (self.max_price_since_entry / self.entry_price - 1) * 100 * leverage_times if self.max_price_since_entry else 0.0
                max_loss = (self.min_price_since_entry / self.entry_price - 1) * 100 * leverage_times if self.min_price_since_entry else 0.0
                excluded = self.position_flip and profit < 0
                if excluded:
                    self.excluded_profits.append(profit)
                else:
                    self.profits.append(profit)
                    self.max_wins.append(max_win)
                    self.max_losses.append(max_loss)
                self.trade_records.append({
                    'dir': 'rise',
                    'open_price': float(self.entry_price),
                    'close_price': float(exit_price),
                    'entry_time': self.entry_time,
                    'exit_time': now_time,
                    'profit': float(profit),
                    'max_win': float(max_win),
                    'max_loss': float(max_loss),
                    'excluded': excluded,
                })
                self.trades.append(exit_price)
                self.trade_times.append(now_time)
                self.last_trade_index = len(self) - 1
                if PRINT_TRADE_OPS:
                    print(f"平多 第{self.k}次 触边即出 BOLL上轨+{exit_mult:.2f}×ATR 限价{exit_price:.4f} 盈亏{profit:.2f}% {now_time}")
                self.pos_size = 0.0
                self.entry_price = None
                self.entry_time = None
                self.entry_long_dir = None
                self.position_flip = False
                self.max_price_since_entry = None
                self.min_price_since_entry = None
            elif self.pos_size < 0 and touch_short_exit:
                exit_price = short_exit_price
                profit = (1 - (exit_price / self.entry_price)) * 100 * leverage_times
                max_win = (1 - (self.min_price_since_entry / self.entry_price)) * 100 * leverage_times if self.min_price_since_entry else 0.0
                max_loss = (1 - (self.max_price_since_entry / self.entry_price)) * 100 * leverage_times if self.max_price_since_entry else 0.0
                excluded = self.position_flip and profit < 0
                if excluded:
                    self.excluded_profits.append(profit)
                else:
                    self.profits.append(profit)
                    self.max_wins.append(max_win)
                    self.max_losses.append(max_loss)
                self.trade_records.append({
                    'dir': 'fall',
                    'open_price': float(self.entry_price),
                    'close_price': float(exit_price),
                    'entry_time': self.entry_time,
                    'exit_time': now_time,
                    'profit': float(profit),
                    'max_win': float(max_win),
                    'max_loss': float(max_loss),
                    'excluded': excluded,
                })
                self.trades.append(exit_price)
                self.trade_times.append(now_time)
                self.last_trade_index = len(self) - 1
                if PRINT_TRADE_OPS:
                    print(f"平空 第{self.k}次 触边即出 BOLL下轨-{exit_mult:.2f}×ATR 限价{exit_price:.4f} 盈亏{profit:.2f}% {now_time}")
                self.pos_size = 0.0
                self.entry_price = None
                self.entry_time = None
                self.entry_long_dir = None
                self.position_flip = False
                self.max_price_since_entry = None
                self.min_price_since_entry = None
            if PRINT_MARKET:
                if self.pos_size > 0 and self.entry_price is not None:
                    current_profit = (close_price / self.entry_price - 1) * 100 * leverage_times
                    state = "持多"
                elif self.pos_size < 0 and self.entry_price is not None:
                    current_profit = (1 - (close_price / self.entry_price)) * 100 * leverage_times
                    state = "持空"
                else:
                    current_profit = 0.0
                    state = "BOLL区间空仓"
                pl_suffix = f" | 盈亏:{current_profit:+.2f}%"
                top_cover = high > boll_top
                bot_cover = low < boll_bot
                if top_cover and bot_cover:
                    rel = "上下轨同时覆盖"
                elif top_cover:
                    rel = "覆盖上轨"
                elif bot_cover:
                    rel = "覆盖下轨"
                elif boll_top > high > low > boll_bot:
                    rel = "轨道区间内"
                else:
                    rel = "触边"
                print(f"{now_time} 长周期:{long_dir} 行情范围: 低{low:.4f} 高{high:.3f} 收{close_price:.3f} | BOLL: 下轨{boll_bot:.3f} 中轨{boll_mid:.3f} 上轨{boll_top:.3f}{pl_suffix} | 状态:{state} | 关系:{rel}")

    cerebro = bt.Cerebro()
    data = PandasDataWithLongDirection(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(DualTimeframeBollLimitStrategy, order_size=float(order_size),
                        leverage_times=float(leverage_times),
                        entry_atr_mult=float(entry_atr_mult),
                        exit_atr_mult=float(exit_atr_mult))
    cerebro.run()
    strat = cerebro.runstrats[0][0]
    latest_data = df.iloc[-1]
    last_trade_data = None
    if strat.last_trade_index is not None:
        last_trade_data = df.iloc[strat.last_trade_index].copy()
        if len(strat.trades) > 0:
            last_trade_data['TRADE_PRICE'] = strat.trades[-1]
            last_trade_data['TRADE_TIME'] = strat.trade_times[-1]
            if len(strat.profits) > 0:
                last_trade_data['PROFIT'] = strat.profits[-1]
            if len(strat.max_wins) > 0:
                last_trade_data['MAX_WIN'] = strat.max_wins[-1]
                last_trade_data['MAX_LOSS'] = strat.max_losses[-1]
    prev_close = df['close'].shift(1) if 'close' in df.columns else None
    tr1 = (df['high'] - df['low']) if 'high' in df.columns and 'low' in df.columns else None
    tr2 = (abs(df['high'] - prev_close)) if 'high' in df.columns and prev_close is not None else None
    tr3 = (abs(df['low'] - prev_close)) if 'low' in df.columns and prev_close is not None else None
    TR = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1) if tr1 is not None and tr2 is not None and tr3 is not None else None
    if TR is not None and len(TR) > 0:
        atr_series = TR.ewm(alpha=1/14, adjust=False).mean()
        atr_value = float(atr_series.iloc[-1])
    else:
        atr_value = float('nan')
    base_money = 100.0
    fix_money_history = [base_money]
    contract_money_history = [base_money]
    mix_money_history = [base_money]
    fix_real_money_history = [base_money]  # 含剔除交易亏损的真实定投曲线（无论是否剔除都计入）
    if PRINT_TRADE_RECORDS and len(strat.trade_records) > 0:
        print("\n=== 交易记录 ===")
    for i, rec in enumerate(strat.trade_records):
        profit = float(rec['profit'])
        fix_before = fix_money_history[-1]
        contract_before = contract_money_history[-1]
        mix_before = mix_money_history[-1]
        if rec['excluded']:
            fix_after = fix_before
            contract_after = contract_before
            mix_after = mix_before
        else:
            fix_after = fix_before + base_money * profit * 0.01
            contract_after = contract_before + contract_before * profit * 0.01
            ratio = mix_before / base_money
            if ratio > 0:
                try:
                    log_value = math.log(ratio) / math.log(1.6)
                    if not math.isnan(log_value) and not math.isinf(log_value):
                        base = base_money * pow(1.6, math.floor(log_value))
                        base = max(base, base_money)
                    else:
                        base = base_money
                except (ValueError, OverflowError):
                    base = base_money
            else:
                base = base_money
            mix_after = mix_before + base * profit * 0.01
        fix_money_history.append(fix_after)
        contract_money_history.append(contract_after)
        mix_money_history.append(mix_after)
        # 真实定投：被剔除的交易亏损也照常计入，反映实盘不可剔除的真实盈亏
        fix_real_money_history.append(fix_real_money_history[-1] + base_money * profit * 0.01)
        if PRINT_TRADE_RECORDS:
            fix_change = fix_after - fix_before
            contract_change = contract_after - contract_before
            mix_change = mix_after - mix_before
            exclude_txt = " 剔除" if rec['excluded'] else ""
            print(f"第{i+1}次交易       开仓:{float(rec['open_price']):.3f} 平仓:{float(rec['close_price']):.3f}   最终盈利:{profit:+.2f}% 最大盈利:{float(rec['max_win']):.2f}%  最大亏损:{float(rec['max_loss']):.2f}% 定投:{fix_before:.0f}→{fix_after:.0f}({fix_change:+.0f})       复投:{contract_before:.0f}→{contract_after:.0f}({contract_change:+.0f})    混投:{mix_before:.0f}→{mix_after:.0f}({mix_change:+.0f}){exclude_txt}")
    if PRINT_ORIGINAL_OUTPUT:
        profits = [r['profit'] for r in strat.trade_records if not r['excluded']]
        max_wins = [r['max_win'] for r in strat.trade_records if not r['excluded']]
        max_losses = [r['max_loss'] for r in strat.trade_records if not r['excluded']]
        total_trades = len(profits)
        winning_trades = sum(1 for p in profits if p > 0)
        losing_trades = sum(1 for p in profits if p < 0)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        total_profit = sum(p for p in profits if p > 0)
        total_loss = sum(p for p in profits if p < 0)
        avg_profit = total_profit / winning_trades if winning_trades > 0 else 0.0
        avg_loss = total_loss / losing_trades if losing_trades > 0 else 0.0
        excluded_trades = sum(1 for r in strat.trade_records if r['excluded'])
        excluded_total_loss = sum(strat.excluded_profits) if strat.excluded_profits else 0.0
        excluded_avg_loss = excluded_total_loss / excluded_trades if excluded_trades > 0 else 0.0
        all_closed_trades = total_trades + excluded_trades
        excluded_ratio = excluded_trades / all_closed_trades if all_closed_trades > 0 else 0.0
        contract_money = contract_money_history[-1] if contract_money_history else 100.0
        fix_money = fix_money_history[-1] if fix_money_history else 100.0
        fix_real_money = fix_real_money_history[-1] if fix_real_money_history else 100.0
        mix_money = mix_money_history[-1] if mix_money_history else 100.0
        win_indices = [i for i, p in enumerate(profits) if p > 0]
        loss_indices = [i for i, p in enumerate(profits) if p < 0]
        hold_total_max_profit = sum(max_wins[i] for i in win_indices) if win_indices else 0.0
        hold_total_max_loss = sum(max_losses[i] for i in loss_indices) if loss_indices else 0.0
        avg_hold_max_profit = (hold_total_max_profit / len(win_indices)) if win_indices else 0.0
        avg_hold_max_loss = (hold_total_max_loss / len(loss_indices)) if loss_indices else 0.0
        hold_pl_ratio = (abs(avg_hold_max_profit / avg_hold_max_loss) if avg_hold_max_loss != 0 else 0.0)
        overall_pl_ratio = (abs(total_profit / total_loss) if total_loss < 0 else 0.0)
        actual_profit_rate = total_profit / hold_total_max_profit if hold_total_max_profit > 0 else 0.0
        actual_loss_rate = abs(total_loss) / abs(hold_total_max_loss) if hold_total_max_loss < 0 else 0.0
        start_time = None
        end_time = None
        included_times = [(r.get('entry_time'), r.get('exit_time')) for r in strat.trade_records if not r['excluded']]
        if included_times:
            start_time = min(t[0] for t in included_times if t[0] is not None)
            end_time = max(t[1] for t in included_times if t[1] is not None)
        total_days = 0
        daily_profit = 0.0
        weekly_profit = 0.0
        if start_time and end_time:
            time_delta = end_time - start_time
            total_days = time_delta.days
            if total_days > 0:
                total_return = (fix_money - 100.0) / 100.0 * 100
                daily_profit = total_return / total_days
                weekly_profit = daily_profit * 7
        max_drawdown = 0.0
        if len(contract_money_history) > 1:
            peak = contract_money_history[0]
            for value in contract_money_history:
                if value > peak:
                    peak = value
                drawdown = (peak - value) / peak * 100 if peak > 0 else 0.0
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
        fix_drawdown = 0.0
        if len(fix_money_history) > 1:
            fix_peak = fix_money_history[0]
            for value in fix_money_history:
                if value > fix_peak:
                    fix_peak = value
                if fix_peak > 0:
                    drawdown = (fix_peak - value) / fix_peak * 100
                    if drawdown > fix_drawdown:
                        fix_drawdown = drawdown
        mix_drawdown = 0.0
        if len(mix_money_history) > 1:
            mix_peak = mix_money_history[0]
            for value in mix_money_history:
                if value > mix_peak:
                    mix_peak = value
                if mix_peak > 0:
                    drawdown = (mix_peak - value) / mix_peak * 100
                    if drawdown > mix_drawdown:
                        mix_drawdown = drawdown
        profit_factor = abs(total_profit / total_loss) if total_loss < 0 else 0.0
        print("\nBOLL限价双周期调度逻辑回测统计")
        print("="*80)
        print(f"交易对: {CURRENT_INSTID}")
        print(f"短周期: {CURRENT_SHORT_BAR}")
        print(f"长周期: {CURRENT_LONG_BAR}")
        print("\n时间统计")
        if start_time and end_time:
            print(f"开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"总天数: {total_days}天")
            print(f"每日盈利: {daily_profit:.2f}%")
            print(f"每周盈利: {weekly_profit:.2f}%")
        else:
            print("无交易记录")
        print(f"\n总交易次数: {total_trades}")
        print(f"胜率: {win_rate:.2%}")
        print(f"盈利次数: {winning_trades}")
        print(f"亏损次数: {losing_trades}")
        print(f"剔除交易数: {excluded_trades}")
        print(f"剔除比例: {excluded_ratio:.2%}")
        print(f"剔除交易总亏损: {excluded_total_loss:.2f}%")
        print(f"剔除交易平均亏损: {excluded_avg_loss:.2f}%")
        print(f"复投最终收益: {contract_money:.2f}")
        print(f"定投最终收益: {fix_money:.2f}")
        print(f"真实定投最终收益(含剔除亏损): {fix_real_money:.2f}")
        print(f"混合最终收益: {mix_money:.2f}")
        print("\n最终盈亏核心指标")
        print(f"最终盈利总和: {total_profit:.2f}%")
        print(f"最终亏损总和: {total_loss:.2f}%")
        print(f"最终盈利均值（仅盈利单）: {avg_profit:.2f}%")
        print(f"最终亏损均值（仅亏损单）: {avg_loss:.2f}%")
        final_mean_pl_ratio = (abs(avg_profit) / abs(avg_loss) if avg_loss != 0 else 0.0)
        final_sum_pl_ratio = (abs(total_profit) / abs(total_loss) if total_loss != 0 else 0.0)
        print(f"最终盈亏均值比: {final_mean_pl_ratio:.2f}")
        print(f"最终盈亏总和比: {final_sum_pl_ratio:.2f}")
        print("\n持仓盈亏核心指标")
        print(f"持仓最大盈利总和: {hold_total_max_profit:.2f}%")
        print(f"持仓最大亏损总和: {hold_total_max_loss:.2f}%")
        print(f"持仓最大盈利均值（仅盈利单）: {avg_hold_max_profit:.2f}%")
        print(f"持仓最大亏损均值（仅亏损单）: {avg_hold_max_loss:.2f}%")
        print(f"持仓最大盈亏均值比: {hold_pl_ratio:.2f}")
        print(f"持仓最大盈亏总和比: {overall_pl_ratio:.2f}")
        print("\n盈亏到手率（最终盈亏与最大盈亏比值）")
        print(f"盈利到手率: {actual_profit_rate:.2%}")
        print(f"亏损到手率: {actual_loss_rate:.2%}")
        print("\n风险与绩效指标")
        print(f"复投最大回撤率: {max_drawdown:.2f}%")
        print(f"定投最大回撤率: {fix_drawdown:.2f}%")
        print(f"混合最大回撤率: {mix_drawdown:.2f}%")
        print(f"盈利因子: {profit_factor:.2f}")
        print("="*80)
    return latest_data, last_trade_data, atr_value, long_direction, df, df_long

def run_with_csv(
    symbol='near',
    calc_full_flag='Y',
    short_bar='15m',
    long_bar='4H',
    order_size=1.0,
    show_original_output=1,
    show_market_output=0,
    show_trade_ops_output=0,
    show_trade_records_output=0,
    trend_indicator='keltner',
    leverage_times=10.0,
    entry_atr_mult=0.0,
    exit_atr_mult=0.0,
):
    global GLOBAL_DF_1M
    global PRINT_MARKET, PRINT_TRADE_OPS, PRINT_TRADE_RECORDS, PRINT_ORIGINAL_OUTPUT
    GLOBAL_DF_1M = load_symbol_csv(symbol, calc_full_flag)
    PRINT_MARKET = 1 if show_market_output else 0
    PRINT_TRADE_OPS = 1 if show_trade_ops_output else 0
    PRINT_TRADE_RECORDS = 1 if show_trade_records_output else 0
    PRINT_ORIGINAL_OUTPUT = 1 if show_original_output else 0
    instId = f"{str(symbol).upper()}-USDT-SWAP"
    latest_data, last_trade_data, atr, long_direction, df, df_long = get_latest_data(
        instId, short_bar, long_bar, order_size=order_size,
        trend_indicator=trend_indicator, leverage_times=leverage_times,
        entry_atr_mult=entry_atr_mult, exit_atr_mult=exit_atr_mult
    )
    if PRINT_ORIGINAL_OUTPUT:
        print(f"\n当前长周期方向: {long_direction}   (方法: {trend_indicator})")
        print("最新行情时间：", latest_data.name)
        print("ATR值:", atr)




# ============================================================================
# copy 7 附加：趋势方向方法横向对比入口
#   python "trend_strategy_boll_limit_scheduler_like copy 7.py" --compare
#   python "trend_strategy_boll_limit_scheduler_like copy 7.py" --run vote3
# ============================================================================
def compare_all_methods(symbol='near', long_bar='4H', calc_full_flag='N'):
    """把全部方法在同一段长周期数据上跑一遍趋势段统计，挑最稳的方向判断。"""
    global GLOBAL_DF_1M
    GLOBAL_DF_1M = load_symbol_csv(symbol, calc_full_flag)
    df_long = _resample_ohlc(GLOBAL_DF_1M, long_bar)
    print(f"\n标的 {symbol.upper()}  长周期 {long_bar}  共 {len(df_long)} 根")
    print(f"时间范围 {df_long.index[0]} ~ {df_long.index[-1]}")
    return compare_trend_indicators(df_long, long_bar)


def _cli():
    import sys
    args = sys.argv[1:]
    if args and args[0] == '--compare':
        lb = args[1] if len(args) > 1 else '4H'
        sym = args[2].lower() if len(args) > 2 else 'near'
        compare_all_methods(symbol=sym, long_bar=lb)
        return
    if args and args[0] == '--run':
        name = args[1] if len(args) > 1 else 'keltner'
        # 复用 main 的配置，只替换趋势方法
        main(trend_indicator=name)
        return
    main()


def main(trend_indicator="keltner"):
    instId = "NEAR-USDT-SWAP"
    short_bar = "15m"
    long_bar = "4H"
    show_original_output = 1
    show_market_output = 0
    show_trade_ops_output = 0
    show_trade_records_output = 0
    # ===== 控制变量 =====
    leverage_times = 10.0    # 杠杆倍数，默认 10 倍
    entry_atr_mult = 0.0     # A：开仓轨在 BOLL 外扩的 ATR 倍数（下轨-A×ATR 开多 / 上轨+A×ATR 开空）
    exit_atr_mult = 0.0      # B：平仓轨在 BOLL 外扩的 ATR 倍数（上轨+B×ATR 平多 / 下轨-B×ATR 平空）
    symbol = instId.split('-')[0].lower()
    run_with_csv(
        symbol=symbol,
        calc_full_flag='Y',
        short_bar=short_bar,
        long_bar=long_bar,
        order_size=1.0,
        show_original_output=show_original_output,
        show_market_output=show_market_output,
        show_trade_ops_output=show_trade_ops_output,
        show_trade_records_output=show_trade_records_output,
        trend_indicator=trend_indicator,
        leverage_times=leverage_times,
        entry_atr_mult=entry_atr_mult,
        exit_atr_mult=exit_atr_mult,
    )


if __name__ == "__main__":
    _cli()
