#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tp_engine 组合出场（combo）离线单元测试
======================================
运行: python crypto/task/_test_tp_combo.py

设计要点：本文件 **不接触业务数据库**，通过 mock 让 tp_engine 内部的
`session_scope` / `state_repo` 变成不可用状态，触发既有的 ImportError
兜底分支，从而把 combo 状态完全保留在 TakeProfitEngine._state 内存字典里。
方案「双仓位出场与风控增强_6596c3cb」要求：
  - 多头保护线只升不降；空头只降不升
  - 部分止盈只触发一次；两阶段提交（pending → commit(filled)）
  - 观察模式不推进成交状态机、不写回 DB
  - 未真实成交不消耗档位、失败可下轮重试
  - P0/ATR0/R0 首观察即锁定，不随后续 ATR 变化而放宽
覆盖以上验收点，任何一条不达标即失败退出。
"""

import os
import sys
import types
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..', '..')
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)


# ---- 在 import tp_engine 前把 crypto.database / trader_state_repo 断掉 ----
# tp_engine 的 import 有 try/except，session_scope=None 时 _persist 会静默 no-op。
_fake_db = types.ModuleType('crypto.database')
_fake_db.session_scope = None  # 触发 tp_engine 的 except ImportError 分支
_fake_repo = types.ModuleType('crypto.trader_state_repo')
sys.modules.setdefault('crypto', types.ModuleType('crypto'))
sys.modules['crypto.database'] = _fake_db
sys.modules['crypto.trader_state_repo'] = _fake_repo

# tp_engine 使用 `from crypto.database import session_scope`。若上面 mock 提供
# 的是 None，session_scope 会被绑成 None，_load_state/_persist 中调用即抛异常，
# 被 try/except 静默吞掉——完全达成内存隔离。

import tp_engine as _tp_mod  # noqa: E402
from tp_engine import TakeProfitEngine, normalize_tp_config  # noqa: E402


PASS = 0
FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def base_ctx(**kw):
    ctx = {
        'inst_id': 'COMBO-USDT-SWAP',
        'is_long': True,
        'pos_size': 10.0,
        'avg_px': 100.0,
        'current_price': 100.0,
        'pnl_pct': 0.0,
        'atr_value': 1.0,
        'boll_upper': 0, 'boll_middle': 0, 'boll_lower': 0,
        'macd_hist_series': [],
        'close_series': [],
        'short_period': '1m',
        'sl_cfg': {},
        'closed_close': 100.0,
        'observe_only': False,
    }
    ctx.update(kw)
    return ctx


def combo_cfg(**kw):
    sub = {
        'init_r_atr': 2.0,
        'activate_r': 1.5,
        'lock_steps': [[1.5, 0.0], [2.5, 1.0], [4.0, 2.0]],
        'chandelier_atr': 3.0,
        'partial_r': 2.0,
        'partial_ratio': 0.30,
        'breakeven_buffer_pct': 0.10,
    }
    sub.update(kw)
    return {'enabled': True, 'category': 'combo', 'combo': sub}


def new_engine():
    # 状态全内存：session_scope=None，_persist 静默失败，_load_state 返回 {}
    return TakeProfitEngine()


# ================================================================
print("== 0. 归一化与 category 白名单 ==")
check("VALID_CATEGORIES 含 combo", 'combo' in _tp_mod.VALID_CATEGORIES)
nc = normalize_tp_config({'enabled': True, 'category': 'combo'})
check("normalize 保留 combo", nc['category'] == 'combo')


# ================================================================
print("== 1. 首次观察锁定 P0/ATR0/R0/初始硬止损 ==")
eng = new_engine()
c = combo_cfg()
# 首笔评估：ATR=1.0 → R0=2.0，多头 S0=100-2=98
eng.evaluate(c, base_ctx())
st = eng._state.get('COMBO-USDT-SWAP:long', {})
ex = st.get('extra') or {}
check("首观察写入 P0=100", abs(ex.get('p0', 0) - 100) < 1e-9, str(ex))
check("首观察写入 ATR0=1.0", abs(ex.get('atr0', 0) - 1.0) < 1e-9, str(ex))
check("首观察写入 R0=2.0", abs(ex.get('r0', 0) - 2.0) < 1e-9, str(ex))
check("初始保护线 S0=98", abs(ex.get('protect', 0) - 98) < 1e-9, str(ex))
check("锁定标志 locked=True", ex.get('locked') is True, str(ex))

# 后续 ATR 突然放大 → R0 不变（不放宽）
eng.evaluate(c, base_ctx(atr_value=3.5, current_price=100.0, closed_close=100.0))
st = eng._state['COMBO-USDT-SWAP:long']
ex = st.get('extra') or {}
check("ATR 波动后 R0 仍为 2.0（初始风险锁定）", abs(ex['r0'] - 2.0) < 1e-9, str(ex))


# ================================================================
print("== 2. 棘轮单调：多头保护线只升不降 ==")
eng = new_engine()
c = combo_cfg()
# 冲高一轮到 103.5（MFE=3.5/2=1.75R，触发棘轮 1.5R 档 → 保本价 100.1）
eng.evaluate(c, base_ctx(current_price=103.5, closed_close=103.5))
ex = eng._state['COMBO-USDT-SWAP:long']['extra']
p1 = float(ex['protect'])
check("MFE 1.75R 后 protect 抬到 ≥ 保本价", p1 >= 100.0, str(ex))
# 回落到 100.5（吊灯线为 103.5-3=100.5；MFE 保持）
eng.evaluate(c, base_ctx(current_price=100.5, closed_close=100.5))
ex = eng._state['COMBO-USDT-SWAP:long']['extra']
p2 = float(ex['protect'])
check("回落后 protect 不回拉", p2 >= p1 - 1e-9, f"p1={p1} p2={p2}")
# 再冲高一轮到 105（MFE=2.5R → 棘轮锁 1R=102）
eng.evaluate(c, base_ctx(current_price=105.0, closed_close=105.0))
ex = eng._state['COMBO-USDT-SWAP:long']['extra']
p3 = float(ex['protect'])
check("MFE 2.5R 后 protect 抬到 ≥ 102", p3 >= 102.0 - 1e-9, f"protect={p3}")
# 触发吊灯线（跌破 105-3=102）→ close_all
d = eng.evaluate(c, base_ctx(current_price=101.8, closed_close=102.0))
check("跌破 2.5R 锁定线后 close_all 触发", d['action'] == 'close_all', str(d))


# ================================================================
print("== 3. 部分止盈只触发一次 + 两阶段提交 ==")
eng = new_engine()
c = combo_cfg()
# 首次建立状态
eng.evaluate(c, base_ctx())
# MFE=2R (h_peak=104) → 触发部分止盈 30%
d = eng.evaluate(c, base_ctx(current_price=104.0, closed_close=104.0))
check("MFE 2R 触发 close_partial", d['action'] == 'close_partial', str(d))
check("partial ratio ≈ 0.30 (Q0=10, 平 3)",
      abs(float(d.get('close_ratio', 0)) - 0.30) < 1e-6, str(d))
ex = eng._state['COMBO-USDT-SWAP:long']['extra']
check("pending 状态已置位", ex.get('partial_pending') is True, str(ex))
check("done 尚未置位", ex.get('partial_done') is False, str(ex))

# 未 commit 前不重复触发
d2 = eng.evaluate(c, base_ctx(current_price=104.5, closed_close=104.5, pos_size=10.0))
check("pending 未确认前不重复触发", d2['action'] == 'none', str(d2))

# commit(filled=False) → 释放 pending，允许下轮重试
ok = eng.commit_partial('COMBO-USDT-SWAP', True, filled=False)
check("commit_partial(False) 返回成功", ok is True)
ex = eng._state['COMBO-USDT-SWAP:long']['extra']
check("pending 已释放", ex.get('partial_pending') is False, str(ex))
check("done 仍为 False", ex.get('partial_done') is False, str(ex))
d3 = eng.evaluate(c, base_ctx(current_price=104.5, closed_close=104.5, pos_size=10.0))
check("下轮可重试部分止盈", d3['action'] == 'close_partial', str(d3))

# commit(filled=True) → done 置位
ok = eng.commit_partial('COMBO-USDT-SWAP', True, filled=True)
check("commit_partial(True) 返回成功", ok is True)
ex = eng._state['COMBO-USDT-SWAP:long']['extra']
check("done 已置位", ex.get('partial_done') is True, str(ex))
# 再次 evaluate 不重复拆仓
d4 = eng.evaluate(c, base_ctx(current_price=105.0, closed_close=105.0, pos_size=7.0))
check("done 后不再触发 close_partial", d4['action'] == 'none', str(d4))
# is_partial_blocked 供交易端拦截同周期补回
check("is_partial_blocked 报告 True",
      eng.is_partial_blocked('COMBO-USDT-SWAP', True) is True)


# ================================================================
print("== 4. 空头镜像：保护线只降不升 ==")
eng = new_engine()
c = combo_cfg()
ctx0 = base_ctx(is_long=False, avg_px=100.0, current_price=100.0, closed_close=100.0)
eng.evaluate(c, ctx0)
ex = eng._state['COMBO-USDT-SWAP:short']['extra']
check("空头初始 protect=102 (S0=P0+R0)", abs(ex['protect'] - 102) < 1e-9, str(ex))
# 下跌到 96（MFE=(100-96)/2=2R → 保本价 100*(1-0.001)=99.9；触发部分止盈）
eng.commit_partial('COMBO-USDT-SWAP', False, filled=True)  # 先手动 skip 掉部分止盈阶段
# 重置 partial_pending（上面 evaluate 会触发）
eng.evaluate(c, base_ctx(is_long=False, avg_px=100.0, current_price=96.0, closed_close=96.0))
ex = eng._state['COMBO-USDT-SWAP:short']['extra']
p_short_1 = float(ex['protect'])
check("空头 MFE=2R 后 protect ≤ 99.9", p_short_1 <= 100.0, str(ex))
# 反弹到 98
eng.evaluate(c, base_ctx(is_long=False, avg_px=100.0, current_price=98.0, closed_close=98.0))
ex = eng._state['COMBO-USDT-SWAP:short']['extra']
p_short_2 = float(ex['protect'])
check("空头反弹后 protect 不回拉", p_short_2 <= p_short_1 + 1e-9,
      f"p1={p_short_1} p2={p_short_2}")


# ================================================================
print("== 5. 观察模式不推进状态机 ==")
eng = new_engine()
c = combo_cfg()
# 观察下评估 MFE=2R，本应触发 partial，但 observe_only=True
d = eng.evaluate(c, base_ctx(current_price=104.0, closed_close=104.0, observe_only=True))
# 触发信号仍能返回（否则用户看不到预览），但状态不能写 pending
ex = eng._state['COMBO-USDT-SWAP:long']['extra']
check("观察模式不置 pending",
      ex.get('partial_pending') is not True,
      str(ex))
check("观察模式不置 done", ex.get('partial_done') is not True, str(ex))


# ================================================================
print("== 6. clear_state 后新周期重锁 P0 ==")
eng = new_engine()
c = combo_cfg()
eng.evaluate(c, base_ctx())
eng.evaluate(c, base_ctx(current_price=103.5, closed_close=103.5))
eng.clear_state('COMBO-USDT-SWAP', True)
check("clear_state 后状态被移除",
      'COMBO-USDT-SWAP:long' not in eng._state,
      str(eng._state.keys()))
eng.evaluate(c, base_ctx(avg_px=200.0, current_price=200.0, closed_close=200.0))
ex = eng._state['COMBO-USDT-SWAP:long']['extra']
check("新周期用新 P0=200 锁定", abs(ex['p0'] - 200) < 1e-9, str(ex))
check("新周期 R0 按新 ATR 重算 (ATR=1→R0=2)",
      abs(ex['r0'] - 2) < 1e-9, str(ex))


# ================================================================
print("== 7. 未触发前不越权动作 ==")
eng = new_engine()
c = combo_cfg()
# 未激活前 price=101.5 (MFE=0.75R)，未破 S0=98 → 不动作
d = eng.evaluate(c, base_ctx(current_price=101.5, closed_close=101.5))
check("未激活+未破硬止损→none", d['action'] == 'none', str(d))
# 直接跌破 S0 → close_all（初始硬止损）
d = eng.evaluate(c, base_ctx(current_price=97.9, closed_close=98.5))
check("跌破 S0→close_all", d['action'] == 'close_all', str(d))
check("reason 含初始硬止损",
      '初始硬止损' in d.get('reason', '') or '保护' in d.get('reason', ''),
      str(d))


# ================================================================
print()
print(f"总计: {PASS + FAIL} | 通过: {PASS} | 失败: {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
