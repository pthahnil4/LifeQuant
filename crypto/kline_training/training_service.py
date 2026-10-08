#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/training_service.py  →  现位置: crypto/kline_training/training_service.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
品类无关训练数据服务
====================
Phase 0 的核心交付：``get_training_chart_data(category, symbol, period, start, end)``
对加密币 / 期货 / 股票三品类返回**结构一致**的图表数据
（candles / volumes / indicators / meta），另提供：

  - ``make_seed``：确定性训练种子（盲选可复现、异步 PK 双方同题的基础）
  - ``load_more_chart_data``：惰性加载 before_ts 之前的更多历史

分层：取数走 providers（品类无关接口）→ 指标/序列化走 chart_serialize（与监控台
/ Pro3 口径一致）→ 符号校验/归一走 registry。

指标预热
--------
递推类指标（EMA/RSI/ADX/SAR）需要前置历史才收敛。区间模式下按
「区间跨度 + WARMUP」估算根数、以 end 为锚向前多取，再计算指标并裁剪回
[start, end]；最近N根模式下多取 WARMUP 根后 tail。两条路径均保证可见区段首部
指标不失真。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import hashlib
import json
import logging
import os
import random
import sys
import uuid

import pandas as pd


from crypto.kline_training import registry
from crypto.kline_training.providers import get_provider, normalize_category
from crypto.kline_training.providers.base import crop_by_range, parse_time, period_to_seconds
from crypto.kline_training.chart_serialize import WARMUP_BARS, calc_all_indicators, df_to_chart_data

logger = logging.getLogger(__name__)

# 单次取数的K线上限（防御性，避免超大区间打爆上游/内存）
MAX_BARS = 2000


# ====================================================================
#  确定性种子
# ====================================================================

def make_seed(category, symbol, period, start_ts=None, bar_count=None,
              engine_config=None):
    """生成确定性训练种子。

    历史K线不可变，故相同 (category, symbol, period, start_ts, bar_count,
    engine_config) 必然对应完全相同的训练数据 —— 这是盲选可复现、异步 PK
    双方「同题」的基础。

    Returns
    -------
    dict : {'seed': <16位hex>, 'payload': {...}, 'canonical': <json str>}
    """
    cat = normalize_category(category)
    try:
        code = registry.resolve(cat, symbol)['code']
    except Exception:
        code = (symbol or '').strip().upper()
    payload = {
        'category': cat,
        'symbol': code,
        'period': period,
        'start_ts': int(start_ts) if start_ts is not None else None,
        'bar_count': int(bar_count) if bar_count is not None else None,
        'engine_config': engine_config or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False)
    seed = hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]
    return {'seed': seed, 'payload': payload, 'canonical': canonical}


# ====================================================================
#  内部：meta 构建 / 区间取数
# ====================================================================

def _build_meta(category, rec, period, bar_count, date_range, provider):
    """构建品类一致、且向后兼容旧加密币字段的 meta"""
    return {
        # 新增：品类感知字段
        'category': category,
        'code': rec['code'],
        'name': rec.get('name', rec['code']),
        'long_only': bool(provider.long_only),
        # 向后兼容：旧 kline.js 读取的字段
        'symbol': rec['code'],
        'display_name': rec.get('name', rec['code']),
        'period': period,
        'exchange': rec.get('exchange', ''),
        'bar_count': bar_count,
        'date_range': date_range,
    }


