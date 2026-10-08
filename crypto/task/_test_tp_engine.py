#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tp_engine.py 离线单元测试（无网络/无API，纯逻辑验证）
运行: python crypto/task/_test_tp_engine.py
覆盖: 六类主止盈各模式 + 时间兜底 + 旧结构兼容 + 状态持久化/清理
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tp_engine import TakeProfitEngine, normalize_tp_config  # noqa: E402

# ---- DB 沙箱（迁移批次5）：状态已迁 MySQL，快照→清场→atexit 恢复真实数据 ----
import atexit as _atexit  # noqa: E402
from sqlalchemy import select as _sa_select, delete as _sa_delete  # noqa: E402
from crypto.database import session_scope as _ss  # noqa: E402
from crypto.models import TpRuntimeState as _TP  # noqa: E402


def _row_dict(row):
    d = dict(row.__dict__)
    d.pop('_sa_instance_state', None)
    return d


with _ss() as _s:
    _SNAP = [_row_dict(r) for r in _s.execute(_sa_select(_TP)).scalars().all()]
    _s.execute(_sa_delete(_TP))


def _restore_snap():
    try:
        with _ss() as _s:
            _s.execute(_sa_delete(_TP))
            for _d in _SNAP:
                _s.add(_TP(**_d))
        print('[沙箱] 已恢复真实止盈状态快照')
    except Exception as e:
        print(f'[沙箱] ❌ 快照恢复失败: {e}')


_atexit.register(_restore_snap)

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


def new_engine():
    """每组用例独立引擎：先清空 tp_runtime_state 表隔离用例，再整份加载构造"""
    with _ss() as _s:
        _s.execute(_sa_delete(_TP))
    return TakeProfitEngine(), None


def base_ctx(**kw):
    ctx = {
        'inst_id': 'TEST-USDT-SWAP',
        'is_long': True,
        'pos_size': 1.0,
        'avg_px': 100.0,
        'current_price': 100.0,
        'pnl_pct': 0.0,
        'atr_value': 1.0,
        'boll_upper': 0, 'boll_middle': 0, 'boll_lower': 0,
        'macd_hist_series': [],
        'close_series': [],
        'short_period': '1m',
        'sl_cfg': {},
    }
    ctx.update(kw)
    return ctx


def cfg(category, sub=None, enabled=True, time_stop=None):
    c = {'enabled': enabled, 'category': category}
    if sub is not None:
        c[category] = sub
    if time_stop is not None:
        c['time_stop'] = time_stop
    return c


def cleanup(path):
    # 状态已迁 MySQL，path 恒为 None，保留签名兼容旧调用点
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


# ================================================================
print("== 0. 总开关与旧结构兼容 ==")
eng, p = new_engine()
d = eng.evaluate({'enabled': False, 'category': 'fixed_target'},
                 base_ctx(pnl_pct=99, current_price=199))
check("enabled=False 不触发", d['action'] == 'none')

nc = normalize_tp_config({'enabled': True, 'type': 'fixed', 'fixed_pct': 3.0})
check("旧结构 fixed → fixed_target/pct",
      nc['category'] == 'fixed_target' and nc['fixed_target']['mode'] == 'pct'
      and nc['fixed_target']['pct'] == 3.0)
nc = normalize_tp_config({'enabled': True, 'type': 'trailing', 'trailing_atr': 2.5})
check("旧结构 trailing → fixed_target/atr_multiple",
      nc['fixed_target']['mode'] == 'atr_multiple'
      and nc['fixed_target']['atr_multiple'] == 2.5)
d = eng.evaluate({'enabled': True, 'type': 'fixed', 'fixed_pct': 3.0},
                 base_ctx(pnl_pct=3.5, current_price=103.5))
check("旧结构直接评估可触发", d['action'] == 'close_all', str(d))
cleanup(p)

# ================================================================
print("== 1. 固定目标止盈 ==")
eng, p = new_engine()
c = cfg('fixed_target', {'mode': 'pct', 'pct': 5.0})
check("pct 未达标不触发",
      eng.evaluate(c, base_ctx(pnl_pct=4.9, current_price=104.9))['action'] == 'none')
check("pct 达标触发全平",
      eng.evaluate(c, base_ctx(pnl_pct=5.0, current_price=105.0))['action'] == 'close_all')
check("pct 亏损不触发",
      eng.evaluate(c, base_ctx(pnl_pct=-2.0, current_price=98.0))['action'] == 'none')

