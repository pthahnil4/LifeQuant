# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/futuresStrategy/pro3_singletimeframe.py  →  现位置: crypto/market_strategy/futures_strategy/pro3_singletimeframe.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
# type: ignore
# -*- coding: utf-8 -*-
"""
期货版 单周期/双周期 Pro3 策略（复制 cryptoStrategy/pro3_singletimeframe.py 核心逻辑）
====================================================================================

与 crypto 版的唯一差异：数据获取层
- crypto 版：OKX API get_mark_price_candlesticks
- 期货版：akshare 期货数据源（crypto.futures.akshare_api）
  * 分钟/小时级（1m/3m/5m/15m/30m/1H/2H/4H/6H/12H）：主力合约分钟K线（必要时 resample）
  * 日线（1D）：主力连续合约日线
  * 周线（1W）：日线 resample('W') 聚合

核心机制（与 crypto 版完全一致）：
- 短周期生成交易信号；长周期确认趋势方向（双周期模式）
- 开仓：短周期信号与长周期方向一致
- 平仓：短周期信号与长周期方向不一致
- 单周期模式：直接使用短周期的 hist_smooth_diff 正负判断趋势方向

信号计算：
- MACD → histogram = 2 * (DIF - DEA)
- ADX 自适应平滑 → smoothed_histogram
- hist_smooth_diff = histogram - smoothed_histogram
- modify_flag: hist_smooth_diff > 0 → rise, < 0 → fall

输出控制：
- PRINT_MARKET: 行情输出
- PRINT_TRADE_OPS: 交易操作与决策输出
- PRINT_TRADE_RECORDS: 交易记录输出
- PRINT_ORIGINAL_OUTPUT: 策略统计输出
"""
import datetime
import math
import re
import sys
import os
import time
import pandas as pd
import numpy as np
import backtrader as bt


from crypto.futures.akshare_api import AkshareFuturesAPI, AkshareAPIError

# 全局变量
CURRENT_INSTID = ""
CURRENT_BAR = ""
CURRENT_SHORT_BAR = ""
CURRENT_LONG_BAR = ""

PRINT_MARKET = 0
PRINT_TRADE_OPS = 0
PRINT_TRADE_RECORDS = 0
PRINT_ORIGINAL_OUTPUT = 1
FAST_MODE = False

ENTRY_PRICE_TYPE = 'close'   # 开仓价格取值：'open' / 'close' / 'zero'
EXIT_PRICE_TYPE = 'open'     # 平仓价格取值：'open' / 'close'


def calculate_adx(df, period=14):
    """计算ADX指标（使用period=14，与version_close_open一致）"""
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
    # ADXR = (ADX + N期前的ADX) / 2
    df['ADXR'] = (df['ADX'] + df['ADX'].shift(period)) / 2

    return df


def get_adaptive_smooth_weight(adx_value, base_weight=0.7):
    """根据ADX计算自适应平滑权重"""
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    curr_weight = 1.0 - hist_weight
    return hist_weight, curr_weight


def _normalize_dir(v):
    """标准化方向字符串"""
    if v is None:
        return None
    s = str(v)
    if s == 'rise' or s == '多':
        return 'rise'
    if s == 'fall' or s == '空':
        return 'fall'
    return None


# ========== akshare 期货数据获取（替代 OKX API） ==========

# 逻辑周期 → (akshare 分钟period, resample倍数)，与 futures_adapter 保持一致
_MINUTE_PERIOD_MAP = {
    '1m':  ('1', 1),
    '3m':  ('1', 3),      # 1m resample → 3m
    '5m':  ('5', 1),
    '15m': ('15', 1),
    '30m': ('30', 1),
    '60m': ('60', 1),
    '1H':  ('60', 1),
    '2H':  ('60', 2),     # 1H resample → 2H
    '4H':  ('60', 4),     # 1H resample → 4H
    '6H':  ('60', 6),     # 1H resample → 6H
    '12H': ('60', 12),    # 1H resample → 12H
}
DAILY_BARS = {'1D', 'D', 'day', 'daily'}
WEEKLY_BARS = {'1W', 'W', 'week', 'weekly'}

# 日线回看天数：3年日线，可支撑约150根周线重采样
_DAILY_FETCH_DAYS = 365 * 4

_KLINE_MAX_RETRIES = 3

# 主力合约缓存：{code: (symbol, cache_time)}，5分钟有效
_main_contract_cache = {}
_MAIN_CONTRACT_CACHE_TTL = 300


def _resolve_main_contract(code):
    """解析品种字母代码对应的当前主力合约符号（按持仓量排序，缓存5分钟）"""
    cache_key = code.upper()
    now = time.time()
    cached = _main_contract_cache.get(cache_key)
    if cached is not None and (now - cached[1]) < _MAIN_CONTRACT_CACHE_TTL:
        return cached[0]

    df_rt = AkshareFuturesAPI.fetch_realtime(code)
    if df_rt is None or df_rt.empty:
        raise RuntimeError(f"实时行情返回空，无法解析主力合约: {code}")

    df_sorted = df_rt.copy()
    if 'position' in df_sorted.columns:
        df_sorted['position'] = pd.to_numeric(df_sorted['position'], errors='coerce')
        df_sorted = df_sorted.sort_values('position', ascending=False)
    elif 'volume' in df_sorted.columns:
        df_sorted['volume'] = pd.to_numeric(df_sorted['volume'], errors='coerce')
        df_sorted = df_sorted.sort_values('volume', ascending=False)

    main_sym = str(df_sorted.iloc[0].get('symbol', '')).strip().upper()
    if not main_sym:
        raise RuntimeError(f"无法解析主力合约: {code}")
    _main_contract_cache[cache_key] = (main_sym, now)
    return main_sym


def _extract_timestamp_column(df):
    """从 DataFrame 中找到时间列并转换为 'timestamp' 列"""
    ts_col = None
    for cand in ('datetime', 'date', '日期', '时间'):
        if cand in df.columns:
            ts_col = cand
            break
    if ts_col is None:
        raise ValueError(f"未找到时间列: {list(df.columns)}")
    df = df.copy()
    df['timestamp'] = pd.to_datetime(df[ts_col])
    return df


def _ensure_ohlc(df):
    """兼容中文列名 → 英文 OHLC 列"""
    if {'open', 'high', 'low', 'close'}.issubset(df.columns):
        return df
    rename_map = {}
    for cn, en in (('开盘', 'open'), ('最高', 'high'), ('最低', 'low'), ('收盘', 'close'),
                   ('开盘价', 'open'), ('最高价', 'high'), ('最低价', 'low'), ('收盘价', 'close')):
        if cn in df.columns:
            rename_map[cn] = en
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def _fetch_daily_df(symbol, max_retries=_KLINE_MAX_RETRIES):
    """获取日线数据（带重试与主力/具体合约回退）

    - 具体合约（如 RB2610）：fetch_daily_contract，失败回退字母代码主力连续
    - 字母代码（如 RB）：fetch_daily_main
    """
    end_date = datetime.date.today().strftime('%Y%m%d')
    start_date = (datetime.date.today() - datetime.timedelta(days=_DAILY_FETCH_DAYS)).strftime('%Y%m%d')
    letter_code = ''.join([ch for ch in symbol if ch.isalpha()]).upper() or symbol

    is_contract = bool(re.search(r'\d$', symbol))
    attempts = []
    if is_contract:
        attempts.append(('contract', symbol))
    attempts.append(('main', letter_code))

    last_err = None
    for kind, sym in attempts:
        for attempt in range(1, max_retries + 1):
            try:
                if kind == 'contract':
                    df = AkshareFuturesAPI.fetch_daily_contract(sym)
                else:
                    df = AkshareFuturesAPI.fetch_daily_main(sym, start_date, end_date)
                if df is not None and not df.empty:
                    return df
                break  # 空数据换下一种方式
            except (AkshareAPIError, Exception) as e:
                last_err = e
                print(f"[FuturesPro3][K线] {sym} 日线获取失败(第{attempt}/{max_retries}次) "
                      f"{type(e).__name__}: {e!r}")
                if attempt < max_retries:
                    time.sleep(min(1.0 * attempt, 3))
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"日线数据为空: {symbol}")


