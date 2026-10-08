# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/pro3_dualtimeframe.py  →  现位置: crypto/market_strategy/pro3_dualtimeframe.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
# -*- coding: utf-8 -*-
import re
import math
import time
import sys
import os
from typing import Optional, Dict

import pandas as pd
import numpy as np
import backtrader as bt
import datetime as dt

# ====================================================================
#  数据接口：使用项目统一的 akshare 期货数据接口
# ====================================================================
# 数据接口：统一走显式包路径（迁移前靠 sys.path 兜底，已移除）
from crypto.futures.akshare_api import AkshareFuturesAPI, AkshareAPIError


# 模块级 DataFrame 引用（策略类通过此变量访问回测数据）
_STRATEGY_DF = None


BAR_MINUTE_MAP = {
    '1m': '1',
    '5m': '5',
    '15m': '15',
    '30m': '30',
    '60m': '60',
    '1H': '60',
}
DAILY_BARS = {'1D', 'D', 'day', 'daily'}
WEEKLY_BARS = {'1W', 'W', 'week', 'weekly'}


def _ensure_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    if {'open', 'high', 'low', 'close'}.issubset(df.columns):
        return df
    rename_map = {}
    if '开盘' in df.columns:
        rename_map['开盘'] = 'open'
    if '最高' in df.columns:
        rename_map['最高'] = 'high'
    if '最低' in df.columns:
        rename_map['最低'] = 'low'
    if '收盘' in df.columns:
        rename_map['收盘'] = 'close'
    # 一些数据源使用“开盘价/最高价/最低价/收盘价”
    if '开盘价' in df.columns:
        rename_map['开盘价'] = 'open'
    if '最高价' in df.columns:
        rename_map['最高价'] = 'high'
    if '最低价' in df.columns:
        rename_map['最低价'] = 'low'
    if '收盘价' in df.columns:
        rename_map['收盘价'] = 'close'
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def _resolve_daily_symbol(symbol: str) -> str:
    m = re.match(r"^([A-Za-z]+)", symbol)
    return m.group(1).upper() if m else symbol.upper()


def _get_base_symbol(symbol: str) -> str:
    # 提取期货品种基础代码，例如 'RB2410' -> 'RB'
    try:
        return ''.join([ch for ch in symbol if ch.isalpha()]) or symbol
    except Exception:
        return symbol


# ── 主力合约缓存 ──
_contract_cache: Dict[str, str] = {}
_contract_cache_time: float = 0.0


def _resolve_minute_contract(symbol: str) -> str:
    """将品种代码解析为具体合约代码用于分钟数据获取。

    AKShare 的分钟K线接口 (futures_zh_minute_sina) 需要具体合约代码（如 LC2609），
    不支持纯品种代码（如 LC）。此函数负责将品种代码转为可用合约代码。

    解析策略（从高到低优先级）：
    1. 若已包含数字，直接返回（已是合约代码）
    2. 通过 akshare 实时行情接口按持仓量获取真实主力合约（缓存5分钟）
    3. 若实时行情获取失败，回退到月份轮动启发式（1/5/9月）
    """
    global _contract_cache, _contract_cache_time

    # 已包含数字 → 已是合约代码，直接返回
    if re.search(r'\d', symbol):
        return symbol

    code = symbol.upper()
    now = time.time()

    # 检查缓存（5分钟内有效）
    if code in _contract_cache and (now - _contract_cache_time) < 300:
        return _contract_cache[code]

    # ── 策略1：通过实时行情获取真实主力合约 ──
    try:
        df_rt = AkshareFuturesAPI.fetch_realtime(code)
        if df_rt is not None and not df_rt.empty:
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
            if main_sym and len(main_sym) >= 3:
                _contract_cache[code] = main_sym
                _contract_cache_time = now
                print(f"品种 {code}: 实时行情解析主力合约 → {main_sym}")
                return main_sym
    except Exception as e:
        print(f"品种 {code}: 实时行情解析失败 ({e})，回退到月份启发式")

    # ── 策略2：月份轮动启发式回退 ──
    today = dt.datetime.now()
    yy = str(today.year)[2:]
    main_months = [1, 5, 9]
    skip_current_month = today.day >= 15 and today.month in main_months
    target_month = None
    for m in main_months:
        if skip_current_month and m == today.month:
            continue
        if m >= today.month:
            target_month = m
            break
    if target_month is None:
        target_month = 1
        yy = str(today.year + 1)[2:]
    mm = f"{target_month:02d}"
    contract = f"{symbol}{yy}{mm}"
    print(f"品种 {symbol}: 月份启发式解析合约 → {contract}")

    _contract_cache[code] = contract
    _contract_cache_time = now
    return contract


def _fetch_minute_with_retry(contract_symbol: str, period: str,
                            max_retries: int = 3) -> Optional[pd.DataFrame]:
    """获取分钟K线数据（带重试机制）

    参考 futures_adapter._fetch_minute_kline_for_code 的稳定获取逻辑。
    """
    for attempt in range(max_retries + 1):
        try:
            df = AkshareFuturesAPI.fetch_minute_kline(
                contract_symbol, period=period)
            if df is not None and not df.empty:
                return df
            return df
        except AkshareAPIError as e:
            if attempt < max_retries:
                wait_s = 1.5 * (attempt + 1)
                print(f"[{contract_symbol}] {period}分钟K线失败 (尝试{attempt+1}/{max_retries+1})，"
                      f"{wait_s:.1f}秒后重试: {e}")
                time.sleep(wait_s)
                continue
            print(f"[{contract_symbol}] {period}分钟K线获取失败（已达最大重试次数）: {e}")
            return None
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            print(f"[{contract_symbol}] {period}分钟K线获取异常: {e}")
            return None
    return None


