#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 2 自动搬运（2026-10-08）。
# 原名: futures/futures_adapter.py  →  现位置: crypto/futures/futures_adapter.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底已移除。
"""
期货策略数据适配器
==================
封装 akshare API 和 Pro3 策略，为期货 Web 页面提供后端数据支撑。
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import datetime
import json
import logging
import os
import time
from typing import Dict, List, Optional, Any

import pandas as pd

logger = logging.getLogger(__name__)

# ====================================================================
#  路径与配置
# ====================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "future.csv")
CONFIG_PATH = os.path.join(BASE_DIR, "futures_config.json")

# 周线数据回看天数（获取足够多的日线数据用于周线重采样）
WEEKLY_DAILY_LOOKBACK_DAYS = 365 * 3  # 3年日线 → 约 150 根周线

# ====================================================================
#  品种数据管理（从 future.csv 读取）
# ====================================================================

def _read_csv() -> pd.DataFrame:
    """读取 future.csv 并返回标准化的 DataFrame"""
    df = pd.read_csv(CSV_PATH, encoding='utf-8-sig')
    df = df.dropna(subset=['代码']).reset_index(drop=True)
    df['代码'] = df['代码'].astype(str).str.strip()
    df['种类'] = df['种类'].astype(str).str.strip()
    df['品种'] = df['品种'].astype(str).str.strip()
    return df


def get_futures_categories() -> Dict[str, List[Dict]]:
    """获取按种类分组的期货品种数据

    Returns
    -------
    dict
        { "能源化工": [{"code": "FG", "name": "玻璃"}, ...], ... }
    """
    df = _read_csv()
    categories = {}
    for _, row in df.iterrows():
        cat = row['种类']
        if cat not in categories:
            categories[cat] = []
        categories[cat].append({
            "code": row['代码'],
            "name": row['品种'],
            "category": cat,
        })
    return categories


def get_all_futures() -> List[str]:
    """返回全部品种字母代码列表"""
    df = _read_csv()
    return df['代码'].tolist()


def get_futures_code_name_map() -> Dict[str, str]:
    """返回 {代码: 品种名} 映射"""
    df = _read_csv()
    return dict(zip(df['代码'], df['品种']))


# ====================================================================
#  自选品种配置管理
# ====================================================================

def _load_config() -> Dict:
    """加载配置文件"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"selected": []}


def _save_config(config: Dict):
    """保存配置文件"""
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_selected_futures() -> List[str]:
    """获取当前自选品种列表"""
    config = _load_config()
    selected = config.get("selected", [])
    all_codes = get_all_futures()
    # 过滤掉已不存在的品种
    return [c for c in selected if c in all_codes]


def set_selected_futures(codes: List[str]):
    """保存自选品种列表"""
    all_codes = get_all_futures()
    valid = [c for c in codes if c in all_codes]
    config = _load_config()
    config["selected"] = valid
    _save_config(config)


def get_starred_futures() -> List[str]:
    """获取当前星标品种列表"""
    config = _load_config()
    starred = config.get("starred", [])
    all_codes = get_all_futures()
    return [c for c in starred if c in all_codes]


def set_starred_futures(codes: List[str]):
    """保存星标品种列表"""
    all_codes = get_all_futures()
    valid = [c for c in codes if c in all_codes]
    config = _load_config()
    config["starred"] = valid
    _save_config(config)


# ====================================================================
#  策略适配函数
# ====================================================================

def _fetch_daily_with_retry(code: str, start_date: str = None,
                            end_date: str = None, max_retries: int = 2
                            ) -> Optional[pd.DataFrame]:
    """获取日线数据（带重试）"""
    from crypto.futures.akshare_api import AkshareFuturesAPI, AkshareAPIError

    for attempt in range(max_retries + 1):
        try:
            df = AkshareFuturesAPI.fetch_daily_main(code, start_date, end_date)
            if df is not None and not df.empty and len(df) >= 50:
                return df
            return df
        except AkshareAPIError as e:
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            logger.warning("[%s] 日线数据获取失败: %s", code, e)
            return None
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            logger.warning("[%s] 日线数据获取异常: %s", code, e)
            return None
    return None


# ====================================================================
#  主力合约解析 & 分钟K线数据获取
# ====================================================================

# 缓存：{code: main_contract_symbol}，避免重复解析
_main_contract_cache: Dict[str, str] = {}
_main_contract_cache_time: float = 0.0

# 逻辑周期 → (akshare period, 是否需要resample倍数)
_MINUTE_PERIOD_MAP = {
    '1m':  ('1', 1),
    '3m':  ('1', 3),      # 1m resample → 3m
    '5m':  ('5', 1),
    '15m': ('15', 1),
    '30m': ('30', 1),
    '1H':  ('60', 1),
    '2H':  ('60', 2),     # 1H resample → 2H
    '4H':  ('60', 4),     # 1H resample → 4H
    '6H':  ('60', 6),     # 1H resample → 6H
    '12H': ('60', 12),    # 1H resample → 12H
}

# 分钟周期对应的策略参数
_MINUTE_STRATEGY_PARAMS = {
    '1m':  {'adx_period': 9, 'macd_fast': 50, 'macd_slow': 100, 'macd_signal': 30, 'base_weight': 0.65},
    '3m':  {'adx_period': 9, 'macd_fast': 30, 'macd_slow': 60,  'macd_signal': 20, 'base_weight': 0.66},
    '5m':  {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.68},
    '15m': {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.70},
    '30m': {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.70},
    '1H':  {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.70},
    '2H':  {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.72},
    '4H':  {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.74},
    '6H':  {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.75},
    '12H': {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.77},
}