def _fetch_minute_df(symbol, bar, max_retries=_KLINE_MAX_RETRIES):
    """获取分钟K线数据（自动解析主力合约，必要时 resample）"""
    period_info = _MINUTE_PERIOD_MAP.get(bar)
    if period_info is None:
        raise ValueError(f"不支持的bar周期: {bar}")
    ak_period, resample_mult = period_info

    # 字母代码需先解析主力合约；具体合约代码直接使用
    contract = symbol if re.search(r'\d$', symbol) else _resolve_main_contract(symbol)

    last_err = None
    df = None
    for attempt in range(1, max_retries + 1):
        try:
            df = AkshareFuturesAPI.fetch_minute_kline(contract, period=ak_period)
            break
        except (AkshareAPIError, Exception) as e:
            last_err = e
            print(f"[FuturesPro3][K线] {contract} {bar} 分钟K线获取失败(第{attempt}/{max_retries}次) "
                  f"{type(e).__name__}: {e!r}")
            if attempt < max_retries:
                time.sleep(min(1.0 * attempt, 3))
    if df is None or df.empty:
        if last_err is not None:
            raise last_err
        raise RuntimeError(f"分钟数据为空: {contract} {ak_period}")

    df = _extract_timestamp_column(df)
    df = _ensure_ohlc(df)

    # 需要 resample 的周期（如 3m=1m×3, 2H=1H×2, 4H=1H×4）
    if resample_mult > 1:
        rule_map = {2: '2h', 3: '3min', 4: '4h', 6: '6h', 12: '12h'}
        rule = rule_map.get(resample_mult)
        if rule:
            df = (
                df[['timestamp', 'open', 'high', 'low', 'close']]
                .dropna()
                .sort_values('timestamp')
                .set_index('timestamp')
                .resample(rule)
                .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                .dropna()
                .reset_index()
            )
    return df


def _fetch_kline_data(instId, bar, lookback_days=None):
    """从 akshare 获取期货K线数据（接口与 crypto 版保持一致）。

    参数:
        instId: 品种字母代码（'RB'）或具体合约代码（'RB2610'）
        bar: K线周期，如 '15m'/'1H'/'4H'/'1D'/'1W'
        lookback_days: 可选，仅保留最近N天数据

    返回:
        DataFrame（DatetimeIndex 升序，float 类型 open/high/low/close 列），
        与 crypto 版 _fetch_kline_data 的返回结构完全一致。
    """
    bar = str(bar)
    symbol = str(instId).strip().upper()

    if bar in DAILY_BARS or bar in WEEKLY_BARS:
        df = _fetch_daily_df(symbol)
        df = _extract_timestamp_column(df)
        df = _ensure_ohlc(df)
        if bar in WEEKLY_BARS:
            # 周线：日线 resample('W')，open首值/high最大/low最小/close末值
            df = (
                df[['timestamp', 'open', 'high', 'low', 'close']]
                .dropna()
                .sort_values('timestamp')
                .set_index('timestamp')
                .resample('W')
                .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                .dropna()
                .reset_index()
            )
    else:
        df = _fetch_minute_df(symbol, bar)

    for col in ['open', 'high', 'low', 'close']:
        if col not in df.columns:
            raise ValueError(f"数据缺少列: {col}")
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df[['timestamp', 'open', 'high', 'low', 'close']].dropna()
    df = df.sort_values('timestamp').set_index('timestamp')
    df = df.astype(float)

    if lookback_days is not None:
        try:
            cutoff = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=int(lookback_days)))
            df = df.loc[df.index >= cutoff]
        except Exception:
            pass

    if df.empty:
        raise RuntimeError(f"K线数据为空: instId={instId}, bar={bar}")
    return df


# ========== 长周期方向计算（用于双周期模式） ==========

def _calculate_long_direction_series(df_long):
    """
    计算长周期方向序列，将 LONG_DIRECTION 列写入 df_long。
    核心逻辑：使用 MACD 平滑差 (hist_smooth_diff) 的正负来判断方向。
    """
    class LongDirStrategy(bt.Strategy):  # type: ignore
        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)  # type: ignore
            self.smoothed_hist = []
            self.modify_flag = None

        def next(self):
            current_idx = len(self) - 1
            adx_value = df_long.iloc[current_idx]['ADX'] if current_idx < len(df_long) else 0

            macd_value = self.macd.macd[0]
            signal_value = self.macd.signal[0]
            histogram = 2 * (macd_value - signal_value)

            hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)

            if len(self.smoothed_hist) == 0:
                smoothed_histogram = histogram
            else:
                smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
            self.smoothed_hist.append(smoothed_histogram)

            hist_smooth_diff = histogram - smoothed_histogram

            # 方向判断：hist_smooth_diff 正负
            if hist_smooth_diff > 0:
                self.modify_flag = "rise"
            elif hist_smooth_diff < 0:
                self.modify_flag = "fall"

            df_long.loc[self.data.datetime.datetime(0), 'LONG_DIRECTION'] = self.modify_flag

    cerebro = bt.Cerebro()  # type: ignore
    data_feed = bt.feeds.PandasData(dataname=df_long)  # type: ignore
    cerebro.adddata(data_feed)  # type: ignore
    cerebro.addstrategy(LongDirStrategy)  # type: ignore
    cerebro.run()  # type: ignore


def _align_long_direction_to_short(df_short, df_long):
    """
    将长周期方向对齐到短周期时间戳。
    使用 merge_asof 向后对齐（每个短周期K线取最近的长周期方向）。
    """
    if 'LONG_DIRECTION' not in df_long.columns:
        df_short['LONG_DIRECTION'] = np.nan
        return

    try:
        # 确保索引无时区
        if hasattr(df_short.index, 'tz') and df_short.index.tz is not None:
            df_short.index = df_short.index.tz_localize(None)
        if hasattr(df_long.index, 'tz') and df_long.index.tz is not None:
            df_long.index = df_long.index.tz_localize(None)

        short_times = pd.DataFrame({'ts': pd.to_datetime(df_short.index)})
        long_times = pd.DataFrame({'ts': pd.to_datetime(df_long.index),
                                   'LONG_DIRECTION': df_long['LONG_DIRECTION'].values})
        short_times = short_times.sort_values('ts')
        long_times = long_times.sort_values('ts')
        aligned = pd.merge_asof(short_times, long_times, on='ts', direction='backward')
        df_short['LONG_DIRECTION'] = aligned['LONG_DIRECTION'].values
    except Exception:
        df_short['LONG_DIRECTION'] = df_long['LONG_DIRECTION'].reindex(df_short.index, method='ffill')

    # 过滤掉没有长周期方向的K线
    first_valid = df_short['LONG_DIRECTION'].first_valid_index()
    if first_valid is not None:
        # 不截断 df_short，只标记为 NaN（截断由调用方决定）
        pass


