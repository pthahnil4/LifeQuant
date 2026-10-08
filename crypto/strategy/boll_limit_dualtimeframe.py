"""
BOLL限价策略适配器 —— 供详情页使用
==========================================
封装 trend_strategy_boll_limit_scheduler_like.py 的策略运行与数据提取，
返回与 Pro3 策略相同格式的完整字典。
"""
import sys
import os
import math
import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # crypto/ 目录

# 确保项目路径（crypto/ 目录，供 trend_strategy_boll_limit_scheduler_like 等模块导入）
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _make_direct_period_fetcher(instId):
    """
    创建一个直接从 API 获取目标周期 K 线的函数，替代 BOLL 模块中
    从 1m 数据重采样的 get_period_data。
    与 pro3_dualtimeframe.py 的数据获取方式完全一致。
    """
    # 导入与 pro3_dualtimeframe.py 相同的数据获取基础设施
    strategy_dir = os.path.dirname(os.path.abspath(__file__))
    if strategy_dir not in sys.path:
        sys.path.insert(0, strategy_dir)
    from pro3_singletimeframe import _fetch_kline_data, calculate_adx

    # 缓存已获取的周期数据，避免重复 API 调用
    _cache = {}

    def fetcher(inst_id, bar):
        if bar not in _cache:
            df = _fetch_kline_data(inst_id, bar)
            df = calculate_adx(df, period=14)
            _cache[bar] = df
        return _cache[bar].copy()

    return fetcher


def get_boll_strategy_full_data(instId, short_bar="1H", long_bar="4H"):
    """
    运行BOLL限价策略并返回详情页所需的完整字典。

    参数:
        instId:    币种ID, 如 "NEAR-USDT-SWAP"
        short_bar: 短周期, 如 "15m", "1H"
        long_bar:  长周期, 如 "4H", "1D"
    """
    # 导入 BOLL 策略模块（核心逻辑全部来自此模块）
    import trend_strategy_boll_limit_scheduler_like as boll_module

    # 关闭打印输出
    old_market = boll_module.PRINT_MARKET
    old_ops = boll_module.PRINT_TRADE_OPS
    old_records = boll_module.PRINT_TRADE_RECORDS
    old_orig = boll_module.PRINT_ORIGINAL_OUTPUT
    boll_module.PRINT_MARKET = 0
    boll_module.PRINT_TRADE_OPS = 0
    boll_module.PRINT_TRADE_RECORDS = 0
    boll_module.PRINT_ORIGINAL_OUTPUT = 0

    # 保存原始 get_period_data，以便恢复
    original_get_period_data = boll_module.get_period_data

    try:
        # 用直接 API 获取替代 1m 重采样（与 pro3_dualtimeframe.py 相同方式）
        boll_module.get_period_data = _make_direct_period_fetcher(instId)

        # 运行 BOLL 限价策略（核心逻辑来自 boll_module，策略逻辑完全不变）
        latest_data, last_trade_data, atr, long_direction, df_short, df_long = \
            boll_module.get_latest_data(instId, short_bar, long_bar)

        # 从 boll_module 已运行的策略结果中提取数据，构建详情页响应
        result = _build_boll_detail_response_from_module(
            boll_module, df_short, latest_data, atr,
            long_direction, short_bar, long_bar)
        return result
    finally:
        # 恢复原始函数和打印设置
        boll_module.get_period_data = original_get_period_data
        boll_module.PRINT_MARKET = old_market
        boll_module.PRINT_TRADE_OPS = old_ops
        boll_module.PRINT_TRADE_RECORDS = old_records
        boll_module.PRINT_ORIGINAL_OUTPUT = old_orig


def _calculate_capital_curves(trade_records, base_money=100.0):
    """
    从 BOLL 策略的交易记录中计算资金曲线。
    逻辑与 trend_strategy_boll_limit_scheduler_like.py 中 get_latest_data 的资金计算完全一致。
    """
    fix_money_history = [base_money]
    contract_money_history = [base_money]
    mix_money_history = [base_money]

    for rec in trade_records:
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

    return fix_money_history, contract_money_history, mix_money_history


def _build_boll_detail_response_from_module(boll_module, df, latest_data, atr_value,
                                              long_direction, short_bar, long_bar):
    """
    从 boll_module 已运行的策略结果中构建详情页响应（BOLL 专用取数入口）。
    直接复用 boll_module.get_latest_data 返回的 df 和策略内部记录，
    不再重新运行策略，确保核心逻辑完全来自 trend_strategy_boll_limit_scheduler_like.py。
    """
    price = float(latest_data.get('close', 0))
    adx_value = float(latest_data.get('ADX', 0)) if 'ADX' in latest_data.index else 0.0

    # --- 使用 boll_module 的策略类运行 BOLL 限价策略 ---
    # 调用 boll_module.run_boll_strategy，确保核心逻辑完全来自
    # trend_strategy_boll_limit_scheduler_like.py 中的 DualTimeframeBollLimitStrategy
    strat = boll_module.run_boll_strategy(df)

    return build_range_detail_response(
        strat.trade_records, df, price, adx_value, atr_value, long_direction,
        short_bar, long_bar, strategy_name=f"BOLL限价({short_bar}/{long_bar})")


