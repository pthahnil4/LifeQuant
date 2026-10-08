#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/providers/stock_provider.py  →  现位置: crypto/kline_training/providers/stock_provider.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
股票行情 Provider（A股，只能做多）
==================================
复用 ``stocks/stock_adapter.py`` 的取数函数（不改动监控台行为）：
  - 日线：``_fetch_daily_with_retry``（新浪源失败自动回退腾讯源，前复权 qfq）
  - 周线：日线标准化后按 'W' 重采样
  - 分钟/小时：``_fetch_minute_with_retry``；akshare 不直接提供 4H，故取 1H
    后重采样为 4H

``long_only = True``：A股训练禁用做空，前端据此 gate「做空」按钮。
akshare 'date' 为北京时间墙钟，经 ``to_standard_ohlcv`` 转 naive 墙钟时间戳。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import datetime
import logging

import pandas as pd

from .base import (MarketDataProvider, crop_by_range, empty_ohlcv,
                   ensure_adapter_paths, parse_time, resample_daily_to_weekly,
                   resample_ohlcv, to_date_str, to_standard_ohlcv)

logger = logging.getLogger(__name__)

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


def _minute_filter_str(value):
    """分钟数据的 akshare 日期过滤串（'YYYY-MM-DD HH:MM:SS'）；None → None"""
    ts = parse_time(value)
    return None if ts is None else ts.strftime('%Y-%m-%d %H:%M:%S')


class StockProvider(MarketDataProvider):
    category = 'stock'
    display_name = '股票'
    long_only = True          # A股只能做多
    periods = ('1m', '5m', '15m', '30m', '1H', '4H', '1D', '1W')

    def fetch_ohlcv(self, symbol, period, start=None, end=None, limit=None,
                    **kwargs):
        ensure_adapter_paths()
        from crypto.stocks import stock_adapter as sa

        code = (symbol or '').strip().upper()
        adjust = kwargs.get('adjust', 'qfq')

        if period in ('1D', '1W'):
            return self._fetch_daily(sa, code, period, start, end, limit, adjust)
        return self._fetch_minute(sa, code, period, start, end, limit, adjust)

    # ----------------------------------------------------------------
    def _fetch_daily(self, sa, code, period, start, end, limit, adjust):
        end_str = to_date_str(end)
        if start is not None:
            start_str = to_date_str(start)
        elif limit:
            start_str = _lookback_start_str(limit, period, end)
        else:
            start_str = _default_start_str(period)
        daily = sa._fetch_daily_with_retry(code, start_str, end_str, adjust=adjust)
        std = to_standard_ohlcv(daily, date_col='date')
        if std.empty:
            logger.warning("[stock] %s %s 日线取数为空", code, period)
            return empty_ohlcv()
        if period == '1W':
            std = resample_daily_to_weekly(std)
        std = crop_by_range(std, start, end)
        # 无显式 start 时，limit 表示「结束于 end 的最近 N 根」
        if limit and start is None and not std.empty:
            std = std.tail(limit).reset_index(drop=True)
        return std

    # ----------------------------------------------------------------
    def _fetch_minute(self, sa, code, period, start, end, limit, adjust):
        # 4H 无原生分钟源 → 取 1H 后重采样
        fetch_period = '1H' if period == '4H' else period
        mdf = sa._fetch_minute_with_retry(
            code, period=fetch_period,
            start_date=_minute_filter_str(start),
            end_date=_minute_filter_str(end),
            adjust=adjust)
        std = to_standard_ohlcv(mdf, date_col='date')
        if std.empty:
            logger.warning("[stock] %s %s 分钟取数为空", code, period)
            return empty_ohlcv()
        if period == '4H':
            std = resample_ohlcv(std, '4h')
        std = crop_by_range(std, start, end)
        if limit and start is None and not std.empty:
            std = std.tail(limit).reset_index(drop=True)
        return std