# R倍数: 止损 fixed 3% → R=3.0；目标 2R=6.0 价格距离
c = cfg('fixed_target', {'mode': 'r_multiple', 'r_multiple': 2.0})
ctx = base_ctx(pnl_pct=5.9, current_price=105.9,
               sl_cfg={'enabled': True, 'type': 'fixed', 'fixed_pct': 3.0})
check("R倍数 5.9<6.0 不触发", eng.evaluate(c, ctx)['action'] == 'none')
ctx = base_ctx(pnl_pct=6.0, current_price=106.0,
               sl_cfg={'enabled': True, 'type': 'fixed', 'fixed_pct': 3.0})
check("R倍数 6.0≥2R 触发", eng.evaluate(c, ctx)['action'] == 'close_all')
# 止损未启用 → 兜底 1R=1ATR=1.0，2R=2.0
ctx = base_ctx(pnl_pct=2.0, current_price=102.0)
check("R倍数 止损未启用兜底1R=1ATR 触发", eng.evaluate(c, ctx)['action'] == 'close_all')

c = cfg('fixed_target', {'mode': 'atr_multiple', 'atr_multiple': 3.0})
check("ATR倍数 2.9<3ATR 不触发",
      eng.evaluate(c, base_ctx(pnl_pct=2.9, current_price=102.9))['action'] == 'none')
check("ATR倍数 3.0≥3ATR 触发",
      eng.evaluate(c, base_ctx(pnl_pct=3.0, current_price=103.0))['action'] == 'close_all')
cleanup(p)

# ================================================================
print("== 2. 追踪止盈 ==")
# 回撤百分比（多头）：先冲高到 105（峰值盈利5%≥1%），再回落 2%
eng, p = new_engine()
c = cfg('trailing', {'mode': 'pullback_pct', 'activate_pct': 1.0, 'pullback_pct': 2.0})
eng.evaluate(c, base_ctx(pnl_pct=5.0, current_price=105.0))  # 建立峰值105
d = eng.evaluate(c, base_ctx(pnl_pct=3.5, current_price=103.5))  # 回撤1.43%
check("回撤% 回撤不足不触发", d['action'] == 'none', str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=2.8, current_price=102.8))  # 回撤2.10%
check("回撤% 达标触发", d['action'] == 'close_all', str(d))
cleanup(p)

# 回撤百分比（空头镜像）：谷值95 → 反弹
eng, p = new_engine()
eng.evaluate(c, base_ctx(is_long=False, pnl_pct=5.0, current_price=95.0))  # 谷值95
d = eng.evaluate(c, base_ctx(is_long=False, pnl_pct=2.9, current_price=97.1))  # 反弹2.21%
check("回撤%(空头) 反弹达标触发", d['action'] == 'close_all', str(d))
cleanup(p)

# 吊灯ATR（多头）：峰值106，回撤≥3ATR=3.0
eng, p = new_engine()
c = cfg('trailing', {'mode': 'chandelier', 'chandelier_atr': 3.0})
eng.evaluate(c, base_ctx(pnl_pct=6.0, current_price=106.0))
d = eng.evaluate(c, base_ctx(pnl_pct=3.1, current_price=103.1))  # 回撤2.9<3.0
check("吊灯 回撤2.9ATR不触发", d['action'] == 'none', str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=3.0, current_price=103.0))  # 回撤3.0
check("吊灯 回撤3.0ATR触发", d['action'] == 'close_all', str(d))
cleanup(p)

# R乘数阶梯：止损未启用 1R=1ATR=1.0。峰值103(3R) → 锁定线抬到 2R=102
eng, p = new_engine()
c = cfg('trailing', {'mode': 'r_ladder',
                     'r_levels': [[1.0, 0.0], [2.0, 1.0], [3.0, 2.0]]})
eng.evaluate(c, base_ctx(pnl_pct=3.0, current_price=103.0))  # 峰值3R
d = eng.evaluate(c, base_ctx(pnl_pct=2.1, current_price=102.1))  # 高于锁定线102
check("R阶梯 未破锁定线不触发", d['action'] == 'none', str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=2.0, current_price=102.0))  # 触及锁定线
check("R阶梯 触及2R锁定线触发", d['action'] == 'close_all', str(d))
cleanup(p)

