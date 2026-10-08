#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
双周期多点位分批限价交易策略 — 回测执行 & 报告
=============================================

测试三组双周期组合（相同总张数/杠杆/手续费/初始资金基准）：
  5m-1H、15m-4H、1H-1D
每组各跑两种仓位比例策略：
  - 静态(static)：使用 config_trend_range.json 的固定 entry_ratios
  - 动态(dynamic)：基于 ADX regime 动态调整三档比例

输出：每组每策略的收益率/最大回撤/夏普/胜率/盈亏比/交易次数，
并横向比较 + 评估动态调整有效性 + 盈利潜力结论。

用法：
  python run_backtest.py                # 默认 NEAR-USDT-SWAP
  python run_backtest.py DOGE-USDT-SWAP
"""

import os
import sys
import json

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

# Windows 控制台默认 GBK，无法输出部分中文/符号 → 强制 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

from signal_engine import build_dataset       # noqa: E402
from batch_sim import BatchBacktester          # noqa: E402

# 三组双周期组合（短周期页数放大以覆盖更长历史）
COMBOS = [
    {'short': '5m', 'long': '1H', 'short_pages': 30, 'long_pages': 12},
    {'short': '15m', 'long': '4H', 'short_pages': 20, 'long_pages': 12},
    {'short': '1H', 'long': '1D', 'short_pages': 12, 'long_pages': 12},
]

# 默认配置（对齐 config_trend_range.json 的参数结构；总张数放大到 100 以获得
# 有意义的名义规模，三组组合共享同一 total_contracts / leverage）
DEFAULT_CFG = {
    'total_contracts': 100,
    'leverage': 10,
    'ttl_periods': 4,
    'boll_amend_min_pct': 0.001,
    'boll_period': 20,
    'boll_dev': 2.0,
    'entry_ratios': {'open': 0.3, 'close': 0.3, 'boll': 0.4},
    'exit_ratios': {'boll': 0.4, 'close': 0.3, 'open': 0.3},
    'ct_val': 1.0,
    'maker_fee': 2e-4,
    'taker_fee': 5e-4,
    'slippage': 0.0005,
}


def _load_config_ratios():
    """尝试从实盘配置读取 entry/exit_ratios 等参数结构（保持一致）。

    实盘已改为双仓位构造，batch_trading 配置块随三开三平体系一同移除，
    因此这里必然回落到 DEFAULT_CFG 的默认比例。
    """
    cfg_path = os.path.join(os.path.dirname(_THIS_DIR), 'task', 'config',
                            'config_trend_range.json')
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        bt = data['currencies'][0]['batch_trading']
        return {
            'ttl_periods': bt.get('ttl_periods', 4),
            'boll_amend_min_pct': bt.get('boll_amend_min_pct', 0.001),
            'boll_period': bt.get('boll_period', 20),
            'boll_dev': bt.get('boll_dev', 2.0),
            'leverage': bt.get('leverage', 10),
            'entry_ratios': bt.get('entry_ratios', DEFAULT_CFG['entry_ratios']),
            'exit_ratios': bt.get('exit_ratios', DEFAULT_CFG['exit_ratios']),
        }
    except Exception as e:
        print(f"[warn] 读取实盘配置失败({e})，使用默认比例")
        return {}


def run_one(inst_id, combo, base_cfg, dynamic):
    short_bar, long_bar = combo['short'], combo['long']
    data = build_dataset(
        inst_id, short_bar, long_bar,
        boll_period=int(base_cfg.get('boll_period', 20)),
        boll_dev=float(base_cfg.get('boll_dev', 2.0)),
        short_pages=combo['short_pages'], long_pages=combo['long_pages'],
        use_cache=True)

    cfg = dict(base_cfg)
    cfg['short_period'] = short_bar
    cfg['dynamic_ratio'] = dynamic
    bt = BatchBacktester(cfg)
    m = bt.run(data)
    m['_bars'] = len(data)
    m['_span'] = (str(data.index[0]), str(data.index[-1])) if len(data) else ('', '')
    return m, bt


def _fmt_row(label, m):
    pf = m['profit_factor']
    pf_s = '∞' if pf == float('inf') else f"{pf:.2f}"
    return (f"  {label:<9} | 收益 {m['return_pct']:>7.2f}% | 回撤 {m['max_drawdown_pct']:>6.2f}% "
            f"| 夏普 {m['sharpe']:>6.2f} | 胜率 {m['win_rate']:>5.1f}% | 盈亏比 {pf_s:>5} "
            f"| 交易 {m['num_trades']:>3} | 净盈亏 {m['net_pnl']:>9.2f}")


def main():
    inst_id = sys.argv[1] if len(sys.argv) > 1 else 'NEAR-USDT-SWAP'

    base = dict(DEFAULT_CFG)
    base.update(_load_config_ratios())

    print('=' * 96)
    print(f"双周期多点位分批限价交易策略 — 回测报告   标的: {inst_id}")
    print(f"总张数={base['total_contracts']}  杠杆={base['leverage']}x  "
          f"maker={base['maker_fee']*100:.3f}%  taker={base['taker_fee']*100:.3f}%  "
          f"TTL={base['ttl_periods']}bar  BOLL={base['boll_period']}/{base['boll_dev']}")
    print(f"静态开仓比例(来自配置): {base['entry_ratios']}")
    print('=' * 96)

    summary = []
    for combo in COMBOS:
        tag = f"{combo['short']}-{combo['long']}"
        print(f"\n【双周期组合 {tag}】")
        try:
            m_s, bt_s = run_one(inst_id, combo, base, dynamic=False)
            m_d, bt_d = run_one(inst_id, combo, base, dynamic=True)
        except Exception as e:
            print(f"  组合 {tag} 回测失败: {e}")
            import traceback
            traceback.print_exc()
            continue

        print(f"  数据: {m_s['_bars']} bars  {m_s['_span'][0]} → {m_s['_span'][1]}  "
              f"初始资金基准={m_s['init_capital']:.2f} USDT")
        print(_fmt_row('静态比例', m_s))
        print(_fmt_row('动态比例', m_d))
        summary.append((tag, m_s, m_d))

    # ---------------- 横向汇总 + 结论 ----------------
    if not summary:
        print("\n无有效回测结果。")
        return

    print('\n' + '=' * 96)
    print("横向汇总（净收益率 %）")
    print('=' * 96)
    print(f"  {'组合':<10} {'静态':>12} {'动态':>12} {'动态-静态':>12}")
    best = None
    for tag, ms, md in summary:
        delta = md['return_pct'] - ms['return_pct']
        print(f"  {tag:<10} {ms['return_pct']:>11.2f}% {md['return_pct']:>11.2f}% {delta:>+11.2f}%")
        for m in (ms, md):
            if best is None or m['return_pct'] > best[2]:
                best = (tag, '动态' if m is md else '静态', m['return_pct'], m)

    # 动态有效性
    dyn_better = sum(1 for _, ms, md in summary if md['return_pct'] > ms['return_pct'])
    print('\n' + '-' * 96)
    print("动态仓位比例有效性评估")
    print('-' * 96)
    print(f"  动态优于静态的组合数: {dyn_better}/{len(summary)}")
    avg_delta = sum(md['return_pct'] - ms['return_pct'] for _, ms, md in summary) / len(summary)
    print(f"  平均收益差(动态-静态): {avg_delta:+.2f}%")
    if dyn_better >= 2 and avg_delta > 0:
        print("  → 动态调整在多数组合上提升收益，基于 ADX regime 的比例调整有效。")
    elif avg_delta > 0:
        print("  → 动态调整平均略有正贡献，但并非全面占优，需结合具体行情谨慎使用。")
    else:
        print("  → 动态调整未带来正贡献，当前 regime 阈值需重新标定或行情不适配。")

    # 盈利潜力结论
    print('\n' + '-' * 96)
    print("盈利潜力结论")
    print('-' * 96)
    pos = [m for _, ms, md in summary for m in (ms, md) if m['net_pnl'] > 0]
    all_m = [m for _, ms, md in summary for m in (ms, md)]
    winners = sum(1 for m in all_m if m['net_pnl'] > 0)
    print(f"  盈利场景: {winners}/{len(all_m)}   最佳: {best[0]} {best[1]} 收益 {best[2]:.2f}%")
    avg_wr = sum(m['win_rate'] for m in all_m) / len(all_m)
    print(f"  平均胜率: {avg_wr:.1f}%")
    if winners >= len(all_m) * 0.6 and best[2] > 0:
        print("  → 策略在测试周期内具备盈利潜力：多点位分批限价 + 点位隔离能有效降低单点错入风险；")
        print("    建议优先采用表现最佳的双周期组合，并对手续费/滑点敏感性做进一步压力测试。")
    else:
        print("  → 当前样本内盈利能力有限：样本区间偏短或行情以震荡为主，")
        print("    建议扩大历史样本、结合更长周期趋势过滤后再评估。")
    print("\n  ⚠ 免责声明：回测基于历史标记价格K线与理想化限价成交(low≤价≤high)假设，")
    print("     实盘存在部分成交/排队/滑点/资金费差异，结果仅供策略研究参考。")
    print('=' * 96)


if __name__ == '__main__':
    main()
