#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/providers/futures_provider.py  →  现位置: crypto/kline_training/providers/futures_provider.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
期货行情 Provider
==================
复用 ``futures/futures_adapter.py`` 的取数函数（不改动监控台行为）：
  - 日线：``_fetch_daily_with_retry``（→ akshare 主力连续合约）
  - 周线：日线标准化后按 'W' 重采样
  - 分钟/小时：``_fetch_minute_kline_for_code``（自动解析主力合约，
    并对 2H/4H/6H/12H 内部重采样）

akshare 返回的 'date' 为北京时间墙钟，经 ``to_standard_ohlcv`` 转为 naive
墙钟时间戳（横轴显示与交易所本地时间一致）。

限制：akshare 分钟数据历史深度有限，固定时段的分钟级训练受近端可取范围约束；
日线/周线历史充足。为保证盲选/PK 的 seed 确定性，分钟级可通过 ``contract``
关键字 pin 具体主力合约（默认走实时解析的当前主力）。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import datetime
import logging

import pandas as pd

from .base import (MarketDataProvider, crop_by_range, empty_ohlcv,
                   ensure_adapter_paths, parse_time, resample_daily_to_weekly,
                   to_date_str, to_standard_ohlcv)

logger = logging.getLogger(__name__)

# 无显式 start 时的默认日线回看天数（周线需更长历史以重采样出足够根数）
_DEFAULT_LOOKBACK_DAYS = {'1D': 365 * 5, '1W': 365 * 8}
_FALLBACK_LOOKBACK_DAYS = 365 * 3

# 「每根K线≈多少日历天」的宽松估计（含休市缺口），用于按 limit 反推回看起点
_DAYS_PER_BAR = {'1D': 2.0, '1W': 8.0}


def _default_start_str(period: str) -> str:
    days = _DEFAULT_LOOKBACK_DAYS.get(period, _FALLBACK_LOOKBACK_DAYS)
    return (datetime.date.today() - datetime.timedelta(days=days)).strftime('%Y%m%d')


def _lookback_start_str(limit: int, period: str, end=None) -> str:
    """按「需要 limit 根、结束于 end」反推一个宽松的回看起点（YYYYMMDD）"""
    per_bar = _DAYS_PER_BAR.get(period, 2.0)
    days = int(limit * per_bar) + 30
    base = parse_time(end) or pd.Timestamp.today().normalize()
    return (base - pd.Timedelta(days=days)).strftime('%Y%m%d')


class FuturesProvider(MarketDataProvider):
    category = 'futures'
    display_name = '期货'
    long_only = False
    periods = ('1m', '5m', '15m', '30m', '1H', '4H', '1D', '1W')

    def fetch_ohlcv(self, symbol, period, start=None, end=None, limit=None,
                    **kwargs):
        ensure_adapter_paths()
        from crypto.futures import futures_adapter as fa

        code = (symbol or '').upper()
        contract = kwargs.get('contract')

        if period in ('1D', '1W'):
            return self._fetch_daily(fa, code, period, start, end, limit)
        return self._fetch_minute(fa, code, period, start, end, limit, contract)

    # ----------------------------------------------------------------
    def _fetch_daily(self, fa, code, period, start, end, limit):
        end_str = to_date_str(end)          # None → akshare 默认今日
        if start is not None:
            start_str = to_date_str(start)
        elif limit:
            start_str = _lookback_start_str(limit, period, end)
        else:
            start_str = _default_start_str(period)
        daily = fa._fetch_daily_with_retry(code, start_str, end_str)
        std = to_standard_ohlcv(daily, date_col='date')
        if std.empty:
            logger.warning("[futures] %s %s 日线取数为空", code, period)
            return empty_ohlcv()
        if period == '1W':
            std = resample_daily_to_weekly(std)
        std = crop_by_range(std, start, end)
        # 无显式 start 时，limit 表示「结束于 end 的最近 N 根」
        if limit and start is None and not std.empty:
            std = std.tail(limit).reset_index(drop=True)
        return std

    # ----------------------------------------------------------------
    def _fetch_minute(self, fa, code, period, start, end, limit, contract):
        mdf = fa._fetch_minute_kline_for_code(code, period, contract_symbol=contract)
        std = to_standard_ohlcv(mdf, date_col='date')
        if std.empty:
            logger.warning("[futures] %s %s 分钟取数为空", code, period)
            return empty_ohlcv()
        std = crop_by_range(std, start, end)
        if limit and start is None and not std.empty:
            std = std.tail(limit).reset_index(drop=True)
        return std