# ========== 核心策略类（供 backtrader 运行） ==========

def _run_strategy(df, long_direction=None, is_dual_period=False):
    """
    运行核心策略。

    参数:
        df: 短周期 DataFrame（含 ADX 列）
        long_direction: 长周期最新方向（双周期模式使用）
        is_dual_period: 是否启用双周期过滤

    返回:
        (strat, cerebro): backtrader 策略实例
    """

    class CoreStrategy(bt.Strategy):  # type: ignore
        """核心策略：MACD平滑+ADX自适应，支持单/双周期"""

        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)  # type: ignore
            self.ema12 = bt.indicators.EMA(self.data.close, period=12)  # type: ignore
            self.ema26 = bt.indicators.EMA(self.data.close, period=26)  # type: ignore

            self.smoothed_hist = []
            self.weight_history = []
            self.last_trade_index = None
            self.previous_diff = None
            self.original_flag = None
            self.modify_flag = None

            self.k = 0
            self.times = 10.0

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

            # 双周期相关
            self.long_direction = long_direction
            self.current_position = None   # 当前持仓方向 'rise'/'fall'/None
            self.entry_price = None
            self.filtered_signals = 0
            self.total_signals = 0
            self.zero_entries = 0

            # 假信号统计
            self.false_signals = 0
            self.true_signals = 0
            self.total_crossings = 0
            self._pending_signal_bar = None
            self._pending_signal_dir = None
            self._pending_signal_diff = None

            self._prev_entry_was_zero = False

        def next(self):
            current_idx = len(self) - 1
            adx_value = df.iloc[current_idx]['ADX'] if current_idx < len(df) else 0

            # === 计算 MACD ===
            macd_value = self.macd.macd[0]
            signal_value = self.macd.signal[0]
            histogram = 2 * (macd_value - signal_value)

            # === ADX 自适应平滑 ===
            hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)
            self.weight_history.append((hist_weight, curr_weight))

            if len(self.smoothed_hist) == 0:
                smoothed_histogram = histogram
            else:
                smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
            self.smoothed_hist.append(smoothed_histogram)

            hist_smooth_diff = histogram - smoothed_histogram

            # === 零点价格计算 ===
            histogram_zero = 0.0
            hist_smooth_diff_zero = 0.0
            interpolated_zero = 0.0
            if len(self.data) > 1:
                prev_dea = self.macd.signal[-1]
                prev_ema12 = self.ema12[-1]
                prev_ema26 = self.ema26[-1]
                prev_smoothed = self.smoothed_hist[-2] if len(self.smoothed_hist) > 1 else 0

                # histogram = 0 时的价格
                histogram_zero = (prev_dea - (prev_ema12 * 11.0 / 13.0) + (prev_ema26 * 25.0 / 27.0)) / (2.0 / 13.0 - 2.0 / 27.0)

                # hist_smooth_diff = 0 时的价格
                if curr_weight != 0:
                    hist_smooth_diff_zero = (prev_dea - (prev_ema12 * 11.0 / 13.0) + (prev_ema26 * 25.0 / 27.0) + (prev_smoothed * hist_weight / curr_weight)) / (2.0 / 13.0 - 2.0 / 27.0)
                else:
                    hist_smooth_diff_zero = histogram_zero

                # 线性插值法估算 hist_smooth_diff 穿越零点时的价格
                prev_diff_val = self.previous_diff if self.previous_diff is not None else 0.0
                curr_diff_val = hist_smooth_diff
                if prev_diff_val != curr_diff_val:
                    ratio = abs(prev_diff_val) / abs(curr_diff_val - prev_diff_val)
                    ratio = max(0.0, min(1.0, ratio))
                    prev_close = float(self.data.close[-1])
                    curr_close = float(self.data.close[0])
                    interpolated_zero = prev_close + ratio * (curr_close - prev_close)

                df.loc[self.data.datetime.datetime(0), 'HISTOGRAM_ZERO'] = histogram_zero
                df.loc[self.data.datetime.datetime(0), 'HIST_SMOOTH_DIFF_ZERO'] = hist_smooth_diff_zero
                df.loc[self.data.datetime.datetime(0), 'INTERPOLATED_ZERO'] = interpolated_zero

                # === 假信号检测 ===
                if self.previous_diff is not None and self.previous_diff * hist_smooth_diff < 0:
                    self.total_crossings += 1
                    self._pending_signal_bar = len(self.data)
                    self._pending_signal_dir = 'rise' if hist_smooth_diff > 0 else 'fall'
                    self._pending_signal_diff = hist_smooth_diff
                elif self._pending_signal_bar is not None:
                    bars_since = len(self.data) - self._pending_signal_bar
                    if bars_since >= 2:
                        if self._pending_signal_diff * hist_smooth_diff < 0:
                            self.false_signals += 1
                        else:
                            self.true_signals += 1
                        self._pending_signal_bar = None
                        self._pending_signal_dir = None
                        self._pending_signal_diff = None

            # === 原始信号判断 ===
            if histogram > smoothed_histogram and self.previous_diff is not None and self.previous_diff < 0:
                self.last_trade_index = len(self) - 1
                self.original_flag = 'rise'
            elif histogram < smoothed_histogram and self.previous_diff is not None and self.previous_diff > 0:
                self.last_trade_index = len(self) - 1
                self.original_flag = 'fall'

            df.loc[self.data.datetime.datetime(0), 'ORIGINAL_FLAG'] = self.original_flag
            self.previous_diff = hist_smooth_diff

            # 写入 DataFrame
            df.loc[self.data.datetime.datetime(0), 'DIF'] = macd_value
            df.loc[self.data.datetime.datetime(0), 'DEA'] = signal_value
            df.loc[self.data.datetime.datetime(0), 'MACD'] = histogram
            df.loc[self.data.datetime.datetime(0), 'SMOOTHED_MACD'] = smoothed_histogram
            df.loc[self.data.datetime.datetime(0), 'HIST_SMOOTH_DIFF'] = hist_smooth_diff
            df.loc[self.data.datetime.datetime(0), 'HIST_WEIGHT'] = hist_weight
            df.loc[self.data.datetime.datetime(0), 'CURR_WEIGHT'] = curr_weight

            # === modify_flag 计算（核心：直接用 hist_smooth_diff 正负判定） ===
            if hist_smooth_diff > 0:
                self.modify_flag = "rise"
            elif hist_smooth_diff < 0:
                self.modify_flag = "fall"

            df.loc[self.data.datetime.datetime(0), 'MODIFY_FLAG'] = self.modify_flag

            # === 交易逻辑 ===
            if is_dual_period:
                self._dual_period_trade_logic(
                    adx_value, hist_weight, curr_weight,
                    hist_smooth_diff, interpolated_zero
                )
            else:
                self._single_period_trade_logic(
                    adx_value, hist_weight, curr_weight,
                    hist_smooth_diff, interpolated_zero
                )

            self._prev_entry_was_zero = False

            # === 行情输出 ===
            if PRINT_MARKET:
                o = float(self.data.open[0])
                h = float(self.data.high[0])
                l = float(self.data.low[0])
                c = float(self.data.close[0])
                ts = self.data.datetime.datetime(0)
                print(f"{len(self)}. Modify: {self.modify_flag} ADX: {adx_value:.2f} "
                      f"Weight: {hist_weight:.3f}/{curr_weight:.3f} "
                      f"OHLC:{o:.3f}/{h:.3f}/{l:.3f}/{c:.3f} "
                      f"macd:{float(histogram):.3f} dif:{float(macd_value):.3f} "
                      f"macd平滑差:{float(hist_smooth_diff):.3f} "
                      f"临界值(macd=0):{float(histogram_zero):.3f} "
                      f"临界值(平滑差=0):{float(hist_smooth_diff_zero):.3f} {ts}")

        # ============================
        # 单周期交易逻辑
        # ============================
        def _single_period_trade_logic(self, adx_value, hist_weight, curr_weight,
                                       hist_smooth_diff, interpolated_zero):
            signal_changed = self.modify_flag != getattr(self, 'last_modify_flag', None)

            # 有持仓 + 方向反转 → 平仓
            if self.current_position is not None and self.modify_flag != self.current_position and signal_changed:
                trade_price = self.data.close[0] if EXIT_PRICE_TYPE == 'close' else self.data.open[0]

                if self.current_position == "rise":
                    profit = ((trade_price / self.trades[-1]) - 1) * 100 * self.times
                else:
                    profit = (1 - (trade_price / self.trades[-1])) * 100 * self.times

                self._update_capital(profit)
                self._record_close(trade_price, profit)

                if PRINT_TRADE_OPS:
                    direction_name = ('平多' if self.current_position == 'rise' else '平空')
                    print(f"{direction_name}(反转) 第{self.k}次 价格{trade_price:.3f} "
                          f"持仓方向:{self.current_position} 信号:{self.modify_flag} "
                          f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                          f"混合:{self.mix_money:.0f} 盈亏:{profit:.1f}% "
                          f"{self.data.datetime.datetime(0)}")

                self.current_position = None
                self.entry_price = None

            # 无持仓 + 信号改变 → 开仓
            if self.current_position is None and signal_changed and self.modify_flag is not None:
                self._open_position(adx_value, hist_weight, curr_weight, interpolated_zero)

            # 更新持仓盈亏
            self._update_position_pnl()
            self.last_modify_flag = self.modify_flag

        # ============================
        # 双周期交易逻辑
        # ============================
        def _dual_period_trade_logic(self, adx_value, hist_weight, curr_weight,
                                     hist_smooth_diff, interpolated_zero):
            # 获取当前bar对应的长周期方向
            raw_long_dir = df.loc[self.data.datetime.datetime(0), 'LONG_DIRECTION'] if 'LONG_DIRECTION' in df.columns else None
            if raw_long_dir is None:
                return
            bar_long_dir = _normalize_dir(raw_long_dir)
            if bar_long_dir is None:
                return

            directions_match = (self.modify_flag == bar_long_dir)
            signal_changed = self.modify_flag != getattr(self, 'last_modify_flag', None)

            # 情况1：有持仓 + 双周期不一致 → 平仓
            if self.current_position is not None and (not directions_match):
                trade_price = self.data.close[0] if EXIT_PRICE_TYPE == 'close' else self.data.open[0]

                if self.current_position == "rise":
                    profit = ((trade_price / self.trades[-1]) - 1) * 100 * self.times
                else:
                    profit = (1 - (trade_price / self.trades[-1])) * 100 * self.times

                self._update_capital(profit)
                self._record_close(trade_price, profit)

                if PRINT_TRADE_OPS:
                    reason = '方向不一致'
                    direction_name = ('平多' if self.current_position == 'rise' else '平空')
                    print(f"{direction_name}({reason}) 第{self.k}次 价格{trade_price:.3f} "
                          f"持仓方向:{self.current_position} 短周期:{self.modify_flag} 长周期:{bar_long_dir} "
                          f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                          f"混合:{self.mix_money:.0f} 盈亏:{profit:.1f}% "
                          f"{self.data.datetime.datetime(0)}")

                self.current_position = None
                self.entry_price = None
                self.filtered_signals += 1

            # 情况2：无持仓 + 双周期一致 + 信号改变 → 开仓
            elif self.current_position is None and directions_match and signal_changed:
                self._open_position(adx_value, hist_weight, curr_weight, interpolated_zero,
                                    bar_long_dir=bar_long_dir)

            # 情况3：方向不一致但无持仓 → 过滤信号
            elif self.current_position is None and not directions_match and signal_changed:
                self.filtered_signals += 1
                if PRINT_TRADE_OPS:
                    print(f"过滤信号: 短周期{self.modify_flag}但长周期{bar_long_dir} (无持仓) "
                          f"{self.data.datetime.datetime(0)}")

            # 更新持仓盈亏
            self._update_position_pnl()
            self.last_modify_flag = self.modify_flag

        # ============================
        # 公共交易方法
        # ============================
        def _open_position(self, adx_value, hist_weight, curr_weight, interpolated_zero,
                           bar_long_dir=None):
            """开仓"""
            self.k += 1
            self.total_signals += 1

            # 计算开仓价格
            _zero_price = interpolated_zero
            _bar_high = float(self.data.high[0])
            _bar_low = float(self.data.low[0])
            _used_zero = False

            if ENTRY_PRICE_TYPE == 'zero' and _zero_price > 0 and _bar_low <= _zero_price <= _bar_high:
                trade_price = _zero_price
                _used_zero = True
                self.zero_entries += 1
            elif ENTRY_PRICE_TYPE == 'close':
                trade_price = self.data.close[0]
            else:
                trade_price = self.data.open[0]

            df.loc[self.data.datetime.datetime(0), 'TRADE_PRICE'] = trade_price
            self._prev_entry_was_zero = _used_zero

            self.trades.append(trade_price)
            self.trade_times.append(self.data.datetime.datetime(0))
            self.trade_dirs.append(self.modify_flag)

            self.max_wins.append(0.0)
            self.max_losses.append(0.0)

            self.current_position = self.modify_flag
            self.entry_price = trade_price

            direction_name = "开多" if self.modify_flag == "rise" else "开空"
            if PRINT_TRADE_OPS:
                long_info = f" 长周期:{bar_long_dir}" if bar_long_dir else ""
                print(f"{direction_name} 第{self.k}次 价格{trade_price:.3f} "
                      f"ADX={adx_value:.1f} 权重({hist_weight:.3f}/{curr_weight:.3f}) "
                      f"信号:{self.modify_flag}{long_info} "
                      f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                      f"混合:{self.mix_money:.0f} "
                      f"{self.data.datetime.datetime(0)}")

        def _update_capital(self, profit):
            """更新三种投资方式的资金"""
            self.fix_money += self.base_money * profit * 0.01
            self.contract_money += self.contract_money * profit * 0.01

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
            self.mix_money += base * profit * 0.01

        def _record_close(self, trade_price, profit):
            """记录平仓数据"""
            self.profits.append(profit)
            self.trades.append(trade_price)
            self.trade_times.append(self.data.datetime.datetime(0))

        def _update_position_pnl(self):
            """更新持仓盈亏跟踪"""
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

                if PRINT_MARKET:
                    pos_label = ('多头' if self.current_position == 'rise' else '空头')
                    ep = float(self.entry_price) if self.entry_price is not None else float(self.trades[-1])
                    print(f"持仓状态: {pos_label} | 开仓价: {ep:.3f} | 当前盈亏: {current_profit:+.2f}%")

        def stop(self):
            """策略结束统计"""
            if globals().get('FAST_MODE', False):
                return

            # === 期末强制平仓（若仍有持仓） ===
            try:
                if self.current_position is not None and len(self.trades) > 0:
                    last_trade_price = self.trades[-1]
                    last_close = float(self.data.close[0])
                    if self.current_position == 'rise':
                        profit = ((last_close / last_trade_price) - 1) * 100 * self.times
                    elif self.current_position == 'fall':
                        profit = (1 - (last_close / last_trade_price)) * 100 * self.times
                    else:
                        profit = 0.0
                    # 资金更新
                    self._update_capital(profit)
                    self.profits.append(profit)
                    self.trades.append(last_close)
                    self.trade_times.append(self.data.datetime.datetime(0))
            except Exception:
                pass

            # 处理未确认的pending信号
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

            # 最大回撤 - 复投资金曲线
            max_drawdown = 0
            if len(self.profits) > 0:
                capital_curve = [self.base_money]
                current_capital = self.base_money
                for profit in self.profits:
                    current_capital = current_capital * (1 + profit * 0.01)
                    capital_curve.append(current_capital)
                peak = capital_curve[0]
                for value in capital_curve:
                    if value > peak:
                        peak = value
                    drawdown = (peak - value) / peak * 100
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown

            # 权重统计
            avg_hist_weight = np.mean([w[0] for w in self.weight_history]) if self.weight_history else 0
            avg_curr_weight = np.mean([w[1] for w in self.weight_history]) if self.weight_history else 0

            if PRINT_ORIGINAL_OUTPUT:
                mode_label = "双周期" if is_dual_period else "单周期"
                print(f"\nMACD平滑+ADX自适应 + {mode_label} - 交易统计")
                print("=" * 80)
                print(f"交易对: {CURRENT_INSTID}")
                if is_dual_period:
                    print(f"短周期: {CURRENT_SHORT_BAR}")
                    print(f"长周期: {CURRENT_LONG_BAR}")
                else:
                    print(f"时间周期: {CURRENT_BAR}")
                print(f"\n总交易次数: {total_trades}")
                print(f"胜率: {win_rate:.2%}")
                print(f"盈利次数: {winning_trades}")
                print(f"亏损次数: {losing_trades}")
                print(f"复投最终收益: {self.contract_money:.2f}")
                print(f"定投最终收益: {self.fix_money:.2f}")
                print(f"混合最终收益: {self.mix_money:.2f}")
                print(f"\n最终盈利总和: {total_profit:.2f}%")
                print(f"最终亏损总和: {total_loss:.2f}%")
                print(f"最终盈利均值（仅盈利单）: {avg_profit:.2f}%")
                print(f"最终亏损均值（仅亏损单）: {avg_loss:.2f}%")
                final_mean_pl = (abs(avg_profit) / abs(avg_loss) if avg_loss != 0 else 0.0)
                final_sum_pl = (abs(total_profit) / abs(total_loss) if total_loss != 0 else 0.0)
                print(f"最终盈亏均值比: {final_mean_pl:.2f}")
                print(f"最终盈亏总和比: {final_sum_pl:.2f}")

                # 持仓盈亏
                win_indices = [i for i, p in enumerate(self.profits) if p > 0]
                loss_indices = [i for i, p in enumerate(self.profits) if p < 0]
                hold_total_max_profit = sum(self.max_wins[i] for i in win_indices if i < len(self.max_wins)) if win_indices else 0.0
                hold_total_max_loss = sum(self.max_losses[i] for i in loss_indices if i < len(self.max_losses)) if loss_indices else 0.0
                pos_count = len(win_indices)
                neg_count = len(loss_indices)
                avg_hold_max_profit = (hold_total_max_profit / pos_count) if pos_count > 0 else 0
                avg_hold_max_loss = (hold_total_max_loss / neg_count) if neg_count > 0 else 0
                print(f"\n持仓最大盈利总和: {hold_total_max_profit:.2f}%")
                print(f"持仓最大亏损总和: {hold_total_max_loss:.2f}%")
                print(f"持仓最大盈利均值: {avg_hold_max_profit:.2f}%")
                print(f"持仓最大亏损均值: {avg_hold_max_loss:.2f}%")

                actual_profit_rate = total_profit / hold_total_max_profit if hold_total_max_profit > 0 else 0
                actual_loss_rate = abs(total_loss) / abs(hold_total_max_loss) if hold_total_max_loss < 0 else 0
                print(f"\n盈利到手率: {actual_profit_rate:.2%}")
                print(f"亏损到手率: {actual_loss_rate:.2%}")

                # 风险指标
                annual_return = ((self.contract_money / self.base_money) ** (252 / len(self.profits)) - 1) * 100 if len(self.profits) > 0 else 0
                calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0
                profit_factor = abs(total_profit / total_loss) if total_loss < 0 else 0
                print(f"\n复投最大回撤率: {max_drawdown:.2f}%")
                print(f"卡玛比率: {calmar_ratio:.2f}")
                print(f"盈利因子: {profit_factor:.2f}")

                print(f"\n自适应权重统计:")
                print(f"平均历史权重: {avg_hist_weight:.3f} (原策略: 0.800)")
                print(f"平均当前权重: {avg_curr_weight:.3f} (原策略: 0.200)")

                if is_dual_period:
                    print(f"\n双周期过滤统计:")
                    print(f"总信号数: {self.total_signals}")
                    print(f"过滤信号数: {self.filtered_signals}")
                    print(f"信号通过率: {((self.total_signals - self.filtered_signals) / self.total_signals * 100 if self.total_signals > 0 else 0):.1f}%")

                if self.zero_entries > 0:
                    print(f"\nZero入场统计:")
                    print(f"Zero入场次数: {self.zero_entries}")

                print(f"\n假信号统计（hist_smooth_diff穿越零点+2bar确认）:")
                _total_confirmed = self.true_signals + self.false_signals
                print(f"穿越零总次数: {self.total_crossings}")
                print(f"已确认信号数: {_total_confirmed}")
                print(f"真信号次数: {self.true_signals}")
                print(f"假信号次数: {self.false_signals}")
                if _total_confirmed > 0:
                    print(f"假信号占比: {self.false_signals / _total_confirmed * 100:.1f}%")
                print("=" * 80)

    # 运行策略
    cerebro = bt.Cerebro()  # type: ignore
    data_feed = bt.feeds.PandasData(dataname=df)  # type: ignore
    cerebro.adddata(data_feed)  # type: ignore
    cerebro.addstrategy(CoreStrategy)  # type: ignore
    cerebro.run()  # type: ignore

    strat = cerebro.runstrats[0][0]
    return strat