# ================================================================
print("== 3. 动能衰竭止盈 ==")
eng, p = new_engine()
c = cfg('momentum', {'mode': 'hist_decay', 'decay_bars': 3, 'min_profit_pct': 0.5})
# 多头：柱体正且连续3根收缩（需4个值）
hist_ok = [0.5, 0.8, 0.6, 0.4, 0.2]
d = eng.evaluate(c, base_ctx(pnl_pct=1.0, current_price=101.0,
                             macd_hist_series=hist_ok))
check("柱衰减 连续收缩触发", d['action'] == 'close_all', str(d))
hist_no = [0.5, 0.8, 0.6, 0.7, 0.2]  # 中途反弹
d = eng.evaluate(c, base_ctx(pnl_pct=1.0, current_price=101.0,
                             macd_hist_series=hist_no))
check("柱衰减 非连续收缩不触发", d['action'] == 'none', str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=0.3, current_price=100.3,
                             macd_hist_series=hist_ok))
check("柱衰减 盈利<0.5%不触发", d['action'] == 'none', str(d))
# 空头镜像：柱体负且绝对值收缩
d = eng.evaluate(c, base_ctx(is_long=False, pnl_pct=1.0, current_price=99.0,
                             macd_hist_series=[-0.5, -0.8, -0.6, -0.4, -0.2]))
check("柱衰减(空头) 触发", d['action'] == 'close_all', str(d))

# 背离：价格新高但柱峰值降低（n=20, 前半 vs 后半）
c = cfg('momentum', {'mode': 'divergence', 'lookback': 20, 'min_profit_pct': 0.5})
closes = [100 + i * 0.1 for i in range(20)]           # 后半价格新高
hist_div = [1.0] * 10 + [0.5] * 10                     # 后半柱峰值降低
d = eng.evaluate(c, base_ctx(pnl_pct=1.5, current_price=101.9,
                             macd_hist_series=hist_div, close_series=closes))
check("顶背离 触发", d['action'] == 'close_all', str(d))
hist_nodiv = [0.5] * 10 + [1.0] * 10                   # 动能同步走强
d = eng.evaluate(c, base_ctx(pnl_pct=1.5, current_price=101.9,
                             macd_hist_series=hist_nodiv, close_series=closes))
check("无背离 不触发", d['action'] == 'none', str(d))
cleanup(p)

# ================================================================
print("== 4. 通道边界止盈 ==")
eng, p = new_engine()
c = cfg('channel', {'mode': 'outer'})
d = eng.evaluate(c, base_ctx(pnl_pct=2.0, current_price=102.0,
                             boll_upper=101.5, boll_middle=100, boll_lower=98.5))
check("外轨 多头触及上轨触发", d['action'] == 'close_all', str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=1.0, current_price=101.0,
                             boll_upper=101.5, boll_middle=100, boll_lower=98.5))
check("外轨 未触及不触发", d['action'] == 'none', str(d))
d = eng.evaluate(c, base_ctx(is_long=False, pnl_pct=2.0, current_price=98.0,
                             boll_upper=101.5, boll_middle=100, boll_lower=98.5))
check("外轨 空头触及下轨触发", d['action'] == 'close_all', str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=-1.0, current_price=102.0,
                             boll_upper=101.5, boll_middle=100, boll_lower=98.5))
check("外轨 亏损时不误触发", d['action'] == 'none', str(d))

c = cfg('channel', {'mode': 'middle', 'min_profit_pct': 0.5})
d = eng.evaluate(c, base_ctx(pnl_pct=1.0, current_price=101.0,
                             boll_upper=104, boll_middle=101.5, boll_lower=99))
check("中轨 多头跌破中轨触发", d['action'] == 'close_all', str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=0.3, current_price=100.3,
                             boll_upper=104, boll_middle=101.5, boll_lower=99))
check("中轨 盈利不足不触发", d['action'] == 'none', str(d))
cleanup(p)

# ================================================================
print("== 5. 分批止盈 ==")
eng, p = new_engine()
levels = [{'pct': 2, 'close_ratio': 0.33},
          {'pct': 4, 'close_ratio': 0.5},
          {'pct': 6, 'close_ratio': 1.0}]
c = cfg('ladder', {'levels': levels})
d = eng.evaluate(c, base_ctx(pnl_pct=1.5, current_price=101.5))
check("阶梯 未达首档不触发", d['action'] == 'none', str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=2.5, current_price=102.5))
check("阶梯 首档部分平33%", d['action'] == 'close_partial'
      and abs(d['close_ratio'] - 0.33) < 1e-9, str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=2.6, current_price=102.6))
