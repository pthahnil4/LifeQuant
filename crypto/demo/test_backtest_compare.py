#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测对比测试：crypto_analysis_batch.calculate_ema_dmd vs pro3_singletimeframe (backtrader)
对比两者在相同数据上的交易信号、方向、盈亏等关键指标
"""
import sys
import os

# 确保路径
_CRYPTO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # crypto/
_PROJECT_ROOT = os.path.dirname(_CRYPTO_DIR)  # project root
for p in [
    os.path.join(_CRYPTO_DIR, 'strategy'),
    os.path.join(_CRYPTO_DIR, 'task'),
    _CRYPTO_DIR,
    _PROJECT_ROOT,
]:
    if p not in sys.path:
        sys.path.insert(0, p)

import pro3_singletimeframe as _single


def run_backtrader_strategy(instId, bar):
    """使用 pro3_singletimeframe.py 的 backtrader 引擎运行策略"""
    old_fast = _single.FAST_MODE
    old_print_orig = _single.PRINT_ORIGINAL_OUTPUT
    old_print_market = _single.PRINT_MARKET
    old_print_ops = _single.PRINT_TRADE_OPS
    old_print_records = _single.PRINT_TRADE_RECORDS
    _single.FAST_MODE = False
    _single.PRINT_ORIGINAL_OUTPUT = 1
    _single.PRINT_MARKET = 0
    _single.PRINT_TRADE_OPS = 0
    _single.PRINT_TRADE_RECORDS = 0

    try:
        df = _single._fetch_kline_data(instId, bar)
        df = _single.calculate_adx(df, period=14)
        _single._init_df_columns(df)
        strat = _single._run_strategy(df, long_direction=None, is_dual_period=False)
    finally:
        _single.FAST_MODE = old_fast
        _single.PRINT_ORIGINAL_OUTPUT = old_print_orig
        _single.PRINT_MARKET = old_print_market
        _single.PRINT_TRADE_OPS = old_print_ops
        _single.PRINT_TRADE_RECORDS = old_print_records

    # 提取策略结果
    latest_data = df.iloc[-1].copy()
    _single._attach_prev_adx(df, latest_data)
    atr_value = _single._calculate_atr(df)
    last_trade_data = _single._build_last_trade_data(strat, df)

    price = float(latest_data.get('close', 0))
    modify_flag = latest_data.get('MODIFY_FLAG', None)
    direction = 'long' if modify_flag == 'rise' else 'short'

    # 提取交易记录
    bt_trades = []
    for i in range(len(strat.profits)):
        open_price = float(strat.trades[i * 2]) if i * 2 < len(strat.trades) else 0
        close_price = float(strat.trades[i * 2 + 1]) if i * 2 + 1 < len(strat.trades) else 0
        trade_dir = '多头' if (i < len(strat.trade_dirs) and strat.trade_dirs[i] == 'rise') else '空头'
        bt_trades.append({
            'direction': trade_dir,
            'open_price': round(open_price, 4),
            'close_price': round(close_price, 4),
            'profit': round(float(strat.profits[i]), 2),
            'open_time': strat.trade_times[i * 2] if i * 2 < len(strat.trade_times) else None,
            'close_time': strat.trade_times[i * 2 + 1] if i * 2 + 1 < len(strat.trade_times) else None,
        })

    # flags
    bt_flags = []
    if 'MODIFY_FLAG' in df.columns:
        bt_flags = [v if v else '' for v in df['MODIFY_FLAG'].tolist()]

    return {
        'direction': direction,
        'modify_flag': modify_flag,
        'price': price,
        'atr': atr_value,
        'latest_data': latest_data,
        'last_trade_data': last_trade_data,
        'trade_records': bt_trades,
        'total_trades': len(strat.profits),
        'contract_money': strat.contract_money,
        'fix_money': strat.fix_money,
        'mix_money': strat.mix_money,
        'base_money': strat.base_money,
        'flags': bt_flags,
        'current_position': strat.current_position,
        'entry_price': strat.entry_price,
    }


def run_batch_strategy(instId, bar):
    """使用 crypto_analysis_batch.py 的 calculate_ema_dmd 运行策略"""
    from crypto_analysis_batch import CryptoAnalysisBatch
    batch = CryptoAnalysisBatch()
    result = batch.calculate_ema_dmd(instId, bar)
    return result


def compare_results(bt_result, batch_result, instId, bar):
    """对比两个策略的结果"""
    print(f"\n{'='*80}")
    print(f"回测对比结果: {instId} {bar}")
    print(f"{'='*80}")

    # 1. 方向对比
    bt_dir = bt_result['direction']
    batch_dir = batch_result['direction']
    dir_match = bt_dir == batch_dir
    print(f"\n[方向]")
    print(f"  Backtrader:  {bt_dir} (modify_flag={bt_result['modify_flag']})")
    print(f"  Batch:       {batch_dir}")
    print(f"  匹配: {'[OK]' if dir_match else '[FAIL]'}")

    # 2. 价格对比
    bt_price = bt_result['price']
    batch_price = batch_result['last_price']
    print(f"\n[当前价格]")
    print(f"  Backtrader:  {bt_price:.4f}")
    print(f"  Batch:       {batch_price:.4f}")
    print(f"  匹配: {'[OK]' if abs(bt_price - batch_price) < 0.01 else '[FAIL]'}")

    # 3. ATR对比
    bt_atr = bt_result['atr']
    batch_atr = batch_result['atr_value']
    atr_diff = abs(bt_atr - batch_atr) if bt_atr and batch_atr else 0
    print(f"\n[ATR]")
    print(f"  Backtrader:  {bt_atr:.6f}")
    print(f"  Batch:       {batch_atr:.6f}")
    print(f"  差异: {atr_diff:.6f} {'[OK]' if atr_diff < 0.001 else '[WARN]'}")

    # 4. 交易统计对比
    print(f"\n[交易统计对比]")
    print(f"  {'指标':<15} {'Backtrader':>12} {'Batch':>12} {'匹配':>6}")
    print(f"  {'-'*50}")
    bt_total = bt_result.get('total_trades', 0)
    batch_total = batch_result.get('total_trades', 0)
    print(f"  {'总交易次数':<15} {bt_total:>12} {batch_total:>12} {'[OK]' if bt_total == batch_total else '[FAIL]':>6}")

    bt_compound = bt_result.get('contract_money', 100) / bt_result.get('base_money', 100)
    batch_compound = batch_result.get('compound_return', 1.0)
    compound_match = abs(bt_compound - batch_compound) < 0.01
    print(f"  {'复投收益':<15} {bt_compound:>12.4f} {batch_compound:>12.4f} {'[OK]' if compound_match else '[FAIL]':>6}")

    bt_fix = bt_result.get('fix_money', 100)
    batch_fix = batch_result.get('final_dca_amount', 100)
    fix_match = abs(bt_fix - batch_fix) < 0.1
    print(f"  {'定投收益':<15} {bt_fix:>12.2f} {batch_fix:>12.2f} {'[OK]' if fix_match else '[FAIL]':>6}")

    bt_mix = bt_result.get('mix_money', 100)
    batch_mix = batch_result.get('final_mix_amount', 100)
    mix_match = abs(bt_mix - batch_mix) < 0.1
    print(f"  {'混合收益':<15} {bt_mix:>12.2f} {batch_mix:>12.2f} {'[OK]' if mix_match else '[FAIL]':>6}")

    batch_win_rate = batch_result.get('win_rate', 0)
    print(f"  {'胜率':<15} {'':>12} {batch_win_rate:>11.2f}%")
    batch_current_profit = batch_result.get('current_profit', 0)
    print(f"  {'当前持仓盈亏':<15} {'':>12} {batch_current_profit:>11.2f}%")

    # 5. 最新交易对比
    bt_last = bt_result.get('last_trade_data')
    if bt_last is not None:
        bt_trade_price = float(bt_last.get('TRADE_PRICE', 0))
        bt_trade_time = bt_last.get('TRADE_TIME', 'N/A')
        print(f"\n[Backtrader最新交易]")
        print(f"  价格: {bt_trade_price:.4f}")
        print(f"  时间: {bt_trade_time}")
    else:
        print(f"\n[Backtrader最新交易] 无交易")

    batch_trade_price = batch_result.get('trade_price', 0)
    batch_trade_time = batch_result.get('trade_time', 'N/A')
    print(f"\n[Batch最新交易]")
    print(f"  价格: {batch_trade_price:.4f}")
    print(f"  时间: {batch_trade_time}")

    # 6. 交易记录详细对比
    bt_records = bt_result.get('trade_records', [])
    batch_records = batch_result.get('trade_records', [])
    print(f"\n[Backtrader交易记录（最后5笔）]")
    for rec in bt_records[-5:]:
        print(f"  {rec['direction']} | 开{rec['open_price']:.4f} → "
              f"平{rec['close_price']:.4f} | 盈亏: {rec['profit']:.2f}%")
    print(f"\n[Batch交易记录（最后5笔）]")
    for rec in batch_records[-5:]:
        print(f"  {rec.get('direction', '')} | 开{rec.get('open_price', 0):.4f} → "
              f"平{rec.get('close_price', 0):.4f} | 盈亏: {rec.get('profit', 0):.2f}%")

    # 7. 逐笔对比
    max_compare = min(len(bt_records), len(batch_records))
    if max_compare > 0:
        print(f"\n[逐笔对比（共{max_compare}笔）]")
        all_match = True
        for i in range(max_compare):
            bt_r = bt_records[i]
            ba_r = batch_records[i]
            profit_diff = abs(bt_r['profit'] - ba_r['profit'])
            match = profit_diff < 0.1
            if not match:
                all_match = False
            if i < 5 or i >= max_compare - 5 or not match:  # 显示前5笔、后5笔和不匹配的
                print(f"  #{i+1} BT:{bt_r['profit']:>8.2f}% | Batch:{ba_r['profit']:>8.2f}% | "
                      f"差异:{profit_diff:>6.2f} {'[OK]' if match else '[FAIL]'}")
            elif i == 5:
                print(f"  ... (省略中间 {max_compare - 10} 笔) ...")
        if all_match:
            print(f"  [OK] 所有交易盈亏完全一致！")
        else:
            print(f"  [WARN] 部分交易存在差异")

    # 8. flags对比
    bt_flags = bt_result.get('flags', [])
    batch_flags = batch_result.get('flags', [])
    if bt_flags and batch_flags:
        min_len = min(len(bt_flags), len(batch_flags))
        flag_match_count = sum(1 for i in range(min_len) if bt_flags[i] == batch_flags[i])
        flag_total = min_len
        print(f"\n[Flags对比]")
        print(f"  总bar数: {flag_total}")
        print(f"  一致: {flag_match_count} ({flag_match_count/flag_total*100:.1f}%)")
        if flag_match_count < flag_total:
            diff_count = 0
            for i in range(min_len):
                if bt_flags[i] != batch_flags[i]:
                    if diff_count < 10:
                        print(f"    bar {i}: BT={bt_flags[i]} Batch={batch_flags[i]}")
                    diff_count += 1
            if diff_count > 10:
                print(f"    ... 共 {diff_count} 处不同")

    print(f"\n{'='*80}")
    if dir_match:
        print("[OK] 方向判断一致！")
    else:
        print("[FAIL] 方向判断不一致！请检查策略逻辑差异。")
    print(f"{'='*80}\n")


def main():
    # 测试参数 - 可切换不同周期验证
    import sys as _sys
    instId = _sys.argv[1] if len(_sys.argv) > 1 else "NEAR-USDT-SWAP"
    bar = _sys.argv[2] if len(_sys.argv) > 2 else "1H"

    print(f"\n{'#'*80}")
    print(f"# 回测对比测试: {instId} {bar}")
    print(f"# Backtrader (pro3_singletimeframe) vs Batch (calculate_ema_dmd)")
    print(f"{'#'*80}\n")

    print("=" * 60)
    print("[1/2] 运行 Backtrader 策略 (pro3_singletimeframe.py)...")
    print("=" * 60)
    bt_result = run_backtrader_strategy(instId, bar)

    print("\n" + "=" * 60)
    print("[2/2] 运行 Batch 策略 (calculate_ema_dmd)...")
    print("=" * 60)
    batch_result = run_batch_strategy(instId, bar)

    if batch_result is None:
        print("❌ Batch策略返回None，无法对比")
        return

    compare_results(bt_result, batch_result, instId, bar)


if __name__ == "__main__":
    main()
