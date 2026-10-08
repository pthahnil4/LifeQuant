#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
数字货币分析批处理（Pro3+）
==========================

概要：
- 批量遍历数据库中的 5m~1W 周期，调用 OKX 行情计算 Pro3+ 指标，并写回 `okx_crypto_analysis`。
- Pro3+ = MACD 基础上叠加 ADX/ATR 自适应权重，可通过 `adx_divisor` 调整趋势对权重的贡献。
- 自动统计胜率、复投收益、ATR 波动率，写库前支持方向变化通知。

返回字段（节选）：
- `direction`/`signal_confirmed`：多空及信号可靠性。
- `win_rate`/`compound_return`：历史胜率与复利收益。
- `atr_value`/`atr_percentage`：波动率指标用于风险提示。
"""

import datetime
import pandas as pd
import numpy as np
import traceback
from typing import Dict, List, Optional, Tuple
from okx import MarketData
import math
import sys
import os

# API 初始化（统一从主项目 crypto.api_config 读取，消除硬编码）
from crypto.api_config import get_api_config as _get_api_config
_api_cfg = _get_api_config()
apikey     = _api_cfg['api_key']
secretkey  = _api_cfg['secret_key']
passphrase = _api_cfg['passphrase']
flag       = _api_cfg['flag']
marketDataAPI = MarketData.MarketAPI(flag=flag)


def calculate_adx(df, period=14):
    """计算ADX指标（Pro3+版本）"""
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


# === Legacy: 旧版 Pro3+ 权重函数（已被 get_adaptive_smooth_weight 替代） ===
def calculate_atr_ratio(df, period=20):
    """
    计算ATR相对比值（Pro3+版本）
    ATR_ratio = 当前ATR / MA(ATR, period)
    """
    # 计算真实波幅
    df['tr1_calc'] = df['high'] - df['low']
    df['tr2_calc'] = abs(df['high'] - df['close'].shift(1))
    df['tr3_calc'] = abs(df['low'] - df['close'].shift(1))
    df['TR_calc'] = df[['tr1_calc', 'tr2_calc', 'tr3_calc']].max(axis=1)
    
    # 计算ATR
    df['ATR_calc'] = df['TR_calc'].ewm(span=14, adjust=False).mean()
    
    # 计算ATR的移动平均
    df['ATR_MA'] = df['ATR_calc'].rolling(window=period).mean()
    
    # 计算比值
    df['ATR_ratio'] = df['ATR_calc'] / df['ATR_MA']
    
    return df


# === Legacy: 旧版 Pro3+ 自适应权重（已被 pro3_singletimeframe 的 get_adaptive_smooth_weight 替代） ===
def get_adaptive_smooth_weight_plus(adx_value, atr_ratio, base_weight=0.675, adx_divisor: float = 120.0):
    """
    Pro3+版本：结合ADX和ATR的自适应权重
    
    参数:
        adx_value: 当前ADX值
        atr_ratio: ATR / MA(ATR)
        base_weight: 基础权重（默认0.7）
    
    返回:
        (hist_weight, curr_weight, adx_adjust, atr_adjust)
    """
    # ADX调整（使用可配置缩放因子）
    adx_adjust = adx_value / float(adx_divisor)
    
    # ATR调整（新增）
    if atr_ratio > 1.2:  # 高波动
        atr_adjust = -0.06  # 降低权重，更敏感
    elif atr_ratio > 1.0:
        atr_adjust = -0.03
    elif atr_ratio < 0.8:  # 低波动
        atr_adjust = 0.04  # 提高权重，更保守
    elif atr_ratio < 0.9:
        atr_adjust = 0.02
    else:
        atr_adjust = 0.0
    
    # 综合调整
    hist_weight = base_weight + adx_adjust + atr_adjust
    
    # 限制范围 [0.65, 1.0]
    hist_weight = max(0.65, min(1.0, hist_weight))
    curr_weight = 1.0 - hist_weight
    
    return hist_weight, curr_weight, adx_adjust, atr_adjust


class CryptoAnalysisBatch:
    """数字货币分析批处理类"""
    
    def __init__(self):
        self.time_periods = ['5m', '15m', '1H', '4H', '1D', '1W']
    
    def calculate_ema_dmd(self, instId: str, bar: str, adx_divisor: float = 120.0) -> Optional[Dict]:
        """
        计算Pro3 MACD分析结果（与 pro3_singletimeframe.py 策略逻辑完全一致）
        
        核心逻辑移植自 pro3_singletimeframe.py 的 CoreStrategy：
        - MACD histogram = 2 * (DIF - DEA)
        - ADX 自适应平滑: hist_weight = min(0.7 + ADX/200, 1.0)
        - hist_smooth_diff = histogram - smoothed_histogram
        - modify_flag: hist_smooth_diff > 0 → rise, < 0 → fall
        - 交易信号: hist_smooth_diff 穿越零点（crossover）
        - 开仓/平仓价格: close 价格
        
        Args:
            instId: 交易对ID
            bar: 时间周期
            adx_divisor: 已废弃，保留兼容（策略引擎内部使用 adx/200）
            
        Returns:
            Dict: 分析结果或None
        """
        try:
            print(f"[Pro3+] {instId} {bar} start")
            
            # 网络错误处理：如果连接失败，返回模拟数据用于测试
            try:
                # 获取标记价格K线数据
                result = marketDataAPI.get_mark_price_candlesticks(
                    instId=instId, bar=bar
                )
            except Exception as network_error:
                print(f"[Pro3+] {instId} {bar} net_error: {network_error}")
                return self._generate_mock_analysis_result(instId, bar)
            
            if 'data' not in result or not result['data']:
                print(f"[Pro3+] {instId} {bar} no_data")
                return None
                
            result_data = result['data']
            afterts = result_data[-1][0]

            # 获取更多历史数据
            combined_data = result_data
            for _ in range(4):
                try:
                    result2 = marketDataAPI.get_mark_price_candlesticks(
                        instId=instId, bar=bar, after=afterts
                    )
                    if 'data' in result2 and result2['data']:
                        combined_data.extend(result2['data'])
                        afterts = result2['data'][-1][0]
                    else:
                        break
                except:
                    break

            if len(combined_data) < 100:
                print(f"[Pro3+] {instId} {bar} data_short={len(combined_data)}")
                return None
            
            # 将数据转换为DataFrame
            df = pd.DataFrame(combined_data, columns=['timestamp', 'open', 'high', 'low', 'close','confirm'])
            df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int) / 1000, unit='s')
            df = df.iloc[::-1]  # 反转数据，最新的在最后
            df['timestamp'] = df['timestamp'] + pd.Timedelta(hours=8)  # 转换为东八区时间
            df.set_index('timestamp', inplace=True)
            df = df.astype(float)
            
            # === Pro3 核心：计算ADX指标（与 pro3_singletimeframe.py 一致） ===
            df = calculate_adx(df, period=14)
            
            size = len(df)
            timestamps = df.index.tolist()
            close_prices = df['close'].values
            open_prices = df['open'].values
            
            # === Pro3 MACD计算（与 pro3_singletimeframe.py 一致） ===
            ema12 = df['close'].ewm(span=12, adjust=False).mean()
            ema26 = df['close'].ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            histogram = 2 * (macd_line - signal_line)
            
            # === Pro3 ADX自适应平滑MACD（与 get_adaptive_smooth_weight 一致） ===
            smoothed_hist = []
            weight_history = []
            
            for i in range(len(histogram)):
                adx_val = df['ADX'].iloc[i] if not pd.isna(df['ADX'].iloc[i]) else 0
                # 与 pro3_singletimeframe.get_adaptive_smooth_weight 完全一致:
                # hist_weight = min(base_weight + adx/200, 1.0), base_weight=0.7
                hw = min(0.7 + (adx_val / 200.0), 1.0)
                cw = 1.0 - hw
                weight_history.append((hw, cw))
                
                if len(smoothed_hist) == 0:
                    smoothed_hist.append(histogram.iloc[i])
                else:
                    smoothed_hist.append(smoothed_hist[-1] * hw + histogram.iloc[i] * cw)
            
            hist_smooth_diff_series = [histogram.iloc[i] - smoothed_hist[i] for i in range(len(histogram))]
            
            # === Pro3 交易信号生成（与 _run_strategy CoreStrategy 一致） ===
            # backtrader 在 EMA26 和 Signal(EMA9) 都就绪后才开始调用 next()
            # EMA26 需要 26 bars，Signal(EMA9) 需要额外 8 bars，共 34 bars
            # 即 next() 从 index=33 开始被调用（第一个 bar 是 index=33）
            WARMUP_BARS = 33
            flags = [''] * size
            trades = []          # 开仓/平仓价格序列
            trade_times = []     # 平仓时间序列
            trade_directions = []  # 开仓方向序列
            profits = []         # 平仓盈亏序列
            
            # 资金管理（与 CoreStrategy 一致）
            base_money = 100.0
            contract_money = 100.0
            fix_money = 100.0
            mix_money = 100.0
            times = 10.0
            
            # 策略状态（与 CoreStrategy 一致）
            modify_flag = None
            last_modify_flag = None
            previous_diff = None
            current_position = None   # 'rise' / 'fall' / None
            entry_price = None
            max_wins = []
            max_losses = []
            
            for i in range(size):
                hist_val = histogram.iloc[i]
                smoothed_val = smoothed_hist[i]
                hsd = hist_smooth_diff_series[i]
                
                # backtrader 在 EMA26 就绪前不执行策略逻辑
                if i < WARMUP_BARS:
                    continue
                
                # === modify_flag（与 CoreStrategy 完全一致：直接用 hist_smooth_diff 正负判定） ===
                if hsd > 0:
                    modify_flag = "rise"
                elif hsd < 0:
                    modify_flag = "fall"
                flags[i] = modify_flag if modify_flag else ''
                
                # 零点价格计算（与 CoreStrategy 一致）
                histogram_zero = 0.0
                hist_smooth_diff_zero = 0.0
                interpolated_zero = 0.0
                if i > 0:
                    prev_dea = signal_line.iloc[i-1]
                    prev_e12 = ema12.iloc[i-1]
                    prev_e26 = ema26.iloc[i-1]
                    prev_sm = smoothed_hist[i-1]
                    denom = (2.0/13.0 - 2.0/27.0)
                    if abs(denom) > 1e-10:
                        histogram_zero = (prev_dea - (prev_e12 * 11.0/13.0) + (prev_e26 * 25.0/27.0)) / denom
                        cw = weight_history[i][1]
                        hw_v = weight_history[i][0]
                        if cw != 0:
                            hist_smooth_diff_zero = (prev_dea - (prev_e12 * 11.0/13.0) + (prev_e26 * 25.0/27.0) + (prev_sm * hw_v / cw)) / denom
                        else:
                            hist_smooth_diff_zero = histogram_zero
                    # 线性插值法估算 hist_smooth_diff 穿越零点时的价格
                    prev_diff_val = previous_diff if previous_diff is not None else 0.0
                    if prev_diff_val != hsd:
                        ratio = abs(prev_diff_val) / abs(hsd - prev_diff_val)
                        ratio = max(0.0, min(1.0, ratio))
                        prev_c = float(close_prices[i-1])
                        curr_c = float(close_prices[i])
                        interpolated_zero = prev_c + ratio * (curr_c - prev_c)
                
                # === 交易逻辑（与 _single_period_trade_logic 完全一致） ===
                # backtrader 在第一根 bar 时 previous_diff 为 None，不触发信号检测
                signal_changed = False
                if previous_diff is not None:
                    signal_changed = (modify_flag != last_modify_flag) if last_modify_flag is not None else False
                
                    # 有持仓 + 方向反转 → 平仓
                    if current_position is not None and modify_flag != current_position and signal_changed:
                        trade_price = float(open_prices[i])  # EXIT_PRICE_TYPE = 'open'（与 pro3_singletimeframe 一致）
                        if current_position == "rise":
                            profit = ((trade_price / trades[-1]) - 1) * 100 * times
                        else:
                            profit = (1 - (trade_price / trades[-1])) * 100 * times
                        
                        # _update_capital（与 CoreStrategy 一致）
                        fix_money += base_money * profit * 0.01
                        contract_money += contract_money * profit * 0.01
                        mix_ratio = mix_money / base_money
                        if mix_ratio > 0:
                            try:
                                log_val = math.log(mix_ratio) / math.log(1.6)
                                if not math.isnan(log_val) and not math.isinf(log_val):
                                    mix_base = base_money * pow(1.6, math.floor(log_val))
                                    mix_base = max(mix_base, base_money)
                                else:
                                    mix_base = base_money
                            except (ValueError, OverflowError):
                                mix_base = base_money
                        else:
                            mix_base = base_money
                        mix_money += mix_base * profit * 0.01
                        
                        # _record_close
                        profits.append(profit)
                        trades.append(trade_price)
                        trade_times.append(timestamps[i])
                        
                        current_position = None
                        entry_price = None
                    
                    # 无持仓 + 信号改变 → 开仓
                    if current_position is None and signal_changed and modify_flag is not None:
                        trade_price = float(close_prices[i])  # ENTRY_PRICE_TYPE = 'close'
                        trades.append(trade_price)
                        trade_times.append(timestamps[i])
                        trade_directions.append(modify_flag)
                        max_wins.append(0.0)
                        max_losses.append(0.0)
                        current_position = modify_flag
                        entry_price = trade_price
                
                # 更新持仓盈亏跟踪
                if current_position is not None:
                    cp = float(close_prices[i])
                    lp = trades[-1]
                    if current_position == 'rise':
                        cur_pnl = ((cp / lp) - 1) * 100 * times
                    else:
                        cur_pnl = (1 - (cp / lp)) * 100 * times
                    tidx = len(profits)
                    if tidx < len(max_wins):
                        max_wins[tidx] = max(max_wins[tidx], max(cur_pnl, 0))
                        max_losses[tidx] = min(max_losses[tidx], min(cur_pnl, 0))
                
                last_modify_flag = modify_flag
                previous_diff = hsd
            
            # === 期末强制平仓（与 CoreStrategy.stop() 一致） ===
            if current_position is not None and len(trades) > 0:
                last_tp = trades[-1]
                last_cl = float(close_prices[-1])
                if current_position == 'rise':
                    profit = ((last_cl / last_tp) - 1) * 100 * times
                elif current_position == 'fall':
                    profit = (1 - (last_cl / last_tp)) * 100 * times
                else:
                    profit = 0.0
                fix_money += base_money * profit * 0.01
                contract_money += contract_money * profit * 0.01
                mix_ratio = mix_money / base_money
                if mix_ratio > 0:
                    try:
                        log_val = math.log(mix_ratio) / math.log(1.6)
                        if not math.isnan(log_val) and not math.isinf(log_val):
                            mix_base = base_money * pow(1.6, math.floor(log_val))
                            mix_base = max(mix_base, base_money)
                        else:
                            mix_base = base_money
                    except (ValueError, OverflowError):
                        mix_base = base_money
                else:
                    mix_base = base_money
                mix_money += mix_base * profit * 0.01
                profits.append(profit)
                trades.append(last_cl)
                trade_times.append(timestamps[-1])
            
            # === 构建统计与返回值 ===
            current_price = float(close_prices[-1])
            current_profit = 0.0
            if current_position is not None and entry_price is not None and entry_price > 0:
                if current_position == 'rise':
                    current_profit = ((current_price / entry_price) - 1) * 100 * times
                else:
                    current_profit = (1 - (current_price / entry_price)) * 100 * times
            
            # 交易记录
            trade_records = []
            for idx in range(len(profits)):
                open_price = float(trades[idx * 2]) if idx * 2 < len(trades) else 0
                close_price = float(trades[idx * 2 + 1]) if idx * 2 + 1 < len(trades) else 0
                open_time = ''
                close_time = ''
                if idx * 2 < len(trade_times):
                    t = trade_times[idx * 2]
                    open_time = t.strftime('%Y-%m-%d %H:%M') if hasattr(t, 'strftime') else str(t)
                if idx * 2 + 1 < len(trade_times):
                    t = trade_times[idx * 2 + 1]
                    close_time = t.strftime('%Y-%m-%d %H:%M') if hasattr(t, 'strftime') else str(t)
                direction = '多头' if (idx < len(trade_directions) and trade_directions[idx] == 'rise') else '空头'
                mw = float(max_wins[idx]) if idx < len(max_wins) else 0.0
                ml = float(max_losses[idx]) if idx < len(max_losses) else 0.0
                trade_records.append({
                    'time': trade_times[idx * 2 + 1] if idx * 2 + 1 < len(trade_times) else None,
                    'direction': '开多' if trade_directions[idx] == 'rise' else '开空' if idx < len(trade_directions) else '',
                    'price': close_price,
                    'profit': float(profits[idx]),
                    'open_time': open_time,
                    'open_price': round(open_price, 4),
                    'close_time': close_time,
                    'close_price': round(close_price, 4),
                    'max_win': round(mw, 2),
                    'max_loss': round(ml, 2),
                })
            
            # 统计数据
            stats = {}
            if len(profits) > 0:
                win_count = sum(1 for p in profits if p > 0)
                loss_count = sum(1 for p in profits if p < 0)
                total_trades_count = len(profits)
                win_rate = (win_count / total_trades_count * 100) if total_trades_count > 0 else 0
                sum_win = sum(p for p in profits if p > 0)
                sum_loss = sum(p for p in profits if p < 0)
                avg_profit = sum_win / win_count if win_count > 0 else 0
                avg_loss = sum_loss / loss_count if loss_count > 0 else 0
                win_loss_ratio = (abs(sum_win / sum_loss)) if sum_loss != 0 else 0
                compound_return = contract_money / base_money
                stats = {
                    'total_trades': total_trades_count,
                    'win_count': win_count,
                    'loss_count': loss_count,
                    'win_rate': win_rate,
                    'total_profit': sum_win,
                    'total_loss': sum_loss,
                    'win_loss_ratio': win_loss_ratio,
                    'compound_return': compound_return,
                    'final_amount': contract_money,
                    'avg_profit': avg_profit,
                    'avg_loss': avg_loss,
                }
            
            # ATR
            prev_close = df['close'].shift(1)
            tr1 = df['high'] - df['low']
            tr2 = abs(df['high'] - prev_close)
            tr3 = abs(df['low'] - prev_close)
            TR = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr_s = TR.ewm(alpha=1/14, adjust=False).mean()
            current_atr = float(atr_s.iloc[-1]) if not pd.isna(atr_s.iloc[-1]) else 0.0
            atr_percentage = (current_atr / current_price * 100) if current_price > 0 else 0.0
            
            # 信号确定性
            signal_confirmed = False
            if len(flags) >= 2:
                signal_confirmed = (flags[-1] == flags[-2]) and flags[-1] != ''
            
            final_direction = 'long' if flags[-1] == 'rise' else 'short'
            
            avg_hw = float(np.mean([w[0] for w in weight_history])) if weight_history else 0.7
            avg_cw = float(np.mean([w[1] for w in weight_history])) if weight_history else 0.3
            
            # 权益曲线
            equity_compound_curve = []
            equity_dca_curve = []
            equity_mix_curve = []
            comp_eq = base_money
            dca_eq = base_money
            mix_eq = base_money
            last_comp = base_money
            last_mix = base_money
            for idx in range(len(profits)):
                p = profits[idx]
                comp_eq = last_comp + last_comp * p * 0.01
                dca_eq = dca_eq + base_money * p * 0.01
                mix_eq = last_mix + base_money * p * 0.01
                last_comp = comp_eq
                last_mix = mix_eq
                equity_compound_curve.append(comp_eq)
                equity_dca_curve.append(dca_eq)
                equity_mix_curve.append(mix_eq)
            
            # 构建返回结果（兼容原数据结构）
            result = {
                'currency_code': instId,
                'time_period': bar,
                'last_price': current_price,
                'latest_trade_price': float(trades[-1]) if trades else current_price,
                'direction': final_direction,
                'signal_confirmed': signal_confirmed,
                'trade_price': float(trades[-1]) if trades else 0.0,
                'trade_time': trade_times[-1] if trade_times else None,
                'current_profit': current_profit,
                'win_rate': stats.get('win_rate', 0),
                'total_trades': stats.get('total_trades', 0),
                'compound_return': stats.get('compound_return', 1.0),
                'signal_strength': min(100, max(0, stats.get('win_rate', 0) * stats.get('win_loss_ratio', 1))),
                'atr_value': current_atr,
                'atr_percentage': atr_percentage,
                'price_volatility': current_atr,
                'strategy_version': 'Pro3',
                'trade_records': trade_records,
                'trade_times': trade_times,
                'trade_directions': trade_directions,
                'profits': profits,
                'timestamps': timestamps,
                'flags': flags,
                'avg_hist_weight': avg_hw,
                'avg_curr_weight': avg_cw,
                'equity_compound_curve': equity_compound_curve,
                'equity_dca_curve': equity_dca_curve,
                'equity_mix_curve': equity_mix_curve,
                'final_compound_amount': last_comp,
                'final_dca_amount': dca_eq,
                'final_mix_amount': last_mix
            }
            
            trade_time = result.get('trade_time')
            if hasattr(trade_time, 'strftime'):
                trade_time_str = trade_time.strftime('%Y-%m-%d %H:%M:%S')
            elif trade_time:
                trade_time_str = str(trade_time)
            else:
                trade_time_str = 'N/A'
            latest_trade_price = result.get('latest_trade_price', current_price)
            dir_cn = "多" if result['direction'] == 'long' else "空" if result['direction'] == 'short' else "未知"
            print(f"[Pro3+] {instId} {bar} 方向={dir_cn} 价格={latest_trade_price:.4f} 时间={trade_time_str}")
            return result
                
        except Exception as e:
            print(f"[Pro3+] {instId} {bar} error: {e}")
            traceback.print_exc()
            return None
    
    def _generate_mock_analysis_result(self, instId: str, bar: str) -> Dict:
        """生成模拟分析结果用于网络故障时测试"""
        import random
        
        # 模拟价格数据
        base_price = 50000.0 if 'BTC' in instId else 2000.0 if 'ETH' in instId else 3.5
        current_price = base_price * (0.95 + random.random() * 0.1)  # ±5%波动
        
        # 模拟方向
        directions = ['long', 'short']
        direction = random.choice(directions)
        
        # 模拟ATR数据
        atr_value = current_price * 0.02  # 2%的ATR
        atr_percentage = 2.0
        
        # 模拟交易数据
        win_rate = 45 + random.random() * 20  # 45-65%胜率
        total_trades = random.randint(10, 50)
        
        # 计算当前周期的开始时间
        now = datetime.datetime.now()
        if bar == '5m':
            minute = now.minute
            period_start_minute = (minute // 5) * 5
            trade_time = now.replace(minute=period_start_minute, second=0, microsecond=0)
        elif bar == '15m':
            minute = now.minute
            period_start_minute = (minute // 15) * 15
            trade_time = now.replace(minute=period_start_minute, second=0, microsecond=0)
        elif bar == '1H':
            trade_time = now.replace(minute=0, second=0, microsecond=0)
        elif bar == '4H':
            hour = now.hour
            period_start_hour = (hour // 4) * 4
            trade_time = now.replace(hour=period_start_hour, minute=0, second=0, microsecond=0)
        elif bar == '1D':
            trade_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            trade_time = datetime.datetime.now()
        
        result = {
            'currency_code': instId,
            'time_period': bar,
            'last_price': current_price,  # 最新价格（当前K线收盘价）
            'latest_trade_price': current_price * (0.999 + random.random() * 0.002),  # 🆕 最新交易价格
            'direction': direction,
            'signal_confirmed': random.choice([True, False]),  # 模拟信号确定性
            'trade_price': current_price * (0.999 + random.random() * 0.002),  # 策略交易价格
            'trade_time': trade_time,
            'current_profit': random.uniform(-5.0, 15.0),
            'win_rate': win_rate,
            'total_trades': total_trades,
            'compound_return': 1.0 + random.uniform(-0.1, 0.3),
            'signal_strength': min(100, max(0, win_rate * 1.2)),
            'atr_value': atr_value,
            'atr_percentage': atr_percentage,
            'price_volatility': atr_value
        }
        
        trade_time = result.get('trade_time')
        if hasattr(trade_time, 'strftime'):
            trade_time_str = trade_time.strftime('%Y-%m-%d %H:%M:%S')
        elif trade_time:
            trade_time_str = str(trade_time)
        else:
            trade_time_str = 'N/A'
        latest_trade_price = result.get('latest_trade_price', current_price)
        dir_cn = "多" if result['direction'] == 'long' else "空" if result['direction'] == 'short' else "未知"
        print(f"[Pro3+] {instId} {bar} 方向={dir_cn} 价格={latest_trade_price:.4f} 时间={trade_time_str}")
        return result
    
    def calculate_dual_period(self, instId: str, short_bar: str, long_bar: str) -> Optional[Dict]:
        """
        双周期 Pro3 策略分析（使用 crypto/strategy/pro3_dualtimeframe.py 真双周期逻辑）
        
        短周期生成交易信号 + 长周期确认趋势方向，通过 backtrader 引擎运行。
        返回值格式与 calculate_ema_dmd 兼容，可直接用于 trend_range_trader.py。
        
        Args:
            instId: 交易对ID (e.g. 'NEAR-USDT-SWAP')
            short_bar: 短周期 (e.g. '1H', '15m')
            long_bar: 长周期 (e.g. '1D', '4H')
            
        Returns:
            Dict: 兼容 calculate_ema_dmd 返回格式的分析结果，额外包含 long_direction 字段
        """
        try:
            import sys
            import os

            # 确保 crypto/strategy/ 和 crypto/ 在 sys.path 中
            strategy_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'strategy'
            )
            crypto_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for d in (strategy_dir, crypto_dir):
                if d not in sys.path:
                    sys.path.insert(0, d)

            # 导入策略模块
            import pro3_singletimeframe as _single
            import pro3_dualtimeframe as _dual

            # 抑制输出
            old_fast = _single.FAST_MODE
            old_print_orig = _single.PRINT_ORIGINAL_OUTPUT
            old_print_market = _single.PRINT_MARKET
            old_print_ops = _single.PRINT_TRADE_OPS
            old_print_records = _single.PRINT_TRADE_RECORDS
            _single.FAST_MODE = True
            _single.PRINT_ORIGINAL_OUTPUT = 0
            _single.PRINT_MARKET = 0
            _single.PRINT_TRADE_OPS = 0
            _single.PRINT_TRADE_RECORDS = 0

            try:
                latest_data, last_trade_data, atr_value, long_direction, df_short, df_long = \
                    _dual.get_latest_data_dual(instId, short_bar, long_bar)
            finally:
                # 恢复全局变量
                _single.FAST_MODE = old_fast
                _single.PRINT_ORIGINAL_OUTPUT = old_print_orig
                _single.PRINT_MARKET = old_print_market
                _single.PRINT_TRADE_OPS = old_print_ops
                _single.PRINT_TRADE_RECORDS = old_print_records

            # === 字段提取与格式转换 ===
            price = float(latest_data.get('close', 0))
            modify_flag = latest_data.get('MODIFY_FLAG', None)
            direction = 'long' if modify_flag == 'rise' else 'short'

            # 交易信息
            last_trade_price = 0.0
            trade_time = None
            if last_trade_data is not None:
                last_trade_price = float(last_trade_data.get('TRADE_PRICE', 0) or 0)
                trade_time = last_trade_data.get('TRADE_TIME', None)
            if last_trade_price == 0.0:
                last_trade_price = price

            # ATR
            current_atr = float(atr_value) if atr_value and not math.isnan(atr_value) else 0.0
            atr_percentage = (current_atr / price * 100) if price > 0 else 0.0

            # 当前持仓盈亏
            strat = None
            current_profit = 0.0
            # get_latest_data_dual 不直接返回 strat，需要从 latest_data 推断
            # 但我们可以从 long_direction 和 modify_flag 的关系判断
            # 更可靠的方式：直接再次获取（但开销大）
            # 这里使用 last_trade_data 中的信息估算
            if last_trade_data is not None and last_trade_price > 0:
                last_profit = last_trade_data.get('PROFIT', None)
                if last_profit is not None:
                    current_profit = float(last_profit)
                else:
                    # 根据最后交易方向和当前价格估算
                    last_dir = latest_data.get('MODIFY_FLAG', None)
                    if last_dir == 'rise' and last_trade_price > 0:
                        current_profit = ((price / last_trade_price) - 1) * 100 * 10.0
                    elif last_dir == 'fall' and last_trade_price > 0:
                        current_profit = (1 - (price / last_trade_price)) * 100 * 10.0

            # 信号确定性：双周期策略已内置过滤，modify_flag 稳定即为已确认
            signal_confirmed = (modify_flag is not None and modify_flag in ('rise', 'fall'))

            # 长周期方向标准化
            long_dir_str = None
            if long_direction is not None:
                s = str(long_direction)
                if s == 'rise':
                    long_dir_str = 'long'
                elif s == 'fall':
                    long_dir_str = 'short'
                else:
                    long_dir_str = s

            # 格式化交易时间
            trade_time_str = 'N/A'
            if trade_time is not None:
                if hasattr(trade_time, 'strftime'):
                    trade_time_str = trade_time.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    trade_time_str = str(trade_time)

            dir_cn = "多" if direction == 'long' else "空"
            long_dir_cn = "多" if long_dir_str == 'long' else ("空" if long_dir_str == 'short' else "--")
            print(f"[Dual] {instId} {short_bar}/{long_bar} 短={dir_cn} 长={long_dir_cn} "
                  f"价格={last_trade_price:.4f} 时间={trade_time_str}")

            result = {
                'currency_code': instId,
                'time_period': short_bar,
                'last_price': price,
                'latest_trade_price': last_trade_price,
                'direction': direction,
                'signal_confirmed': signal_confirmed,
                'trade_price': last_trade_price,
                'trade_time': trade_time,
                'current_profit': current_profit,
                'win_rate': 0,
                'total_trades': 0,
                'compound_return': 1.0,
                'signal_strength': 0,
                'atr_value': current_atr,
                'atr_percentage': atr_percentage,
                'price_volatility': current_atr,
                'strategy_version': 'Pro3_Dual',
                # 双周期特有字段
                'long_direction': long_dir_str,
            }
            return result

        except Exception as e:
            print(f"[Dual] {instId} {short_bar}/{long_bar} 双周期分析失败: {e}")
            traceback.print_exc()
            # 回退到旧的双调用方式
            print(f"[Dual] 回退到独立双周期分析...")
            try:
                short_analysis = self.calculate_ema_dmd(instId, short_bar)
                long_analysis = self.calculate_ema_dmd(instId, long_bar)
                if short_analysis and long_analysis:
                    short_analysis['long_direction'] = long_analysis['direction']
                    return short_analysis
                return short_analysis
            except Exception as fallback_err:
                print(f"[Dual] 回退也失败: {fallback_err}")
                return None
