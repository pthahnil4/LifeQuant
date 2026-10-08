# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/futures_boll_limit_dualtimeframe.py  →  现位置: crypto/market_strategy/futures_boll_limit_dualtimeframe.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
# -*- coding: utf-8 -*-
"""
期货双周期布林带限价策略
================================================================
基于 trend_strategy_boll_limit_scheduler_like.py（加密货币版）适配至国内期货市场。

核心逻辑保持不变：
  - 双周期框架：长周期（如日线/周线）判断趋势方向，短周期（如1H/15m）BOLL触边入场
  - 布林带限价：触下轨开多、触上轨平多；触上轨开空、触下轨平空
  - 风险控制：持仓期间若长周期方向反转导致亏损，则剔除该笔交易统计
  - ADX 自适应平滑权重 + MACD 平滑直方图方向判定

期货特性适配：
  - 数据源：AKShare 期货接口（主力合约解析、分钟/日线K线获取）
  - 杠杆机制：leverage 参数控制保证金杠杆倍数，盈亏按杠杆放大
  - 合约乘数：contract_multiplier 用于参考绝对盈亏（不影响百分比统计口径）
  - 合约规则：自动解析主力合约代码（持仓量最大），支持品种代码与合约代码两种输入

输出格式与统计指标与加密货币版保持一致，便于跨市场对比分析。
================================================================
"""

import re
import math
import time
import sys
import os
import datetime as dt
from typing import Optional, Dict

import pandas as pd
import numpy as np
import backtrader as bt

# ── AKShare 数据接口（直接调用，避免外部适配器依赖）──
try:
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None


# ====================================================================
#  全局状态（统计输出时引用）
# ====================================================================
CURRENT_SYMBOL = ""
CURRENT_SHORT_BAR = ""
CURRENT_LONG_BAR = ""

# 输出开关
PRINT_MARKET = 0
PRINT_TRADE_OPS = 0
PRINT_TRADE_RECORDS = 0
PRINT_ORIGINAL_OUTPUT = 1

# 周期映射：策略周期 -> AKShare 分钟参数
BAR_MINUTE_MAP = {
    '1m': '1', '5m': '5', '15m': '15',
    '30m': '30', '60m': '60', '1H': '60', '4H': '240',
}
DAILY_BARS = {'1D', 'D', 'day', 'daily'}
WEEKLY_BARS = {'1W', 'W', 'week', 'weekly'}


# ====================================================================
#  方向辅助函数
# ====================================================================
def _normalize_dir(v):
    """统一方向表示：rise/多 -> 'rise'，fall/空 -> 'fall'"""
    s = str(v)
    if s in ('rise', '多'):
        return 'rise'
    if s in ('fall', '空'):
        return 'fall'
    return None


def _dir_to_value(v):
    """方向 -> 数值：rise=1.0, fall=-1.0"""
    d = _normalize_dir(v)
    if d == 'rise':
        return 1.0
    if d == 'fall':
        return -1.0
    return 0.0


def _value_to_dir(v):
    """数值 -> 方向：>=0.5 为 rise，<=-0.5 为 fall"""
    try:
        if v >= 0.5:
            return 'rise'
        if v <= -0.5:
            return 'fall'
    except Exception:
        pass
    return None


# ====================================================================
#  期货数据获取层（AKShare）
# ====================================================================
def _ensure_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """将中文列名统一为英文 OHLC"""
    if {'open', 'high', 'low', 'close'}.issubset(df.columns):
        return df
    rename_map = {}
    for cn, en in [('开盘', 'open'), ('最高', 'high'), ('最低', 'low'), ('收盘', 'close'),
                   ('开盘价', 'open'), ('最高价', 'high'), ('最低价', 'low'), ('收盘价', 'close')]:
        if cn in df.columns:
            rename_map[cn] = en
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def _resolve_daily_symbol(symbol: str) -> str:
    """从合约代码提取品种代码（如 'RB2410' -> 'RB'）"""
    m = re.match(r"^([A-Za-z]+)", symbol)
    return m.group(1).upper() if m else symbol.upper()


def _get_base_symbol(symbol: str) -> str:
    """提取品种基础代码（仅保留字母部分）"""
    return ''.join(ch for ch in symbol if ch.isalpha()) or symbol


# ── 主力合约缓存 ──
_contract_cache: Dict[str, str] = {}
_contract_cache_time: float = 0.0


def _resolve_minute_contract(symbol: str) -> str:
    """将品种代码解析为具体合约代码，用于分钟K线获取。

    AKShare 分钟接口需要具体合约代码（如 LC2609），不支持纯品种代码。
    解析策略：
      1. 已含数字 -> 直接返回
      2. 通过实时行情按持仓量取主力合约（缓存5分钟）
      3. 回退到月份启发式（1/5/9月轮动）
    """
    global _contract_cache, _contract_cache_time
    if re.search(r'\d', symbol):
        return symbol

    code = symbol.upper()
    now = time.time()
    if code in _contract_cache and (now - _contract_cache_time) < 300:
        return _contract_cache[code]

    # 策略1：实时行情取主力合约
    try:
        if ak is not None:
            df_rt = ak.futures_zh_realtime(symbol=code)
            if df_rt is not None and not df_rt.empty:
                df_sorted = df_rt.copy()
                for col in ('position', 'volume'):
                    if col in df_sorted.columns:
                        df_sorted[col] = pd.to_numeric(df_sorted[col], errors='coerce')
                        df_sorted = df_sorted.sort_values(col, ascending=False)
                        break
                main_sym = str(df_sorted.iloc[0].get('symbol', '')).strip().upper()
                if main_sym and len(main_sym) >= 3:
                    _contract_cache[code] = main_sym
                    _contract_cache_time = now
                    print(f"品种 {code}: 实时行情解析主力合约 -> {main_sym}")
                    return main_sym
    except Exception as e:
        print(f"品种 {code}: 实时行情解析失败 ({e})，回退到月份启发式")

    # 策略2：月份启发式
    today = dt.datetime.now()
    yy = str(today.year)[2:]
    main_months = [1, 5, 9]
    skip_current = today.day >= 15 and today.month in main_months
    target_month = None
    for m in main_months:
        if skip_current and m == today.month:
            continue
        if m >= today.month:
            target_month = m
            break
    if target_month is None:
        target_month = 1
        yy = str(today.year + 1)[2:]
    contract = f"{symbol}{yy}{target_month:02d}"
    print(f"品种 {symbol}: 月份启发式解析合约 -> {contract}")
    _contract_cache[code] = contract
    _contract_cache_time = now
    return contract


