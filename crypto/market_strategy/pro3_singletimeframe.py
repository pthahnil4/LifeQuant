# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/pro3_singletimeframe.py  →  现位置: crypto/market_strategy/pro3_singletimeframe.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
# -*- coding: utf-8 -*-
"""
pro3_singletimeframe.py
单周期版本 —— 移除长周期趋势确认，仅基于短周期 MACD 平滑 + ADX 自适应信号进行交易。
与双周期版本（pro3_dualtimeframe.py）的区别：
  - 不再获取和计算长周期方向
  - 开仓条件：无持仓 + 信号变化（不再要求与长周期方向一致）
  - 平仓条件：持仓方向与信号方向不一致（即信号反转）
  - 其余核心算法（MACD 平滑差、ADX 自适应权重、修正信号判定、临界价计算）全部保留

用法示例：
  python pro3_singletimeframe.py --symbol LC2609 --short 1H --year 2025 --leverage 10.0
"""
import re
import math
import sys
import os
import pandas as pd
import numpy as np
import backtrader as bt

# 使用项目统一的 akshare 期货数据接口
# 数据接口：统一走显式包路径（迁移前靠 sys.path 兜底，已移除）
from crypto.futures.akshare_api import AkshareFuturesAPI, AkshareAPIError



# ----------------------------------------------------------------
# 数据获取适配函数（替代原 akshare_futures_demo 接口）
# ----------------------------------------------------------------

def _get_futures_daily_data(symbol: str) -> pd.DataFrame:
    """获取日线数据，自动判断主力合约或具体合约"""
    try:
        # 如果是具体合约（含数字后缀，如 IF2506），用 fetch_daily_contract
        if re.search(r'\d$', symbol):
            return AkshareFuturesAPI.fetch_daily_contract(symbol)
        else:
            return AkshareFuturesAPI.fetch_daily_main(symbol)
    except AkshareAPIError:
        raise
    except Exception as e:
        raise ValueError(f"获取日线数据失败 [{symbol}]: {e}")


def _get_futures_minute_data(symbol: str, period: str = '60') -> pd.DataFrame:
    """获取分钟K线数据"""
    return AkshareFuturesAPI.fetch_minute_kline(symbol, period=period)


CURRENT_SYMBOL = ""
CURRENT_BAR = ""


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
    try:
        return ''.join([ch for ch in symbol if ch.isalpha()]) or symbol
    except Exception:
        return symbol


