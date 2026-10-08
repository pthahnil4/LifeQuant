# type: ignore
import datetime
import math
import os
import sys

import backtrader as bt
import numpy as np
import pandas as pd

'''
趋势策略模版（双周期 + MACD/ADX + 开仓限价单+平仓市价）
========================================================

用途：作为后续策略的统一模版文件，可直接拷贝并按需扩展。

核心机制：
- 短周期生成交易信号；长周期确认趋势方向
- 开仓：短周期信号与长周期方向一致时，以open价提交限价单
- 平仓：短周期信号反转时，直接以open价成交（无限价单）

输出控制：
- 行情输出、交易操作与决策输出、交易记录输出

【相对基准模版的修改】
1. 开仓改为限价单：以信号bar的open价提交限价单，有效期4根bar，未成交则撤单
2. 平仓改为市价：信号反转时直接以open价成交，不提交限价单
3. 简化 modify_flag 计算：移除 justice_flag 状态机，直接用 histogram vs smoothed_histogram 判断方向
4. 新增限价单统计计数器：entry_orders_submitted / filled / expired / cancelled
5. 新增 total_exits / total_signal_flips 统计
6. 数据回溯期从90天改为60天
7. pandas resample频率格式从 'T' 改为 'min'
8. 平仓离场扩展：长周期方向翻转到持仓反方向时也触发平仓（不再仅依赖短周期信号翻转）
9. 挂单期间短周期信号反向时撤销未成交开仓限价单
10. 年化收益改为按实际回测天数（365天/年）几何年化，并对爆仓/无交易做保护
'''

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 全局运行态变量（模板可复用）
CURRENT_INSTID = ""            # 当前交易对，如 'NEAR-USDT-SWAP'
CURRENT_SHORT_BAR = ""         # 当前短周期字符串，如 '1H'
CURRENT_LONG_BAR = ""          # 当前长周期字符串，如 '1D'

PRINT_MARKET = 0               # 行情输出开关：1启用，0禁用
PRINT_TRADE_OPS = 0            # 交易操作与决策输出开关：1启用，0禁用
PRINT_TRADE_RECORDS = 0        # 交易记录输出开关：1启用，0禁用
PRINT_ORIGINAL_OUTPUT = 1      # 策略统计输出开关：1输出统计，0不输出

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
        start = end - pd.Timedelta(days=60)
        df = df.loc[df.index >= start]
    return df.sort_index()

def _normalize_dir(v):
    s = str(v)
    if s == 'rise' or s == '多':
        return 'rise'
    if s == 'fall' or s == '空':
        return 'fall'
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

def get_adaptive_smooth_weight(adx_value, base_weight=0.7):
    """
    根据ADX计算自适应平滑权重
    
    参数:
        adx_value: 当前ADX值
        base_weight: 基础权重（默认0.7）
    
    返回:
        (hist_weight, curr_weight): 历史权重和当前权重
    """
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    curr_weight = 1.0 - hist_weight
    
    return hist_weight, curr_weight

def get_period_data(instId, bar):
    if GLOBAL_DF_1M is None:
        raise RuntimeError('GLOBAL_DF_1M is not initialized')
    return _resample_ohlc(GLOBAL_DF_1M, bar)

def calculate_period_direction(df):
    """
    计算单个周期的方向（不进行实际交易，只计算信号）
    返回最新的modify_flag
    """
    class DirectionStrategy(bt.Strategy):  # type: ignore
        """仅用于计算方向的策略"""
        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)  # type: ignore
            self.smoothed_hist = []
            self.previous_diff = None
            self.justice_flag = "smoothed_histogram"
            self.dealjustice_flag = None
            self.modify_flag = None
            self.final_direction = None

        def next(self):
            """计算方向信号"""
            current_idx = len(self) - 1
            adx_value = df.iloc[current_idx]['ADX'] if current_idx < len(df) else 0
            
            # 计算MACD
            macd_value = self.macd.macd[0]
            signal_value = self.macd.signal[0]
            histogram = 2 * (macd_value - signal_value)
            
            # 使用ADX自适应平滑系数
            hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)
            
            if len(self.smoothed_hist) == 0:
                smoothed_histogram = histogram
            else:
                smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
            self.smoothed_hist.append(smoothed_histogram)
            
            hist_smooth_diff = histogram - smoothed_histogram
            self.previous_diff = hist_smooth_diff

            # modify_flag计算（MACD平滑直接交叉，比状态机更快更灵敏）
            # 直接用 histogram 与 smoothed_histogram 的交叉判定方向
            # 无需 justice_flag 状态机，避免 MACD 接近零轴时的额外确认延迟
            if histogram - smoothed_histogram > 0:
                self.modify_flag = "rise"
            else:
                self.modify_flag = "fall"

            self.final_direction = self.modify_flag

    cerebro = bt.Cerebro()  # type: ignore
    data = bt.feeds.PandasData(dataname=df)  # type: ignore
    cerebro.adddata(data)  # type: ignore
    cerebro.addstrategy(DirectionStrategy)  # type: ignore
    cerebro.run()  # type: ignore
    
    strat = cerebro.runstrats[0][0]
    return strat.final_direction

