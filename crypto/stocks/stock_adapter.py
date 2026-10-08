#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 3 自动搬运（2026-10-08）。
# 原名: stocks/stock_adapter.py  →  现位置: crypto/stocks/stock_adapter.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底与根级 config 依赖已移除。
"""
股票策略数据适配器
==================
封装 akshare 股票 API，提供与 futures/futures_adapter.py
相同接口风格的数据获取函数，为股票 Web 页面提供后端数据支撑。

主要功能：
  - 品种数据管理（自选股配置）
  - 日线/分钟K线数据获取（带重试机制）
  - 多周期信号分析（MACD、RSI等技术指标）
  - 标准化数据输出，与期货模块格式对齐
  - 资金流向、财务指标等扩展数据查询

已集成 AkshareStockAPI 扩展功能（通过 akshare_api.py）：
  - get_all_stock_codes()：全市场A股列表
  - get_stock_list_by_market()：各交易所股票列表
  - get_industry_board_ths()：同花顺行业板块
  - get_concept_board_ths()：同花顺概念板块
  - get_stock_info()：股票基本信息（含降级策略）
  - get_company_profile()：公司详细资料
  - get_share_change()：历史股本变动/分红送转
  - get_financial_report()：财务指标
  - get_individual_fund_flow()：个股资金流向
  - get_lhb_detail()：龙虎榜明细
  - get_shareholder_count_detail()：股东户数变化
  - get_research_reports()：机构研报
  - get_profit_forecast()：业绩预告
  - get_profit_express()：业绩快报
  - get_new_ipo_list()：新股发行列表
  - get_margin_account_statistics()：融资融券统计
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import datetime
import json
import logging
import os
import time
from typing import Dict, List, Optional, Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ====================================================================
#  路径与配置
# ====================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "stock_config.json")

# ====================================================================
#  品种数据管理
# ====================================================================

# 常用A股分类（按行业板块）
_STOCK_CATEGORIES: Dict[str, List[Dict]] = {
    "白酒": [
        {"code": "600519", "name": "贵州茅台"},
        {"code": "000858", "name": "五粮液"},
        {"code": "000568", "name": "泸州老窖"},
        {"code": "600809", "name": "山西汾酒"},
        {"code": "002304", "name": "洋河股份"},
    ],
    "金融": [
        {"code": "601318", "name": "中国平安"},
        {"code": "600036", "name": "招商银行"},
        {"code": "601166", "name": "兴业银行"},
        {"code": "600030", "name": "中信证券"},
        {"code": "000001", "name": "平安银行"},
        {"code": "300059", "name": "东方财富"},
    ],
    "科技": [
        {"code": "002415", "name": "海康威视"},
        {"code": "002475", "name": "立讯精密"},
        {"code": "002230", "name": "科大讯飞"},
        {"code": "002371", "name": "北方华创"},
        {"code": "600703", "name": "三安光电"},
    ],
    "新能源": [
        {"code": "300750", "name": "宁德时代"},
        {"code": "300274", "name": "阳光电源"},
        {"code": "600438", "name": "通威股份"},
        {"code": "601012", "name": "隆基绿能"},
    ],
    "医药": [
        {"code": "600276", "name": "恒瑞医药"},
        {"code": "300760", "name": "迈瑞医疗"},
        {"code": "603259", "name": "药明康德"},
        {"code": "300015", "name": "爱尔眼科"},
        {"code": "600196", "name": "复星医药"},
    ],
    "消费": [
        {"code": "600887", "name": "伊利股份"},
        {"code": "000333", "name": "美的集团"},
        {"code": "000651", "name": "格力电器"},
        {"code": "002714", "name": "牧原股份"},
        {"code": "600690", "name": "海尔智家"},
    ],
}

# 展平为 {code: name} 映射
_ALL_STOCKS_MAP: Dict[str, str] = {}
for cat_list in _STOCK_CATEGORIES.values():
    for item in cat_list:
        _ALL_STOCKS_MAP[item["code"]] = item["name"]


def get_stock_categories() -> Dict[str, List[Dict]]:
    """获取按行业分组的股票品种数据

    Returns
    -------
    dict
        { "白酒": [{"code": "600519", "name": "贵州茅台"}, ...], ... }
    """
    return dict(_STOCK_CATEGORIES)


def get_all_stocks() -> List[str]:
    """返回全部股票代码列表"""
    return list(_ALL_STOCKS_MAP.keys())


def get_stock_code_name_map() -> Dict[str, str]:
    """返回 {代码: 股票名} 映射"""
    return dict(_ALL_STOCKS_MAP)


# ====================================================================
#  自选股配置管理
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


def get_selected_stocks() -> List[str]:
    """获取当前自选股列表"""
    config = _load_config()
    selected = config.get("selected", [])
    all_codes = get_all_stocks()
    return [c for c in selected if c in all_codes]


def set_selected_stocks(codes: List[str]):
    """保存自选股列表"""
    all_codes = get_all_stocks()
    valid = [c for c in codes if c in all_codes]
    config = _load_config()
    config["selected"] = valid
    _save_config(config)


# ====================================================================
#  技术指标工具函数
# ====================================================================

def _calc_macd(df: pd.DataFrame,
               fast: int = 12, slow: int = 26, signal: int = 9
               ) -> pd.DataFrame:
    """计算 MACD 指标

    返回 DataFrame，包含 DIF, DEA, MACD 列（MACD = 柱状图 = DIF - DEA）
    """
    close = df['close']
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd = 2 * (dif - dea)  # 柱状图（与通达信等软件一致）

    df = df.copy()
    df['DIF'] = dif
    df['DEA'] = dea
    df['MACD'] = macd
    return df


def _calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算 RSI 指标"""
    close = df['close']
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float('nan'))
    rsi = 100 - (100 / (1 + rs))

    df = df.copy()
    df['RSI'] = rsi
    return df


def _calc_ma(df: pd.DataFrame, periods: List[int] = [5, 10, 20, 60]
             ) -> pd.DataFrame:
    """计算移动平均线"""
    df = df.copy()
    for p in periods:
        df[f'MA{p}'] = df['close'].rolling(window=p).mean()
    return df


def _calc_bollinger(df: pd.DataFrame, period: int = 20,
                    std_dev: float = 2.0) -> pd.DataFrame:
    """计算布林带"""
    df = df.copy()
    df['BOLL_MID'] = df['close'].rolling(window=period).mean()
    df['BOLL_STD'] = df['close'].rolling(window=period).std()
    df['BOLL_UP'] = df['BOLL_MID'] + std_dev * df['BOLL_STD']
    df['BOLL_DN'] = df['BOLL_MID'] - std_dev * df['BOLL_STD']
    return df


