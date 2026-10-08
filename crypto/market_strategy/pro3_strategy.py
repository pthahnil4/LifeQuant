#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/pro3_strategy.py  →  现位置: crypto/market_strategy/pro3_strategy.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
"""
Pro3 策略 — MACD平滑+ADX自适应平滑系数系统（统一版）
=====================================================

合并原 futures/pro3_strategy.py 和 stocks/stock_pro3_strategy.py，
通过 long_only 参数区分多空双向 / 仅做多模式，消除重复代码。

核心思想：
  不干预交易逻辑，只调整 smoothed_histogram 的计算权重
  - 原策略固定权重：0.8/0.2
  - 自适应规则：ADX越大 → 权重越大（更保守）；ADX越小 → 权重越小（更敏感）
  - 权重公式：hist_weight = base_weight + (ADX / 200)

适用市场：
  - 期货（多空双向，long_only=False）
  - 股票（可选仅做多，long_only=True；或多空双向，long_only=False）

使用示例::

    from crypto.market_strategy.pro3_strategy import Pro3Strategy

    # 期货：多空双向
    strategy = Pro3Strategy(df, long_only=False)
    result = strategy.run()

    # 股票：仅做多
    strategy = Pro3Strategy(df, long_only=True)
    result = strategy.run()
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import datetime as _dt
import logging
from typing import Optional, Dict, List, Tuple, Any

import numpy as np
import pandas as pd

from crypto.market_strategy.indicators import (
    calculate_adx,
    calc_sar,
    calc_atr,
    get_adaptive_smooth_weight,
    calculate_ema,
    calculate_macd,
)

logger = logging.getLogger(__name__)


# ====================================================================
#  Pro3 策略执行器（纯 pandas/numpy 实现，无需 backtrader）
# ====================================================================

class Pro3Strategy:
    """Pro3 策略 — MACD平滑+ADX自适应平滑系数系统

    适用市场：期货 / 股票（通过 long_only 参数切换）
    交易方向：默认多空双向（反手策略），可切换为仅做多模式
    核心逻辑：ADX 自适应调整 MACD 柱状图平滑权重

    Parameters
    ----------
    df : pd.DataFrame
        行情数据，必须包含列: 'high', 'low', 'close'。
        推荐列: 'date', 'open', 'high', 'low', 'close', 'volume'。
    adx_period : int
        ADX 计算周期，默认 9。
    macd_fast, macd_slow, macd_signal : int
        MACD 参数。
    base_weight : float
        自适应权重的基础值，默认 0.7。
    trade_multiplier : float
        交易乘数（放大盈亏显示），默认 10.0。
    initial_capital : float
        初始资金，默认 100（用于收益率计算）。
    long_only : bool
        是否仅做多。True 时忽略"下跌"信号，只在"上涨"信号时开多仓；
        False 时多空双向反手交易（默认）。
    use_sar : bool
        是否计算 SAR 指标。期货版默认 True，股票版可设为 False。

    使用示例::

        # 期货（多空双向）
        strategy = Pro3Strategy(df, long_only=False)
        result = strategy.run()

        # 股票（仅做多）
        strategy = Pro3Strategy(df, long_only=True, use_sar=False)
        result = strategy.run()
    """

    def __init__(self,
                 df: pd.DataFrame,
                 adx_period: int = 9,
                 macd_fast: int = 12,
                 macd_slow: int = 26,
                 macd_signal: int = 9,
                 base_weight: float = 0.7,
                 trade_multiplier: float = 10.0,
                 initial_capital: float = 100.0,
                 long_only: bool = False,
                 use_sar: bool = True,
                 long_direction: str = None):
        # 数据校验
        required_cols = ['high', 'low', 'close']
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError("数据缺少必要列: %s，实际列: %s" %
                             (missing, df.columns.tolist()))

        self.df = df.copy()
        self.adx_period = adx_period
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.base_weight = base_weight
        self.trade_multiplier = trade_multiplier
        self.initial_capital = initial_capital
        self.long_only = long_only
        self.use_sar = use_sar
        # 双周期方向过滤：'rise'只做多、'fall'只做空、None不过滤
        self.long_direction = long_direction

        # 运行时状态
        self._indicators: Optional[pd.DataFrame] = None
        self._signals: Optional[pd.DataFrame] = None
        self._trades: List[Dict] = []
        self._summary: Dict = {}

    # ----------------------------------------------------------------
    #  指标计算
    # ----------------------------------------------------------------

    def _prepare_indicators(self) -> pd.DataFrame:
        """计算所有技术指标

        Returns
        -------
        pd.DataFrame
            包含 ADX, ATR_pct, MACD, SAR(可选) 等全部指标列的 DataFrame
        """
        data = self.df.copy()

        # 1. 确保 date 列为 datetime 索引
        if 'date' in data.columns:
            data['date'] = pd.to_datetime(data['date'], errors='coerce')
            data = data.dropna(subset=['date'])
            data = data.set_index('date').sort_index()
        elif not isinstance(data.index, pd.DatetimeIndex):
            raise ValueError("数据需要 'date' 列或 DatetimeIndex 索引")

        # 2. 计算 ADX
        data = calculate_adx(data, self.adx_period)

        # 3. 计算 ATR（百分比，含杠杆倍数）
        data['ATR_pct'] = calc_atr(data, self.adx_period) / data['close'] * 100 * 10

        # 4. 计算 MACD
        data = calculate_macd(data, self.macd_fast, self.macd_slow,
                              self.macd_signal)

        # 5. 计算 SAR（可选）
        if self.use_sar:
            data['SAR'] = calc_sar(data)

        return data

    # ----------------------------------------------------------------
    #  信号生成
    # ----------------------------------------------------------------

    def _generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """生成交易信号

        核心逻辑：
        1. 使用 ADX 自适应权重计算 smoothed_histogram
        2. 比较 histogram 与 smoothed_histogram 生成方向信号
        3. 反手交易：每产生新信号即平旧仓开新仓
        """
        df = data.copy()

        # 初始化信号列
        df['hist_weight'] = 0.0
        df['curr_weight'] = 0.0
        df['smoothed_macd'] = 0.0
        df['signal'] = 'hold'
        df['HISTOGRAM_ZERO'] = np.nan
        df['HIST_SMOOTH_DIFF_ZERO'] = np.nan
        df['dealjustice'] = ''

        smoothed_hist_list = []
        previous_diff = 0
        original_flag = None
        justice_flag = 'smoothed_histogram'
        dealjustice_flag = None
        modify_flag = None

        for i in range(len(df)):
            adx_val = df.iloc[i]['ADX'] if pd.notna(df.iloc[i]['ADX']) else 0
            macd_val = df.iloc[i]['MACD'] if pd.notna(df.iloc[i]['MACD']) else 0

            # 自适应权重
            hist_w, curr_w = get_adaptive_smooth_weight(adx_val, self.base_weight)
            df.iloc[i, df.columns.get_loc('hist_weight')] = hist_w
            df.iloc[i, df.columns.get_loc('curr_weight')] = curr_w

            # 平滑柱状图
            if len(smoothed_hist_list) == 0:
                smoothed_hist = macd_val
            else:
                smoothed_hist = (smoothed_hist_list[-1] * hist_w
                                 + macd_val * curr_w)
            smoothed_hist_list.append(smoothed_hist)
            df.iloc[i, df.columns.get_loc('smoothed_macd')] = smoothed_hist

            hist_smooth_diff = macd_val - smoothed_hist

            # 计算零点价格（使用动态 MACD 参数）
            if i > 0:
                prev_dea = df.iloc[i - 1]['DEA']
                prev_ema12 = df.iloc[i - 1]['EMA12']
                prev_ema26 = df.iloc[i - 1]['EMA26']
                prev_smoothed = (smoothed_hist_list[-2]
                                 if len(smoothed_hist_list) > 1 else 0)
                if (pd.notna(prev_dea) and pd.notna(prev_ema12)
                        and pd.notna(prev_ema26)):
                    alpha_f = 2.0 / (self.macd_fast + 1)
                    alpha_s = 2.0 / (self.macd_slow + 1)
                    denom = alpha_f - alpha_s
                    histogram_zero = (
                        prev_dea - prev_ema12 * (1 - alpha_f)
                        + prev_ema26 * (1 - alpha_s)) / denom
                    hist_smooth_diff_zero = (
                        prev_dea - prev_ema12 * (1 - alpha_f)
                        + prev_ema26 * (1 - alpha_s)
                        + prev_smoothed * 0.5) / denom
                    df.iloc[i, df.columns.get_loc('HISTOGRAM_ZERO')] = (
                        histogram_zero)
                    df.iloc[i, df.columns.get_loc('HIST_SMOOTH_DIFF_ZERO')] = (
                        hist_smooth_diff_zero)

            # 原始信号
            if (i > 0
                    and macd_val > smoothed_hist
                    and previous_diff < 0):
                original_flag = 'rise'
            elif (i > 0
                  and macd_val < smoothed_hist
                  and previous_diff > 0):
                original_flag = 'fall'

            previous_diff = hist_smooth_diff

            # ★ 使用前一根K线的 DIF 进行确认
            prev_dif = (df.iloc[i - 1]['DIF']
                        if i > 0 and pd.notna(df.iloc[i - 1]['DIF']) else 0)

            # modify_flag 计算（Pro3 核心）
            if justice_flag == 'smoothed_histogram':
                if hist_smooth_diff > 0:
                    if i > 0 and prev_dif > 0:
                        modify_flag = 'rise'
                        justice_flag = 'smoothed_histogram'
                    else:
                        justice_flag = 'histogram'
                        dealjustice_flag = 'waitRise'
                else:
                    if i > 0 and prev_dif < 0:
                        modify_flag = 'fall'
                        justice_flag = 'smoothed_histogram'
                    else:
                        justice_flag = 'histogram'
                        dealjustice_flag = 'waitFall'

            elif justice_flag == 'histogram':
                if dealjustice_flag == 'waitRise':
                    if macd_val > 0:
                        modify_flag = 'rise'
                        justice_flag = 'smoothed_histogram'
                    else:
                        modify_flag = 'fall'
                elif dealjustice_flag == 'waitFall':
                    if macd_val < 0:
                        modify_flag = 'fall'
                        justice_flag = 'smoothed_histogram'
                    else:
                        modify_flag = 'rise'

            df.iloc[i, df.columns.get_loc('signal')] = modify_flag or 'hold'
            df.iloc[i, df.columns.get_loc('dealjustice')] = (
                dealjustice_flag or '')

        return df

    # ----------------------------------------------------------------
    #  交易执行
    # ----------------------------------------------------------------

    def _execute_trades(self, data: pd.DataFrame) -> Dict:
        """基于信号执行交易

        Returns
        -------
        dict
            包含 'trade_log', 'summary', 'signals_df' 等的结果字典。
        """
        df = data.copy()

        # 交易状态
        trades: List[float] = []
        trade_times: List[pd.Timestamp] = []
        trade_directions: List[str] = []
        profits: List[float] = []
        entry_adx: List[float] = []
        max_wins: List[float] = []
        max_losses: List[float] = []

        k = 0
        error = 0
        last_modify_flag = None
        profit_retracements: List[float] = []

        for i in range(len(df)):
            signal = df.iloc[i]['signal']

            # ★ 逐K线跟踪持仓期间最大盈亏
            if len(trades) > 0 and len(profits) < len(trades):
                last_trade_price = trades[-1]
                current_high = float(df.iloc[i]['high'])
                current_low = float(df.iloc[i]['low'])
                if trade_directions[-1] == 'Long':
                    cur_win = ((current_high / last_trade_price) - 1) * \
                              100 * self.trade_multiplier
                    cur_loss = ((current_low / last_trade_price) - 1) * \
                               100 * self.trade_multiplier
                else:
                    cur_win = (1 - (current_low / last_trade_price)) * \
                              100 * self.trade_multiplier
                    cur_loss = (1 - (current_high / last_trade_price)) * \
                               100 * self.trade_multiplier
                max_wins[-1] = max(max_wins[-1], max(0.0, cur_win))
                max_losses[-1] = min(max_losses[-1], min(0.0, cur_loss))

            if signal not in ('rise', 'fall'):
                continue

            # long_only 模式下忽略 fall 信号
            if self.long_only and signal == 'fall':
                last_modify_flag = signal
                continue

            # 双周期方向过滤：优先使用逐bar动态方向，否则使用固定方向
            bar_long_dir = None
            if 'LONG_DIRECTION' in df.columns:
                raw = df.iloc[i].get('LONG_DIRECTION', None)
                if raw is not None and str(raw) not in ('None', 'nan', ''):
                    s = str(raw)
                    if s in ('rise', '多'):
                        bar_long_dir = 'rise'
                    elif s in ('fall', '空'):
                        bar_long_dir = 'fall'
                    # 逐bar方向为None/无效时不阻止交易（等同于不过滤）
                    if bar_long_dir is not None:
                        if bar_long_dir == 'rise' and signal == 'fall':
                            last_modify_flag = signal
                            continue
                        if bar_long_dir == 'fall' and signal == 'rise':
                            last_modify_flag = signal
                            continue
            elif self.long_direction is not None:
                if self.long_direction == 'rise' and signal == 'fall':
                    last_modify_flag = signal
                    continue
                if self.long_direction == 'fall' and signal == 'rise':
                    last_modify_flag = signal
                    continue

            # 反手策略：首次信号只记录方向，等待反向信号才开仓
            if last_modify_flag is not None:
                is_anti = ((signal == 'rise' and last_modify_flag == 'fall')
                           or (signal == 'fall' and last_modify_flag == 'rise'))
                if not is_anti:
                    last_modify_flag = signal
                    continue
            else:
                last_modify_flag = signal
                continue

            k += 1

            # ★ 精确入场价：使用零点价格
            dealjustice = df.iloc[i].get('dealjustice', '')
            if i > 0:
                if ((signal == 'rise' and dealjustice == 'waitRise')
                        or (signal == 'fall'
                            and dealjustice == 'waitFall')):
                    trade_price = df.iloc[i - 1]['HISTOGRAM_ZERO']
                else:
                    trade_price = df.iloc[i - 1]['HIST_SMOOTH_DIFF_ZERO']
            else:
                trade_price = np.nan

            # 价格校验：越界则回退到 open
            current_high = float(df.iloc[i]['high'])
            current_low = float(df.iloc[i]['low'])
            if (pd.isna(trade_price)
                    or trade_price > current_high
                    or trade_price < current_low):
                error += 1
                trade_price = float(df.iloc[i]['open'])
            else:
                trade_price = float(trade_price)

            profit = 0.0

            if k > 1 and len(trades) > 0:
                # 平仓并计算盈亏
                if trade_directions[-1] == 'Long':
                    profit = ((trade_price / trades[-1]) - 1) * 100 * \
                             self.trade_multiplier
                else:
                    profit = (1 - (trade_price / trades[-1])) * 100 * \
                             self.trade_multiplier

                # 用平仓价更新最大盈亏
                if profit > 0:
                    max_wins[-1] = max(max_wins[-1], profit)
                else:
                    max_losses[-1] = min(max_losses[-1], profit)

                # 计算利润回撤
                retracement = max_wins[-1] - profit
                profit_retracements.append(retracement)

                profits.append(profit)

            # 开新仓
            direction = 'Long' if signal == 'rise' else 'Short'
            trades.append(trade_price)
            trade_times.append(df.index[i])
            trade_directions.append(direction)
            entry_adx.append(float(df.iloc[i]['ADX']))
            max_wins.append(0.0)
            max_losses.append(0.0)

            if signal in ('rise', 'fall'):
                last_modify_flag = signal

        # 期末平仓
        if len(trades) > 0 and len(profits) < len(trades):
            last_price = float(df.iloc[-1]['close'])
            if trade_directions[-1] == 'Long':
                profit = ((last_price / trades[-1]) - 1) * 100 * \
                         self.trade_multiplier
            else:
                profit = (1 - (last_price / trades[-1])) * 100 * \
                         self.trade_multiplier
            retracement = max_wins[-1] - profit
            profit_retracements.append(retracement)
            profits.append(profit)

        # ---- 统计汇总 ----
        total_trades = len(profits)
        winning_trades = sum(1 for p in profits if p > 0)
        losing_trades = total_trades - winning_trades
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

        total_profit = sum(p for p in profits if p > 0) if profits else 0.0
        total_loss = sum(p for p in profits if p < 0) if profits else 0.0
        avg_profit = (total_profit / winning_trades) if winning_trades > 0 else 0.0
        avg_loss = (total_loss / losing_trades) if losing_trades > 0 else 0.0
        profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0.0

        net_profit = sum(profits) if profits else 0.0

        # 最大回撤（基于复投资金曲线）
        if len(profits) > 0:
            capital = self.initial_capital
            peak = capital
            max_dd = 0.0
            for p in profits:
                capital *= (1 + p / 100.0)
                if capital < 0:
                    capital = 0.0
                if capital > peak:
                    peak = capital
                if peak > 0:
                    dd = (peak - capital) / peak * 100
                    if dd > max_dd:
                        max_dd = dd
        else:
            max_dd = 0.0

        # ---- 持仓周期统计 ----
        win_durations = []
        loss_durations = []
        total_durations = []
        for i in range(len(profits)):
            if i < len(trade_times):
                entry_time = trade_times[i]
                if i + 1 < len(trade_times):
                    exit_time = trade_times[i + 1]
                else:
                    exit_time = df.index[-1]
                dur_h = (exit_time - entry_time).total_seconds() / 3600.0
                total_durations.append(dur_h)
                if profits[i] > 0:
                    win_durations.append(dur_h)
                else:
                    loss_durations.append(dur_h)

        avg_win_dur = sum(win_durations) / len(win_durations) if win_durations else 0.0
        avg_loss_dur = sum(loss_durations) / len(loss_durations) if loss_durations else 0.0
        avg_total_dur = sum(total_durations) / len(total_durations) if total_durations else 0.0

        # 平均利润回撤
        avg_retracement = (sum(profit_retracements) / len(profit_retracements)
                          if profit_retracements else 0.0)

        # ---- ADX 统计 ----
        adx_entries_trimmed = entry_adx[:len(profits)] if entry_adx else []
        adx_mean = float(np.mean(adx_entries_trimmed)) if adx_entries_trimmed else 0.0
        wins_adx = [adx_entries_trimmed[i] for i, p in enumerate(profits)
                    if p > 0 and i < len(adx_entries_trimmed)]
        losses_adx = [adx_entries_trimmed[i] for i, p in enumerate(profits)
                      if p < 0 and i < len(adx_entries_trimmed)]
        adx_win_mean = float(np.mean(wins_adx)) if wins_adx else 0.0
        adx_loss_mean = float(np.mean(losses_adx)) if losses_adx else 0.0

        # ADX 相关性
        try:
            x = np.array(adx_entries_trimmed, dtype=float)
            y = np.array(profits, dtype=float)
            adx_corr = float(np.corrcoef(x, y)[0, 1]) if x.size > 1 and y.size > 1 and np.std(x) > 0 and np.std(y) > 0 else 0.0
        except Exception:
            adx_corr = 0.0

        # ADX 高低阈值胜率
        high_thresh = 25.0
        high_idx_list = [i for i, a in enumerate(adx_entries_trimmed) if a >= high_thresh]
        low_idx_list = [i for i, a in enumerate(adx_entries_trimmed) if a < high_thresh]
        high_wins_count = sum(1 for i in high_idx_list if i < len(profits) and profits[i] > 0)
        low_wins_count = sum(1 for i in low_idx_list if i < len(profits) and profits[i] > 0)
        high_win_rate = high_wins_count / len(high_idx_list) if high_idx_list else 0.0
        low_win_rate = low_wins_count / len(low_idx_list) if low_idx_list else 0.0

        # 权重统计
        hist_weights = df['hist_weight'].dropna().tolist()
        avg_hist_weight = float(np.mean(hist_weights)) if hist_weights else 0.0
        curr_weights = df['curr_weight'].dropna().tolist()
        avg_curr_weight = float(np.mean(curr_weights)) if curr_weights else 0.0

        # 卡玛比率
        total_return_pct = net_profit
        calmar_ratio = total_return_pct / max_dd if max_dd > 0 else 0.0

        # 盈利因子
        profit_factor = abs(total_profit / total_loss) if total_loss != 0 else 0.0

        # 资金曲线
        base_money = self.initial_capital
        contract_money = base_money
        fix_money = base_money
        mix_money = base_money
        for p in profits:
            fix_money += base_money * p * 0.01
            contract_money *= (1 + p * 0.01)
            if contract_money < 0:
                contract_money = 0.0
            ratio = mix_money / base_money
            if ratio > 0 and p != 0:
                try:
                    log_value = np.log(ratio) / np.log(1.6)
                    if not np.isnan(log_value) and not np.isinf(log_value):
                        base_mix = base_money * pow(1.6, np.floor(log_value))
                        base_mix = max(base_mix, base_money)
                    else:
                        base_mix = base_money
                except Exception:
                    base_mix = base_money
            else:
                base_mix = base_money
            mix_money += base_mix * p * 0.01

        contract_return_pct = (contract_money - base_money) / base_money * 100

        summary = {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': win_rate,
            'net_profit_pct': round(net_profit, 2),
            'total_profit': round(total_profit, 2),
            'total_loss': round(total_loss, 2),
            'avg_profit': round(avg_profit, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_loss_ratio': round(profit_loss_ratio, 2),
            'max_drawdown': round(max_dd, 2),
            'initial_capital': self.initial_capital,
            'final_capital': round(self.initial_capital * (1 + net_profit / 100), 2),
            'fix_money': round(fix_money, 2),
            'contract_money': round(contract_money, 2),
            'mix_money': round(mix_money, 2),
            'total_return_pct': round(total_return_pct, 2),
            'contract_return_pct': round(contract_return_pct, 2),
            'calmar_ratio': round(calmar_ratio, 2),
            'profit_factor': round(profit_factor, 2),
            'avg_win_dur_h': round(avg_win_dur, 1),
            'avg_loss_dur_h': round(avg_loss_dur, 1),
            'avg_total_dur_h': round(avg_total_dur, 1),
            'avg_retracement_pct': round(avg_retracement, 2),
            'adx_mean': round(adx_mean, 1),
            'adx_win_mean': round(adx_win_mean, 1),
            'adx_loss_mean': round(adx_loss_mean, 1),
            'adx_corr': round(adx_corr, 3),
            'high_adx_win_rate': round(high_win_rate, 4),
            'low_adx_win_rate': round(low_win_rate, 4),
            'avg_hist_weight': round(avg_hist_weight, 3),
            'avg_curr_weight': round(avg_curr_weight, 3),
            'total_errors': error,
            'long_only': self.long_only,
        }

        # 交易日志
        trade_log = []
        for i in range(len(profits)):
            trade_log.append({
                'trade_no': i + 1,
                'open_time': trade_times[i] if i < len(trade_times) else None,
                'direction': trade_directions[i] if i < len(trade_directions)
                             else 'Unknown',
                'open_price': trades[i] if i < len(trades) else 0.0,
                'exit_price': trades[i + 1] if (i + 1) < len(trades)
                              else float(df.iloc[-1]['close']),
                'profit_pct': profits[i],
                'entry_adx': entry_adx[i] if i < len(entry_adx) else 0.0,
                'max_win_pct': max_wins[i] if i < len(max_wins) else 0.0,
                'max_loss_pct': max_losses[i] if i < len(max_losses) else 0.0,
                'retracement_pct': profit_retracements[i] if i < len(profit_retracements) else 0.0,
            })

        self._trades = trade_log
        self._summary = summary
        self._signals = df

        return {
            'trade_log': trade_log,
            'summary': summary,
            'signals_df': df,
            'profits': profits,
            'trades': trades,
            'trade_times': trade_times,
            'trade_directions': trade_directions,
            'entry_adx': entry_adx,
            'max_wins': max_wins,
            'max_losses': max_losses,
            'profit_retracements': profit_retracements,
            'data_start': df.index[0],
            'data_end': df.index[-1],
        }

    # ----------------------------------------------------------------
    #  主入口
    # ----------------------------------------------------------------

    def run(self) -> Dict:
        """运行策略，执行完整回测

        流程: 准备指标 → 生成信号 → 执行交易

        Returns
        -------
        dict
            {
                'trade_log': list[dict],     # 每笔交易记录
                'summary': dict,              # 交易统计汇总
                'signals_df': pd.DataFrame,   # 带全部指标的完整数据
            }
        """
        data = self._prepare_indicators()
        data = self._generate_signals(data)
        result = self._execute_trades(data)
        self._indicators = data
        return result

    # ----------------------------------------------------------------
    #  Properties
    # ----------------------------------------------------------------

    @property
    def indicators(self) -> Optional[pd.DataFrame]:
        """获取计算后的指标数据，需先调用 run()"""
        return self._indicators

    @property
    def trades(self) -> List[Dict]:
        """获取交易记录列表，需先调用 run()"""
        return self._trades

    @property
    def summary(self) -> Dict:
        """获取策略摘要统计，需先调用 run()"""
        return self._summary

    # ----------------------------------------------------------------
    #  获取最新信号
    # ----------------------------------------------------------------

    def get_last_signal(self) -> Dict[str, Any]:
        """获取最后一根K线的信号数据（用于实时监控/详情页）

        Returns
        -------
        dict
            {price, action_signal, modify_flag, macd_histogram, macd_dif,
             adx, plus_di, minus_di, smoothed_macd, hist_weight, atr,
             timestamp, position_status, entry_price, current_profit}
        """
        if self._signals is None or self._signals.empty:
            return {
                "price": 0,
                "action_signal": "--",
                "modify_flag": "wait",
            }

        last = self._signals.iloc[-1]
        modify_flag = last.get('signal', 'wait')
        if modify_flag == 'hold':
            modify_flag = 'wait'

        action_map = {'rise': '上涨', 'fall': '下跌', 'wait': '观望'}
        action_signal = action_map.get(modify_flag, '观望')

        # 当前持仓信息
        position_status = "空仓"
        entry_price = 0.0
        current_profit = 0.0
        if self._trades:
            last_trade = self._trades[-1]
            entry_price = last_trade['open_price']
            direction = last_trade['direction']
            current_close = float(last.get('close', 0))
            if current_close > 0 and entry_price > 0:
                if direction == 'Long':
                    current_profit = round(
                        ((current_close / entry_price) - 1) * 100 *
                        self.trade_multiplier, 2)
                else:
                    current_profit = round(
                        (1 - (current_close / entry_price)) * 100 *
                        self.trade_multiplier, 2)
            position_status = "多头持仓" if direction == 'Long' else "空头持仓"

        timestamp = (last.name.strftime("%Y-%m-%d %H:%M:%S")
                     if hasattr(last.name, 'strftime')
                     else str(last.name))

        sar_val = round(float(last.get('SAR', 0)), 4) if 'SAR' in last.index else 0

        return {
            "timestamp": timestamp,
            "price": round(float(last.get('close', 0)), 4),
            "action_signal": action_signal,
            "modify_flag": modify_flag,
            "macd_histogram": round(float(last.get('MACD', 0)), 4),
            "macd_dif": round(float(last.get('DIF', 0)), 4),
            "macd_dea": round(float(last.get('DEA', 0)), 4),
            "smoothed_macd": round(float(last.get('smoothed_macd', 0)), 4),
            "adx": round(float(last.get('ADX', 0)), 2),
            "plus_di": round(float(last.get('+DI', 0)), 2),
            "minus_di": round(float(last.get('-DI', 0)), 2),
            "hist_weight": round(float(last.get('hist_weight', 0)), 3),
            "curr_weight": round(float(last.get('curr_weight', 0)), 3),
            "atr": round(float(last.get('ATR_pct', 0)), 4),
            "sar": sar_val,
            "position_status": position_status,
            "entry_price": round(entry_price, 4),
            "current_profit": current_profit,
        }

    # ----------------------------------------------------------------
    #  打印报告
    # ----------------------------------------------------------------

    def print_report(self):
        """打印策略运行报告"""
        if not self._summary:
            print("请先调用 run() 运行策略。")
            return

        s = self._summary
        mode_str = "仅做多" if self.long_only else "多空双向"
        print("\n" + "=" * 70)
        print("  Pro3 策略 — MACD平滑+ADX自适应 (交易模式: %s)" % mode_str)
        print("=" * 70)
        print("  交易统计:")
        print("    总交易次数:  %d" % s['total_trades'])
        print("    盈利次数:    %d" % s['winning_trades'])
        print("    亏损次数:    %d" % s['losing_trades'])
        print("    胜率:        %.2f%%" % (s['win_rate'] * 100))
        print("    净利润:      %.2f%%" % s['net_profit_pct'])
        print("    盈利因子:    %.2f" % (abs(s['total_profit'] / s['total_loss'])
                                        if s['total_loss'] != 0 else float('inf')))
        print("    盈亏均值比:  %.2f" % s['profit_loss_ratio'])
        print("    最大回撤:    %.2f%%" % s['max_drawdown'])
        print("    初始资金:    %.2f → 最终: %.2f" %
              (s['initial_capital'], s['final_capital']))
        print("-" * 70)

        print("  持仓周期统计:")
        print("    盈利单平均:  %.1f 小时" % s['avg_win_dur_h'])
        print("    亏损单平均:  %.1f 小时" % s['avg_loss_dur_h'])
        print("    总平均:      %.1f 小时" % s['avg_total_dur_h'])
        print("-" * 70)

        print("  ADX 统计:")
        print("    ADX均值:    %.1f" % s['adx_mean'])
        print("    胜单ADX:    %.1f" % s['adx_win_mean'])
        print("    亏单ADX:    %.1f" % s['adx_loss_mean'])
        print("    ADX相关性:  %.3f" % s['adx_corr'])
        print("    ADX≥25:    %.2f%%" % (s['high_adx_win_rate'] * 100))
        print("    ADX<25:    %.2f%%" % (s['low_adx_win_rate'] * 100))
        print("-" * 70)

        print("  自适应权重统计:")
        print("    平均历史权重: %.3f" % s['avg_hist_weight'])
        print("    平均当前权重: %.3f" % s['avg_curr_weight'])
        print("-" * 70)

        print("  最近 5 笔交易:")
        print("  %-4s %-20s %-6s %-12s %-12s %-10s" %
              ("#", "开仓时间", "方向", "开仓价", "平仓价", "盈亏%"))
        for t in self._trades[-5:]:
            ot = t['open_time'].strftime('%Y-%m-%d %H:%M') if t['open_time'] else 'N/A'
            print("  %-4d %-20s %-6s %-12.3f %-12.3f %+10.2f" %
                  (t['trade_no'], ot, t['direction'],
                   t['open_price'], t['exit_price'], t['profit_pct']))
        print("=" * 70)


# ====================================================================
#  向后兼容别名
# ====================================================================

class FuturesPro3Strategy(Pro3Strategy):
    """期货 Pro3 策略（向后兼容别名）

    等价于 Pro3Strategy(long_only=False, use_sar=True)
    """

    def __init__(self, df: pd.DataFrame, **kwargs):
        kwargs.setdefault('long_only', False)
        kwargs.setdefault('use_sar', True)
        super().__init__(df, **kwargs)


class StockPro3Strategy(Pro3Strategy):
    """股票 Pro3 策略（向后兼容别名）

    等价于 Pro3Strategy(use_sar=False)，long_only 由参数决定
    """

    def __init__(self, df: pd.DataFrame, **kwargs):
        kwargs.setdefault('use_sar', False)
        super().__init__(df, **kwargs)


# ====================================================================
#  便捷运行函数
# ====================================================================

def run_pro3_strategy(df: pd.DataFrame, **kwargs) -> Dict:
    """便捷函数：一行运行 Pro3 策略（期货模式）

    Parameters
    ----------
    df : pd.DataFrame
        行情数据，必须含 'high', 'low', 'close'。
    **kwargs
        其他参数传递给 Pro3Strategy。

    Returns
    -------
    dict
        策略结果，包含 trade_log, summary, signals_df。

    示例::

        from crypto.market_strategy.pro3_strategy import run_pro3_strategy
        result = run_pro3_strategy(df, base_weight=0.7)
        print(result['summary'])
    """
    kwargs.setdefault('long_only', False)
    strategy = Pro3Strategy(df, **kwargs)
    return strategy.run()


def run_stock_pro3_strategy(df: pd.DataFrame, **kwargs) -> Dict:
    """便捷函数：一行运行 Pro3 策略（股票模式）

    Parameters
    ----------
    df : pd.DataFrame
        行情数据，必须含 'high', 'low', 'close'。
    **kwargs
        其他参数传递给 Pro3Strategy。

    Returns
    -------
    dict
        策略结果，包含 trade_log, summary, signals_df。

    示例::

        from crypto.market_strategy.pro3_strategy import run_stock_pro3_strategy
        result = run_stock_pro3_strategy(df, long_only=True)
        print(result['summary'])
    """
    kwargs.setdefault('use_sar', False)
    strategy = Pro3Strategy(df, **kwargs)
    return strategy.run()


# ====================================================================
#  完整策略结果函数（供详情页使用）
# ====================================================================

def get_strategy_full_data(df: pd.DataFrame,
                            symbol: str = "",
                            bar: str = "1H",
                            display_name: str = "",
                            lookback_days: int = None,
                            **strategy_kwargs) -> Dict:
    """运行 Pro3 策略并返回完整结构化数据

    本函数封装 Pro3Strategy，提供完整的策略运行结果，供详情页使用。

    Parameters
    ----------
    df : pd.DataFrame
        行情数据，必须包含 'high', 'low', 'close' 列。
        推荐列: 'date', 'open', 'high', 'low', 'close', 'volume'。
    symbol : str
        品种代码（如 'RB'），用于标识。
    bar : str
        K线周期标识（如 '1H', '1D'）。
    display_name : str
        品种显示名称（如 '螺纹钢'）。
    lookback_days : int, optional
        回看天数，用于日收益率计算（如 365 表示一年）。
        若为 None，则根据数据实际跨度自动计算。
    **strategy_kwargs
        传递给 Pro3Strategy 的额外参数：
        - adx_period: int (默认 9)
        - macd_fast: int (默认 12)
        - macd_slow: int (默认 26)
        - macd_signal: int (默认 9)
        - base_weight: float (默认 0.7)
        - trade_multiplier: float (默认 10.0)
        - initial_capital: float (默认 100.0)
        - long_only: bool (默认 False)
        - use_sar: bool (默认 True)

    Returns
    -------
    dict
        {
            "symbol": str,
            "display_name": str,
            "bar": str,
            "start_time": str,
            "end_time": str,
            "total_days": float,
            "daily_profit": float,
            "weekly_profit": float,
            "market": {...},
            "current_position": {...},
            "trade_records": [...],
            "stats": {...}
        }
    """
    # 提取策略参数
    adx_period = strategy_kwargs.pop('adx_period', 9)
    macd_fast = strategy_kwargs.pop('macd_fast', 12)
    macd_slow = strategy_kwargs.pop('macd_slow', 26)
    macd_signal = strategy_kwargs.pop('macd_signal', 9)
    base_weight = strategy_kwargs.pop('base_weight', 0.7)
    trade_multiplier = strategy_kwargs.pop('trade_multiplier', 10.0)
    initial_capital = strategy_kwargs.pop('initial_capital', 100.0)
    long_only = strategy_kwargs.pop('long_only', False)
    use_sar = strategy_kwargs.pop('use_sar', True)
    long_direction = strategy_kwargs.pop('long_direction', None)

    # 创建并运行策略
    strategy = Pro3Strategy(
        df,
        adx_period=adx_period,
        macd_fast=macd_fast,
        macd_slow=macd_slow,
        macd_signal=macd_signal,
        base_weight=base_weight,
        trade_multiplier=trade_multiplier,
        initial_capital=initial_capital,
        long_only=long_only,
        use_sar=use_sar,
        long_direction=long_direction,
    )
    result = strategy.run()

    summary = result['summary']
    trade_log = result['trade_log']
    signals_df = result['signals_df']
    profits = result['profits']
    trades = result['trades']
    trade_times = result['trade_times']
    trade_directions = result['trade_directions']
    entry_adx = result['entry_adx']
    max_wins = result['max_wins']
    max_losses = result['max_losses']
    profit_retracements = result['profit_retracements']
    data_start = result['data_start']
    data_end = result['data_end']

    # ---- 时间范围计算 ----
    if hasattr(data_start, 'strftime'):
        start_time_str = data_start.strftime('%Y-%m-%d')
        end_time_str = data_end.strftime('%Y-%m-%d')
        actual_days = (data_end - data_start).days
    else:
        start_time_str = str(data_start)
        end_time_str = str(data_end)
        actual_days = 365
    if actual_days < 1:
        actual_days = 1

    total_days = lookback_days if lookback_days else actual_days

    net_profit = summary.get('net_profit_pct', 0)
    daily_profit = net_profit / total_days if total_days > 0 else 0
    weekly_profit = daily_profit * 7

    # ---- 市场行情（最后一根K线）----
    last_signal = strategy.get_last_signal()
    modify_flag = last_signal['modify_flag']

    market = {
        "timestamp": last_signal['timestamp'],
        "price": last_signal['price'],
        "macd_histogram": last_signal['macd_histogram'],
        "macd_dif": last_signal['macd_dif'],
        "macd_dea": last_signal.get('macd_dea', 0),
        "smoothed_macd": last_signal['smoothed_macd'],
        "adx": last_signal['adx'],
        "plus_di": last_signal['plus_di'],
        "minus_di": last_signal['minus_di'],
        "hist_weight": last_signal['hist_weight'],
        "modify_flag": modify_flag,
        "action_signal": last_signal['action_signal'],
        "atr": last_signal.get('atr', 0),
    }

    # ---- 当前持仓信息 ----
    pos_status = "空仓"
    entry_price = 0.0
    current_profit_pct = 0.0
    if trades:
        last_dir = trade_directions[-1] if trade_directions else ''
        pos_status = "多头持仓" if last_dir == 'Long' else "空头持仓"
        entry_price = round(trades[-1], 4)
        if profits:
            current_profit_pct = round(profits[-1], 2)

    current_position = {
        "status": pos_status,
        "profit_pct": current_profit_pct,
        "entry_price": entry_price,
    }

    # ---- 交易记录列表 ----
    trade_records = []
    for i in range(len(profits)):
        direction = trade_directions[i] if i < len(trade_directions) else 'Unknown'
        open_time = trade_times[i] if i < len(trade_times) else None
        open_price = trades[i] if i < len(trades) else 0.0

        if (i + 1) < len(trades):
            close_price = trades[i + 1]
            close_time = trade_times[i + 1] if (i + 1) < len(trade_times) else None
        else:
            close_price = float(signals_df['close'].iloc[-1])
            close_time = signals_df.index[-1]

        profit = profits[i]
        max_win = max_wins[i] if i < len(max_wins) else 0.0
        max_loss = max_losses[i] if i < len(max_losses) else 0.0
        entry_adx_val = entry_adx[i] if i < len(entry_adx) else 0.0
        retracement = (profit_retracements[i]
                       if i < len(profit_retracements) else 0.0)

        # 持仓时长
        duration_str = ""
        if open_time is not None and close_time is not None:
            try:
                delta = close_time - open_time
                hours = delta.total_seconds() / 3600.0
                if hours >= 24:
                    days = int(hours // 24)
                    remain_hours = int(hours % 24)
                    duration_str = f"{days}天{remain_hours}小时"
                else:
                    duration_str = f"{hours:.1f}小时"
            except Exception:
                duration_str = ""

        trade_records.append({
            "index": i + 1,
            "direction": "多头" if direction == 'Long' else "空头",
            "direction_raw": direction,
            "open_time": open_time.strftime('%Y-%m-%d %H:%M') if open_time else "N/A",
            "open_price": round(open_price, 4),
            "close_time": close_time.strftime('%Y-%m-%d %H:%M') if close_time else "N/A",
            "close_price": round(close_price, 4),
            "profit": round(profit, 2),
            "max_win": round(max_win, 2),
            "max_loss": round(max_loss, 2),
            "entry_adx": round(entry_adx_val, 1),
            "duration": duration_str,
            "retracement_pct": round(retracement, 2),
        })

    # ---- 策略统计 ----
    stats = {
        "total_trades": summary['total_trades'],
        "winning_trades": summary['winning_trades'],
        "losing_trades": summary['losing_trades'],
        "win_rate": round(summary['win_rate'], 4),
        "total_profit_pct": summary['total_profit'],
        "total_loss_pct": summary['total_loss'],
        "avg_profit_pct": summary['avg_profit'],
        "avg_loss_pct": summary['avg_loss'],
        "max_single_profit_pct": round(max(profits), 2) if profits else 0,
        "max_single_loss_pct": round(min(profits), 2) if profits else 0,
        "profit_factor": summary['profit_factor'],
        "profit_loss_ratio": summary['profit_loss_ratio'],
        "fix_money": summary['fix_money'],
        "contract_money": summary['contract_money'],
        "mix_money": summary['mix_money'],
        "total_return_pct": summary['total_return_pct'],
        "contract_return_pct": summary['contract_return_pct'],
        "max_drawdown_pct": summary['max_drawdown'],
        "calmar_ratio": summary['calmar_ratio'],
        "avg_win_dur_h": summary['avg_win_dur_h'],
        "avg_loss_dur_h": summary['avg_loss_dur_h'],
        "avg_total_dur_h": summary['avg_total_dur_h'],
        "avg_retracement_pct": summary['avg_retracement_pct'],
        "adx_mean": summary['adx_mean'],
        "adx_win_mean": summary['adx_win_mean'],
        "adx_loss_mean": summary['adx_loss_mean'],
        "adx_corr": summary['adx_corr'],
        "high_adx_win_rate": summary['high_adx_win_rate'],
        "low_adx_win_rate": summary['low_adx_win_rate'],
        "avg_hist_weight": summary['avg_hist_weight'],
        "avg_curr_weight": summary['avg_curr_weight'],
        "total_errors": summary['total_errors'],
    }

    return {
        "symbol": symbol,
        "display_name": display_name or symbol,
        "bar": bar,
        "start_time": start_time_str,
        "end_time": end_time_str,
        "total_days": round(total_days, 1),
        "daily_profit": round(daily_profit, 2),
        "weekly_profit": round(weekly_profit, 2),
        "market": market,
        "current_position": current_position,
        "trade_records": trade_records,
        "stats": stats,
    }


# ====================================================================
#  双周期 Pro3 策略（短周期交易 + 长周期方向过滤）
# ====================================================================

def get_dual_timeframe_strategy_full_data(
    df_short: pd.DataFrame,
    df_long: pd.DataFrame,
    symbol: str = "",
    short_bar: str = "1H",
    long_bar: str = "1D",
    display_name: str = "",
    lookback_days: int = None,
    **strategy_kwargs,
) -> Dict:
    """双周期 Pro3 策略 — 只在短周期和长周期方向一致时执行交易

    核心逻辑：
    1. 在长周期数据上运行 Pro3Strategy，获取最后一根K线的信号方向
    2. 将该方向作为过滤条件传入短周期 Pro3Strategy：
       - 长周期为 'rise' → 短周期只执行多头信号，忽略空头信号
       - 长周期为 'fall' → 短周期只执行空头信号，忽略多头信号
       - 长周期为 'hold/wait' → 不执行任何交易（观望）
    3. 返回与 get_strategy_full_data 兼容的数据结构，增加双周期信息

    Parameters
    ----------
    df_short : pd.DataFrame   短周期行情数据（交易信号周期）
    df_long  : pd.DataFrame   长周期行情数据（趋势方向确认）
    symbol   : str            品种代码
    short_bar: str            短周期标识，如 '1H'
    long_bar : str            长周期标识，如 '1D'
    display_name : str        品种显示名称
    lookback_days : int       回看天数
    **strategy_kwargs        传递给 Pro3Strategy 的参数

    Returns
    -------
    dict   包含 symbol, market, current_position, trade_records, stats 等
    """
    # ── 1. 在长周期数据上运行 Pro3Strategy 获取逐bar方向序列 ──
    long_direction = None       # 最终方向（仅用于显示）
    long_signal_text = '观望'
    long_modify_flag = 'wait'
    long_dir_series = None      # 逐bar方向序列

    try:
        if df_long is not None and len(df_long) > 30:
            long_strategy = Pro3Strategy(
                df_long,
                use_sar=strategy_kwargs.get('use_sar', True),
                adx_period=strategy_kwargs.get('adx_period', 9),
                macd_fast=strategy_kwargs.get('macd_fast', 12),
                macd_slow=strategy_kwargs.get('macd_slow', 26),
                macd_signal=strategy_kwargs.get('macd_signal', 9),
                base_weight=strategy_kwargs.get('base_weight', 0.7),
                trade_multiplier=strategy_kwargs.get('trade_multiplier', 10.0),
            )
            long_result = long_strategy.run()
            long_signal = long_strategy.get_last_signal()
            long_modify_flag = long_signal.get('modify_flag', 'wait')
            if long_modify_flag == 'rise':
                long_direction = 'rise'
                long_signal_text = '上涨'
            elif long_modify_flag == 'fall':
                long_direction = 'fall'
                long_signal_text = '下跌'
            else:
                long_direction = None
                long_signal_text = '观望'

            # 提取逐bar方向序列（signal 列包含每根K线的 modify_flag）
            long_signals_df = long_result.get('signals_df', None)
            if long_signals_df is not None and 'signal' in long_signals_df.columns:
                long_dir_series = long_signals_df['signal'].copy()
                # 将 hold/wait 等中间态映射为 None（不阻止交易）
                long_dir_series = long_dir_series.map(
                    lambda x: x if x in ('rise', 'fall') else None)
    except Exception as e:
        logger.warning("长周期方向计算失败: %s", e)

    # ── 1b. 将长周期逐bar方向对齐到短周期 df ──
    if long_dir_series is not None and df_short is not None:
        try:
            short_times = pd.DataFrame({'ts': pd.to_datetime(df_short.index)})
            long_times = pd.DataFrame({
                'ts': pd.to_datetime(long_dir_series.index),
                'LONG_DIRECTION': long_dir_series.values,
            })
            aligned = pd.merge_asof(
                short_times.sort_values('ts'),
                long_times.sort_values('ts'),
                on='ts', direction='backward')
            df_short = df_short.copy()
            df_short['LONG_DIRECTION'] = aligned['LONG_DIRECTION'].values
        except Exception as e:
            logger.warning("长周期方向对齐失败: %s", e)

    # ── 2. 运行短周期策略（带逐bar方向过滤，不传固定 long_direction）──
    result = get_strategy_full_data(
        df_short,
        symbol=symbol,
        bar=short_bar,
        display_name=display_name,
        lookback_days=lookback_days,
        long_direction=None,   # 不传固定方向，由 LONG_DIRECTION 列逐bar控制
        **strategy_kwargs,
    )

    # ── 3. 补充双周期信息 ──
    result['strategy'] = 'dual_pro3'
    result['short_bar'] = short_bar
    result['long_bar'] = long_bar
    result['long_direction'] = long_signal_text
    result['long_modify_flag'] = long_modify_flag

    # 在 market 中增加长周期方向
    result['market']['long_direction'] = long_signal_text

    return result