def _resolve_main_contract(code: str) -> Optional[str]:
    """解析品种代码对应的当前主力合约符号

    通过 akshare 实时行情接口获取该品种所有合约，
    按持仓量排序找到主力合约，返回具体合约代码（如 'RB2610'）。
    结果缓存 5 分钟，避免频繁请求。

    Parameters
    ----------
    code : str
        品种字母代码，如 'RB', 'AU', 'IF'

    Returns
    -------
    str or None
        主力合约符号，如 'RB2610'；解析失败返回 None
    """
    global _main_contract_cache, _main_contract_cache_time

    cache_key = code.upper()
    now = time.time()
    # 缓存 5 分钟有效
    if cache_key in _main_contract_cache and (now - _main_contract_cache_time) < 300:
        return _main_contract_cache[cache_key]

    from crypto.futures.akshare_api import AkshareFuturesAPI, AkshareAPIError
    try:
        df_rt = AkshareFuturesAPI.fetch_realtime(code)
        if df_rt is None or df_rt.empty:
            logger.warning("[%s] 实时行情返回空，无法解析主力合约", code)
            return None

        # 按持仓量排序找主力合约
        df_sorted = df_rt.copy()
        if 'position' in df_sorted.columns:
            df_sorted['position'] = pd.to_numeric(
                df_sorted['position'], errors='coerce')
            df_sorted = df_sorted.sort_values('position', ascending=False)
        elif 'volume' in df_sorted.columns:
            df_sorted['volume'] = pd.to_numeric(
                df_sorted['volume'], errors='coerce')
            df_sorted = df_sorted.sort_values('volume', ascending=False)

        main_sym = str(df_sorted.iloc[0].get('symbol', '')).strip().upper()
        if main_sym:
            _main_contract_cache[cache_key] = main_sym
            _main_contract_cache_time = now
            logger.info("[%s] 解析主力合约: %s", code, main_sym)
            return main_sym
        return None
    except (AkshareAPIError, Exception) as e:
        logger.warning("[%s] 解析主力合约失败: %s", code, e)
        return None


def _fetch_minute_kline_for_code(code: str, period: str,
                                  contract_symbol: str = None
                                  ) -> Optional[pd.DataFrame]:
    """获取指定品种的分钟K线数据

    自动解析主力合约并调用 akshare 分钟K线接口。
    对于需要 resample 的周期（如 2H/4H/6H/12H），先获取 1H 数据再聚合。

    Parameters
    ----------
    code : str
        品种字母代码，如 'RB'
    period : str
        逻辑周期标识，如 '5m', '1H', '2H'
    contract_symbol : str, optional
        如果已知具体合约代码可直接传入，跳过解析步骤

    Returns
    -------
    pd.DataFrame or None
        标准化的分钟K线数据，列: ['date','open','high','low','close','volume','hold']
    """
    from crypto.futures.akshare_api import AkshareFuturesAPI, AkshareAPIError

    period_info = _MINUTE_PERIOD_MAP.get(period)
    if period_info is None:
        logger.warning("[%s] 不支持的分钟周期: %s", code, period)
        return None

    ak_period, resample_mult = period_info

    # 解析主力合约
    if contract_symbol is None:
        contract_symbol = _resolve_main_contract(code)
    if contract_symbol is None:
        logger.warning("[%s] 无法解析主力合约，分钟K线获取失败", code)
        return None

    try:
        df = AkshareFuturesAPI.fetch_minute_kline(
            contract_symbol, period=ak_period)
    except AkshareAPIError as e:
        logger.warning("[%s] %s 分钟K线获取失败: %s", code, period, e)
        return None
    except Exception as e:
        logger.warning("[%s] %s 分钟K线获取异常: %s", code, period, e)
        return None

    if df is None or df.empty:
        return None

    # 如果需要 resample（如 2H=1H×2, 4H=1H×4）
    if resample_mult > 1 and 'date' in df.columns:
        df = df.set_index('date')
        rule_map = {2: '2h', 3: '3min', 4: '4h', 6: '6h', 12: '12h'}
        rule = rule_map.get(resample_mult)
        if rule:
            df = df.resample(rule).agg({
                'open': 'first',
                'high': 'max',
                'low': 'min',
                'close': 'last',
                'volume': 'sum',
                'hold': 'last',
            }).dropna()
        df = df.reset_index()

    logger.info("[%s] 成功获取 %s 分钟K线: %d 条", code, period, len(df))
    return df


def _resample_daily_to_weekly(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """将日线数据重采样为周线数据

    使用 'W' 规则（周日为周末），取当周最后一天。

    Parameters
    ----------
    df : pd.DataFrame
        日线数据，必须包含 'date', 'open', 'high', 'low', 'close', 'volume' 列。

    Returns
    -------
    pd.DataFrame or None
        周线数据，列: ['date', 'open', 'high', 'low', 'close', 'volume']
    """
    if df is None or df.empty or len(df) < 10:
        return None

    df = df.copy()
    if 'date' not in df.columns:
        return None

    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date'])
    df = df.set_index('date').sort_index()

    weekly = df.resample('W').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
    }).dropna()

    if len(weekly) < 20:
        return None

    weekly = weekly.reset_index()
    logger.info("周线 resample 完成: %d 根", len(weekly))
    return weekly


def _is_minute_period(period: str) -> bool:
    """判断是否为分钟/小时级周期（非日线/周线）"""
    return period in _MINUTE_PERIOD_MAP


