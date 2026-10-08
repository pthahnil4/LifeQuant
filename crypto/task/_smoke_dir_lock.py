#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：人工方向锁定语义（2026-09-01 事故修复）
==================================================
事故：manual_direction=long 时旧实现把 short_prev/short_prev_prev 伪造成
"刚刚反转"，使 open_window 恒为 True，空头信号窗口期仍在高位追涨补多。

新语义：锁定只作用于长周期方向；双周期模式短周期三时段保留真实信号，
开仓仍须通过真实共振窗口；单周期模式无窗口概念，方向即持仓方向。

场景清单：
1. 锁定多 + 真实空头窗口   -> 短周期三时段保持真实，open_window=False（不开多）
2. 锁定多 + 真实多头共振窗口 -> open_window=True（正常开多）
3. 锁定多 + 空头共振窗口 + 持多仓 -> close_window=True（平多跟随真实信号）
4. 锁定空 + 真实多头共振窗口 -> 长周期=short，绝不开多（open_window 判空头共振=False）
5. auto  + 真实空头共振窗口 -> 基线：长周期取上一时段，窗口逻辑不受影响
6. 单周期模式 + 锁定多 + 短周期真实空头 -> target_dir 锁定为 long
7. 页面锁定（manual_override_cache）与配置锁定行为一致，且短周期不被篡改
8. 锁定多 + 空头非共振（上上也是空）-> 既不开多也不平多（窗口全 False）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'utils'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from crypto.task.trend_range_trader import TrendRangeTrader as T

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


def new_trader(override=None):
    """绕开 __init__（不连 DB/交易所），只测纯方向决议逻辑"""
    t = T.__new__(T)
    t.manual_override_cache = dict(override or {})
    return t


def resolve(t, manual_dir, mode, short, prev, prev_prev,
            long_raw='long', long_prev='long'):
    return t._resolve_directions(
        'NEAR-USDT-SWAP', manual_dir, mode,
        {'short': short, 'short_prev': prev, 'short_prev_prev': prev_prev,
         'long_raw': long_raw, 'long_prev': long_prev})


def windows(d):
    """复刻 _run_trend_position 的三时段窗口判定（dual 模式）"""
    desired = d['long']
    opp = 'short' if desired == 'long' else 'long'
    ow = (d['short'] == desired and d['short_prev'] == desired
          and d['short_prev_prev'] == opp)
    cw = (d['short'] == opp and d['short_prev'] == opp
          and d['short_prev_prev'] == desired)
    return ow, cw


print("\n=== 场景1：锁定多 + 真实空头共振窗口 -> 禁止开多 ===")
t = new_trader()
d = resolve(t, 'long', 'dual', 'short', 'short', 'long')
ow, cw = windows(d)
check('短周期当前保持真实(short)', d['short'] == 'short', f"short={d['short']}")
check('上一时段保持真实(short)', d['short_prev'] == 'short')
check('上上时段保持真实(long)，未被伪造为 short',
      d['short_prev_prev'] == 'long', f"prev_prev={d['short_prev_prev']}")
check('长周期锁定为 long', d['long'] == 'long')
check('open_window=False（空头窗口期绝不开多）', ow is False, f"ow={ow}")
check('close_window=True（真实空头共振应平多）', cw is True, f"cw={cw}")
check('方向来源=配置锁定(long)', d['dir_source'] == '配置锁定(long)', d['dir_source'])

print("\n=== 场景2：锁定多 + 真实多头共振窗口 -> 正常开多 ===")
d = resolve(t, 'long', 'dual', 'long', 'long', 'short')
ow, cw = windows(d)
check('open_window=True（多头共振才开多）', ow is True, f"ow={ow}")
check('close_window=False', cw is False)

print("\n=== 场景3：锁定多 + 空头共振 -> 平仓窗口成立（平仓跟随真实信号）===")
d = resolve(t, 'long', 'dual', 'short', 'short', 'long')
ow, cw = windows(d)
check('平仓窗口成立', cw is True and ow is False, f"ow={ow} cw={cw}")

print("\n=== 场景4：锁定空 + 真实多头共振窗口 -> 绝不开多 ===")
d = resolve(t, 'short', 'dual', 'long', 'long', 'short')
ow, cw = windows(d)
check('长周期锁定为 short', d['long'] == 'short')
check('open_window=False（多头共振但锁定做空，不开空也不开多）',
      ow is False, f"ow={ow}")
check('close_window=True（持空仓时按真实多头信号平空）', cw is True)

print("\n=== 场景5：auto 基线不受影响 ===")
d = resolve(t, 'auto', 'dual', 'short', 'short', 'long',
            long_raw='short', long_prev='short')
ow, cw = windows(d)
check('长周期取上一时段(short)', d['long'] == 'short', f"long={d['long']}")
check('open_window=True（空头共振开空）', ow is True, f"ow={ow}")
check('方向来源=信号驱动', d['dir_source'] == '信号驱动', d['dir_source'])
check('locked_dir 为 None', d['locked_dir'] is None)

print("\n=== 场景6：单周期模式 + 锁定多 + 短周期真实空头 -> 方向锁定为 long ===")
d = resolve(t, 'long', 'single', 'short', 'short', 'long')
check('单周期模式短周期一并锁定为 long', d['short'] == 'long', f"short={d['short']}")
check('长周期同为 long', d['long'] == 'long')
check('方向来源=配置锁定(long)', d['dir_source'] == '配置锁定(long)')

print("\n=== 场景7：页面锁定（manual_override_cache）语义一致 ===")
t2 = new_trader({'NEAR-USDT-SWAP': 'LONG'})
d = resolve(t2, 'auto', 'dual', 'short', 'short', 'long')
ow, cw = windows(d)
check('页面锁定生效，长周期=long', d['long'] == 'long')
check('短周期未被篡改(short/short/long)',
      (d['short'], d['short_prev'], d['short_prev_prev']) == ('short', 'short', 'long'))
check('open_window=False（空头窗口期不开多）', ow is False, f"ow={ow}")
check('方向来源=页面锁定(long)', d['dir_source'] == '页面锁定(long)', d['dir_source'])

print("\n=== 场景8：锁定多 + 空头非共振（错过窗口）-> 既不开多也不平多 ===")
d = resolve(t, 'long', 'dual', 'short', 'short', 'short')
ow, cw = windows(d)
check('open_window=False', ow is False, f"ow={ow}")
check('close_window=False（上上时段已同向，错过窗口）', cw is False, f"cw={cw}")

print("\n" + "=" * 60)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print('  失败:', f)
    sys.exit(1)
print("方向锁定语义全部符合预期：锁定不篡改短周期三时段，开仓严格受真实共振窗口约束")
