# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/version_close_open.py  →  现位置: crypto/market_strategy/version_close_open.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
# type: ignore
# -*- coding: utf-8 -*-
"""
趋势策略模版（双周期 + MACD/ADX）— 期货版
==========================================
核心机制：短周期生成交易信号；长周期确认趋势方向
数据源：akshare 期货数据 API
输出控制：行情输出、交易操作与决策输出、交易记录输出
"""
import math
import os
import sys
import time
import re
import datetime as dt
from typing import Optional, Dict

import backtrader as bt
import numpy as np
import pandas as pd

# ====================================================================
#  数据接口：使用项目统一的 akshare 期货数据接口
# ====================================================================
# 数据接口：统一走显式包路径（迁移前靠 sys.path 兜底，已移除）
from crypto.futures.akshare_api import AkshareFuturesAPI, AkshareAPIError

# 全局运行态变量
CURRENT_INSTID = ""
CURRENT_SHORT_BAR = ""
CURRENT_LONG_BAR = ""

PRINT_MARKET = 0
PRINT_TRADE_OPS = 0
PRINT_TRADE_RECORDS = 0
PRINT_ORIGINAL_OUTPUT = 1

ENTRY_PRICE_TYPE = 'close'
EXIT_PRICE_TYPE = 'open'

GLOBAL_DF_1M = None

# ── 期货数据辅助常量 ──
BAR_MINUTE_MAP = {
    '1m': '1', '5m': '5', '15m': '15', '30m': '30',
    '60m': '60', '1H': '60',
}
DAILY_BARS = {'1D', 'D', 'day', 'daily'}
WEEKLY_BARS = {'1W', 'W', 'week', 'weekly'}

_contract_cache: Dict[str, str] = {}
_contract_cache_time: float = 0.0


# ====================================================================
#  期货数据获取（复用 pro3_dualtimeframe 的数据管线）
# ====================================================================

def _ensure_ohlc(df: pd.DataFrame) -> pd.DataFrame:
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
    m = re.match(r"^([A-Za-z]+)", symbol)
    return m.group(1).upper() if m else symbol.upper()


def _resolve_minute_contract(symbol: str) -> str:
    global _contract_cache, _contract_cache_time
    if re.search(r'\d', symbol):
        return symbol
    code = symbol.upper()
    now = time.time()
    if code in _contract_cache and (now - _contract_cache_time) < 300:
        return _contract_cache[code]
    try:
        df_rt = AkshareFuturesAPI.fetch_realtime(code)
        if df_rt is not None and not df_rt.empty:
            df_sorted = df_rt.copy()
            if 'position' in df_sorted.columns:
                df_sorted['position'] = pd.to_numeric(df_sorted['position'], errors='coerce')
                df_sorted = df_sorted.sort_values('position', ascending=False)
            elif 'volume' in df_sorted.columns:
                df_sorted['volume'] = pd.to_numeric(df_sorted['volume'], errors='coerce')
                df_sorted = df_sorted.sort_values('volume', ascending=False)
            main_sym = str(df_sorted.iloc[0].get('symbol', '')).strip().upper()
            if main_sym and len(main_sym) >= 3:
                _contract_cache[code] = main_sym
                _contract_cache_time = now
                print(f"品种 {code}: 实时行情解析主力合约 → {main_sym}")
                return main_sym
    except Exception as e:
        print(f"品种 {code}: 实时行情解析失败 ({e})")
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
    contract = f"{symbol}{yy}{target_month:02d}"
    _contract_cache[code] = contract
    _contract_cache_time = now
    return contract


def _fetch_minute_with_retry(contract_symbol, period, max_retries=3):
    for attempt in range(max_retries + 1):
        try:
            df = AkshareFuturesAPI.fetch_minute_kline(contract_symbol, period=period)
            if df is not None and not df.empty:
                return df
            return df
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
    return None


