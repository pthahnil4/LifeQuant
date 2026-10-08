#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/providers/crypto_provider.py  →  现位置: crypto/kline_training/providers/crypto_provider.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
加密币行情 Provider
====================
封装 ``kline_adapter.fetch_kline_okx``（OKX 公开 API，动态代理 + 双通道回退 +
自动分页），补齐「固定区间」取数能力，输出标准化 OHLCV。

OKX 时间本身即 UTC；``fetch_kline_okx`` 返回 tz-aware UTC，经
``to_standard_ohlcv`` 统一转为 naive 墙钟（UTC），与期货/股票口径一致。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import logging

from .base import (MarketDataProvider, crop_by_range, empty_ohlcv,
                   ensure_adapter_paths, parse_time, period_to_seconds,
                   to_standard_ohlcv)

logger = logging.getLogger(__name__)

# OKX 深历史翻页防御性上限（与 training_service 的取数上限协同）
_MAX_BARS = 2000


class CryptoProvider(MarketDataProvider):
    category = 'crypto'
    display_name = '加密币'
    long_only = False
    periods = ('1m', '5m', '15m', '30m', '1H', '4H', '1D')

    def fetch_ohlcv(self, symbol, period, start=None, end=None, limit=None):
        ensure_adapter_paths()
        from crypto.kline_training.kline_adapter import fetch_kline_okx

        sym = (symbol or '').upper()
        start_ts = parse_time(start)
        end_ts = parse_time(end)
        period_sec = period_to_seconds(period) or 3600

        if end_ts is not None:
            # OKX 'after' 返回严格早于该毫秒时间的K线，+1ms 以包含 end 当根
            after_ms = int(end_ts.timestamp() * 1000) + 1
            if start_ts is not None:
                span = int((end_ts - start_ts).total_seconds() // period_sec) + 2
            else:
                span = limit or 500
            want = min(max(span, 50), _MAX_BARS) + 5   # 少量 buffer 应对边界
            df = fetch_kline_okx(sym, period, limit=want, after=after_ms)
        else:
            # 无区间：取最近 N 根（沿用既有「最近N根」体验）
            want = min(max(limit or 500, 50), _MAX_BARS)
            df = fetch_kline_okx(sym, period, limit=want)

        std = to_standard_ohlcv(df, date_col='ts')
        if std.empty:
            logger.warning("[crypto] %s %s 区间取数为空", sym, period)
            return empty_ohlcv()

        # 精确裁剪到 [start, end]（fetch 可能多拉了 buffer）
        return crop_by_range(std, start, end)
