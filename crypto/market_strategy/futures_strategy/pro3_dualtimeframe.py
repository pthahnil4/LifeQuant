# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/futuresStrategy/pro3_dualtimeframe.py  →  现位置: crypto/market_strategy/futures_strategy/pro3_dualtimeframe.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
# type: ignore
# -*- coding: utf-8 -*-
"""
期货版 双周期Pro3策略模块（复制 cryptoStrategy/pro3_dualtimeframe.py 核心逻辑）
=====================================================================
真·双周期实现（非"两个单周期算法叠加"）：
- 短周期（信号）：histogram 与 smoothed_histogram 简单交叉，灵敏
- 长周期（方向）：justice_flag 状态机 + MACD零轴确认，稳定、带确认延迟
  → 平滑差翻向后不立即改方向，进入 waitRise/waitFall 等待态，
    需 MACD 主线站上/跌破零轴才确认翻转，否则维持原方向 → 长周期更稳
- 开仓：双周期一致且短周期信号翻转时，以 open 价提交限价单
        （有效期4根bar，未成交过期作废，挂单期信号反向撤单）
- 平仓：市价（open 价），触发条件二选一：
        ① 短周期信号刚翻转且与长周期不一致
        ② 长周期方向翻转到持仓反方向

数据源：akshare 期货数据（与单周期共享 _fetch_kline_data / calculate_adx）
对外接口：get_latest_data_dual / get_strategy_full_data_dual（签名与返回结构不变）
"""

import sys
import os
import math

import numpy as np
import pandas as pd
import backtrader as bt


# 从单周期模块复用共享基础设施（数据获取、指标、辅助函数），不复用其交易/长周期逻辑
from crypto.market_strategy.futures_strategy.pro3_singletimeframe import (
    calculate_adx,
    get_adaptive_smooth_weight,
    _fetch_kline_data,
    _align_long_direction_to_short,
    _init_df_columns,
    _attach_prev_adx,
    _calculate_atr,
    _build_detail_response,
    _build_last_trade_data,
    _normalize_dir,
)

from crypto.market_strategy.futures_strategy import pro3_singletimeframe as _single

# 本模块打印开关：
# - 适配器可直接设置本模块这些开关后调用 get_strategy_full_data_dual
# - 或设置 _single 同名开关后调用 get_latest_data_dual
#   （get_latest_data_dual 内部会从 _single 继承这些开关）
PRINT_MARKET = 0
PRINT_TRADE_OPS = 0
PRINT_TRADE_RECORDS = 0
PRINT_ORIGINAL_OUTPUT = 1
FAST_MODE = False

# 全局状态
CURRENT_INSTID = ""
CURRENT_SHORT_BAR = ""
CURRENT_LONG_BAR = ""


# ========== 长周期方向序列（justice_flag 状态机） ==========

def _fill_long_direction_series(df_long):
    """
    计算长周期方向序列，将 LONG_DIRECTION 列写入 df_long。

    核心：justice_flag 状态机（与短周期"简单交叉"完全不同）。
    平滑差翻正/翻负时不立即改方向，先进入 waitRise/waitFall 等待态，
    需 MACD 主线（macd）站上/跌破零轴才确认翻转，否则维持原方向。
    → 带确认延迟，长周期方向更稳、不会随平滑差穿零来回跳。
    """
    class _LongDirSeries(bt.Strategy):  # type: ignore
        def __init__(self):
            self.macd = bt.indicators.MACD(self.data.close)  # type: ignore
            self.smoothed_hist = []
            self.previous_diff = None
            self.justice_flag = "smoothed_histogram"
            self.dealjustice_flag = None
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

            df_long.loc[self.data.datetime.datetime(0), 'LONG_DIRECTION'] = self.modify_flag
            self.previous_diff = hist_smooth_diff

    cerebro = bt.Cerebro()  # type: ignore
    data_feed = bt.feeds.PandasData(dataname=df_long)  # type: ignore
    cerebro.adddata(data_feed)  # type: ignore
    cerebro.addstrategy(_LongDirSeries)  # type: ignore
    cerebro.run()  # type: ignore


# ========== 双周期核心策略（开仓限价单 + 平仓市价） ==========