def _get_strategy_params_for_period(period: str
                                     ) -> Dict[str, Any]:
    """根据周期获取合适的策略参数

    分钟周期使用专用的分钟级参数（MACD 周期按 bar 数缩放），
    日线/周线使用日线参数。
    """
    if period in _MINUTE_STRATEGY_PARAMS:
        return dict(_MINUTE_STRATEGY_PARAMS[period])
    # 日线/周线默认参数（与监控台 _run_pro3_on_data 保持一致）
    return {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,
            'macd_signal': 9, 'base_weight': 0.70}


def _run_pro3_on_data(df: pd.DataFrame) -> Dict:
    """在 DataFrame 上运行 Pro3 策略，返回完整结果"""
    from crypto.market_strategy.pro3_strategy import FuturesPro3Strategy

    strategy = FuturesPro3Strategy(
        df,
        adx_period=9,
        base_weight=0.7,
        trade_multiplier=10.0,
    )
    result = strategy.run()
    return result


def _extract_last_signal(result: Dict, df: pd.DataFrame) -> Dict:
    """从策略结果中提取最后一根K线的信号数据，包括交易记录"""
    signals_df = result.get('signals_df', pd.DataFrame())
    last_row = signals_df.iloc[-1] if not signals_df.empty else {}

    modify_flag = last_row.get('signal', 'hold')
    if modify_flag is None or modify_flag == 'hold':
        modify_flag = 'wait'

    # 趋势方向
    if modify_flag == 'rise':
        action_signal = "上涨"
    elif modify_flag == 'fall':
        action_signal = "下跌"
    else:
        action_signal = "观望"

    # 获取实际数据日期（从 DataFrame 索引取最后一天）
    if hasattr(signals_df.index[-1], 'strftime'):
        data_timestamp = signals_df.index[-1].strftime("%Y-%m-%d %H:%M:%S") if not signals_df.empty else "--"
    else:
        data_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 从交易记录中提取最后一笔交易的入场信息
    trade_log = result.get('trade_log', [])
    trade_entry_price = None
    trade_entry_time = None
    trade_direction = None
    trade_profit_pct = None

    if trade_log:
        last_trade = trade_log[-1]
        trade_entry_price = round(float(last_trade.get('open_price', 0)), 4)
        trade_direction = last_trade.get('direction', None)
        ot = last_trade.get('open_time', None)
        if ot and hasattr(ot, 'strftime'):
            trade_entry_time = ot.strftime("%Y-%m-%d %H:%M:%S")
        elif ot:
            trade_entry_time = str(ot)
        # 计算持仓盈亏（未平仓，10倍杠杆）
        current_price = float(last_row.get('close', 0))
        if current_price > 0 and trade_entry_price > 0:
            if trade_direction == 'Long':
                trade_profit_pct = round(((current_price / trade_entry_price) - 1) * 100 * 10.0, 2)
            elif trade_direction == 'Short':
                trade_profit_pct = round((1 - (current_price / trade_entry_price)) * 100 * 10.0, 2)

    return {
        "price": round(float(last_row.get('close', 0)), 4),
        "action_signal": action_signal,
        "modify_flag": modify_flag,
        "macd_histogram": round(float(last_row.get('MACD', 0)), 4),
        "macd_dif": round(float(last_row.get('DIF', 0)), 4),
        "adx": round(float(last_row.get('ADX', 0)), 4),
        "plus_di": round(float(last_row.get('+DI', 0)), 4),
        "minus_di": round(float(last_row.get('-DI', 0)), 4),
        "smoothed_macd": round(float(last_row.get('smoothed_macd', 0)), 4),
        "hist_weight": round(float(last_row.get('hist_weight', 0)), 4),
        "atr": round(float(last_row.get('ATR_pct', 0)), 4),
        "sar": round(float(last_row.get('SAR', 0)), 4),
        "timestamp": data_timestamp,
        # 交易相关字段（每个周期独立）
        "trade_entry_price": trade_entry_price,
        "trade_entry_time": trade_entry_time,
        "trade_direction": trade_direction,
        "trade_profit_pct": trade_profit_pct,
    }


def _get_realtime_price(code: str) -> float:
    """通过 akshare 实时行情获取最新价格（回退到日线最后收盘价）"""
    from crypto.futures.akshare_api import AkshareFuturesAPI, AkshareAPIError
    try:
        df_rt = AkshareFuturesAPI.fetch_realtime(code)
        if df_rt is not None and not df_rt.empty:
            # 取主力合约（第一行）的最新价
            trade_col = 'trade' if 'trade' in df_rt.columns else None
            if trade_col:
                return float(df_rt.iloc[0][trade_col])
    except (AkshareAPIError, Exception):
        pass
    return None