# ========== 对外接口 ==========

def get_latest_data(instId, bar):
    """
    单周期接口（向后兼容）。
    使用短周期的 MACD 平滑差作为趋势指标进行计算。

    返回:
        (latest_data, last_trade_data, atr_value)
    """
    global CURRENT_INSTID, CURRENT_BAR

    CURRENT_INSTID = instId
    CURRENT_BAR = bar

    # 获取K线数据
    df = _fetch_kline_data(instId, bar)
    df = calculate_adx(df, period=14)

    # 初始化 DataFrame 列
    _init_df_columns(df)

    # 运行策略（单周期模式）
    strat = _run_strategy(df, long_direction=None, is_dual_period=False)

    # 构建返回值
    latest_data = df.iloc[-1].copy()
    _attach_prev_adx(df, latest_data)

    last_trade_data = _build_last_trade_data(strat, df)

    # 计算 ATR
    atr_value = _calculate_atr(df)

    return latest_data, last_trade_data, atr_value


def get_latest_data_dual(instId, short_bar="1H", long_bar="1D"):
    """
    双周期接口。
    短周期生成交易信号，长周期确认趋势方向。

    返回:
        (latest_data, last_trade_data, atr_value, long_direction, df_short, df_long)
    """
    global CURRENT_INSTID, CURRENT_SHORT_BAR, CURRENT_LONG_BAR

    CURRENT_INSTID = instId
    CURRENT_SHORT_BAR = short_bar
    CURRENT_LONG_BAR = long_bar

    # 获取短/长周期数据
    df_short = _fetch_kline_data(instId, short_bar)
    df_long = _fetch_kline_data(instId, long_bar)

    df_short = calculate_adx(df_short, period=14)
    df_long = calculate_adx(df_long, period=14)

    # 计算长周期方向序列
    _calculate_long_direction_series(df_long)

    # 获取长周期最新方向
    long_direction = None
    if 'LONG_DIRECTION' in df_long.columns:
        long_direction = df_long['LONG_DIRECTION'].dropna().iloc[-1] if len(df_long['LONG_DIRECTION'].dropna()) > 0 else None

    # 将长周期方向对齐到短周期
    _align_long_direction_to_short(df_short, df_long)

    # 截断没有长周期方向的短周期数据
    if 'LONG_DIRECTION' in df_short.columns:
        first_valid = df_short['LONG_DIRECTION'].first_valid_index()
        if first_valid is not None:
            df_short = df_short.loc[df_short.index >= first_valid]

    # 初始化 DataFrame 列
    _init_df_columns(df_short)

    # 运行策略（双周期模式）
    strat = _run_strategy(df_short, long_direction=long_direction, is_dual_period=True)

    # 构建返回值
    latest_data = df_short.iloc[-1].copy()
    _attach_prev_adx(df_short, latest_data)

    last_trade_data = _build_last_trade_data(strat, df_short)

    atr_value = _calculate_atr(df_short)

    return latest_data, last_trade_data, atr_value, long_direction, df_short, df_long