def _fetch_range(provider, code, period, start_ts, end_ts, limit, target_bars,
                 warmup):
    """区间/半开区间取数：以 end 为锚向前多取 warmup，返回未裁剪的原始 df"""
    period_sec = period_to_seconds(period) or 86400

    if start_ts is not None and end_ts is not None:
        span_sec = max((end_ts - start_ts).total_seconds(), 0)
        count = int(span_sec // period_sec) + warmup + 5
        count = max(warmup + 10, min(count, MAX_BARS))
        return provider.fetch_ohlcv(code, period, end=end_ts, limit=count)

    if end_ts is not None:                 # 只给 end：取 end 之前 N 根
        base = target_bars or limit or 500
        count = max(warmup + 10, min(int(base) + warmup + 5, MAX_BARS))
        return provider.fetch_ohlcv(code, period, end=end_ts, limit=count)

    # 只给 start：从 start 到最新（无 start 之前的预热，属边界场景）
    return provider.fetch_ohlcv(code, period, start=start_ts, end=None)


# ====================================================================
#  主查询
# ====================================================================

def get_training_chart_data(category, symbol, period='1H', start=None, end=None,
                            limit=None, full_data=False, target_bars=None,
                            warmup=WARMUP_BARS):
    """品类无关的训练图表数据入口。

    Parameters
    ----------
    category : str   'crypto' | 'futures' | 'stock'（含常见别名）
    symbol   : str   品类内符号
    period   : str   周期（须在该品类 periods 白名单内）
    start, end : 可选固定区间（unix秒/毫秒 或 'YYYY-MM-DD' 等）
    limit / full_data / target_bars : 无区间时的「最近N根」控制
        - full_data=True 且有 target_bars → 取 target_bars 根
        - 否则取 limit 根（默认 500）

    Returns
    -------
    dict : {'candles', 'volumes', 'indicators', 'meta'}（三品类结构一致）
    """
    provider = get_provider(category)          # 归一 + 校验品类
    cat = provider.category
    rec = registry.resolve(cat, symbol)        # 校验 + 归一符号（非法抛 ValueError）
    code = rec['code']

    if not provider.supports_period(period):
        raise ValueError('品类 %s 不支持周期 %s，可选: %s'
                         % (cat, period, ', '.join(provider.periods)))

    start_ts = parse_time(start)
    end_ts = parse_time(end)
    want_range = (start_ts is not None or end_ts is not None)

    if want_range:
        df = _fetch_range(provider, code, period, start_ts, end_ts,
                          limit, target_bars, warmup)
        want = None
    else:
        want = target_bars if (full_data and target_bars) else (limit or 500)
        want = max(50, min(int(want), MAX_BARS))
        df = provider.fetch_ohlcv(code, period, limit=want + warmup)

    if df is None or df.empty:
        logger.warning("[%s] %s %s 取数为空", cat, code, period)
        return {'candles': [], 'volumes': [], 'indicators': {},
                'error': '无法获取K线数据',
                'meta': _build_meta(cat, rec, period, 0, None, provider)}

    df = calc_all_indicators(df)

    if want_range:
        df = crop_by_range(df, start_ts, end_ts)
    elif len(df) > want:
        df = df.tail(want).reset_index(drop=True)

    chart = df_to_chart_data(df, period=period)
    candles = chart['candles']
    date_range = ({'start': candles[0]['time'], 'end': candles[-1]['time']}
                  if candles else None)
    meta = _build_meta(cat, rec, period, len(candles), date_range, provider)
    # 附带确定性种子（盲选/PK 场景可回传校验）
    meta['seed'] = make_seed(
        cat, code, period,
        start_ts=candles[0]['time'] if candles else None,
        bar_count=len(candles),
    )['seed']
    chart['meta'] = meta
    return chart


# ====================================================================
#  惰性加载（before_ts 之前的更多历史）
# ====================================================================

def load_more_chart_data(category, symbol, period='1H', before_ts=None,
                         target_bars=200, warmup=WARMUP_BARS):
    """加载 before_ts（lightweight-charts 秒级时间戳）之前的更多历史K线。

    同样多取 warmup 根做指标预热后裁剪，避免分段计算的指标接缝跳变。
    """
    empty = {'candles': [], 'volumes': [], 'indicators': {},
             'meta': {'bar_count': 0}}
    if not before_ts:
        return empty

    provider = get_provider(category)
    cat = provider.category
    rec = registry.resolve(cat, symbol)
    code = rec['code']
    if not provider.supports_period(period):
        raise ValueError('品类 %s 不支持周期 %s' % (cat, period))

    end = int(before_ts) - 1                   # 严格早于 before_ts
    target_bars = max(50, min(int(target_bars), 1000))

    df = provider.fetch_ohlcv(code, period, end=end, limit=target_bars + warmup)
    if df is None or df.empty:
        return empty

    df = calc_all_indicators(df)
    df = crop_by_range(df, None, end)
    if len(df) > target_bars:
        df = df.tail(target_bars).reset_index(drop=True)

    chart = df_to_chart_data(df, period=period)
    chart['meta'] = {
        'category': cat, 'symbol': code, 'period': period,
        'bar_count': len(chart['candles']),
    }
    return chart


# ====================================================================
#  盲选训练（服务器随机分配 + 去标识化）
# ====================================================================

# 各品类盲选周期权重：偏向历史充足、可复现的日线/周线；加密币额外含 intraday
# （OKX 深度充足）。期货/股票仅用日线/周线，规避 akshare 分钟历史深度不足。
_BLIND_PERIOD_WEIGHTS = {
    'crypto':  ['1D', '1D', '4H', '1H'],
    'futures': ['1D', '1D', '1W'],
    'stock':   ['1D', '1D', '1W'],
}

# intraday 周期集合（end 需取近端：akshare 分钟历史深度有限）
_INTRADAY_PERIODS = {'1m', '5m', '15m', '30m', '1H', '2H', '4H', '6H', '12H'}


def _blind_end_ts(category, period, rng):
    """为盲选挑一个历史锚点 end（naive 墙钟 Timestamp，当日 23:59:59）。

    - intraday：加密币可回溯较深（OKX），期货/股票取近端（akshare 分钟有限）；
    - 日线/周线：三品类历史均充足，取 120~1000 天前，保证「已走完」的确定区间。
    """
    today = pd.Timestamp.today().normalize()
    if period in _INTRADAY_PERIODS:
        days = rng.randint(10, 300) if category == 'crypto' else rng.randint(2, 25)
    else:
        days = rng.randint(120, 1000)
    return (today - pd.Timedelta(days=int(days))).replace(
        hour=23, minute=59, second=59)


def _blind_fetch(provider, code, period, end_ts, bar_count, warmup):
    """以 end 为锚取 bar_count 根（多取 warmup 预热后裁剪），返回标准化 df 或 None"""
    count = max(warmup + 10, min(int(bar_count) + warmup + 5, MAX_BARS))
    df = provider.fetch_ohlcv(code, period, end=end_ts, limit=count)
    if df is None or df.empty:
        return None
    df = calc_all_indicators(df)
    df = crop_by_range(df, None, end_ts)
    if len(df) > bar_count:
        df = df.tail(bar_count).reset_index(drop=True)
    return df


def start_blind(bar_count=250, categories=None, min_bars=100, max_attempts=8,
                warmup=WARMUP_BARS, rng=None):
    """盲选训练：服务器随机选「品类+品种+周期+历史区间」，返回去标识化K线与答案。

    历史K线不可变 → 同一 (category, symbol, period, end, bar_count) 完全可复现，
    是盲选公平与后续 PK 的基础。答案（真实品类/品种/名称/区间/seed）由调用方按
    training_id 存入 SQLite，揭晓前**绝不下发前端**；chart.meta 已彻底去标识化
    （category='blind'、code='??????'、name='神秘品种'、exchange=''，且
    long_only=False 以免「仅做多」泄露其为A股）。

    Parameters
    ----------
    bar_count    : 目标K线根数（默认 250，范围 60~MAX_BARS）
    categories   : 限定品类列表（默认三品类全选）
    min_bars     : 可接受的最少根数（不足则换题重试）
    max_attempts : 最大重试次数（每次换随机题，容忍个别品种取数失败/限频）

    Returns
    -------
    dict : {'training_id': <uuid hex>, 'answer': {category, symbol, name, period,
            start_ts, end_ts, bar_count, seed}, 'chart': {...去标识化...}}

    Raises
    ------
    RuntimeError : 连续 max_attempts 次都取不到足够K线
    """
    rng = rng or random.Random()
    cats = [normalize_category(c)
            for c in (categories or ['crypto', 'futures', 'stock'])]
    bar_count = max(60, min(int(bar_count), MAX_BARS))
    min_bars = max(30, min(int(min_bars), bar_count))

    last_err = None
    for _attempt in range(max_attempts):
        cat = rng.choice(cats)
        provider = get_provider(cat)
        try:
            symbols = registry.list_symbols(cat)
        except Exception as e:            # 符号清单加载失败（如 csv 缺失）→ 换品类
            last_err = e
            continue
        if not symbols:
            continue
        rec = rng.choice(symbols)
        code = rec['code']
        name = rec.get('name', code)

        weights = _BLIND_PERIOD_WEIGHTS.get(cat, ['1D'])
        supported = [p for p in weights if provider.supports_period(p)]
        period = rng.choice(supported or list(provider.periods))
        end_ts = _blind_end_ts(cat, period, rng)

        try:
            df = _blind_fetch(provider, code, period, end_ts, bar_count, warmup)
        except Exception as e:            # 取数异常（网络/限频/主力合约解析）→ 换题重试
            last_err = e
            logger.warning("[blind] %s %s %s 取数失败(%s)，重试", cat, code, period, e)
            continue

        if df is None or len(df) < min_bars:
            last_err = RuntimeError('K线根数不足(%d)' % (0 if df is None else len(df)))
            logger.warning("[blind] %s %s %s 根数不足(%s)，重试",
                           cat, code, period, 0 if df is None else len(df))
            continue

        # ── 成功：构建去标识化 chart + 真实答案 ──
        chart = df_to_chart_data(df, period=period)
        candles = chart['candles']
        date_range = ({'start': candles[0]['time'], 'end': candles[-1]['time']}
                      if candles else None)
        start_ts = candles[0]['time'] if candles else None
        seed = make_seed(cat, code, period, start_ts=start_ts,
                         bar_count=len(candles))['seed']

        chart['meta'] = {
            'category': 'blind',
            'code': '??????',
            'name': '神秘品种',
            'display_name': '神秘品种',
            'symbol': '??????',
            'exchange': '',
            'long_only': False,          # 关键：不泄露「仅做多」→ 不暴露是A股
            'period': period,
            'bar_count': len(candles),
            'date_range': date_range,
            'blind': True,
        }

        answer = {
            'category': cat,
            'symbol': code,
            'name': name,
            'period': period,
            'start_ts': int(start_ts) if start_ts is not None else None,
            'end_ts': int(end_ts.timestamp()),
            'bar_count': len(candles),
            'seed': seed,
        }
        return {'training_id': uuid.uuid4().hex, 'answer': answer, 'chart': chart}

    raise RuntimeError('盲选取数连续 %d 次失败，请重试（最近错误: %s）'
                       % (max_attempts, last_err))
