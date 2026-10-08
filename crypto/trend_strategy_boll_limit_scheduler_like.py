import datetime
import math
import os
import sys

import backtrader as bt
import numpy as np
import pandas as pd

'''
    这里的boll去除了长周期带来的干扰，只关注短周期的趋势
    如果长周期方向反转导致短周期亏损，那忽略这笔交易
'''

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

CURRENT_INSTID = ""
CURRENT_SHORT_BAR = ""
CURRENT_LONG_BAR = ""

PRINT_MARKET = 0
PRINT_TRADE_OPS = 0
PRINT_TRADE_RECORDS = 0
PRINT_ORIGINAL_OUTPUT = 1

GLOBAL_DF_1M = None

def _csv_path_for_symbol(symbol):
    symbol = str(symbol).lower()
    fname = f"{symbol}_ohlcv_robust_1m_since_2022.csv"
    base = os.path.dirname(__file__)
    p1 = os.path.abspath(os.path.join(base, '..', 'strategyAI', 'data', fname))
    p2 = os.path.abspath(os.path.join(base, '..', '..', 'strategyAI', 'data', fname))
    return p1 if os.path.exists(p1) else p2

def load_symbol_csv(symbol, calc_full_flag='Y'):
    path = _csv_path_for_symbol(symbol)
    df = pd.read_csv(path)
    ts = pd.to_datetime(df['datetime'], utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    df['timestamp'] = ts
    df.set_index('timestamp', inplace=True)
    df = df[['open', 'high', 'low', 'close']].astype(float)
    if str(calc_full_flag).upper() == 'N' and len(df) > 0:
        end = pd.to_datetime(df.index.max())
        start = end - pd.Timedelta(days=300)
        df = df.loc[df.index >= start]
    return df.sort_index()

def _normalize_dir(v):
    s = str(v)
    if s == 'rise' or s == '多':
        return 'rise'
    if s == 'fall' or s == '空':
        return 'fall'
    return None

def _dir_to_value(v):
    d = _normalize_dir(v)
    if d == 'rise':
        return 1.0
    if d == 'fall':
        return -1.0
    return 0.0

def _value_to_dir(v):
    try:
        if v >= 0.5:
            return 'rise'
        if v <= -0.5:
            return 'fall'
    except Exception:
        return None
    return None

def _to_pandas_freq(bar):
    s = str(bar).strip()
    sl = s.lower()
    if sl.endswith('m'):
        n = ''.join(ch for ch in sl if ch.isdigit()) or '1'
        return f"{n}min"
    if sl.endswith('h') or s.endswith('H'):
        n = ''.join(ch for ch in sl if ch.isdigit()) or '1'
        return f"{n}h"
    if sl.endswith('d') or s.endswith('D'):
        n = ''.join(ch for ch in sl if ch.isdigit()) or '1'
        return f"{n}D"
    if sl.endswith('w') or s.endswith('W'):
        n = ''.join(ch for ch in sl if ch.isdigit()) or '1'
        return f"{n}W"
    return s

def _resample_ohlc(df_1m, bar):
    freq = _to_pandas_freq(bar)
    ohlc = df_1m.resample(freq, label='right', closed='right').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
    try:
        ohlc.index = ohlc.index.tz_localize(None)
    except Exception:
        pass
    ohlc['confirm'] = 1.0
    ohlc = calculate_adx(ohlc, period=14)
    return ohlc

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
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    curr_weight = 1.0 - hist_weight
    return hist_weight, curr_weight

def get_period_data(instId, bar):
    if GLOBAL_DF_1M is None:
        raise RuntimeError('GLOBAL_DF_1M is not initialized')
    return _resample_ohlc(GLOBAL_DF_1M, bar)

def calculate_period_direction(df):
    class DirectionStrategy(bt.Strategy):
        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)
            self.smoothed_hist = []
            self.previous_diff = None
            self.justice_flag = "smoothed_histogram"
            self.dealjustice_flag = None
            self.modify_flag = None
            self.final_direction = None

        def next(self):
            current_idx = len(self) - 1
            adx_value = df.iloc[current_idx]['ADX'] if current_idx < len(df) else 0
            macd_value = self.macd.macd[0]
            signal_value = self.macd.signal[0]
            histogram = 2 * (macd_value - signal_value)
            hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)
            if len(self.smoothed_hist) == 0:
                smoothed_histogram = histogram
            else:
                smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
            self.smoothed_hist.append(smoothed_histogram)
            if self.justice_flag == "smoothed_histogram":
                if histogram - smoothed_histogram > 0:
                    if len(self.data) > 1 and self.macd.macd[-1] > 0:
                        self.modify_flag = "rise"
                        self.justice_flag = "smoothed_histogram"
                    else:
                        self.justice_flag = "histogram"
                        self.dealjustice_flag = "waitRise"
                else:
                    if len(self.data) > 1 and self.macd.macd[-1] < 0:
                        self.modify_flag = "fall"
                        self.justice_flag = "smoothed_histogram"
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
            self.final_direction = self.modify_flag

    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(DirectionStrategy)
    cerebro.run()
    strat = cerebro.runstrats[0][0]
    return strat.final_direction