def get_period_data(symbol: str, bar: str, year: int | None = None, prefer_contract_daily: bool = False) -> pd.DataFrame:
    """获取指定周期的K线数据（保留与原版一致的多种数据源回退逻辑）。"""
    bar = str(bar)
    if bar in DAILY_BARS:
        df = None
        primary_symbol = symbol if prefer_contract_daily else _resolve_daily_symbol(symbol)
        fallback_symbol = _resolve_daily_symbol(symbol) if prefer_contract_daily else symbol
        try:
            df = _get_futures_daily_data(symbol=primary_symbol)
        except Exception as e:
            print(f"获取 {primary_symbol} 日线数据异常: {e}")
            df = None
        if df is None or df.empty:
            try:
                print(f"尝试备用日线数据: {fallback_symbol}")
                df = _get_futures_daily_data(symbol=fallback_symbol)
            except Exception as e2:
                print(f"备用日线数据获取异常: {e2}")
                df = None
        if df is None or df.empty:
            print(f"日线数据为空，改用 {symbol} 的 60 分钟数据聚合为日线")
            df_min = _get_futures_minute_data(symbol=symbol, period='60')
            if df_min is None or df_min.empty:
                raise ValueError(f"分钟数据为空无法回退: {symbol} 60")
            ts_col = 'datetime' if 'datetime' in df_min.columns else (
                'date' if 'date' in df_min.columns else (
                    '日期' if '日期' in df_min.columns else (
                        '时间' if '时间' in df_min.columns else None
                    )
                )
            )
            if ts_col is None:
                raise ValueError(f"未找到时间列: {list(df_min.columns)}")
            df_min['timestamp'] = pd.to_datetime(df_min[ts_col])
            df_min = _ensure_ohlc(df_min)
            df_resampled = (
                df_min[['timestamp', 'open', 'high', 'low', 'close']]
                .dropna()
                .sort_values('timestamp')
                .set_index('timestamp')
                .resample('D')
                .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                .dropna()
            )
            df = df_resampled.reset_index()
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        else:
            ts_col = 'date' if 'date' in df.columns else (
                'datetime' if 'datetime' in df.columns else (
                    '日期' if '日期' in df.columns else (
                        '时间' if '时间' in df.columns else None
                    )
                )
            )
            if ts_col is None:
                raise ValueError(f"未找到日期列: {list(df.columns)}")
            df['timestamp'] = pd.to_datetime(df[ts_col])
            df = _ensure_ohlc(df)
    elif bar in WEEKLY_BARS:
        df_daily = None
        primary_symbol = symbol if prefer_contract_daily else _resolve_daily_symbol(symbol)
        fallback_symbol = _resolve_daily_symbol(symbol) if prefer_contract_daily else symbol
        try:
            df_daily = _get_futures_daily_data(symbol=primary_symbol)
        except Exception as e:
            print(f"获取 {primary_symbol} 日线数据异常（用于周线聚合）: {e}")
            df_daily = None
        if df_daily is None or df_daily.empty:
            try:
                print(f"尝试备用日线数据用于周线聚合: {fallback_symbol}")
                df_daily = _get_futures_daily_data(symbol=fallback_symbol)
            except Exception as e2:
                print(f"备用日线数据获取异常: {e2}")
                df_daily = None
        df = None
        if df_daily is not None and not df_daily.empty:
            ts_col = 'date' if 'date' in df_daily.columns else (
                'datetime' if 'datetime' in df_daily.columns else (
                    '日期' if '日期' in df_daily.columns else (
                        '时间' if '时间' in df_daily.columns else None
                    )
                )
            )
            if ts_col is None:
                raise ValueError(f"未找到日期列: {list(df_daily.columns)}")
            df_daily['timestamp'] = pd.to_datetime(df_daily[ts_col])
            df_daily = _ensure_ohlc(df_daily)
            df_week = (
                df_daily[['timestamp', 'open', 'high', 'low', 'close']]
                .dropna()
                .sort_values('timestamp')
                .set_index('timestamp')
                .resample('W-FRI')
                .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                .dropna()
            )
            df = df_week.reset_index()
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        else:
            print(f"日线数据为空，改用 {symbol} 的 60 分钟数据聚合为周线")
            df_min = _get_futures_minute_data(symbol=symbol, period='60')
            if df_min is None or df_min.empty:
                raise ValueError(f"分钟数据为空无法回退到周线: {symbol} 60")
            ts_col = 'datetime' if 'datetime' in df_min.columns else (
                'date' if 'date' in df_min.columns else (
                    '日期' if '日期' in df_min.columns else (
                        '时间' if '时间' in df_min.columns else None
                    )
                )
            )
            if ts_col is None:
                raise ValueError(f"未找到时间列: {list(df_min.columns)}")
            df_min['timestamp'] = pd.to_datetime(df_min[ts_col])
            df_min = _ensure_ohlc(df_min)
            df_week = (
                df_min[['timestamp', 'open', 'high', 'low', 'close']]
                .dropna()
                .sort_values('timestamp')
                .set_index('timestamp')
                .resample('W-FRI')
                .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                .dropna()
            )
            df = df_week.reset_index()
            df['timestamp'] = pd.to_datetime(df['timestamp'])
    else:
        period = BAR_MINUTE_MAP.get(bar, None)
        if period is None:
            raise ValueError(f"不支持的bar周期: {bar}")
        df = _get_futures_minute_data(symbol=symbol, period=period)
        if df is None or df.empty:
            raise ValueError(f"分钟数据为空: {symbol} {period}")
        ts_col = 'datetime' if 'datetime' in df.columns else (
            'date' if 'date' in df.columns else (
                '日期' if '日期' in df.columns else (
                    '时间' if '时间' in df.columns else None
                )
            )
        )
        if ts_col is None:
            raise ValueError(f"未找到时间列: {list(df.columns)}")
        df['timestamp'] = pd.to_datetime(df[ts_col])
        df = _ensure_ohlc(df)

    for col in ['open', 'high', 'low', 'close']:
        if col not in df.columns:
            raise ValueError(f"数据缺少列: {col}")
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df[['timestamp', 'open', 'high', 'low', 'close']].dropna()
    df = df.sort_values('timestamp').set_index('timestamp')
    if year is not None:
        try:
            df = df.loc[df.index.year == int(year)]
        except Exception:
            pass

    df = calculate_adx(df, period=14)
    return df