# ========== 辅助函数 ==========

def _init_df_columns(df):
    """初始化 DataFrame 所需的策略列"""
    for col in ['HISTOGRAM_ZERO', 'HIST_SMOOTH_DIFF_ZERO', 'INTERPOLATED_ZERO',
                'DIF', 'DEA', 'MACD', 'SMOOTHED_MACD', 'HIST_SMOOTH_DIFF',
                'HIST_WEIGHT', 'CURR_WEIGHT', 'TRADE_PRICE']:
        if col not in df.columns:
            df[col] = 0.0
    for col in ['ORIGINAL_FLAG', 'MODIFY_FLAG']:
        if col not in df.columns:
            df[col] = None


def _attach_prev_adx(df, latest_data):
    """附加前一K线的ADX/DI值"""
    if len(df) > 1:
        prev = df.iloc[-2]
        latest_data['ADX_PREV'] = prev.get('ADX', latest_data.get('ADX', 0))
        latest_data['+DI_PREV'] = prev.get('+DI', latest_data.get('+DI', 0))
        latest_data['-DI_PREV'] = prev.get('-DI', latest_data.get('-DI', 0))
    else:
        latest_data['ADX_PREV'] = latest_data.get('ADX', 0)
        latest_data['+DI_PREV'] = latest_data.get('+DI', 0)
        latest_data['-DI_PREV'] = latest_data.get('-DI', 0)


