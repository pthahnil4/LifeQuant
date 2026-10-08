#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
止盈引擎实盘验证脚本（主账号 0.2 张 NEAR-USDT-SWAP）
======================================================
验证目标（用户要求：验证"对已有持仓设置止盈能否成功"，不要求触发平仓）：
  Step1 人工强制开多 0.2 张（manual_direction='long'）
  Step2 带止盈配置再跑一轮 → 验证引擎对已有持仓建立评估状态
        （tp_runtime_state.json 生成 NEAR-USDT-SWAP:long：入场时间/峰值价）
  Step3 验证大阈值(50%)下不误触发（持仓仍在）
  Step4 市价平掉测试仓位 + 清理引擎状态（干净收尾）

注：双仓位改造后趋势跟踪仓改为限价挂单（不追价），Step1 不再保证
即时成交；未成交时脚本会在 Step1 安全中止（无需回滚）。

运行: python crypto/task/_test_tp_real.py
"""

import os
import sys
import json
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
for p in (_ROOT,):
    if p not in sys.path:
        sys.path.insert(0, p)

from crypto.task.trend_range_trader import TrendRangeTrader  # noqa: E402

INST_ID = 'NEAR-USDT-SWAP'
SHORT_PERIOD = '5m'
LONG_PERIOD = '4H'
TEST_POSITION = 0.2

# 测试止盈配置：固定目标 50%（绝不会触发，仅验证配置生效+状态建立）
TP_CFG = {
    'enabled': True,
    'category': 'fixed_target',
    'fixed_target': {'mode': 'pct', 'pct': 50.0, 'r_multiple': 2.0, 'atr_multiple': 3.0},
    'time_stop': {'enabled': False, 'max_bars': 24, 'min_profit_pct': 1.0},
}
SL_CFG = {'enabled': False}

# 趋势跟踪仓配置（止盈止损已归属本仓位），区间波动仓全程关闭
TREND_CFG = {
    'enabled': True,
    'period_mode': 'dual',
    'contracts': TEST_POSITION,
    'exchange_algo_backup': False,
    'take_profit': TP_CFG,
    'stop_loss': SL_CFG,
}
RANGE_CFG = {'enabled': False, 'contracts': 0}

STATE_FILE = os.path.join(_HERE, 'tp_runtime_state.json')


def read_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def get_cross_pos(sched):
    pos = sched._read_positions_by_mode(INST_ID)
    if pos is None:
        return None
    return pos['cross']


def main():
    print('=' * 60)
    print('止盈引擎实盘验证（主账号 0.2 张 NEAR-USDT-SWAP）')
    print('=' * 60)

    sched = TrendRangeTrader(manual_direction_config={})

    # 前置检查：当前持仓
    pos0 = get_cross_pos(sched)
    if pos0 is None:
        print('[中止] 持仓查询失败（网络/API），请稍后重试')
        return 1
    print(f'[前置] 当前全仓多头持仓: {pos0:.1f} 张')
    if pos0 > 0.01:
        print('[中止] 账号已有全仓多头持仓，为避免误动真实仓位，请先处理后再测试')
        return 1

    # ---- Step1: 人工强制开多 0.2 张（限价挂单，区间波动仓关闭）----
    print('\n[Step1] 人工强制开多 0.2 张（限价）...')
    r1 = sched.analyze_and_trade_real(
        inst_id=INST_ID, short_period=SHORT_PERIOD, long_period=LONG_PERIOD,
        trend_cfg=TREND_CFG, range_cfg=RANGE_CFG, manual_direction='long',
        run_id='TP-TEST-1')
    print(f'[Step1] 返回: {r1}')

    time.sleep(3)
    pos1 = get_cross_pos(sched)
    print(f'[Step1] 开仓后全仓多头持仓: {pos1} 张')
    if pos1 is None or pos1 < 0.19:
        print('[失败] 开仓未成功，测试中止（无需回滚）')
        return 1
    print('[Step1] ✓ 下单成功')

    # ---- Step2: 再跑一轮 → 止盈引擎对已有持仓建立评估状态 ----
    print('\n[Step2] 带止盈配置再跑一轮（验证引擎接管持仓评估）...')
    r2 = sched.analyze_and_trade_real(
        inst_id=INST_ID, short_period=SHORT_PERIOD, long_period=LONG_PERIOD,
        trend_cfg=TREND_CFG, range_cfg=RANGE_CFG, manual_direction='long',
        run_id='TP-TEST-2')
    print(f'[Step2] 返回: {r2}')

    st = read_state()
    key = f'{INST_ID}:long'
    ok_state = key in st and st[key].get('entry_ts') and st[key].get('peak', 0) > 0
    print(f'[Step2] tp_runtime_state.json[{key}] = {st.get(key)}')
    print('[Step2] ' + ('✓ 止盈状态建立成功（入场时间/峰值价已记录）' if ok_state
                        else '✗ 止盈状态未建立'))

    # ---- Step3: 大阈值不误触发（持仓应仍在）----
    pos2 = get_cross_pos(sched)
    ok_no_trigger = (pos2 is not None and pos2 >= 0.19
                     and not r2.get('tp_sl_triggered'))
    print(f'\n[Step3] 评估后持仓: {pos2} 张 | tp_sl_triggered={r2.get("tp_sl_triggered", False)}')
    print('[Step3] ' + ('✓ 50%大阈值未误触发，持仓完好' if ok_no_trigger
                        else '✗ 异常：止盈被误触发或持仓丢失'))

    # ---- Step4: 平掉测试仓位 + 清理状态（收尾）----
    print('\n[Step4] 市价平掉测试仓位（含撤挂单、清本地账本）...')
    print(f"[Step4] {'; '.join(sched.force_close_manual(INST_ID, 'long'))}")
    time.sleep(3)
    pos3 = get_cross_pos(sched)
    print(f'[Step4] 平仓后持仓: {pos3} 张')
    sched.tp_engine.clear_state(INST_ID, True)
    st2 = read_state()
    ok_clear = key not in st2
    ok_flat = pos3 is not None and pos3 <= 0.01
    print('[Step4] ' + ('✓ 平仓成功' if ok_flat else '✗ 平仓失败，请手动检查 OKX 持仓！'))
    print('[Step4] ' + ('✓ 引擎状态已清理' if ok_clear else '✗ 状态清理失败'))

    print('\n' + '=' * 60)
    all_ok = ok_state and ok_no_trigger and ok_flat and ok_clear
    print('测试结论: ' + ('全部通过 ✓ 下单→持仓→止盈设置→评估→平仓 全链路正常'
                          if all_ok else '存在失败项，见上方明细'))
    print('=' * 60)
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
