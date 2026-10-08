#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指标导航 —— 可视化回测服务层
================================
为 Web「指标导航」页（/indicator-nav）提供长周期趋势方法对比回测：

- 指标注册表：直接读取 strategy/trend_strategy_boll_limit_scheduler_like copy 7.py
  的 TREND_INDICATORS（含指标导航页补充的 supertrend/donchian/ema_cross/
  ma_slope/linreg/trix_ma），方向计算复用其 compute_direction_series。
- K 线数据源：优先 copy 7 的 1m CSV（strategyAI/data/{symbol}_ohlcv_robust_1m_
  since_2022.csv，用其 load_symbol_csv/_resample_ohlc 重采样）；CSV 不存在时
  回退 OKX 公共行情接口（ccxt，无需密钥，只读 candles），按请求周期直接拉取。
- 回测引擎：本模块内独立复刻 copy 7 的 BOLL 限价双周期策略（触边即入/触边即出、
  长周期反转亏损剔除、定投/复投/混投资金曲线），输出逐笔记录与全部统计指标；
  不复用其 get_latest_data（该函数打印报表且需要 backtrader 全局态），
  正确性由 _smoke_indicator_nav.py 与 copy 7 引擎做 A/B 对齐验证。

纪律红线（对齐项目「GET 不写库」与限频口径）：
- 本模块零数据库写入、零交易接口触达；OKX 回退只用公共行情（fetch_ohlcv），
  enableRateLimit 限速，且带进程内 TTL 缓存与串行锁，避免连点打爆公共配额。
- copy 7 模块含 GLOBAL_DF_1M 等全局态，所有对其函数的调用都在同一把
  threading.RLock 内完成（对齐「批量接口串行执行规避全局状态竞争」决策）。
"""

import contextlib
import importlib.util
import io
import logging
import math
import os
import re
import threading
import time

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# =============================================================================
# copy 7 策略模块动态加载（文件名带空格，无法常规 import）
# =============================================================================
_STRATEGY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy')
_STRATEGY_FILE = os.path.join(
    _STRATEGY_DIR, 'trend_strategy_boll_limit_scheduler_like copy 7.py')

_v7_lock = threading.RLock()   # 串行保护：copy 7 全局态 + OKX 公共拉取
_v7_mod = None
_v7_mod_mtime = 0.0


def _load_v7_module():
    """按文件路径加载 copy 7 模块；源文件更新（mtime 变化）时自动重载。"""
    global _v7_mod, _v7_mod_mtime
    with _v7_lock:
        try:
            mtime = os.path.getmtime(_STRATEGY_FILE)
        except OSError as e:
            raise RuntimeError(f'找不到策略源文件: {_STRATEGY_FILE} ({e})')
        if _v7_mod is None or mtime != _v7_mod_mtime:
            spec = importlib.util.spec_from_file_location('_v7_strategy_copy7', _STRATEGY_FILE)
            mod = importlib.util.module_from_spec(spec)
            # backtrader 的 metabase 会反查 sys.modules[cls.__module__]（本模块的
            # PandasDataWithLongDirection 等类定义在其中），须先登记再 exec
            import sys as _sys
            _sys.modules[spec.name] = mod
            # 源文件导入期无副作用（main 仅在 __main__ 下执行），可安全 exec
            with contextlib.redirect_stdout(io.StringIO()):
                spec.loader.exec_module(mod)
            _v7_mod = mod
            _v7_mod_mtime = mtime
        return _v7_mod


def list_indicators() -> list:
    """返回注册表键名列表（下拉菜单数据源），加载失败返回空列表。"""
    try:
        v7 = _load_v7_module()
        return list(v7.TREND_INDICATORS.keys())
    except Exception as e:
        logger.warning('[IndicatorNav] 指标注册表加载失败: %s', e)
        return []


# =============================================================================
# 数据源一：copy 7 的 1m CSV（全量历史，重采样出请求周期）
# =============================================================================
_csv_cache = {}          # symbol -> (expire_ts, df_1m)
_CSV_TTL_SEC = 15 * 60
_SYMBOL_RE = re.compile(r'^[A-Za-z0-9]{1,20}$')

SHORT_BAR_OPTIONS = ['5m', '15m', '30m', '1H', '2H', '4H']
LONG_BAR_OPTIONS = ['1H', '2H', '4H', '6H', '12H', '1D']


def _quiet(fn, *args, **kwargs):
    """copy 7 的加载函数会 print 清洗日志，Web 请求里静默收集为备注。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def _get_csv_1m(v7, symbol: str):
    """读 copy 7 口径的 1m CSV（带 TTL 缓存）；文件不存在返回 None。"""
    if not os.path.exists(v7._csv_path_for_symbol(symbol)):
        return None
    now = time.time()
    hit = _csv_cache.get(symbol)
    if hit and hit[0] > now:
        return hit[1]
    df, _notes = _quiet(v7.load_symbol_csv, symbol, 'Y')
    if df is None or len(df) == 0:
        return None
    _csv_cache[symbol] = (now + _CSV_TTL_SEC, df)
    return df