def fill_long_direction_series(df):
    class DirectionSeries(bt.Strategy):
        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)
            self.smoothed_hist = []
            self.previous_diff = None
            self.justice_flag = "smoothed_histogram"
            self.dealjustice_flag = None
            self.modify_flag = None

        def next(self):
            current_idx = len(self) - 1
            adx_value = df.iloc[current_idx]['ADX'] if current_idx < len(df) else 0
            macd_value = self.macd.macd[0]
            signal_value = self.macd.signal[0]
            histogram = 2 * (macd_value - signal_value)
            hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)
            if len(self.smoothed_hist) == 0:
                smoothed_histogram = histogram
            else:
                smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
            self.smoothed_hist.append(smoothed_histogram)
            if self.justice_flag == "smoothed_histogram":
                if histogram - smoothed_histogram > 0:
                    if len(self.data) > 1 and self.macd.macd[-1] > 0:
                        self.modify_flag = "rise"
                        self.justice_flag = "smoothed_histogram"
                    else:
                        self.justice_flag = "histogram"
                        self.dealjustice_flag = "waitRise"
                else:
                    if len(self.data) > 1 and self.macd.macd[-1] < 0:
                        self.modify_flag = "fall"
                        self.justice_flag = "smoothed_histogram"
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
            df.loc[self.data.datetime.datetime(0), 'LONG_DIRECTION'] = _dir_to_value(self.modify_flag)

    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(DirectionSeries)
    cerebro.run()

class PandasDataWithLongDirection(bt.feeds.PandasData):
    lines = ('LONG_DIRECTION', 'LONG_CONF')
    params = (('LONG_DIRECTION', -1), ('LONG_CONF', -1))