def calculate_adx(df, period=14):
    """计算 ADX 指标（与原版完全一致）。"""
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


def calculate_sar(df, af_start=0.02, af_step=0.02, af_max=0.20):
    """计算抛物线 SAR 指标（标准算法）。"""
    high = df['high'].values
    low = df['low'].values
    n = len(df)
    sar = np.zeros(n)
    trend = np.ones(n, dtype=int)
    ep = np.zeros(n)
    af = np.zeros(n)

    if n >= 2:
        sar[0] = low[0]
        trend[0] = 1 if high[1] > high[0] else -1
        ep[0] = high[0] if trend[0] == 1 else low[0]
    else:
        sar[0] = low[0]
        trend[0] = 1
        ep[0] = high[0]
    af[0] = af_start

    for i in range(1, n):
        prev_sar, prev_trend, prev_ep, prev_af = sar[i-1], trend[i-1], ep[i-1], af[i-1]
        sar[i] = prev_sar + prev_af * (prev_ep - prev_sar)

        if prev_trend == 1:
            sar[i] = min(sar[i], low[i-1], low[i])
            if low[i] < sar[i]:
                trend[i] = -1
                sar[i] = max(high[i], high[i-1])
                ep[i] = low[i]
                af[i] = af_start
            else:
                trend[i] = 1
                if high[i] > prev_ep:
                    ep[i] = high[i]
                    af[i] = min(prev_af + af_step, af_max)
                else:
                    ep[i], af[i] = prev_ep, prev_af
        else:
            sar[i] = max(sar[i], high[i-1], high[i])
            if high[i] > sar[i]:
                trend[i] = 1
                sar[i] = min(low[i], low[i-1])
                ep[i] = high[i]
                af[i] = af_start
            else:
                trend[i] = -1
                if low[i] < prev_ep:
                    ep[i] = low[i]
                    af[i] = min(prev_af + af_step, af_max)
                else:
                    ep[i], af[i] = prev_ep, prev_af

    df['SAR'] = sar
    df['SAR_TREND'] = ['↑' if t == 1 else '↓' for t in trend]
    return df


def get_adaptive_smooth_weight(adx_value, base_weight=0.7):
    """根据ADX计算自适应平滑权重（与VCO完全一致，无下限钳制）"""
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    curr_weight = 1.0 - hist_weight
    return hist_weight, curr_weight