def fill_long_direction_series(df):
    """填充长周期方向序列"""
    class DirectionSeries(bt.Strategy):  # type: ignore
        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)  # type: ignore
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
            
            hist_smooth_diff = histogram - smoothed_histogram
            
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
                        
            df.loc[self.data.datetime.datetime(0), 'LONG_DIRECTION'] = self.modify_flag
            self.previous_diff = hist_smooth_diff
            
    cerebro = bt.Cerebro()  # type: ignore
    data = bt.feeds.PandasData(dataname=df)  # type: ignore
    cerebro.adddata(data)  # type: ignore
    cerebro.addstrategy(DirectionSeries)  # type: ignore
    cerebro.run()  # type: ignore

def get_latest_data(instId, short_bar="1H", long_bar="1D"):
    """获取双周期数据并执行策略"""
    global CURRENT_INSTID, CURRENT_SHORT_BAR, CURRENT_LONG_BAR
    
    # 设置全局变量
    CURRENT_INSTID = instId
    CURRENT_SHORT_BAR = short_bar
    CURRENT_LONG_BAR = long_bar
    
    # 获取短周期数据
    if PRINT_MARKET:
        print(f"正在获取短周期数据 ({short_bar})...")
    df_short = get_period_data(instId, short_bar)
    
    if PRINT_MARKET:
        print(f"正在获取长周期数据 ({long_bar})...")
    df_long = get_period_data(instId, long_bar)
    
    # 计算长周期方向
    if PRINT_MARKET:
        print("计算长周期方向...")
    long_direction = calculate_period_direction(df_long)
    fill_long_direction_series(df_long)
    if PRINT_MARKET:
        print(f"长周期方向: {long_direction}")
    
    # 使用短周期数据运行主策略
    df = df_short
    
    # 初始化需要的列，避免nan值
    df['HISTOGRAM_ZERO'] = 0.0
    df['HIST_SMOOTH_DIFF_ZERO'] = 0.0
    df['ORIGINAL_FLAG'] = None
    df['DIF'] = 0.0
    df['DEA'] = 0.0
    df['MACD'] = 0.0
    df['SMOOTHED_MACD'] = 0.0
    df['HIST_SMOOTH_DIFF'] = 0.0
    df['HIST_WEIGHT'] = 0.0
    df['CURR_WEIGHT'] = 0.0
    df['MODIFY_FLAG'] = None
    df['TRADE_PRICE'] = 0.0

    if 'LONG_DIRECTION' in df_long.columns:
        try:
            if hasattr(df.index, 'tz') and df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            if hasattr(df_long.index, 'tz') and df_long.index.tz is not None:
                df_long.index = df_long.index.tz_localize(None)
            short_times = pd.DataFrame({'ts': pd.to_datetime(df.index)})
            long_times = pd.DataFrame({'ts': pd.to_datetime(df_long.index), 'LONG_DIRECTION': df_long['LONG_DIRECTION'].values})
            short_times = short_times.sort_values('ts')
            long_times = long_times.sort_values('ts')
            aligned = pd.merge_asof(short_times, long_times, on='ts', direction='backward')
            df['LONG_DIRECTION'] = aligned['LONG_DIRECTION'].values
        except Exception:
            df['LONG_DIRECTION'] = df_long['LONG_DIRECTION'].reindex(df.index, method='ffill')
    else:
        df['LONG_DIRECTION'] = np.nan

    first_valid = df['LONG_DIRECTION'].first_valid_index() if 'LONG_DIRECTION' in df.columns else None
    if first_valid is not None:
        df = df.loc[df.index >= first_valid]

    class DualTimeframeStrategy(bt.Strategy):  # type: ignore
        params = ()
        """双周期策略：短周期信号 + 长周期方向确认"""
        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)  # type: ignore
            self.ema12 = bt.indicators.EMA(self.data.close, period=12)  # type: ignore
            self.ema26 = bt.indicators.EMA(self.data.close, period=26)  # type: ignore
            
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
            self.current_position = None  # 当前持仓方向
            self.filtered_signals = 0  # 被过滤的信号数（空仓时方向不一致）
            self.total_signals = 0  # 实际开仓信号数
            self.total_exits = 0    # 平仓次数
            self.total_signal_flips = 0  # 信号翻转总次数
            self.entry_price = None
            
            # === 限价单相关参数与状态 ===
            self.limit_order_ttl = 4  # 限价单有效期（N个bar，默认4个15分钟=1小时）
            self.pending_order = None  # 当前挂单: dict(type, price, submit_bar, direction, open_price_for_close)
            
            # 限价单统计计数器
            self.entry_orders_submitted = 0   # 提交的开仓限价单总数
            self.entry_orders_filled = 0      # 成功成交的开仓限价单数
            self.entry_orders_expired = 0     # 过期未成交的开仓限价单数
            self.entry_orders_cancelled = 0   # 因信号反向而撤销的开仓限价单数
            # 平仓统计（平仓为直接成交，无需限价单）

        def next(self):
            """策略主逻辑 - 双周期版本"""
            current_idx = len(self) - 1
            adx_value = df.iloc[current_idx]['ADX'] if current_idx < len(df) else 0
            
            # 计算MACD
            macd_value = self.macd.macd[0]
            signal_value = self.macd.signal[0]
            histogram = 2 * (macd_value - signal_value)
            
            # ADX自适应平滑系数
            hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)
            self.weight_history.append((hist_weight, curr_weight))
            
            if len(self.smoothed_hist) == 0:
                smoothed_histogram = histogram
            else:
                smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
            self.smoothed_hist.append(smoothed_histogram)
            
            hist_smooth_diff = histogram - smoothed_histogram
            
            # 计算零点价格
            histogram_zero = 0.0
            hist_smooth_diff_zero = 0.0
            if len(self.data) > 1:
                prev_dea = self.macd.signal[-1]
                prev_ema12 = self.ema12[-1]
                prev_ema26 = self.ema26[-1]
                prev_smoothed = self.smoothed_hist[-2] if len(self.smoothed_hist) > 1 else 0
                
                # 精确计算：求解下一根K线收盘价为多少时，MACD柱=0
                histogram_zero = (prev_dea - (prev_ema12 * 11.0/13.0) + (prev_ema26 * 25.0/27.0)) / (2.0/13.0 - 2.0/27.0)
                
                if curr_weight != 0:
                    hist_smooth_diff_zero = (prev_dea - (prev_ema12 * 11.0/13.0) + (prev_ema26 * 25.0/27.0) + (prev_smoothed * hist_weight / curr_weight)) / (2.0/13.0 - 2.0/27.0)
                else:
                    hist_smooth_diff_zero = histogram_zero
                
                df.loc[self.data.datetime.datetime(0), 'HISTOGRAM_ZERO'] = histogram_zero
                df.loc[self.data.datetime.datetime(0), 'HIST_SMOOTH_DIFF_ZERO'] = hist_smooth_diff_zero

            # 原始信号判断
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
            df.loc[self.data.datetime.datetime(0), 'SMOOTHED_MACD'] = smoothed_histogram
            df.loc[self.data.datetime.datetime(0), 'HIST_SMOOTH_DIFF'] = hist_smooth_diff
            df.loc[self.data.datetime.datetime(0), 'HIST_WEIGHT'] = hist_weight
            df.loc[self.data.datetime.datetime(0), 'CURR_WEIGHT'] = curr_weight

            # modify_flag计算（MACD平滑直接交叉，比状态机更快更灵敏）
            if histogram - smoothed_histogram > 0:
                self.modify_flag = "rise"
            else:
                self.modify_flag = "fall"

            df.loc[self.data.datetime.datetime(0), 'MODIFY_FLAG'] = self.modify_flag

            # === 双周期交易逻辑（开仓限价单 + 平仓市价）===
            # 判断双周期是否一致
            raw_long_dir = df.loc[self.data.datetime.datetime(0), 'LONG_DIRECTION'] if 'LONG_DIRECTION' in df.columns else None
            if raw_long_dir is None:
                return
            bar_long_dir = _normalize_dir(raw_long_dir)
            directions_match = (self.modify_flag == bar_long_dir)
            signal_changed = self.modify_flag != getattr(self, 'last_modify_flag', None)

            # 统计所有信号翻转
            if signal_changed:
                self.total_signal_flips += 1

            current_bar_idx = len(self) - 1
            cur_open = float(self.data.open[0])
            cur_high = float(self.data.high[0])
            cur_low  = float(self.data.low[0])

            # -------------------------------------------------------
            # Step 1: 处理开仓挂单（成交判定 + 过期撤单）
            # -------------------------------------------------------
            if self.pending_order is not None:
                order = self.pending_order
                bars_elapsed = current_bar_idx - order['submit_bar']
                order_price = order['price']

                # 判定是否成交：当前bar的 high/low 覆盖限价单价格
                filled = (cur_low <= order_price <= cur_high)

                if filled:
                    # === 开仓限价单成交 ===
                    self.entry_orders_filled += 1
                    self.k += 1
                    trade_price = order_price
                    df.loc[self.data.datetime.datetime(0), 'TRADE_PRICE'] = trade_price
                    self.trades.append(trade_price)
                    self.trade_times.append(self.data.datetime.datetime(0))
                    self.trade_dirs.append(order['direction'])
                    self.max_wins.append(0.0)
                    self.max_losses.append(0.0)
                    self.current_position = order['direction']
                    self.entry_price = trade_price
                    self.pending_order = None
                    if PRINT_TRADE_OPS:
                        dn = '开多' if order['direction'] == 'rise' else '开空'
                        print(f"[限价单成交] {dn} 第{self.k}次 价格{trade_price:.3f} "
                              f"短周期:{order['direction']} 长周期:{order.get('long_dir','')} "
                              f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                              f"混合:{self.mix_money:.0f} {self.data.datetime.datetime(0)}")

                elif self.modify_flag != order['direction']:
                    # === 信号反向撤单 ===
                    # 挂单期间短周期信号已翻转到相反方向，该开仓前提已不成立，撤销未成交挂单
                    self.entry_orders_cancelled += 1
                    self.pending_order = None
                    if PRINT_TRADE_OPS:
                        print(f"[限价单撤销] 开仓单信号反向 价格{order_price:.3f} "
                              f"原方向:{order['direction']} 新信号:{self.modify_flag} "
                              f"{self.data.datetime.datetime(0)}")

                elif bars_elapsed >= self.limit_order_ttl:
                    # === 开仓限价单过期撤单 ===
                    self.entry_orders_expired += 1
                    self.pending_order = None
                    if PRINT_TRADE_OPS:
                        print(f"[限价单过期] 开仓单未成交 价格{order_price:.3f} "
                              f"已等待{bars_elapsed}个bar {self.data.datetime.datetime(0)}")
                # else: 继续等待，不做任何操作

            # -------------------------------------------------------
            # Step 2: 生成新信号 → 平仓直接成交 / 开仓提交限价单
            # -------------------------------------------------------
            if self.pending_order is None:
                # 情况A：【平仓】有持仓 + 双周期不再共振 → 直接以open价平仓
                # 触发任一离场条件（二选一）：
                #   ① 短周期信号刚发生反转且与长周期不一致（signal_changed 且 not directions_match）
                #   ② 长周期方向翻转到持仓的反方向（long_dir_against，即使短周期信号未变）
                long_dir_against = (bar_long_dir is not None and self.current_position != bar_long_dir)
                if self.current_position is not None and (
                    ((not directions_match) and signal_changed) or long_dir_against
                ):
                    trade_price = cur_open  # 直接以当前K线open价成交
                    # 计算盈亏
                    if self.current_position == 'rise':
                        profit = ((trade_price / self.entry_price) - 1) * 100 * self.times
                    else:
                        profit = (1 - (trade_price / self.entry_price)) * 100 * self.times
                    # 更新资金
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
                    self.profits.append(profit)
                    self.trades.append(trade_price)
                    self.trade_times.append(self.data.datetime.datetime(0))
                    if PRINT_TRADE_OPS:
                        dn = '平多' if self.current_position == 'rise' else '平空'
                        print(f"[直接平仓] {dn}(方向反转) 第{self.k}次 价格{trade_price:.3f} "
                              f"持仓方向:{self.current_position} 盈亏:{profit:.1f}% "
                              f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                              f"混合:{self.mix_money:.0f} {self.data.datetime.datetime(0)}")
                    self.last_position_type = self.current_position
                    self.current_position = None
                    self.entry_price = None
                    self.total_exits += 1

                # 情况B：【开仓】无持仓 + 双周期一致 + 信号改变 → 提交开仓限价单
                # 前置条件：① current_position == None（必须空仓）
                #              ② 短周期方向与长周期一致（directions_match == True）
                #              ③ 短周期信号刚刚发生变化（signal_changed == True）
                elif self.current_position is None and directions_match and signal_changed:
                    self.total_signals += 1
                    entry_price = cur_open  # 以当前K线open价作为限价单价格
                    self.entry_orders_submitted += 1
                    self.pending_order = {
                        'type': 'entry',
                        'price': entry_price,
                        'submit_bar': current_bar_idx,
                        'direction': self.modify_flag,
                        'long_dir': bar_long_dir,
                    }
                    if PRINT_TRADE_OPS:
                        dn = '开多' if self.modify_flag == 'rise' else '开空'
                        print(f"[提交开仓限价单] {dn} 价格{entry_price:.3f} "
                              f"ADX={adx_value:.1f} 权重({hist_weight:.3f}/{curr_weight:.3f}) "
                              f"短周期:{self.modify_flag} 长周期:{bar_long_dir} "
                              f"有效期:{self.limit_order_ttl}bar "
                              f"{self.data.datetime.datetime(0)}")

                # 情况C：【过滤】无持仓 + 方向不一致 + 信号改变 → 不操作，记录过滤
                elif self.current_position is None and not directions_match and signal_changed:
                    self.filtered_signals += 1
                    if PRINT_TRADE_OPS:
                        print(f"过滤信号: 短周期{self.modify_flag}但长周期{bar_long_dir} (无持仓) {self.data.datetime.datetime(0)}")

            # -------------------------------------------------------
            # Step 3: 更新持仓盈亏显示
            # -------------------------------------------------------
            current_profit_display = 0.0
            max_win_display = 0.0
            max_loss_display = 0.0
            
            if self.current_position is not None and self.entry_price is not None:
                current_price = self.data.close[0]
                
                # 根据当前持仓方向计算盈亏
                if self.current_position == 'rise':  # 多头持仓
                    current_profit = ((current_price / self.entry_price) - 1) * 100 * self.times
                else:  # 空头持仓
                    current_profit = (1 - (current_price / self.entry_price)) * 100 * self.times
                
                # 更新当前持仓的最大盈利和最大亏损
                current_trade_idx = len(self.profits)
                if current_trade_idx < len(self.max_wins):
                    self.max_wins[current_trade_idx] = max(self.max_wins[current_trade_idx], max(current_profit, 0))
                    self.max_losses[current_trade_idx] = min(self.max_losses[current_trade_idx], min(current_profit, 0))
                    max_win_display = self.max_wins[current_trade_idx]
                    max_loss_display = self.max_losses[current_trade_idx]
                
                current_profit_display = current_profit

            self.last_modify_flag = self.modify_flag
            
            if PRINT_MARKET:
                o = float(self.data.open[0])
                h = float(self.data.high[0])
                l = float(self.data.low[0])
                c = float(self.data.close[0])
                ts = self.data.datetime.datetime(0)
                hz = df.loc[ts, 'HISTOGRAM_ZERO'] if 'HISTOGRAM_ZERO' in df.columns else float('nan')
                hsdz = df.loc[ts, 'HIST_SMOOTH_DIFF_ZERO'] if 'HIST_SMOOTH_DIFF_ZERO' in df.columns else float('nan')
                print(f"{len(self)}. Modify: {self.modify_flag} ADX: {adx_value:.2f} Weight: {hist_weight:.3f}/{curr_weight:.3f} OHLC:{o:.3f}/{h:.3f}/{l:.3f}/{c:.3f} macd:{float(histogram):.3f} dif:{float(macd_value):.3f} macd平滑差:{float(hist_smooth_diff):.3f} 临界值(macd=0):{float(hz):.3f} 临界值(平滑差=0):{float(hsdz):.3f} {ts}")
                if self.current_position is not None:
                    pos_label = ('多头' if self.current_position == 'rise' else '空头')
                    ep = float(self.entry_price) if self.entry_price is not None else float(self.trades[-1])
                    cp = float(current_profit_display)
                    mw = float(max_win_display)
                    ml = float(max_loss_display)
                    cp_txt = ("+" + f"{cp:.2f}" if cp >= 0 else f"{cp:.2f}")
                    mw_txt = ("+" + f"{mw:.2f}" if mw >= 0 else f"{mw:.2f}")
                    print(f"持仓状态: {pos_label} | 开仓价: {ep:.3f} | 当前盈亏: {cp_txt}% | 最大盈利: {mw_txt}% | 最大亏损: {ml:.2f}%")

        def stop(self):
            """策略结束统计"""
            total_trades = len(self.profits)
            winning_trades = sum(1 for p in self.profits if p > 0)
            losing_trades = sum(1 for p in self.profits if p < 0)
            win_rate = winning_trades / total_trades if total_trades > 0 else 0
            
            total_profit = sum(p for p in self.profits if p > 0)
            total_loss = sum(p for p in self.profits if p < 0)
            avg_profit = total_profit / winning_trades if winning_trades > 0 else 0
            avg_loss = total_loss / losing_trades if losing_trades > 0 else 0
            
            # 最大单笔盈利应该从profits中取（最终盈利）
            max_profit = max(self.profits) if self.profits else 0
            max_loss = min(self.profits) if self.profits else 0
            
            # 过程中的最大盈利和亏损（从max_wins/max_losses中取，只取开仓位置的值）
            max_win_during_trade = max([self.max_wins[i] for i in range(len(self.profits)) if i < len(self.max_wins)]) if self.profits and self.max_wins else 0
            max_loss_during_trade = min([self.max_losses[i] for i in range(len(self.profits)) if i < len(self.max_losses)]) if self.profits and self.max_losses else 0
            
            profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0
            
            # 计算最大回撤 - 使用复投资金曲线（更稳定可靠）
            if len(self.profits) > 0:
                # 使用复投资金曲线计算回撤（避免混合投资的异常base值）
                capital_curve = [self.base_money]
                current_capital = self.base_money
                
                for profit in self.profits:
                    # 复投模式：当前资金 * (1 + 收益率%)
                    current_capital = current_capital * (1 + profit * 0.01)
                    capital_curve.append(current_capital)
                
                # 计算回撤：从峰值到谷底的最大跌幅
                peak = capital_curve[0]
                max_drawdown = 0
                
                for value in capital_curve:
                    if value > peak:
                        peak = value
                    drawdown = (peak - value) / peak * 100
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown
            else:
                max_drawdown = 0
            
            # 计算夏普比率 - 针对杠杆交易优化
            returns = pd.Series(self.profits)
            sharpe_ratio = returns.mean() / returns.std() if len(returns) > 0 and returns.std() > 0 else 0
            
            # 权重统计
            avg_hist_weight = np.mean([w[0] for w in self.weight_history]) if self.weight_history else 0
            avg_curr_weight = np.mean([w[1] for w in self.weight_history]) if self.weight_history else 0

            # 资金曲线列表（在此初始化，确保无成交交易时后续回撤统计不会因未定义而崩溃）
            fix_money_history = [self.base_money]
            contract_money_history = [self.base_money]
            mix_money_history = [self.base_money]

            # 详细交易记录输出
            if len(self.profits) > 0:
                if PRINT_TRADE_RECORDS:
                    print("\n=== 交易记录 ===")
                
                for profit in self.profits:
                    fix_money_current = fix_money_history[-1] + self.base_money * profit * 0.01
                    fix_money_history.append(fix_money_current)
                    
                    contract_money_current = contract_money_history[-1] + contract_money_history[-1] * profit * 0.01
                    contract_money_history.append(contract_money_current)
                    
                    ratio = mix_money_history[-1] / self.base_money
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
                    
                    mix_money_current = mix_money_history[-1] + base * profit * 0.01
                    mix_money_history.append(mix_money_current)
                
                for i in range(len(self.profits)):
                    current_profit = self.profits[i]
                    # trades数组：[开1, 平1, 开2, 平2, ...]
                    # 第i次交易（从0开始）：开仓价=trades[i*2]，平仓价=trades[i*2+1]
                    open_price = self.trades[i*2] if i*2 < len(self.trades) else 0
                    close_price = self.trades[i*2+1] if i*2+1 < len(self.trades) else 0
                    
                    # max_wins/max_losses索引与profits一致（每次交易一个索引）
                    if i < len(self.max_wins):
                        max_win = self.max_wins[i]
                        max_loss = self.max_losses[i]
                    else:
                        max_win = 0.0
                        max_loss = 0.0
                    
                    fix_after = fix_money_history[i+1]
                    contract_after = contract_money_history[i+1]
                    mix_after = mix_money_history[i+1]
                    
                    fix_change = fix_after - fix_money_history[i]
                    contract_change = contract_after - contract_money_history[i]
                    mix_change = mix_after - mix_money_history[i]
                    
                    if PRINT_TRADE_RECORDS:
                        dir_flag = (self.trade_dirs[i] if i < len(self.trade_dirs) and self.trade_dirs[i] is not None else self.long_direction)
                        open_label = ('开多' if str(dir_flag) == 'rise' else '开空')
                        close_label = ('平多' if str(dir_flag) == 'rise' else '平空')
                        print(f"第{i+1}次交易盈利\t"
                              f"{open_label}:{open_price:.3f} {close_label}:{close_price:.3f}\t"
                              f"最终盈利:{current_profit:.2f}%\t"
                              f"最大盈利:{max_win:.2f}%\t"
                              f"最大亏损:{max_loss:.2f}%\t"
                              f"定投:{fix_money_history[i]:.0f}→{fix_after:.0f}({fix_change:+.0f})\t"
                              f"复投:{contract_money_history[i]:.0f}→{contract_after:.0f}({contract_change:+.0f})\t"
                              f"混投:{mix_money_history[i]:.0f}→{mix_after:.0f}({mix_change:+.0f})")

            # 计算新的统计指标
            global_max_loss = min(self.profits) if self.profits else 0
            win_indices = [i for i, p in enumerate(self.profits) if p > 0]
            loss_indices = [i for i, p in enumerate(self.profits) if p < 0]
            hold_total_max_profit = sum(self.max_wins[i] for i in win_indices) if win_indices else 0.0
            hold_total_max_loss = sum(self.max_losses[i] for i in loss_indices) if loss_indices else 0.0
            actual_profit_rate = total_profit / hold_total_max_profit if hold_total_max_profit > 0 else 0
            actual_loss_rate = abs(total_loss) / abs(hold_total_max_loss) if hold_total_max_loss < 0 else 0
            pos_count = len(win_indices)
            neg_count = len(loss_indices)
            avg_hold_max_profit = (hold_total_max_profit / pos_count) if pos_count > 0 else 0
            avg_hold_max_loss = (hold_total_max_loss / neg_count) if neg_count > 0 else 0
            hold_pl_ratio = (abs(avg_hold_max_profit / avg_hold_max_loss) if avg_hold_max_loss != 0 else 0)
            overall_pl_ratio = (abs(total_profit / total_loss) if total_loss < 0 else 0)
            
            # 计算三种投资方式的最大回撤率
            reinvest_drawdown = max_drawdown
            
            fix_drawdown = 0
            if len(fix_money_history) > 1:
                fix_peak = fix_money_history[0]
                for value in fix_money_history:
                    if value > fix_peak:
                        fix_peak = value
                    if fix_peak > 0:
                        drawdown = (fix_peak - value) / fix_peak * 100
                        if drawdown > fix_drawdown:
                            fix_drawdown = drawdown
            
            mix_drawdown = 0
            if len(mix_money_history) > 1:
                mix_peak = mix_money_history[0]
                for value in mix_money_history:
                    if value > mix_peak:
                        mix_peak = value
                    if mix_peak > 0:
                        drawdown = (mix_peak - value) / mix_peak * 100
                        if drawdown > mix_drawdown:
                            mix_drawdown = drawdown
            
            # 年化收益：基于实际回测天数做几何年化（365天/年）
            # 复投资金为正且跨度天数>0才计算；爆仓（≤0）时视为 -100%，避免负数分数次幂产生复数
            annual_return = 0
            if len(self.profits) > 0 and len(self.trade_times) >= 2:
                capital_ratio = self.contract_money / self.base_money
                span_days = (self.trade_times[-1] - self.trade_times[0]).days
                if capital_ratio <= 0:
                    annual_return = -100.0
                elif span_days > 0:
                    annual_return = (capital_ratio ** (365.0 / span_days) - 1) * 100
            calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0
            profit_factor = abs(total_profit / total_loss) if total_loss < 0 else 0

            # 时间统计
            start_time = self.trade_times[0] if len(self.trade_times) > 0 else None
            end_time = self.trade_times[-1] if len(self.trade_times) > 0 else None
            total_days = 0
            daily_profit = 0.0
            weekly_profit = 0.0
            
            if start_time and end_time:
                time_delta = end_time - start_time
                total_days = time_delta.days
                if total_days > 0:
                    # 使用定投收益计算每日和每周盈利（定投收益是线性增长，更合理）
                    total_return = (self.fix_money - self.base_money) / self.base_money * 100
                    daily_profit = total_return / total_days
                    weekly_profit = daily_profit * 7

            if PRINT_ORIGINAL_OUTPUT:
                print("\nPro3版：MACD平滑+ADX自适应 + 双周期 + 开仓限价单+平仓市价 - 交易统计")
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
                print(f"复投最终收益: {self.contract_money:.2f}")
                print(f"定投最终收益: {self.fix_money:.2f}")
                print(f"混合最终收益: {self.mix_money:.2f}")
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
                hold_sum_pl_ratio = (abs(hold_total_max_profit) / abs(hold_total_max_loss) if hold_total_max_loss != 0 else 0.0)
                print(f"持仓最大盈亏均值比: {hold_pl_ratio:.2f}")
                print(f"持仓最大盈亏总和比: {hold_sum_pl_ratio:.2f}")
                print("\n盈亏到手率（最终盈亏与最大盈亏比值）")
                print(f"盈利到手率: {actual_profit_rate:.2%}")
                print(f"亏损到手率: {actual_loss_rate:.2%}")
                print("\n风险与绩效指标")
                print(f"复投最大回撤率: {reinvest_drawdown:.2f}%")
                print(f"定投最大回撤率: {fix_drawdown:.2f}%")
                print(f"混合最大回撤率: {mix_drawdown:.2f}%")
                print(f"卡玛比率: {calmar_ratio:.2f}")
                print(f"盈利因子: {profit_factor:.2f}")
                print("\n自适应权重统计:")
                print(f"平均历史权重: {avg_hist_weight:.3f} (原策略: 0.800)")
                print(f"平均当前权重: {avg_curr_weight:.3f} (原策略: 0.200)")
                print(f"\n双周期信号统计:")
                print(f"信号翻转总次数: {self.total_signal_flips}")
                print(f"开仓信号触发(提交挂单): {self.total_signals}")
                print(f"实际开仓成交: {self.entry_orders_filled}")
                print(f"平仓次数: {self.total_exits}")
                print(f"空仓被过滤: {self.filtered_signals}")
                entry_trigger_rate = (self.total_signals / self.total_signal_flips * 100) if self.total_signal_flips > 0 else 0
                print(f"开仓触发率: {entry_trigger_rate:.1f}% (提交挂单次数/信号翻转总数)")
                print("="*80)

                # === 开仓限价单统计 ===
                entry_fill_rate = (self.entry_orders_filled / self.entry_orders_submitted * 100) if self.entry_orders_submitted > 0 else 0

                print("\n开仓限价单执行统计")
                print("-"*80)
                print(f"限价单有效期: {self.limit_order_ttl}个bar (每个{CURRENT_SHORT_BAR})")
                print(f"开仓限价单: 提交{self.entry_orders_submitted}次 | 成交{self.entry_orders_filled}次 | 过期{self.entry_orders_expired}次 | 撤销(信号反向){self.entry_orders_cancelled}次 | 成功率{entry_fill_rate:.1f}%")
                print(f"平仓方式: 信号反转时直接以open价成交（无限价单）")
                print(f"完整交易次数: {total_trades} (开仓成交+平仓成交配对)")
                if total_trades > 0:
                    print(f"交易胜率: {win_rate:.2%}")
                    print(f"盈亏比(均值): {profit_loss_ratio:.2f}")
                    print(f"最大回撤(复投): {max_drawdown:.2f}%")
                print("="*80)

            print("="*80)

    # 运行主策略并返回最新数据、最后一次交易数据、ATR与长周期方向
    cerebro = bt.Cerebro()  # type: ignore
    data = bt.feeds.PandasData(dataname=df)  # type: ignore
    cerebro.adddata(data)  # type: ignore
    cerebro.addstrategy(DualTimeframeStrategy)  # type: ignore
    cerebro.run()  # type: ignore

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
    return latest_data, last_trade_data, atr_value, long_direction, df, df_long