class DualTimeframeBollLimitStrategy(bt.Strategy):
    params = (
        ('boll_period', 20),
        ('boll_dev', 2.0),
        ('order_size', 1.0),
        ('leverage_times', 10.0),
    )
    def __init__(self):
        self.boll = bt.indicators.BollingerBands(self.data.close, period=self.p.boll_period, devfactor=self.p.boll_dev)
        # 逐 bar ATR（Wilder 14）：BOLL 限价策略本身不做 ATR 外扩，仅用于详情页
        # 展示挂单推导链（轨道→ATR→挂单价）时回溯开仓时刻的波动幅度。
        self.atr = bt.indicators.ATR(self.data, period=14)
        self.k = 0
        self.pos_size = 0.0
        self.entry_price = None
        self.entry_time = None
        self.entry_long_dir = None
        self.entry_band = None    # 开仓时刻的原始 BOLL 轨道（多头=下轨/空头=上轨）
        self.entry_atr = None     # 开仓时刻的 ATR 值
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
        low = float(self.data.low[0])
        high = float(self.data.high[0])
        close_price = float(self.data.close[0])
        now_time = self.data.datetime.datetime(0)
        if self.pos_size != 0 and self.entry_price is not None:
            if self.max_price_since_entry is None or high > self.max_price_since_entry:
                self.max_price_since_entry = high
            if self.min_price_since_entry is None or low < self.min_price_since_entry:
                self.min_price_since_entry = low
            if self.entry_long_dir and long_dir != self.entry_long_dir:
                self.position_flip = True
        touch_bot = low <= boll_bot <= high
        touch_top = low <= boll_top <= high
        if self.pos_size == 0:
            if long_dir == 'rise' and touch_bot:
                self.k += 1
                self.pos_size = self.p.order_size
                self.entry_price = boll_bot
                self.entry_time = now_time
                self.entry_long_dir = long_dir
                self.entry_band = boll_bot
                self.entry_atr = float(self.atr[0]) if not math.isnan(self.atr[0]) else 0.0
                self.position_flip = False
                self.max_price_since_entry = boll_bot
                self.min_price_since_entry = boll_bot
                self.trades.append(boll_bot)
                self.trade_times.append(now_time)
                self.trade_dirs.append('rise')
                if PRINT_TRADE_OPS:
                    print(f"开多 第{self.k}次 (方向:{long_dir}) 触边即入 BOLL下轨 限价{boll_bot:.4f} {now_time}")
            elif long_dir == 'fall' and touch_top:
                self.k += 1
                self.pos_size = -self.p.order_size
                self.entry_price = boll_top
                self.entry_time = now_time
                self.entry_long_dir = long_dir
                self.entry_band = boll_top
                self.entry_atr = float(self.atr[0]) if not math.isnan(self.atr[0]) else 0.0
                self.position_flip = False
                self.max_price_since_entry = boll_top
                self.min_price_since_entry = boll_top
                self.trades.append(boll_top)
                self.trade_times.append(now_time)
                self.trade_dirs.append('fall')
                if PRINT_TRADE_OPS:
                    print(f"开空 第{self.k}次 (方向:{long_dir}) 触边即入 BOLL上轨 限价{boll_top:.4f} {now_time}")
        elif self.pos_size > 0 and touch_top:
            exit_price = boll_top
            profit = (exit_price / self.entry_price - 1) * 100 * leverage_times
            max_win = (self.max_price_since_entry / self.entry_price - 1) * 100 * leverage_times if self.max_price_since_entry else 0.0
            max_loss = (self.min_price_since_entry / self.entry_price - 1) * 100 * leverage_times if self.min_price_since_entry else 0.0
            excluded = self.position_flip and profit < 0
            if excluded:
                self.excluded_profits.append(profit)
            else:
                self.profits.append(profit)
                self.max_wins.append(max_win)
                self.max_losses.append(max_loss)
            self.trade_records.append({
                'dir': 'rise',
                'open_price': float(self.entry_price),
                'close_price': float(exit_price),
                'entry_time': self.entry_time,
                'exit_time': now_time,
                'profit': float(profit),
                'max_win': float(max_win),
                'max_loss': float(max_loss),
                'excluded': excluded,
                # 挂单推导链：BOLL 不做外扩，故 boll_band 即实际挂单价 order_price
                'boll_band': float(self.entry_band) if self.entry_band is not None else None,
                'atr_value': float(self.entry_atr) if self.entry_atr is not None else 0.0,
                'order_price': float(self.entry_price),
            })
            self.trades.append(exit_price)
            self.trade_times.append(now_time)
            self.last_trade_index = len(self) - 1
            if PRINT_TRADE_OPS:
                print(f"平多 第{self.k}次 触边即出 BOLL上轨 限价{exit_price:.4f} 盈亏{profit:.2f}% {now_time}")
            self.pos_size = 0.0
            self.entry_price = None
            self.entry_time = None
            self.entry_long_dir = None
            self.entry_band = None
            self.entry_atr = None
            self.position_flip = False
            self.max_price_since_entry = None
            self.min_price_since_entry = None
        elif self.pos_size < 0 and touch_bot:
            exit_price = boll_bot
            profit = (1 - (exit_price / self.entry_price)) * 100 * leverage_times
            max_win = (1 - (self.min_price_since_entry / self.entry_price)) * 100 * leverage_times if self.min_price_since_entry else 0.0
            max_loss = (1 - (self.max_price_since_entry / self.entry_price)) * 100 * leverage_times if self.max_price_since_entry else 0.0
            excluded = self.position_flip and profit < 0
            if excluded:
                self.excluded_profits.append(profit)
            else:
                self.profits.append(profit)
                self.max_wins.append(max_win)
                self.max_losses.append(max_loss)
            self.trade_records.append({
                'dir': 'fall',
                'open_price': float(self.entry_price),
                'close_price': float(exit_price),
                'entry_time': self.entry_time,
                'exit_time': now_time,
                'profit': float(profit),
                'max_win': float(max_win),
                'max_loss': float(max_loss),
                'excluded': excluded,
                # 挂单推导链：BOLL 不做外扩，故 boll_band 即实际挂单价 order_price
                'boll_band': float(self.entry_band) if self.entry_band is not None else None,
                'atr_value': float(self.entry_atr) if self.entry_atr is not None else 0.0,
                'order_price': float(self.entry_price),
            })
            self.trades.append(exit_price)
            self.trade_times.append(now_time)
            self.last_trade_index = len(self) - 1
            if PRINT_TRADE_OPS:
                print(f"平空 第{self.k}次 触边即出 BOLL下轨 限价{exit_price:.4f} 盈亏{profit:.2f}% {now_time}")
            self.pos_size = 0.0
            self.entry_price = None
            self.entry_time = None
            self.entry_long_dir = None
            self.entry_band = None
            self.entry_atr = None
            self.position_flip = False
            self.max_price_since_entry = None
            self.min_price_since_entry = None
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
            print(f"{now_time} 长周期:{long_dir} 行情范围: 低{low:.4f} 高{high:.3f} 收{close_price:.3f} | BOLL: 下轨{boll_bot:.3f} 中轨{boll_mid:.3f} 上轨{boll_top:.3f}{pl_suffix} | 状态:{state} | 关系:{rel}")


