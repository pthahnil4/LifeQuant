import datetime
import pandas as pd
import numpy as np
import backtrader as bt
from okx import MarketData
import math

'''
方案Pro3: ADX自适应平滑系数系统
=================================

核心思想：
不干预交易逻辑，只调整smoothed_histogram的计算权重
- 原策略固定权重：0.8/0.2
- 改为自适应：根据ADX动态调整

自适应规则：
- ADX越大 → 权重越大 → 更保守（减少噪音）
- ADX越小 → 权重越小 → 更敏感（快速反应）

权重计算公式：
hist_weight = base_weight + (ADX / 200)
- ADX=0:  权重0.70 (更敏感)
- ADX=20: 权重0.80 (原策略)
- ADX=40: 权重0.90 (更保守)
- ADX=60: 权重1.00 (极保守，上限)

理论优势：
1. 在强趋势中减少噪音和假信号
2. 在震荡市中保持灵敏度
3. 完全不干预交易逻辑
4. 参数自适应，无需人工调整

预期效果：
- 强趋势中信号更稳定
- 震荡市中反应更快
- 整体提升信号质量
'''

from api_config import get_api_config, validate_config

config = get_api_config()
is_valid, message = validate_config(config)
if not is_valid:
    raise ValueError(f"API配置错误: {message}")

flag = config['flag']
marketDataAPI = MarketData.MarketAPI(flag=flag)

# 全局变量用于存储交易对和时间周期信息
CURRENT_INSTID = ""
CURRENT_BAR = ""

PRINT_MARKET = 0
PRINT_TRADE_OPS = 0
PRINT_TRADE_RECORDS = 0

def calculate_adx(df, period=7):
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
    # 权重计算：base_weight + (ADX / 200)
    # ADX=0:  0.70
    # ADX=20: 0.80
    # ADX=40: 0.90
    # ADX=60: 1.00 (上限)
    hist_weight = min(base_weight + (adx_value / 200.0), 1.0)
    curr_weight = 1.0 - hist_weight
    
    return hist_weight, curr_weight