def _build_last_trade_data(strat, df):
    """构建 last_trade_data"""
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
    return last_trade_data


def _calculate_atr(df):
    """计算ATR值"""
    prev_close = df['close'].shift(1) if 'close' in df.columns else None
    tr1 = (df['high'] - df['low']) if 'high' in df.columns and 'low' in df.columns else None
    tr2 = (abs(df['high'] - prev_close)) if 'high' in df.columns and prev_close is not None else None
    tr3 = (abs(df['low'] - prev_close)) if 'low' in df.columns and prev_close is not None else None
    TR = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1) if tr1 is not None and tr2 is not None and tr3 is not None else None
    if TR is not None and len(TR) > 0:
        atr_series = TR.ewm(alpha=1 / 14, adjust=False).mean()
        return float(atr_series.iloc[-1])
    return float('nan')


def _build_detail_response(strat, df, latest_data, atr_value, strategy_name="单周期Pro3",
                           symbol=None, display_name=None, bar=None):
    """
    从策略对象构建详情页所需的完整字典。
    供 get_strategy_full_data / get_strategy_full_data_dual 复用。

    期货版差异：额外附加顶层 symbol / display_name / bar 字段，
    与现有期货详情页接口（pro3_strategy.get_strategy_full_data）保持兼容。
    """
    price = float(latest_data.get('close', 0))
    modify_flag = latest_data.get('MODIFY_FLAG', 'wait')

    # --- 行情概览 ---
    action_signal = '上涨' if modify_flag == 'rise' else ('下跌' if modify_flag == 'fall' else '观望')
    market_dict = {
        "price": round(price, 4),
        "action_signal": action_signal,
        "macd_histogram": round(float(latest_data.get('MACD', 0)), 4),
        "macd_dif": round(float(latest_data.get('DIF', 0)), 4),
        "adx": round(float(latest_data.get('ADX', 0)), 4),
        "plus_di": round(float(latest_data.get('+DI', 0)), 4),
        "minus_di": round(float(latest_data.get('-DI', 0)), 4),
        "smoothed_macd": round(float(latest_data.get('SMOOTHED_MACD', 0)), 4),
        "hist_weight": round(float(latest_data.get('HIST_WEIGHT', 0)), 4),
        "atr": round(atr_value, 4) if atr_value and not math.isnan(atr_value) else 0,
    }

    # --- 当前持仓 ---
    entry_price_val = 0.0
    profit_pct = 0.0
    status_str = '无持仓'
    if strat.current_position is not None and strat.entry_price is not None:
        entry_price_val = float(strat.entry_price)
        status_str = '多头' if strat.current_position == 'rise' else '空头'
        if entry_price_val > 0:
            if strat.current_position == 'rise':
                profit_pct = ((price / entry_price_val) - 1) * 100 * strat.times
            else:
                profit_pct = (1 - (price / entry_price_val)) * 100 * strat.times
    elif len(strat.trades) > 0:
        entry_price_val = float(strat.trades[-1])
        last_dir = strat.trade_dirs[-1] if strat.trade_dirs else 'rise'
        status_str = '多头' if last_dir == 'rise' else '空头'
        if entry_price_val > 0:
            if last_dir == 'rise':
                profit_pct = ((price / entry_price_val) - 1) * 100 * strat.times
            else:
                profit_pct = (1 - (price / entry_price_val)) * 100 * strat.times

    current_position_dict = {
        "status": status_str,
        "profit_pct": round(profit_pct, 2),
        "entry_price": round(entry_price_val, 4),
    }

    # --- 交易记录 ---
    trade_records = _build_trade_records(strat, df)

    # --- 统计数据 ---
    stats_dict = _build_stats_dict(strat, df)

    # 时间统计
    start_time = df.index.min()
    end_time = df.index.max()
    time_delta = end_time - start_time
    total_days = time_delta.days + time_delta.seconds / 86400
    if total_days < 1:
        total_days = 1.0
    total_return_pct = (strat.fix_money - strat.base_money) / strat.base_money * 100
    daily_profit = total_return_pct / total_days
    weekly_profit = daily_profit * 7
    contract_return_pct = (strat.contract_money - strat.base_money) / strat.base_money * 100

    stats_dict.update({
        "total_return_pct": round(total_return_pct, 2),
        "contract_return_pct": round(contract_return_pct, 2),
    })

    _symbol = symbol if symbol is not None else CURRENT_INSTID
    return {
        "strategy_name": strategy_name,
        "symbol": _symbol,
        "display_name": display_name if display_name is not None else _symbol,
        "bar": bar if bar is not None else CURRENT_BAR,
        "market": market_dict,
        "current_position": current_position_dict,
        "trade_records": trade_records,
        "stats": stats_dict,
        "start_time": start_time.strftime('%Y-%m-%d %H:%M') if hasattr(start_time, 'strftime') else str(start_time),
        "end_time": end_time.strftime('%Y-%m-%d %H:%M') if hasattr(end_time, 'strftime') else str(end_time),
        "total_days": round(total_days, 1),
        "daily_profit": round(daily_profit, 2),
        "weekly_profit": round(weekly_profit, 2),
    }