def _fetch_daily_with_retry(code: str, start_date: str,
                            end_date: str, max_retries: int = 3
                            ) -> Optional[pd.DataFrame]:
    """获取日线数据（带重试机制）

    参考 futures_adapter._fetch_daily_with_retry 的稳定获取逻辑。
    """
    for attempt in range(max_retries + 1):
        try:
            df = AkshareFuturesAPI.fetch_daily_main(
                code, start_date, end_date)
            if df is not None and not df.empty:
                return df
            return df
        except AkshareAPIError as e:
            if attempt < max_retries:
                wait_s = 1.5 * (attempt + 1)
                print(f"[{code}] 日线数据失败 (尝试{attempt+1}/{max_retries+1})，"
                      f"{wait_s:.1f}秒后重试: {e}")
                time.sleep(wait_s)
                continue
            print(f"[{code}] 日线数据获取失败（已达最大重试次数）: {e}")
            return None
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            print(f"[{code}] 日线数据获取异常: {e}")
            return None
    return None


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名：将各种来源的K线 DataFrame 转为标准OHLC格式

    处理 akshare 返回的中文列名（开盘/最高/最低/收盘）和英文列名，
    统一转为 ['timestamp','open','high','low','close']。
    """
    # 先通过 _ensure_ohlc 转换 OHLC 列名
    df = _ensure_ohlc(df)

    # 识别时间列
    ts_col = None
    for candidate in ['date', 'datetime', '日期', '时间', 'timestamp']:
        if candidate in df.columns:
            ts_col = candidate
            break

    if ts_col is None:
        raise ValueError(f"未找到时间列，可用列: {list(df.columns)}")

    if ts_col != 'timestamp':
        df['timestamp'] = pd.to_datetime(df[ts_col], errors='coerce')
        if ts_col != 'timestamp' and ts_col in df.columns:
            df = df.drop(columns=[ts_col])

    # 确保 OHLC 为数值类型
    for col in ['open', 'high', 'low', 'close']:
        if col not in df.columns:
            raise ValueError(f"数据缺少必要列: {col}")
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # 只保留需要的列
    df = df[['timestamp', 'open', 'high', 'low', 'close']].dropna()
    df = df.sort_values('timestamp').set_index('timestamp')
    return df


def get_period_data(symbol: str, bar: str,
                    year: int | None = None,
                    prefer_contract_daily: bool = False,
                    backtest_days: int = 200) -> pd.DataFrame:
    """获取指定品种、指定周期的历史K线数据（一次性获取足够时间范围）

    数据获取策略（参考策略详情页 get_futures_strategy_detail）：
    - 日线/周线：传入 start_date/end_date，一次性获取指定日期范围的全部数据
    - 分钟K线：获取该合约全部可用分钟数据（akshare 返回最近数周~数月），
      再按 backtest_days 裁剪
    - 所有请求均具备 3 次重试机制，确保网络波动时不会直接失败

    Parameters
    ----------
    symbol : str
        品种代码（如 'LC', 'RB'）或合约代码（如 'LC2609'）
    bar : str
        K线周期：'1m','5m','15m','30m','1H' 等分钟级；'1D','1W' 等日周级
    year : int, optional
        按年度过滤数据，默认 None（不过滤）
    prefer_contract_daily : bool
        日线数据优先使用合约代码还是品种代码（默认 False=品种代码）
    backtest_days : int
        回看日历天数，控制最终返回数据的时间跨度（默认200天）

    Returns
    -------
    pd.DataFrame
        索引为 timestamp (DatetimeIndex)，包含列: open, high, low, close, ADX 等

    Raises
    ------
    ValueError
        数据为空或不支持的周期
    """
    bar = str(bar)
    today = dt.date.today()
    end_date_str = today.strftime('%Y%m%d')

    # ================================================================
    # 日线处理
    # ================================================================
    if bar in DAILY_BARS:
        # 计算获取范围：回看天数 + 缓冲（MACD慢线26 + ADX 14 + 额外余量）
        fetch_days = max(backtest_days + 60, 365)
        start_date_str = (today - dt.timedelta(days=fetch_days)).strftime('%Y%m%d')

        primary_code = (symbol if prefer_contract_daily
                        else _resolve_daily_symbol(symbol))
        fallback_code = (_resolve_daily_symbol(symbol) if prefer_contract_daily
                         else symbol)

        df = None
        source_label = ""

        # 首选数据源
        df = _fetch_daily_with_retry(primary_code, start_date_str, end_date_str)
        if df is not None and not df.empty:
            source_label = f"日线源: {primary_code}"

        # 备用数据源
        if df is None or df.empty:
            if fallback_code != primary_code:
                print(f"首选日线源 {primary_code} 为空，尝试备用: {fallback_code}")
                df = _fetch_daily_with_retry(fallback_code, start_date_str, end_date_str)
                if df is not None and not df.empty:
                    source_label = f"备用日线源: {fallback_code}"

        # 分钟聚合回退（日线接口均失败时）
        if df is None or df.empty:
            print(f"日线数据为空，改用 {symbol} 的 60 分钟数据聚合为日线")
            minute_sym = _resolve_minute_contract(symbol)
            df_min = _fetch_minute_with_retry(minute_sym, '60')
            if (df_min is None or df_min.empty) and minute_sym != symbol:
                df_min = _fetch_minute_with_retry(symbol, '60')
            if df_min is None or df_min.empty:
                raise ValueError(
                    f"日线和分钟数据均为空，无法获取: {symbol}")
            df_min = _normalize_columns(df_min)
            df = (df_min
                  .resample('D')
                  .agg({'open': 'first', 'high': 'max',
                        'low': 'min', 'close': 'last'})
                  .dropna()
                  .reset_index())
            source_label = f"分钟聚合日线: {minute_sym}"
        else:
            df = _normalize_columns(df)

        print(f"[{bar}] {source_label} → {len(df)} 条原始日线数据")

    # ================================================================
    # 周线处理
    # ================================================================
    elif bar in WEEKLY_BARS:
        # 周线需要更长的时间跨度以确保足够的周K线数量
        fetch_days = max(backtest_days + 90, 730)  # 至少2年
        start_date_str = (today - dt.timedelta(days=fetch_days)).strftime('%Y%m%d')

        primary_code = (symbol if prefer_contract_daily
                        else _resolve_daily_symbol(symbol))
        fallback_code = (_resolve_daily_symbol(symbol) if prefer_contract_daily
                         else symbol)

        # 优先用日线聚合为周线
        df_daily = _fetch_daily_with_retry(primary_code, start_date_str, end_date_str)
        if (df_daily is None or df_daily.empty) and fallback_code != primary_code:
            print(f"首选日线源 {primary_code} 为空，尝试备用: {fallback_code}")
            df_daily = _fetch_daily_with_retry(fallback_code, start_date_str, end_date_str)

        if df_daily is not None and not df_daily.empty:
            df_daily = _normalize_columns(df_daily)
            df = (df_daily
                  .resample('W-FRI')
                  .agg({'open': 'first', 'high': 'max',
                        'low': 'min', 'close': 'last'})
                  .dropna()
                  .reset_index())
            print(f"[周线] 日线聚合 → {len(df)} 条周K线")
        else:
            # 日线失败 → 分钟聚合回退
            print(f"日线数据为空，改用 {symbol} 的 60 分钟数据聚合为周线")
            minute_sym = _resolve_minute_contract(symbol)
            df_min = _fetch_minute_with_retry(minute_sym, '60')
            if (df_min is None or df_min.empty) and minute_sym != symbol:
                df_min = _fetch_minute_with_retry(symbol, '60')
            if df_min is None or df_min.empty:
                raise ValueError(
                    f"日线和分钟数据均为空，无法获取周线: {symbol}")
            df_min = _normalize_columns(df_min)
            df = (df_min
                  .resample('W-FRI')
                  .agg({'open': 'first', 'high': 'max',
                        'low': 'min', 'close': 'last'})
                  .dropna()
                  .reset_index())
            print(f"[周线] 分钟聚合 → {len(df)} 条周K线")

    # ================================================================
    # 分钟级K线处理
    # ================================================================
    else:
        period = BAR_MINUTE_MAP.get(bar, None)
        if period is None:
            raise ValueError(f"不支持的bar周期: {bar}")

        # 解析主力合约（若已是合约代码则直接使用）
        contract_symbol = _resolve_minute_contract(symbol)

        # 获取分钟K线（先尝试合约代码，再尝试原始symbol）
        df = _fetch_minute_with_retry(contract_symbol, period)
        if (df is None or df.empty) and contract_symbol != symbol:
            print(f"合约 {contract_symbol} 分钟数据为空，尝试原始符号 {symbol}...")
            df = _fetch_minute_with_retry(symbol, period)

        if df is None or df.empty:
            raise ValueError(
                f"分钟数据为空: symbol={symbol}, contract={contract_symbol}, "
                f"period={period}")

        df = _normalize_columns(df)
        print(f"[{bar}] 分钟K线源 {contract_symbol} → {len(df)} 条")

    # ================================================================
    # 统一后处理：年度过滤 + 回看天数裁剪 + ADX 计算
    # ================================================================
    if year is not None:
        try:
            df = df.loc[df.index.year == int(year)]
            print(f"  年度过滤 ({year}): 剩余 {len(df)} 条")
        except Exception:
            pass

    # 截取最近 backtest_days 日历天的数据
    if backtest_days is not None and backtest_days > 0:
        cutoff_date = df.index.max() - pd.Timedelta(days=backtest_days)
        df = df[df.index >= cutoff_date]
        print(f"  回看 {backtest_days} 天: 最终 {len(df)} 条")

    # 数据完整性校验
    min_required = 20  # 最少需要20根K线才能有效计算指标
    if len(df) < min_required:
        raise ValueError(
            f"数据量不足: 仅 {len(df)} 条，需要至少 {min_required} 条"
            f"（symbol={symbol}, bar={bar}, backtest_days={backtest_days}）")

    df = calculate_adx(df, period=14)
    return df


def calculate_adx(df, period=14):
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
    """根据ADX计算自适应平滑权重（与VCO完全一致，无下限钳制）"""
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    curr_weight = 1.0 - hist_weight
    return hist_weight, curr_weight


def _normalize_dir(v):
    """标准化方向字符串"""
    s = str(v)
    if s in ('rise', '多'):
        return 'rise'
    if s in ('fall', '空'):
        return 'fall'
    return None


class _MACDDirectionCalculator:
    """VCO 风格的 MACD 平滑方向计算状态机"""

    def __init__(self):
        self.smoothed_hist = []
        self.previous_diff = None
        self.justice_flag = "smoothed_histogram"
        self.dealjustice_flag = None
        self.modify_flag = None

    def update(self, histogram, adx_value, macd_prev_value=0):
        hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)
        if len(self.smoothed_hist) == 0:
            smoothed_histogram = histogram
        else:
            smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
        self.smoothed_hist.append(smoothed_histogram)
        self.previous_diff = histogram - smoothed_histogram

        if self.justice_flag == "smoothed_histogram":
            if histogram - smoothed_histogram > 0:
                if macd_prev_value > 0:
                    self.modify_flag = "rise"
                else:
                    self.justice_flag = "histogram"
                    self.dealjustice_flag = "waitRise"
            else:
                if macd_prev_value < 0:
                    self.modify_flag = "fall"
                else:
                    self.justice_flag = "histogram"
                    self.dealjustice_flag = "waitFall"
        elif self.justice_flag == "histogram":
            if self.dealjustice_flag == "waitRise":
                if histogram > 0:
                    self.modify_flag = "rise"
                    self.justice_flag = "smoothed_histogram"
                else:
                    self.modify_flag = "fall"
            elif self.dealjustice_flag == "waitFall":
                if histogram < 0:
                    self.modify_flag = "fall"
                    self.justice_flag = "smoothed_histogram"
                else:
                    self.modify_flag = "rise"
        return self.modify_flag


def calculate_period_direction(df):
    """计算长周期方向（与VCO完全一致）"""
    strat = _run_direction_calc(df, fill_series=False)
    return strat.final_direction


def fill_long_direction_series(df):
    """填充长周期方向序列到 df 的 LONG_DIRECTION 列（与VCO完全一致：backtrader MACD）"""
    _run_direction_calc(df, fill_series=True)


def _run_direction_calc(df, fill_series=False):
    """运行方向计算策略（与VCO的_run_direction_calc完全一致）"""
    class _DirStrategy(bt.Strategy):
        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)
            self.calc = _MACDDirectionCalculator()
            self.final_direction = None

        def next(self):
            current_idx = len(self) - 1
            adx_value = df.iloc[current_idx]['ADX'] if current_idx < len(df) else 0
            histogram = 2 * (self.macd.macd[0] - self.macd.signal[0])
            macd_prev = self.macd.macd[-1] if len(self.data) > 1 else 0
            direction = self.calc.update(histogram, adx_value, macd_prev)
            if fill_series:
                df.loc[self.data.datetime.datetime(0), 'LONG_DIRECTION'] = direction
            self.final_direction = direction

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(_DirStrategy)
    cerebro.run()
    return cerebro.runstrats[0][0]


def _align_long_direction(df, df_long):
    """将长周期方向对齐到短周期 df（与VCO完全一致）"""
    if 'LONG_DIRECTION' not in df_long.columns:
        df['LONG_DIRECTION'] = np.nan
        return
    try:
        if hasattr(df.index, 'tz') and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        if hasattr(df_long.index, 'tz') and df_long.index.tz is not None:
            df_long.index = df_long.index.tz_localize(None)
        short_times = pd.DataFrame({'ts': pd.to_datetime(df.index)})
        long_times = pd.DataFrame({
            'ts': pd.to_datetime(df_long.index),
            'LONG_DIRECTION': df_long['LONG_DIRECTION'].values
        })
        aligned = pd.merge_asof(
            short_times.sort_values('ts'),
            long_times.sort_values('ts'),
            on='ts', direction='backward')
        df['LONG_DIRECTION'] = aligned['LONG_DIRECTION'].values
    except Exception:
        df['LONG_DIRECTION'] = df_long['LONG_DIRECTION'].reindex(df.index, method='ffill')
    # VCO: 使用 first_valid_index 删除 warmup 前缀
    first_valid = df['LONG_DIRECTION'].first_valid_index()
    if first_valid is not None:
        df.drop(df.index[df.index < first_valid], inplace=True)


# ====================================================================
#  Backtrader 策略类 — MACD平滑 + ADX自适应 + 双周期共振
# ====================================================================

class DualTimeframePro3Strategy(bt.Strategy):
    """双周期 Pro3 策略 — 模块级可复用策略类

    核心逻辑：
    1. MACD 柱状图 + ADX 自适应平滑权重
    2. 短周期信号与长周期方向共振时才开仓
    3. 双周期方向不一致时平仓
    """
    params = (
        ('times', 10.0),
        ('base_money', 100.0),
        ('entry_price_type', 'threshold'),
        ('exit_price_type', 'open'),
        ('long_direction', 'rise'),
        ('symbol', ''),
        ('short_bar', '1H'),
        ('long_bar', '1D'),
        ('show_original_output', 1),
        ('show_market_output', 0),
        ('show_trade_ops_output', 0),
        ('show_trade_records_output', 0),
    )

    def __init__(self):
        self.macd = bt.indicators.MACD(self.data.close)
        self.ema12 = bt.indicators.EMA(self.data.close, period=12)
        self.ema26 = bt.indicators.EMA(self.data.close, period=26)
        self.smoothed_hist = []
        self.weight_history = []
        self.last_trade_index = None
        self.previous_diff = None
        self.original_flag = None
        self.justice_flag = "smoothed_histogram"
        self.dealjustice_flag = None
        self.modify_flag = None
        self.k = 0
        self.error = 0
        self.times = self.p.times
        self.base_money = self.p.base_money
        self.entry_price_type = str(self.p.entry_price_type)
        self.exit_price_type = str(self.p.exit_price_type)
        self.show_original_output = int(self.p.show_original_output)
        self.show_market_output = int(self.p.show_market_output)
        self.show_trade_ops_output = int(self.p.show_trade_ops_output)
        self.show_trade_records_output = int(self.p.show_trade_records_output)
        self.contract_money = self.base_money
        self.fix_money = self.base_money
        self.mix_money = self.base_money
        self.trades = []
        self.trade_times = []
        self.trade_dirs = []
        self.profits = []
        self.max_wins = []
        self.max_losses = []
        self.long_direction = self.p.long_direction
        self.symbol = self.p.symbol
        self.short_bar = self.p.short_bar
        self.long_bar_name = self.p.long_bar
        self.current_position = None
        self.filtered_signals = 0
        self.total_signals = 0
        self.bar_seq = 0
        self.entry_position_types = []
        self.entries = []
        self.entry_times = []
        self.exits = []
        self.exit_times = []
        self.fix_before = []
        self.fix_after = []
        self.contract_before = []
        self.contract_after = []
        self.mix_before = []
        self.mix_after = []

    def next(self):
        df = _STRATEGY_DF
        self.bar_seq += 1
        current_idx = len(self) - 1
        adx_value = df.iloc[current_idx]['ADX'] if current_idx < len(df) else 0

        macd_value = self.macd.macd[0]
        signal_value = self.macd.signal[0]
        histogram = 2 * (macd_value - signal_value)

        hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)
        self.weight_history.append((hist_weight, curr_weight))

        if len(self.smoothed_hist) == 0:
            smoothed_histogram = histogram
        else:
            smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
        self.smoothed_hist.append(smoothed_histogram)

        hist_smooth_diff = histogram - smoothed_histogram

        histogram_zero = 0.0
        hist_smooth_diff_zero = 0.0
        interpolated_zero = 0.0
        if len(self.data) > 1:
            prev_dea = self.macd.signal[-1]
            prev_ema12 = self.ema12[-1]
            prev_ema26 = self.ema26[-1]
            prev_smoothed = self.smoothed_hist[-2] if len(self.smoothed_hist) > 1 else 0
            histogram_zero = (prev_dea - (prev_ema12 * 11.0/13.0) + (prev_ema26 * 25.0/27.0)) / (2.0/13.0 - 2.0/27.0)
            if curr_weight != 0:
                hist_smooth_diff_zero = (prev_dea - (prev_ema12 * 11.0/13.0) + (prev_ema26 * 25.0/27.0) + (prev_smoothed * hist_weight / curr_weight)) / (2.0/13.0 - 2.0/27.0)
            else:
                hist_smooth_diff_zero = histogram_zero
            df.loc[self.data.datetime.datetime(0), 'HISTOGRAM_ZERO'] = histogram_zero
            df.loc[self.data.datetime.datetime(0), 'HIST_SMOOTH_DIFF_ZERO'] = hist_smooth_diff_zero
            # VCO: 计算插值零点价格
            prev_diff_val = self.previous_diff if self.previous_diff is not None else 0.0
            if prev_diff_val != hist_smooth_diff:
                denom_val = abs(hist_smooth_diff - prev_diff_val)
                if denom_val > 0:
                    ratio = max(0.0, min(1.0, abs(prev_diff_val) / denom_val))
                    interpolated_zero = float(self.data.close[-1]) + ratio * (float(self.data.close[0]) - float(self.data.close[-1]))

        if histogram > smoothed_histogram and self.previous_diff is not None and self.previous_diff < 0:
            self.last_trade_index = len(self) - 1
            self.original_flag = 'rise'
        elif histogram < smoothed_histogram and self.previous_diff is not None and self.previous_diff > 0:
            self.last_trade_index = len(self) - 1
            self.original_flag = 'fall'

        df.loc[self.data.datetime.datetime(0), 'ORIGINAL_FLAG'] = self.original_flag
        self.previous_diff = hist_smooth_diff

        df.loc[self.data.datetime.datetime(0), 'DIF'] = macd_value
        df.loc[self.data.datetime.datetime(0), 'DEA'] = signal_value
        df.loc[self.data.datetime.datetime(0), 'MACD'] = histogram
        df.loc[self.data.datetime.datetime(0), 'HIST_SMOOTH_DIFF'] = hist_smooth_diff
        df.loc[self.data.datetime.datetime(0), 'HIST_WEIGHT'] = hist_weight
        df.loc[self.data.datetime.datetime(0), 'CURR_WEIGHT'] = curr_weight
        # 注意：LONG_DIRECTION 由 _align_long_direction 预填充，不在此覆盖

        # VCO信号：用 hist_smooth_diff 正负号直接判定方向（替代原状态机）
        if hist_smooth_diff > 0:
            self.modify_flag = "rise"
        elif hist_smooth_diff < 0:
            self.modify_flag = "fall"

        df.loc[self.data.datetime.datetime(0), 'MODIFY_FLAG'] = self.modify_flag

        # ---- 行情输出（每根K线）----
        if self.show_market_output:
            ts = self.data.datetime.datetime(0)
            oh = self.data.open[0]
            hh = self.data.high[0]
            ll = self.data.low[0]
            cc = self.data.close[0]
            print(f"{self.bar_seq}. Modify: {self.modify_flag or 'N/A'} ADX: {adx_value:.2f} Weight: {hist_weight:.3f}/{curr_weight:.3f} OHLC:{oh:.3f}/{hh:.3f}/{ll:.3f}/{cc:.3f} macd:{histogram:.3f} dif:{macd_value:.3f} macd平滑差:{hist_smooth_diff:.3f} 临界值(macd=0):{histogram_zero:.3f} 临界值(平滑差=0):{hist_smooth_diff_zero:.3f} {ts}")

        # VCO: 从 df 读取逐 bar 动态长周期方向（替代静态 self.long_direction）
        ts = self.data.datetime.datetime(0)
        raw_long_dir = df.loc[ts, 'LONG_DIRECTION'] if 'LONG_DIRECTION' in df.columns else None
        if raw_long_dir is None:
            self.last_modify_flag = self.modify_flag
            return
        bar_long_dir = _normalize_dir(raw_long_dir)
        if bar_long_dir is None:
            self.last_modify_flag = self.modify_flag
            return

        directions_match = (self.modify_flag == bar_long_dir)
        signal_changed = self.modify_flag != getattr(self, 'last_modify_flag', None)

        # 平仓：有持仓 + 双周期不一致（VCO: 默认开盘价平仓）
        if self.current_position is not None and not directions_match:
            trade_price = self.data.close[0] if self.exit_price_type == 'close' else self.data.open[0]
            if self.current_position == "rise":
                profit = ((trade_price / self.trades[-1]) - 1) * 100 * self.times
            else:
                profit = (1 - (trade_price / self.trades[-1])) * 100 * self.times
            self.fix_before.append(self.fix_money)
            self.fix_money += self.base_money * profit * 0.01
            self.fix_after.append(self.fix_money)
            self.contract_before.append(self.contract_money)
            self.contract_money += self.contract_money * profit * 0.01
            self.contract_after.append(self.contract_money)
            ratio = self.mix_money / self.base_money
            if ratio > 0:
                try:
                    log_value = math.log(ratio) / math.log(1.6)
                    if not math.isnan(log_value) and not math.isinf(log_value):
                        base = self.base_money * pow(1.6, math.floor(log_value))
                        base = max(base, self.base_money)
                    else:
                        base = self.base_money
                except (ValueError, OverflowError):
                    base = self.base_money
            else:
                base = self.base_money
            self.mix_before.append(self.mix_money)
            self.mix_money += base * profit * 0.01
            self.mix_after.append(self.mix_money)
            self.profits.append(profit)
            self.exits.append(trade_price)
            self.exit_times.append(self.data.datetime.datetime(0))
            if self.show_trade_ops_output:
                print(f"平仓(方向不一致) 第{self.k}次 价格{trade_price:.3f} 持仓:{self.current_position} 短周期:{self.modify_flag} 长周期:{bar_long_dir} 定投:{int(self.fix_money)} 复投:{int(self.contract_money)} 混合:{int(self.mix_money)} 盈亏:{profit:.1f}% {self.data.datetime.datetime(0)}")
            self.last_position_type = self.current_position
            self.current_position = None
            self.filtered_signals += 1

        # 开仓：无持仓 + 双周期一致 + 信号改变（VCO: 默认收盘价开仓，支持zero插值）
        elif self.current_position is None and directions_match and signal_changed:
            self.total_signals += 1
            self.k += 1
            _bar_high = float(self.data.high[0])
            _bar_low = float(self.data.low[0])
            _used_zero = False
            if self.entry_price_type == 'zero' and interpolated_zero > 0 and _bar_low <= interpolated_zero <= _bar_high:
                trade_price = interpolated_zero
                _used_zero = True
            elif self.entry_price_type == 'close':
                trade_price = self.data.close[0]
            elif self.entry_price_type == 'open':
                trade_price = self.data.open[0]
            else:  # 'threshold' 兼容保留
                try:
                    if self.modify_flag == "rise":
                        trade_price = df.loc[self.data.datetime.datetime(-1), 'HISTOGRAM_ZERO']
                    else:
                        trade_price = df.loc[self.data.datetime.datetime(-1), 'HIST_SMOOTH_DIFF_ZERO']
                except (KeyError, IndexError):
                    trade_price = self.data.open[0]
                if pd.isna(trade_price) or trade_price <= 0 or trade_price > _bar_high or trade_price < _bar_low:
                    self.error += 1
                    trade_price = self.data.open[0]
            df.loc[self.data.datetime.datetime(0), 'TRADE_PRICE'] = trade_price
            self.trades.append(trade_price)
            self.trade_dirs.append(self.modify_flag)
            self.entry_position_types.append(self.modify_flag)
            self.entries.append(trade_price)
            self.trade_times.append(self.data.datetime.datetime(0))
            self.entry_times.append(self.data.datetime.datetime(0))
            self.max_wins.append(0.0)
            self.max_losses.append(0.0)
            self.current_position = self.modify_flag
            self.entry_price = trade_price
            if self.show_trade_ops_output:
                print(f"{'开多' if self.modify_flag=='rise' else '开空'} 第{self.k}次 价格{trade_price:.3f} ADX={adx_value:.1f} 权重({hist_weight:.3f}/{curr_weight:.3f}) 短周期:{self.modify_flag} 长周期:{bar_long_dir} 定投:{int(self.fix_money)} 复投:{int(self.contract_money)} 混合:{int(self.mix_money)} {self.data.datetime.datetime(0)}")

        # 情况3：方向不一致但无持仓 → 过滤信号
        elif self.current_position is None and not directions_match and signal_changed:
            self.filtered_signals += 1

        # 更新持仓盈亏（VCO: 使用 close 价格跟踪）
        if self.current_position is not None:
            current_price = self.data.close[0]
            last_trade_price = self.trades[-1]
            if self.current_position == 'rise':
                current_profit = ((current_price / last_trade_price) - 1) * 100 * self.times
            else:
                current_profit = (1 - (current_price / last_trade_price)) * 100 * self.times
            current_trade_idx = len(self.profits)
            if current_trade_idx < len(self.max_wins):
                self.max_wins[current_trade_idx] = max(self.max_wins[current_trade_idx], max(current_profit, 0))
                self.max_losses[current_trade_idx] = min(self.max_losses[current_trade_idx], min(current_profit, 0))

        self.last_modify_flag = self.modify_flag
    def stop(self):
        """策略结束时汇总统计"""
        total_trades = len(self.profits)
        winning_trades = sum(1 for p in self.profits if p > 0)
        losing_trades = total_trades - winning_trades
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        total_profit = sum(p for p in self.profits if p > 0)
        total_loss = sum(p for p in self.profits if p < 0)
        avg_profit = total_profit / winning_trades if winning_trades > 0 else 0
        avg_loss = total_loss / losing_trades if losing_trades > 0 else 0
        profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0
        total_profit_loss_ratio = total_profit / abs(total_loss) if total_loss != 0 else 0

        # 资金曲线与回撤
        comp_curve = [100.0]
        fix_curve = [100.0]
        mix_curve = [100.0]
        for p in self.profits:
            comp_curve.append(comp_curve[-1] * (1 + p * 0.01))
            fix_curve.append(fix_curve[-1] + 100.0 * p * 0.01)
            ratio = mix_curve[-1] / 100.0
            if ratio > 0:
                try:
                    log_value = math.log(ratio) / math.log(1.6)
                    if not math.isnan(log_value) and not math.isinf(log_value):
                        base = 100.0 * pow(1.6, math.floor(log_value))
                        base = max(base, 100.0)
                    else:
                        base = 100.0
                except (ValueError, OverflowError):
                    base = 100.0
            else:
                base = 100.0
            mix_curve.append(mix_curve[-1] + base * p * 0.01)

        def _max_dd(curve):
            peak = curve[0]
            max_dd = 0.0
            for v in curve:
                peak = max(peak, v)
                dd = (peak - v) / peak
                max_dd = max(max_dd, dd)
            return max_dd
        comp_dd = _max_dd(comp_curve)

        # 持仓盈亏核心指标
        win_indices = [i for i, p in enumerate(self.profits) if p > 0]
        loss_indices = [i for i, p in enumerate(self.profits) if p < 0]
        pos_count = len(win_indices)
        neg_count = len(loss_indices)
        hold_max_profit_total = sum(self.max_wins[i] for i in win_indices if i < len(self.max_wins)) if win_indices else 0.0
        hold_max_loss_total = sum(self.max_losses[i] for i in loss_indices if i < len(self.max_losses)) if loss_indices else 0.0
        hold_avg_profit = hold_max_profit_total / pos_count if pos_count > 0 else 0.0
        hold_avg_loss = hold_max_loss_total / neg_count if neg_count > 0 else 0.0
        hold_profit_loss_ratio = abs(hold_avg_profit / hold_avg_loss) if hold_avg_loss != 0 else 0.0
        hold_total_profit_loss_ratio = hold_max_profit_total / abs(hold_max_loss_total) if hold_max_loss_total != 0 else 0.0

        profit_factor = (total_profit / abs(total_loss)) if total_loss != 0 else 0
        calmar = (((comp_curve[-1] / comp_curve[0]) - 1.0) / comp_dd) if comp_dd > 0 else 0.0

        # 时间统计
        df = _STRATEGY_DF
        start_dt = df.index[0] if df is not None and len(df) > 0 else None
        end_dt = df.index[-1] if df is not None and len(df) > 0 else None
        total_days = (end_dt - start_dt).days if start_dt is not None and end_dt is not None else 0
        net_profit_pct = sum(self.profits) if self.profits else 0.0
        daily_profit = (net_profit_pct / total_days) if total_days > 0 else 0.0
        weekly_profit = daily_profit * 7

        # 存储指标供外部提取
        self._start_dt = start_dt
        self._end_dt = end_dt
        self._total_days = total_days
        self._daily_profit = daily_profit
        self._weekly_profit = weekly_profit
        self._losing_trades = losing_trades
        self._total_profit_pct = total_profit
        self._total_loss_pct = total_loss
        self._avg_profit_pct = avg_profit
        self._avg_loss_pct = avg_loss
        self._profit_loss_ratio = profit_loss_ratio
        self._total_profit_loss_ratio = total_profit_loss_ratio
        self._hold_max_profit_total = hold_max_profit_total
        self._hold_max_loss_total = hold_max_loss_total
        self._hold_avg_profit_pct = hold_avg_profit
        self._hold_avg_loss_pct = hold_avg_loss
        self._hold_profit_loss_ratio = hold_profit_loss_ratio
        self._hold_total_profit_loss_ratio = hold_total_profit_loss_ratio
        self._comp_dd = comp_dd
        self._calmar = calmar
        self._profit_factor = profit_factor

        if self.show_original_output:
            print()
            print("【策略回测结果概览】")
            print(f"交易对: {self.symbol if self.symbol else 'FUTURES'}")
            print(f"短周期: {self.short_bar}")
            print(f"长周期: {self.long_bar_name}")
            print()
            print("【时间统计】")
            print(f"开始时间: {start_dt.strftime('%Y-%m-%d %H:%M') if start_dt else 'N/A'}")
            print(f"结束时间: {end_dt.strftime('%Y-%m-%d %H:%M') if end_dt else 'N/A'}")
            print(f"总天数: {total_days}天")
            print(f"每日盈利: {daily_profit:.2f}%")
            print(f"每周盈利: {weekly_profit:.2f}%")
            print()
            print("【交易统计】")
            print(f"总交易次数: {total_trades}")
            print(f"胜率: {win_rate*100:.2f}%")
            print(f"盈利次数: {winning_trades}")
            print(f"亏损次数: {losing_trades}")
            print(f"复投最终收益: {self.contract_money:.2f}")
            print(f"定投最终收益: {self.fix_money:.2f}")
            print(f"混合最终收益: {self.mix_money:.2f}")
            print()
            print("【最终盈亏核心指标】")
            print(f"最终盈利总和: {total_profit:.2f}%")
            print(f"最终亏损总和: {total_loss:.2f}%")
            print(f"最终盈利均值（仅盈利单）: {avg_profit:.2f}%")
            print(f"最终亏损均值（仅亏损单）: {avg_loss:.2f}%")
            print(f"最终盈亏均值比: {profit_loss_ratio:.2f}")
            print(f"最终盈亏总和比: {total_profit_loss_ratio:.2f}")
            print()
            print("【持仓盈亏核心指标】")
            print(f"持仓最大盈利总和: {hold_max_profit_total:.2f}%")
            print(f"持仓最大亏损总和: {hold_max_loss_total:.2f}%")
            print(f"持仓最大盈利均值（仅盈利单）: {hold_avg_profit:.2f}%")
            print(f"持仓最大亏损均值（仅亏损单）: {hold_avg_loss:.2f}%")
            print(f"持仓最大盈亏均值比: {hold_profit_loss_ratio:.2f}")
            print(f"持仓最大盈亏总和比: {hold_total_profit_loss_ratio:.2f}")


# ====================================================================
#  DualTimeframeBacktestTool — 双周期回测工具类
# ====================================================================

class DualTimeframeBacktestTool:
    """双周期回测工具类

    封装完整的双周期 Pro3 策略回测流程，支持：
    - 单品种回测：run_backtest()
    - 批量回测：batch_test_from_csv()

    使用示例::

        tool = DualTimeframeBacktestTool('LC', backtest_days=200)
        result = tool.run_backtest('1H', '1W')
        print(result['net_profit_pct'], result['win_rate'])

        df = DualTimeframeBacktestTool.batch_test_from_csv('future.csv',
               symbols=['LC', 'C', 'CU'])
    """

    def __init__(self, symbol: str, backtest_days: int = 200,
                 entry_price_type: str = 'close', exit_price_type: str = 'open',
                 leverage: float = 10.0):
        """初始化回测工具

        Parameters
        ----------
        symbol : str
            品种代码，如 'LC', 'C', 'CU'
        backtest_days : int
            回看日历天数
        entry_price_type : str
            开仓价格类型: 'close' | 'open' | 'threshold'
        exit_price_type : str
            平仓价格类型: 'close' | 'open'
        leverage : float
            杠杆倍数
        """
        self.symbol = symbol
        self.backtest_days = backtest_days
        self.entry_price_type = entry_price_type
        self.exit_price_type = exit_price_type
        self.leverage = leverage

    def _fetch_and_prepare_data(self, short_bar: str, long_bar: str):
        """获取并准备双周期数据

        Returns
        -------
        tuple
            (df_short, df_long, long_direction, short_period_info, long_period_info)
        """
        print(f"[{self.symbol}] 正在获取短周期数据 ({short_bar})...")
        df_short_all = get_period_data(self.symbol, short_bar, year=None,
                                       backtest_days=self.backtest_days)
        if df_short_all is None or len(df_short_all) == 0:
            raise ValueError(f"短周期数据为空: {self.symbol} {short_bar}")

        df_short = df_short_all
        base_sym = _get_base_symbol(self.symbol)

        # 短/长周期数据获取之间冷却5秒，避免连续请求触发反爬
        print(f"[{self.symbol}] 冷却 5 秒后获取长周期数据...")
        time.sleep(5.0)

        print(f"[{self.symbol}] 正在获取长周期数据 ({long_bar})，基础品种: {base_sym} ...")

        df_long = pd.DataFrame()
        try:
            df_long = get_period_data(base_sym, long_bar, year=None,
                                      backtest_days=self.backtest_days)
        except Exception as e:
            fallback_text = '周线' if long_bar in WEEKLY_BARS else '日线'
            print(f"[{self.symbol}] 基础品种长周期获取失败: {e}; 将尝试用短周期聚合为{fallback_text}")

        if df_long is None or len(df_long) == 0:
            print(f"[{self.symbol}] 基础品种长周期为空，尝试直接获取合约...")
            try:
                df_long = get_period_data(self.symbol, long_bar, year=None,
                                          prefer_contract_daily=True,
                                          backtest_days=self.backtest_days)
            except Exception as e2:
                print(f"[{self.symbol}] 合约长周期获取失败: {e2}")

        if df_long is None or len(df_long) == 0:
            if long_bar in WEEKLY_BARS:
                print(f"[{self.symbol}] 长周期为空，使用短周期数据聚合为周线")
                try:
                    df_long = df_short.resample('W-FRI').agg(
                        {'open': 'first', 'high': 'max',
                         'low': 'min', 'close': 'last'}).dropna()
                except Exception as e:
                    raise ValueError(f"短周期聚合为周线失败: {e}")
            else:
                print(f"[{self.symbol}] 长周期为空，使用短周期数据聚合为日线")
                try:
                    df_long = df_short.resample('1D').agg(
                        {'open': 'first', 'high': 'max',
                         'low': 'min', 'close': 'last'}).dropna()
                except Exception as e:
                    raise ValueError(f"短周期聚合为日线失败: {e}")

        print(f"[{self.symbol}] 计算长周期方向...")
        long_direction = calculate_period_direction(df_long)
        fill_long_direction_series(df_long)  # VCO: 填充逐 bar 动态方向序列
        print(f"[{self.symbol}] 长周期方向: {long_direction}")

        short_info = {'start': str(df_short.index.min().date()),
                      'end': str(df_short.index.max().date()),
                      'count': len(df_short)}
        long_info = {'start': str(df_long.index.min().date()),
                     'end': str(df_long.index.max().date()),
                     'count': len(df_long)}

        return df_short, df_long, long_direction, short_info, long_info

    def run_backtest(self, short_bar: str = '1H', long_bar: str = '1W',
                     verbose: bool = True) -> dict:
        """执行单次双周期回测

        Parameters
        ----------
        short_bar : str
            短周期（交易信号周期），如 '1H', '1D'
        long_bar : str
            长周期（趋势确认周期），如 '1W', '1D'
        verbose : bool
            是否打印策略详细输出

        Returns
        -------
        dict
            结构化回测结果
        """
        global _STRATEGY_DF
        result = {
            'symbol': self.symbol,
            'short_bar': short_bar,
            'long_bar': long_bar,
            'backtest_days': self.backtest_days,
            'entry_price_type': self.entry_price_type,
            'exit_price_type': self.exit_price_type,
            'success': False,
            'error': None,
            'long_direction': None,
            'last_price': None,
            'last_trade_price': None,
            'atr': None,
            'trade_count': 0,
            'win_rate': None,
            'winning_trades': 0,
            'losing_trades': 0,
            'net_profit_pct': None,
            'max_drawdown': None,
            'calmar_ratio': None,
            'profit_factor': None,
            'fix_money': None,
            'contract_money': None,
            'mix_money': None,
            'data_period': {},
            'trades': [],
            # 时间统计
            'start_time': '',
            'end_time': '',
            'total_days': 0,
            'daily_profit': 0.0,
            'weekly_profit': 0.0,
            # 最终盈亏核心指标
            'total_profit_pct': 0.0,
            'total_loss_pct': 0.0,
            'avg_profit_pct': 0.0,
            'avg_loss_pct': 0.0,
            'profit_loss_ratio': 0.0,
            'total_profit_loss_ratio': 0.0,
            # 持仓盈亏核心指标
            'hold_max_profit_total': 0.0,
            'hold_max_loss_total': 0.0,
            'hold_avg_profit_pct': 0.0,
            'hold_avg_loss_pct': 0.0,
            'hold_profit_loss_ratio': 0.0,
            'hold_total_profit_loss_ratio': 0.0,
        }

        try:
            # 1. 获取数据
            df_short, df_long, long_direction, short_info, long_info = \
                self._fetch_and_prepare_data(short_bar, long_bar)

            result['long_direction'] = long_direction
            result['data_period'] = {'short': short_info, 'long': long_info}
            result['last_price'] = float(df_short.iloc[-1]['close'])

            # 2. 预计算指标
            df = df_short.copy()
            try:
                df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
                df['EMA26'] = df['close'].ewm(span=26, adjust=False).mean()
                df['DIF'] = df['EMA12'] - df['EMA26']
                df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
                df['MACD'] = 2 * (df['DIF'] - df['DEA'])
            except Exception:
                df['DIF'] = 0.0
                df['DEA'] = 0.0
                df['MACD'] = 0.0
            try:
                df = calculate_adx(df)
            except Exception:
                df['ADX'] = 0.0

            df['HISTOGRAM_ZERO'] = 0.0
            df['HIST_SMOOTH_DIFF_ZERO'] = 0.0
            df['ORIGINAL_FLAG'] = None
            df['HIST_SMOOTH_DIFF'] = 0.0
            df['HIST_WEIGHT'] = 0.0
            df['CURR_WEIGHT'] = 0.0
            df['LONG_DIRECTION'] = None
            df['MODIFY_FLAG'] = None
            df['TRADE_PRICE'] = 0.0

            # 3. VCO: 将长周期方向对齐到短周期 df
            _align_long_direction(df, df_long)

            # 4. 设置模块级 DataFrame 引用（策略需要）
            _STRATEGY_DF = df

            # 5. 运行 Backtrader
            show_out = 1 if verbose else 0
            cerebro = bt.Cerebro()
            cerebro.adddata(bt.feeds.PandasData(dataname=df))
            cerebro.addstrategy(DualTimeframePro3Strategy,
                                times=self.leverage,
                                entry_price_type=self.entry_price_type,
                                exit_price_type=self.exit_price_type,
                                long_direction=long_direction,
                                symbol=self.symbol,
                                short_bar=short_bar,
                                long_bar=long_bar,
                                show_original_output=show_out,
                                show_market_output=0,
                                show_trade_ops_output=0,
                                show_trade_records_output=0)
            strats = cerebro.run()
            strat = strats[0]

            # 5. 提取结果
            total_trades = len(strat.profits)
            winning_trades = sum(1 for p in strat.profits if p > 0)
            losing_trades = total_trades - winning_trades
            win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
            net_profit = sum(strat.profits) if strat.profits else 0.0

            # 最大回撤
            comp_curve = [100.0]
            for p in strat.profits:
                comp_curve.append(comp_curve[-1] * (1 + p * 0.01))
            peak = comp_curve[0]
            max_dd = 0.0
            for v in comp_curve:
                peak = max(peak, v)
                dd = (peak - v) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
            calmar = (((comp_curve[-1] / comp_curve[0]) - 1.0) / max_dd) if max_dd > 0 else 0.0

            last_row = df.iloc[-1]
            atr_like = float(last_row['ATR_ADX']) if 'ATR_ADX' in df.columns else 0.0
            last_trade = strat.trades[-1] if strat.trades else np.nan

            # 从策略中提取新增指标
            start_dt = getattr(strat, '_start_dt', None)
            end_dt = getattr(strat, '_end_dt', None)
            total_days = getattr(strat, '_total_days', 0)
            daily_profit = getattr(strat, '_daily_profit', 0.0)
            weekly_profit = getattr(strat, '_weekly_profit', 0.0)

            result.update({
                'success': True,
                'last_trade_price': float(last_trade) if not (isinstance(last_trade, float) and np.isnan(last_trade)) else None,
                'atr': atr_like,
                'trade_count': total_trades,
                'win_rate': round(win_rate, 4),
                'winning_trades': winning_trades,
                'losing_trades': losing_trades,
                'net_profit_pct': round(net_profit, 2),
                'max_drawdown': round(max_dd * 100, 2),
                'calmar_ratio': round(calmar, 2),
                'profit_factor': round(getattr(strat, '_profit_factor', 0), 2),
                'fix_money': round(strat.fix_money, 2),
                'contract_money': round(strat.contract_money, 2),
                'mix_money': round(strat.mix_money, 2),
                # 时间统计
                'start_time': start_dt.strftime('%Y-%m-%d %H:%M') if start_dt else '',
                'end_time': end_dt.strftime('%Y-%m-%d %H:%M') if end_dt else '',
                'total_days': total_days,
                'daily_profit': round(daily_profit, 4),
                'weekly_profit': round(weekly_profit, 4),
                # 最终盈亏核心指标
                'total_profit_pct': round(getattr(strat, '_total_profit_pct', 0), 2),
                'total_loss_pct': round(getattr(strat, '_total_loss_pct', 0), 2),
                'avg_profit_pct': round(getattr(strat, '_avg_profit_pct', 0), 2),
                'avg_loss_pct': round(getattr(strat, '_avg_loss_pct', 0), 2),
                'profit_loss_ratio': round(getattr(strat, '_profit_loss_ratio', 0), 2),
                'total_profit_loss_ratio': round(getattr(strat, '_total_profit_loss_ratio', 0), 2),
                # 持仓盈亏核心指标
                'hold_max_profit_total': round(getattr(strat, '_hold_max_profit_total', 0), 2),
                'hold_max_loss_total': round(getattr(strat, '_hold_max_loss_total', 0), 2),
                'hold_avg_profit_pct': round(getattr(strat, '_hold_avg_profit_pct', 0), 2),
                'hold_avg_loss_pct': round(getattr(strat, '_hold_avg_loss_pct', 0), 2),
                'hold_profit_loss_ratio': round(getattr(strat, '_hold_profit_loss_ratio', 0), 2),
                'hold_total_profit_loss_ratio': round(getattr(strat, '_hold_total_profit_loss_ratio', 0), 2),
            })

            # 交易记录
            for i in range(len(strat.profits)):
                direction = strat.entry_position_types[i] if i < len(strat.entry_position_types) else 'rise'
                entry = strat.entries[i] if i < len(strat.entries) else 0.0
                exitp = strat.exits[i] if i < len(strat.exits) else 0.0
                open_time = strat.entry_times[i] if i < len(strat.entry_times) else None
                close_time = strat.exit_times[i] if i < len(strat.exit_times) else None
                result['trades'].append({
                    'index': i + 1,
                    'direction': '开多' if direction == 'rise' else '开空',
                    'open_time': open_time.strftime('%Y-%m-%d %H:%M') if open_time else 'N/A',
                    'open_price': round(float(entry), 4),
                    'close_time': close_time.strftime('%Y-%m-%d %H:%M') if close_time else 'N/A',
                    'close_price': round(float(exitp), 4),
                    'profit_pct': round(strat.profits[i], 2),
                    'max_win': round(strat.max_wins[i], 2) if i < len(strat.max_wins) else 0,
                    'max_loss': round(strat.max_losses[i], 2) if i < len(strat.max_losses) else 0,
                })

        except Exception as e:
            import traceback
            result['error'] = f"{type(e).__name__}: {e}"
            if verbose:
                traceback.print_exc()

        return result


    @staticmethod
    def batch_test_from_csv(csv_path: str, symbols=None, backtest_days: int = 200,
                            entry_price_type: str = 'open',
                            exit_price_type: str = 'open',
                            leverage: float = 10.0,
                            verbose: bool = True) -> pd.DataFrame:
        """从 CSV 批量读取品种并执行双周期回测

        对每个品种执行两种双周期组合：
        - 1H (短) + 1W (长)
        - 1D (短) + 1D (长)

        Parameters
        ----------
        csv_path : str
            CSV 文件路径，必须包含 '代码' 列
        symbols : list, optional
            品种代码列表，默认 None 表示全部品种
        backtest_days : int
            回看天数
        entry_price_type : str
            开仓价格类型
        exit_price_type : str
            平仓价格类型
        leverage : float
            杠杆倍数
        verbose : bool
            是否打印详细输出

        Returns
        -------
        pd.DataFrame
            所有品种和组合的回测结果汇总
        """
        # 读取 CSV（使用 utf-8-sig 处理 BOM）
        df_csv = pd.read_csv(csv_path, encoding='utf-8-sig')
        df_csv = df_csv.dropna(subset=['代码']).reset_index(drop=True)
        df_csv['代码'] = df_csv['代码'].astype(str).str.strip()

        all_codes = df_csv['代码'].tolist()
        if symbols is not None:
            codes = [c for c in symbols if c in all_codes]
        else:
            codes = all_codes

        if not codes:
            print("没有找到有效的品种代码")
            return pd.DataFrame()

        print(f"\n{'='*80}")
        print(f"  批量双周期回测 — {len(codes)} 个品种")
        print(f"  回看天数: {backtest_days}  开仓类型: {entry_price_type}  平仓类型: {exit_price_type}")
        print(f"{'='*80}\n")

        results = []
        combinations = [
            ('1H', '1W', '1H-1W'),
            ('1D', '1W', '1D-1W'),
        ]

        # 符号间延迟（秒），避免触发新浪反爬机制
        SYMBOL_DELAY = 15.0
        COMBO_DELAY = 10.0

        for idx, code in enumerate(codes):
            # 符号间延迟（第一个符号不延迟）
            if idx > 0:
                time.sleep(SYMBOL_DELAY)

            name_row = df_csv[df_csv['代码'] == code]
            variety_name = name_row.iloc[0]['品种'] if not name_row.empty else code
            category = name_row.iloc[0]['种类'] if not name_row.empty else ''

            for ci, (short_bar, long_bar, combo_label) in enumerate(combinations):
                # 组合间延迟
                if ci > 0:
                    time.sleep(COMBO_DELAY)
                print(f"\n[{idx+1}/{len(codes)}] {code} {variety_name} {combo_label} ...")

                try:
                    tool = DualTimeframeBacktestTool(
                        code, backtest_days=backtest_days,
                        entry_price_type=entry_price_type,
                        exit_price_type=exit_price_type,
                        leverage=leverage)
                    r = tool.run_backtest(short_bar, long_bar, verbose=verbose)

                    row = {
                        '品种代码': code,
                        '品种名称': variety_name,
                        '种类': category,
                        '组合': combo_label,
                        '短周期': short_bar,
                        '长周期': long_bar,
                        '成功': r['success'],
                        '错误信息': r.get('error', ''),
                        '长周期方向': r.get('long_direction', ''),
                        '最新价格': r.get('last_price'),
                        '最新交易价': r.get('last_trade_price'),
                        'ATR': r.get('atr'),
                        '交易次数': r.get('trade_count', 0),
                        '胜率': r.get('win_rate'),
                        '净利润%': r.get('net_profit_pct'),
                        '最大回撤%': r.get('max_drawdown'),
                        '卡玛比率': r.get('calmar_ratio'),
                        '盈利因子': r.get('profit_factor'),
                        '定投资金': r.get('fix_money'),
                        '复投资金': r.get('contract_money'),
                        '混投资金': r.get('mix_money'),
                        '开始时间': r.get('start_time', ''),
                        '结束时间': r.get('end_time', ''),
                        '总天数': r.get('total_days', 0),
                        '每日盈利%': r.get('daily_profit', 0),
                        '每周盈利%': r.get('weekly_profit', 0),
                        '盈利次数': r.get('winning_trades', 0),
                        '亏损次数': r.get('losing_trades', 0),
                        '最终盈利总和%': r.get('total_profit_pct', 0),
                        '最终亏损总和%': r.get('total_loss_pct', 0),
                        '最终盈利均值%': r.get('avg_profit_pct', 0),
                        '最终亏损均值%': r.get('avg_loss_pct', 0),
                        '最终盈亏均值比': r.get('profit_loss_ratio', 0),
                        '最终盈亏总和比': r.get('total_profit_loss_ratio', 0),
                        '持仓最大盈利总和%': r.get('hold_max_profit_total', 0),
                        '持仓最大亏损总和%': r.get('hold_max_loss_total', 0),
                        '持仓最大盈利均值%': r.get('hold_avg_profit_pct', 0),
                        '持仓最大亏损均值%': r.get('hold_avg_loss_pct', 0),
                        '持仓最大盈亏均值比': r.get('hold_profit_loss_ratio', 0),
                        '持仓最大盈亏总和比': r.get('hold_total_profit_loss_ratio', 0),
                    }
                    results.append(row)

                    if r['success']:
                        print(f"  [OK] 方向={r['long_direction']} 交易={r['trade_count']}次 "
                              f"胜率={r['win_rate']:.2%} 净利={r['net_profit_pct']:.2f}% "
                              f"回撤={r['max_drawdown']:.2f}%")
                    else:
                        print(f"  [FAIL] {r.get('error', '未知错误')}")

                except Exception as e:
                    print(f"  [ERROR] {code} {combo_label}: {e}")
                    results.append({
                        '品种代码': code,
                        '品种名称': variety_name,
                        '种类': category,
                        '组合': combo_label,
                        '短周期': short_bar,
                        '长周期': long_bar,
                        '成功': False,
                        '错误信息': str(e),
                        '长周期方向': '',
                        '最新价格': None,
                        '最新交易价': None,
                        'ATR': None,
                        '交易次数': 0,
                        '胜率': None,
                        '净利润%': None,
                        '最大回撤%': None,
                        '卡玛比率': None,
                        '盈利因子': None,
                        '定投资金': None,
                        '复投资金': None,
                        '混投资金': None,
                        '开始时间': '',
                        '结束时间': '',
                        '总天数': 0,
                        '每日盈利%': 0,
                        '每周盈利%': 0,
                        '盈利次数': 0,
                        '亏损次数': 0,
                        '最终盈利总和%': 0,
                        '最终亏损总和%': 0,
                        '最终盈利均值%': 0,
                        '最终亏损均值%': 0,
                        '最终盈亏均值比': 0,
                        '最终盈亏总和比': 0,
                        '持仓最大盈利总和%': 0,
                        '持仓最大亏损总和%': 0,
                        '持仓最大盈利均值%': 0,
                        '持仓最大亏损均值%': 0,
                        '持仓最大盈亏均值比': 0,
                        '持仓最大盈亏总和比': 0,
                    })

        result_df = pd.DataFrame(results)
        print(f"\n{'='*80}")
        print(f"  批量回测完成: {len(results)} 条结果")
        print(f"{'='*80}\n")

        # 自动保存到 CSV
        try:
            DualTimeframeBacktestTool.save_results_to_csv(result_df)
        except Exception as save_err:
            print(f"  [WARN] CSV 保存失败: {save_err}")

        return result_df

    @staticmethod
    def save_results_to_csv(results_df: pd.DataFrame, output_dir: str = 'results'):
        """将回测结果保存到 CSV 文件

        Parameters
        ----------
        results_df : pd.DataFrame
            回测结果 DataFrame
        output_dir : str
            输出目录路径，默认为 'results'

        Returns
        -------
        str
            保存的 CSV 文件路径
        """
        import os as _os
        from datetime import datetime as _datetime

        # 确保目录存在
        script_dir = _os.path.dirname(_os.path.abspath(__file__))
        full_output_dir = _os.path.join(script_dir, output_dir)
        _os.makedirs(full_output_dir, exist_ok=True)

        # 生成带时间戳的文件名
        timestamp = _datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_filename = f'dual_timeframe_backtest_results_{timestamp}.csv'
        csv_path = _os.path.join(full_output_dir, csv_filename)

        # 定义 CSV 目标列（包含所有要求的指标）
        csv_columns = [
            '品种代码', '品种名称', '种类', '组合', '短周期', '长周期',
            '成功', '错误信息', '长周期方向',
            '最新价格', '最新交易价', 'ATR',
            '开始时间', '结束时间', '总天数', '每日盈利%', '每周盈利%',
            '交易次数', '胜率', '盈利次数', '亏损次数',
            '净利润%', '最大回撤%', '卡玛比率', '盈利因子',
            '复投资金', '定投资金', '混投资金',
            '最终盈利总和%', '最终亏损总和%', '最终盈利均值%', '最终亏损均值%',
            '最终盈亏均值比', '最终盈亏总和比',
            '持仓最大盈利总和%', '持仓最大亏损总和%',
            '持仓最大盈利均值%', '持仓最大亏损均值%',
            '持仓最大盈亏均值比', '持仓最大盈亏总和比',
        ]

        # 只保留存在的列
        available_cols = [c for c in csv_columns if c in results_df.columns]
        df_to_save = results_df[available_cols].copy()

        # 保存为 utf-8-sig 以支持中文字符
        df_to_save.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"回测结果已保存至: {csv_path}")
        return csv_path


# ====================================================================
#  向后兼容：保留原 get_latest_data 函数签名
# ====================================================================

def get_latest_data(symbol, short_bar="1H", long_bar="1D",
                    year: int | None = None, leverage: float = 10.0,
                    entry_price_type='close', exit_price_type='open',
                    backtest_days: int = 200,
                    show_original_output=1, show_market_output=1,
                    show_trade_ops_output=1, show_trade_records_output=1):
    """向后兼容的 get_latest_data 函数

    内部使用 DualTimeframeBacktestTool 实现，返回格式保持兼容。
    """
    tool = DualTimeframeBacktestTool(
        symbol, backtest_days=backtest_days,
        entry_price_type=entry_price_type,
        exit_price_type=exit_price_type,
        leverage=leverage)
    result = tool.run_backtest(short_bar, long_bar, verbose=bool(show_original_output))

    if not result['success']:
        raise RuntimeError(f"回测失败: {result.get('error', '未知错误')}")

    # 构造兼容的返回格式
    df_short = get_period_data(symbol, short_bar, year=year, backtest_days=backtest_days)
    last_row = df_short.iloc[-1]
    atr_like = float(last_row['ATR_ADX']) if 'ATR_ADX' in df_short.columns else 0.0
    last_trade = result.get('last_trade_price')
    long_direction = result.get('long_direction', 'fall')

    return last_row, last_trade if last_trade else np.nan, atr_like, long_direction


# ====================================================================
#  测试验证入口
# ====================================================================

def main():
    """主函数 — 三品种验证测试"""
    import os

    # CSV 路径
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'future.csv')
    test_symbols = ['LC', 'C', 'CU']
    backtest_days = 200

    print("=" * 80)
    print("  双周期回测工具类 — 三品种验证测试")
    print("  测试品种: LC(碳酸锂), C(玉米), CU(沪铜)")
    print("=" * 80)

    # ── Step 1: 工具类基本功能验证 ──
    print("\n" + "-" * 60)
    print("  Step 1: 工具类基本功能验证 (LC, 1H-1W)")
    print("-" * 60)

    tool = DualTimeframeBacktestTool('LC', backtest_days=backtest_days,
                                     entry_price_type='open',
                                     exit_price_type='open')
    result = tool.run_backtest('1H', '1W', verbose=False)

    print(f"  成功: {result['success']}")
    print(f"  长周期方向: {result['long_direction']}")
    print(f"  交易次数: {result['trade_count']}")
    print(f"  胜率: {result['win_rate']}")
    print(f"  净利润: {result['net_profit_pct']}%")
    print(f"  最大回撤: {result['max_drawdown']}%")
    print(f"  卡玛比率: {result['calmar_ratio']}")
    print(f"  交易记录数: {len(result['trades'])}")

    # 验证必要字段
    required_fields = ['symbol', 'short_bar', 'long_bar', 'success',
                       'long_direction', 'trade_count', 'win_rate',
                       'net_profit_pct', 'max_drawdown', 'trades']
    missing = [f for f in required_fields if f not in result]
    if missing:
        print(f"  [WARN] 缺少字段: {missing}")
    else:
        print(f"  [PASS] 所有必要字段完整")

    # ── Step 2: 双周期组合验证 ──
    print("\n" + "-" * 60)
    print("  Step 2: 双周期组合对比 (1H-1W vs 1D-1D)")
    print("-" * 60)

    print(f"\n  {'品种':<6} {'组合':<10} {'成功':<6} {'方向':<6} {'交易':<6} {'胜率':<8} {'净利润%':<10} {'回撤%':<8} {'卡玛':<6}")
    print(f"  {'-'*6} {'-'*10} {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*10} {'-'*8} {'-'*6}")

    for sym in test_symbols:
        for short_b, long_b, label in [('1H', '1W', '1H-1W'), ('1D', '1D', '1D-1D')]:
            try:
                t = DualTimeframeBacktestTool(sym, backtest_days=backtest_days,
                                              entry_price_type='open',
                                              exit_price_type='open')
                r = t.run_backtest(short_b, long_b, verbose=False)
                if r['success']:
                    print(f"  {sym:<6} {label:<10} {'OK':<6} {r['long_direction']:<6} "
                          f"{r['trade_count']:<6} {r['win_rate'] or 0:<8.2%} "
                          f"{r['net_profit_pct'] or 0:<10.2f} {r['max_drawdown'] or 0:<8.2f} "
                          f"{r['calmar_ratio'] or 0:<6.2f}")
                else:
                    print(f"  {sym:<6} {label:<10} {'FAIL':<6} - {r.get('error', '')[:40]}")
            except Exception as e:
                print(f"  {sym:<6} {label:<10} {'ERROR':<6} - {e}")

    # ── Step 3: 批量测试入口验证 ──
    print("\n" + "-" * 60)
    print("  Step 3: 批量测试入口验证 (仅 LC, C, CU)")
    print("-" * 60)

    df_summary = DualTimeframeBacktestTool.batch_test_from_csv(
        csv_path, symbols=test_symbols, backtest_days=backtest_days,
        entry_price_type='open', exit_price_type='open', verbose=False)

    if not df_summary.empty:
        print(f"\n  批量结果汇总 ({len(df_summary)} 条):")
        print(f"  成功: {df_summary['成功'].sum()} / {len(df_summary)}")
        print(f"\n  按品种汇总:")
        key_cols = ['品种代码', '品种名称', '组合', '成功', '长周期方向', '交易次数', '胜率', '净利润%', '最大回撤%']
        available_cols = [c for c in key_cols if c in df_summary.columns]
        print(df_summary[available_cols].to_string(index=False))
    else:
        print("  批量测试返回空结果")

    print("\n" + "=" * 80)
    print("  三品种验证测试完成")
    print("=" * 80)


if __name__ == '__main__':
    main()