def _run_dual_strategy(df, long_direction=None):
    """
    运行双周期策略（自包含，不依赖单周期的 _run_strategy）。

    参数:
        df: 短周期 DataFrame（含 ADX 列 + 已对齐的 LONG_DIRECTION 列）
        long_direction: 长周期最新方向（用于展示/记录）

    返回:
        strat: backtrader 策略实例
    """

    class DualTimeframeStrategy(bt.Strategy):  # type: ignore
        params = ()
        """双周期策略：短周期信号(简单交叉) + 长周期方向确认(状态机)"""

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
            self.current_position = None   # 当前持仓方向
            self.filtered_signals = 0      # 被过滤的信号数（空仓时方向不一致）
            self.total_signals = 0         # 实际开仓信号数（提交挂单）
            self.total_exits = 0           # 平仓次数
            self.total_signal_flips = 0    # 信号翻转总次数
            self.entry_price = None

            # === 限价单相关参数与状态 ===
            self.limit_order_ttl = 4       # 限价单有效期（N个bar）
            self.pending_order = None      # 当前挂单

            # 限价单统计计数器
            self.entry_orders_submitted = 0
            self.entry_orders_filled = 0
            self.entry_orders_expired = 0
            self.entry_orders_cancelled = 0

            # 兼容单周期详情构建：假信号/zero 统计占位
            self.zero_entries = 0
            self.false_signals = 0
            self.true_signals = 0
            self.total_crossings = 0

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
                histogram_zero = (prev_dea - (prev_ema12 * 11.0 / 13.0) + (prev_ema26 * 25.0 / 27.0)) / (2.0 / 13.0 - 2.0 / 27.0)

                if curr_weight != 0:
                    hist_smooth_diff_zero = (prev_dea - (prev_ema12 * 11.0 / 13.0) + (prev_ema26 * 25.0 / 27.0) + (prev_smoothed * hist_weight / curr_weight)) / (2.0 / 13.0 - 2.0 / 27.0)
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

            # modify_flag计算（短周期：MACD平滑直接交叉，比状态机更快更灵敏）
            if histogram - smoothed_histogram > 0:
                self.modify_flag = "rise"
            else:
                self.modify_flag = "fall"

            df.loc[self.data.datetime.datetime(0), 'MODIFY_FLAG'] = self.modify_flag

            # === 双周期交易逻辑（开仓限价单 + 平仓市价）===
            raw_long_dir = df.loc[self.data.datetime.datetime(0), 'LONG_DIRECTION'] if 'LONG_DIRECTION' in df.columns else None
            if raw_long_dir is None:
                self.last_modify_flag = self.modify_flag
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
            cur_low = float(self.data.low[0])

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
                              f"短周期:{order['direction']} 长周期:{order.get('long_dir', '')} "
                              f"定投:{self.fix_money:.0f} 复投:{self.contract_money:.0f} "
                              f"混合:{self.mix_money:.0f} {self.data.datetime.datetime(0)}")

                elif self.modify_flag != order['direction']:
                    # === 信号反向撤单 ===
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
                # else: 继续等待

            # -------------------------------------------------------
            # Step 2: 生成新信号 → 平仓直接成交 / 开仓提交限价单
            # -------------------------------------------------------
            if self.pending_order is None:
                # 情况A：【平仓】有持仓 + 双周期不再共振 → 直接以open价平仓
                #   ① 短周期信号刚反转且与长周期不一致，或
                #   ② 长周期方向翻转到持仓反方向
                long_dir_against = (bar_long_dir is not None and self.current_position != bar_long_dir)
                if self.current_position is not None and (
                    ((not directions_match) and signal_changed) or long_dir_against
                ):
                    trade_price = cur_open
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
                elif self.current_position is None and directions_match and signal_changed:
                    self.total_signals += 1
                    entry_price = cur_open
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

                if self.current_position == 'rise':
                    current_profit = ((current_price / self.entry_price) - 1) * 100 * self.times
                else:
                    current_profit = (1 - (current_price / self.entry_price)) * 100 * self.times

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
            """策略结束统计（注意：不做期末强制平仓）"""
            if globals().get('FAST_MODE', False):
                return

            total_trades = len(self.profits)
            winning_trades = sum(1 for p in self.profits if p > 0)
            losing_trades = sum(1 for p in self.profits if p < 0)
            win_rate = winning_trades / total_trades if total_trades > 0 else 0

            total_profit = sum(p for p in self.profits if p > 0)
            total_loss = sum(p for p in self.profits if p < 0)
            avg_profit = total_profit / winning_trades if winning_trades > 0 else 0
            avg_loss = total_loss / losing_trades if losing_trades > 0 else 0

            profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0

            # 最大回撤 - 复投资金曲线
            if len(self.profits) > 0:
                capital_curve = [self.base_money]
                current_capital = self.base_money
                for profit in self.profits:
                    current_capital = current_capital * (1 + profit * 0.01)
                    capital_curve.append(current_capital)
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

            # 权重统计
            avg_hist_weight = np.mean([w[0] for w in self.weight_history]) if self.weight_history else 0
            avg_curr_weight = np.mean([w[1] for w in self.weight_history]) if self.weight_history else 0

            # 资金曲线
            fix_money_history = [self.base_money]
            contract_money_history = [self.base_money]
            mix_money_history = [self.base_money]

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
                    open_price = self.trades[i * 2] if i * 2 < len(self.trades) else 0
                    close_price = self.trades[i * 2 + 1] if i * 2 + 1 < len(self.trades) else 0

                    if i < len(self.max_wins):
                        max_win = self.max_wins[i]
                        max_loss = self.max_losses[i]
                    else:
                        max_win = 0.0
                        max_loss = 0.0

                    fix_after = fix_money_history[i + 1]
                    contract_after = contract_money_history[i + 1]
                    mix_after = mix_money_history[i + 1]

                    fix_change = fix_after - fix_money_history[i]
                    contract_change = contract_after - contract_money_history[i]
                    mix_change = mix_after - mix_money_history[i]

                    if PRINT_TRADE_RECORDS:
                        dir_flag = (self.trade_dirs[i] if i < len(self.trade_dirs) and self.trade_dirs[i] is not None else self.long_direction)
                        open_label = ('开多' if str(dir_flag) == 'rise' else '开空')
                        close_label = ('平多' if str(dir_flag) == 'rise' else '平空')
                        print(f"第{i + 1}次交易盈利\t"
                              f"{open_label}:{open_price:.3f} {close_label}:{close_price:.3f}\t"
                              f"最终盈利:{current_profit:.2f}%\t"
                              f"最大盈利:{max_win:.2f}%\t"
                              f"最大亏损:{max_loss:.2f}%\t"
                              f"定投:{fix_money_history[i]:.0f}→{fix_after:.0f}({fix_change:+.0f})\t"
                              f"复投:{contract_money_history[i]:.0f}→{contract_after:.0f}({contract_change:+.0f})\t"
                              f"混投:{mix_money_history[i]:.0f}→{mix_after:.0f}({mix_change:+.0f})")

            # 统计指标
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

            # 年化收益：基于实际回测天数做几何年化（365天/年），爆仓保护
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
                    total_return = (self.fix_money - self.base_money) / self.base_money * 100
                    daily_profit = total_return / total_days
                    weekly_profit = daily_profit * 7

            if PRINT_ORIGINAL_OUTPUT:
                print("\n期货Pro3版：MACD平滑+ADX自适应 + 双周期 + 开仓限价单+平仓市价 - 交易统计")
                print("=" * 80)
                print(f"品种: {CURRENT_INSTID}")
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
                print("=" * 80)

                entry_fill_rate = (self.entry_orders_filled / self.entry_orders_submitted * 100) if self.entry_orders_submitted > 0 else 0
                print("\n开仓限价单执行统计")
                print("-" * 80)
                print(f"限价单有效期: {self.limit_order_ttl}个bar (每个{CURRENT_SHORT_BAR})")
                print(f"开仓限价单: 提交{self.entry_orders_submitted}次 | 成交{self.entry_orders_filled}次 | 过期{self.entry_orders_expired}次 | 撤销(信号反向){self.entry_orders_cancelled}次 | 成功率{entry_fill_rate:.1f}%")
                print(f"平仓方式: 信号反转时直接以open价成交（无限价单）")
                print(f"完整交易次数: {total_trades} (开仓成交+平仓成交配对)")
                if total_trades > 0:
                    print(f"交易胜率: {win_rate:.2%}")
                    print(f"盈亏比(均值): {profit_loss_ratio:.2f}")
                    print(f"最大回撤(复投): {max_drawdown:.2f}%")
                print("=" * 80)

    cerebro = bt.Cerebro()  # type: ignore
    data_feed = bt.feeds.PandasData(dataname=df)  # type: ignore
    cerebro.adddata(data_feed)  # type: ignore
    cerebro.addstrategy(DualTimeframeStrategy)  # type: ignore
    cerebro.run()  # type: ignore

    strat = cerebro.runstrats[0][0]
    return strat