def run_with_csv(
    symbol='near',
    calc_full_flag='Y',
    short_bar='1H',
    long_bar='1D',
    show_original_output=1,
    show_market_output=0,
    show_trade_ops_output=0,
    show_trade_records_output=0,
):
    """运行双周期策略回测"""
    global GLOBAL_DF_1M
    global PRINT_MARKET, PRINT_TRADE_OPS, PRINT_TRADE_RECORDS, PRINT_ORIGINAL_OUTPUT
    
    GLOBAL_DF_1M = load_symbol_csv(symbol, calc_full_flag)
    PRINT_MARKET = 1 if show_market_output else 0
    PRINT_TRADE_OPS = 1 if show_trade_ops_output else 0
    PRINT_TRADE_RECORDS = 1 if show_trade_records_output else 0
    PRINT_ORIGINAL_OUTPUT = 1 if show_original_output else 0

    instId = f"{str(symbol).upper()}-USDT-SWAP"
    latest_data, last_trade_data, atr, long_direction, df, df_long = get_latest_data(instId, short_bar, long_bar)

    if PRINT_ORIGINAL_OUTPUT:
        print(f"\n=== 方案Pro3双周期+开仓限价单+平仓市价版本: ADX自适应平滑系统 + 双周期共振 ===")
        print(f"当前长周期方向: {long_direction}")
        print("最新行情时间：", latest_data.name)
        print("ATR值:", atr)

def main():
    """主函数 - 示例用法"""
    instId = "NEAR-USDT-SWAP"
    short_bar = "15m"
    long_bar = "4H"
    show_original_output = 1          # 统计输出：1输出，0不输出
    show_market_output = 0            # 行情输出：1输出，0不输出
    show_trade_ops_output = 0         # 交易操作与决策输出：1输出，0不输出
    show_trade_records_output = 0        # 交易记录输出：1输出，0不输出
    
    symbol = instId.split('-')[0].lower()
    run_with_csv(
        symbol=symbol,
        calc_full_flag='Y',
        short_bar=short_bar,
        long_bar=long_bar,
        show_original_output=show_original_output,
        show_market_output=show_market_output,
        show_trade_ops_output=show_trade_ops_output,
        show_trade_records_output=show_trade_records_output,
    )

if __name__ == "__main__":
    main()
