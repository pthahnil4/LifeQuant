#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/providers/base.py  →  现位置: crypto/kline_training/providers/base.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
品类无关行情数据层 —— 基类与公共工具
====================================
定义 ``MarketDataProvider`` 接口与三品类共用的时间归一化 / 重采样 / OHLCV
标准化工具。

统一约定
--------
所有 provider 的 ``fetch_ohlcv`` 返回**标准化 DataFrame**：
    列 = ['ts', 'open', 'high', 'low', 'close', 'volume']，按 ``ts`` 升序。

``ts`` 一律为 **naive（无时区）pandas.Timestamp**，代表「交易所本地墙钟」：
  - 加密币：UTC 墙钟（OKX 本身即 UTC 7×24）；
  - 期货 / 股票：北京时间墙钟（akshare 返回的 'date' 即北京时间）。

下游 ``chart_serialize._ts_to_seconds`` 会把 naive 时间当作 UTC 转 POSIX 秒，
因此 lightweight-charts 横轴显示的日期/时刻与交易所本地墙钟一致，且与服务器
时区无关、完全可复现（盲选 / PK 确定性的基础）。休市缺口按时间戳自然留白。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
import sys
from abc import ABC, abstractmethod

import pandas as pd



def ensure_adapter_paths():
    """合并迁移后为空操作（保留函数名与调用点，避免改动 registry/providers 时序）。

    原实现把 项目根 / futures / stocks / kline 四个目录塞进 sys.path，好让
    ``import futures_adapter`` / ``import stock_adapter`` 这类裸导入能解析到
    futureStockTrade 自己的那份文件——这正是「多目录同名模块互相遮蔽」的隐患来源。
    并入 cryptoTrade 后，三品类 adapter 一律走 ``crypto.futures`` /
    ``crypto.stocks`` / ``crypto.kline_training`` 显式包路径导入，不再需要拼路径，
    所以这里只保留一个空壳，历史调用点全部原样可用。
    """


# ====================================================================
#  周期 → 秒
# ====================================================================

PERIOD_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1H': 3600, '2H': 7200, '4H': 14400, '6H': 21600, '12H': 43200,
    '1D': 86400, '2D': 172800, '3D': 259200, '1W': 604800, '1M': 2592000,
}


def period_to_seconds(period: str):
    """周期标识 → 每根K线秒数；未知周期返回 None"""
    return PERIOD_SECONDS.get(period)


# ====================================================================
#  时间解析
# ====================================================================

def parse_time(value):
    """把 unix 秒/毫秒 或 日期字符串解析为 naive 墙钟 pd.Timestamp。

    - None / '' → None
    - int/float：> 1e12 视为毫秒，否则秒
    - pd.Timestamp：tz-aware → 转 UTC 后去时区；naive 原样返回
    - str：支持 'YYYY-MM-DD'、'YYYYMMDD'、'YYYY-MM-DD HH:MM:SS'
    """
    if value is None or value == '':
        return None
    if isinstance(value, pd.Timestamp):
        ts = value
        if ts.tzinfo is not None:
            ts = ts.tz_convert('UTC').tz_localize(None)
        return ts
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:            # 毫秒
            v /= 1000.0
        return pd.Timestamp(v, unit='s')
    s = str(value).strip()
    if not s:
        return None
    ts = pd.to_datetime(s, errors='coerce')
    if pd.isna(ts):
        raise ValueError('无法解析时间参数: %r' % (value,))
    if getattr(ts, 'tzinfo', None) is not None:
        ts = ts.tz_convert('UTC').tz_localize(None)
    return ts


def to_date_str(value, fmt='%Y%m%d'):
    """把 start/end 转为 akshare 需要的日期字符串；None → None"""
    ts = parse_time(value)
    return None if ts is None else ts.strftime(fmt)


def to_unix_seconds(value):
    """把 start/end 转为 naive 墙钟对应的 POSIX 秒（int）；None → None"""
    ts = parse_time(value)
    return None if ts is None else int(ts.timestamp())


# ====================================================================
#  OHLCV 标准化
# ====================================================================

_STANDARD_COLS = ['ts', 'open', 'high', 'low', 'close', 'volume']