def build_range_detail_response(trade_records_raw, df, price, adx_value, atr_value,
                                long_direction, short_bar, long_bar, strategy_name,
                                entry_mult=0.0, boll_period=20, boll_dev=2.0):
    """把区间类策略（BOLL限价 / keltner_boll）的交易记录构建为详情页统一响应结构。

    与具体引擎解耦：只依赖 trade_records_raw（每笔含 dir/open_price/close_price/
    entry_time/exit_time/profit/max_win/max_loss/excluded）与短周期 df（含 ADX 列
    则用于入场 ADX 统计）。keltner_boll 的 simulate() 记录与此同构，可直接复用，
    确保两种区间策略在详情页的统计口径完全一致。
    """
    base_money = 100.0
    profits = [r['profit'] for r in trade_records_raw if not r['excluded']]

    # 计算资金曲线（与 boll_module 内部逻辑完全一致）
    fix_hist, contract_hist, mix_hist = _calculate_capital_curves(trade_records_raw, base_money)
    fix_money = fix_hist[-1]
    contract_money = contract_hist[-1]
    mix_money = mix_hist[-1]

    # --- 行情概览 ---
    long_dir_str = "上涨" if long_direction == 'rise' else ("下跌" if long_direction == 'fall' else "观望")

    # 当前持仓状态（从最后一次交易记录推断）
    status_str = '无持仓'
    profit_pct = 0.0
    entry_price_val = 0.0

    # ---- 当前待成交挂单价（区间策略未持仓时，等待触价成交的那张开仓限价单）----
    # 口径与实盘 current_range_signal 完全一致：以最新一根短K线的 BOLL 轨道与 ATR，
    # 按长周期方向与 ATR 外扩倍数算出下一笔开仓挂单价，供与实盘任务实际挂单数值核对。
    #   多头(rise)：挂多=下轨 - entry_mult×ATR ；空头(fall)：挂空=上轨 + entry_mult×ATR
    #   boll 策略 entry_mult=0 → 挂单价直接等于 BOLL 轨道值本身。
    pending_price = pending_band = pending_atr = None
    pending_dir = ''
    try:
        close_s = df['close'] if 'close' in df.columns else None
        if close_s is not None and len(close_s) >= int(boll_period):
            _mid = close_s.rolling(boll_period, min_periods=boll_period).mean()
            _std = close_s.rolling(boll_period, min_periods=boll_period).std(ddof=0)
            top_last = float(_mid.iloc[-1] + boll_dev * _std.iloc[-1])
            bot_last = float(_mid.iloc[-1] - boll_dev * _std.iloc[-1])
            atr_last = float(atr_value) if (atr_value is not None
                                            and not math.isnan(float(atr_value))) else 0.0
            _em = float(entry_mult) if entry_mult else 0.0
            if long_direction == 'rise':
                pending_dir, pending_band = '多', bot_last
                pending_price = bot_last - _em * atr_last
            elif long_direction == 'fall':
                pending_dir, pending_band = '空', top_last
                pending_price = top_last + _em * atr_last
            pending_atr = atr_last
    except Exception:
        pass

    market_dict = {
        "price": round(price, 4),
        "action_signal": long_dir_str,
        "macd_histogram": 0,
        "macd_dif": 0,
        "adx": round(float(adx_value), 4) if adx_value else 0,
        "plus_di": 0,
        "minus_di": 0,
        "smoothed_macd": 0,
        "hist_weight": 0,
        "atr": round(atr_value, 4) if atr_value and not math.isnan(atr_value) else 0,
        "is_boll": True,
        "long_direction": long_dir_str,
    }

    current_position_dict = {
        "status": status_str,
        "profit_pct": round(profit_pct, 2),
        "entry_price": round(entry_price_val, 4),
        # 待成交挂单（供与实盘任务实际挂单数值核对）：价格 / 方向 / 原始轨道 / ATR
        "pending_order_price": (round(pending_price, 4)
                                if pending_price is not None else None),
        "pending_order_dir": pending_dir,
        "pending_order_mult": round(float(entry_mult) if entry_mult else 0.0, 4),
        "pending_order_band": (round(pending_band, 4)
                               if pending_band is not None else None),
        "pending_order_atr": (round(pending_atr, 4)
                              if pending_atr is not None else None),
    }

    # --- 交易记录 ---
    trade_records = []
    for i, rec in enumerate(trade_records_raw):
        open_price = float(rec['open_price'])
        close_price = float(rec['close_price'])
        entry_time = rec.get('entry_time')
        exit_time = rec.get('exit_time')
        profit_val = float(rec['profit'])
        mw = float(rec['max_win'])
        ml = float(rec['max_loss'])
        excluded = rec.get('excluded', False)

        open_time_str = entry_time.strftime('%Y-%m-%d %H:%M') if hasattr(entry_time, 'strftime') and entry_time else ''
        close_time_str = exit_time.strftime('%Y-%m-%d %H:%M') if hasattr(exit_time, 'strftime') and exit_time else ''
        direction = '多头' if rec.get('dir') == 'rise' else '空头'

        entry_adx_val = 0.0
        if entry_time and entry_time in df.index and 'ADX' in df.columns:
            entry_adx_val = float(df.loc[entry_time, 'ADX'])

        duration_str = ''
        if entry_time and exit_time:
            delta = exit_time - entry_time
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
            # 挂单价格推导链（仅区间类策略有值，趋势策略为 None）：
            # 原始 BOLL 轨道 → 开仓时刻 ATR → 实际限价挂单价（外扩后）
            "boll_band": (round(float(rec['boll_band']), 4)
                          if rec.get('boll_band') is not None else None),
            "atr_value": (round(float(rec['atr_value']), 4)
                          if rec.get('atr_value') is not None else None),
            "order_price": (round(float(rec['order_price']), 4)
                            if rec.get('order_price') is not None else None),
            "profit": round(profit_val, 2),
            "max_win": round(mw, 2),
            "max_loss": round(ml, 2),
            "entry_adx": round(entry_adx_val, 2),
            "duration": duration_str,
            "excluded": excluded,
            "status": "剔除" if excluded else "",
        })

    # --- 统计数据 ---
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

    # 最大回撤（复投）
    max_drawdown = 0.0
    if len(contract_hist) > 1:
        peak = contract_hist[0]
        for v in contract_hist:
            if v > peak: peak = v
            dd = (peak - v) / peak * 100 if peak > 0 else 0
            if dd > max_drawdown: max_drawdown = dd

    annual_return = ((contract_money / base_money) ** (252 / total_trades) - 1) * 100 if total_trades > 0 else 0
    calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0
    profit_factor = abs(total_profit / total_loss) if total_loss < 0 else 0
    profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0

    # 持续时间统计
    durations_h, win_dur, loss_dur = [], [], []
    for rec in trade_records_raw:
        if not rec['excluded'] and rec.get('entry_time') and rec.get('exit_time'):
            d = (rec['exit_time'] - rec['entry_time']).total_seconds() / 3600
            durations_h.append(d)
            if rec['profit'] > 0: win_dur.append(d)
            else: loss_dur.append(d)
    avg_total_dur_h = sum(durations_h) / len(durations_h) if durations_h else 0
    avg_win_dur_h = sum(win_dur) / len(win_dur) if win_dur else 0
    avg_loss_dur_h = sum(loss_dur) / len(loss_dur) if loss_dur else 0

    # 回撤统计
    retracements = [r['max_win'] - r['profit'] for r in trade_records_raw if not r['excluded']]
    avg_retracement = sum(retracements) / len(retracements) if retracements else 0

    # ADX 统计
    adx_entries = []
    for rec in trade_records_raw:
        if not rec['excluded'] and rec.get('entry_time'):
            t = rec['entry_time']
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

    # 时间统计
    start_time = df.index.min()
    end_time = df.index.max()
    time_delta = end_time - start_time
    total_days = time_delta.days + time_delta.seconds / 86400
    if total_days < 1: total_days = 1.0
    total_return_pct = (fix_money - base_money) / base_money * 100
    daily_profit = total_return_pct / total_days
    weekly_profit = daily_profit * 7
    contract_return_pct = (contract_money - base_money) / base_money * 100

    # 剔除交易统计
    excluded_trades = [r for r in trade_records_raw if r['excluded']]
    excluded_count = len(excluded_trades)

    stats_dict = {
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
        "avg_hist_weight": 0,
        "avg_curr_weight": 0,
        "fix_money": round(fix_money, 2),
        "contract_money": round(contract_money, 2),
        "mix_money": round(mix_money, 2),
        "total_profit_pct": round(total_profit, 2),
        "total_loss_pct": round(total_loss, 2),
        "total_return_pct": round(total_return_pct, 2),
        "contract_return_pct": round(contract_return_pct, 2),
        "excluded_trades": excluded_count,
    }

    return {
        "strategy_name": strategy_name,
        "market": market_dict,
        "current_position": current_position_dict,
        "trade_records": trade_records,
        "stats": stats_dict,
        "start_time": start_time.strftime('%Y-%m-%d %H:%M') if hasattr(start_time, 'strftime') else str(start_time),
        "end_time": end_time.strftime('%Y-%m-%d %H:%M') if hasattr(end_time, 'strftime') else str(end_time),
        "total_days": round(total_days, 1),
        "daily_profit": round(daily_profit, 2),
        "weekly_profit": round(weekly_profit, 2),
        "dual_period": {
            "short_bar": short_bar,
            "long_bar": long_bar,
            "long_direction": long_dir_str,
        },
    }