# ====================================================================
#  数据获取（带重试）
# ====================================================================

MAX_RETRIES = 2
RETRY_DELAY = 1.5


def _fetch_daily_with_retry(code: str,
                            start_date: str = None,
                            end_date: str = None,
                            adjust: str = 'qfq',
                            max_retries: int = MAX_RETRIES
                            ) -> Optional[pd.DataFrame]:
    """获取日线数据（带重试）

    先尝试新浪数据源，失败后自动回退腾讯数据源。
    """
    from crypto.stocks.stock_akshare_api import AkshareStockAPI, AkshareAPIError

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            df = AkshareStockAPI.fetch_daily(
                code, start_date, end_date, adjust=adjust)
            if df is not None and not df.empty:
                return df
        except (AkshareAPIError, Exception) as e:
            last_error = e
            if attempt < max_retries:
                delay = RETRY_DELAY * (attempt + 1)
                logger.warning("[%s] 第%d次重试 (%s)", code, attempt + 1, e)
                time.sleep(delay)
                continue

    logger.warning("[%s] 日线数据获取失败: %s", code, last_error)
    return None


def _fetch_minute_with_retry(code: str, period: str = '5m',
                             start_date: str = None,
                             end_date: str = None,
                             adjust: str = 'qfq',
                             max_retries: int = MAX_RETRIES
                             ) -> Optional[pd.DataFrame]:
    """获取分钟K线数据（带重试）"""
    from crypto.stocks.stock_akshare_api import AkshareStockAPI, AkshareAPIError

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            df = AkshareStockAPI.fetch_minute_kline(
                code, period=period,
                start_date=start_date, end_date=end_date,
                adjust=adjust)
            if df is not None and not df.empty:
                return df
        except (AkshareAPIError, Exception) as e:
            last_error = e
            if attempt < max_retries:
                delay = RETRY_DELAY * (attempt + 1)
                logger.warning("[%s] %s 第%d次重试 (%s)",
                               code, period, attempt + 1, e)
                time.sleep(delay)
                continue

    logger.warning("[%s] %s 分钟K线获取失败: %s", code, period, last_error)
    return None


# ====================================================================
#  分钟周期配置
# ====================================================================

_MINUTE_PERIODS = ['1m', '5m', '15m', '30m', '60m', '1H', '4H']