def _build_trade_records(strat, df):
    """从策略对象构建交易记录列表"""
    trade_records = []
    profits = strat.profits
    trades = strat.trades
    trade_times = strat.trade_times
    trade_dirs = strat.trade_dirs
    max_wins = strat.max_wins
    max_losses = strat.max_losses

    for i in range(len(profits)):
        open_price = float(trades[i * 2]) if i * 2 < len(trades) else 0
        close_price = float(trades[i * 2 + 1]) if i * 2 + 1 < len(trades) else 0

        open_time_str = ''
        close_time_str = ''
        if i * 2 < len(trade_times):
            t = trade_times[i * 2]
            open_time_str = t.strftime('%Y-%m-%d %H:%M') if hasattr(t, 'strftime') else str(t)
        if i * 2 + 1 < len(trade_times):
            t = trade_times[i * 2 + 1]
            close_time_str = t.strftime('%Y-%m-%d %H:%M') if hasattr(t, 'strftime') else str(t)

        direction = '多头' if (i < len(trade_dirs) and trade_dirs[i] == 'rise') else '空头'
        mw = float(max_wins[i]) if i < len(max_wins) else 0.0
        ml = float(max_losses[i]) if i < len(max_losses) else 0.0

        entry_adx_val = 0.0
        if i * 2 < len(trade_times):
            t = trade_times[i * 2]
            if t in df.index and 'ADX' in df.columns:
                entry_adx_val = float(df.loc[t, 'ADX'])

        duration_str = ''
        if i * 2 < len(trade_times) and i * 2 + 1 < len(trade_times):
            delta = trade_times[i * 2 + 1] - trade_times[i * 2]
            hours = delta.total_seconds() / 3600
            if hours >= 24:
                duration_str = f"{hours / 24:.1f}天"
            else:
                duration_str = f"{hours:.1f}小时"

        trade_records.append({
            "index": i + 1,
            "direction": direction,
            "open_time": open_time_str,
            "open_price": round(open_price, 4),
            "close_time": close_time_str,
            "close_price": round(close_price, 4),
            "profit": round(float(profits[i]), 2),
            "max_win": round(mw, 2),
            "max_loss": round(ml, 2),
            "entry_adx": round(entry_adx_val, 2),
            "duration": duration_str,
        })
    return trade_records


def _build_stats_dict(strat, df):
    """从策略对象构建统计数据字典"""
    profits = strat.profits
    max_wins = strat.max_wins
    max_losses = strat.max_losses
    trade_times = strat.trade_times
    total_trades = len(profits)
    winning_trades = sum(1 for p in profits if p > 0)
    losing_trades = sum(1 for p in profits if p < 0)
    win_rate = winning_trades / total_trades if total_trades > 0 else 0
    total_profit = sum(p for p in profits if p > 0)
    total_loss = sum(p for p in profits if p < 0)
    avg_profit = total_profit / winning_trades if winning_trades > 0 else 0
    avg_loss = total_loss / losing_trades if losing_trades > 0 else 0
    max_single_profit = max(profits) if profits else 0
    max_single_loss = min(profits) if profits else 0

    max_drawdown = 0
    if total_trades > 0:
        capital_curve = [strat.base_money]
        cap = strat.base_money
        for p in profits:
            cap = cap * (1 + p * 0.01)
            capital_curve.append(cap)
        peak = capital_curve[0]
        for v in capital_curve:
            if v > peak: peak = v
            dd = (peak - v) / peak * 100
            if dd > max_drawdown: max_drawdown = dd

    annual_return = ((strat.contract_money / strat.base_money) ** (252 / total_trades) - 1) * 100 if total_trades > 0 else 0
    calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0
    profit_factor = abs(total_profit / total_loss) if total_loss < 0 else 0
    profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0

    durations_h, win_dur, loss_dur = [], [], []
    for i in range(len(profits)):
        if i * 2 < len(trade_times) and i * 2 + 1 < len(trade_times):
            d = (trade_times[i * 2 + 1] - trade_times[i * 2]).total_seconds() / 3600
            durations_h.append(d)
            if profits[i] > 0: win_dur.append(d)
            else: loss_dur.append(d)
    avg_total_dur_h = sum(durations_h) / len(durations_h) if durations_h else 0
    avg_win_dur_h = sum(win_dur) / len(win_dur) if win_dur else 0
    avg_loss_dur_h = sum(loss_dur) / len(loss_dur) if loss_dur else 0

    retracements = [max_wins[i] - profits[i] if i < len(max_wins) else 0 for i in range(len(profits))]
    avg_retracement = sum(retracements) / len(retracements) if retracements else 0

    adx_entries = []
    for i in range(len(profits)):
        if i * 2 < len(trade_times):
            t = trade_times[i * 2]
            if t in df.index and 'ADX' in df.columns:
                adx_entries.append(float(df.loc[t, 'ADX']))
    adx_mean = float(np.mean(adx_entries)) if adx_entries else 0
    wins_adx = [adx_entries[i] for i in range(len(profits)) if i < len(adx_entries) and profits[i] > 0]
    losses_adx = [adx_entries[i] for i in range(len(profits)) if i < len(adx_entries) and profits[i] < 0]
    adx_win_mean = float(np.mean(wins_adx)) if wins_adx else 0
    adx_loss_mean = float(np.mean(losses_adx)) if losses_adx else 0
    adx_corr = 0.0
    try:
        if len(adx_entries) > 1 and len(profits) > 1:
            x = np.array(adx_entries[:len(profits)], dtype=float)
            y = np.array(profits, dtype=float)
            if np.std(x) > 0 and np.std(y) > 0:
                adx_corr = float(np.corrcoef(x, y)[0, 1])
    except Exception: pass
    high_idx = [i for i, a in enumerate(adx_entries) if a >= 25]
    low_idx = [i for i, a in enumerate(adx_entries) if a < 25]
    high_wins = sum(1 for i in high_idx if i < len(profits) and profits[i] > 0)
    low_wins = sum(1 for i in low_idx if i < len(profits) and profits[i] > 0)
    high_adx_win_rate = high_wins / len(high_idx) if high_idx else 0
    low_adx_win_rate = low_wins / len(low_idx) if low_idx else 0

    avg_hist_weight = float(np.mean([w[0] for w in strat.weight_history])) if strat.weight_history else 0
    avg_curr_weight = float(np.mean([w[1] for w in strat.weight_history])) if strat.weight_history else 0

    return {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 4),
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "avg_profit_pct": round(avg_profit, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "calmar_ratio": round(calmar_ratio, 2),
        "profit_factor": round(profit_factor, 2),
        "profit_loss_ratio": round(profit_loss_ratio, 2),
        "max_single_profit_pct": round(max_single_profit, 2),
        "max_single_loss_pct": round(max_single_loss, 2),
        "avg_total_dur_h": round(avg_total_dur_h, 2),
        "avg_win_dur_h": round(avg_win_dur_h, 2),
        "avg_loss_dur_h": round(avg_loss_dur_h, 2),
        "avg_retracement_pct": round(avg_retracement, 2),
        "adx_mean": round(adx_mean, 2),
        "adx_win_mean": round(adx_win_mean, 2),
        "adx_loss_mean": round(adx_loss_mean, 2),
        "adx_corr": round(adx_corr, 3),
        "high_adx_win_rate": round(high_adx_win_rate, 4),
        "low_adx_win_rate": round(low_adx_win_rate, 4),
        "avg_hist_weight": round(avg_hist_weight, 3),
        "avg_curr_weight": round(avg_curr_weight, 3),
        "fix_money": round(strat.fix_money, 2),
        "contract_money": round(strat.contract_money, 2),
        "mix_money": round(strat.mix_money, 2),
        "total_profit_pct": round(total_profit, 2),
        "total_loss_pct": round(total_loss, 2),
    }