def calculate_futures_single(code: str) -> Dict:
    """计算单个期货品种的策略数据（监控台用）

    Parameters
    ----------
    code : str
        品种字母代码，如 'RB', 'IF', 'M'

    Returns
    -------
    dict
        包含行情、指标、分析、交易信息的字典
    """
    name_map = get_futures_code_name_map()
    display_name = name_map.get(code, code)

    # 获取日线数据
    end_date = datetime.date.today().strftime('%Y%m%d')
    start_date = (datetime.date.today() - datetime.timedelta(days=365)
                  ).strftime('%Y%m%d')

    df = _fetch_daily_with_retry(code, start_date, end_date)
    if df is None or df.empty:
        raise Exception(f"品种 [{code}] 无法获取日线数据")

    # 运行策略
    result = _run_pro3_on_data(df)
    signal = _extract_last_signal(result, df)

    # 尝试获取实时价格
    rt_price = _get_realtime_price(code)
    if rt_price is not None:
        signal['price'] = round(rt_price, 4)

    # 获取最后一笔交易信息
    trade_log = result.get('trade_log', [])
    last_trade_price = 0.0
    current_profit = 0.0
    trade_time = ""
    hold_time = ""

    if trade_log:
        last_trade = trade_log[-1]
        last_trade_price = last_trade.get('open_price', 0.0)
        trade_time = str(last_trade.get('open_time', ''))

        # 计算持仓盈亏
        price = signal['price']
        if last_trade_price > 0:
            direction = last_trade.get('direction', 'Long')
            if direction == 'Long':
                current_profit = round(
                    ((price / last_trade_price) - 1) * 100 * 10.0, 2)
            else:
                current_profit = round(
                    (1 - (price / last_trade_price)) * 100 * 10.0, 2)

        # 计算持仓时长
        if trade_time:
            try:
                trade_dt = pd.Timestamp(trade_time)
                now = datetime.datetime.now()
                delta = now - trade_dt.to_pydatetime()
                days = delta.days
                hours = delta.seconds // 3600
                mins = (delta.seconds % 3600) // 60
                if days > 0:
                    hold_time = f"{days}天{hours}小时{mins}分"
                else:
                    hold_time = f"{hours}小时{mins}分"
                if isinstance(trade_dt, pd.Timestamp):
                    trade_time = trade_dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                hold_time = ""

    # 综合行情判断
    modify_flag = signal['modify_flag']
    if modify_flag == 'rise':
        overall = f"{display_name} 整体向上运行"
    elif modify_flag == 'fall':
        overall = f"{display_name} 整体向下运行"
    else:
        overall = f"{display_name} 震荡/方向不明"

    return {
        "timestamp": signal['timestamp'],
        "symbol": code,
        "display_name": display_name,
        "price": signal['price'],
        "indicators": {
            "macd": {
                "histogram": signal['macd_histogram'],
                "dif": signal['macd_dif'],
            },
            "adx": {
                "adx": signal['adx'],
                "+di": signal['plus_di'],
                "-di": signal['minus_di'],
            },
            "atr": signal.get('atr', 0),
            "sar": signal.get('sar', 0),
        },
        "analysis": {
            "overall_market": overall,
            "action_signal": signal['action_signal'],
            "modify_flag": signal['modify_flag'],
        },
        "trade_info": {
            "last_trade_price": round(last_trade_price, 4),
            "current_profit": round(current_profit, 2),
            "trade_time": trade_time,
            "hold_time": hold_time,
        },
    }


def calculate_futures_strategy_data() -> Dict:
    """批量获取自选品种的策略数据（监控台用）

    Returns
    -------
    dict
        { "overall_market": "...", "products": [...] }
    """
    selected = get_selected_futures()
    if not selected:
        selected = get_all_futures()[:3]

    results = []
    up_count = 0
    down_count = 0
    wait_count = 0
    error_count = 0

    for code in selected:
        try:
            data = calculate_futures_single(code)
            flag = data['analysis']['modify_flag']
            if flag == 'rise':
                up_count += 1
            elif flag == 'fall':
                down_count += 1
            else:
                wait_count += 1
            results.append(data)
        except Exception as e:
            error_count += 1
            name_map = get_futures_code_name_map()
            display_name = name_map.get(code, code)
            results.append({
                "symbol": code,
                "display_name": display_name,
                "price": 0,
                "indicators": {
                    "macd": {"histogram": 0, "dif": 0},
                    "adx": {"adx": 0, "+di": 0, "-di": 0},
                    "atr": 0,
                    "sar": 0,
                },
                "analysis": {
                    "overall_market": "数据获取失败",
                    "action_signal": "--",
                    "modify_flag": "wait",
                },
                "trade_info": {
                    "last_trade_price": 0,
                    "current_profit": 0,
                    "trade_time": "",
                    "hold_time": "",
                },
                "error": str(e),
            })

    total = len(selected)
    if total == 0:
        total = 1
    if up_count > total * 0.6:
        overall_market = f"期货市场整体向上 (多头:{up_count} 空头:{down_count} 观望:{wait_count} 错误:{error_count})"
    elif down_count > total * 0.6:
        overall_market = f"期货市场整体向下 (多头:{up_count} 空头:{down_count} 观望:{wait_count} 错误:{error_count})"
    else:
        overall_market = f"期货市场走势分化/震荡 (多头:{up_count} 空头:{down_count} 观望:{wait_count} 错误:{error_count})"

    return {
        "overall_market": overall_market,
        "products": results,
    }