def run_boll_strategy(df, order_size=1.0):
    """
    运行BOLL限价策略并返回策略对象（含trade_records等）。
    供适配器调用，策略逻辑与get_latest_data内部完全一致。
    """
    cerebro = bt.Cerebro()
    data = PandasDataWithLongDirection(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(DualTimeframeBollLimitStrategy, order_size=float(order_size))
    cerebro.run()
    return cerebro.runstrats[0][0]

def get_latest_data(instId, short_bar="1H", long_bar="1D", order_size=1.0):
    global CURRENT_INSTID, CURRENT_SHORT_BAR, CURRENT_LONG_BAR
    CURRENT_INSTID = instId
    CURRENT_SHORT_BAR = short_bar
    CURRENT_LONG_BAR = long_bar
    if PRINT_MARKET:
        print(f"正在获取短周期数据 ({short_bar})...")
    df_short = get_period_data(instId, short_bar)
    if PRINT_MARKET:
        print(f"正在获取长周期数据 ({long_bar})...")
    df_long = get_period_data(instId, long_bar)
    if PRINT_MARKET:
        print("计算长周期方向...")
    long_direction = calculate_period_direction(df_long)
    fill_long_direction_series(df_long)
    if 'LONG_DIRECTION' not in df_long.columns:
        df_long['LONG_DIRECTION'] = 0.0
    df_long['LONG_DIRECTION'] = df_long['LONG_DIRECTION'].fillna(0.0)
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
            'LONG_CONF': df_long['LONG_CONF'].values
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
    first_valid = df['LONG_DIRECTION'].first_valid_index() if 'LONG_DIRECTION' in df.columns else None
    if first_valid is not None:
        df = df.loc[df.index >= first_valid]


    cerebro = bt.Cerebro()
    data = PandasDataWithLongDirection(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(DualTimeframeBollLimitStrategy, order_size=float(order_size))
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
    prev_close = df['close'].shift(1) if 'close' in df.columns else None
    tr1 = (df['high'] - df['low']) if 'high' in df.columns and 'low' in df.columns else None
    tr2 = (abs(df['high'] - prev_close)) if 'high' in df.columns and prev_close is not None else None
    tr3 = (abs(df['low'] - prev_close)) if 'low' in df.columns and prev_close is not None else None
    TR = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1) if tr1 is not None and tr2 is not None and tr3 is not None else None
    if TR is not None and len(TR) > 0:
        atr_series = TR.ewm(alpha=1/14, adjust=False).mean()
        atr_value = float(atr_series.iloc[-1])
    else:
        atr_value = float('nan')
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
            fix_change = fix_after - fix_before
            contract_change = contract_after - contract_before
            mix_change = mix_after - mix_before
            exclude_txt = " 剔除" if rec['excluded'] else ""
            print(f"第{i+1}次交易       开仓:{float(rec['open_price']):.3f} 平仓:{float(rec['close_price']):.3f}   最终盈利:{profit:+.2f}% 最大盈利:{float(rec['max_win']):.2f}%  最大亏损:{float(rec['max_loss']):.2f}% 定投:{fix_before:.0f}→{fix_after:.0f}({fix_change:+.0f})       复投:{contract_before:.0f}→{contract_after:.0f}({contract_change:+.0f})    混投:{mix_before:.0f}→{mix_after:.0f}({mix_change:+.0f}){exclude_txt}")
    if PRINT_ORIGINAL_OUTPUT:
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
        avg_hold_max_profit = (hold_total_max_profit / len(win_indices)) if win_indices else 0.0
        avg_hold_max_loss = (hold_total_max_loss / len(loss_indices)) if loss_indices else 0.0
        hold_pl_ratio = (abs(avg_hold_max_profit / avg_hold_max_loss) if avg_hold_max_loss != 0 else 0.0)
        overall_pl_ratio = (abs(total_profit / total_loss) if total_loss < 0 else 0.0)
        actual_profit_rate = total_profit / hold_total_max_profit if hold_total_max_profit > 0 else 0.0
        actual_loss_rate = abs(total_loss) / abs(hold_total_max_loss) if hold_total_max_loss < 0 else 0.0
        start_time = None
        end_time = None
        included_times = [(r.get('entry_time'), r.get('exit_time')) for r in strat.trade_records if not r['excluded']]
        if included_times:
            start_time = min(t[0] for t in included_times if t[0] is not None)
            end_time = max(t[1] for t in included_times if t[1] is not None)
        total_days = 0
        daily_profit = 0.0
        weekly_profit = 0.0
        if start_time and end_time:
            time_delta = end_time - start_time
            total_days = time_delta.days
            if total_days > 0:
                total_return = (fix_money - 100.0) / 100.0 * 100
                daily_profit = total_return / total_days
                weekly_profit = daily_profit * 7
        max_drawdown = 0.0
        if len(contract_money_history) > 1:
            peak = contract_money_history[0]
            for value in contract_money_history:
                if value > peak:
                    peak = value
                drawdown = (peak - value) / peak * 100 if peak > 0 else 0.0
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
        fix_drawdown = 0.0
        if len(fix_money_history) > 1:
            fix_peak = fix_money_history[0]
            for value in fix_money_history:
                if value > fix_peak:
                    fix_peak = value
                if fix_peak > 0:
                    drawdown = (fix_peak - value) / fix_peak * 100
                    if drawdown > fix_drawdown:
                        fix_drawdown = drawdown
        mix_drawdown = 0.0
        if len(mix_money_history) > 1:
            mix_peak = mix_money_history[0]
            for value in mix_money_history:
                if value > mix_peak:
                    mix_peak = value
                if mix_peak > 0:
                    drawdown = (mix_peak - value) / mix_peak * 100
                    if drawdown > mix_drawdown:
                        mix_drawdown = drawdown
        annual_return = ((contract_money / 100.0) ** (252 / total_trades) - 1) * 100 if total_trades > 0 else 0.0
        calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0.0
        profit_factor = abs(total_profit / total_loss) if total_loss < 0 else 0.0
        print("\nBOLL限价双周期调度逻辑回测统计")
        print("="*80)
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
        final_mean_pl_ratio = (abs(avg_profit) / abs(avg_loss) if avg_loss != 0 else 0.0)
        final_sum_pl_ratio = (abs(total_profit) / abs(total_loss) if total_loss != 0 else 0.0)
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
        print("="*80)
    return latest_data, last_trade_data, atr_value, long_direction, df, df_long

def run_with_csv(
    symbol='near',
    calc_full_flag='Y',
    short_bar='15m',
    long_bar='4H',
    order_size=1.0,
    show_original_output=1,
    show_market_output=0,
    show_trade_ops_output=0,
    show_trade_records_output=0,
):
    global GLOBAL_DF_1M
    global PRINT_MARKET, PRINT_TRADE_OPS, PRINT_TRADE_RECORDS, PRINT_ORIGINAL_OUTPUT
    GLOBAL_DF_1M = load_symbol_csv(symbol, calc_full_flag)
    PRINT_MARKET = 1 if show_market_output else 0
    PRINT_TRADE_OPS = 1 if show_trade_ops_output else 0
    PRINT_TRADE_RECORDS = 1 if show_trade_records_output else 0
    PRINT_ORIGINAL_OUTPUT = 1 if show_original_output else 0
    instId = f"{str(symbol).upper()}-USDT-SWAP"
    latest_data, last_trade_data, atr, long_direction, df, df_long = get_latest_data(
        instId, short_bar, long_bar, order_size=order_size
    )
    if PRINT_ORIGINAL_OUTPUT:
        print(f"\n当前长周期方向: {long_direction}")
        print("最新行情时间：", latest_data.name)
        print("ATR值:", atr)

def main():
    instId = "NEAR-USDT-SWAP"
    short_bar = "15m"
    long_bar = "4H"
    show_original_output = 1            # 统计输出：1输出，0不输出
    show_market_output = 0              # 行情输出：1输出，0不输出
    show_trade_ops_output = 0           # 交易操作输出：1输出，0不输出
    show_trade_records_output = 0       # 交易记录输出：1输出，0不输出
    symbol = instId.split('-')[0].lower()
    run_with_csv(
        symbol=symbol,
        calc_full_flag='N',
        short_bar=short_bar,
        long_bar=long_bar,
        order_size=1.0,
        show_original_output=show_original_output,
        show_market_output=show_market_output,
        show_trade_ops_output=show_trade_ops_output,
        show_trade_records_output=show_trade_records_output,
    )

if __name__ == "__main__":
    main()
