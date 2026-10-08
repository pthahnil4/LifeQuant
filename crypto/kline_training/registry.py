#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/registry.py  →  现位置: crypto/kline_training/registry.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
统一符号注册表
==============
以品类无关的方式提供符号（品种）清单与校验，覆盖三品类既有来源：
  - crypto ：``kline_adapter.CRYPTO_SYMBOLS``（本地精选，离线可用）
  - futures：``futures_adapter.get_futures_code_name_map / get_futures_categories``
             （读 future.csv）
  - stock  ：``stock_adapter.get_stock_code_name_map / get_stock_categories``

对外主要函数：
  - ``list_categories()``               品类元信息（含 long_only / periods）
  - ``list_symbols(category)``          扁平符号清单
  - ``list_symbol_groups(category)``    分组符号清单（行业/种类）
  - ``resolve(category, symbol)``       校验并归一化单个符号
  - ``is_valid_symbol(category, sym)``  布尔校验
  - ``valid_periods(category)``         该品类支持的周期

所有查询均为离线操作（读本地 csv / 常量字典），不触发网络。adapter 采用惰性
导入 + 防御性 sys.path，确保本模块可独立运行。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import logging

from crypto.kline_training.providers import get_provider, normalize_category
from crypto.kline_training.providers.base import ensure_adapter_paths

logger = logging.getLogger(__name__)

# 股票/期货带市场前缀时的剥离（如 sh600519 → 600519）
_MARKET_PREFIXES = ('sh', 'sz', 'bj')


# ====================================================================
#  品类元信息
# ====================================================================

def list_categories():
    """返回三品类的元信息列表（供首页卡片 / 前端渲染）"""
    out = []
    for cat in ('crypto', 'futures', 'stock'):
        p = get_provider(cat)
        out.append({
            'category': cat,
            'display_name': p.display_name,
            'long_only': p.long_only,
            'periods': list(p.periods),
        })
    return out


def category_meta(category):
    """单品类元信息"""
    cat = normalize_category(category)
    p = get_provider(cat)
    return {
        'category': cat,
        'display_name': p.display_name,
        'long_only': p.long_only,
        'periods': list(p.periods),
    }


def valid_periods(category):
    """该品类支持的周期白名单（有序）"""
    return list(get_provider(normalize_category(category)).periods)


# ====================================================================
#  符号清单
# ====================================================================

def list_symbols(category):
    """扁平符号清单：[{code, name, ...}]"""
    cat = normalize_category(category)
    ensure_adapter_paths()

    if cat == 'crypto':
        from crypto.kline_training.kline_adapter import CRYPTO_SYMBOLS
        return [{
            'code': code,
            'name': cfg.get('display_name', code),
            'exchange': cfg.get('exchange', ''),
            'inst_id': cfg.get('inst_id', ''),
        } for code, cfg in CRYPTO_SYMBOLS.items()]

    if cat == 'futures':
        from crypto.futures import futures_adapter as fa
        return [{'code': code, 'name': name}
                for code, name in fa.get_futures_code_name_map().items()]

    # stock
    from crypto.stocks import stock_adapter as sa
    return [{'code': code, 'name': name}
            for code, name in sa.get_stock_code_name_map().items()]


def list_symbol_groups(category):
    """分组符号清单：[{group, symbols:[{code,name}]}]

    - futures：按「种类」（能源化工/黑色金属/…）
    - stock  ：按「行业」（白酒/金融/…）
    - crypto ：单组（本地精选）
    """
    cat = normalize_category(category)
    ensure_adapter_paths()

    if cat == 'crypto':
        return [{'group': '精选品种', 'symbols': list_symbols('crypto')}]

    if cat == 'futures':
        from crypto.futures import futures_adapter as fa
        raw = fa.get_futures_categories()
        return [{'group': g, 'symbols': [{'code': it['code'], 'name': it['name']}
                                         for it in items]}
                for g, items in raw.items()]

    from crypto.stocks import stock_adapter as sa
    raw = sa.get_stock_categories()
    return [{'group': g, 'symbols': [{'code': it['code'], 'name': it['name']}
                                     for it in items]}
            for g, items in raw.items()]


# ====================================================================
#  单符号校验 / 归一化
# ====================================================================

def _strip_market_prefix(sym: str) -> str:
    low = sym.lower()
    for p in _MARKET_PREFIXES:
        if low.startswith(p) and len(sym) > len(p):
            return sym[len(p):]
    return sym


def resolve(category, symbol):
    """校验并归一化单个符号，返回统一记录；非法则抛 ValueError。

    返回结构：
        {category, code, name, exchange, long_only, inst_id?}
    """
    cat = normalize_category(category)
    sym = (symbol or '').strip()
    if not sym:
        raise ValueError('符号不能为空')
    ensure_adapter_paths()

    if cat == 'crypto':
        from crypto.kline_training.kline_adapter import CRYPTO_SYMBOLS
        code = sym.upper()
        cfg = CRYPTO_SYMBOLS.get(code)
        if not cfg:
            raise ValueError('不支持的加密币品种: %s，可选: %s'
                             % (symbol, ', '.join(CRYPTO_SYMBOLS)))
        return {
            'category': 'crypto', 'code': code,
            'name': cfg.get('display_name', code),
            'exchange': cfg.get('exchange', ''),
            'inst_id': cfg.get('inst_id', ''),
            'long_only': False,
        }

    if cat == 'futures':
        from crypto.futures import futures_adapter as fa
        code = sym.upper()
        name_map = fa.get_futures_code_name_map()
        if code not in name_map:
            raise ValueError('不支持的期货品种: %s' % symbol)
        return {
            'category': 'futures', 'code': code,
            'name': name_map[code], 'exchange': '', 'long_only': False,
        }

    # stock
    from crypto.stocks import stock_adapter as sa
    code = _strip_market_prefix(sym).upper()
    name_map = sa.get_stock_code_name_map()
    if code not in name_map:
        raise ValueError('不支持的股票代码: %s' % symbol)
    return {
        'category': 'stock', 'code': code,
        'name': name_map[code], 'exchange': '', 'long_only': True,
    }


def is_valid_symbol(category, symbol) -> bool:
    """布尔校验（不抛异常）"""
    try:
        resolve(category, symbol)
        return True
    except Exception:
        return False