# ========== 内部：打印开关继承 ==========

def _inherit_flags_from_single():
    """
    从单周期模块继承打印/快速开关。
    适配器在 _single 上设置这些开关后调用 get_latest_data_dual，
    需在此同步到本模块，供自包含策略读取。
    """
    global PRINT_MARKET, PRINT_TRADE_OPS, PRINT_TRADE_RECORDS, PRINT_ORIGINAL_OUTPUT, FAST_MODE
    PRINT_MARKET = getattr(_single, 'PRINT_MARKET', PRINT_MARKET)
    PRINT_TRADE_OPS = getattr(_single, 'PRINT_TRADE_OPS', PRINT_TRADE_OPS)
    PRINT_TRADE_RECORDS = getattr(_single, 'PRINT_TRADE_RECORDS', PRINT_TRADE_RECORDS)
    PRINT_ORIGINAL_OUTPUT = getattr(_single, 'PRINT_ORIGINAL_OUTPUT', PRINT_ORIGINAL_OUTPUT)
    FAST_MODE = getattr(_single, 'FAST_MODE', FAST_MODE)


def _prepare_dual_data(instId, short_bar, long_bar, lookback_days=None):
    """获取短/长周期数据并计算指标、长周期方向、对齐、截断。返回 (df_short, df_long, long_direction)。"""
    df_short = _fetch_kline_data(instId, short_bar, lookback_days=lookback_days)
    df_long = _fetch_kline_data(instId, long_bar)

    df_short = calculate_adx(df_short, period=14)
    df_long = calculate_adx(df_long, period=14)

    # 长周期方向序列（状态机，更稳）
    _fill_long_direction_series(df_long)

    # 长周期最新方向 = 状态机序列最后一个有效值（与交易所用序列同源，保证一致）
    long_direction = None
    if 'LONG_DIRECTION' in df_long.columns:
        valid = df_long['LONG_DIRECTION'].dropna()
        long_direction = valid.iloc[-1] if len(valid) > 0 else None

    # 对齐长周期方向到短周期
    _align_long_direction_to_short(df_short, df_long)

    # 截断没有长周期方向的短周期数据
    if 'LONG_DIRECTION' in df_short.columns:
        first_valid = df_short['LONG_DIRECTION'].first_valid_index()
        if first_valid is not None:
            df_short = df_short.loc[df_short.index >= first_valid]

    _init_df_columns(df_short)
    return df_short, df_long, long_direction


