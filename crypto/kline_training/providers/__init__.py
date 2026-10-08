#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/providers/__init__.py  →  现位置: crypto/kline_training/providers/__init__.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
品类无关行情数据层（providers 包）
==================================
统一入口：``get_provider(category)`` 返回对应品类的 ``MarketDataProvider`` 实例。

三实现：
  - crypto  → CryptoProvider （OKX 公开 API）
  - futures → FuturesProvider（akshare，复用 futures_adapter）
  - stock   → StockProvider  （akshare，复用 stock_adapter，long_only）

导入约定：本包以顶层包名 ``providers`` 被导入（kline 目录已在 sys.path 中），
包内模块间使用相对导入（``from .base import ...``）。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from .base import (MarketDataProvider, crop_by_range, empty_ohlcv,
                   ensure_adapter_paths, parse_time, period_to_seconds,
                   resample_daily_to_weekly, resample_ohlcv, to_date_str,
                   to_standard_ohlcv, to_unix_seconds)
from .crypto_provider import CryptoProvider
from .futures_provider import FuturesProvider
from .stock_provider import StockProvider

# 品类 → provider 单例（无状态，可安全复用）
_PROVIDERS = {
    'crypto': CryptoProvider(),
    'futures': FuturesProvider(),
    'stock': StockProvider(),
}

# 常见别名归一（前端/URL 可能传入的写法）
_ALIASES = {
    'crypto': 'crypto', 'cryptocurrency': 'crypto', 'coin': 'crypto',
    'coins': 'crypto', 'digital': 'crypto',
    'futures': 'futures', 'future': 'futures', 'fut': 'futures',
    'stock': 'stock', 'stocks': 'stock', 'ashare': 'stock', 'a_share': 'stock',
    'equity': 'stock',
}


def normalize_category(category: str) -> str:
    """把品类别名归一为 'crypto' / 'futures' / 'stock'；无法识别时抛 ValueError"""
    key = (category or '').strip().lower()
    key = _ALIASES.get(key, key)
    if key not in _PROVIDERS:
        raise ValueError('未知品类: %r（可选: %s）'
                         % (category, ', '.join(sorted(_PROVIDERS))))
    return key


def get_provider(category: str) -> MarketDataProvider:
    """按品类获取 provider 实例"""
    return _PROVIDERS[normalize_category(category)]


def list_categories():
    """返回支持的品类列表"""
    return list(_PROVIDERS.keys())


__all__ = [
    'MarketDataProvider', 'CryptoProvider', 'FuturesProvider', 'StockProvider',
    'get_provider', 'list_categories', 'normalize_category',
    'crop_by_range', 'empty_ohlcv', 'ensure_adapter_paths', 'parse_time',
    'period_to_seconds', 'resample_daily_to_weekly', 'resample_ohlcv',
    'to_date_str', 'to_standard_ohlcv', 'to_unix_seconds',
]