def empty_ohlcv() -> pd.DataFrame:
    """空的标准化 OHLCV DataFrame"""
    return pd.DataFrame(columns=_STANDARD_COLS)


def to_standard_ohlcv(df: pd.DataFrame, date_col: str = 'date',
                      volume_col: str = 'volume') -> pd.DataFrame:
    """把 akshare 风格 DataFrame 标准化为 ['ts','open','high','low','close','volume']

    - 'date' 列解析为 naive 墙钟 Timestamp；
    - OHLC 转数值，缺失行丢弃；volume 缺失填 0；
    - 按 ts 升序、去重、重置索引。
    """
    if df is None or len(df) == 0:
        return empty_ohlcv()

    out = df.copy()
    if date_col not in out.columns:
        raise ValueError('数据缺少时间列: %s（实际列: %s）' % (date_col, list(out.columns)))

    ts = pd.to_datetime(out[date_col], errors='coerce')
    if getattr(ts.dt, 'tz', None) is not None:
        ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
    out['ts'] = ts

    for c in ('open', 'high', 'low', 'close'):
        if c not in out.columns:
            raise ValueError('数据缺少 OHLC 列: %s（实际列: %s）' % (c, list(out.columns)))
        out[c] = pd.to_numeric(out[c], errors='coerce')

    if volume_col in out.columns:
        out['volume'] = pd.to_numeric(out[volume_col], errors='coerce').fillna(0.0)
    else:
        out['volume'] = 0.0

    out = out.dropna(subset=['ts', 'open', 'high', 'low', 'close'])
    out = out[_STANDARD_COLS].sort_values('ts').drop_duplicates(subset=['ts'])
    return out.reset_index(drop=True)


def crop_by_range(df: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    """按 [start, end]（unix秒/毫秒 或 日期字符串）裁剪标准化 OHLCV"""
    if df is None or df.empty:
        return empty_ohlcv()
    start_ts = parse_time(start)
    end_ts = parse_time(end)
    out = df
    if start_ts is not None:
        out = out[out['ts'] >= start_ts]
    if end_ts is not None:
        out = out[out['ts'] <= end_ts]
    return out.reset_index(drop=True)


# ====================================================================
#  重采样（周线 / 更高周期）
# ====================================================================

def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """对标准化 OHLCV（含 'ts' 列）按 pandas 频率规则重采样。

    用于股票 1H→4H 等 akshare 不直接提供的高周期。
    """
    if df is None or df.empty:
        return empty_ohlcv()
    d = df.set_index('ts')
    out = d.resample(rule).agg({
        'open': 'first', 'high': 'max', 'low': 'min',
        'close': 'last', 'volume': 'sum',
    }).dropna(subset=['open', 'high', 'low', 'close'])
    return out.reset_index()


def resample_daily_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """日线 → 周线（'W' 规则，取当周最后一根），与 futures_adapter 口径一致"""
    if df is None or df.empty:
        return empty_ohlcv()
    return resample_ohlcv(df, 'W')


# ====================================================================
#  Provider 抽象基类
# ====================================================================

class MarketDataProvider(ABC):
    """品类无关行情数据接口

    子类需设置 ``category`` / ``display_name`` / ``long_only`` / ``periods``，
    并实现 ``fetch_ohlcv``。
    """

    category = ''            # 'crypto' | 'futures' | 'stock'
    display_name = ''        # 中文名，用于前端展示
    long_only = False        # A股只能做多 → True
    periods = ()             # 该品类支持的周期白名单（有序）

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, period: str, start=None, end=None,
                    limit: int = None) -> pd.DataFrame:
        """获取标准化 OHLCV（['ts','open','high','low','close','volume']，升序）

        Parameters
        ----------
        symbol : str   品类内符号（加密币短代码 / 期货品种代码 / 股票代码）
        period : str   周期标识（见各 provider 的 periods）
        start, end : 可选区间（unix秒/毫秒 或 'YYYY-MM-DD' 等字符串）
        limit : int    无区间时返回最近 N 根

        说明：给定 [start, end] 时返回该区间内的K线；否则返回最近 limit 根。
        返回的 ts 均为 naive 墙钟时间戳。
        """
        raise NotImplementedError

    def supports_period(self, period: str) -> bool:
        return period in self.periods
