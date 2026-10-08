#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
回测信号引擎 — 历史数据加载 + 引擎逐bar注解 + CSV缓存
====================================================

只读复用 crypto/strategy/pro3_dualtimeframe.py 的信号逻辑，保证回测信号与
实盘 100% 同源；本模块只负责：

1. 扩展分页拉取更长的历史 K 线（OKX get_mark_price_candlesticks，after 分页）
2. 复用引擎函数生成带 MODIFY_FLAG / LONG_DIRECTION / ADX 的短周期 DataFrame
3. 派生逐bar的因果字段（短周期方向序列、长周期"已用方向"、BOLL 上中下轨、
   反转 bar open/close、long_dir_changed），供 batch_sim 逐bar模拟
4. CSV 缓存，避免重复请求交易所

关键因果约定（避免未来函数 look-ahead）：
- 站在短周期 bar i（已收线）：可用信息 = 截至 bar i 的一切
  short_dir      = MODIFY_FLAG[i]
  short_prev     = MODIFY_FLAG[i-1]
  short_prev_prev= MODIFY_FLAG[i-2]
  long_dir_used  = 截至 bar i 时间，长周期"上一根已完成bar"的 LONG_DIRECTION
                   （复刻 trend_range_trader 用 df_long.LONG_DIRECTION.dropna().iloc[-2] 的稳健做法）
  boll_*         = closes[:i+1].rolling(boll_period) 在 i 处的值
  reversal_*     = open[i-1] / close[i-1]