def get_latest_data(symbol, bar="1H", year: int | None = None, leverage: float = 10.0):
    """
    【单周期版本】仅获取短周期数据，不进行长周期趋势确认。
    返回：latest_data, last_trade_data, atr_like, direction
    注意：direction 字段为 None（单周期版本无长周期方向）
    """
    print(f"正在获取周期数据 ({bar})...")
    df_all = get_period_data(symbol, bar, year=None)
    if df_all is None or len(df_all) == 0:
        raise ValueError(f"周期数据为空: {symbol} {bar}")
    target_year = year if year is not None else int(pd.Timestamp(df_all.index[-1]).year)
    df = df_all.loc[df_all.index.year == target_year]

    if target_year is not None:
        print(f"本年度({target_year})数据条数: {len(df)}")
    try:
        print(f"数据起止日期： {df.index.min().date()} ~ {df.index.max().date()}")
    except Exception:
        pass

    # 预计算EMA/MACD，供策略使用
    try:
        df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
        df['EMA26'] = df['close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = df['EMA12'] - df['EMA26']
        df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['MACD'] = 2 * (df['DIF'] - df['DEA'])
    except Exception:
        df['EMA12'] = np.nan
        df['EMA26'] = np.nan
        df['DIF'] = 0.0
        df['DEA'] = 0.0
        df['MACD'] = 0.0
    try:
        df = calculate_adx(df)
    except Exception:
        df['ADX'] = 0.0

    # ---- 计算 SAR / BOLL / ATR 指标 ----
    try:
        df = calculate_sar(df)
    except Exception:
        df['SAR'] = 0.0
        df['SAR_TREND'] = '?'
    try:
        df['BOLL_MIDDLE'] = df['close'].rolling(window=20).mean()
        boll_std = df['close'].rolling(window=20).std()
        df['BOLL_UPPER'] = df['BOLL_MIDDLE'] + 2 * boll_std
        df['BOLL_LOWER'] = df['BOLL_MIDDLE'] - 2 * boll_std
    except Exception:
        df['BOLL_UPPER'] = 0.0
        df['BOLL_MIDDLE'] = 0.0
        df['BOLL_LOWER'] = 0.0
    try:
        df['ATR'] = df['TR'].rolling(window=14).mean()
    except Exception:
        df['ATR'] = 0.0

    df['HISTOGRAM_ZERO'] = 0.0
    df['HIST_SMOOTH_DIFF_ZERO'] = 0.0
    df['ORIGINAL_FLAG'] = None
    df['HIST_SMOOTH_DIFF'] = 0.0
    df['HIST_WEIGHT'] = 0.0
    df['CURR_WEIGHT'] = 0.0
    df['MODIFY_FLAG'] = None
    df['TRADE_PRICE'] = 0.0
    df['SMOOTHED_HIST'] = 0.0

    class SingleTimeframePro3(bt.Strategy):
        """
        Pro3 单周期版本策略 —— 基于 MACD 平滑差 + ADX 自适应权重生成交易信号。
        开仓：无持仓 + 信号变化
        平仓：持仓方向与当前信号方向不一致（即信号反转）
        """
        params = (
            ('times', 10.0),
            ('base_money', 100.0),
        )
        def __init__(self):
            # 指标使用 pandas 预计算值（ewm(adjust=False)标准算法），不再依赖 Backtrader 内置指标
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
            self.contract_money = self.base_money
            self.fix_money = self.base_money
            self.mix_money = self.base_money
            self.trades = []
            self.trade_times = []
            self.profits = []
            self.max_wins = []
            self.max_losses = []
            # ---- 单周期版本：无长周期方向 ----
            self.current_position = None
            self.total_signals = 0
            self.bar_seq = 0
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
            current_idx = len(self) - 1
            adx_value = df.iloc[current_idx]['ADX'] if current_idx < len(df) else 0

            # 使用 pandas 预计算的 MACD 值（ewm(adjust=False)标准算法，无需 warmup）
            macd_value = df.iloc[current_idx]['DIF']
            signal_value = df.iloc[current_idx]['DEA']
            histogram = df.iloc[current_idx]['MACD']

            hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)
            self.weight_history.append((hist_weight, curr_weight))

            if len(self.smoothed_hist) == 0:
                smoothed_histogram = histogram
            else:
                smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
            self.smoothed_hist.append(smoothed_histogram)
            df.loc[self.data.datetime.datetime(0), 'SMOOTHED_HIST'] = smoothed_histogram

            hist_smooth_diff = histogram - smoothed_histogram

            histogram_zero = 0.0
            hist_smooth_diff_zero = 0.0
            if len(self.data) > 1 and current_idx > 0:
                prev_dea = df.iloc[current_idx - 1]['DEA']
                prev_ema12 = df.iloc[current_idx - 1]['EMA12']
                prev_ema26 = df.iloc[current_idx - 1]['EMA26']
                prev_smoothed = self.smoothed_hist[-2] if len(self.smoothed_hist) > 1 else 0
                histogram_zero = (prev_dea - (prev_ema12 * 11.0/13.0) + (prev_ema26 * 25.0/27.0)) / (2.0/13.0 - 2.0/27.0)
                if curr_weight != 0:
                    hist_smooth_diff_zero = (prev_dea - (prev_ema12 * 11.0/13.0) + (prev_ema26 * 25.0/27.0) + (prev_smoothed * hist_weight / curr_weight)) / (2.0/13.0 - 2.0/27.0)
                else:
                    hist_smooth_diff_zero = histogram_zero
                df.loc[self.data.datetime.datetime(0), 'HISTOGRAM_ZERO'] = histogram_zero
                df.loc[self.data.datetime.datetime(0), 'HIST_SMOOTH_DIFF_ZERO'] = hist_smooth_diff_zero

            if histogram > smoothed_histogram and self.previous_diff is not None and self.previous_diff < 0:
                self.last_trade_index = len(self) - 1
                self.original_flag = 'rise'
            elif histogram < smoothed_histogram and self.previous_diff is not None and self.previous_diff > 0:
                self.last_trade_index = len(self) - 1
                self.original_flag = 'fall'

            df.loc[self.data.datetime.datetime(0), 'ORIGINAL_FLAG'] = self.original_flag
            self.previous_diff = hist_smooth_diff

            df.loc[self.data.datetime.datetime(0), 'HIST_SMOOTH_DIFF'] = hist_smooth_diff
            df.loc[self.data.datetime.datetime(0), 'HIST_WEIGHT'] = hist_weight
            df.loc[self.data.datetime.datetime(0), 'CURR_WEIGHT'] = curr_weight

            # ---- VCO信号：用 MACD平滑差 (histogram - smoothed_histogram) 正负号直接判定趋势方向 ----
            if hist_smooth_diff > 0:
                self.modify_flag = "rise"
            elif hist_smooth_diff < 0:
                self.modify_flag = "fall"

            df.loc[self.data.datetime.datetime(0), 'MODIFY_FLAG'] = self.modify_flag

            # ---- 单周期版本：交易逻辑（无长周期方向过滤） ----
            signal_changed = self.modify_flag != getattr(self, 'last_modify_flag', None)

            # 平仓：有持仓 + 信号方向与持仓方向不一致（即信号反转）
            if self.current_position is not None and self.current_position != self.modify_flag:
                trade_price = self.data.open[0]
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
                self.last_position_type = self.current_position
                self.current_position = None

            # 开仓：无持仓 + 信号变化（VCO: 默认开盘价入场）
            elif self.current_position is None and signal_changed:
                self.total_signals += 1
                self.k += 1
                trade_price = self.data.open[0]
                df.loc[self.data.datetime.datetime(0), 'TRADE_PRICE'] = trade_price
                self.trades.append(trade_price)
                self.entries.append(trade_price)
                self.trade_times.append(self.data.datetime.datetime(0))
                self.entry_times.append(self.data.datetime.datetime(0))
                self.max_wins.append(0.0)
                self.max_losses.append(0.0)
                self.current_position = self.modify_flag

            # 记录持仓期间的最大盈亏
            if len(self.trades) > 0 and self.current_position is not None:
                last_trade_price = self.trades[-1]
                if self.current_position == 'rise':
                    potential_win = ((self.data.high[0] / last_trade_price) - 1) * 100 * self.times
                    potential_loss = ((self.data.low[0] / last_trade_price) - 1) * 100 * self.times
                else:
                    potential_win = (1 - (self.data.low[0] / last_trade_price)) * 100 * self.times
                    potential_loss = (1 - (self.data.high[0] / last_trade_price)) * 100 * self.times
                self.max_wins[-1] = max(self.max_wins[-1], potential_win)
                self.max_losses[-1] = min(self.max_losses[-1], potential_loss)

            self.last_modify_flag = self.modify_flag

        def stop(self):
            total_trades = len(self.profits)
            winning_trades = sum(1 for p in self.profits if p > 0)
            win_rate = winning_trades / total_trades if total_trades > 0 else 0
            total_profit = sum(p for p in self.profits if p > 0)
            total_loss = sum(p for p in self.profits if p < 0)
            avg_profit = total_profit / winning_trades if winning_trades > 0 else 0
            avg_loss = total_loss / (total_trades - winning_trades) if (total_trades - winning_trades) > 0 else 0
            max_profit = max(self.profits) if self.profits else 0
            max_loss = min(self.profits) if self.profits else 0
            profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0
            returns = pd.Series(self.profits)
            sharpe_ratio = returns.mean() / returns.std() if len(returns) > 0 and returns.std() > 0 else 0

            print("\n=== 交易记录 ===")
            for i in range(len(self.profits)):
                entry = self.entries[i] if i < len(self.entries) else float('nan')
                exitp = self.exits[i] if i < len(self.exits) else float('nan')
                prof = self.profits[i]
                maxw = self.max_wins[i] if i < len(self.max_wins) else 0.0
                maxl = self.max_losses[i] if i < len(self.max_losses) else 0.0
                fix_b = int(self.fix_before[i]) if i < len(self.fix_before) else 0
                fix_a = int(self.fix_after[i]) if i < len(self.fix_after) else 0
                con_b = int(self.contract_before[i]) if i < len(self.contract_before) else 0
                con_a = int(self.contract_after[i]) if i < len(self.contract_after) else 0
                mix_b = int(self.mix_before[i]) if i < len(self.mix_before) else 0
                mix_a = int(self.mix_after[i]) if i < len(self.mix_after) else 0
                print(f"第{i+1}次交易盈利   开仓:{entry:.3f} 平仓:{exitp:.3f}   最终盈利:{prof:.2f}%        最大盈利:{maxw:.2f}%        最大亏损:{maxl:.2f}%  定投:{fix_b}→{fix_a}({fix_a-fix_b:+d})        复投:{con_b}→{con_a}({con_a-con_b:+d})      混投:{mix_b}→{mix_a}({mix_a-mix_b:+d})")

            print("\nPro3版：MACD平滑+ADX自适应 【单周期版本】 - 交易统计")
            print("=======================================================")
            comp_curve = [100.0]
            fix_curve = [100.0]
            for p in self.profits:
                comp_curve.append(comp_curve[-1] * (1 + p * 0.01))
                fix_curve.append(fix_curve[-1] + 100.0 * p * 0.01)
            def _max_dd(curve):
                peak = curve[0]
                max_dd = 0.0
                for v in curve:
                    peak = max(peak, v)
                    dd = (peak - v) / peak
                    max_dd = max(max_dd, dd)
                return max_dd
            comp_dd = _max_dd(comp_curve)
            fix_dd = _max_dd(fix_curve)
            profit_factor = (total_profit / abs(total_loss)) if total_loss != 0 else 0
            print(f"交易对: {CURRENT_SYMBOL if CURRENT_SYMBOL else 'FUTURES'}")
            print(f"周期: {CURRENT_BAR}")
            print(f"策略类型: 单周期（无长周期趋势确认）")
            print(f"总交易次数: {total_trades}")
            print(f"胜率: {win_rate:.2%}")
            print(f"复投最终收益: {self.contract_money:.2f}")
            print(f"定投最终收益: {self.fix_money:.2f}")
            print(f"混合最终收益: {self.mix_money:.2f}")
            print(f"总盈利: {total_profit:.2f}")
            print(f"总亏损: {total_loss:.2f}")
            print(f"平均盈利: {avg_profit:.2f}")
            print(f"平均亏损: {avg_loss:.2f}")
            print(f"最大单笔盈利: {max_profit:.2f}")
            print(f"最大单笔亏损: {max_loss:.2f}")
            print(f"盈亏比: {profit_loss_ratio:.2f}")
            try:
                if len(self.exit_times) == len(self.profits) and len(self.profits) > 0:
                    df_month = pd.DataFrame({'time': pd.to_datetime(self.exit_times), 'profit_pct': self.profits})
                    df_month['month'] = df_month['time'].dt.to_period('M')
                    monthly_sum = df_month.groupby('month')['profit_pct'].sum()
                    monthly_avg = float(monthly_sum.mean()) if len(monthly_sum) > 0 else 0.0
                    print(f"每月定投平均收益率: {monthly_avg:.2f}%（按平仓月份累计）")
            except Exception:
                pass
            print(f"复投最大回撤率: {comp_dd*100:.2f}%")
            print(f"定投最大回撤率: {fix_dd*100:.2f}%")
            calmar = (((comp_curve[-1] / comp_curve[0]) - 1.0) / comp_dd) if comp_dd > 0 else 0.0
            print(f"卡玛比率: {calmar:.2f}")
            print(f"盈利因子: {profit_factor:.2f}")

    cerebro = bt.Cerebro()
    global CURRENT_SYMBOL, CURRENT_BAR
    CURRENT_SYMBOL = symbol
    CURRENT_BAR = bar
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(SingleTimeframePro3, times=leverage)
    strats = cerebro.run()
    strat = strats[0]

    # ---- 逐根K线指标输出 ----
    print("\n" + "=" * 150)
    print("【单周期策略】逐根K线指标明细（MACD平滑+ADX自适应）")
    print("=" * 150)
    col_names = ["#", "日期", "收盘", "DIF", "DEA", "MACD", "平滑柱", "平滑差",
                 "原信号", "修正信号", "ADX", "ATR", "SAR", "方向", "布林中轨"]
    col_widths = [4, 10, 10, 9, 9, 9, 9, 9, 6, 6, 5, 7, 9, 4, 10]
    header = "".join(f"{n:<{w}}" for n, w in zip(col_names, col_widths))
    print(header)
    print("-" * sum(col_widths))

    def _fmt(v, fmt_spec=".2f", default="-"):
        try:
            if pd.isna(v):
                return default
            return format(float(v), fmt_spec)
        except (ValueError, TypeError):
            return default

    df_out = df.copy()
    for col in ['ORIGINAL_FLAG', 'MODIFY_FLAG', 'SAR_TREND']:
        if col in df_out.columns:
            df_out[col] = df_out[col].fillna('-')

    for i, (idx, row) in enumerate(df_out.iterrows()):
        date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, 'strftime') else str(idx)[:10]
        o_flag = str(row.get('ORIGINAL_FLAG', '-'))[:4]
        m_flag = str(row.get('MODIFY_FLAG', '-'))[:4]
        s_trend = str(row.get('SAR_TREND', '-'))
        vals = [
            f"{i+1:<4}", f"{date_str:<10}",
            f"{_fmt(row['close']):>10}",
            f"{_fmt(row['DIF']):>9}", f"{_fmt(row['DEA']):>9}", f"{_fmt(row['MACD']):>9}",
            f"{_fmt(row['SMOOTHED_HIST']):>9}", f"{_fmt(row['HIST_SMOOTH_DIFF']):>9}",
            f"{o_flag:<6}", f"{m_flag:<6}",
            f"{_fmt(row['ADX'], '.1f'):>5}", f"{_fmt(row['ATR']):>7}",
            f"{_fmt(row['SAR']):>9}", f"{s_trend:<4}",
            f"{_fmt(row['BOLL_MIDDLE']):>10}",
        ]
        print("".join(vals))

    print("-" * sum(col_widths))
    print(f"共 {len(df)} 根K线")

    last_row = df.iloc[-1]
    atr_like = float(last_row['ATR_ADX']) if 'ATR_ADX' in df.columns else 0.0
    # 单周期版本 direction 返回 None
    return last_row, strat.trades[-1] if strat.trades else np.nan, atr_like, None