# ========== 对外接口（签名与返回结构保持不变） ==========

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

    # 继承外部（_single）设置的打印/快速开关
    _inherit_flags_from_single()

    df_short, df_long, long_direction = _prepare_dual_data(instId, short_bar, long_bar)

    # 运行双周期策略
    strat = _run_dual_strategy(df_short, long_direction=long_direction)

    latest_data = df_short.iloc[-1].copy()
    _attach_prev_adx(df_short, latest_data)

    last_trade_data = _build_last_trade_data(strat, df_short)

    atr_value = _calculate_atr(df_short)

    return latest_data, last_trade_data, atr_value, long_direction, df_short, df_long


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
    global CURRENT_INSTID, CURRENT_SHORT_BAR, CURRENT_LONG_BAR, FAST_MODE, PRINT_ORIGINAL_OUTPUT
    CURRENT_INSTID = instId
    CURRENT_SHORT_BAR = short_bar
    CURRENT_LONG_BAR = long_bar

    df_short, df_long, long_direction = _prepare_dual_data(instId, short_bar, long_bar,
                                                           lookback_days=lookback_days)

    # 详情模式：完整计算（不 FAST_MODE），但不打印控制台统计
    old_fast = FAST_MODE
    FAST_MODE = False
    old_print_orig = PRINT_ORIGINAL_OUTPUT
    PRINT_ORIGINAL_OUTPUT = 0

    strat = _run_dual_strategy(df_short, long_direction=long_direction)

    FAST_MODE = old_fast
    PRINT_ORIGINAL_OUTPUT = old_print_orig

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

    global PRINT_ORIGINAL_OUTPUT
    PRINT_ORIGINAL_OUTPUT = 1

    latest_data, last_trade_data, atr, long_dir, df_short, df_long = get_latest_data_dual(
        instId, short_bar, long_bar
    )

    print(f"\n=== 期货双周期Pro3（长周期状态机 + 开仓限价单） ===")
    print(f"当前长周期方向: {long_dir}")
    print("最新行情时间：", latest_data.name)
    print("ATR值:", atr)


if __name__ == "__main__":
    main()