# =============================================================================
# 数据源二：OKX 公共行情回退（CSV 缺失时按请求周期直拉，无需密钥）
# =============================================================================
_okx_cache = {}          # (symbol, bar) -> (expire_ts, df)
_OKX_TTL_SEC = 10 * 60
_OKX_SPAN_DAYS = 120     # 回退拉取跨度：够跑回测又不至于打太多公共请求
_OKX_MAX_BARS = 12000
_OKX_MAX_REQUESTS = 60

_ccxt_exchange = None


def _get_ccxt_okx():
    """惰性建立 OKX 公共实例（复用 kline_fetcher 的 get_exchange 口径）。"""
    global _ccxt_exchange
    if _ccxt_exchange is None:
        from .kline_fetcher.exchange_fetcher import get_exchange
        _ccxt_exchange = get_exchange('okx')
    return _ccxt_exchange


def _ccxt_timeframe(bar: str) -> str:
    s = str(bar).strip()
    return s if s.endswith('m') else s.lower()


def _okx_anchor(now_ms: int, bar_min: int):
    """返回 (want, since_ms)，把 OKX 取数窗口锚定为「结束于 now_ms」。

    want = 实际能拉到的 bar 数 = min(120天所需+300, _OKX_MAX_BARS, 请求数×每页100)；
    since_ms = now_ms - want*step，从 since 向后翻 want 根正好落在当前时间。
    细周期（5m/15m）受请求数上限拉不满 120 天，此时优先保「最新」而非「最长」，
    避免窗口被截断在几十天前（末笔交易停在数周前的根因）。
    """
    step_ms = bar_min * 60 * 1000
    target = min(int(_OKX_SPAN_DAYS * 24 * 60 / bar_min) + 300, _OKX_MAX_BARS)
    want = min(target, _OKX_MAX_REQUESTS * 100)
    return want, now_ms - want * step_ms


def _fetch_okx_frame(symbol: str, bar: str) -> pd.DataFrame:
    """从 OKX 公共接口分页拉取近 _OKX_SPAN_DAYS 天的 K 线，
    返回与 CSV 重采样同口径的 DataFrame（Asia/Shanghai 无时区索引 + ADX 列）。"""
    v7 = _load_v7_module()
    now = _get_ccxt_okx()
    pair = f'{str(symbol).upper()}/USDT:USDT'
    tf = _ccxt_timeframe(bar)
    bar_min = v7._bar_minutes(bar)
    step_ms = bar_min * 60 * 1000
    # 窗口锚定见 _okx_anchor：保证结束于当前时间（细周期优先保最新而非最长）
    want, since = _okx_anchor(now.milliseconds(), bar_min)
    rows, seen, requests = [], set(), 0
    while len(rows) < want and requests < _OKX_MAX_REQUESTS:
        batch = now.fetch_ohlcv(pair, timeframe=tf, since=since, limit=100)
        requests += 1
        if not batch:
            break
        fresh = [r for r in batch if r[0] not in seen]
        if not fresh:
            break
        for r in fresh:
            seen.add(r[0])
        rows.extend(fresh)
        since = batch[-1][0] + step_ms
    if not rows:
        raise RuntimeError(f'OKX 公共行情未返回数据（{pair} {tf}），请检查网络或稍后再试')
    rows.sort(key=lambda r: r[0])
    ts = pd.to_datetime([r[0] for r in rows], unit='ms', utc=True) \
             .tz_convert('Asia/Shanghai').tz_localize(None)
    df = pd.DataFrame({
        'open': [float(r[1]) for r in rows],
        'high': [float(r[2]) for r in rows],
        'low': [float(r[3]) for r in rows],
        'close': [float(r[4]) for r in rows],
    }, index=ts)
    df.index.name = 'timestamp'
    df = df[~df.index.duplicated(keep='first')]
    # 对齐 _resample_ohlc 的产物：补 ADX 列（macd_adx 等方法需要）
    return v7.calculate_adx(df, period=14)


def _get_okx_frame(symbol: str, bar: str) -> pd.DataFrame:
    key = (str(symbol).lower(), str(bar))
    now_ts = time.time()
    hit = _okx_cache.get(key)
    if hit and hit[0] > now_ts:
        return hit[1]
    df = _fetch_okx_frame(symbol, bar)
    _okx_cache[key] = (now_ts + _OKX_TTL_SEC, df)
    return df


def build_frames(symbol: str, short_bar: str, long_bar: str):
    """构造短/长周期 OHLC 帧。返回 (df_short, df_long, source, notes)。

    source='csv'：1m CSV 重采样（与 copy 7 回测完全同源）；
    source='okx'：公共行情直拉（近 120 天，无 CSV 时的回退）。
    """
    v7 = _load_v7_module()
    notes = []
    with _v7_lock:
        df_1m = _get_csv_1m(v7, symbol)
        if df_1m is not None:
            df_short, n1 = _quiet(v7._resample_ohlc, df_1m, short_bar)
            df_long, n2 = _quiet(v7._resample_ohlc, df_1m, long_bar)
            notes.extend(x for x in (n1, n2) if x)
            return df_short, df_long, 'csv', notes
        df_short = _get_okx_frame(symbol, short_bar)
        df_long = _get_okx_frame(symbol, long_bar)
        notes.append('本地无 1m CSV，已回退 OKX 公共行情（近 %d 天）' % _OKX_SPAN_DAYS)
        return df_short, df_long, 'okx', notes