def main(symbol: str = 'LC2609', bar: str = '1H', year: int | None = None, leverage: float = 10.0):
    """
    【单周期版本】入口函数 —— 仅含临界值策略。
    仅使用单个周期的数据进行回测，不进行长周期趋势确认。
    """
    global CURRENT_SYMBOL, CURRENT_BAR
    CURRENT_SYMBOL = symbol
    CURRENT_BAR = bar
    latest_data, last_trade_data, atr, direction = get_latest_data(symbol, bar, year=year, leverage=leverage)
    print("\n=== 方案Pro3 【单周期版本】: ADX自适应（无长周期趋势确认） ===")
    print(f"交易对: {symbol}")
    print(f"周期: {bar} (交易信号)")
    print(f"策略类型: 单周期 —— 仅基于 MACD 平滑差 + ADX 自适应权重生成信号（临界值策略）")
    print(f"最新行情时间： {latest_data.name}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Pro3 单周期期货策略（MACD平滑+ADX自适应，无长周期趋势确认）')
    parser.add_argument('--symbol', type=str, default='LC2609', help='合约代码，如 RB2410')
    parser.add_argument('--short', type=str, default='1D', help='交易周期，如 1H / 1D')
    parser.add_argument('--year', type=int, default=None, help='过滤年度，如 2024；默认自动采用数据最后一条的年度')
    parser.add_argument('--leverage', type=float, default=10.0, help='杠杆倍数，用于收益计算显示')
    args = parser.parse_args()
    main(symbol=args.symbol, bar=args.short, year=args.year, leverage=args.leverage)