def _fetch_minute_with_retry(contract_symbol: str, period: str,
                             max_retries: int = 3) -> Optional[pd.DataFrame]:
    """获取分钟K线（带重试）"""
    for attempt in range(max_retries + 1):
        try:
            if ak is None:
                return None
            df = ak.futures_zh_minute_sina(symbol=contract_symbol, period=period)
            if df is not None and not df.empty:
                return df
            return df
        except Exception as e:
            if attempt < max_retries:
                wait_s = 1.5 * (attempt + 1)
                print(f"[{contract_symbol}] {period}分钟K线失败 (尝试{attempt+1}/{max_retries+1})，"
                      f"{wait_s:.1f}秒后重试: {e}")
                time.sleep(wait_s)
                continue
            print(f"[{contract_symbol}] {period}分钟K线获取失败（已达最大重试次数）: {e}")
            return None
    return None


def _fetch_daily_with_retry(code: str, start_date: str,
                            end_date: str, max_retries: int = 3) -> Optional[pd.DataFrame]:
    """获取日线数据（带重试，数据源优先级：futures_zh_daily_sina > futures_main_sina）"""
    for attempt in range(max_retries + 1):
        try:
            if ak is None:
                return None
            sina_symbol = f"{code}0"
            df = ak.futures_zh_daily_sina(symbol=sina_symbol)
            if df is not None and not df.empty:
                if 'date' in df.columns:
                    df['date'] = pd.to_datetime(df['date'])
                if start_date:
                    df = df[df['date'] >= pd.to_datetime(start_date)]
                if end_date:
                    df = df[df['date'] <= pd.to_datetime(end_date)]
                return df
            # 备用接口
            df = ak.futures_main_sina(symbol=code, start_date=start_date, end_date=end_date)
            return df
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            print(f"[{code}] 日线数据获取失败: {e}")
            return None
    return None


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名：将各种来源的K线 DataFrame 转为标准 OHLC + timestamp 索引"""
    df = _ensure_ohlc(df)
    ts_col = None
    for candidate in ['date', 'datetime', '日期', '时间', 'timestamp']:
        if candidate in df.columns:
            ts_col = candidate
            break
    if ts_col is None:
        raise ValueError(f"未找到时间列，可用列: {list(df.columns)}")
    if ts_col != 'timestamp':
        df['timestamp'] = pd.to_datetime(df[ts_col], errors='coerce')
        df = df.drop(columns=[ts_col])
    for col in ['open', 'high', 'low', 'close']:
        if col not in df.columns:
            raise ValueError(f"数据缺少必要列: {col}")
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df[['timestamp', 'open', 'high', 'low', 'close']].dropna()
    df = df.sort_values('timestamp').set_index('timestamp')
    return df


def get_period_data(symbol: str, bar: str,
                    backtest_days: int = 300) -> pd.DataFrame:
    """获取指定品种、指定周期的历史K线数据

    数据获取策略：
      - 日线/周线：通过 futures_zh_daily_sina 获取品种日线，周线由日线聚合
      - 分钟K线：通过 futures_zh_minute_sina 获取具体合约分钟数据
      - 所有请求具备重试机制，日线失败时回退到分钟聚合

    Parameters
    ----------
    symbol : str      品种代码（如 'RB'）或合约代码（如 'RB2410'）
    bar : str         K线周期：'15m','1H','4H','1D','1W' 等
    backtest_days : int  回看日历天数

    Returns
    -------
    pd.DataFrame  索引为 timestamp，包含 open/high/low/close/ADX 等列
    """
    bar = str(bar)
    today = dt.date.today()
    end_date_str = today.strftime('%Y%m%d')

    # ── 日线 ──
    if bar in DAILY_BARS:
        fetch_days = max(backtest_days + 60, 365)
        start_date_str = (today - dt.timedelta(days=fetch_days)).strftime('%Y%m%d')
        daily_code = _resolve_daily_symbol(symbol)
        df = _fetch_daily_with_retry(daily_code, start_date_str, end_date_str)
        if df is None or df.empty:
            # 回退：分钟聚合日线
            print(f"日线数据为空，改用 {symbol} 的60分钟数据聚合为日线")
            minute_sym = _resolve_minute_contract(symbol)
            df_min = _fetch_minute_with_retry(minute_sym, '60')
            if (df_min is None or df_min.empty) and minute_sym != symbol:
                df_min = _fetch_minute_with_retry(symbol, '60')
            if df_min is None or df_min.empty:
                raise ValueError(f"日线和分钟数据均为空，无法获取: {symbol}")
            df_min = _normalize_columns(df_min)
            df = (df_min.resample('D')
                  .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                  .dropna())
        else:
            df = _normalize_columns(df)
        print(f"[{bar}] 日线源 {daily_code} -> {len(df)} 条")

    # ── 周线 ──
    elif bar in WEEKLY_BARS:
        fetch_days = max(backtest_days + 90, 730)
        start_date_str = (today - dt.timedelta(days=fetch_days)).strftime('%Y%m%d')
        daily_code = _resolve_daily_symbol(symbol)
        df_daily = _fetch_daily_with_retry(daily_code, start_date_str, end_date_str)
        if df_daily is not None and not df_daily.empty:
            df_daily = _normalize_columns(df_daily)
            df = (df_daily.resample('W-FRI')
                  .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                  .dropna())
            print(f"[周线] 日线聚合 -> {len(df)} 条")
        else:
            print(f"日线为空，改用 {symbol} 的60分钟数据聚合为周线")
            minute_sym = _resolve_minute_contract(symbol)
            df_min = _fetch_minute_with_retry(minute_sym, '60')
            if (df_min is None or df_min.empty) and minute_sym != symbol:
                df_min = _fetch_minute_with_retry(symbol, '60')
            if df_min is None or df_min.empty:
                raise ValueError(f"日线和分钟数据均为空，无法获取周线: {symbol}")
            df_min = _normalize_columns(df_min)
            df = (df_min.resample('W-FRI')
                  .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                  .dropna())
            print(f"[周线] 分钟聚合 -> {len(df)} 条")

    # ── 分钟级K线 ──
    else:
        period = BAR_MINUTE_MAP.get(bar)
        if period is None:
            raise ValueError(f"不支持的bar周期: {bar}")
        contract_symbol = _resolve_minute_contract(symbol)
        df = _fetch_minute_with_retry(contract_symbol, period)
        if (df is None or df.empty) and contract_symbol != symbol:
            print(f"合约 {contract_symbol} 分钟数据为空，尝试原始符号 {symbol}...")
            df = _fetch_minute_with_retry(symbol, period)
        if df is None or df.empty:
            raise ValueError(f"分钟数据为空: symbol={symbol}, contract={contract_symbol}, period={period}")
        df = _normalize_columns(df)
        print(f"[{bar}] 分钟K线源 {contract_symbol} -> {len(df)} 条")

    # ── 后处理：回看天数裁剪 + ADX 计算 ──
    if backtest_days and backtest_days > 0:
        cutoff = df.index.max() - pd.Timedelta(days=backtest_days)
        df = df[df.index >= cutoff]
        print(f"  回看 {backtest_days} 天: 最终 {len(df)} 条")

    if len(df) < 20:
        raise ValueError(f"数据量不足: 仅 {len(df)} 条，需要至少 20 条 (symbol={symbol}, bar={bar})")

    df = calculate_adx(df, period=14)
    return df


# ====================================================================
#  技术指标计算
# ====================================================================
def calculate_adx(df, period=14):
    """计算 ADX 及相关指标（+DI/-DI/ATR_ADX）"""
    df['prev_close'] = df['close'].shift(1)
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['prev_close'])
    df['tr3'] = abs(df['low'] - df['prev_close'])
    df['TR'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    df['high_diff'] = df['high'] - df['high'].shift(1)
    df['low_diff'] = df['low'].shift(1) - df['low']
    df['+DM'] = np.where((df['high_diff'] > df['low_diff']) & (df['high_diff'] > 0), df['high_diff'], 0)
    df['-DM'] = np.where((df['low_diff'] > df['high_diff']) & (df['low_diff'] > 0), df['low_diff'], 0)
    alpha = 1 / period
    df['ATR_ADX'] = df['TR'].ewm(alpha=alpha, adjust=False).mean()
    df['+DM_smooth'] = df['+DM'].ewm(alpha=alpha, adjust=False).mean()
    df['-DM_smooth'] = df['-DM'].ewm(alpha=alpha, adjust=False).mean()
    df['+DI'] = 100 * (df['+DM_smooth'] / df['ATR_ADX'])
    df['-DI'] = 100 * (df['-DM_smooth'] / df['ATR_ADX'])
    df['DI_sum'] = df['+DI'] + df['-DI']
    df['DI_diff'] = abs(df['+DI'] - df['-DI'])
    df['DX'] = np.where(df['DI_sum'] != 0, 100 * (df['DI_diff'] / df['DI_sum']), 0)
    df['ADX'] = df['DX'].ewm(alpha=alpha, adjust=False).mean()
    return df


def get_adaptive_smooth_weight(adx_value, base_weight=0.7):
    """ADX 自适应平滑权重：ADX 越大，历史权重越高（趋势越强越平滑）"""
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    hist_weight = max(0.65, hist_weight)
    curr_weight = 1.0 - hist_weight
    return hist_weight, curr_weight


# ====================================================================
#  长周期方向计算（MACD平滑直方图 + ADX自适应）
# ====================================================================
def _compute_direction_values(df):
    """逐K线计算长周期方向序列，返回 numpy 数组（rise=1.0, fall=-1.0）。

    使用 pandas 计算 EMA/MACD（避免 Backtrader 对小样本的最小周期限制），
    方向判定逻辑与加密货币版 calculate_period_direction 完全一致：
      - 基于 MACD 直方图与自适应平滑线的穿越
      - ADX 越强，平滑权重越高，方向越稳定
    """
    if df is None or len(df) < 5:
        return np.zeros(len(df) if df is not None else 0)

    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = 2 * (dif - dea)

    n = len(df)
    directions = np.zeros(n)
    smoothed = []
    justice_flag = 'smoothed_histogram'
    dealjustice_flag = None
    modify_flag = None

    for i in range(n):
        adx_val = float(df['ADX'].iloc[i]) if 'ADX' in df.columns else 0.0
        h = float(hist.iloc[i])
        hist_weight, curr_weight = get_adaptive_smooth_weight(adx_val)
        s = h if i == 0 else smoothed[-1] * hist_weight + h * curr_weight
        smoothed.append(s)

        if justice_flag == 'smoothed_histogram':
            if h - s > 0:
                if i > 0 and float(dif.iloc[i]) > 0:
                    modify_flag = 'rise'
                    justice_flag = 'smoothed_histogram'
                else:
                    justice_flag = 'histogram'
                    dealjustice_flag = 'waitRise'
            else:
                if i > 0 and float(dif.iloc[i]) < 0:
                    modify_flag = 'fall'
                    justice_flag = 'smoothed_histogram'
                else:
                    justice_flag = 'histogram'
                    dealjustice_flag = 'waitFall'
        elif justice_flag == 'histogram':
            if dealjustice_flag == 'waitRise':
                modify_flag = 'rise' if h > 0 else 'fall'
                if h > 0:
                    justice_flag = 'smoothed_histogram'
            elif dealjustice_flag == 'waitFall':
                modify_flag = 'fall' if h < 0 else 'rise'
                if h < 0:
                    justice_flag = 'smoothed_histogram'

        directions[i] = _dir_to_value(modify_flag)
    return directions


def calculate_period_direction(df):
    """计算长周期最终方向（rise/fall）"""
    directions = _compute_direction_values(df)
    if len(directions) == 0:
        return 'fall'
    return _value_to_dir(directions[-1]) or 'fall'


def fill_long_direction_series(df):
    """填充 df 的 LONG_DIRECTION 列（逐K线方向值）"""
    directions = _compute_direction_values(df)
    df['LONG_DIRECTION'] = directions
    return df


# ====================================================================
#  自定义数据Feed（携带长周期方向列）
# ====================================================================
class PandasDataWithLongDirection(bt.feeds.PandasData):
    """扩展 PandasData，增加 LONG_DIRECTION / LONG_CONF 两条数据线"""
    lines = ('LONG_DIRECTION', 'LONG_CONF')
    params = (('LONG_DIRECTION', -1), ('LONG_CONF', -1))


# ====================================================================
#  核心策略 + 回测主流程
# ====================================================================
def get_latest_data(symbol, short_bar="1H", long_bar="1D",
                    order_size=1.0, leverage=10.0, contract_multiplier=1.0,
                    backtest_days=300,
                    show_original_output=1, show_market_output=0,
                    show_trade_ops_output=0, show_trade_records_output=0):
    """期货双周期BOLL限价策略回测主流程

    Parameters
    ----------
    symbol : str              品种代码（如 'RB'）或合约代码（如 'RB2410'）
    short_bar : str           短周期（BOLL触边交易），如 '15m','1H'
    long_bar : str            长周期（趋势方向），如 '1D','1W','4H'
    order_size : float        开仓手数
    leverage : float          杠杆倍数（盈亏按此放大，模拟保证金交易）
    contract_multiplier : float  合约乘数（如 RB=10吨/手），用于参考绝对盈亏
    backtest_days : int       回看日历天数
    show_* : int              各级输出开关（1输出/0不输出）
    """
    global CURRENT_SYMBOL, CURRENT_SHORT_BAR, CURRENT_LONG_BAR
    global PRINT_MARKET, PRINT_TRADE_OPS, PRINT_TRADE_RECORDS, PRINT_ORIGINAL_OUTPUT
    CURRENT_SYMBOL = symbol
    CURRENT_SHORT_BAR = short_bar
    CURRENT_LONG_BAR = long_bar
    PRINT_MARKET = 1 if show_market_output else 0
    PRINT_TRADE_OPS = 1 if show_trade_ops_output else 0
    PRINT_TRADE_RECORDS = 1 if show_trade_records_output else 0
    PRINT_ORIGINAL_OUTPUT = 1 if show_original_output else 0

    base_sym = _get_base_symbol(symbol)

    # ── 获取短周期数据 ──
    if PRINT_MARKET:
        print(f"正在获取短周期数据 ({short_bar})...")
    df_short = get_period_data(symbol, short_bar, backtest_days=backtest_days)
    if df_short is None or len(df_short) == 0:
        raise ValueError(f"短周期数据为空: {symbol} {short_bar}")

    # ── 获取长周期数据 ──
    if PRINT_MARKET:
        print(f"正在获取长周期数据 ({long_bar})，基础品种: {base_sym} ...")
    df_long = pd.DataFrame()
    try:
        df_long = get_period_data(base_sym, long_bar, backtest_days=backtest_days)
    except Exception as e:
        print(f"基础品种长周期获取失败: {e}; 将尝试用短周期聚合")
    if df_long is None or len(df_long) == 0:
        # 回退：用短周期聚合为长周期
        agg_freq = 'W-FRI' if long_bar in WEEKLY_BARS else '1D'
        try:
            df_long = (df_short.resample(agg_freq)
                       .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                       .dropna())
            df_long = calculate_adx(df_long, period=14)
            print(f"短周期聚合为{long_bar}: {len(df_long)} 条")
        except Exception as e:
            raise ValueError(f"长周期数据获取失败: {e}")

    # ── 计算长周期方向 ──
    if PRINT_MARKET:
        print("计算长周期方向...")
    long_direction = calculate_period_direction(df_long)
    fill_long_direction_series(df_long)
    if 'LONG_DIRECTION' not in df_long.columns:
        df_long['LONG_DIRECTION'] = 0.0
    df_long['LONG_DIRECTION'] = df_long['LONG_DIRECTION'].fillna(0.0)

    # 长周期方向确认度：连续3根方向一致时置1
    conf_window = 3
    long_dir_values = df_long['LONG_DIRECTION'].values
    long_conf = np.zeros(len(long_dir_values), dtype=float)
    for i in range(len(long_dir_values)):
        if i + 1 < conf_window:
            continue
        window = long_dir_values[i + 1 - conf_window:i + 1]
        if np.all(window == window[0]) and window[0] != 0.0:
            long_conf[i] = 1.0
    df_long['LONG_CONF'] = long_conf
    if PRINT_MARKET:
        print(f"长周期方向: {long_direction}")

    # ── 长短周期方向对齐（merge_asof backward）──
    df = df_short
    try:
        if hasattr(df.index, 'tz') and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        if hasattr(df_long.index, 'tz') and df_long.index.tz is not None:
            df_long.index = df_long.index.tz_localize(None)
        short_times = pd.DataFrame({'ts': pd.to_datetime(df.index)})
        long_times = pd.DataFrame({
            'ts': pd.to_datetime(df_long.index),
            'LONG_DIRECTION': df_long['LONG_DIRECTION'].values,
            'LONG_CONF': df_long['LONG_CONF'].values,
        })
        short_times = short_times.sort_values('ts')
        long_times = long_times.sort_values('ts')
        aligned = pd.merge_asof(short_times, long_times, on='ts', direction='backward')
        df['LONG_DIRECTION'] = aligned['LONG_DIRECTION'].values
        df['LONG_CONF'] = aligned['LONG_CONF'].values
    except Exception:
        df['LONG_DIRECTION'] = df_long['LONG_DIRECTION'].reindex(df.index, method='ffill')
        df['LONG_CONF'] = df_long['LONG_CONF'].reindex(df.index, method='ffill')
    df['LONG_DIRECTION'] = df['LONG_DIRECTION'].fillna(0.0)
    df['LONG_CONF'] = df['LONG_CONF'].fillna(0.0)

    # 截取至长周期方向首次有效处
    first_valid = df['LONG_DIRECTION'].first_valid_index() if 'LONG_DIRECTION' in df.columns else None
    if first_valid is not None:
        df = df.loc[df.index >= first_valid]

    # ==================================================================
    #  策略类：双周期BOLL限价（触边开平仓 + 方向反转剔除）
    # ==================================================================
    class DualTimeframeBollLimitStrategy(bt.Strategy):
        params = (
            ('boll_period', 20),
            ('boll_dev', 2.0),
            ('order_size', 1.0),       # 开仓手数
            ('leverage_times', 10.0),  # 杠杆倍数
            ('contract_multiplier', 1.0),  # 合约乘数（参考用）
        )

        def __init__(self):
            self.boll = bt.indicators.BollingerBands(
                self.data.close, period=self.p.boll_period, devfactor=self.p.boll_dev)
            self.k = 0
            self.pos_size = 0.0
            self.entry_price = None
            self.entry_time = None
            self.entry_long_dir = None
            self.position_flip = False
            self.max_price_since_entry = None
            self.min_price_since_entry = None
            self.trades = []
            self.trade_times = []
            self.trade_dirs = []
            self.trade_records = []
            self.profits = []
            self.excluded_profits = []
            self.max_wins = []
            self.max_losses = []
            self.last_trade_index = None

        def next(self):
            long_dir_value = float(self.data.LONG_DIRECTION[0])
            if math.isnan(long_dir_value):
                long_dir_value = 0.0
            long_dir = _value_to_dir(long_dir_value)
            boll_top = float(self.boll.top[0]) if not math.isnan(self.boll.top[0]) else None
            boll_mid = float(self.boll.mid[0]) if not math.isnan(self.boll.mid[0]) else None
            boll_bot = float(self.boll.bot[0]) if not math.isnan(self.boll.bot[0]) else None
            if boll_top is None or boll_bot is None or boll_mid is None:
                return
            if long_dir is None:
                return

            leverage_times = float(self.p.leverage_times)
            multiplier = float(self.p.contract_multiplier)
            low = float(self.data.low[0])
            high = float(self.data.high[0])
            close_price = float(self.data.close[0])
            now_time = self.data.datetime.datetime(0)

            # 持仓期间跟踪极值 + 检测长周期方向反转
            if self.pos_size != 0 and self.entry_price is not None:
                if self.max_price_since_entry is None or high > self.max_price_since_entry:
                    self.max_price_since_entry = high
                if self.min_price_since_entry is None or low < self.min_price_since_entry:
                    self.min_price_since_entry = low
                if self.entry_long_dir and long_dir != self.entry_long_dir:
                    self.position_flip = True

            touch_bot = low <= boll_bot <= high
            touch_top = low <= boll_top <= high

            # ── 无持仓：触边开仓 ──
            if self.pos_size == 0:
                if long_dir == 'rise' and touch_bot:
                    # 开多：长周期看涨 + 触及BOLL下轨（限价买入）
                    self.k += 1
                    self.pos_size = self.p.order_size
                    self.entry_price = boll_bot
                    self.entry_time = now_time
                    self.entry_long_dir = long_dir
                    self.position_flip = False
                    self.max_price_since_entry = boll_bot
                    self.min_price_since_entry = boll_bot
                    self.trades.append(boll_bot)
                    self.trade_times.append(now_time)
                    self.trade_dirs.append('rise')
                    if PRINT_TRADE_OPS:
                        abs_pnl_hint = (boll_bot * multiplier * self.p.order_size)
                        print(f"开多 第{self.k}次 (方向:{long_dir}) 触边即入 BOLL下轨 "
                              f"限价{boll_bot:.4f} 杠杆{leverage_times}x {now_time}")
                elif long_dir == 'fall' and touch_top:
                    # 开空：长周期看跌 + 触及BOLL上轨（限价卖出）
                    self.k += 1
                    self.pos_size = -self.p.order_size
                    self.entry_price = boll_top
                    self.entry_time = now_time
                    self.entry_long_dir = long_dir
                    self.position_flip = False
                    self.max_price_since_entry = boll_top
                    self.min_price_since_entry = boll_top
                    self.trades.append(boll_top)
                    self.trade_times.append(now_time)
                    self.trade_dirs.append('fall')
                    if PRINT_TRADE_OPS:
                        print(f"开空 第{self.k}次 (方向:{long_dir}) 触边即入 BOLL上轨 "
                              f"限价{boll_top:.4f} 杠杆{leverage_times}x {now_time}")

            # ── 持多仓：触上轨平多 ──
            elif self.pos_size > 0 and touch_top:
                exit_price = boll_top
                profit = (exit_price / self.entry_price - 1) * 100 * leverage_times
                max_win = ((self.max_price_since_entry / self.entry_price - 1) * 100 * leverage_times
                           if self.max_price_since_entry else 0.0)
                max_loss = ((self.min_price_since_entry / self.entry_price - 1) * 100 * leverage_times
                            if self.min_price_since_entry else 0.0)
                excluded = self.position_flip and profit < 0
                if excluded:
                    self.excluded_profits.append(profit)
                else:
                    self.profits.append(profit)
                    self.max_wins.append(max_win)
                    self.max_losses.append(max_loss)
                self.trade_records.append({
                    'dir': 'rise', 'open_price': float(self.entry_price),
                    'close_price': float(exit_price), 'entry_time': self.entry_time,
                    'exit_time': now_time, 'profit': float(profit),
                    'max_win': float(max_win), 'max_loss': float(max_loss),
                    'excluded': excluded,
                })
                self.trades.append(exit_price)
                self.trade_times.append(now_time)
                self.last_trade_index = len(self) - 1
                if PRINT_TRADE_OPS:
                    tag = " 剔除" if excluded else ""
                    print(f"平多 第{self.k}次 触边即出 BOLL上轨 限价{exit_price:.4f} "
                          f"盈亏{profit:.2f}%{tag} {now_time}")
                self._reset_position()

            # ── 持空仓：触下轨平空 ──
            elif self.pos_size < 0 and touch_bot:
                exit_price = boll_bot
                profit = (1 - (exit_price / self.entry_price)) * 100 * leverage_times
                max_win = ((1 - (self.min_price_since_entry / self.entry_price)) * 100 * leverage_times
                           if self.min_price_since_entry else 0.0)
                max_loss = ((1 - (self.max_price_since_entry / self.entry_price)) * 100 * leverage_times
                            if self.max_price_since_entry else 0.0)
                excluded = self.position_flip and profit < 0
                if excluded:
                    self.excluded_profits.append(profit)
                else:
                    self.profits.append(profit)
                    self.max_wins.append(max_win)
                    self.max_losses.append(max_loss)
                self.trade_records.append({
                    'dir': 'fall', 'open_price': float(self.entry_price),
                    'close_price': float(exit_price), 'entry_time': self.entry_time,
                    'exit_time': now_time, 'profit': float(profit),
                    'max_win': float(max_win), 'max_loss': float(max_loss),
                    'excluded': excluded,
                })
                self.trades.append(exit_price)
                self.trade_times.append(now_time)
                self.last_trade_index = len(self) - 1
                if PRINT_TRADE_OPS:
                    tag = " 剔除" if excluded else ""
                    print(f"平空 第{self.k}次 触边即出 BOLL下轨 限价{exit_price:.4f} "
                          f"盈亏{profit:.2f}%{tag} {now_time}")
                self._reset_position()

            # ── 行情输出 ──
            if PRINT_MARKET:
                if self.pos_size > 0 and self.entry_price is not None:
                    current_profit = (close_price / self.entry_price - 1) * 100 * leverage_times
                    state = "持多"
                elif self.pos_size < 0 and self.entry_price is not None:
                    current_profit = (1 - (close_price / self.entry_price)) * 100 * leverage_times
                    state = "持空"
                else:
                    current_profit = 0.0
                    state = "BOLL区间空仓"
                pl_suffix = f" | 盈亏:{current_profit:+.2f}%"
                top_cover = high > boll_top
                bot_cover = low < boll_bot
                if top_cover and bot_cover:
                    rel = "上下轨同时覆盖"
                elif top_cover:
                    rel = "覆盖上轨"
                elif bot_cover:
                    rel = "覆盖下轨"
                elif boll_top > high > low > boll_bot:
                    rel = "轨道区间内"
                else:
                    rel = "触边"
                print(f"{now_time} 长周期:{long_dir} 行情范围: 低{low:.4f} 高{high:.4f} 收{close_price:.4f} "
                      f"| BOLL: 下轨{boll_bot:.4f} 中轨{boll_mid:.4f} 上轨{boll_top:.4f}{pl_suffix} "
                      f"| 状态:{state} | 关系:{rel}")

        def _reset_position(self):
            """平仓后重置持仓状态"""
            self.pos_size = 0.0
            self.entry_price = None
            self.entry_time = None
            self.entry_long_dir = None
            self.position_flip = False
            self.max_price_since_entry = None
            self.min_price_since_entry = None

    # ==================================================================
    #  运行回测
    # ==================================================================
    cerebro = bt.Cerebro()
    data_feed = PandasDataWithLongDirection(dataname=df)
    cerebro.adddata(data_feed)
    cerebro.addstrategy(DualTimeframeBollLimitStrategy,
                        order_size=float(order_size), leverage_times=float(leverage),
                        contract_multiplier=float(contract_multiplier))
    cerebro.run()
    strat = cerebro.runstrats[0][0]

    latest_data = df.iloc[-1]
    last_trade_data = None
    if strat.last_trade_index is not None:
        last_trade_data = df.iloc[strat.last_trade_index].copy()
        if len(strat.trades) > 0:
            last_trade_data['TRADE_PRICE'] = strat.trades[-1]
            last_trade_data['TRADE_TIME'] = strat.trade_times[-1]
            if len(strat.profits) > 0:
                last_trade_data['PROFIT'] = strat.profits[-1]
            if len(strat.max_wins) > 0:
                last_trade_data['MAX_WIN'] = strat.max_wins[-1]
                last_trade_data['MAX_LOSS'] = strat.max_losses[-1]

    # ── ATR 值（供参考）──
    atr_series = df['TR'].ewm(alpha=1 / 14, adjust=False).mean() if 'TR' in df.columns else None
    atr_value = float(atr_series.iloc[-1]) if atr_series is not None and len(atr_series) > 0 else float('nan')

    # ==================================================================
    #  统计输出（与加密货币版格式完全一致）
    # ==================================================================
    metrics = _print_statistics(strat, leverage, contract_multiplier, order_size)

    return latest_data, last_trade_data, atr_value, long_direction, df, df_long, metrics, strat.trade_records


def _print_statistics(strat, leverage, contract_multiplier, order_size):
    """输出回测统计（格式与加密货币版 trend_strategy_boll_limit_scheduler_like 一致）"""
    base_money = 100.0
    fix_money_history = [base_money]
    contract_money_history = [base_money]
    mix_money_history = [base_money]

    if PRINT_TRADE_RECORDS and len(strat.trade_records) > 0:
        print("\n=== 交易记录 ===")
    for i, rec in enumerate(strat.trade_records):
        profit = float(rec['profit'])
        fix_before = fix_money_history[-1]
        contract_before = contract_money_history[-1]
        mix_before = mix_money_history[-1]
        if rec['excluded']:
            fix_after = fix_before
            contract_after = contract_before
            mix_after = mix_before
        else:
            fix_after = fix_before + base_money * profit * 0.01
            contract_after = contract_before + contract_before * profit * 0.01
            ratio = mix_before / base_money
            if ratio > 0:
                try:
                    log_value = math.log(ratio) / math.log(1.6)
                    if not math.isnan(log_value) and not math.isinf(log_value):
                        base = base_money * pow(1.6, math.floor(log_value))
                        base = max(base, base_money)
                    else:
                        base = base_money
                except (ValueError, OverflowError):
                    base = base_money
            else:
                base = base_money
            mix_after = mix_before + base * profit * 0.01
        fix_money_history.append(fix_after)
        contract_money_history.append(contract_after)
        mix_money_history.append(mix_after)
        if PRINT_TRADE_RECORDS:
            exclude_txt = " 剔除" if rec['excluded'] else ""
            print(f"第{i+1}次交易       开仓:{float(rec['open_price']):.3f} "
                  f"平仓:{float(rec['close_price']):.3f}   最终盈利:{profit:+.2f}% "
                  f"最大盈利:{float(rec['max_win']):.2f}%  最大亏损:{float(rec['max_loss']):.2f}% "
                  f"定投:{fix_before:.0f}->{fix_after:.0f}({fix_after-fix_before:+.0f})       "
                  f"复投:{contract_before:.0f}->{contract_after:.0f}({contract_after-contract_before:+.0f})    "
                  f"混投:{mix_before:.0f}->{mix_after:.0f}({mix_after-mix_before:+.0f}){exclude_txt}")

    profits = [r['profit'] for r in strat.trade_records if not r['excluded']]
    max_wins = [r['max_win'] for r in strat.trade_records if not r['excluded']]
    max_losses = [r['max_loss'] for r in strat.trade_records if not r['excluded']]
    total_trades = len(profits)
    winning_trades = sum(1 for p in profits if p > 0)
    losing_trades = sum(1 for p in profits if p < 0)
    win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
    total_profit = sum(p for p in profits if p > 0)
    total_loss = sum(p for p in profits if p < 0)
    avg_profit = total_profit / winning_trades if winning_trades > 0 else 0.0
    avg_loss = total_loss / losing_trades if losing_trades > 0 else 0.0
    excluded_trades = sum(1 for r in strat.trade_records if r['excluded'])
    excluded_total_loss = sum(strat.excluded_profits) if strat.excluded_profits else 0.0
    excluded_avg_loss = excluded_total_loss / excluded_trades if excluded_trades > 0 else 0.0
    contract_money = contract_money_history[-1] if contract_money_history else 100.0
    fix_money = fix_money_history[-1] if fix_money_history else 100.0
    mix_money = mix_money_history[-1] if mix_money_history else 100.0

    win_indices = [i for i, p in enumerate(profits) if p > 0]
    loss_indices = [i for i, p in enumerate(profits) if p < 0]
    hold_total_max_profit = sum(max_wins[i] for i in win_indices) if win_indices else 0.0
    hold_total_max_loss = sum(max_losses[i] for i in loss_indices) if loss_indices else 0.0
    avg_hold_max_profit = hold_total_max_profit / len(win_indices) if win_indices else 0.0
    avg_hold_max_loss = hold_total_max_loss / len(loss_indices) if loss_indices else 0.0
    hold_pl_ratio = abs(avg_hold_max_profit / avg_hold_max_loss) if avg_hold_max_loss != 0 else 0.0
    overall_pl_ratio = abs(total_profit / total_loss) if total_loss < 0 else 0.0
    actual_profit_rate = total_profit / hold_total_max_profit if hold_total_max_profit > 0 else 0.0
    actual_loss_rate = abs(total_loss) / abs(hold_total_max_loss) if hold_total_max_loss < 0 else 0.0

    # 时间统计
    start_time = end_time = None
    included_times = [(r.get('entry_time'), r.get('exit_time'))
                      for r in strat.trade_records if not r['excluded']]
    if included_times:
        start_time = min(t[0] for t in included_times if t[0] is not None)
        end_time = max(t[1] for t in included_times if t[1] is not None)
    total_days = 0
    daily_profit = weekly_profit = 0.0
    if start_time and end_time:
        total_days = (end_time - start_time).days
        if total_days > 0:
            total_return = (fix_money - 100.0) / 100.0 * 100
            daily_profit = total_return / total_days
            weekly_profit = daily_profit * 7

    # 回撤计算
    def _max_drawdown(history):
        if len(history) <= 1:
            return 0.0
        peak = history[0]
        max_dd = 0.0
        for v in history:
            peak = max(peak, v)
            if peak > 0:
                dd = (peak - v) / peak * 100
                max_dd = max(max_dd, dd)
        return max_dd

    max_drawdown = _max_drawdown(contract_money_history)
    fix_drawdown = _max_drawdown(fix_money_history)
    mix_drawdown = _max_drawdown(mix_money_history)

    # 年化收益：contract_money/100 为正时才可取分数幂，否则置0
    _base = contract_money / 100.0
    annual_return = ((_base ** (252 / total_trades) - 1) * 100
                     if total_trades > 0 and _base > 0 else 0.0)
    calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0.0
    profit_factor = abs(total_profit / total_loss) if total_loss < 0 else 0.0
    final_mean_pl_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0.0
    final_sum_pl_ratio = abs(total_profit / total_loss) if total_loss != 0 else 0.0

    # ── 指标字典（始终构建，供批量回测调用）──
    metrics = {
        'total_trades': total_trades, 'win_rate': win_rate,
        'winning_trades': winning_trades, 'losing_trades': losing_trades,
        'excluded_trades': excluded_trades, 'excluded_total_loss': excluded_total_loss,
        'excluded_avg_loss': excluded_avg_loss,
        'total_profit': total_profit, 'total_loss': total_loss,
        'avg_profit': avg_profit, 'avg_loss': avg_loss,
        'mean_pl_ratio': final_mean_pl_ratio, 'sum_pl_ratio': final_sum_pl_ratio,
        'hold_max_profit_sum': hold_total_max_profit, 'hold_max_loss_sum': hold_total_max_loss,
        'hold_max_profit_avg': avg_hold_max_profit, 'hold_max_loss_avg': avg_hold_max_loss,
        'hold_pl_ratio': hold_pl_ratio, 'hold_sum_ratio': overall_pl_ratio,
        'profit_capture_rate': actual_profit_rate, 'loss_capture_rate': actual_loss_rate,
        'contract_final': contract_money, 'fix_final': fix_money, 'mix_final': mix_money,
        'max_drawdown': max_drawdown, 'fix_drawdown': fix_drawdown, 'mix_drawdown': mix_drawdown,
        'calmar_ratio': calmar_ratio, 'profit_factor': profit_factor, 'annual_return': annual_return,
        'total_days': total_days, 'daily_profit': daily_profit, 'weekly_profit': weekly_profit,
    }

    if not PRINT_ORIGINAL_OUTPUT:
        return metrics

    # ── 输出 ──
    print("\nBOLL限价双周期调度逻辑回测统计（期货版）")
    print("=" * 80)
    print(f"交易对: {CURRENT_SYMBOL}")
    print(f"短周期: {CURRENT_SHORT_BAR}")
    print(f"长周期: {CURRENT_LONG_BAR}")
    print(f"杠杆倍数: {leverage}x  (保证金率: {100/leverage:.1f}%)")
    print(f"合约乘数: {contract_multiplier}  开仓手数: {order_size}")
    print("\n时间统计")
    if start_time and end_time:
        print(f"开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"总天数: {total_days}天")
        print(f"每日盈利: {daily_profit:.2f}%")
        print(f"每周盈利: {weekly_profit:.2f}%")
    else:
        print("无交易记录")
    print(f"\n总交易次数: {total_trades}")
    print(f"胜率: {win_rate:.2%}")
    print(f"盈利次数: {winning_trades}")
    print(f"亏损次数: {losing_trades}")
    print(f"剔除交易数: {excluded_trades}")
    print(f"剔除交易总亏损: {excluded_total_loss:.2f}%")
    print(f"剔除交易平均亏损: {excluded_avg_loss:.2f}%")
    print(f"复投最终收益: {contract_money:.2f}")
    print(f"定投最终收益: {fix_money:.2f}")
    print(f"混合最终收益: {mix_money:.2f}")
    print("\n最终盈亏核心指标")
    print(f"最终盈利总和: {total_profit:.2f}%")
    print(f"最终亏损总和: {total_loss:.2f}%")
    print(f"最终盈利均值（仅盈利单）: {avg_profit:.2f}%")
    print(f"最终亏损均值（仅亏损单）: {avg_loss:.2f}%")
    print(f"最终盈亏均值比: {final_mean_pl_ratio:.2f}")
    print(f"最终盈亏总和比: {final_sum_pl_ratio:.2f}")
    print("\n持仓盈亏核心指标")
    print(f"持仓最大盈利总和: {hold_total_max_profit:.2f}%")
    print(f"持仓最大亏损总和: {hold_total_max_loss:.2f}%")
    print(f"持仓最大盈利均值（仅盈利单）: {avg_hold_max_profit:.2f}%")
    print(f"持仓最大亏损均值（仅亏损单）: {avg_hold_max_loss:.2f}%")
    print(f"持仓最大盈亏均值比: {hold_pl_ratio:.2f}")
    print(f"持仓最大盈亏总和比: {overall_pl_ratio:.2f}")
    print("\n盈亏到手率（最终盈亏与最大盈亏比值）")
    print(f"盈利到手率: {actual_profit_rate:.2%}")
    print(f"亏损到手率: {actual_loss_rate:.2%}")
    print("\n风险与绩效指标")
    print(f"复投最大回撤率: {max_drawdown:.2f}%")
    print(f"定投最大回撤率: {fix_drawdown:.2f}%")
    print(f"混合最大回撤率: {mix_drawdown:.2f}%")
    print(f"卡玛比率: {calmar_ratio:.2f}")
    print(f"盈利因子: {profit_factor:.2f}")
    print("=" * 80)
    return metrics


# ====================================================================
#  对外封装入口
# ====================================================================
def run_backtest(
    symbol='RB',
    short_bar='1H',
    long_bar='1D',
    order_size=1.0,
    leverage=10.0,
    contract_multiplier=10.0,
    backtest_days=300,
    show_original_output=1,
    show_market_output=0,
    show_trade_ops_output=0,
    show_trade_records_output=0,
):
    """便捷回测入口

    Parameters
    ----------
    symbol : str              品种代码（如 'RB','CU','C'）或合约代码（如 'RB2410'）
    short_bar : str           短周期，如 '15m','1H'
    long_bar : str            长周期，如 '1D','1W','4H'
    order_size : float        开仓手数
    leverage : float          杠杆倍数
    contract_multiplier : float  合约乘数（RB=10, CU=5, C=10 等，参考用）
    backtest_days : int       回测天数
    """
    latest_data, last_trade_data, atr, long_direction, df, df_long, metrics, trade_records = get_latest_data(
        symbol, short_bar, long_bar,
        order_size=order_size, leverage=leverage,
        contract_multiplier=contract_multiplier,
        backtest_days=backtest_days,
        show_original_output=show_original_output,
        show_market_output=show_market_output,
        show_trade_ops_output=show_trade_ops_output,
        show_trade_records_output=show_trade_records_output,
    )
    if show_original_output:
        print(f"\n当前长周期方向: {long_direction}")
        print("最新行情时间：", latest_data.name)
        print("ATR值:", atr)
    return latest_data, last_trade_data, atr, long_direction, metrics, trade_records


def main():
    """主函数 - 示例用法

    常见期货品种合约乘数参考：
      RB(螺纹钢)=10  CU(铜)=5  C(玉米)=10  MA(甲醇)=10
      SR(白糖)=10   TA(PTA)=5  RU(橡胶)=10  I(铁矿石)=100
    """
    symbol = 'RB'                      # 品种代码
    short_bar = '1H'                   # 短周期（BOLL触边交易）
    long_bar = '1D'                    # 长周期（趋势方向）
    order_size = 1.0                   # 开仓手数
    leverage = 10.0                    # 杠杆倍数
    contract_multiplier = 10.0         # 合约乘数（RB=10吨/手）
    backtest_days = 300                # 回测天数
    show_original_output = 1          # 统计输出：1输出，0不输出
    show_market_output = 0             # 行情输出：1输出，0不输出
    show_trade_ops_output = 0          # 交易操作输出：1输出，0不输出
    show_trade_records_output = 0      # 交易记录输出：1输出，0不输出

    run_backtest(
        symbol=symbol,
        short_bar=short_bar,
        long_bar=long_bar,
        order_size=order_size,
        leverage=leverage,
        contract_multiplier=contract_multiplier,
        backtest_days=backtest_days,
        show_original_output=show_original_output,
        show_market_output=show_market_output,
        show_trade_ops_output=show_trade_ops_output,
        show_trade_records_output=show_trade_records_output,
    )


if __name__ == '__main__':
    main()