def calculate_futures_multi_period(code: str,
                                   periods: List[str] = None) -> Dict:
    """多周期方向总览数据

    数据源策略：
    - 分钟/小时级周期（5m/15m/30m/1H/2H/4H/6H/12H）：使用 akshare
      futures_zh_minute_sina 获取真实分钟K线数据
    - 日线/周线周期（1D/1W）：使用 akshare futures_main_sina 获取日线数据

    每个周期使用独立的策略参数和真实K线数据，确保信号完全独立。

    Parameters
    ----------
    code : str
        品种字母代码
    periods : list of str, optional
        周期列表，默认 ['5m', '15m', '1H', '6H', '1D', '1W']

    Returns
    -------
    dict
        { "5m": {...}, "15m": {...}, ... }
    """
    from crypto.market_strategy.pro3_strategy import FuturesPro3Strategy

    if periods is None:
        periods = ['5m', '15m', '30m', '1H', '4H', '1D']

    # 分离分钟周期和日线周期
    minute_periods = [p for p in periods if _is_minute_period(p)]
    daily_periods = [p for p in periods if not _is_minute_period(p)]

    # ================================================================
    # 日线/周线周期的参数配置
    # ================================================================
    daily_period_config = {
        '1D':  (14, 12,  26,  9, 1095,  0.70),   # 与监控台一致
        '1W':  (14, 12,  26,  9, 1500,  0.70),   # 与监控台一致
    }

    result = {}
    end_date = datetime.date.today()
    end_date_str = end_date.strftime('%Y%m%d')

    # ── 分钟周期：逐个获取真实分钟K线数据 ──
    contract_symbol = None  # 延迟解析，所有分钟周期共用
    for period in minute_periods:
        try:
            # 获取真实分钟K线
            df = _fetch_minute_kline_for_code(code, period, contract_symbol)
            if df is None or df.empty:
                result[period] = {
                    "error": f"[{code}] 无法获取 {period} 分钟K线数据",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            # 获取该周期对应的策略参数
            params = _get_strategy_params_for_period(period)
            min_required = max(20, params['adx_period'] + params['macd_slow'] + 5)

            if len(df) < min_required:
                result[period] = {
                    "error": f"[{code}] 周期 {period} 数据不足 (仅{len(df)}行, 需>{min_required}行)",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            strategy = FuturesPro3Strategy(
                df,
                adx_period=params['adx_period'],
                macd_fast=params['macd_fast'],
                macd_slow=params['macd_slow'],
                macd_signal=params['macd_signal'],
                base_weight=params['base_weight'],
                trade_multiplier=10.0,
            )
            strat_result = strategy.run()
            signal = _extract_last_signal(strat_result, df)

            result[period] = {
                "price": signal['price'],
                "action_signal": signal['action_signal'],
                "modify_flag": signal['modify_flag'],
                "macd_histogram": signal['macd_histogram'],
                "macd_dif": signal.get('macd_dif', 0),
                "adx": signal['adx'],
                "atr": signal.get('atr', 0),
                "sar": signal.get('sar', 0),
                "timestamp": signal['timestamp'],
                "rows_used": len(df),
                # 交易相关字段（每个周期独立，提取自 trade_log）
                "trade_entry_price": signal.get('trade_entry_price'),
                "trade_entry_time": signal.get('trade_entry_time'),
                "trade_direction": signal.get('trade_direction'),
                "trade_profit_pct": signal.get('trade_profit_pct'),
            }
        except Exception as e:
            result[period] = {
                "error": str(e),
                "action_signal": "--",
                "modify_flag": "wait",
            }

    # ── 日线周期：使用原有逻辑（一次性获取，按行数裁剪）──
    if daily_periods:
        # 区分真正的日线周期和需要周线重采样的周期
        real_daily = [p for p in daily_periods if p != '1W']
        weekly_periods = [p for p in daily_periods if p == '1W']

        max_rows = 0
        if real_daily:
            max_rows = max(daily_period_config.get(p, (14, 12, 26, 9, 1095, 0.70))[4]
                           for p in real_daily)

        # 周线需要更多日线数据用于重采样
        weekly_lookback = 0
        if weekly_periods:
            weekly_lookback = WEEKLY_DAILY_LOOKBACK_DAYS

        fetch_days = max(max_rows + 60, weekly_lookback)
        max_start_date = (end_date - datetime.timedelta(days=fetch_days + 60)
                          ).strftime('%Y%m%d')

        full_df = _fetch_daily_with_retry(code, max_start_date, end_date_str)
        if full_df is None or full_df.empty:
            for p in daily_periods:
                result[p] = {
                    "error": f"无法获取 [{code}] 日线数据",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
        else:
            total_rows = len(full_df)
            for period in daily_periods:
                if period == '1W':
                    # ── 周线：从日线重采样 ──
                    df = _resample_daily_to_weekly(full_df)
                    if df is None or df.empty:
                        result[period] = {
                            "error": f"[{code}] 无法从日线重采样周线数据",
                            "action_signal": "--",
                            "modify_flag": "wait",
                        }
                        continue

                    # 周线使用专用参数
                    cfg = daily_period_config.get(period, (14, 12, 26, 9, 1500, 0.70))
                    adx_period, macd_fast, macd_slow, macd_signal, tail_rows, base_weight = cfg

                    if len(df) < max(20, adx_period + macd_slow + 5):
                        result[period] = {
                            "error": f"[{code}] 周期 {period} 数据不足 (仅{len(df)}行, 需>{max(20, adx_period + macd_slow + 5)}行)",
                            "action_signal": "--",
                            "modify_flag": "wait",
                        }
                        continue

                    try:
                        strategy = FuturesPro3Strategy(
                            df,
                            adx_period=adx_period,
                            macd_fast=macd_fast,
                            macd_slow=macd_slow,
                            macd_signal=macd_signal,
                            base_weight=base_weight,
                            trade_multiplier=10.0,
                        )
                        strat_result = strategy.run()
                        signal = _extract_last_signal(strat_result, df)

                        result[period] = {
                            "price": signal['price'],
                            "action_signal": signal['action_signal'],
                            "modify_flag": signal['modify_flag'],
                            "macd_histogram": signal['macd_histogram'],
                            "macd_dif": signal.get('macd_dif', 0),
                            "adx": signal['adx'],
                            "atr": signal.get('atr', 0),
                            "sar": signal.get('sar', 0),
                            "timestamp": signal['timestamp'],
                            "rows_used": len(df),
                            "trade_entry_price": signal.get('trade_entry_price'),
                            "trade_entry_time": signal.get('trade_entry_time'),
                            "trade_direction": signal.get('trade_direction'),
                            "trade_profit_pct": signal.get('trade_profit_pct'),
                        }
                    except Exception as e:
                        result[period] = {
                            "error": str(e),
                            "action_signal": "--",
                            "modify_flag": "wait",
                        }
                    continue

                # ── 普通日线周期 ──
                cfg = daily_period_config.get(
                    period, (14, 12, 26, 9, 1095, 0.70))
                adx_period, macd_fast, macd_slow, macd_signal, tail_rows, base_weight = cfg

                actual_rows = min(tail_rows, total_rows)
                df = full_df.tail(actual_rows).copy()

                if len(df) < max(20, adx_period + macd_slow + 5):
                    result[period] = {
                        "error": f"[{code}] 周期 {period} 数据不足 (仅{len(df)}行, 需>{max(20, adx_period + macd_slow + 5)}行)",
                        "action_signal": "--",
                        "modify_flag": "wait",
                    }
                    continue

                try:
                    strategy = FuturesPro3Strategy(
                        df,
                        adx_period=adx_period,
                        macd_fast=macd_fast,
                        macd_slow=macd_slow,
                        macd_signal=macd_signal,
                        base_weight=base_weight,
                        trade_multiplier=10.0,
                    )
                    strat_result = strategy.run()
                    signal = _extract_last_signal(strat_result, df)

                    result[period] = {
                        "price": signal['price'],
                        "action_signal": signal['action_signal'],
                        "modify_flag": signal['modify_flag'],
                        "macd_histogram": signal['macd_histogram'],
                        "macd_dif": signal.get('macd_dif', 0),
                        "adx": signal['adx'],
                        "atr": signal.get('atr', 0),
                        "sar": signal.get('sar', 0),
                        "timestamp": signal['timestamp'],
                        "rows_used": len(df),
                        "trade_entry_price": signal.get('trade_entry_price'),
                        "trade_entry_time": signal.get('trade_entry_time'),
                        "trade_direction": signal.get('trade_direction'),
                        "trade_profit_pct": signal.get('trade_profit_pct'),
                    }
                except Exception as e:
                    result[period] = {
                        "error": str(e),
                        "action_signal": "--",
                        "modify_flag": "wait",
                    }

    return result


def get_futures_strategy_detail(code: str, bar: str = '1H', lookback: str = '1Y') -> Dict:
    """获取期货品种的完整策略回测详情

    数据源策略：
    - 分钟/小时级 bar（3m/5m/15m/30m/1H/2H/4H/6H/12H）：
      使用 akshare futures_zh_minute_sina 获取真实分钟K线数据
    - 日线/周线 bar（1D/1W）：使用日线数据

    策略核心：调用 crypto.market_strategy.futures_strategy.pro3_singletimeframe.get_strategy_full_data()，
    （crypto Pro3 完整复刻版，自带 akshare 数据层：主力合约解析/resample/周线聚合），
    返回完整的策略数据结构。

    Parameters
    ----------
    code : str
        品种字母代码
    bar : str
        K线周期标识，如 '3m', '5m', '15m', '30m', '1H', '2H', '4H', '6H', '12H', '1D', '1W'
    lookback : str
        回看周期长度，决定数据回看的时间范围：
        - '30D': 回看30天
        - '3M': 回看90天（3个月）
        - '6M': 回看180天（6个月）
        - '1Y': 回看365天（1年，默认）
        - '3Y': 回看1095天（3年）
        - '5Y': 回看1825天（5年）

    Returns
    -------
    dict
        包含 symbol, market, current_position, trade_records, stats 的完整字典
    """
    from crypto.market_strategy.futures_strategy.pro3_singletimeframe import get_strategy_full_data

    name_map = get_futures_code_name_map()
    display_name = name_map.get(code, code)

    # 回看周期 → 天数映射
    lookback_days_map = {
        '30D': 30, '3M': 90, '6M': 180,
        '1Y': 365, '3Y': 1095, '5Y': 1825,
    }
    lookback_days = lookback_days_map.get(lookback, 365)

    # futuresStrategy 新策略自带数据层（内部完成取数/裁剪/指标计算），
    # 统一使用文档标准参数 MACD(12,26,9) + ADX(14)，盈亏×10杠杆
    result = get_strategy_full_data(
        code, bar,
        detail_mode=True,
        lookback_days=lookback_days,
        display_name=display_name,
    )

    return result


# ====================================================================
#  BOLL 限价双周期策略适配
# ====================================================================

def get_futures_boll_strategy_detail(code: str, short_bar: str = '1H',
                                     long_bar: str = '1D',
                                     lookback: str = '1Y') -> Dict:
    """获取期货品种的 BOLL 限价双周期策略回测详情

    调用 strategies.futures_boll_limit_dualtimeframe.get_latest_data()，
    将 BOLL 策略的输出转换为前端详情页兼容的数据结构。

    Parameters
    ----------
    code : str         品种字母代码，如 'RB'
    short_bar : str    短周期（BOLL触边交易），如 '15m','1H','4H'
    long_bar : str     长周期（趋势方向），如 '1D','1W','4H'
    lookback : str     回看范围：'30D','3M','6M','1Y','3Y','5Y'

    Returns
    -------
    dict   包含 symbol, market, current_position, trade_records, stats 等
    """
    from crypto.market_strategy.futures_boll_limit_dualtimeframe import get_latest_data as boll_get_data

    name_map = get_futures_code_name_map()
    display_name = name_map.get(code, code)

    lookback_days_map = {
        '30D': 30, '3M': 90, '6M': 180,
        '1Y': 365, '3Y': 1095, '5Y': 1825,
    }
    backtest_days = lookback_days_map.get(lookback, 365)

    # ── 运行 BOLL 策略 ──
    latest_data, last_trade_data, atr_value, long_direction, \
        df, df_long, metrics, trade_records = boll_get_data(
            symbol=code,
            short_bar=short_bar,
            long_bar=long_bar,
            backtest_days=backtest_days,
            show_original_output=0,
            show_market_output=0,
            show_trade_ops_output=0,
            show_trade_records_output=0,
        )

    # ── 计算 BOLL 布林带（与策略内 backtrader 参数一致）──
    boll_period = 20
    boll_dev = 2.0
    boll_mid = df['close'].rolling(boll_period).mean()
    boll_std = df['close'].rolling(boll_period).std()
    boll_top = boll_mid + boll_dev * boll_std
    boll_bot = boll_mid - boll_dev * boll_std

    # ── 行情概览 ──
    long_dir_text = ('上涨' if long_direction == 'rise'
                     else '下跌' if long_direction == 'fall'
                     else '观望')

    def _safe_round(val, n=4):
        try:
            return round(float(val), n)
        except (TypeError, ValueError):
            return 0.0

    market = {
        'price': _safe_round(latest_data.get('close', 0)),
        'action_signal': long_dir_text,
        'boll_top': _safe_round(boll_top.iloc[-1]) if len(boll_top) > 0 else 0,
        'boll_mid': _safe_round(boll_mid.iloc[-1]) if len(boll_mid) > 0 else 0,
        'boll_bot': _safe_round(boll_bot.iloc[-1]) if len(boll_bot) > 0 else 0,
        'adx': _safe_round(latest_data.get('ADX', 0)),
        'plus_di': _safe_round(latest_data.get('+DI', 0)),
        'minus_di': _safe_round(latest_data.get('-DI', 0)),
        'atr': _safe_round(atr_value),
    }

    # ── 当前持仓状态 ──
    current_position = {
        'status': '空仓',
        'profit_pct': 0,
        'entry_price': 0,
    }
    if last_trade_data is not None:
        trade_price = _safe_round(last_trade_data.get('TRADE_PRICE', 0))
        profit = _safe_round(last_trade_data.get('PROFIT', 0), 2)
        current_position = {
            'status': '末笔已平仓',
            'profit_pct': profit,
            'entry_price': trade_price,
        }

    # ── 交易记录（前端格式）──
    def _fmt_time(t):
        if t is None:
            return ''
        if hasattr(t, 'strftime'):
            return t.strftime('%Y-%m-%d %H:%M:%S')
        return str(t)

    frontend_trades = []
    for i, rec in enumerate(trade_records):
        frontend_trades.append({
            'index': i + 1,
            'direction': '多头' if rec['dir'] == 'rise' else '空头',
            'open_time': _fmt_time(rec.get('entry_time')),
            'open_price': _safe_round(rec.get('open_price', 0)),
            'close_time': _fmt_time(rec.get('exit_time')),
            'close_price': _safe_round(rec.get('close_price', 0)),
            'profit': _safe_round(rec.get('profit', 0), 2),
            'max_win': _safe_round(rec.get('max_win', 0), 2),
            'max_loss': _safe_round(rec.get('max_loss', 0), 2),
            'excluded': bool(rec.get('excluded', False)),
        })

    # ── 统计指标 ──
    stats = {
        'total_trades': metrics.get('total_trades', 0),
        'win_rate': metrics.get('win_rate', 0),
        'winning_trades': metrics.get('winning_trades', 0),
        'losing_trades': metrics.get('losing_trades', 0),
        'excluded_trades': metrics.get('excluded_trades', 0),
        'excluded_total_loss': _safe_round(metrics.get('excluded_total_loss', 0), 2),
        'excluded_avg_loss': _safe_round(metrics.get('excluded_avg_loss', 0), 2),
        'total_profit_pct': _safe_round(metrics.get('total_profit', 0), 2),
        'total_loss_pct': _safe_round(metrics.get('total_loss', 0), 2),
        'avg_profit_pct': _safe_round(metrics.get('avg_profit', 0), 2),
        'avg_loss_pct': _safe_round(metrics.get('avg_loss', 0), 2),
        'max_drawdown_pct': _safe_round(metrics.get('max_drawdown', 0), 2),
        'profit_factor': _safe_round(metrics.get('profit_factor', 0), 2),
        'calmar_ratio': _safe_round(metrics.get('calmar_ratio', 0), 2),
        'annual_return': _safe_round(metrics.get('annual_return', 0), 2),
        'fix_money': _safe_round(metrics.get('fix_final', 0), 2),
        'contract_money': _safe_round(metrics.get('contract_final', 0), 2),
        'mix_money': _safe_round(metrics.get('mix_final', 0), 2),
        'profit_capture_rate': _safe_round(metrics.get('profit_capture_rate', 0), 4),
        'loss_capture_rate': _safe_round(metrics.get('loss_capture_rate', 0), 4),
        'hold_pl_ratio': _safe_round(metrics.get('hold_pl_ratio', 0), 2),
        'hold_sum_ratio': _safe_round(metrics.get('hold_sum_ratio', 0), 2),
        'mean_pl_ratio': _safe_round(metrics.get('mean_pl_ratio', 0), 2),
        'sum_pl_ratio': _safe_round(metrics.get('sum_pl_ratio', 0), 2),
    }

    # ── 时间信息 ──
    total_days = metrics.get('total_days', 0)
    daily_profit = _safe_round(metrics.get('daily_profit', 0), 2)
    weekly_profit = _safe_round(metrics.get('weekly_profit', 0), 2)

    start_time = ''
    end_time = ''
    included = [r for r in trade_records if not r.get('excluded', False)]
    if included:
        times_in = [r.get('entry_time') for r in included if r.get('entry_time')]
        times_out = [r.get('exit_time') for r in included if r.get('exit_time')]
        if times_in:
            start_time = _fmt_time(min(times_in))
        if times_out:
            end_time = _fmt_time(max(times_out))

    return {
        'symbol': code,
        'display_name': display_name,
        'strategy': 'boll_limit',
        'short_bar': short_bar,
        'long_bar': long_bar,
        'market': market,
        'current_position': current_position,
        'trade_records': frontend_trades,
        'stats': stats,
        'start_time': start_time,
        'end_time': end_time,
        'total_days': total_days,
        'daily_profit': daily_profit,
        'weekly_profit': weekly_profit,
    }


# ====================================================================
#  双周期 Pro3 策略适配器
# ====================================================================

def _fetch_period_df(code: str, bar: str, lookback_days: int,
                     today=None) -> 'pd.DataFrame':
    """获取指定周期的K线数据（供双周期策略使用）

    统一封装分钟K线和日线/周线的数据获取逻辑。
    """
    if today is None:
        today = datetime.date.today()

    if _is_minute_period(bar):
        # 分钟K线
        df = _fetch_minute_kline_for_code(code, bar)
        if df is None or df.empty:
            raise Exception(f"品种 [{code}] 无法获取 {bar} 分钟K线数据")
        if lookback_days and 'date' in df.columns:
            cutoff_date = today - datetime.timedelta(days=lookback_days)
            try:
                mask = pd.to_datetime(df['date']) >= pd.Timestamp(cutoff_date)
                df = df[mask].copy()
            except Exception:
                pass
    else:
        # 日线/周线
        bar_rows_map = {'1D': 1095, '1W': 1500}
        bar_rows = bar_rows_map.get(bar, 1095)
        fetch_days = max(bar_rows + 60, lookback_days + 30)
        if bar == '1W':
            fetch_days = max(WEEKLY_DAILY_LOOKBACK_DAYS, fetch_days)
        end_date = today.strftime('%Y%m%d')
        start_date = (today - datetime.timedelta(days=fetch_days)).strftime('%Y%m%d')

        df = _fetch_daily_with_retry(code, start_date, end_date)
        if df is None or df.empty:
            raise Exception(f"品种 [{code}] 无法获取日线数据")

        if bar == '1W':
            df = _resample_daily_to_weekly(df)
            if df is None or df.empty:
                raise Exception(f"品种 [{code}] 无法从日线重采样周线数据")
        else:
            actual_rows = min(bar_rows, len(df))
            df = df.tail(actual_rows).copy()

        if lookback_days and 'date' in df.columns:
            cutoff_date = today - datetime.timedelta(days=lookback_days)
            try:
                mask = pd.to_datetime(df['date']) >= pd.Timestamp(cutoff_date)
                df = df[mask].copy()
            except Exception:
                pass

    if df is None or df.empty:
        raise Exception(f"品种 [{code}] {bar} 周期数据为空")

    return df


def get_futures_dual_pro3_strategy_detail(code: str, short_bar: str = '1H',
                                          long_bar: str = '1D',
                                          lookback: str = '1Y') -> Dict:
    """获取期货品种的双周期 Pro3 策略回测详情

    核心算法（crypto pro3_dualtimeframe 完整复刻）：
    - 长周期 justice_flag 状态机（待涨/待跌 + DIF零轴确认）判断趋势方向
    - 短周期信号 + 开仓限价单机制（open价挂单/TTL 4根bar/反向撤单）
    - 无期末强平，年化按 365 天回测跨度计算

    Parameters
    ----------
    code : str         品种字母代码，如 'RB'
    short_bar : str    短周期（交易信号），如 '5m','15m','1H'
    long_bar : str     长周期（趋势方向），如 '4H','1D','1W'
    lookback : str     回看范围：'30D','3M','6M','1Y','3Y','5Y'

    Returns
    -------
    dict   包含 symbol, market, current_position, trade_records, stats 等
    """
    from crypto.market_strategy.futures_strategy.pro3_dualtimeframe import get_strategy_full_data_dual

    name_map = get_futures_code_name_map()
    display_name = name_map.get(code, code)

    lookback_days_map = {
        '30D': 30, '3M': 90, '6M': 180,
        '1Y': 365, '3Y': 1095, '5Y': 1825,
    }
    lookback_days = lookback_days_map.get(lookback, 365)

    # futuresStrategy 新策略自带数据层（短/长周期取数、merge_asof 对齐、截断）
    result = get_strategy_full_data_dual(
        code,
        short_bar=short_bar,
        long_bar=long_bar,
        lookback_days=lookback_days,
        display_name=display_name,
    )

    return result


# ====================================================================
#  命令行快速测试
# ====================================================================
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(message)s'
    )

    print("=" * 60)
    print("  期货策略数据适配器 - 快速测试")
    print("=" * 60)

    # 1. 测试品种分类
    cats = get_futures_categories()
    print(f"\n品种分类: {len(cats)} 个类别, 共 {len(get_all_futures())} 个品种")
    for cat, items in cats.items():
        print(f"  {cat}: {len(items)} 个品种")

    # 2. 测试单品种策略
    try:
        print("\n测试品种策略 (RB 螺纹钢)...")
        data = calculate_futures_single('RB')
        print(f"  品种: {data['display_name']}")
        print(f"  价格: {data['price']}")
        print(f"  方向: {data['analysis']['action_signal']}")
        print(f"  ADX: {data['indicators']['adx']['adx']}")
        print(f"  MACD: {data['indicators']['macd']['histogram']}")
    except Exception as e:
        print(f"  测试失败: {e}")

    # 3. 测试详情
    try:
        print("\n测试策略详情 (RB 螺纹钢)...")
        detail = get_futures_strategy_detail('RB')
        print(f"  交易次数: {detail['stats']['total_trades']}")
        print(f"  胜率: {detail['stats']['win_rate']:.2%}")
        print(f"  净利润: {detail['stats']['total_return_pct']:.2f}%")
    except Exception as e:
        print(f"  测试失败: {e}")
