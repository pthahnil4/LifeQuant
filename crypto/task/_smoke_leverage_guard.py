#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：杠杆设置的交易所回核机制（2026-09-01）
================================================
背景：_set_leverage_if_needed 原实现盲信本地缓存 lev_set —— 人工在 OKX App
改杠杆、交易所侧重置或账户杠杆模式变更后，系统永不纠正，后续开仓一直沿用
错误杠杆（用户报告"配置10x实际5x"）。
新逻辑：缓存命中后每 LEV_VERIFY_INTERVAL(24h) 回交易所核对一次实际值，
不一致则重设并打【杠杆校正】警告；查询失败保守信任缓存不阻断交易。

场景清单：
1. 缓存为空                     -> 调 set_leverage 并写缓存 + 回核时间戳
2. 缓存命中 + 回核期内          -> 零 API 调用
3. 缓存命中 + 超期 + 交易所一致 -> 不重设，仅刷新回核时间戳
4. 缓存命中 + 超期 + 交易所=5 ≠ 配置10 -> 重设为 10 并保留缓存
5. 缓存命中 + 超期 + 查询失败   -> 不重设、不崩溃（保守信任缓存）
6. leverage 为 0/None           -> 零动作
7. 超期 + 不一致 + 重设失败     -> 清除缓存，下轮重试
8. cross/isolated 分键独立回核  -> 互不干扰
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'utils'))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from crypto.task.utils.position_order_manager import DualPositionOrderManager as M

PASS, FAIL = [], []
INST = 'NEAR-USDT-SWAP'


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


class FakeExec:
    """记录调用次数的交易所桩"""

    def __init__(self, actual=10.0, set_ok=True):
        self.actual = actual          # 交易所侧"实际"杠杆；None=查询失败
        self.set_ok = set_ok
        self.set_calls = []
        self.get_calls = []

    def set_leverage(self, inst_id, lever, mgn_mode='cross', pos_side=None):
        self.set_calls.append((inst_id, lever, mgn_mode))
        return self.set_ok

    def get_leverage_setting(self, inst_id, mgn_mode='cross'):
        self.get_calls.append((inst_id, mgn_mode))
        return self.actual


def new_mgr(actual=10.0, set_ok=True):
    """绕开 __init__（不连 DB），只测杠杆回核逻辑"""
    m = M.__new__(M)
    m.executor = FakeExec(actual=actual, set_ok=set_ok)
    m._lock = threading.RLock()
    m.state = {}
    m.state_file = None
    m._run_id = ''
    m._verbose = False
    m._lev_verified = {}
    return m


print("\n=== 场景1：缓存为空 -> 设置并写缓存 ===")
m = new_mgr()
s = {}
m._set_leverage_if_needed(INST, s, 10, 'cross')
check('调用 set_leverage 一次', len(m.executor.set_calls) == 1, str(m.executor.set_calls))
check('缓存写入 cross=10', s['lev_set'].get('cross') == 10, str(s['lev_set']))
check('回核时间戳已写入', m._lev_verified.get(f'{INST}:cross', 0) > 0)
check('未做多余查询', len(m.executor.get_calls) == 0)

print("\n=== 场景2：缓存命中 + 回核期内 -> 零 API 调用 ===")
m._set_leverage_if_needed(INST, s, 10, 'cross')
check('无新增 set_leverage', len(m.executor.set_calls) == 1)
check('无 get_leverage_setting 查询', len(m.executor.get_calls) == 0)

print("\n=== 场景3：缓存命中 + 超回核期 + 交易所一致 -> 只刷新时间戳 ===")
m = new_mgr(actual=10.0)
s = {'lev_set': {'cross': 10}}
m._lev_verified[f'{INST}:cross'] = time.time() - M.LEV_VERIFY_INTERVAL - 10
m._set_leverage_if_needed(INST, s, 10, 'cross')
check('回交易所核对一次', len(m.executor.get_calls) == 1, str(m.executor.get_calls))
check('一致 -> 不重设', len(m.executor.set_calls) == 0)
check('时间戳已刷新',
      time.time() - m._lev_verified[f'{INST}:cross'] < 5)

print("\n=== 场景4：超期 + 交易所5x ≠ 配置10x -> 自动校正 ===")
m = new_mgr(actual=5.0)
s = {'lev_set': {'cross': 10}}
m._lev_verified[f'{INST}:cross'] = 0     # 进程重启后首次开仓即核对
m._set_leverage_if_needed(INST, s, 10, 'cross')
check('检测到不一致并重设', len(m.executor.set_calls) == 1, str(m.executor.set_calls))
check('重设值为配置的 10x', m.executor.set_calls[0][1] == 10)
check('缓存保留 cross=10', s['lev_set'].get('cross') == 10)

print("\n=== 场景5：超期 + 查询失败 -> 保守不动作 ===")
m = new_mgr(actual=None)
s = {'lev_set': {'cross': 10}}
m._lev_verified[f'{INST}:cross'] = 0
m._set_leverage_if_needed(INST, s, 10, 'cross')
check('查询失败不重设', len(m.executor.set_calls) == 0)
check('缓存不被清除（不阻断交易）', s['lev_set'].get('cross') == 10)
check('时间戳未刷新（下轮继续尝试核对）',
      m._lev_verified.get(f'{INST}:cross', 0) == 0)

print("\n=== 场景6：leverage 为 0/None -> 零动作 ===")
m = new_mgr()
s = {}
m._set_leverage_if_needed(INST, s, 0, 'cross')
m._set_leverage_if_needed(INST, s, None, 'cross')
check('无任何 API 调用',
      not m.executor.set_calls and not m.executor.get_calls)
check('不写缓存', 'lev_set' not in s)

print("\n=== 场景7：超期 + 不一致 + 重设失败 -> 清缓存下轮重试 ===")
m = new_mgr(actual=5.0, set_ok=False)
s = {'lev_set': {'cross': 10}}
m._lev_verified[f'{INST}:cross'] = 0
m._set_leverage_if_needed(INST, s, 10, 'cross')
check('尝试重设', len(m.executor.set_calls) == 1)
check('重设失败 -> 缓存被清除', 'cross' not in s['lev_set'], str(s['lev_set']))

print("\n=== 场景8：cross / isolated 分键独立 ===")
m = new_mgr(actual=10.0)
s = {}
m._set_leverage_if_needed(INST, s, 10, 'cross')
m._set_leverage_if_needed(INST, s, 10, 'isolated')
check('两模式各设置一次', len(m.executor.set_calls) == 2, str(m.executor.set_calls))
check('两模式缓存独立',
      s['lev_set'] == {'cross': 10, 'isolated': 10}, str(s['lev_set']))
m._lev_verified[f'{INST}:isolated'] = 0      # 只让 isolated 超期
m._set_leverage_if_needed(INST, s, 10, 'cross')
m._set_leverage_if_needed(INST, s, 10, 'isolated')
check('仅 isolated 触发回核',
      m.executor.get_calls == [(INST, 'isolated')], str(m.executor.get_calls))

print("\n" + "=" * 60)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print('  失败:', f)
    sys.exit(1)
print("杠杆回核机制符合预期：缓存不再被盲信，交易所侧失真可自动发现并校正")