_MINUTE_INDICATOR_PARAMS = {
    '1m':  {'macd_fast': 24, 'macd_slow': 52, 'macd_signal': 18, 'rsi_period': 14},
    '5m':  {'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,  'rsi_period': 14},
    '15m': {'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,  'rsi_period': 14},
    '30m': {'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,  'rsi_period': 14},
    '60m': {'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,  'rsi_period': 14},
    '1H':  {'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,  'rsi_period': 14},
    '4H':  {'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,  'rsi_period': 14},
}

_DAILY_INDICATOR_PARAMS = {
    'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9, 'rsi_period': 14,
}


def _is_minute_period(period: str) -> bool:
    """判断是否为分钟周期"""
    return period in _MINUTE_PERIODS


# ====================================================================
#  信号生成
# ====================================================================

def _generate_signal_from_macd(macd_val: float, dif_val: float,
                                dea_val: float) -> str:
    """基于 MACD 生成交易信号

    Returns
    -------
    str
        'rise'(上涨), 'fall'(下跌), 'wait'(观望)
    """
    if macd_val > 0 and dif_val > dea_val:
        return 'rise'
    elif macd_val < 0 and dif_val < dea_val:
        return 'fall'
    elif macd_val > 0 and dif_val > 0:
        return 'rise'
    elif macd_val < 0 and dif_val < 0:
        return 'fall'
    return 'wait'


def _generate_signal_from_rsi(rsi_val: float) -> str:
    """基于 RSI 生成信号"""
    if rsi_val is None or pd.isna(rsi_val):
        return 'wait'
    if rsi_val > 70:
        return 'fall'    # 超买
    elif rsi_val < 30:
        return 'rise'    # 超卖
    return 'wait'


def _get_last_signal(df: pd.DataFrame, params: Dict = None
                     ) -> Dict[str, Any]:
    """从已计算指标的 DataFrame 中提取最后一行的信号

    Parameters
    ----------
    df : pd.DataFrame
        已包含 MACD, RSI 等指标列的 DataFrame
    params : dict, optional
        指标参数（仅用于日志）

    Returns
    -------
    dict
        {price, action_signal, modify_flag, macd, rsi, timestamp, ...}
    """
    if df is None or df.empty:
        return {
            "price": 0,
            "action_signal": "--",
            "modify_flag": "wait",
            "macd": 0,
            "dif": 0,
            "rsi": 0,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    last = df.iloc[-1]

    # MACD 信号
    macd_val = float(last.get('MACD', 0))
    dif_val = float(last.get('DIF', 0))
    dea_val = float(last.get('DEA', 0))
    macd_signal = _generate_signal_from_macd(macd_val, dif_val, dea_val)

    # RSI 信号
    rsi_val = float(last.get('RSI', 50))
    rsi_signal = _generate_signal_from_rsi(rsi_val)

    # 综合信号（MACD 优先）
    if macd_signal == 'rise' or rsi_signal == 'rise':
        modify_flag = 'rise'
    elif macd_signal == 'fall' or rsi_signal == 'fall':
        modify_flag = 'fall'
    else:
        modify_flag = 'wait'

    if modify_flag == 'rise':
        action_signal = "上涨"
    elif modify_flag == 'fall':
        action_signal = "下跌"
    else:
        action_signal = "观望"

    # 时间戳
    dt_val = last.get('date')
    if hasattr(dt_val, 'strftime'):
        timestamp = dt_val.strftime("%Y-%m-%d %H:%M:%S")
    else:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return {
        "price": round(float(last.get('close', 0)), 4),
        "action_signal": action_signal,
        "modify_flag": modify_flag,
        "macd": round(macd_val, 4),
        "macd_dif": round(dif_val, 4),
        "macd_dea": round(dea_val, 4),
        "rsi": round(rsi_val, 2),
        "ma5": round(float(last.get('MA5', 0)), 4),
        "ma20": round(float(last.get('MA20', 0)), 4),
        "timestamp": timestamp,
    }


def _run_indicators(df: pd.DataFrame, params: Dict = None) -> pd.DataFrame:
    """在 DataFrame 上运行全部技术指标

    Parameters
    ----------
    df : pd.DataFrame
        必须包含 ['date', 'open', 'high', 'low', 'close', 'volume'] 列
    params : dict, optional
        可覆盖指标参数，如 {'macd_fast': 12, 'macd_slow': 26, ...}

    Returns
    -------
    pd.DataFrame
        附加指标列后的 DataFrame
    """
    if df is None or df.empty:
        return df

    p = params or _DAILY_INDICATOR_PARAMS

    df = _calc_macd(df,
                    fast=p.get('macd_fast', 12),
                    slow=p.get('macd_slow', 26),
                    signal=p.get('macd_signal', 9))
    df = _calc_rsi(df, period=p.get('rsi_period', 14))
    df = _calc_ma(df, periods=[5, 10, 20, 60])
    df = _calc_bollinger(df, period=20, std_dev=2.0)
    return df


# ====================================================================
#  单品种分析
# ====================================================================

def calculate_stock_single(code: str, adjust: str = 'qfq') -> Dict:
    """计算单只股票的策略分析数据

    Parameters
    ----------
    code : str
        股票代码，如 '600519', '000001'
    adjust : str
        复权类型

    Returns
    -------
    dict
        包含行情、指标、分析信息的字典
    """
    name_map = get_stock_code_name_map()
    display_name = name_map.get(code, code)

    # 获取日线数据（近1年）
    end_date = datetime.date.today().strftime('%Y%m%d')
    start_date = (datetime.date.today() - datetime.timedelta(days=365)
                  ).strftime('%Y%m%d')

    df = _fetch_daily_with_retry(code, start_date, end_date, adjust=adjust)
    if df is None or df.empty:
        raise Exception(f"股票 [{code}] 无法获取日线数据")

    # 计算指标
    df = _run_indicators(df)
    signal = _get_last_signal(df)

    # 综合行情判断
    modify_flag = signal['modify_flag']
    if modify_flag == 'rise':
        overall = f"{display_name} 技术面偏多"
    elif modify_flag == 'fall':
        overall = f"{display_name} 技术面偏空"
    else:
        overall = f"{display_name} 震荡/方向不明"

    return {
        "timestamp": signal['timestamp'],
        "symbol": code,
        "display_name": display_name,
        "price": signal['price'],
        "indicators": {
            "macd": {
                "histogram": signal['macd'],
                "dif": signal['macd_dif'],
                "dea": signal['macd_dea'],
            },
            "rsi": signal['rsi'],
            "ma": {
                "ma5": signal['ma5'],
                "ma20": signal['ma20'],
            },
        },
        "analysis": {
            "overall_market": overall,
            "action_signal": signal['action_signal'],
            "modify_flag": signal['modify_flag'],
        },
    }


def calculate_stock_strategy_data() -> Dict:
    """批量获取自选股的策略分析数据（监控台用）

    Returns
    -------
    dict
        { "overall_market": "...", "products": [...] }
    """
    selected = get_selected_stocks()
    if not selected:
        # 默认返回前6只股票
        all_stocks = get_all_stocks()
        selected = all_stocks[:6]

    results = []
    up_count = 0
    down_count = 0
    wait_count = 0
    error_count = 0

    for code in selected:
        try:
            data = calculate_stock_single(code)
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
            name_map = get_stock_code_name_map()
            display_name = name_map.get(code, code)
            results.append({
                "symbol": code,
                "display_name": display_name,
                "price": 0,
                "indicators": {
                    "macd": {"histogram": 0, "dif": 0, "dea": 0},
                    "rsi": 0,
                    "ma": {"ma5": 0, "ma20": 0},
                },
                "analysis": {
                    "overall_market": "数据获取失败",
                    "action_signal": "--",
                    "modify_flag": "wait",
                },
                "error": str(e),
            })

    total = len(selected)
    if total == 0:
        total = 1

    if up_count > total * 0.6:
        overall_market = (f"股票池整体偏多 "
                          f"(看多:{up_count} 看空:{down_count} "
                          f"观望:{wait_count} 错误:{error_count})")
    elif down_count > total * 0.6:
        overall_market = (f"股票池整体偏空 "
                          f"(看多:{up_count} 看空:{down_count} "
                          f"观望:{wait_count} 错误:{error_count})")
    else:
        overall_market = (f"股票池走势分化/震荡 "
                          f"(看多:{up_count} 看空:{down_count} "
                          f"观望:{wait_count} 错误:{error_count})")

    return {
        "overall_market": overall_market,
        "products": results,
    }


# ====================================================================
#  多周期分析
# ====================================================================

def calculate_stock_multi_period(code: str,
                                 periods: List[str] = None) -> Dict:
    """多周期方向总览数据

    每个周期使用独立的参数和真实K线数据，确保信号完全独立。

    Parameters
    ----------
    code : str
        股票代码
    periods : list of str, optional
        周期列表，默认 ['5m', '15m', '30m', '1H', '4H', '1D']

    Returns
    -------
    dict
        { "5m": {...}, "15m": {...}, ... }
    """
    if periods is None:
        periods = ['5m', '15m', '30m', '1H', '4H', '1D']

    minute_periods = [p for p in periods if _is_minute_period(p)]
    daily_periods = [p for p in periods if not _is_minute_period(p)]

    # 日线回看天数
    _daily_lookback = {
        'daily': 365,
        '1D': 365,
    }

    result = {}
    today = datetime.date.today()
    today_str = today.strftime('%Y%m%d')

    # ── 分钟周期 ──
    for period in minute_periods:
        try:
            params = _MINUTE_INDICATOR_PARAMS.get(
                period, _MINUTE_INDICATOR_PARAMS['5m'])

            # 计算分钟数据获取天数（长周期需要更多数据）
            minute_lookback_days = {
                '1m': 3,
                '5m': 5,
                '15m': 10,
                '30m': 15,
                '60m': 30,
                '1H': 30,
                '4H': 60,
            }
            lookback_d = minute_lookback_days.get(period, 5)

            # 4H 周期：获取 60m 数据后 resample
            if period == '4H':
                fetch_period = '60m'
                resample_factor = 4
            else:
                fetch_period = period
                resample_factor = None

            start_dt = (today - datetime.timedelta(days=lookback_d)).strftime('%Y%m%d')
            df = _fetch_minute_with_retry(
                code, period=fetch_period,
                start_date=start_dt, end_date=today_str,
                adjust='qfq')

            if df is None or df.empty:
                result[period] = {
                    "error": f"[{code}] 无法获取 {period} 分钟K线数据",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            # 4H resample
            if resample_factor and not df.empty and 'date' in df.columns:
                df = df.set_index('date')
                df = df.resample(f'{resample_factor}H').agg({
                    'open': 'first', 'high': 'max', 'low': 'min',
                    'close': 'last', 'volume': 'sum', 'amount': 'sum',
                }).dropna().reset_index()

            if len(df) < 30:
                result[period] = {
                    "error": f"[{code}] {period} 数据不足 (仅{len(df)}行)",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            df = _run_indicators(df, params)
            signal = _get_last_signal(df)

            result[period] = {
                "price": signal['price'],
                "action_signal": signal['action_signal'],
                "modify_flag": signal['modify_flag'],
                "macd": signal['macd'],
                "rsi": signal['rsi'],
                "timestamp": signal['timestamp'],
                "rows_used": len(df),
            }
        except Exception as e:
            result[period] = {
                "error": str(e),
                "action_signal": "--",
                "modify_flag": "wait",
            }

    # ── 日线周期 ──
    for period in daily_periods:
        try:
            lookback_days = _daily_lookback.get(period, 365)

            start_date = (today - datetime.timedelta(
                days=lookback_days)).strftime('%Y%m%d')

            df = _fetch_daily_with_retry(
                code, start_date, today_str, adjust='qfq')

            if df is None or df.empty:
                result[period] = {
                    "error": f"[{code}] 无法获取 {period} 数据",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            params = _DAILY_INDICATOR_PARAMS

            if len(df) < 20:
                result[period] = {
                    "error": f"[{code}] {period} 数据不足 (仅{len(df)}行)",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            df = _run_indicators(df, params)
            signal = _get_last_signal(df)

            result[period] = {
                "price": signal['price'],
                "action_signal": signal['action_signal'],
                "modify_flag": signal['modify_flag'],
                "macd": signal['macd'],
                "rsi": signal['rsi'],
                "timestamp": signal['timestamp'],
                "rows_used": len(df),
            }
        except Exception as e:
            result[period] = {
                "error": str(e),
                "action_signal": "--",
                "modify_flag": "wait",
            }

    return result


# ====================================================================
#  完整策略详情
# ====================================================================

def get_stock_strategy_detail(code: str, bar: str = 'daily',
                              lookback: str = '1Y') -> Dict:
    """获取股票的完整策略回测详情

    Parameters
    ----------
    code : str
        股票代码
    bar : str
        K线周期：'5m', '15m', '30m', '60m', 'daily', 'weekly'
    lookback : str
        回看周期：'30D', '3M', '6M', '1Y', '3Y', '5Y'

    Returns
    -------
    dict
        包含 market, current_position, trade_records, stats 的完整字典
    """
    name_map = get_stock_code_name_map()
    display_name = name_map.get(code, code)

    lookback_days_map = {
        '30D': 30, '3M': 90, '6M': 180,
        '1Y': 365, '3Y': 1095, '5Y': 1825,
    }
    lookback_days = lookback_days_map.get(lookback, 365)

    today = datetime.date.today()
    start_date = (today - datetime.timedelta(
        days=lookback_days + 30)).strftime('%Y%m%d')
    end_date = today.strftime('%Y%m%d')

    if _is_minute_period(bar):
        # 分钟K线
        df = _fetch_minute_with_retry(
            code, period=bar,
            start_date=start_date, end_date=end_date,
            adjust='qfq')
        params = _MINUTE_INDICATOR_PARAMS.get(
            bar, _MINUTE_INDICATOR_PARAMS['5m'])
    else:
        # 日线/周线
        df = _fetch_daily_with_retry(code, start_date, end_date, adjust='qfq')
        if bar == 'weekly' and df is not None and not df.empty:
            df = df.set_index('date')
            df = df.resample('W').agg({
                'open': 'first', 'high': 'max', 'low': 'min',
                'close': 'last', 'volume': 'sum', 'amount': 'sum',
            }).dropna().reset_index()
        params = _DAILY_INDICATOR_PARAMS

    if df is None or df.empty:
        raise Exception(f"股票 [{code}] 无法获取 {bar} 数据")

    # 计算指标
    df = _run_indicators(df, params)
    signal = _get_last_signal(df)

    # 行情概览
    last_close = float(df.iloc[-1]['close']) if not df.empty else 0

    market = {
        "price": signal['price'],
        "action_signal": signal['action_signal'],
        "macd_histogram": signal['macd'],
        "macd_dif": signal['macd_dif'],
        "macd_dea": signal['macd_dea'],
        "rsi": signal['rsi'],
        "ma5": signal['ma5'],
        "ma20": signal['ma20'],
    }

    # 时间范围
    if 'date' in df.columns:
        dates = pd.to_datetime(df['date'], errors='coerce')
        start_time = dates.min()
        end_time = dates.max()
    else:
        start_time = today
        end_time = today

    start_time_str = (start_time.strftime('%Y-%m-%d')
                      if hasattr(start_time, 'strftime') else str(start_time))
    end_time_str = (end_time.strftime('%Y-%m-%d')
                    if hasattr(end_time, 'strftime') else str(end_time))
    total_days = (end_time - start_time).days if hasattr(
        end_time, '__sub__') else lookback_days
    if total_days < 1:
        total_days = 1

    return {
        "symbol": code,
        "display_name": display_name,
        "market": market,
        "current_position": {
            "status": "--",
            "profit_pct": 0,
            "entry_price": 0,
        },
        "trade_records": [],
        "stats": {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0,
            "avg_profit_pct": 0,
            "avg_loss_pct": 0,
            "profit_loss_ratio": 0,
            "max_drawdown_pct": 0,
            "total_return_pct": 0,
            "total_profit_pct": 0,
            "total_loss_pct": 0,
            "fix_money": 100,
            "contract_money": 100,
            "mix_money": 100,
        },
        "start_time": start_time_str,
        "end_time": end_time_str,
        "total_days": total_days,
        "daily_profit": 0,
        "weekly_profit": 0,
    }


# ====================================================================
#  Pro3 策略适配函数
# ====================================================================

# Pro3 分钟周期策略参数（对应 _MINUTE_PERIODS）
_PRO3_MINUTE_PARAMS = {
    '1m':  {'adx_period': 9, 'macd_fast': 50, 'macd_slow': 100, 'macd_signal': 30, 'base_weight': 0.65},
    '5m':  {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.68},
    '15m': {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.70},
    '30m': {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.70},
    '60m': {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.70},
    '1H':  {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.70},
    '4H':  {'adx_period': 9, 'macd_fast': 12, 'macd_slow': 26,  'macd_signal': 9,  'base_weight': 0.74},
}

# Pro3 日线/周线周期参数
_PRO3_DAILY_PARAMS = {
    'daily': {'adx_period': 28, 'macd_fast': 34, 'macd_slow': 72, 'macd_signal': 14, 'base_weight': 0.78},
    'weekly': {'adx_period': 35, 'macd_fast': 55, 'macd_slow': 120, 'macd_signal': 18, 'base_weight': 0.80},
}


def _run_stock_pro3_on_data(df: pd.DataFrame,
                             long_only: bool = True,
                             adx_period: int = 28,
                             macd_fast: int = 34,
                             macd_slow: int = 72,
                             macd_signal: int = 14,
                             base_weight: float = 0.78) -> Dict:
    """在 DataFrame 上运行 Pro3 策略，返回完整结果

    Parameters
    ----------
    df : pd.DataFrame
        行情数据，必须含 'high', 'low', 'close'。
    long_only : bool
        仅做多模式，默认 True 适合 A 股。
    adx_period, macd_fast, macd_slow, macd_signal : int
        Pro3 策略参数，默认使用日线优化参数。
    base_weight : float
        自适应权重基础值。

    Returns
    -------
    dict
        策略结果，包含 trade_log, summary, signals_df。
    """
    from crypto.market_strategy.pro3_strategy import StockPro3Strategy

    strategy = StockPro3Strategy(
        df,
        adx_period=adx_period,
        macd_fast=macd_fast,
        macd_slow=macd_slow,
        macd_signal=macd_signal,
        base_weight=base_weight,
        trade_multiplier=10.0,
        long_only=long_only,
    )
    return strategy.run()


def _extract_stock_pro3_signal(result: Dict, df: pd.DataFrame) -> Dict:
    """从 Pro3 策略结果中提取最后一根K线的信号数据

    Parameters
    ----------
    result : dict
        策略运行结果，含 'signals_df', 'trade_log', 'summary'。
    df : pd.DataFrame
        原始行情数据（用于回退时间戳）。

    Returns
    -------
    dict
        行情信号字典，包含 price, action_signal, macd, adx 等字段。
    """
    signals_df = result.get('signals_df', pd.DataFrame())
    last_row = signals_df.iloc[-1] if not signals_df.empty else {}

    modify_flag = last_row.get('signal', 'hold')
    if modify_flag is None or modify_flag == 'hold':
        modify_flag = 'wait'

    action_map = {'rise': '上涨', 'fall': '下跌', 'wait': '观望'}
    action_signal = action_map.get(modify_flag, '观望')

    # 获取时间戳
    if not signals_df.empty and hasattr(signals_df.index[-1], 'strftime'):
        timestamp = signals_df.index[-1].strftime("%Y-%m-%d %H:%M:%S")
    else:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 从交易记录提取持仓信息
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

        # 计算持仓盈亏
        current_price = float(last_row.get('close', 0))
        if current_price > 0 and trade_entry_price and trade_entry_price > 0:
            if trade_direction == 'Long':
                trade_profit_pct = round(
                    ((current_price / trade_entry_price) - 1) * 100, 4)
            elif trade_direction == 'Short':
                trade_profit_pct = round(
                    (1 - (current_price / trade_entry_price)) * 100, 4)

    return {
        "price": round(float(last_row.get('close', 0)), 4),
        "action_signal": action_signal,
        "modify_flag": modify_flag,
        "macd_histogram": round(float(last_row.get('MACD', 0)), 4),
        "macd_dif": round(float(last_row.get('DIF', 0)), 4),
        "macd_dea": round(float(last_row.get('DEA', 0)), 4),
        "smoothed_macd": round(float(last_row.get('smoothed_macd', 0)), 4),
        "adx": round(float(last_row.get('ADX', 0)), 2),
        "plus_di": round(float(last_row.get('+DI', 0)), 2),
        "minus_di": round(float(last_row.get('-DI', 0)), 2),
        "hist_weight": round(float(last_row.get('hist_weight', 0)), 3),
        "timestamp": timestamp,
        "trade_entry_price": trade_entry_price,
        "trade_entry_time": trade_entry_time,
        "trade_direction": trade_direction,
        "trade_profit_pct": trade_profit_pct,
    }


# ====================================================================
#  Pro3 单品种分析
# ====================================================================


def calculate_stock_pro3_single(code: str, adjust: str = 'qfq',
                                 long_only: bool = True) -> Dict:
    """计算单只股票的 Pro3 策略分析数据

    使用 StockPro3Strategy 替代传统 MACD/RSI 指标。

    Parameters
    ----------
    code : str
        股票代码，如 '600519', '000001'
    adjust : str
        复权类型
    long_only : bool
        仅做多模式（A股默认 True）

    Returns
    -------
    dict
        包含行情、Pro3 策略指标、分析信息的字典
    """
    name_map = get_stock_code_name_map()
    display_name = name_map.get(code, code)

    # 获取日线数据（近1年）
    end_date = datetime.date.today().strftime('%Y%m%d')
    start_date = (datetime.date.today() - datetime.timedelta(days=365)
                  ).strftime('%Y%m%d')

    df = _fetch_daily_with_retry(code, start_date, end_date, adjust=adjust)
    if df is None or df.empty:
        raise Exception(f"股票 [{code}] 无法获取日线数据")

    # 运行 Pro3 策略
    result = _run_stock_pro3_on_data(df, long_only=long_only)
    signal = _extract_stock_pro3_signal(result, df)

    # 综合行情判断
    modify_flag = signal['modify_flag']
    if modify_flag == 'rise':
        overall = f"{display_name} 技术面偏多 (Pro3)"
    elif modify_flag == 'fall':
        overall = f"{display_name} 技术面偏空 (Pro3)"
    else:
        overall = f"{display_name} 震荡/方向不明 (Pro3)"

    summary = result.get('summary', {})

    return {
        "timestamp": signal['timestamp'],
        "symbol": code,
        "display_name": display_name,
        "price": signal['price'],
        "indicators": {
            "macd": {
                "histogram": signal['macd_histogram'],
                "dif": signal['macd_dif'],
                "dea": signal['macd_dea'],
                "smoothed": signal['smoothed_macd'],
            },
            "adx": {
                "adx": signal['adx'],
                "plus_di": signal['plus_di'],
                "minus_di": signal['minus_di'],
            },
            "adaptive_weight": signal['hist_weight'],
        },
        "analysis": {
            "overall_market": overall,
            "action_signal": signal['action_signal'],
            "modify_flag": signal['modify_flag'],
        },
        "backtest": {
            "total_trades": summary.get('total_trades', 0),
            "win_rate": summary.get('win_rate', 0),
            "net_profit_pct": summary.get('net_profit_pct', 0),
            "max_drawdown": summary.get('max_drawdown', 0),
            "final_capital": summary.get('final_capital', 0),
        },
    }


# ====================================================================
#  Pro3 多周期方向总览
# ====================================================================


def calculate_stock_pro3_multi_period(code: str,
                                       periods: List[str] = None,
                                       long_only: bool = True) -> Dict:
    """多周期 Pro3 策略方向总览

    每个周期使用独立的 Pro3 策略参数和真实K线数据，确保信号完全独立。

    Parameters
    ----------
    code : str
        股票代码
    periods : list of str, optional
        周期列表，默认 ['5m', '15m', '30m', '1H', '4H', '1D']
    long_only : bool
        仅做多模式，默认 True

    Returns
    -------
    dict
        { "5m": {...}, "15m": {...}, ... }
    """
    if periods is None:
        periods = ['5m', '15m', '30m', '1H', '4H', '1D']

    minute_periods = [p for p in periods if _is_minute_period(p)]
    daily_periods = [p for p in periods if not _is_minute_period(p)]

    _daily_lookback = {
        'daily': 365,
        '1D': 365,
    }

    result = {}
    today = datetime.date.today()
    today_str = today.strftime('%Y%m%d')

    # ── 分钟周期 ──
    for period in minute_periods:
        try:
            params = _PRO3_MINUTE_PARAMS.get(
                period, _PRO3_MINUTE_PARAMS['5m'])

            minute_lookback_days = {
                '1m': 3, '5m': 5, '15m': 10, '30m': 15, '60m': 30,
                '1H': 30, '4H': 60,
            }
            lookback_d = minute_lookback_days.get(period, 5)

            # 4H 周期：获取 60m 数据后 resample
            if period == '4H':
                fetch_period = '60m'
                resample_factor = 4
            else:
                fetch_period = period
                resample_factor = None

            start_dt = (today - datetime.timedelta(
                days=lookback_d)).strftime('%Y%m%d')
            df = _fetch_minute_with_retry(
                code, period=fetch_period,
                start_date=start_dt, end_date=today_str,
                adjust='qfq')

            if df is None or df.empty:
                result[period] = {
                    "error": f"[{code}] 无法获取 {period} 分钟K线数据",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            # 4H resample
            if resample_factor and not df.empty and 'date' in df.columns:
                df = df.set_index('date')
                df = df.resample(f'{resample_factor}H').agg({
                    'open': 'first', 'high': 'max', 'low': 'min',
                    'close': 'last', 'volume': 'sum', 'amount': 'sum',
                }).dropna().reset_index()

            min_required = max(20, params.get('adx_period', 14) +
                               params.get('macd_slow', 26) + 5)
            if len(df) < min_required:
                result[period] = {
                    "error": f"[{code}] {period} 数据不足 (仅{len(df)}行, 需>{min_required}行)",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            from crypto.market_strategy.pro3_strategy import StockPro3Strategy
            strategy = StockPro3Strategy(
                df,
                adx_period=params['adx_period'],
                macd_fast=params['macd_fast'],
                macd_slow=params['macd_slow'],
                macd_signal=params['macd_signal'],
                base_weight=params['base_weight'],
                trade_multiplier=10.0,
                long_only=long_only,
            )
            strat_result = strategy.run()
            signal = _extract_stock_pro3_signal(strat_result, df)

            result[period] = {
                "price": signal['price'],
                "action_signal": signal['action_signal'],
                "modify_flag": signal['modify_flag'],
                "macd_histogram": signal['macd_histogram'],
                "adx": signal['adx'],
                "smoothed_macd": signal['smoothed_macd'],
                "hist_weight": signal['hist_weight'],
                "timestamp": signal['timestamp'],
                "rows_used": len(df),
                "trade_entry_price": signal.get('trade_entry_price'),
                "trade_entry_time": signal.get('trade_entry_time'),
                "trade_direction": signal.get('trade_direction'),
                "trade_profit_pct": signal.get('trade_profit_pct'),
            }
        except Exception as e:
            import traceback
            result[period] = {
                "error": str(e),
                "detail": traceback.format_exc(),
                "action_signal": "--",
                "modify_flag": "wait",
            }

    # ── 日线周期 ──
    for period in daily_periods:
        try:
            lookback_days = _daily_lookback.get(period, 365)
            start_date = (today - datetime.timedelta(
                days=lookback_days)).strftime('%Y%m%d')

            df = _fetch_daily_with_retry(
                code, start_date, today_str, adjust='qfq')

            if df is None or df.empty:
                result[period] = {
                    "error": f"[{code}] 无法获取 {period} 数据",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            params = _PRO3_DAILY_PARAMS.get('daily')

            if not params:
                params = {'adx_period': 28, 'macd_fast': 34,
                          'macd_slow': 72, 'macd_signal': 14,
                          'base_weight': 0.78}

            min_required = max(20, params['adx_period'] + params['macd_slow'] + 5)
            if len(df) < min_required:
                result[period] = {
                    "error": f"[{code}] {period} 数据不足 (仅{len(df)}行, 需>{min_required}行)",
                    "action_signal": "--",
                    "modify_flag": "wait",
                }
                continue

            from crypto.market_strategy.pro3_strategy import StockPro3Strategy
            tail_rows = 1095
            df_trim = df.tail(min(tail_rows, len(df))).copy()

            strategy = StockPro3Strategy(
                df_trim,
                adx_period=params['adx_period'],
                macd_fast=params['macd_fast'],
                macd_slow=params['macd_slow'],
                macd_signal=params['macd_signal'],
                base_weight=params['base_weight'],
                trade_multiplier=10.0,
                long_only=long_only,
            )
            strat_result = strategy.run()
            signal = _extract_stock_pro3_signal(strat_result, df_trim)

            result[period] = {
                "price": signal['price'],
                "action_signal": signal['action_signal'],
                "modify_flag": signal['modify_flag'],
                "macd_histogram": signal['macd_histogram'],
                "adx": signal['adx'],
                "smoothed_macd": signal['smoothed_macd'],
                "hist_weight": signal['hist_weight'],
                "timestamp": signal['timestamp'],
                "rows_used": len(df_trim),
                "trade_entry_price": signal.get('trade_entry_price'),
                "trade_entry_time": signal.get('trade_entry_time'),
                "trade_direction": signal.get('trade_direction'),
                "trade_profit_pct": signal.get('trade_profit_pct'),
            }
        except Exception as e:
            import traceback
            result[period] = {
                "error": str(e),
                "detail": traceback.format_exc(),
                "action_signal": "--",
                "modify_flag": "wait",
            }

    return result


# ====================================================================
#  Pro3 完整策略详情
# ====================================================================


def get_stock_pro3_strategy_detail(code: str, bar: str = 'daily',
                                    lookback: str = '1Y',
                                    long_only: bool = True) -> Dict:
    """获取股票的 Pro3 完整策略回测详情

    包含行情信号、持仓信息、交易记录、详细统计和 ADX 分析。

    Parameters
    ----------
    code : str
        股票代码
    bar : str
        K线周期：'5m', '15m', '30m', '1H', '4H', '1D'
    lookback : str
        回看周期：'30D', '3M', '6M', '1Y', '3Y', '5Y'
    long_only : bool
        仅做多模式，默认 True

    Returns
    -------
    dict
        包含 market, current_position, trade_records, stats 的完整字典
    """
    name_map = get_stock_code_name_map()
    display_name = name_map.get(code, code)

    lookback_days_map = {
        '30D': 30, '3M': 90, '6M': 180,
        '1Y': 365, '3Y': 1095, '5Y': 1825,
    }
    lookback_days = lookback_days_map.get(lookback, 365)

    today = datetime.date.today()
    end_date = today.strftime('%Y%m%d')
    start_date = (today - datetime.timedelta(
        days=lookback_days + 30)).strftime('%Y%m%d')

    if _is_minute_period(bar):
        # 4H 周期：获取 60m 数据后 resample
        if bar == '4H':
            df = _fetch_minute_with_retry(
                code, period='60m',
                start_date=start_date, end_date=end_date,
                adjust='qfq')
            if df is not None and not df.empty and 'date' in df.columns:
                df = df.set_index('date')
                df = df.resample('4H').agg({
                    'open': 'first', 'high': 'max', 'low': 'min',
                    'close': 'last', 'volume': 'sum', 'amount': 'sum',
                }).dropna().reset_index()
        else:
            df = _fetch_minute_with_retry(
                code, period=bar,
                start_date=start_date, end_date=end_date,
                adjust='qfq')
        params = _PRO3_MINUTE_PARAMS.get(bar, _PRO3_MINUTE_PARAMS['5m'])
    else:
        # 日线
        df = _fetch_daily_with_retry(code, start_date, end_date, adjust='qfq')
        params = _PRO3_DAILY_PARAMS.get('daily')

    if df is None or df.empty:
        raise Exception(f"股票 [{code}] 无法获取 {bar} 数据")

    # 运行 Pro3 策略
    from crypto.market_strategy.pro3_strategy import StockPro3Strategy
    strategy = StockPro3Strategy(
        df,
        adx_period=params['adx_period'],
        macd_fast=params['macd_fast'],
        macd_slow=params['macd_slow'],
        macd_signal=params['macd_signal'],
        base_weight=params['base_weight'],
        trade_multiplier=10.0,
        long_only=long_only,
    )
    result = strategy.run()

    summary = result.get('summary', {})
    trade_log = result.get('trade_log', [])
    signals_df = result.get('signals_df', pd.DataFrame())

    # 行情概览
    last_close = float(df.iloc[-1]['close']) if not df.empty else 0
    last_signal_row = signals_df.iloc[-1] if not signals_df.empty else {}

    modify_flag = last_signal_row.get('signal', 'hold')
    if modify_flag is None or modify_flag == 'hold':
        modify_flag = 'wait'

    action_map = {'rise': '上涨', 'fall': '下跌', 'wait': '观望'}
    action_signal = action_map.get(modify_flag, '观望')

    market = {
        "price": round(last_close, 4),
        "action_signal": action_signal,
        "macd_histogram": round(float(last_signal_row.get('MACD', 0)), 4),
        "macd_dif": round(float(last_signal_row.get('DIF', 0)), 4),
        "macd_dea": round(float(last_signal_row.get('DEA', 0)), 4),
        "smoothed_macd": round(float(last_signal_row.get('smoothed_macd', 0)), 4),
        "adx": round(float(last_signal_row.get('ADX', 0)), 2),
        "plus_di": round(float(last_signal_row.get('+DI', 0)), 2),
        "minus_di": round(float(last_signal_row.get('-DI', 0)), 2),
        "hist_weight": round(float(last_signal_row.get('hist_weight', 0)), 3),
        "curr_weight": round(float(last_signal_row.get('curr_weight', 0)), 3),
    }

    # 当前持仓信息
    last_trade = trade_log[-1] if trade_log else {}
    last_trade_price = last_trade.get('open_price', 0)
    current_profit = 0.0
    if last_trade_price > 0 and last_trade.get('direction'):
        direction = last_trade.get('direction', 'Long')
        if direction == 'Long':
            current_profit = round(
                ((last_close / last_trade_price) - 1) * 100 * 10.0, 2)
        else:
            current_profit = round(
                (1 - (last_close / last_trade_price)) * 100 * 10.0, 2)

    pos_status = "空仓"
    if trade_log and last_trade.get('direction'):
        pos_status = "多头持仓" if last_trade.get('direction') == 'Long' else "空头持仓"

    current_position = {
        "status": pos_status,
        "profit_pct": current_profit,
        "entry_price": round(last_trade_price, 4),
    }

    # 交易记录
    trade_records = []
    for i, t in enumerate(trade_log):
        open_time = t.get('open_time')
        open_time_str = (open_time.strftime('%Y-%m-%d %H:%M')
                         if isinstance(open_time, pd.Timestamp) else str(open_time or 'N/A'))

        close_time = 'N/A'
        close_price = 0.0
        if i + 1 < len(trade_log):
            ct = trade_log[i + 1].get('open_time')
            close_time = (ct.strftime('%Y-%m-%d %H:%M')
                          if isinstance(ct, pd.Timestamp) else str(ct))
            close_price = trade_log[i + 1].get('open_price', 0)
        elif i == len(trade_log) - 1:
            close_time = '持仓中'
            close_price = last_close

        direction_cn = "多头" if t.get('direction') == 'Long' else "空头"

        # 持仓时长
        duration = ""
        if open_time and close_time != '持仓中':
            try:
                ot = pd.Timestamp(open_time) if not isinstance(
                    open_time, pd.Timestamp) else open_time
                ct = pd.Timestamp(close_time) if not isinstance(
                    close_time, pd.Timestamp) else pd.Timestamp(close_time)
                delta = ct - ot
                days = delta.days
                hours = delta.seconds // 3600
                mins = (delta.seconds % 3600) // 60
                if days > 0:
                    duration = f"{days}天{hours}小时{mins}分"
                else:
                    duration = f"{hours}小时{mins}分"
            except Exception:
                duration = ""

        trade_records.append({
            "index": i + 1,
            "direction": direction_cn,
            "open_time": open_time_str,
            "open_price": round(t.get('open_price', 0), 4),
            "close_time": close_time,
            "close_price": round(close_price, 4),
            "profit": round(t.get('profit_pct', 0), 2),
            "max_win": round(t.get('max_win_pct', 0), 2),
            "max_loss": round(t.get('max_loss_pct', 0), 2),
            "entry_adx": round(t.get('entry_adx', 0), 1),
            "duration": duration,
        })

    # 时间范围
    if 'date' in df.columns:
        dates = pd.to_datetime(df['date'], errors='coerce')
        start_time = dates.min()
        end_time = dates.max()
    elif isinstance(df.index, pd.DatetimeIndex):
        start_time = df.index.min()
        end_time = df.index.max()
    else:
        start_time = today
        end_time = today

    start_time_str = (start_time.strftime('%Y-%m-%d')
                      if hasattr(start_time, 'strftime') else str(start_time))
    end_time_str = (end_time.strftime('%Y-%m-%d')
                    if hasattr(end_time, 'strftime') else str(end_time))
    total_days = (end_time - start_time).days if hasattr(
        end_time, '__sub__') else lookback_days
    if total_days < 1:
        total_days = 1

    net_profit = summary.get('net_profit_pct', 0)
    daily_profit = net_profit / total_days if total_days > 0 else 0
    weekly_profit = daily_profit * 7

    # 统计
    stats = {
        "total_trades": summary.get('total_trades', 0),
        "winning_trades": summary.get('winning_trades', 0),
        "losing_trades": summary.get('losing_trades', 0),
        "win_rate": summary.get('win_rate', 0),
        "net_profit_pct": summary.get('net_profit_pct', 0),
        "total_profit_pct": summary.get('total_profit', 0),
        "total_loss_pct": summary.get('total_loss', 0),
        "avg_profit_pct": summary.get('avg_profit', 0),
        "avg_loss_pct": summary.get('avg_loss', 0),
        "profit_loss_ratio": summary.get('profit_loss_ratio', 0),
        "max_drawdown_pct": summary.get('max_drawdown', 0),
        "profit_factor": (abs(summary.get('total_profit', 0)
                              / summary.get('total_loss', 0))
                          if summary.get('total_loss', 0) != 0 else 0),
        "initial_capital": summary.get('initial_capital', 100),
        "final_capital": summary.get('final_capital', 100),
        "total_return_pct": summary.get('net_profit_pct', 0),
        "contract_return_pct": summary.get('net_profit_pct', 0),
        "fix_money": 100 + net_profit,
        "contract_money": 100 + net_profit,
        "mix_money": 100 + net_profit,
        # Pro3 专属统计
        "avg_hist_weight": summary.get('avg_hist_weight', 0),
        "avg_curr_weight": summary.get('avg_curr_weight', 0),
        "adx_mean": summary.get('adx_mean', 0),
        "adx_win_mean": summary.get('adx_win_mean', 0),
        "adx_loss_mean": summary.get('adx_loss_mean', 0),
        "adx_corr": summary.get('adx_corr', 0),
        "high_adx_win_rate": summary.get('high_adx_win_rate', 0),
        "low_adx_win_rate": summary.get('low_adx_win_rate', 0),
        "avg_total_dur_h": summary.get('avg_total_dur_h', 0),
        "avg_win_dur_h": summary.get('avg_win_dur_h', 0),
        "avg_loss_dur_h": summary.get('avg_loss_dur_h', 0),
        "long_only": long_only,
    }

    return {
        "symbol": code,
        "display_name": display_name,
        "market": market,
        "current_position": current_position,
        "trade_records": trade_records,
        "stats": stats,
        "start_time": start_time_str,
        "end_time": end_time_str,
        "total_days": total_days,
        "daily_profit": round(daily_profit, 2),
        "weekly_profit": round(weekly_profit, 2),
    }


# ====================================================================
#  命令行快速测试
# ====================================================================
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(message)s'
    )

    print("=" * 60)
    print("  股票策略数据适配器 - 快速测试")
    print("=" * 60)

    # 1. 测试行业分类
    cats = get_stock_categories()
    print(f"\n行业分类: {len(cats)} 个类别, 共 {len(get_all_stocks())} 只股票")
    for cat, items in cats.items():
        print(f"  {cat}: {len(items)} 只")

    # 2. 测试单股票分析
    try:
        print("\n测试单股票分析 (600519 贵州茅台)...")
        data = calculate_stock_single('600519')
        print(f"  股票: {data['display_name']}")
        print(f"  价格: {data['price']}")
        print(f"  方向: {data['analysis']['action_signal']}")
        print(f"  MACD: {data['indicators']['macd']['histogram']}")
        print(f"  RSI: {data['indicators']['rsi']}")
    except Exception as e:
        print(f"  测试失败: {e}")

    # 3. 测试多周期
    try:
        print("\n测试多周期分析 (000001 平安银行)...")
        mp = calculate_stock_multi_period('000001')
        for period, data in mp.items():
            signal = data.get('action_signal', '--')
            print(f"  {period}: {signal}  (price={data.get('price', '--')})")
    except Exception as e:
        print(f"  测试失败: {e}")

    print()