check("阶梯 首档不重复触发", d['action'] == 'none', str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=4.2, current_price=104.2))
check("阶梯 第二档部分平50%", d['action'] == 'close_partial'
      and abs(d['close_ratio'] - 0.5) < 1e-9, str(d))
d = eng.evaluate(c, base_ctx(pnl_pct=6.1, current_price=106.1))
check("阶梯 末档清仓", d['action'] == 'close_all', str(d))
cleanup(p)

# 跳档合并：直接冲到 4.5%（跨1、2档）
eng, p = new_engine()
d = eng.evaluate(c, base_ctx(pnl_pct=4.5, current_price=104.5))
check("阶梯 跳档合并 33%+50%=83%", d['action'] == 'close_partial'
      and abs(d['close_ratio'] - 0.83) < 1e-9, str(d))
cleanup(p)

# 直接冲到末档 → 清仓
eng, p = new_engine()
d = eng.evaluate(c, base_ctx(pnl_pct=7.0, current_price=107.0))
check("阶梯 直冲末档清仓", d['action'] == 'close_all', str(d))
cleanup(p)

# ================================================================
print("== 6. 时间止盈兜底 ==")
eng, p = new_engine()
ts = {'enabled': True, 'max_bars': 10, 'min_profit_pct': 1.0}
c = cfg('none', time_stop=ts)
ctx = base_ctx(pnl_pct=0.2, current_price=100.2, short_period='1m')
d = eng.evaluate(c, ctx)
check("时间兜底 刚入场不触发", d['action'] == 'none', str(d))
# 人工把入场时间拨回 11 分钟前（11根1mK线 > 10）
key = 'TEST-USDT-SWAP:long'
eng._state[key]['entry_ts'] = time.time() - 11 * 60
d = eng.evaluate(c, ctx)
check("时间兜底 超10根K线且盈利0.2%<1% 触发", d['action'] == 'close_all', str(d))
cleanup(p)

# 盈利达标则不兜底
eng, p = new_engine()
ctx2 = base_ctx(pnl_pct=1.5, current_price=101.5, short_period='1m')
eng.evaluate(c, ctx2)
eng._state[key]['entry_ts'] = time.time() - 11 * 60
d = eng.evaluate(c, ctx2)
check("时间兜底 盈利1.5%≥1% 不触发", d['action'] == 'none', str(d))
# 时间兜底与主止盈共存：主止盈仍可评估
c2 = cfg('fixed_target', {'mode': 'pct', 'pct': 1.2}, time_stop=ts)
d = eng.evaluate(c2, ctx2)
check("兜底未触发时主止盈正常评估", d['action'] == 'close_all', str(d))
cleanup(p)

# ================================================================
print("== 7. 状态持久化与清理 ==")
eng, p = new_engine()
c = cfg('trailing', {'mode': 'pullback_pct', 'activate_pct': 1.0, 'pullback_pct': 2.0})
eng.evaluate(c, base_ctx(pnl_pct=5.0, current_price=105.0))  # 峰值105
# 模拟重启：新引擎实例从 DB 重新加载同一状态
eng2 = TakeProfitEngine(state_file=p)
st = eng2._state.get('TEST-USDT-SWAP:long', {})
check("重启后峰值保留", abs(st.get('peak', 0) - 105.0) < 1e-9, str(st))
d = eng2.evaluate(c, base_ctx(pnl_pct=2.8, current_price=102.8))
check("重启后追踪止盈仍可触发", d['action'] == 'close_all', str(d))
# 清理状态
eng2.clear_state('TEST-USDT-SWAP', True)
check("clear_state 清理成功", 'TEST-USDT-SWAP:long' not in eng2._state)
# 多空状态互不干扰
eng2.evaluate(c, base_ctx(pnl_pct=1.0, current_price=101.0))
eng2.evaluate(c, base_ctx(is_long=False, pnl_pct=1.0, current_price=99.0))
check("多空状态键分离",
      'TEST-USDT-SWAP:long' in eng2._state and 'TEST-USDT-SWAP:short' in eng2._state)
cleanup(p)

# ================================================================
print()
print(f"总计: {PASS + FAIL} | 通过: {PASS} | 失败: {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