def get_latest_data(instId, bar):
    """获取数据并执行策略"""
    global CURRENT_INSTID, CURRENT_BAR
    
    # 设置全局变量
    CURRENT_INSTID = instId
    CURRENT_BAR = bar
    
    result = marketDataAPI.get_mark_price_candlesticks(instId=instId, bar=bar)
    result = result['data']
    afterts = result[-1][0]

    combined_data = result
    for _ in range(4):
        result = marketDataAPI.get_mark_price_candlesticks(instId=instId, bar=bar, after=afterts)['data']
        combined_data.extend(result)
        afterts = result[-1][0]

    data = combined_data
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close','confirm'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int) / 1000, unit='s')
    df = df.iloc[::-1]
    df['timestamp'] = df['timestamp'] + pd.Timedelta(hours=8)
    df.set_index('timestamp', inplace=True)
    df = df.astype(float)
    
    df = calculate_adx(df, period=7)

    class MyStrategy(bt.Strategy):
        """方案Pro3: ADX自适应平滑系数系统"""
        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)
            self.ema12 = bt.indicators.EMA(self.data.close, period=12)
            self.ema26 = bt.indicators.EMA(self.data.close, period=26)
            self.atr = bt.indicators.ATR(self.data, period=7)
            
            self.smoothed_hist = []
            self.weight_history = []  # 记录权重历史
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
            self.trade_directions = []
            self.profits = []
            self.max_wins = []
            self.max_losses = []
            self.profit_retracements = []  # 记录每次交易的利润回撤 (最大盈利 - 最终盈利)
            self.entry_adx = []

        def next(self):
            """策略主逻辑 - 使用自适应平滑权重"""
            current_idx = len(self) - 1
            adx_value = df.iloc[current_idx]['ADX'] if current_idx < len(df) else 0
            
            # 计算MACD
            macd_value = self.macd.macd[0]
            signal_value = self.macd.signal[0]
            histogram = 2 * (macd_value - signal_value)
            
            # === 核心创新：ADX自适应平滑系数 ===
            hist_weight, curr_weight = get_adaptive_smooth_weight(adx_value)
            self.weight_history.append((hist_weight, curr_weight))
            
            if len(self.smoothed_hist) == 0:
                smoothed_histogram = histogram
            else:
                # 使用自适应权重计算平滑值
                smoothed_histogram = (self.smoothed_hist[-1] * hist_weight) + (histogram * curr_weight)
            self.smoothed_hist.append(smoothed_histogram)
            
            hist_smooth_diff = histogram - smoothed_histogram
            
            # 计算零点价格（使用自适应权重）
            histogram_zero = 0.0
            hist_smooth_diff_zero = 0.0
            if len(self.data) > 1:
                prev_dea = self.macd.signal[-1]
                prev_ema12 = self.ema12[-1]
                prev_ema26 = self.ema26[-1]
                prev_smoothed = self.smoothed_hist[-2] if len(self.smoothed_hist) > 1 else 0
                
                histogram_zero = (prev_dea - (prev_ema12 * 11.0/13.0) + (prev_ema26 * 25.0/27.0)) / (2.0/13.0 - 2.0/27.0)
                
                # 使用当前的自适应权重计算零点价格
                # 修正：计算 hist_smooth_diff = 0 时的价格 (即 histogram = prev_smoothed)
                # histogram = 2 * (MACD - DEA) => prev_smoothed = 2 * (MACD - DEA)
                # MACD = DEA + 0.5 * prev_smoothed
                hist_smooth_diff_zero = (prev_dea - (prev_ema12 * 11.0/13.0) + (prev_ema26 * 25.0/27.0) + (prev_smoothed * 0.5)) / (2.0/13.0 - 2.0/27.0)
                
                df.loc[self.data.datetime.datetime(0), 'HISTOGRAM_ZERO'] = histogram_zero
                df.loc[self.data.datetime.datetime(0), 'HIST_SMOOTH_DIFF_ZERO'] = hist_smooth_diff_zero

            # 原始信号判断（完全保留）
            if histogram > smoothed_histogram and self.previous_diff < 0:
                self.last_trade_index = len(self) - 1
                self.original_flag = 'rise'
            elif histogram < smoothed_histogram and self.previous_diff > 0:
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

            # modify_flag计算（完全保留）
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

            df.loc[self.data.datetime.datetime(0), 'MODIFY_FLAG'] = self.modify_flag

            # 交易逻辑（完全保留）
            if self.modify_flag == "rise" and getattr(self, 'last_modify_flag', None) == "fall":
                self.k += 1
                
                if self.dealjustice_flag == "waitRise":
                    trade_price = df.loc[self.data.datetime.datetime(-1), 'HISTOGRAM_ZERO']
                else:
                    trade_price = df.loc[self.data.datetime.datetime(-1), 'HIST_SMOOTH_DIFF_ZERO']
                    
                if trade_price > self.data.high[0] or trade_price < self.data.low[0]:
                    if PRINT_TRADE_OPS:
                        print(f"Warning: Price {trade_price:.3f} out of range [{self.data.low[0]:.3f}, {self.data.high[0]:.3f}], using Open {self.data.open[0]:.3f}")
                    self.error += 1
                    trade_price = self.data.open[0]
                
                df.loc[self.data.datetime.datetime(0), 'TRADE_PRICE'] = trade_price

                profit = 0.0
                retracement = 0.0
                if self.k > 1:
                    # 平仓，计算本次交易盈利（空头平仓）
                    profit = (1 - (trade_price / self.trades[-1])) * 100 * self.times
                    
                    # 用平仓价格更新上一次持仓的最大盈亏
                    if profit > 0:
                        self.max_wins[-1] = max(self.max_wins[-1], profit)
                    else:
                        self.max_losses[-1] = min(self.max_losses[-1], profit)
                    
                    # 计算利润回撤
                    retracement = self.max_wins[-1] - profit
                    self.profit_retracements.append(retracement)
                    
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
                self.trade_directions.append('Long')
                self.entry_adx.append(adx_value)
                self.max_wins.append(0.0)
                self.max_losses.append(0.0)
                
                if PRINT_TRADE_OPS:
                    print(f"开多 第{self.k}次 价格{trade_price:.3f} "
                      f"ADX={adx_value:.1f} 权重({hist_weight:.3f}/{curr_weight:.3f}) "
                      f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                      f"混合:{self.mix_money:.0f} 交易:{profit:.1f}% 回撤:{retracement:.1f}% "
                      f"{self.data.datetime.datetime(0)}")

            elif self.modify_flag == "fall" and getattr(self, 'last_modify_flag', None) == "rise":
                self.k += 1
                
                if self.dealjustice_flag == "waitFall":
                    trade_price = df.loc[self.data.datetime.datetime(-1), 'HISTOGRAM_ZERO']
                else:
                    trade_price = df.loc[self.data.datetime.datetime(-1), 'HIST_SMOOTH_DIFF_ZERO']

                if trade_price > self.data.high[0] or trade_price < self.data.low[0]:
                    if PRINT_TRADE_OPS:
                        print(f"Warning: Price {trade_price:.3f} out of range [{self.data.low[0]:.3f}, {self.data.high[0]:.3f}], using Open {self.data.open[0]:.3f}")
                    self.error += 1
                    trade_price = self.data.open[0]
                
                df.loc[self.data.datetime.datetime(0), 'TRADE_PRICE'] = trade_price

                profit = 0.0
                retracement = 0.0
                if self.k > 1:
                    # 平仓，计算本次交易盈利（多头平仓）
                    profit = ((trade_price / self.trades[-1]) - 1) * 100 * self.times
                    
                    # 用平仓价格更新上一次持仓的最大盈亏
                    if profit > 0:
                        self.max_wins[-1] = max(self.max_wins[-1], profit)
                    else:
                        self.max_losses[-1] = min(self.max_losses[-1], profit)
                    
                    # 计算利润回撤
                    retracement = self.max_wins[-1] - profit
                    self.profit_retracements.append(retracement)
                    
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
                self.trade_directions.append('Short')
                self.entry_adx.append(adx_value)
                self.max_wins.append(0.0)
                self.max_losses.append(0.0)
                
                if PRINT_TRADE_OPS:
                    print(f"开空 第{self.k}次 价格{trade_price:.3f} "
                      f"ADX={adx_value:.1f} 权重({hist_weight:.3f}/{curr_weight:.3f}) "
                      f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                      f"混合:{self.mix_money:.0f} 交易:{profit:.1f}% 回撤:{retracement:.1f}% "
                      f"{self.data.datetime.datetime(0)}")

            # 更新持仓盈亏（显示基于收盘价；最大值跟踪改为用当根高/低极值）
            current_profit_display = 0.0
            max_win_display = 0.0
            max_loss_display = 0.0
            
            if len(self.trades) > 0:
                current_price = self.data.close[0]
                last_trade_price = self.trades[-1]
                
                # 根据上一次的交易方向判断当前持仓方向
                current_direction = self.trade_directions[-1]
                if current_direction == 'Long':  # 多头持仓
                    current_profit = ((current_price / last_trade_price) - 1) * 100 * self.times
                    # 使用高低价统计持仓期间的最大盈利/亏损
                    cur_win = ((self.data.high[0] / last_trade_price) - 1) * 100 * self.times
                    cur_loss = ((self.data.low[0] / last_trade_price) - 1) * 100 * self.times
                elif current_direction == 'Short':  # 空头持仓
                    current_profit = (1 - (current_price / last_trade_price)) * 100 * self.times
                    # 使用高低价统计持仓期间的最大盈利/亏损
                    cur_win = (1 - (self.data.low[0] / last_trade_price)) * 100 * self.times
                    cur_loss = (1 - (self.data.high[0] / last_trade_price)) * 100 * self.times
                else:
                    current_profit = 0.0
                    cur_win = 0.0
                    cur_loss = 0.0
                # 更新最大盈利/亏损（按当根极值，而非仅收盘价）
                self.max_wins[-1] = max(self.max_wins[-1], max(0.0, cur_win))
                self.max_losses[-1] = min(self.max_losses[-1], min(0.0, cur_loss))
                
                current_profit_display = current_profit
                max_win_display = self.max_wins[-1]
                max_loss_display = self.max_losses[-1]

            self.last_modify_flag = self.modify_flag
            
            if PRINT_MARKET:
                atr_val = self.atr[0]
                c = self.data.close[0]
                atr_pct = (atr_val / c * 100 * 10) if c > 0 else 0.0
                print(f"{len(self)}. Modify: {self.modify_flag} ADX: {adx_value:.2f} "
                  f"ATR: {atr_pct:.1f}% "
                  f"Open: {self.data.open[0]:.3f} "
                  f"High: {self.data.high[0]:.3f} "
                  f"Low: {self.data.low[0]:.3f} "
                  f"Close: {self.data.close[0]:.3f} "
                  f"MACD: {histogram:.3f} "
                  f"DIF: {macd_value:.3f} "
                  f"histogram: {histogram_zero:.3f} "
                  f"hist_smooth_diff: {hist_smooth_diff_zero:.3f} "
                  f"{self.data.datetime.datetime(0)}")
            
            # 输出当前持仓盈亏情况
            if PRINT_MARKET and len(self.trades) > 0:
                position_type = "多头" if self.last_modify_flag == 'rise' else "空头" if self.last_modify_flag == 'fall' else "无"
                print(f"持仓状态: {position_type} | "
                      f"开仓价: {self.trades[-1]:.3f} | "
                      f"当前盈亏: {current_profit_display:+.2f}% | "
                      f"最大盈利: {max_win_display:+.2f}% | "
                      f"最大亏损: {max_loss_display:+.2f}%")
            
            if PRINT_MARKET:
                print("-" * 100)

        def stop(self):
            """策略结束统计 - 按模板输出并进行期末强制平仓与完善统计"""
            # 期末强制平仓（若仍有持仓）
            # profits 数量通常等于 trades-1；若有持仓，需用最后收盘价计算最后一笔盈利
            try:
                if len(self.trades) > 0 and len(self.profits) < len(self.trades):
                    last_trade_price = self.trades[-1]
                    last_close = float(self.data.close[0])
                    if self.last_modify_flag == 'rise':
                        profit = ((last_close / last_trade_price) - 1) * 100 * self.times
                    elif self.last_modify_flag == 'fall':
                        profit = (1 - (last_close / last_trade_price)) * 100 * self.times
                    else:
                        profit = 0.0
                    # 资金更新与记录
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
            except Exception:
                pass
            # 计算交易统计指标
            total_trades = len(self.profits)
            winning_trades = sum(1 for p in self.profits if p > 0)
            win_rate = winning_trades / total_trades if total_trades > 0 else 0
            
            total_profit = sum(p for p in self.profits if p > 0)
            total_loss = sum(p for p in self.profits if p < 0)
            avg_profit = total_profit / winning_trades if winning_trades > 0 else 0
            avg_loss = total_loss / (total_trades - winning_trades) if (total_trades - winning_trades) > 0 else 0
            
            max_profit = max(self.profits) if self.profits else 0
            global_max_loss = min(self.profits) if self.profits else 0  # 保存全局最大亏损
            profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0
            
            # === 计算平均持仓周期 (执行三次计算：盈利/亏损/总计) ===
            win_durations = []
            loss_durations = []
            total_durations = []

            # 回调分析数据准备 - 临界值模式
            callback_trades_data = []

            for i in range(len(self.profits)):
                if i < len(self.trade_times):
                    entry_time = self.trade_times[i]
                    if i + 1 < len(self.trade_times):
                        exit_time = self.trade_times[i+1]
                    else:
                        exit_time = self.data.datetime.datetime(0)
                    
                    duration_hours = (exit_time - entry_time).total_seconds() / 3600.0
                    profit = self.profits[i]
                    
                    total_durations.append(duration_hours)
                    if profit > 0:
                        win_durations.append(duration_hours)
                    else:
                        loss_durations.append(duration_hours)

                    # 收集回调分析所需数据
                    if i < len(self.trades):
                        entry_price = self.trades[i]
                        direction = self.trade_directions[i] if i < len(self.trade_directions) else 'Unknown'
                        is_win = profit > 0
                        callback_trades_data.append({
                            'entry_time': entry_time,
                            'exit_time': exit_time,
                            'entry_price': entry_price,
                            'direction': direction,
                            'is_win': is_win
                        })

            avg_win_dur = sum(win_durations) / len(win_durations) if win_durations else 0
            avg_loss_dur = sum(loss_durations) / len(loss_durations) if loss_durations else 0
            avg_total_dur = sum(total_durations) / len(total_durations) if total_durations else 0

            # === 回调分析函数 ===
            def analyze_callbacks(mode_name, trades_data, df, N=3):
                """
                分析回调概率和时段
                trades_data: list of dict {'entry_time', 'exit_time', 'entry_price', 'direction', 'is_win'}
                N: 排除初始N个时段（小时）
                """
                win_callbacks = []
                loss_callbacks = []
                win_callback_times = []
                loss_callback_times = []
                
                win_total = 0
                loss_total = 0

                for trade in trades_data:
                    entry_time = trade['entry_time']
                    exit_time = trade['exit_time']
                    entry_price = trade['entry_price']
                    direction = trade['direction'] # 'Long' or 'Short' or 'rise'/'fall' mapping needed?
                    # 统一方向标识: Critical模式用Long/Short, Open/Close模式用rise/fall
                    is_long = direction in ['Long', 'rise']
                    
                    # 确定分析的时间范围：Entry + N hours 到 Exit
                    start_time = entry_time + pd.Timedelta(hours=N)
                    if start_time >= exit_time:
                        # 交易时间太短，无法分析回调（或者视为无回调? 根据需求，排除初始阶段后若无时间则不计入分母? 或计入但不回调?
                        # 这里假设计入分母，标记为未回调，因为确实没有回调发生）
                        # 但如果排除N后没有K线了，说明持仓不足N小时。这种情况下是否应该计入统计？
                        # 用户说“排除每个趋势交易的前N个时段”。如果交易总时长 < N，则完全被排除。
                        continue

                    # 获取价格片段
                    # df index is timestamp. 
                    # 注意: df.loc[start:end] 是包含边界的。
                    # 我们需要严格大于 start_time 的K线? 
                    # start_time 是 entry_time + N小时。
                    # 假设 entry_time 是 10:00 (bar open time)。+3h = 13:00。
                    # 13:00 bar covers 13:00-14:00.
                    # 这样是排除了 10, 11, 12 三根K线。
                    
                    segment = df.loc[start_time:exit_time]
                    if segment.empty:
                        continue
                        
                    has_callback = False
                    callback_time = 0
                    
                    # 遍历查找首次回调
                    for idx, row in segment.iterrows():
                        # idx is timestamp
                        # 检查当前K线的高低范围是否覆盖开仓价
                        low = row['low']
                        high = row['high']
                        
                        if low <= entry_price <= high:
                            has_callback = True
                            # 计算回调所需时段数 (从开仓时间开始算的多少个时段后? 还是从N之后?)
                            # "所需平均时段数" usually means time from Entry to Callback.
                            # 计算时差 (小时)
                            callback_ts = idx
                            time_diff = (callback_ts - entry_time).total_seconds() / 3600.0
                            callback_time = time_diff
                            break
                    
                    if trade['is_win']:
                        win_total += 1
                        if has_callback:
                            win_callbacks.append(callback_time)
                    else:
                        loss_total += 1
                        if has_callback:
                            loss_callbacks.append(callback_time)

                # 统计结果
                win_prob = len(win_callbacks) / win_total if win_total > 0 else 0.0
                loss_prob = len(loss_callbacks) / loss_total if loss_total > 0 else 0.0
                
                avg_win_cb_time = sum(win_callbacks) / len(win_callbacks) if win_callbacks else 0.0
                avg_loss_cb_time = sum(loss_callbacks) / len(loss_callbacks) if loss_callbacks else 0.0
                
                return {
                    'win_prob': win_prob,
                    'loss_prob': loss_prob,
                    'avg_win_cb_time': avg_win_cb_time,
                    'avg_loss_cb_time': avg_loss_cb_time,
                    'win_total': win_total,
                    'loss_total': loss_total,
                    'win_cb_count': len(win_callbacks),
                    'loss_cb_count': len(loss_callbacks)
                }

            # 执行临界值模式回调分析
            cb_stats_critical = analyze_callbacks("临界值模式", callback_trades_data, df, N=1)

            print("\n=== 持仓周期统计 (小时) ===")
            print(f"盈利单平均周期: {avg_win_dur:.2f} 小时 (共{len(win_durations)}笔)")
            print(f"亏损单平均周期: {avg_loss_dur:.2f} 小时 (共{len(loss_durations)}笔)")
            print(f"总平均周期: {avg_total_dur:.2f} 小时 (共{len(total_durations)}笔)")
            
            # 输出临界值模式回调统计
            print(f"\n=== 价格回调至开仓位统计 (排除前1小时) ===")
            print(f"盈利单回调概率: {cb_stats_critical['win_prob']:.2%} ({cb_stats_critical['win_cb_count']}/{cb_stats_critical['win_total']})")
            print(f"  平均回调时段: {cb_stats_critical['avg_win_cb_time']:.1f} 小时")
            print(f"亏损单回调概率: {cb_stats_critical['loss_prob']:.2%} ({cb_stats_critical['loss_cb_count']}/{cb_stats_critical['loss_total']})")
            print(f"  平均回调时段: {cb_stats_critical['avg_loss_cb_time']:.1f} 小时")
            
            # 计算并输出平均利润回撤
            avg_retracement = sum(self.profit_retracements) / len(self.profit_retracements) if self.profit_retracements else 0.0
            print(f"\n=== 利润回撤统计 (Max Profit - Final Profit) ===")
            print(f"平均利润回撤: {avg_retracement:.2f}%")
            
            print("==========================\n")

            # 计算最大回撤 - 使用复投资金曲线（负值保护，回撤上限100%）
            if len(self.profits) > 0:
                # 使用复投资金曲线计算回撤（避免混合投资的异常base值）
                capital_curve = [self.base_money]
                current_capital = self.base_money
                
                for profit in self.profits:
                    # 复投模式：当前资金 * (1 + 收益率%)
                    current_capital = current_capital * (1 + profit * 0.01)
                    # 负值保护：资金曲线不允许低于0
                    if current_capital < 0:
                        current_capital = 0.0
                    capital_curve.append(current_capital)
                
                # 计算回撤：从峰值到谷底的最大跌幅
                peak = capital_curve[0]
                max_drawdown = 0
                
                for value in capital_curve:
                    # 使用非负值计算回撤
                    v = value if value > 0 else 0.0
                    if v > peak:
                        peak = v
                    if peak > 0:
                        drawdown = (peak - v) / peak * 100
                        # 回撤上限100%
                        if drawdown > 100:
                            drawdown = 100.0
                        if drawdown > max_drawdown:
                            max_drawdown = drawdown
            else:
                max_drawdown = 0

            # 详细交易记录输出
            if len(self.profits) > 0:
                if PRINT_TRADE_RECORDS:
                    print("\n=== 交易记录 ===")
                
                # 计算各种投资方式的历史金额
                fix_money_history = [self.base_money]
                contract_money_history = [self.base_money]
                mix_money_history = [self.base_money]
                
                for profit in self.profits:
                    # 定投计算
                    fix_money_current = fix_money_history[-1] + self.base_money * profit * 0.01
                    fix_money_history.append(fix_money_current)
                    
                    # 复投计算
                    contract_money_current = contract_money_history[-1] + contract_money_history[-1] * profit * 0.01
                    contract_money_history.append(contract_money_current)
                    
                    # 混投计算
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
                    # 获取当前交易的盈利和价格
                    current_profit = self.profits[i]  # 第i+1次平仓的盈利
                    # 安全获取平仓价格：优先使用 trades[i+1]；若期末强制平仓导致无 i+1，则退回到最后收盘价
                    if (i + 1) < len(self.trades):
                        current_trade_price = self.trades[i+1]
                    else:
                        current_trade_price = float(self.data.close[0])
                    
                    # 计算这次交易的最大盈利和最大亏损
                    # profits[i]是第i+1次持仓期间的盈利
                    # max_wins[i]是第i+1次持仓期间的最大盈利（从trades[i]开仓到trades[i+1]平仓）
                    if i < len(self.max_wins):  # 确保索引有效
                        max_win = self.max_wins[i]  # 修正：使用i而不是i+1
                        max_loss_current = self.max_losses[i]
                    else:
                        max_win = 0.0
                        max_loss_current = 0.0
                    
                    # 计算到当前交易为止的盈亏次数
                    trades_to_now = self.profits[:i+1]
                    winning_trades = sum(1 for p in trades_to_now if p > 0)
                    losing_trades = sum(1 for p in trades_to_now if p < 0)
                    
                    # 计算达标次数（盈利超过10%或亏损超过10%）
                    qualified_trades = sum(1 for p in trades_to_now if abs(p) > 10)
                    
                    # 获取交易后的资金状态
                    fix_after = fix_money_history[i+1]
                    contract_after = contract_money_history[i+1]
                    mix_after = mix_money_history[i+1]
                    
                    # 计算资金变化
                    fix_change = fix_after - fix_money_history[i]
                    contract_change = contract_after - contract_money_history[i]
                    mix_change = mix_after - mix_money_history[i]

                    # 获取交易方向和时间信息
                    direction_cn = "多头" if i < len(self.trade_directions) and self.trade_directions[i] == 'Long' else "空头"
                    open_time_str = self.trade_times[i].strftime('%Y-%m-%d %H:%M') if i < len(self.trade_times) else "N/A"
                    open_price = self.trades[i] if i < len(self.trades) else 0.0

                    if PRINT_TRADE_RECORDS:
                        print(f"第{i+1}次交易({direction_cn})\t"
                          f"开仓时间：{open_time_str}\t"
                          f"开仓价格：{open_price:.3f}\t"
                          f"平仓价格：{current_trade_price:.3f}\t"
                          f"最终盈利:{current_profit:.1f}%\t"
                          f"最大盈利:{max_win:.2f}%\t"
                          f"最大亏损:{max_loss_current:.1f}%\t"
                          f"定投:{fix_money_history[i]:.0f}→{fix_after:.0f}({fix_change:+.0f})\t"
                        #   f"复投:{contract_money_history[i]:.0f}→{contract_after:.0f}({contract_change:+.0f})\t"
                        #   f"混投:{mix_money_history[i]:.0f}→{mix_after:.0f}({mix_change:+.0f})"
                          )

            print(f"\nPro3版：MACD平滑+ADX自适应 - 交易统计")
            print("=======================================================")
            print(f"交易对: {CURRENT_INSTID}")
            print(f"时间周期: {CURRENT_BAR}")
            print(f"总交易次数: {total_trades}")
            print(f"胜率: {win_rate:.2%}")
            
            # 计算卡玛比率 (Calmar Ratio)
            # 卡玛比率 = 年化收益率 / 最大回撤率
            total_return = (self.contract_money - self.base_money) / self.base_money * 100
            calmar_ratio = total_return / max_drawdown if max_drawdown > 0 else 0
            
            # 计算盈利因子 (Profit Factor)
            # 盈利因子 = 总盈利 / 总亏损的绝对值
            profit_factor = total_profit / abs(total_loss) if total_loss != 0 else 0
            
            # 计算最大盈利/亏损之和（对齐最终胜/负单，仅统计已平仓的持仓）
            closed_count = total_trades
            max_wins_closed = self.max_wins[:closed_count] if self.max_wins else []
            max_losses_closed = self.max_losses[:closed_count] if self.max_losses else []

            winning_indices = [i for i, p in enumerate(self.profits) if p > 0]
            losing_indices = [i for i, p in enumerate(self.profits) if p < 0]

            total_max_profit = sum((max(0.0, max_wins_closed[i]) for i in winning_indices)) if len(winning_indices) > 0 else 0.0
            total_max_loss = sum((min(0.0, max_losses_closed[i]) for i in losing_indices)) if len(losing_indices) > 0 else 0.0
            # 到手率（最终盈亏与最大盈亏比值；盈利用胜单的最大盈利之和，亏损用负单的最大亏损之和）
            actual_profit_rate = (total_profit / total_max_profit * 100.0) if total_max_profit > 0 else 0.0
            actual_loss_rate = (abs(total_loss) / abs(total_max_loss) * 100.0) if total_max_loss < 0 else 0.0
            
            # 计算三种投资方式的最大回撤率
            # 复投回撤率（已计算）
            reinvest_drawdown = max_drawdown
            
            # 定投回撤率（负值保护，回撤上限100%）
            fix_drawdown = 0
            if len(fix_money_history) > 1:
                fix_peak = max(0.0, fix_money_history[0])
                for value in fix_money_history:
                    v = value if value > 0 else 0.0
                    if v > fix_peak:
                        fix_peak = v
                    if fix_peak > 0:
                        drawdown = (fix_peak - v) / fix_peak * 100
                        if drawdown > 100:
                            drawdown = 100.0
                        if drawdown > fix_drawdown:
                            fix_drawdown = drawdown
            
            # 混投回撤率（负值保护，回撤上限100%）
            mix_drawdown = 0
            if len(mix_money_history) > 1:
                mix_peak = max(0.0, mix_money_history[0])
                for value in mix_money_history:
                    v = value if value > 0 else 0.0
                    if v > mix_peak:
                        mix_peak = v
                    if mix_peak > 0:
                        drawdown = (mix_peak - v) / mix_peak * 100
                        if drawdown > 100:
                            drawdown = 100.0
                        if drawdown > mix_drawdown:
                            mix_drawdown = drawdown
            
            print(f"复投最终收益: {self.contract_money:.2f}")
            print(f"定投最终收益: {self.fix_money:.2f}")
            print(f"混合最终收益: {self.mix_money:.2f}")
            print("最终盈亏核心指标")
            print(f"最终盈利总和: {total_profit:.2f}")
            print(f"最终亏损总和: {total_loss:.2f}")
            print(f"最终盈利均值（仅盈利单）: {avg_profit:.2f}")
            print(f"最终亏损均值（仅亏损单）: {avg_loss:.2f}")
            # 辅助函数：格式化比值输出
            def fmt_ratio(val):
                if val == float('inf'): return "∞"
                if val == 0: return "0"
                return f"{val:.4f}"

            # 计算四种盈亏比
            # 1. 最终盈亏均值比
            final_avg_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else (float('inf') if avg_profit != 0 else 0.0)
            # 2. 最终盈亏总和比
            final_total_ratio = abs(total_profit / total_loss) if total_loss != 0 else (float('inf') if total_profit != 0 else 0.0)

            print(f"最终盈亏均值比: {fmt_ratio(final_avg_ratio)}")
            print(f"最终盈亏总和比: {fmt_ratio(final_total_ratio)}")

            print("持仓盈亏核心指标")
            # 计算持仓最大盈利/亏损均值与盈亏比（按最终胜/负单分组）
            pos_max_list = [max_wins_closed[i] for i in winning_indices] if len(winning_indices) > 0 else []
            neg_max_list = [max_losses_closed[i] for i in losing_indices] if len(losing_indices) > 0 else []
            avg_pos_max = (sum(pos_max_list) / len(pos_max_list)) if len(pos_max_list) > 0 else 0.0
            avg_neg_max = (sum(neg_max_list) / len(neg_max_list)) if len(neg_max_list) > 0 else 0.0
            
            # 3. 持仓最大盈亏均值比
            hold_avg_ratio = abs(avg_pos_max / avg_neg_max) if avg_neg_max != 0 else (float('inf') if avg_pos_max != 0 else 0.0)
            # 4. 持仓最大盈亏总和比
            hold_total_ratio = abs(total_max_profit / total_max_loss) if total_max_loss != 0 else (float('inf') if total_max_profit != 0 else 0.0)

            print(f"持仓最大盈利总和: {total_max_profit:.2f}")
            print(f"持仓最大亏损总和: {total_max_loss:.2f}")
            print(f"持仓最大盈利均值（仅盈利单）: {avg_pos_max:.2f}")
            print(f"持仓最大亏损均值（仅亏损单）: {avg_neg_max:.2f}")
            print(f"持仓最大盈亏均值比: {fmt_ratio(hold_avg_ratio)}")
            print(f"持仓最大盈亏总和比: {fmt_ratio(hold_total_ratio)}")
            print("盈亏到手率（最终盈亏与最大盈亏比值）")
            print(f"盈利到手率: {actual_profit_rate:.2f}%")
            print(f"亏损到手率: {actual_loss_rate:.2f}%")
            print("风险与绩效指标")
            print(f"复投最大回撤率: {reinvest_drawdown:.2f}%")
            print(f"定投最大回撤率: {fix_drawdown:.2f}%")
            print(f"混合最大回撤率: {mix_drawdown:.2f}%")
            print(f"卡玛比率: {calmar_ratio:.2f}")
            print(f"盈利因子: {profit_factor:.2f}")
            if len(self.profits) > 0 and len(self.entry_adx) > 0:
                adx_entries = self.entry_adx[:len(self.profits)]
                wins = [adx_entries[i] for i, p in enumerate(self.profits) if p > 0]
                losses = [adx_entries[i] for i, p in enumerate(self.profits) if p < 0]
                adx_mean = float(np.mean(adx_entries)) if len(adx_entries) > 0 else 0.0
                adx_win_mean = float(np.mean(wins)) if len(wins) > 0 else 0.0
                adx_loss_mean = float(np.mean(losses)) if len(losses) > 0 else 0.0
                corr = 0.0
                try:
                    x = np.array(adx_entries, dtype=float)
                    y = np.array(self.profits, dtype=float)
                    if x.size > 1 and y.size > 1 and np.std(x) > 0 and np.std(y) > 0:
                        corr = float(np.corrcoef(x, y)[0, 1])
                except Exception:
                    corr = 0.0
                high_thresh = 25.0
                high_idx = [i for i, a in enumerate(adx_entries) if a >= high_thresh]
                low_idx = [i for i, a in enumerate(adx_entries) if a < high_thresh]
                high_wins = sum(1 for i in high_idx if self.profits[i] > 0)
                low_wins = sum(1 for i in low_idx if self.profits[i] > 0)
                high_rate = (high_wins / len(high_idx)) if len(high_idx) > 0 else 0.0
                low_rate = (low_wins / len(low_idx)) if len(low_idx) > 0 else 0.0
                print("ADX入场统计与相关性")
                print(f"样本数: {len(adx_entries)}")
                print(f"ADX均值: {adx_mean:.2f}")
                print(f"胜单ADX均值: {adx_win_mean:.2f}")
                print(f"亏单ADX均值: {adx_loss_mean:.2f}")
                print(f"ADX-最终盈亏皮尔逊相关: {corr:.3f}")
                print(f"ADX≥{high_thresh:.0f} 样本: {len(high_idx)} 胜率: {high_rate:.2%}")
                print(f"ADX<{high_thresh:.0f} 样本: {len(low_idx)} 胜率: {low_rate:.2%}")
            
            # 权重统计 - 保留Pro3版本的特色信息
            avg_hist_weight = np.mean([w[0] for w in self.weight_history]) if self.weight_history else 0
            avg_curr_weight = np.mean([w[1] for w in self.weight_history]) if self.weight_history else 0
            print(f"\n自适应权重统计:")
            print(f"平均历史权重: {avg_hist_weight:.3f} (原策略: 0.800)")
            print(f"平均当前权重: {avg_curr_weight:.3f} (原策略: 0.200)")

                    # === 新增：开盘价/收盘价两组详细交易统计（同一运行）===
            if len(self.trade_times) >= 2:
                try:
                    # 修正：之前逻辑为 trade_pairs = len(self.trade_times) // 2，导致只统计了不重叠的交易对，丢失了一半交易
                    # 策略为反手策略，每次平仓同时开仓，因此 t[i] -> t[i+1] 为一笔交易
                    # trade_times 记录了所有开仓/反手的时间点
                    num_trades_to_analyze = len(self.trade_times) - 1
                    
                    profits_open = []
                    profits_close = []
                    max_wins_open = []
                    max_losses_open = []
                    max_wins_close = []
                    max_losses_close = []
                    durations_open = []
                    durations_close = []
                    
                    # 回调分析数据准备
                    callback_data_open = []
                    callback_data_close = []
                    
                    # 预计算平均ATR（用于对比分析）
                    if 'high' in df.columns and 'low' in df.columns and 'close' in df.columns:
                        prev_close = df['close'].shift(1)
                        tr1 = df['high'] - df['low']
                        tr2 = (df['high'] - prev_close).abs()
                        tr3 = (df['low'] - prev_close).abs()
                        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                        avg_atr = tr.mean()
                    else:
                        avg_atr = 0.0

                    for i in range(num_trades_to_analyze):
                        open_time = self.trade_times[i]
                        close_time = self.trade_times[i+1]
                        
                        if open_time not in df.index or close_time not in df.index:
                            continue
                        
                        direction = df.loc[open_time, 'MODIFY_FLAG']
                        
                        entry_open = float(df.loc[open_time, 'open'])
                        exit_open = float(df.loc[close_time, 'open'])
                        entry_close = float(df.loc[open_time, 'close'])
                        exit_close = float(df.loc[close_time, 'close'])
                        
                        intr = df.loc[open_time:close_time]['close']
                        max_price = float(intr.max())
                        min_price = float(intr.min())
                        
                        if direction == 'rise':
                            p_open = ((exit_open/entry_open) - 1) * 100 * self.times
                            p_close = ((exit_close/entry_close) - 1) * 100 * self.times
                            mw_open = ((max_price/entry_open) - 1) * 100 * self.times
                            ml_open = ((min_price/entry_open) - 1) * 100 * self.times
                            mw_close = ((max_price/entry_close) - 1) * 100 * self.times
                            ml_close = ((min_price/entry_close) - 1) * 100 * self.times
                        else:
                            p_open = (1 - (exit_open/entry_open)) * 100 * self.times
                            p_close = (1 - (exit_close/entry_close)) * 100 * self.times
                            mw_open = (1 - (min_price/entry_open)) * 100 * self.times
                            ml_open = (1 - (max_price/entry_open)) * 100 * self.times
                            mw_close = (1 - (min_price/entry_close)) * 100 * self.times
                            ml_close = (1 - (max_price/entry_close)) * 100 * self.times
                            
                        profits_open.append(float(p_open))
                        profits_close.append(float(p_close))
                        max_wins_open.append(float(max(mw_open, 0)))
                        max_losses_open.append(float(min(ml_open, 0)))
                        max_wins_close.append(float(max(mw_close, 0)))
                        max_losses_close.append(float(min(ml_close, 0)))
                        
                        duration = (close_time - open_time).total_seconds() / 3600.0
                        durations_open.append(duration)
                        durations_close.append(duration)
                        
                        # 收集回调分析数据
                        callback_data_open.append({
                            'entry_time': open_time,
                            'exit_time': close_time,
                            'entry_price': entry_open,
                            'direction': direction,
                            'is_win': float(p_open) > 0
                        })
                        callback_data_close.append({
                            'entry_time': open_time,
                            'exit_time': close_time,
                            'entry_price': entry_close,
                            'direction': direction,
                            'is_win': float(p_close) > 0
                        })

                    # 统计汇总函数
                    def _summary_from_profits(profits, max_wins, max_losses, durations, cb_stats):
                        total_trades = len(profits)
                        wins = sum(1 for p in profits if p > 0)
                        losses_count = sum(1 for p in profits if p < 0)
                        win_rate = wins / total_trades if total_trades > 0 else 0.0
                        total_profit = sum(p for p in profits if p > 0)
                        total_loss = sum(p for p in profits if p < 0)
                        avg_profit = total_profit / wins if wins > 0 else 0.0
                        avg_loss = total_loss / losses_count if losses_count > 0 else 0.0
                        max_profit = max(profits) if profits else 0.0
                        global_max_loss = min(profits) if profits else 0.0
                        pl_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0.0

                        # 计算持仓周期统计
                        win_durs = [d for p, d in zip(profits, durations) if p > 0]
                        loss_durs = [d for p, d in zip(profits, durations) if p < 0]
                        
                        avg_win_dur = sum(win_durs) / len(win_durs) if win_durs else 0.0
                        avg_loss_dur = sum(loss_durs) / len(loss_durs) if loss_durs else 0.0
                        avg_total_dur = sum(durations) / len(durations) if durations else 0.0
                        
                        # 回撤与三种资金曲线
                        fix_money_history = [self.base_money]
                        contract_money_history = [self.base_money]
                        mix_money_history = [self.base_money]
                        
                        for profit in profits:
                            fix_money_history.append(fix_money_history[-1] + self.base_money * profit * 0.01)
                            contract_money_history.append(contract_money_history[-1] * (1 + profit * 0.01))
                            
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
                            mix_money_history.append(mix_money_history[-1] + base * profit * 0.01)
                            
                        # 最大回撤（复投）
                        capital_curve = contract_money_history
                        peak = capital_curve[0]
                        max_drawdown = 0.0
                        for v in capital_curve:
                            if v > peak:
                                peak = v
                            dd = (peak - v) / peak * 100
                            if dd > max_drawdown:
                                max_drawdown = dd
                                
                        # 定投/混投回撤
                        fix_drawdown = 0.0
                        if len(fix_money_history) > 1:
                            fpeak = fix_money_history[0]
                            for v in fix_money_history:
                                if v > fpeak:
                                    fpeak = v
                                if fpeak > 0:
                                    dd = (fpeak - v) / fpeak * 100
                                    if dd > fix_drawdown:
                                        fix_drawdown = dd
                                        
                        mix_drawdown = 0.0
                        if len(mix_money_history) > 1:
                            mpeak = mix_money_history[0]
                            for v in mix_money_history:
                                if v > mpeak:
                                    mpeak = v
                                if mpeak > 0:
                                    dd = (mpeak - v) / mpeak * 100
                                    if dd > mix_drawdown:
                                        mix_drawdown = dd

                        # 卡玛和因子
                        annual_return = ((contract_money_history[-1] / self.base_money) ** (252 / len(profits)) - 1) * 100 if len(profits) > 0 else 0.0
                        calmar = annual_return / max_drawdown if max_drawdown > 0 else 0.0
                        profit_factor = abs(total_profit / total_loss) if total_loss < 0 else 0.0
                        
                        # 按最终胜/负单分组统计持仓最大盈亏
                        win_indices = [i for i, p in enumerate(profits) if p > 0]
                        loss_indices = [i for i, p in enumerate(profits) if p < 0]
                        total_max_profit = sum(max_wins[i] for i in win_indices) if win_indices else 0.0
                        total_max_loss = sum(max_losses[i] for i in loss_indices) if loss_indices else 0.0
                        
                        actual_profit_rate = (total_profit / total_max_profit) if total_max_profit > 0 else 0.0
                        actual_loss_rate = (abs(total_loss) / abs(total_max_loss)) if total_max_loss < 0 else 0.0
                        
                        # 分组均值与持仓盈亏比
                        pos_count = len(win_indices)
                        neg_count = len(loss_indices)
                        avg_hold_max_profit = (total_max_profit / pos_count) if pos_count > 0 else 0.0
                        avg_hold_max_loss = (total_max_loss / neg_count) if neg_count > 0 else 0.0
                    
                        # 辅助函数：计算比值并格式化
                        def calc_ratio(numerator, denominator):
                            if numerator == 0: return 0.0 # 无盈利单
                            if denominator == 0: return float('inf') # 无亏损单
                            return abs(numerator / denominator)

                        final_avg_ratio = calc_ratio(avg_profit, avg_loss)
                        final_total_ratio = calc_ratio(total_profit, total_loss)
                        
                        # 新增：与最大单笔亏损的比值
                        avg_profit_max_loss_ratio = calc_ratio(avg_profit, global_max_loss)
                        total_profit_max_loss_ratio = calc_ratio(total_profit, global_max_loss)
                        
                        hold_avg_ratio = calc_ratio(avg_hold_max_profit, avg_hold_max_loss)
                        hold_total_ratio = calc_ratio(total_max_profit, total_max_loss)
                    
                        return {
                            'total_trades': total_trades,
                            'win_rate': win_rate,
                            'total_profit': total_profit,
                            'total_loss': total_loss,
                            'avg_profit': avg_profit,
                            'avg_loss': avg_loss,
                            'max_profit': max_profit,
                            'global_max_loss': global_max_loss,
                            'final_avg_ratio': final_avg_ratio,
                            'final_total_ratio': final_total_ratio,
                            'avg_profit_max_loss_ratio': avg_profit_max_loss_ratio,
                            'total_profit_max_loss_ratio': total_profit_max_loss_ratio,
                            'fix_money_history': fix_money_history,
                            'contract_money_history': contract_money_history,
                            'mix_money_history': mix_money_history,
                            'max_drawdown': max_drawdown,
                            'fix_drawdown': fix_drawdown,
                            'mix_drawdown': mix_drawdown,
                            'annual_return': annual_return,
                            'calmar': calmar,
                            'profit_factor': final_total_ratio, # Same as final_total_ratio
                            'total_max_profit': total_max_profit,
                            'total_max_loss': total_max_loss,
                            'avg_hold_max_profit': avg_hold_max_profit,
                            'avg_hold_max_loss': avg_hold_max_loss,
                            'hold_avg_ratio': hold_avg_ratio,
                            'hold_total_ratio': hold_total_ratio,
                            'actual_profit_rate': actual_profit_rate,
                            'actual_loss_rate': actual_loss_rate,
                            'total_return': (contract_money_history[-1] - self.base_money) / self.base_money * 100,
                            'trades_count': total_trades,
                            'avg_profit_val': total_profit / total_trades if total_trades > 0 else 0.0,
                            'avg_win_dur': avg_win_dur,
                            'avg_loss_dur': avg_loss_dur,
                            'avg_total_dur': avg_total_dur,
                            'win_durs_count': len(win_durs),
                            'loss_durs_count': len(loss_durs),
                            'total_durs_count': len(durations),
                            'cb_stats': cb_stats
                        }

                    # 执行回调分析
                    cb_stats_open = analyze_callbacks("开盘价模式", callback_data_open, df, N=1)
                    cb_stats_close = analyze_callbacks("收盘价模式", callback_data_close, df, N=1)

                    s_open = _summary_from_profits(profits_open, max_wins_open, max_losses_open, durations_open, cb_stats_open)
                    s_close = _summary_from_profits(profits_close, max_wins_close, max_losses_close, durations_close, cb_stats_close)

                    # 输出两组统计
                    print("\nPro3版：MACD平滑+ADX自适应 - 交易统计对比 (开盘价/收盘价)")
                    print("="*80)
                    print(f"交易对: {CURRENT_INSTID}")
                    print(f"时间周期: {CURRENT_BAR}")
                    
                    # Calculate total days
                    start_time = df.index.min()
                    end_time = df.index.max()
                    total_days = 0
                    if start_time and end_time:
                         time_delta = end_time - start_time
                         total_days = time_delta.days + time_delta.seconds / 86400
                         if total_days < 1: total_days = 1.0
                    
                    print(f"开始时间: {start_time}")
                    print(f"结束时间: {end_time}")
                    print(f"总天数: {total_days:.1f}天")
                    print("-------------------------------------------------------")
                    
                    def fmt_ratio(val):
                        if val == float('inf'): return "∞"
                        if val == 0: return "0"
                        return f"{val:.4f}"

                    for mode, s in [("开盘价模式", s_open), ("收盘价模式", s_close)]:
                        # Calculate daily/weekly profit
                        # fix_money_history[-1] is the final amount. Base is self.base_money.
                        final_fix = s['fix_money_history'][-1]
                        total_return_pct = (final_fix - self.base_money) / self.base_money * 100
                        daily_profit = total_return_pct / total_days if total_days > 0 else 0
                        weekly_profit = daily_profit * 7
                        
                        print(f"\n价格模式: {mode}")
                        print(f"每日盈利: {daily_profit:.2f}%")
                        print(f"每周盈利: {weekly_profit:.2f}%")
                        print(f"总交易次数: {s['total_trades']}")
                        print(f"胜率: {s['win_rate']:.2%}")
                        print(f"复投最终收益: {s['contract_money_history'][-1]:.2f}")
                        print(f"定投最终收益: {s['fix_money_history'][-1]:.2f}")
                        print(f"混合最终收益: {s['mix_money_history'][-1]:.2f}")
                        print("最终盈亏核心指标")
                        print(f"最终盈利总和: {s['total_profit']:.2f}%")
                        print(f"最终亏损总和: {s['total_loss']:.2f}%")
                        print(f"最终盈利均值（仅盈利单）: {s['avg_profit']:.2f}%")
                        print(f"最终亏损均值（仅亏损单）: {s['avg_loss']:.2f}%")
                        
                        print(f"最终盈亏均值比: {fmt_ratio(s['final_avg_ratio'])}")
                        print(f"最终盈亏总和比: {fmt_ratio(s['final_total_ratio'])}")
                        print(f"平均盈利/最大亏损比: {fmt_ratio(s['avg_profit_max_loss_ratio'])}")
                        print(f"总盈利/最大亏损比: {fmt_ratio(s['total_profit_max_loss_ratio'])}")
                        
                        print("持仓盈亏核心指标")
                        print(f"持仓最大盈利总和: {s['total_max_profit']:.2f}%")
                        print(f"持仓最大亏损总和: {s['total_max_loss']:.2f}%")
                        print(f"持仓最大盈利均值（仅盈利单）: {s['avg_hold_max_profit']:.2f}%")
                        print(f"持仓最大亏损均值（仅亏损单）: {s['avg_hold_max_loss']:.2f}%")
                        
                        print(f"持仓最大盈亏均值比: {fmt_ratio(s['hold_avg_ratio'])}")
                        print(f"持仓最大盈亏总和比: {fmt_ratio(s['hold_total_ratio'])}")
                        
                        print("盈亏到手率（最终盈亏与最大盈亏比值）")
                        print(f"盈利到手率: {s['actual_profit_rate']*100:.2f}%")
                        print(f"亏损到手率: {s['actual_loss_rate']*100:.2f}%")

                        print("=== 持仓周期统计 (小时) ===")
                        print(f"盈利单平均周期: {s['avg_win_dur']:.2f} 小时 (共{s['win_durs_count']}笔)")
                        print(f"亏损单平均周期: {s['avg_loss_dur']:.2f} 小时 (共{s['loss_durs_count']}笔)")
                        print(f"总平均周期: {s['avg_total_dur']:.2f} 小时 (共{s['total_durs_count']}笔)")
                        
                        cb = s['cb_stats']
                        print(f"\n=== 价格回调至开仓位统计 (排除前1小时) ===")
                        print(f"盈利单回调概率: {cb['win_prob']:.2%} ({cb['win_cb_count']}/{cb['win_total']})")
                        print(f"  平均回调时段: {cb['avg_win_cb_time']:.1f} 小时")
                        print(f"亏损单回调概率: {cb['loss_prob']:.2%} ({cb['loss_cb_count']}/{cb['loss_total']})")
                        print(f"  平均回调时段: {cb['avg_loss_cb_time']:.1f} 小时")
                        print("-"*50)
                    
                    # === 新增：对比分析输出 ===
                    print("\n>>> 策略对比分析报告 <<<")
                    print("="*60)
                    
                    # 1. 胜率对比
                    wr_open = s_open['win_rate']
                    wr_close = s_close['win_rate']
                    wr_diff = wr_open - wr_close
                    n = s_open['total_trades']
                    # Z-score calculation for proportion difference (assuming same sample size)
                    # p_pool = (x1 + x2) / (n1 + n2)
                    p_pool = (wr_open * n + wr_close * n) / (2 * n) if n > 0 else 0
                    se = math.sqrt(p_pool * (1 - p_pool) * (2 / n)) if n > 0 and p_pool > 0 and p_pool < 1 else 0
                    z_score = wr_diff / se if se > 0 else 0.0
                    sig_level = ""
                    if abs(z_score) > 2.58: sig_level = "(*** 极显著 p<0.01)"
                    elif abs(z_score) > 1.96: sig_level = "(** 显著 p<0.05)"
                    elif abs(z_score) > 1.65: sig_level = "(* 弱显著 p<0.1)"
                    else: sig_level = "(无显著差异)"
                    
                    print(f"1. 胜率对比:")
                    print(f"   开盘模式: {wr_open:.2%} vs 收盘模式: {wr_close:.2%}")
                    print(f"   差异: {wr_diff:+.2%} {sig_level} Z={z_score:.2f}")

                    # 2. 盈亏比对比
                    print(f"\n2. 盈亏比对比 (平均盈利/平均亏损):")
                    r_open = s_open['final_avg_ratio']
                    r_close = s_close['final_avg_ratio']
                    print(f"   开盘模式: {fmt_ratio(r_open)} vs 收盘模式: {fmt_ratio(r_close)}")
                    
                    # 3. 收益率对比
                    print(f"\n3. 收益率对比:")
                    print(f"   年化收益率: {s_open['annual_return']:.2f}% vs {s_close['annual_return']:.2f}%")
                    print(f"   累计总收益: {s_open['total_return']:.2f}% vs {s_close['total_return']:.2f}%")

                    # 4. 单笔收益率对比
                    print(f"\n4. 单笔收益率对比:")
                    avg_p_open = s_open['avg_profit_val']
                    avg_p_close = s_close['avg_profit_val']
                    p_diff = avg_p_open - avg_p_close
                    p_change_pct = (p_diff / abs(avg_p_close) * 100) if avg_p_close != 0 else 0.0
                    
                    # ATR倍数
                    # 使用 entry_price 的百分比来估算ATR的百分比? 
                    # 策略中的profit是百分比。
                    # avg_atr 是绝对值。
                    # 我们需要将 avg_atr 转换为百分比，大概是 avg_atr / avg_price * 100?
                    # 或者反过来，将 avg_profit_val (百分比) 转换为 ATR倍数?
                    # 假设 avg_profit_val = 1.0 (1%). ATR = 0.1, Price = 10. ATR% = 1%. Ratio = 1.
                    # 我们可以用 (avg_profit_val / 100) * avg_price / avg_atr.
                    # 为了简化，我们直接用 avg_profit_val (百分比) 对比 avg_atr_pct (ATR/Price * 100)
                    # 计算平均价格
                    avg_price = df['close'].mean()
                    avg_atr_pct = (avg_atr / avg_price * 100) if avg_price > 0 else 0
                    
                    atr_mult_open = avg_p_open / avg_atr_pct if avg_atr_pct > 0 else 0
                    atr_mult_close = avg_p_close / avg_atr_pct if avg_atr_pct > 0 else 0
                    
                    print(f"   平均单笔收益: {avg_p_open:.2f}% vs {avg_p_close:.2f}%")
                    print(f"   增减幅度: {p_change_pct:+.2f}%")
                    print(f"   ATR参考值(平均): {avg_atr:.4f} ({avg_atr_pct:.2f}%)")
                    print(f"   单笔收益/ATR倍数: {atr_mult_open:.2f}x vs {atr_mult_close:.2f}x")
                    
                    print("="*60)

                except Exception as e:
                    import traceback
                    print(f"计算开盘/收盘价统计时出错: {e}")
                    traceback.print_exc()
    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(MyStrategy)
    cerebro.run()
    
    strat = cerebro.runstrats[0][0]
    
    # Calculate Time Stats & Daily/Weekly Profit
    start_time = df.index.min()
    end_time = df.index.max()
    time_delta = end_time - start_time
    total_days = time_delta.days + time_delta.seconds / 86400
    if total_days < 1: total_days = 1.0
    
    # Use fix_money (DCA) for calculation
    # Base money is assumed 100 (default in MyStrategy init if not specified, but typically 100)
    # Check strat.base_money if available, else assume 100
    base_money = getattr(strat, 'base_money', 100.0)
    fix_money = getattr(strat, 'fix_money', 100.0)
    
    total_return = (fix_money - base_money) / base_money * 100
    daily_profit = total_return / total_days
    weekly_profit = daily_profit * 7
    
    print("\n时间统计")
    print(f"开始时间: {start_time}")
    print(f"结束时间: {end_time}")
    print(f"总天数: {total_days:.1f}天")
    print(f"每日盈利: {daily_profit:.2f}%")
    print(f"每周盈利: {weekly_profit:.2f}%")

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
    
    atr_value = strat.atr[0]
    return latest_data, last_trade_data, atr_value

def main():
    instId = "NEAR-USDT-SWAP"
    bar = "1H"
    show_market_output = 1
    show_trade_ops_output = 1
    show_trade_records_output = 1
    global PRINT_MARKET, PRINT_TRADE_OPS, PRINT_TRADE_RECORDS
    PRINT_MARKET = 1 if show_market_output else 0
    PRINT_TRADE_OPS = 1 if show_trade_ops_output else 0
    PRINT_TRADE_RECORDS = 1 if show_trade_records_output else 0
    
    latest_data, last_trade_data, atr = get_latest_data(instId, bar)
    
    print("\n=== 方案Pro3: ADX自适应平滑系数系统 ===")
    print("最新行情时间：", latest_data.name)
    print("ATR值:", atr)

if __name__ == "__main__":
    main()