# =============================================================================
# 回测引擎：独立复刻 copy 7 的 BOLL 限价双周期策略
# =============================================================================
def _attach_long_direction(v7, df_short: pd.DataFrame, df_long: pd.DataFrame,
                           indicator: str):
    """长周期方向序列 → 按 copy 7 口径对齐到短周期（merge_asof backward +
    3 根连续同向确认 LONG_CONF），返回 (对齐后的 df_short, dirs_long)。"""
    dirs = v7.compute_direction_series(df_long, indicator)
    dl = df_long.copy()
    dl['LONG_DIRECTION'] = pd.Series(dirs, index=dl.index).astype(float).fillna(0.0)
    conf_window = 3
    vals = dl['LONG_DIRECTION'].values
    conf = np.zeros(len(vals), dtype=float)
    for i in range(len(vals)):
        if i + 1 < conf_window:
            continue
        win = vals[i + 1 - conf_window:i + 1]
        if np.all(win == win[0]) and win[0] != 0.0:
            conf[i] = 1.0
    dl['LONG_CONF'] = conf
    df = df_short.copy()
    short_times = pd.DataFrame({'ts': pd.to_datetime(df.index)})
    long_times = pd.DataFrame({
        'ts': pd.to_datetime(dl.index),
        'LONG_DIRECTION': dl['LONG_DIRECTION'].values,
        'LONG_CONF': dl['LONG_CONF'].values,
    })
    aligned = pd.merge_asof(short_times.sort_values('ts'),
                            long_times.sort_values('ts'),
                            on='ts', direction='backward')
    df['LONG_DIRECTION'] = aligned['LONG_DIRECTION'].values
    df['LONG_CONF'] = aligned['LONG_CONF'].values
    df['LONG_DIRECTION'] = df['LONG_DIRECTION'].fillna(0.0)
    df['LONG_CONF'] = df['LONG_CONF'].fillna(0.0)
    first_valid = df['LONG_DIRECTION'].first_valid_index()
    if first_valid is not None:
        df = df.loc[df.index >= first_valid]
    return df, dirs