"""

import os
import sys
import math

import numpy as np
import pandas as pd

# --- 只读复用策略引擎（不修改运行中的文件）---
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CRYPTO_DIR = os.path.dirname(_THIS_DIR)
_STRATEGY_DIR = os.path.join(_CRYPTO_DIR, 'strategy')
for _d in (_STRATEGY_DIR, _CRYPTO_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import pro3_singletimeframe as _single       # noqa: E402
import pro3_dualtimeframe as _dual            # noqa: E402

# 历史 K 线缓存目录：crypto/backtest/data_cache/
_CACHE_DIR = os.path.join(_THIS_DIR, 'data_cache')
os.makedirs(_CACHE_DIR, exist_ok=True)


# =====================================================================
# 1) 扩展历史 K 线拉取（分页）
# =====================================================================

def _find_fallback_cache(inst_id: str, bar: str, exclude: str = None):
    """网络不可用时，在 data_cache 里找同币种同周期、任意页数的旧缓存。

    返回 (df, 文件路径) 或 None；多个候选时选行数最多的一个。
    """
    import glob as _glob
    best = None
    for path in _glob.glob(os.path.join(_CACHE_DIR, f"{inst_id}_{bar}_*p.csv")):
        if exclude and os.path.normcase(path) == os.path.normcase(exclude):
            continue
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            if len(df) > 0 and (best is None or len(df) > len(best[0])):
                best = (df.astype(float), path)
        except Exception:
            continue
    return best

def fetch_klines(inst_id: str, bar: str, pages: int = 12,
                 use_cache: bool = True, flag: str = '0') -> pd.DataFrame:
    """分页拉取标记价格 K 线（较 _fetch_kline_data 的固定 5 页扩展至 pages 页）。

    OKX get_mark_price_candlesticks 每页最多 100 根，newest-first；用 after=最老时间戳
    向更早翻页。返回 index=timestamp(升序, +8h)、列 [open,high,low,close,confirm] 的 df。

    网络容错：复用实盘 pro3_singletimeframe._request_candles（显式超时 + 自动重试 +
    每次重试重建客户端），避免弱网/代理环境下偶发 ConnectTimeout 直接中断回测；
    首页失败重试后仍失败才抛出，历史翻页单页失败则降级返回已获取部分。
    首页也失败时，回退到同币种同周期的其它页数缓存文件（如 30p 未命中但存在
    20p 缓存），确保断网环境下仍可用历史数据完成回测。
    """
    cache_file = os.path.join(_CACHE_DIR, f"{inst_id}_{bar}_{pages}p.csv")
    if use_cache and os.path.exists(cache_file):
        try:
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            if len(df) > 0:
                return df.astype(float)
        except Exception:
            pass

    # 首页（最近K线）：失败会自动重试，仍失败则回退其它页数的旧缓存
    try:
        first = _single._request_candles(inst_id, bar)
    except Exception as e:
        fallback = _find_fallback_cache(inst_id, bar, exclude=cache_file)
        if fallback is not None:
            fb_df, fb_path = fallback
            print(f"[回测][K线] {inst_id} {bar} 网络获取失败（{type(e).__name__}），"
                  f"回退使用旧缓存 {os.path.basename(fb_path)}（{len(fb_df)} 根，"
                  f"截至 {fb_df.index[-1]}）；如需最新数据请检查网络/代理")
            return fb_df
        raise
    if not first:
        raise RuntimeError(f"API 返回空数据: instId={inst_id}, bar={bar}")
    combined = list(first)
    after_ts = first[-1][0]
    for page_no in range(max(0, pages - 1)):
        try:
            page = _single._request_candles(inst_id, bar, after=after_ts)
        except Exception as e:
            # 历史翻页降级：单页重试仍失败时保留已获取数据，不整体丢弃
            print(f"[回测][K线] {inst_id} {bar} 第{page_no + 2}页获取失败，"
                  f"降级使用已获取的 {len(combined)} 根 {type(e).__name__}: {e!r}")
            break
        if not page:
            break
        combined.extend(page)
        after_ts = page[-1][0]

    df = pd.DataFrame(combined, columns=['timestamp', 'open', 'high', 'low', 'close', 'confirm'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype('int64') / 1000, unit='s')
    df = df.drop_duplicates(subset='timestamp')
    df = df.iloc[::-1]                       # 升序（旧→新）
    df['timestamp'] = df['timestamp'] + pd.Timedelta(hours=8)
    df.set_index('timestamp', inplace=True)
    df = df.astype(float).sort_index()

    if use_cache:
        try:
            df.to_csv(cache_file)
        except Exception:
            pass
    return df


# =====================================================================
# 2) 引擎注解：生成带 MODIFY_FLAG / LONG_DIRECTION / ADX 的短周期 df
# =====================================================================

def _annotate_dual(inst_id: str, short_bar: str, long_bar: str,
                   short_pages: int, long_pages: int, use_cache: bool):
    """复刻 _prepare_dual_data，但使用扩展分页历史。返回 (df_short, df_long)。

    步骤与引擎完全一致：calculate_adx → _fill_long_direction_series →
    _align_long_direction_to_short → 截断 → _init_df_columns → _run_dual_strategy。
    _run_dual_strategy 会把 MODIFY_FLAG/MACD 等列逐bar写回 df_short。
    """
    df_short = fetch_klines(inst_id, short_bar, pages=short_pages, use_cache=use_cache)
    df_long = fetch_klines(inst_id, long_bar, pages=long_pages, use_cache=use_cache)

    df_short = _single.calculate_adx(df_short, period=14)
    df_long = _single.calculate_adx(df_long, period=14)

    # 长周期方向序列（justice_flag 状态机）
    _dual._fill_long_direction_series(df_long)

    # 对齐长周期方向到短周期（merge_asof backward）
    _single._align_long_direction_to_short(df_short, df_long)

    # 截断没有长周期方向的短周期数据
    if 'LONG_DIRECTION' in df_short.columns:
        first_valid = df_short['LONG_DIRECTION'].first_valid_index()
        if first_valid is not None:
            df_short = df_short.loc[df_short.index >= first_valid]

    _single._init_df_columns(df_short)

    # 抑制引擎打印
    old = (_single.FAST_MODE, _single.PRINT_ORIGINAL_OUTPUT, _single.PRINT_MARKET,
           _single.PRINT_TRADE_OPS, _single.PRINT_TRADE_RECORDS)
    _single.FAST_MODE = True
    _single.PRINT_ORIGINAL_OUTPUT = 0
    _single.PRINT_MARKET = 0
    _single.PRINT_TRADE_OPS = 0
    _single.PRINT_TRADE_RECORDS = 0
    old_d = (_dual.FAST_MODE, _dual.PRINT_ORIGINAL_OUTPUT, _dual.PRINT_MARKET,
             _dual.PRINT_TRADE_OPS, _dual.PRINT_TRADE_RECORDS)
    _dual.FAST_MODE = True
    _dual.PRINT_ORIGINAL_OUTPUT = 0
    _dual.PRINT_MARKET = 0
    _dual.PRINT_TRADE_OPS = 0
    _dual.PRINT_TRADE_RECORDS = 0
    try:
        _dual._run_dual_strategy(df_short, long_direction=None)
    finally:
        (_single.FAST_MODE, _single.PRINT_ORIGINAL_OUTPUT, _single.PRINT_MARKET,
         _single.PRINT_TRADE_OPS, _single.PRINT_TRADE_RECORDS) = old
        (_dual.FAST_MODE, _dual.PRINT_ORIGINAL_OUTPUT, _dual.PRINT_MARKET,
         _dual.PRINT_TRADE_OPS, _dual.PRINT_TRADE_RECORDS) = old_d

    return df_short, df_long


# =====================================================================
# 3) 派生逐bar因果字段
# =====================================================================

def _flag_to_dir(flag):
    if flag == 'rise':
        return 'long'
    if flag == 'fall':
        return 'short'
    return None


def _long_dir_used_series(df_short: pd.DataFrame, df_long: pd.DataFrame) -> list:
    """为每个短周期 bar 计算"实盘会采用的长周期方向"。

    复刻 trend_range_trader：long_direction = df_long.LONG_DIRECTION.dropna().iloc[-2]
    （用上一根已完成的长周期 bar，避免采用当前正在形成的长周期 bar，抗震荡）。
    历史化实现：对短bar i（时间 t_i），取所有 open_time <= t_i 的长bar中
    倒数第二根有效方向。
    """
    long_valid = df_long['LONG_DIRECTION'].dropna()
    lv_times = long_valid.index.values
    lv_dirs = [_flag_to_dir(x) for x in long_valid.values]
    n_long = len(lv_dirs)

    used = []
    st = df_short.index.values
    j = -1  # 指向 <= t_i 的最后一根长bar
    for i in range(len(df_short)):
        t = st[i]
        while j + 1 < n_long and lv_times[j + 1] <= t:
            j += 1
        # 用倒数第二根（j-1）已完成的长bar方向，抗震荡；不足则退化为 j
        if j >= 1:
            used.append(lv_dirs[j - 1])
        elif j == 0:
            used.append(lv_dirs[0])
        else:
            used.append(None)
    return used


def build_dataset(inst_id: str, short_bar: str, long_bar: str,
                  boll_period: int = 20, boll_dev: float = 2.0,
                  short_pages: int = 12, long_pages: int = 12,
                  use_cache: bool = True) -> pd.DataFrame:
    """产出可直接逐bar回测的注解数据集。

    返回 DataFrame（index=时间，升序），列包含：
      open, high, low, close, ADX,
      short_dir, short_prev, short_prev_prev  (long/short/None)
      long_dir, long_dir_changed
      boll_upper, boll_middle, boll_lower
      rev_open, rev_close   (上一bar的 open/close)
    """
    df_short, df_long = _annotate_dual(
        inst_id, short_bar, long_bar, short_pages, long_pages, use_cache)

    closes = df_short['close'].astype(float)
    mid = closes.rolling(boll_period).mean()
    std = closes.rolling(boll_period).std(ddof=0)
    boll_upper = mid + boll_dev * std
    boll_lower = mid - boll_dev * std

    mf = df_short['MODIFY_FLAG'] if 'MODIFY_FLAG' in df_short.columns else pd.Series(index=df_short.index)
    short_dir = [_flag_to_dir(x) for x in mf.values]

    long_used = _long_dir_used_series(df_short, df_long)

    out = pd.DataFrame(index=df_short.index)
    out['open'] = df_short['open'].astype(float).values
    out['high'] = df_short['high'].astype(float).values
    out['low'] = df_short['low'].astype(float).values
    out['close'] = closes.values
    out['ADX'] = df_short['ADX'].astype(float).values if 'ADX' in df_short.columns else 0.0

    out['short_dir'] = short_dir
    out['short_prev'] = [None] + short_dir[:-1]
    out['short_prev_prev'] = [None, None] + short_dir[:-2]

    out['long_dir'] = long_used
    out['long_dir_changed'] = [False] + [
        (long_used[i] is not None and long_used[i - 1] is not None
         and long_used[i] != long_used[i - 1])
        for i in range(1, len(long_used))
    ]

    out['boll_upper'] = boll_upper.values
    out['boll_middle'] = mid.values
    out['boll_lower'] = boll_lower.values
    out['rev_open'] = out['open'].shift(1)
    out['rev_close'] = out['close'].shift(1)

    # 丢弃布林带/长周期未就绪的前段
    out = out.dropna(subset=['boll_upper', 'long_dir'])
    return out


if __name__ == '__main__':
    # 自检：打印数据集规模与样例
    df = build_dataset('NEAR-USDT-SWAP', '1H', '1D', short_pages=6, long_pages=6)
    print(f"数据集: {len(df)} 行  {df.index[0]} → {df.index[-1]}")
    print(df[['close', 'ADX', 'short_dir', 'long_dir', 'boll_upper', 'boll_lower']].tail(8))