def get_strategy_full_data(instId, bar, detail_mode=True, lookback_days=None, display_name=None):
    """
    单周期策略详情页接口（期货版）。

    参数:
        instId: 品种字母代码（'RB'）或具体合约代码（'RB2610'）
        bar: K线周期，如 '15m'/'1H'/'1D'/'1W'
        detail_mode: True 返回详情页字典，False 返回三元组
        lookback_days: 可选，仅回测最近N天数据
        display_name: 可选，品种中文名（用于前端展示）
    """
    global CURRENT_INSTID, CURRENT_BAR
    CURRENT_INSTID = instId
    CURRENT_BAR = bar

    df = _fetch_kline_data(instId, bar, lookback_days=lookback_days)
    df = calculate_adx(df, period=14)
    _init_df_columns(df)

    old_fast = globals().get('FAST_MODE', False)
    globals()['FAST_MODE'] = False
    old_print_orig = globals().get('PRINT_ORIGINAL_OUTPUT', 1)
    globals()['PRINT_ORIGINAL_OUTPUT'] = 0

    strat = _run_strategy(df, long_direction=None, is_dual_period=False)

    globals()['FAST_MODE'] = old_fast
    globals()['PRINT_ORIGINAL_OUTPUT'] = old_print_orig

    latest_data = df.iloc[-1].copy()
    _attach_prev_adx(df, latest_data)
    atr_value = _calculate_atr(df)

    if not detail_mode:
        last_trade_data = _build_last_trade_data(strat, df)
        return latest_data, last_trade_data, atr_value

    return _build_detail_response(strat, df, latest_data, atr_value, "单周期Pro3",
                                  symbol=instId, display_name=display_name, bar=bar)


def get_strategy_full_data_dual(instId, short_bar="1H", long_bar="1D",
                                lookback_days=None, display_name=None):
    """
    双周期策略详情页接口（期货版）。

    参数:
        instId: 品种字母代码（'RB'）或具体合约代码（'RB2610'）
        short_bar: 短周期（交易信号）
        long_bar: 长周期（趋势方向）
        lookback_days: 可选，短周期仅回测最近N天数据
        display_name: 可选，品种中文名（用于前端展示）
    """
    global CURRENT_INSTID, CURRENT_SHORT_BAR, CURRENT_LONG_BAR
    CURRENT_INSTID = instId
    CURRENT_SHORT_BAR = short_bar
    CURRENT_LONG_BAR = long_bar

    df_short = _fetch_kline_data(instId, short_bar, lookback_days=lookback_days)
    df_long = _fetch_kline_data(instId, long_bar)
    df_short = calculate_adx(df_short, period=14)
    df_long = calculate_adx(df_long, period=14)

    _calculate_long_direction_series(df_long)
    long_direction = None
    if 'LONG_DIRECTION' in df_long.columns:
        long_direction = df_long['LONG_DIRECTION'].dropna().iloc[-1] if len(df_long['LONG_DIRECTION'].dropna()) > 0 else None

    _align_long_direction_to_short(df_short, df_long)
    if 'LONG_DIRECTION' in df_short.columns:
        first_valid = df_short['LONG_DIRECTION'].first_valid_index()
        if first_valid is not None:
            df_short = df_short.loc[df_short.index >= first_valid]

    _init_df_columns(df_short)

    old_fast = globals().get('FAST_MODE', False)
    globals()['FAST_MODE'] = False
    old_print_orig = globals().get('PRINT_ORIGINAL_OUTPUT', 1)
    globals()['PRINT_ORIGINAL_OUTPUT'] = 0

    strat = _run_strategy(df_short, long_direction=long_direction, is_dual_period=True)

    globals()['FAST_MODE'] = old_fast
    globals()['PRINT_ORIGINAL_OUTPUT'] = old_print_orig

    latest_data = df_short.iloc[-1].copy()
    _attach_prev_adx(df_short, latest_data)
    atr_value = _calculate_atr(df_short)

    result = _build_detail_response(strat, df_short, latest_data, atr_value,
                                    f"双周期Pro3({short_bar}/{long_bar})",
                                    symbol=instId, display_name=display_name,
                                    bar=f"{short_bar}/{long_bar}")
    # 附加长周期方向信息
    norm_dir = _normalize_dir(long_direction)
    # 顶层中文 long_direction，供前端 renderDualPro3MarketSummary 读取
    result["long_direction"] = ('上涨' if norm_dir == 'rise'
                                else '下跌' if norm_dir == 'fall'
                                else '观望')
    # 顶层 short_bar/long_bar，供前端副标题（data.short_bar + '/' + data.long_bar）读取
    result["short_bar"] = short_bar
    result["long_bar"] = long_bar
    result["dual_period"] = {
        "short_bar": short_bar,
        "long_bar": long_bar,
        "long_direction": norm_dir or "--",
    }
    return result


def main():
    instId = "RB"          # 螺纹钢主力
    short_bar = "1H"
    long_bar = "1D"
    show_original_output = 1
    show_market_output = 0
    show_trade_ops_output = 0
    show_trade_records_output = 0
    entry_price_type = 'close'
    exit_price_type = 'open'

    global PRINT_MARKET, PRINT_TRADE_OPS, PRINT_TRADE_RECORDS, PRINT_ORIGINAL_OUTPUT
    global ENTRY_PRICE_TYPE, EXIT_PRICE_TYPE

    PRINT_MARKET = 1 if show_market_output else 0
    PRINT_TRADE_OPS = 1 if show_trade_ops_output else 0
    PRINT_TRADE_RECORDS = 1 if show_trade_records_output else 0
    PRINT_ORIGINAL_OUTPUT = 1 if show_original_output else 0
    ENTRY_PRICE_TYPE = entry_price_type
    EXIT_PRICE_TYPE = exit_price_type

    # 双周期示例
    latest_data, last_trade_data, atr, long_dir, df_short, df_long = get_latest_data_dual(
        instId, short_bar, long_bar
    )

    print(f"\n=== 期货双周期版本: ADX自适应平滑系统 + 双周期共振 ===")
    print(f"当前长周期方向: {long_dir}")
    print("最新行情时间：", latest_data.name)
    print("ATR值:", atr)


if __name__ == "__main__":
    main()