def _boll_and_atr(df: pd.DataFrame, period=20, dev=2.0):
    """BOLL(20,2) 与短周期 ATR(Wilder 14)，口径与 copy 7 一致。
    注：backtrader 的 StandardDeviation 用总体方差 E[X²]−E[X]²（ddof=0），
    不是 pandas 默认的样本方差 ddof=1；此处显式 ddof=0 以与引擎 A/B 对齐。"""
    mid = df['close'].rolling(period, min_periods=period).mean()
    std = df['close'].rolling(period, min_periods=period).std(ddof=0)
    top = mid + dev * std
    bot = mid - dev * std
    prev_close = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'],
                    (df['high'] - prev_close).abs(),
                    (df['low'] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
    return mid.values, top.values, bot.values, atr.values


def _long_chart_payload(df_long: pd.DataFrame, dirs, records_long, chart_bars: int):
    """长周期视图图表载荷：K 线用长周期 bar 本身，着色区间=趋势指标在长周期
    数据上直接切出的多空方向段（与 analyze_trend_segments 同口径），并附长周期
    BOLL(20,2) 轨道与长周期回测买卖点。dirs 为与 df_long 等长的方向序列，
    records_long 为长周期回测逐笔（索引对齐 df_long）。后端一次回测同时返回，
    前端切换视图免二次请求。"""
    dirs = np.nan_to_num(np.asarray(dirs, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    n = len(df_long)
    lo_idx = max(0, n - chart_bars)
    times = [t.strftime('%Y-%m-%d %H:%M') for t in df_long.index[lo_idx:]]
    o = df_long['open'].values
    h = df_long['high'].values
    lo_arr = df_long['low'].values
    c = df_long['close'].values
    mid, top, bot, _atr = _boll_and_atr(df_long)

    def _r4(arr):
        return [None if (arr[i] is None or not np.isfinite(arr[i])) else round(float(arr[i]), 6)
                for i in range(lo_idx, n)]

    # 多空方向段：连续同向且非零的长周期 bar 区间（裁剪到图表窗口内）
    zones = []
    i = lo_idx
    while i < n:
        d = dirs[i]
        if d != 0.0:
            j = i
            while j + 1 < n and dirs[j + 1] == d:
                j += 1
            zones.append([i - lo_idx, j - lo_idx, 'rise' if d >= 0.5 else 'fall'])
            i = j + 1
        else:
            i += 1
    # 长周期回测买卖点（与短周期图同构：开=三角、平=pin）
    markers = []
    for r in records_long or []:
        if r['entry_idx'] >= lo_idx:
            markers.append({'idx': r['entry_idx'] - lo_idx, 'time': times[r['entry_idx'] - lo_idx],
                            'price': round(r['open_price'], 6), 'side': 'entry', 'dir': r['dir']})
        if r['exit_idx'] >= lo_idx:
            markers.append({'idx': r['exit_idx'] - lo_idx, 'time': times[r['exit_idx'] - lo_idx],
                            'price': round(r['close_price'], 6), 'side': 'exit', 'dir': r['dir'],
                            'excluded': r['excluded']})
    return {
        'times': times,
        'candles': [[float(o[i]), float(c[i]), float(lo_arr[i]), float(h[i])] for i in range(lo_idx, n)],
        'boll_mid': _r4(mid), 'boll_top': _r4(top), 'boll_bot': _r4(bot),
        'long_dir': [float(dirs[i]) for i in range(lo_idx, n)],
        'markers': markers, 'zones': zones, 'offset': lo_idx,
    }


def _segment_records(df_long: pd.DataFrame, dirs, leverage_times=10.0):
    """长周期视图的回测记录：把趋势指标在长周期上切出的每个多空方向段视为一笔
    趋势跟随交易（段首开盘入场 → 段尾收盘出场），口径与 analyze_trend_segments
    的方向段一致。返回与 _run_backtest 同 schema 的 records（供统计/资金曲线复用）。
    excluded 恒为 False（方向段自身即信号，无反转剔除概念）。"""
    dirs = np.nan_to_num(np.asarray(dirs, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    o = df_long['open'].values
    h = df_long['high'].values
    l = df_long['low'].values
    c = df_long['close'].values
    times = df_long.index.to_pydatetime()
    n = len(dirs)
    lev = float(leverage_times)
    records = []
    i = 0
    while i < n:
        d = dirs[i]
        if d == 0.0:
            i += 1
            continue
        j = i
        while j + 1 < n and dirs[j + 1] == d:
            j += 1
        base = float(o[i])
        exitp = float(c[j])
        seg_high = float(np.max(h[i:j + 1]))
        seg_low = float(np.min(l[i:j + 1]))
        if base > 0:
            if d >= 0.5:
                profit = (exitp / base - 1) * 100 * lev
                max_win = (seg_high / base - 1) * 100 * lev
                max_loss = (seg_low / base - 1) * 100 * lev
                rec_dir = 'rise'
            else:
                profit = (1 - exitp / base) * 100 * lev
                max_win = (1 - seg_low / base) * 100 * lev
                max_loss = (1 - seg_high / base) * 100 * lev
                rec_dir = 'fall'
            records.append({
                'dir': rec_dir, 'open_price': base, 'close_price': exitp,
                'entry_time': times[i], 'exit_time': times[j],
                'entry_idx': i, 'exit_idx': j,
                'profit': float(profit), 'max_win': float(max_win),
                'max_loss': float(max_loss), 'excluded': False,
            })
        i = j + 1
    return records


def _run_backtest(df: pd.DataFrame, leverage_times=10.0,
                  entry_atr_mult=0.0, exit_atr_mult=0.0):
    """逐 bar 复刻 copy 7 DualTimeframeBollLimitStrategy.next() 的完整语义：
    触边即入/触边即出、同 bar 平仓后可再开、长周期反转期间的亏损单剔除。
    返回 (trade_records, zones, bars)。"""
    mid, top, bot, atr = _boll_and_atr(df)
    longs = df['LONG_DIRECTION'].values
    highs = df['high'].values
    lows = df['low'].values
    times = df.index.to_pydatetime()
    n = len(df)

    pos_size = 0.0
    entry_price = None
    entry_time = None
    entry_idx = None
    entry_long_dir = None
    position_flip = False
    max_price = None
    min_price = None
    records = []
    zones = []      # 持仓区间 [start_idx, end_idx, dir]（含未平仓的进行中持仓）

    for i in range(n):
        long_dir = None
        v = longs[i]
        if np.isfinite(v):
            if v >= 0.5:
                long_dir = 'rise'
            elif v <= -0.5:
                long_dir = 'fall'
        if any(not np.isfinite(x) for x in (top[i], bot[i], mid[i])):
            continue
        if long_dir is None:
            continue
        low, high = float(lows[i]), float(highs[i])
        now_time = times[i]
        if pos_size != 0 and entry_price is not None:
            if max_price is None or high > max_price:
                max_price = high
            if min_price is None or low < min_price:
                min_price = low
            if entry_long_dir and long_dir != entry_long_dir:
                position_flip = True
        atr_now = float(atr[i]) if np.isfinite(atr[i]) else 0.0
        long_entry_price = bot[i] - entry_atr_mult * atr_now
        short_entry_price = top[i] + entry_atr_mult * atr_now
        long_exit_price = top[i] + exit_atr_mult * atr_now
        short_exit_price = bot[i] - exit_atr_mult * atr_now
        touch_long_entry = low <= long_entry_price <= high
        touch_short_entry = low <= short_entry_price <= high
        touch_long_exit = low <= long_exit_price <= high
        touch_short_exit = low <= short_exit_price <= high
        if pos_size == 0:
            if long_dir == 'rise' and touch_long_entry:
                pos_size = 1.0
                entry_price = float(long_entry_price)
                entry_time = now_time
                entry_idx = i
                entry_long_dir = long_dir
                position_flip = False
                max_price = min_price = entry_price
            elif long_dir == 'fall' and touch_short_entry:
                pos_size = -1.0
                entry_price = float(short_entry_price)
                entry_time = now_time
                entry_idx = i
                entry_long_dir = long_dir
                position_flip = False
                max_price = min_price = entry_price
        elif pos_size > 0 and touch_long_exit:
            exit_price = float(long_exit_price)
            profit = (exit_price / entry_price - 1) * 100 * leverage_times
            max_win = (max_price / entry_price - 1) * 100 * leverage_times if max_price else 0.0
            max_loss = (min_price / entry_price - 1) * 100 * leverage_times if min_price else 0.0
            excluded = position_flip and profit < 0
            records.append({
                'dir': 'rise', 'open_price': entry_price, 'close_price': exit_price,
                'entry_time': entry_time, 'exit_time': now_time,
                'entry_idx': entry_idx, 'exit_idx': i,
                'profit': float(profit), 'max_win': float(max_win),
                'max_loss': float(max_loss), 'excluded': bool(excluded),
            })
            zones.append([entry_idx, i, 'rise'])
            pos_size = 0.0
            entry_price = None
            entry_long_dir = None
            position_flip = False
            max_price = min_price = None
        elif pos_size < 0 and touch_short_exit:
            exit_price = float(short_exit_price)
            profit = (1 - (exit_price / entry_price)) * 100 * leverage_times
            max_win = (1 - (min_price / entry_price)) * 100 * leverage_times if min_price else 0.0
            max_loss = (1 - (max_price / entry_price)) * 100 * leverage_times if max_price else 0.0
            excluded = position_flip and profit < 0
            records.append({
                'dir': 'fall', 'open_price': entry_price, 'close_price': exit_price,
                'entry_time': entry_time, 'exit_time': now_time,
                'entry_idx': entry_idx, 'exit_idx': i,
                'profit': float(profit), 'max_win': float(max_win),
                'max_loss': float(max_loss), 'excluded': bool(excluded),
            })
            zones.append([entry_idx, i, 'fall'])
            pos_size = 0.0
            entry_price = None
            entry_long_dir = None
            position_flip = False
            max_price = min_price = None
    if pos_size != 0 and entry_idx is not None:
        zones.append([entry_idx, n - 1, 'rise' if pos_size > 0 else 'fall'])
    return records, zones


def _money_curves(records, base_money=100.0):
    """定投/复投/混投/真实定投四条资金曲线，公式与 copy 7 完全一致
    （混投基数按 1.6 倍阶梯放大；真实定投把剔除单亏损也计入）。"""
    fix_h = [base_money]
    contract_h = [base_money]
    mix_h = [base_money]
    fix_real_h = [base_money]
    for rec in records:
        profit = float(rec['profit'])
        if rec['excluded']:
            fix_h.append(fix_h[-1])
            contract_h.append(contract_h[-1])
            mix_h.append(mix_h[-1])
        else:
            fix_h.append(fix_h[-1] + base_money * profit * 0.01)
            contract_h.append(contract_h[-1] + contract_h[-1] * profit * 0.01)
            mix_before = mix_h[-1]
            ratio = mix_before / base_money
            if ratio > 0:
                try:
                    log_value = math.log(ratio) / math.log(1.6)
                    if not math.isnan(log_value) and not math.isinf(log_value):
                        base = max(base_money * pow(1.6, math.floor(log_value)), base_money)
                    else:
                        base = base_money
                except (ValueError, OverflowError):
                    base = base_money
            else:
                base = base_money
            mix_h.append(mix_before + base * profit * 0.01)
        fix_real_h.append(fix_real_h[-1] + base_money * profit * 0.01)
    return fix_h, contract_h, mix_h, fix_real_h


def _max_drawdown(history):
    peak = history[0] if history else 0.0
    mdd = 0.0
    for value in history:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak * 100
            if dd > mdd:
                mdd = dd
    return mdd


def _build_stats(records, fix_h, contract_h, mix_h, fix_real_h, bar_min):
    """统计口径逐项对齐 copy 7 的 PRINT_ORIGINAL_OUTPUT 报表。"""
    profits = [r['profit'] for r in records if not r['excluded']]
    max_wins = [r['max_win'] for r in records if not r['excluded']]
    max_losses = [r['max_loss'] for r in records if not r['excluded']]
    total_trades = len(profits)
    wins = sum(1 for p in profits if p > 0)
    losses = sum(1 for p in profits if p < 0)
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    total_profit = sum(p for p in profits if p > 0)
    total_loss = sum(p for p in profits if p < 0)
    avg_profit = total_profit / wins if wins > 0 else 0.0
    avg_loss = total_loss / losses if losses > 0 else 0.0
    excluded_trades = sum(1 for r in records if r['excluded'])
    excluded_total_loss = sum(r['profit'] for r in records if r['excluded'])
    excluded_avg_loss = excluded_total_loss / excluded_trades if excluded_trades else 0.0
    all_closed = total_trades + excluded_trades
    win_idx = [i for i, p in enumerate(profits) if p > 0]
    loss_idx = [i for i, p in enumerate(profits) if p < 0]
    hold_total_max_profit = sum(max_wins[i] for i in win_idx) if win_idx else 0.0
    hold_total_max_loss = sum(max_losses[i] for i in loss_idx) if loss_idx else 0.0
    avg_hold_max_profit = hold_total_max_profit / len(win_idx) if win_idx else 0.0
    avg_hold_max_loss = hold_total_max_loss / len(loss_idx) if loss_idx else 0.0
    # 平均持仓时长（短周期 bar 数 × bar 分钟 → 小时）
    hold_hours = [(r['exit_idx'] - r['entry_idx']) * bar_min / 60.0
                  for r in records if not r['excluded']]
    avg_hold_hours = float(np.mean(hold_hours)) if hold_hours else 0.0
    start_time = end_time = None
    included = [(r['entry_time'], r['exit_time']) for r in records if not r['excluded']]
    if included:
        start_time = min(t[0] for t in included if t[0] is not None)
        end_time = max(t[1] for t in included if t[1] is not None)
    total_days = 0
    daily_profit = weekly_profit = 0.0
    fix_money = fix_h[-1] if fix_h else 100.0
    if start_time is not None and end_time is not None:
        total_days = (end_time - start_time).days
        if total_days > 0:
            total_return = (fix_money - 100.0) / 100.0 * 100
            daily_profit = total_return / total_days
            weekly_profit = daily_profit * 7
    return {
        'total_trades': total_trades,
        'winning_trades': wins,
        'losing_trades': losses,
        'win_rate': win_rate,
        'excluded_trades': excluded_trades,
        'excluded_ratio': excluded_trades / all_closed if all_closed > 0 else 0.0,
        'excluded_total_loss': excluded_total_loss,
        'excluded_avg_loss': excluded_avg_loss,
        'total_profit': total_profit,
        'total_loss': total_loss,
        'avg_profit': avg_profit,
        'avg_loss': avg_loss,
        'final_mean_pl_ratio': abs(avg_profit) / abs(avg_loss) if avg_loss != 0 else 0.0,
        'final_sum_pl_ratio': abs(total_profit) / abs(total_loss) if total_loss != 0 else 0.0,
        'hold_total_max_profit': hold_total_max_profit,
        'hold_total_max_loss': hold_total_max_loss,
        'avg_hold_max_profit': avg_hold_max_profit,
        'avg_hold_max_loss': avg_hold_max_loss,
        'hold_pl_ratio': abs(avg_hold_max_profit / avg_hold_max_loss) if avg_hold_max_loss != 0 else 0.0,
        'actual_profit_rate': total_profit / hold_total_max_profit if hold_total_max_profit > 0 else 0.0,
        'actual_loss_rate': abs(total_loss) / abs(hold_total_max_loss) if hold_total_max_loss < 0 else 0.0,
        'fix_money': fix_money,
        'contract_money': contract_h[-1] if contract_h else 100.0,
        'mix_money': mix_h[-1] if mix_h else 100.0,
        'fix_real_money': fix_real_h[-1] if fix_real_h else 100.0,
        'fix_drawdown': _max_drawdown(fix_h),
        'contract_drawdown': _max_drawdown(contract_h),
        'mix_drawdown': _max_drawdown(mix_h),
        'profit_factor': abs(total_profit / total_loss) if total_loss < 0 else 0.0,
        'start_time': start_time.strftime('%Y-%m-%d %H:%M:%S') if start_time is not None else None,
        'end_time': end_time.strftime('%Y-%m-%d %H:%M:%S') if end_time is not None else None,
        'total_days': total_days,
        'daily_profit': daily_profit,
        'weekly_profit': weekly_profit,
        'avg_hold_hours': avg_hold_hours,
    }


# =============================================================================
# 对外主入口
# =============================================================================
def _build_trades(records, fix_h, contract_h, mix_h, bar_min):
    """逐笔交易 → 前端可渲染列表（短/长周期共用）；hold_hours 按当前周期 bar 分钟换算。"""
    trades = []
    for k, r in enumerate(records):
        trades.append({
            'no': k + 1, 'dir': r['dir'],
            'open_price': round(r['open_price'], 6), 'close_price': round(r['close_price'], 6),
            'entry_time': pd.Timestamp(r['entry_time']).strftime('%Y-%m-%d %H:%M'),
            'exit_time': pd.Timestamp(r['exit_time']).strftime('%Y-%m-%d %H:%M'),
            'profit': round(r['profit'], 2), 'max_win': round(r['max_win'], 2),
            'max_loss': round(r['max_loss'], 2), 'excluded': r['excluded'],
            'hold_hours': round((r['exit_idx'] - r['entry_idx']) * bar_min / 60.0, 1),
            'fix_after': round(fix_h[k + 1], 2), 'contract_after': round(contract_h[k + 1], 2),
            'mix_after': round(mix_h[k + 1], 2),
            'entry_idx': r['entry_idx'], 'exit_idx': r['exit_idx'],
        })
    return trades


def _build_money_curves(records, fix_h, contract_h, mix_h, fix_real_h):
    """资金曲线 → 前端载荷（短/长周期共用）。"""
    return {
        'times': [pd.Timestamp(r['exit_time']).strftime('%Y-%m-%d %H:%M') for r in records],
        'fix': [round(v, 2) for v in fix_h[1:]],
        'contract': [round(v, 2) for v in contract_h[1:]],
        'mix': [round(v, 2) for v in mix_h[1:]],
        'fix_real': [round(v, 2) for v in fix_real_h[1:]],
    }


_CHART_MAX_BARS = 2000      # 图表最多回传最近多少根 bar（全量统计不受影响）
_MIN_BARS = 120             # 短周期少于该根数直接报错（指标预热都不够）

_compare_cache = {}         # (symbol, long_bar) -> (expire_ts, rows)
_COMPARE_TTL_SEC = 15 * 60


def compare_indicators(symbol='near', long_bar='4H'):
    """对注册表全部方法跑一遍趋势段统计（analyze_trend_segments 口径），
    按日均反转升序返回；结果按 (symbol, long_bar) 缓存，避免频繁重算。"""
    symbol = str(symbol or 'near').strip().lower()
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f'非法币种: {symbol}')
    long_bar = str(long_bar or '4H').strip()
    if long_bar not in LONG_BAR_OPTIONS:
        raise ValueError(f'长周期须为 {LONG_BAR_OPTIONS}')
    key = (symbol, long_bar)
    now_ts = time.time()
    hit = _compare_cache.get(key)
    if hit and hit[0] > now_ts:
        return hit[1]
    with _v7_lock:
        v7 = _load_v7_module()
        notes = []
        df_1m = _get_csv_1m(v7, symbol)
        if df_1m is not None:
            df_long, n2 = _quiet(v7._resample_ohlc, df_1m, long_bar)
            notes.extend(x for x in (n2,) if x)
            source = 'csv'
        else:
            df_long = _get_okx_frame(symbol, long_bar)
            source = 'okx'
        if df_long is None or len(df_long) < 30:
            raise RuntimeError(f'{symbol.upper()} {long_bar} 长周期数据不足')
        rows = []
        for ind in v7.TREND_INDICATORS.keys():
            try:
                seg = v7.analyze_trend_segments(df_long, ind, long_bar)
                seg.pop('total_days', None)
                rows.append(seg)
            except Exception as e:
                rows.append({'indicator': ind, 'error': f'{type(e).__name__}: {e}'})
    rows.sort(key=lambda r: r.get('reversals_per_day', 1e9))
    payload = {'rows': rows, 'source': source, 'long_bars': int(len(df_long))}
    _compare_cache[key] = (now_ts + _COMPARE_TTL_SEC, payload)
    return payload


def run_backtest(symbol='near', short_bar='15m', long_bar='4H', indicator='keltner',
                 leverage_times=10.0, entry_atr_mult=0.0, exit_atr_mult=0.0,
                 chart_bars=_CHART_MAX_BARS, view='short'):
    """执行一次完整回测，返回可直接 jsonify 的 dict。参数非法抛 ValueError，
    数据/依赖故障抛 RuntimeError（路由层分别映射 400/502）。"""
    symbol = str(symbol or 'near').strip().lower()
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f'非法币种: {symbol}')
    short_bar = str(short_bar or '15m').strip()
    long_bar = str(long_bar or '4H').strip()
    if short_bar not in SHORT_BAR_OPTIONS:
        raise ValueError(f'短周期须为 {SHORT_BAR_OPTIONS}')
    if long_bar not in LONG_BAR_OPTIONS:
        raise ValueError(f'长周期须为 {LONG_BAR_OPTIONS}')
    indicator = str(indicator or 'keltner').strip().lower()
    known = list_indicators()
    if indicator not in known:
        raise ValueError(f"未知趋势指标 '{indicator}'，可选: {known}")
    if str(view or 'short').strip().lower() not in ('short', 'long'):
        raise ValueError("视图仅支持 short / long")
    leverage_times = max(0.1, min(float(leverage_times), 125.0))
    entry_atr_mult = max(0.0, min(float(entry_atr_mult), 10.0))
    exit_atr_mult = max(0.0, min(float(exit_atr_mult), 10.0))
    chart_bars = max(200, min(int(chart_bars), 6000))

    t0 = time.time()
    with _v7_lock:
        v7 = _load_v7_module()
        df_short, df_long, source, notes = build_frames(symbol, short_bar, long_bar)
        if df_short is None or len(df_short) < _MIN_BARS:
            raise RuntimeError(f'{symbol.upper()} {short_bar} 数据不足（{0 if df_short is None else len(df_short)} 根）')
        if df_long is None or len(df_long) < 30:
            raise RuntimeError(f'{symbol.upper()} {long_bar} 长周期数据不足')
        df, dirs_long = _attach_long_direction(v7, df_short, df_long, indicator)
        if len(df) < _MIN_BARS:
            raise RuntimeError('长周期方向对齐后短周期数据不足，无法回测')
        bar_min = v7._bar_minutes(short_bar)
        records, zones = _run_backtest(
            df, leverage_times=leverage_times,
            entry_atr_mult=entry_atr_mult, exit_atr_mult=exit_atr_mult)
        segments = v7.analyze_trend_segments(df_long, indicator, long_bar)
        # ---- 长周期视图回测：每个趋势多空方向段视为一笔趋势跟随交易 ----
        # 口径直接来自指标在长周期上切出的方向段（与 analyze_trend_segments 一致），
        # 不沿用短周期交易记录；统计/资金曲线/逐笔均为长周期趋势口径
        bar_min_long = v7._bar_minutes(long_bar)
        records_long = _segment_records(df_long, dirs_long, leverage_times=leverage_times)
        chart_long = _long_chart_payload(df_long, dirs_long, records_long, chart_bars)
    fix_h, contract_h, mix_h, fix_real_h = _money_curves(records)
    stats = _build_stats(records, fix_h, contract_h, mix_h, fix_real_h, bar_min)
    fix_l, contract_l, mix_l, fix_real_l = _money_curves(records_long)
    stats_long = _build_stats(records_long, fix_l, contract_l, mix_l, fix_real_l, bar_min_long)

    # ---- 图表载荷：最近 chart_bars 根 ----
    mid, top, bot, _atr = _boll_and_atr(df)
    n = len(df)
    lo_idx = max(0, n - chart_bars)
    times = [t.strftime('%Y-%m-%d %H:%M') for t in df.index[lo_idx:]]
    o = df['open'].values
    h = df['high'].values
    lo_arr = df['low'].values
    c = df['close'].values
    ld = df['LONG_DIRECTION'].values

    def _r4(arr):
        return [None if (arr[i] is None or not np.isfinite(arr[i])) else round(float(arr[i]), 6)
                for i in range(lo_idx, n)]

    candles = [[float(o[i]), float(c[i]), float(lo_arr[i]), float(h[i])] for i in range(lo_idx, n)]
    markers = []
    for r in records:
        if r['entry_idx'] >= lo_idx:
            markers.append({'idx': r['entry_idx'] - lo_idx, 'time': times[r['entry_idx'] - lo_idx],
                            'price': round(r['open_price'], 6), 'side': 'entry', 'dir': r['dir']})
        if r['exit_idx'] >= lo_idx:
            markers.append({'idx': r['exit_idx'] - lo_idx, 'time': times[r['exit_idx'] - lo_idx],
                            'price': round(r['close_price'], 6), 'side': 'exit', 'dir': r['dir'],
                            'excluded': r['excluded']})
    chart_zones = [[max(z[0] - lo_idx, 0), z[1] - lo_idx, z[2]]
                   for z in zones if z[1] >= lo_idx]

    trades = _build_trades(records, fix_h, contract_h, mix_h, bar_min)
    trades_long = _build_trades(records_long, fix_l, contract_l, mix_l, bar_min_long)

    return {
        'params': {'symbol': symbol, 'short_bar': short_bar, 'long_bar': long_bar,
                   'indicator': indicator, 'leverage_times': leverage_times,
                   'entry_atr_mult': entry_atr_mult, 'exit_atr_mult': exit_atr_mult,
                   'view': str(view or 'short').strip().lower()},
        'source': source, 'notes': notes,
        'indicators': known,
        'short_bar_options': SHORT_BAR_OPTIONS, 'long_bar_options': LONG_BAR_OPTIONS,
        'data_range': {'start': times[0] if times else None, 'end': times[-1] if times else None,
                       'total_bars': n, 'chart_bars': n - lo_idx,
                       'long_bars': int(len(df_long))},
        'chart': {
            'times': times, 'candles': candles,
            'boll_mid': _r4(mid), 'boll_top': _r4(top), 'boll_bot': _r4(bot),
            'long_dir': [float(ld[i]) for i in range(lo_idx, n)],
            'markers': markers, 'zones': chart_zones, 'offset': lo_idx,
        },
        # 长周期视图：一次回测同帧返回，前端切换无需再请求
        'chart_long': chart_long,
        'money_curves': _build_money_curves(records, fix_h, contract_h, mix_h, fix_real_h),
        # 长周期口径的交易统计/资金曲线/逐笔明细（视图切至长周期时展示这套）
        'stats_long': stats_long,
        'money_curves_long': _build_money_curves(records_long, fix_l, contract_l, mix_l, fix_real_l),
        'trades_long': trades_long,
        'trades': trades,
        'stats': stats,
        'segments': segments,
        'elapsed_ms': int((time.time() - t0) * 1000),
    }