def _fetch_daily_with_retry(code, start_date, end_date, max_retries=3):
    for attempt in range(max_retries + 1):
        try:
            df = AkshareFuturesAPI.fetch_daily_main(code, start_date, end_date)
            if df is not None and not df.empty:
                return df
            return df
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
    return None


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
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
        if ts_col in df.columns and ts_col != 'timestamp':
            df = df.drop(columns=[ts_col])
    for col in ['open', 'high', 'low', 'close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df[['timestamp', 'open', 'high', 'low', 'close']].dropna()
    df = df.sort_values('timestamp').set_index('timestamp')
    return df


def calculate_adx(df, period=14):
    """计算ADX指标"""
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


def get_period_data(symbol: str, bar: str, backtest_days: int = 200) -> pd.DataFrame:
    """获取期货K线数据（兼容分钟/日线/周线）"""
    bar = str(bar)
    today = dt.date.today()
    end_date_str = today.strftime('%Y%m%d')

    if bar in DAILY_BARS:
        fetch_days = max(backtest_days + 60, 365)
        start_date_str = (today - dt.timedelta(days=fetch_days)).strftime('%Y%m%d')
        primary_code = _resolve_daily_symbol(symbol)
        df = _fetch_daily_with_retry(primary_code, start_date_str, end_date_str)
        if (df is None or df.empty) and primary_code != symbol:
            df = _fetch_daily_with_retry(symbol, start_date_str, end_date_str)
        if df is None or df.empty:
            raise ValueError(f"日线数据为空: {symbol}")
        df = _normalize_columns(df)
        print(f"[{bar}] 日线 {primary_code} → {len(df)} 条")

    elif bar in WEEKLY_BARS:
        fetch_days = max(backtest_days + 90, 730)
        start_date_str = (today - dt.timedelta(days=fetch_days)).strftime('%Y%m%d')
        primary_code = _resolve_daily_symbol(symbol)
        df_daily = _fetch_daily_with_retry(primary_code, start_date_str, end_date_str)
        if (df_daily is None or df_daily.empty) and primary_code != symbol:
            df_daily = _fetch_daily_with_retry(symbol, start_date_str, end_date_str)
        if df_daily is not None and not df_daily.empty:
            df_daily = _normalize_columns(df_daily)
            df = df_daily.resample('W-FRI').agg(
                {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
            print(f"[周线] 日线聚合 → {len(df)} 条周K线")
        else:
            raise ValueError(f"周线数据为空: {symbol}")
    else:
        period = BAR_MINUTE_MAP.get(bar, None)
        if period is None:
            raise ValueError(f"不支持的bar周期: {bar}")
        contract_symbol = _resolve_minute_contract(symbol)
        df = _fetch_minute_with_retry(contract_symbol, period)
        if (df is None or df.empty) and contract_symbol != symbol:
            df = _fetch_minute_with_retry(symbol, period)
        if df is None or df.empty:
            raise ValueError(f"分钟数据为空: {symbol}")
        df = _normalize_columns(df)
        print(f"[{bar}] 分钟K线 {contract_symbol} → {len(df)} 条")

    if backtest_days and backtest_days > 0:
        cutoff = df.index.max() - pd.Timedelta(days=backtest_days)
        df = df[df.index >= cutoff]
        print(f"  回看 {backtest_days} 天: 最终 {len(df)} 条")

    if len(df) < 20:
        raise ValueError(f"数据量不足: {len(df)} 条")

    df = calculate_adx(df, period=14)
    return df


# ====================================================================
#  原有策略逻辑（保持不变）
# ====================================================================

def _normalize_dir(v):
    s = str(v)
    if s in ('rise', '多'):
        return 'rise'
    if s in ('fall', '空'):
        return 'fall'
    return None


def _to_pandas_freq(bar):
    s = str(bar).strip()
    sl = s.lower()
    for suffix, freq_map in [('m', 'T'), ('h', 'h'), ('d', 'D'), ('w', 'W')]:
        if sl.endswith(suffix):
            n = ''.join(ch for ch in sl if ch.isdigit()) or '1'
            return f"{n}{freq_map}"
    return s


def _resample_ohlc(df_1m, bar):
    freq = _to_pandas_freq(bar)
    ohlc = df_1m.resample(freq, label='right', closed='right').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    try:
        ohlc.index = ohlc.index.tz_localize(None)
    except Exception:
        pass
    ohlc['confirm'] = 1.0
    ohlc = calculate_adx(ohlc, period=14)
    return ohlc


def get_adaptive_smooth_weight(adx_value, base_weight=0.7):
    """根据ADX计算自适应平滑权重"""
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    return hist_weight, 1.0 - hist_weight


class _MACDDirectionCalculator:
    """共享的MACD平滑方向计算状态机"""

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


def _run_direction_calc(df, fill_series=False):
    """运行方向计算策略"""
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


def calculate_period_direction(df):
    """计算长周期方向，返回最新modify_flag"""
    strat = _run_direction_calc(df, fill_series=False)
    return strat.final_direction


def fill_long_direction_series(df):
    """填充长周期方向序列到df的LONG_DIRECTION列"""
    _run_direction_calc(df, fill_series=True)


def _align_long_direction(df, df_long):
    """将长周期方向对齐到短周期df"""
    if 'LONG_DIRECTION' not in df_long.columns:
        df['LONG_DIRECTION'] = np.nan
        return
    try:
        if hasattr(df.index, 'tz') and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        if hasattr(df_long.index, 'tz') and df_long.index.tz is not None:
            df_long.index = df_long.index.tz_localize(None)
        short_times = pd.DataFrame({'ts': pd.to_datetime(df.index)})
        long_times = pd.DataFrame({'ts': pd.to_datetime(df_long.index), 'LONG_DIRECTION': df_long['LONG_DIRECTION'].values})
        aligned = pd.merge_asof(short_times.sort_values('ts'), long_times.sort_values('ts'), on='ts', direction='backward')
        df['LONG_DIRECTION'] = aligned['LONG_DIRECTION'].values
    except Exception:
        df['LONG_DIRECTION'] = df_long['LONG_DIRECTION'].reindex(df.index, method='ffill')
    first_valid = df['LONG_DIRECTION'].first_valid_index()
    if first_valid is not None:
        df.drop(df.index[df.index < first_valid], inplace=True)


def _calc_mix_base(mix_money, base_money):
    ratio = mix_money / base_money
    if ratio > 0:
        try:
            log_value = math.log(ratio) / math.log(1.6)
            if not math.isnan(log_value) and not math.isinf(log_value):
                return max(base_money * pow(1.6, math.floor(log_value)), base_money)
        except (ValueError, OverflowError):
            pass
    return base_money


def _calc_drawdown(curve):
    if len(curve) <= 1:
        return 0
    peak = curve[0]
    max_dd = 0
    for value in curve:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return max_dd


def get_latest_data(symbol, short_bar="1H", long_bar="1D", backtest_days=200):
    """获取期货双周期数据并执行策略"""
    global CURRENT_INSTID, CURRENT_SHORT_BAR, CURRENT_LONG_BAR
    global GLOBAL_DF_1M

    CURRENT_INSTID = symbol
    CURRENT_SHORT_BAR = short_bar
    CURRENT_LONG_BAR = long_bar

    if PRINT_MARKET:
        print(f"正在获取短周期数据 ({short_bar})...")

    # 获取短周期数据（期货数据源）
    df_short_raw = get_period_data(symbol, short_bar, backtest_days=backtest_days)

    if PRINT_MARKET:
        print(f"正在获取长周期数据 ({long_bar})...")
        print("冷却5秒后获取长周期...")
    time.sleep(5.0)

    # 获取长周期数据
    base_sym = _resolve_daily_symbol(symbol)
    try:
        df_long_raw = get_period_data(base_sym, long_bar, backtest_days=backtest_days)
    except Exception:
        # 回退：用短周期数据聚合
        if long_bar in WEEKLY_BARS:
            df_long_raw = df_short_raw.resample('W-FRI').agg(
                {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
            df_long_raw = calculate_adx(df_long_raw, period=14)
        else:
            df_long_raw = df_short_raw.resample('D').agg(
                {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
            df_long_raw = calculate_adx(df_long_raw, period=14)

    if PRINT_MARKET:
        print("计算长周期方向...")

    long_direction = calculate_period_direction(df_long_raw)
    fill_long_direction_series(df_long_raw)

    if PRINT_MARKET:
        print(f"长周期方向: {long_direction}")

    df = df_short_raw.copy()

    # 初始化列
    for col in ['HISTOGRAM_ZERO', 'HIST_SMOOTH_DIFF_ZERO', 'DIF', 'DEA', 'MACD',
                'SMOOTHED_MACD', 'HIST_SMOOTH_DIFF', 'HIST_WEIGHT', 'CURR_WEIGHT', 'TRADE_PRICE']:
        df[col] = 0.0
    for col in ['ORIGINAL_FLAG', 'MODIFY_FLAG']:
        df[col] = None

    _align_long_direction(df, df_long_raw)

    class DualTimeframeStrategy(bt.Strategy):
        """双周期策略：短周期信号 + 长周期方向确认"""
        params = ()

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
            self.base_money = 100
            self.contract_money = self.base_money
            self.fix_money = self.base_money
            self.mix_money = self.base_money

            self.trades = []
            self.trade_times = []
            self.trade_dirs = []
            self.profits = []
            self.max_wins = []
            self.max_losses = []

            self.long_direction = long_direction
            self.current_position = None
            self.filtered_signals = 0
            self.total_signals = 0
            self.entry_price = None
            self._prev_entry_was_zero = False
            self.false_signals = 0
            self.true_signals = 0
            self.zero_entries = 0
            self.total_crossings = 0
            self._pending_signal_bar = None
            self._pending_signal_dir = None
            self._pending_signal_diff = None

        def next(self):
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

            # 零点价格计算与假信号检测
            interpolated_zero = 0.0
            if len(self.data) > 1:
                prev_dea = self.macd.signal[-1]
                prev_ema12 = self.ema12[-1]
                prev_ema26 = self.ema26[-1]
                prev_smoothed = self.smoothed_hist[-2] if len(self.smoothed_hist) > 1 else 0

                denom = 2.0 / 13.0 - 2.0 / 27.0
                numer_base = prev_dea - (prev_ema12 * 11.0 / 13.0) + (prev_ema26 * 25.0 / 27.0)
                histogram_zero = numer_base / denom

                if curr_weight != 0:
                    hist_smooth_diff_zero = (numer_base + prev_smoothed * hist_weight / curr_weight) / denom
                else:
                    hist_smooth_diff_zero = histogram_zero

                prev_diff_val = self.previous_diff if self.previous_diff is not None else 0.0
                if prev_diff_val != hist_smooth_diff:
                    ratio = max(0.0, min(1.0, abs(prev_diff_val) / abs(hist_smooth_diff - prev_diff_val)))
                    interpolated_zero = float(self.data.close[-1]) + ratio * (float(self.data.close[0]) - float(self.data.close[-1]))

                ts = self.data.datetime.datetime(0)
                df.loc[ts, 'HISTOGRAM_ZERO'] = histogram_zero
                df.loc[ts, 'HIST_SMOOTH_DIFF_ZERO'] = hist_smooth_diff_zero
                df.loc[ts, 'INTERPOLATED_ZERO'] = interpolated_zero

                # 假信号检测
                if self.previous_diff is not None and self.previous_diff * hist_smooth_diff < 0:
                    self.total_crossings += 1
                    self._pending_signal_bar = len(self.data)
                    self._pending_signal_dir = 'rise' if hist_smooth_diff > 0 else 'fall'
                    self._pending_signal_diff = hist_smooth_diff
                elif self._pending_signal_bar is not None:
                    if len(self.data) - self._pending_signal_bar >= 2:
                        if self._pending_signal_diff * hist_smooth_diff < 0:
                            self.false_signals += 1
                        else:
                            self.true_signals += 1
                        self._pending_signal_bar = None
                        self._pending_signal_dir = None
                        self._pending_signal_diff = None

            # 原始信号判断
            if self.previous_diff is not None:
                if histogram > smoothed_histogram and self.previous_diff < 0:
                    self.last_trade_index = len(self) - 1
                    self.original_flag = 'rise'
                elif histogram < smoothed_histogram and self.previous_diff > 0:
                    self.last_trade_index = len(self) - 1
                    self.original_flag = 'fall'

            ts = self.data.datetime.datetime(0)
            df.loc[ts, 'ORIGINAL_FLAG'] = self.original_flag
            self.previous_diff = hist_smooth_diff
            df.loc[ts, 'DIF'] = macd_value
            df.loc[ts, 'DEA'] = signal_value
            df.loc[ts, 'MACD'] = histogram
            df.loc[ts, 'SMOOTHED_MACD'] = smoothed_histogram
            df.loc[ts, 'HIST_SMOOTH_DIFF'] = hist_smooth_diff
            df.loc[ts, 'HIST_WEIGHT'] = hist_weight
            df.loc[ts, 'CURR_WEIGHT'] = curr_weight

            # modify_flag：短周期用hist_smooth_diff正负判定方向
            if hist_smooth_diff > 0:
                self.modify_flag = "rise"
            elif hist_smooth_diff < 0:
                self.modify_flag = "fall"
            df.loc[ts, 'MODIFY_FLAG'] = self.modify_flag

            # === 双周期交易逻辑 ===
            raw_long_dir = df.loc[ts, 'LONG_DIRECTION'] if 'LONG_DIRECTION' in df.columns else None
            if raw_long_dir is None:
                return
            bar_long_dir = _normalize_dir(raw_long_dir)
            directions_match = (self.modify_flag == bar_long_dir)
            signal_changed = self.modify_flag != getattr(self, 'last_modify_flag', None)

            # 情况1：有持仓 + 双周期不一致 → 平仓
            if self.current_position is not None and not directions_match:
                trade_price = self.data.close[0] if EXIT_PRICE_TYPE == 'close' else self.data.open[0]

                if self.current_position == "rise":
                    profit = ((trade_price / self.trades[-1]) - 1) * 100 * 10.0
                else:
                    profit = (1 - (trade_price / self.trades[-1])) * 100 * 10.0

                self.fix_money += self.base_money * profit * 0.01
                self.contract_money += self.contract_money * profit * 0.01
                self.mix_money += _calc_mix_base(self.mix_money, self.base_money) * profit * 0.01

                self.profits.append(profit)
                self.trades.append(trade_price)
                self.trade_times.append(ts)

                if PRINT_TRADE_OPS:
                    direction_name = '平多' if self.current_position == 'rise' else '平空'
                    print(f"{direction_name}(方向不一致) 第{self.k}次 价格{trade_price:.3f} "
                          f"持仓方向:{self.current_position} 短周期:{self.modify_flag} 长周期:{bar_long_dir} "
                          f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                          f"混合:{self.mix_money:.0f} 盈亏:{profit:.1f}% {ts}")

                self.current_position = None
                self.entry_price = None
                self.filtered_signals += 1

            # 情况2：无持仓 + 双周期一致 + 信号改变 → 开仓
            elif self.current_position is None and directions_match and signal_changed:
                self.total_signals += 1
                self.k += 1

                _bar_high = float(self.data.high[0])
                _bar_low = float(self.data.low[0])
                _used_zero = False
                if ENTRY_PRICE_TYPE == 'zero' and interpolated_zero > 0 and _bar_low <= interpolated_zero <= _bar_high:
                    trade_price = interpolated_zero
                    _used_zero = True
                    self.zero_entries += 1
                elif ENTRY_PRICE_TYPE == 'close':
                    trade_price = self.data.close[0]
                else:
                    trade_price = self.data.open[0]

                df.loc[ts, 'TRADE_PRICE'] = trade_price
                self._prev_entry_was_zero = _used_zero

                self.trades.append(trade_price)
                self.trade_times.append(ts)
                self.trade_dirs.append(self.modify_flag)
                self.max_wins.append(0.0)
                self.max_losses.append(0.0)

                self.current_position = self.modify_flag
                self.entry_price = trade_price

                if PRINT_TRADE_OPS:
                    direction_name = "开多" if self.modify_flag == "rise" else "开空"
                    print(f"{direction_name} 第{self.k}次 价格{trade_price:.3f} "
                          f"ADX={adx_value:.1f} 权重({hist_weight:.3f}/{curr_weight:.3f}) "
                          f"短周期:{self.modify_flag} 长周期:{bar_long_dir} "
                          f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                          f"混合:{self.mix_money:.0f} {ts}")

            # 情况3：方向不一致但无持仓 → 过滤信号
            elif self.current_position is None and not directions_match and signal_changed:
                self.filtered_signals += 1

            # 更新持仓盈亏
            if self.current_position is not None:
                current_price = self.data.close[0]
                last_trade_price = self.trades[-1]
                if self.current_position == 'rise':
                    current_profit = ((current_price / last_trade_price) - 1) * 100 * 10.0
                else:
                    current_profit = (1 - (current_price / last_trade_price)) * 100 * 10.0
                current_trade_idx = len(self.profits)
                if current_trade_idx < len(self.max_wins):
                    self.max_wins[current_trade_idx] = max(self.max_wins[current_trade_idx], max(current_profit, 0))
                    self.max_losses[current_trade_idx] = min(self.max_losses[current_trade_idx], min(current_profit, 0))

            self._prev_entry_was_zero = False
            self.last_modify_flag = self.modify_flag

        def stop(self):
            """策略结束统计"""
            if self._pending_signal_bar is not None:
                if self._pending_signal_diff is not None and self.previous_diff is not None:
                    if self._pending_signal_diff * self.previous_diff < 0:
                        self.false_signals += 1
                    else:
                        self.true_signals += 1
                self._pending_signal_bar = None

            total_trades = len(self.profits)
            winning_trades = sum(1 for p in self.profits if p > 0)
            losing_trades = sum(1 for p in self.profits if p < 0)
            win_rate = winning_trades / total_trades if total_trades > 0 else 0

            total_profit = sum(p for p in self.profits if p > 0)
            total_loss = sum(p for p in self.profits if p < 0)
            avg_profit = total_profit / winning_trades if winning_trades > 0 else 0
            avg_loss = total_loss / losing_trades if losing_trades > 0 else 0

            profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0

            if total_trades > 0:
                capital_curve = [self.base_money]
                cap = self.base_money
                for profit in self.profits:
                    cap *= (1 + profit * 0.01)
                    capital_curve.append(cap)
                max_drawdown = _calc_drawdown(capital_curve)
            else:
                max_drawdown = 0

            returns = pd.Series(self.profits)
            sharpe_ratio = returns.mean() / returns.std() if len(returns) > 0 and returns.std() > 0 else 0

            avg_hist_weight = np.mean([w[0] for w in self.weight_history]) if self.weight_history else 0
            avg_curr_weight = np.mean([w[1] for w in self.weight_history]) if self.weight_history else 0

            fix_money_history = [self.base_money]
            contract_money_history = [self.base_money]
            mix_money_history = [self.base_money]
            for profit in self.profits:
                fix_money_history.append(fix_money_history[-1] + self.base_money * profit * 0.01)
                contract_money_history.append(contract_money_history[-1] * (1 + profit * 0.01))
                base = _calc_mix_base(mix_money_history[-1], self.base_money)
                mix_money_history.append(mix_money_history[-1] + base * profit * 0.01)

            fix_drawdown = _calc_drawdown(fix_money_history)
            mix_drawdown = _calc_drawdown(mix_money_history)

            annual_return = ((self.contract_money / self.base_money) ** (252 / max(total_trades, 1)) - 1) * 100 if total_trades > 0 else 0
            calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0
            profit_factor = abs(total_profit / total_loss) if total_loss < 0 else 0

            start_time = self.trade_times[0] if self.trade_times else None
            end_time = self.trade_times[-1] if self.trade_times else None
            total_days = 0
            daily_profit = 0.0
            weekly_profit = 0.0
            if start_time and end_time:
                total_days = (end_time - start_time).days
                if total_days > 0:
                    total_return = (self.fix_money - self.base_money) / self.base_money * 100
                    daily_profit = total_return / total_days
                    weekly_profit = daily_profit * 7

            if PRINT_ORIGINAL_OUTPUT:
                print("\nVersion_Close_Open 期货版：MACD平滑+ADX自适应 + 双周期 - 交易统计")
                print("=" * 80)
                print(f"交易对: {CURRENT_INSTID}")
                print(f"短周期: {CURRENT_SHORT_BAR}")
                print(f"长周期: {CURRENT_LONG_BAR}")
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
                print(f"复投最终收益: {self.contract_money:.2f}")
                print(f"定投最终收益: {self.fix_money:.2f}")
                print(f"混合最终收益: {self.mix_money:.2f}")
                print("\n最终盈亏核心指标")
                print(f"最终盈利总和: {total_profit:.2f}%")
                print(f"最终亏损总和: {total_loss:.2f}%")
                print(f"最终盈利均值（仅盈利单）: {avg_profit:.2f}%")
                print(f"最终亏损均值（仅亏损单）: {avg_loss:.2f}%")
                print(f"最终盈亏均值比: {abs(avg_profit) / abs(avg_loss) if avg_loss != 0 else 0:.2f}")
                print(f"最终盈亏总和比: {abs(total_profit) / abs(total_loss) if total_loss != 0 else 0:.2f}")
                print("\n风险与绩效指标")
                print(f"复投最大回撤率: {max_drawdown:.2f}%")
                print(f"定投最大回撤率: {fix_drawdown:.2f}%")
                print(f"混合最大回撤率: {mix_drawdown:.2f}%")
                print(f"卡玛比率: {calmar_ratio:.2f}")
                print(f"盈利因子: {profit_factor:.2f}")
                print("\n自适应权重统计:")
                print(f"平均历史权重: {avg_hist_weight:.3f}")
                print(f"平均当前权重: {avg_curr_weight:.3f}")
                print(f"\n双周期过滤统计:")
                print(f"总信号数: {self.total_signals}")
                print(f"过滤信号数: {self.filtered_signals}")
                print(f"假信号次数（2bar内反转）: {self.false_signals}")
                print(f"真信号次数（穿越后持续）: {self.true_signals}")
                print("=" * 80)

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(DualTimeframeStrategy)
    cerebro.run()

    strat = cerebro.runstrats[0][0]
    latest_data = df.iloc[-1]
    atr_value = float(df['ATR_ADX'].iloc[-1]) if 'ATR_ADX' in df.columns and len(df) > 0 else float('nan')

    return latest_data, strat, atr_value, long_direction, df, df_long_raw


def run_with_futures(
    symbol='LC',
    backtest_days=200,
    short_bar='1H',
    long_bar='1D',
    show_original_output=1,
    show_market_output=0,
    show_trade_ops_output=0,
    show_trade_records_output=0,
    entry_price_type='close',
    exit_price_type='open',
):
    """运行期货双周期策略回测"""
    global PRINT_MARKET, PRINT_TRADE_OPS, PRINT_TRADE_RECORDS, PRINT_ORIGINAL_OUTPUT
    global ENTRY_PRICE_TYPE, EXIT_PRICE_TYPE

    PRINT_MARKET = 1 if show_market_output else 0
    PRINT_TRADE_OPS = 1 if show_trade_ops_output else 0
    PRINT_TRADE_RECORDS = 1 if show_trade_records_output else 0
    PRINT_ORIGINAL_OUTPUT = 1 if show_original_output else 0
    ENTRY_PRICE_TYPE = entry_price_type
    EXIT_PRICE_TYPE = exit_price_type

    latest_data, strat, atr, long_direction, df, df_long = get_latest_data(
        symbol, short_bar, long_bar, backtest_days=backtest_days)

    if PRINT_ORIGINAL_OUTPUT:
        print(f"\n=== 方案 Version_Close_Open 期货版: ADX自适应平滑系统 + 双周期共振 ===")
        print(f"当前长周期方向: {long_direction}")
        print("最新行情时间：", latest_data.name)
        print("ATR值:", atr)

    return strat


# ====================================================================
#  向后兼容：保留原 run_with_csv 签名
# ====================================================================
def run_with_csv(
    symbol='LC',
    calc_full_flag='Y',
    short_bar='1H',
    long_bar='1D',
    show_original_output=1,
    show_market_output=0,
    show_trade_ops_output=0,
    show_trade_records_output=0,
    entry_price_type='close',
    exit_price_type='open',
):
    """兼容原接口，内部调用 run_with_futures"""
    return run_with_futures(
        symbol=symbol,
        backtest_days=90 if str(calc_full_flag).upper() == 'N' else 200,
        short_bar=short_bar,
        long_bar=long_bar,
        show_original_output=show_original_output,
        show_market_output=show_market_output,
        show_trade_ops_output=show_trade_ops_output,
        show_trade_records_output=show_trade_records_output,
        entry_price_type=entry_price_type,
        exit_price_type=exit_price_type,
    )


def main():
    """主函数 - 期货版示例"""
    run_with_futures(
        symbol='LC',
        backtest_days=200,
        short_bar='1H',
        long_bar='1D',
        show_original_output=1,
        entry_price_type='close',
        exit_price_type='open',
    )


if __name__ == "__main__":
    main()
