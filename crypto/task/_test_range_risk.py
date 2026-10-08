#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
trend_range_trader._check_range_risk_exit 离线单元测试
======================================================
运行: python crypto/task/_test_range_risk.py

设计要点：参照 _test_tp_combo.py 的内存隔离写法——在 import 被测模块前把
crypto.database / crypto.trader_state_repo / crypto.config_store_repo 换成
session_scope=None 的假模块，触发 trend_range_trader 既有的 ImportError 兜底
分支，全程零数据库读写。实例用 __new__ 绕过 __init__（不建 API 客户端、
不碰账本文件），只注入本函数用到的四个属性：
  _range_risk_state / _range_pause_until / pos_mgr(假) / _close_bucket(假)
task_log 替换为记录器以断言"关闭态不打【区间风险锁定】日志"；
模块级 time 替换为可控时钟以精确驱动冷却消化。

验收点（对应 2026-09 默认值改造：enabled=False、各数值 0 = 单项不启用）：
  1. 默认/显式关闭 + 各数值 0：不锁定、不写状态、不平仓
  2. 已生效的 _range_pause_until 冷却仍被消化（消化先于 enabled 检查，
     保证关闭开关不会把在途冷却变成永久禁入场）
  3. enabled=True 且 hard_stop_atr_mult=0：状态正常锁定，但价格打穿
     硬止损位也跳过判定、不平仓（mult=0 与"算出止损价=轨道本身"可区分）
  4. 对照组 mult>0：同场景立即触发硬止损全平，证明 0 守卫未误伤正路径
任何一条不达标即失败退出。
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..', '..')
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)


# ---- 在 import trend_range_trader 前把持久化依赖断掉（同 _test_tp_combo）----
# 保留真实 crypto 包（其 __init__ 仅为文档字符串），只把三个持久化子模块
# 换成假模块；否则 trend_range_trader 的 `from crypto.api_config import ...`
# 会因 crypto 不是真实包而报 ModuleNotFoundError。
import crypto  # noqa: E402  真实包，轻量

_fake_db = types.ModuleType('crypto.database')
_fake_db.session_scope = None
_fake_db.db_health = None
_fake_repo = types.ModuleType('crypto.trader_state_repo')
_fake_csr = types.ModuleType('crypto.config_store_repo')
sys.modules['crypto.database'] = _fake_db
sys.modules['crypto.trader_state_repo'] = _fake_repo
sys.modules['crypto.config_store_repo'] = _fake_csr

import trend_range_trader as trt  # noqa: E402
from trend_range_trader import TrendRangeTrader, BUCKET_RANGE  # noqa: E402

# 确认兜底分支生效：真连了库说明隔离失败，测试不可信
assert trt.session_scope is None, '数据库未被隔离，禁止继续（防止触碰业务库）'


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


# ---- 测试替身 ----------------------------------------------------------

class FakePosMgr:
    """只提供 get_position：返回预设的 (持仓张数, 均价)"""

    def __init__(self, held=0.0, avg_px=0.0):
        self.held = held
        self.avg_px = avg_px

    def get_position(self, inst_id, bucket, direction):
        return self.held, self.avg_px


class LogRecorder:
    """替换 task_log：留痕所有 info/warning 文本供断言"""

    def __init__(self):
        self.lines = []

    def info(self, msg, *a, **kw):
        self.lines.append(('info', str(msg)))

    def warning(self, msg, *a, **kw):
        self.lines.append(('warning', str(msg)))

    def error(self, msg, *a, **kw):
        self.lines.append(('error', str(msg)))

    def has(self, kw_text):
        return any(kw_text in m for _, m in self.lines)


class FakeClock:
    """替换模块级 time：仅驱动 time.time()，可手动拨表"""

    def __init__(self, now=1000.0):
        self.now = float(now)

    def time(self):
        return self.now


def new_trader(held=0.0, avg_px=0.0, clock=None):
    """绕过 __init__ 构造裸实例，注入 _check_range_risk_exit 所需的最小依赖"""
    t = TrendRangeTrader.__new__(TrendRangeTrader)
    t._range_risk_state = {}
    t._range_pause_until = {}
    t.pos_mgr = FakePosMgr(held, avg_px)
    t.close_calls = []
    t._close_bucket = lambda *a, **kw: (t.close_calls.append((a, kw)), True)[1]
    trt.task_log = LogRecorder()
    trt.time = FakeClock() if clock is None else clock
    return t


def ctx(price, direction='short', short_period='1m', **kw):
    c = {
        'rid': '', 'price': price,
        'short_period': short_period, 'long_period': '4H', 'run_id': '',
        'analysis': {'atr_value': 2.0, 'boll_upper': 101.0, 'boll_lower': 99.0},
        'long_dir': direction,
    }
    c.update(kw)
    return c


# 显式"关闭 + 全 0"配置（与改造后的 _RANGE_RISK_DEFAULTS 一致）
# 注意：_range_risk_cfg 读的是 rcfg['risk'] 子块，故配置必须包在 risk 键下
OFF_CFG = {'risk': {'enabled': False, 'hard_stop_atr_mult': 0, 'max_hold_bars': 0,
                    'breakdown_rounds': 0, 'cooldown_bars': 0}}


# ================================================================
print("== 0. 类内置默认值口径 ==")
d = TrendRangeTrader._RANGE_RISK_DEFAULTS
check("默认 enabled=False", d['enabled'] is False, str(d))
check("默认各数值均为 0",
      all(d[k] == 0 for k in ('hard_stop_atr_mult', 'max_hold_bars',
                              'breakdown_rounds', 'cooldown_bars')), str(d))


# ================================================================
print("== 1. 关闭态（默认缺失 risk 块 / 显式全 0）：零动作 ==")
for label, rcfg in (('缺失 risk 块走内置默认', {}), ('显式 enabled=False 全 0', OFF_CFG)):
    t = new_trader(held=5.0, avg_px=100.0)
    c = ctx(120.0)  # 空头且现价已远超冻结上轨：若风控在跑必然有动作
    t._check_range_risk_exit('RISK-USDT-SWAP', rcfg, c)
    log = trt.task_log
    check(f"[{label}] 不打【区间风险锁定】日志", not log.has('区间风险锁定'), str(log.lines))
    check(f"[{label}] 不写 _range_risk_state", t._range_risk_state == {},
          str(t._range_risk_state))
    check(f"[{label}] 不平仓（_close_bucket 零调用）", t.close_calls == [],
          str(t.close_calls))
    check(f"[{label}] 不干预入场（range_allow_entry 未置位）",
          'range_allow_entry' not in c, str(c.get('range_allow_entry')))


# ================================================================
print("== 2. 已生效的冷却仍被消化（先于 enabled 检查）==")
clock = FakeClock(1000.0)
t = new_trader(held=5.0, avg_px=100.0, clock=clock)
inst = 'COOL-USDT-SWAP'
t._range_pause_until[inst] = clock.now + 10.0  # 在途冷却，10 秒后到期

c1 = ctx(100.0)
t._check_range_risk_exit(inst, OFF_CFG, c1)
check("冷却未到期：range_allow_entry=False（禁入场仍生效）",
      c1.get('range_allow_entry') is False, str(c1))
check("冷却未到期：_range_pause_until 保留", inst in t._range_pause_until)

clock.now += 11.0  # 拨表越过到期点
c2 = ctx(100.0)
t._check_range_risk_exit(inst, OFF_CFG, c2)
check("冷却到期：_range_pause_until 被消化移除",
      inst not in t._range_pause_until, str(t._range_pause_until))
check("冷却到期后：不再禁入场（range_allow_entry 未置位）",
      'range_allow_entry' not in c2, str(c2))
check("关闭态消化冷却全程零平仓", t.close_calls == [], str(t.close_calls))


# ================================================================
print("== 3. enabled=True 且 hard_stop_atr_mult=0：跳过硬止损判定 ==")
clock = FakeClock(2000.0)
t = new_trader(held=5.0, avg_px=100.0, clock=clock)
inst = 'ZERO-USDT-SWAP'
cfg_on_zero = {'risk': {'enabled': True, 'hard_stop_atr_mult': 0,
                        'max_hold_bars': 0, 'breakdown_rounds': 0,
                        'cooldown_bars': 0}}

# 首轮：现价在轨内 → 正常锁定状态（enabled=True 时锁定应发生）
t._check_range_risk_exit(inst, cfg_on_zero, ctx(100.0))
log = trt.task_log
st = t._range_risk_state.get(inst) or {}
check("enabled=True 首轮正常锁定状态", bool(st), str(t._range_risk_state))
check("打【区间风险锁定】日志", log.has('区间风险锁定'), str(log.lines))
check("mult=0 时硬止损价=冻结上轨本身（空头 101+0×ATR）",
      abs(float(st.get('hard_stop', 0)) - 101.0) < 1e-9, str(st))

# 后续三轮：现价 104 ≥ hard_stop(101)，若 mult=0 未被守卫则首轮就该硬止损平仓
for i in range(3):
    clock.now += 60.0
    t._check_range_risk_exit(inst, cfg_on_zero, ctx(104.0))
check("mult=0：价格打穿硬止损位也不平仓", t.close_calls == [], str(t.close_calls))
check("mult=0：无硬止损退出日志", not log.has('区间硬止损'), str(log.lines))
check("mult=0：状态保留供后续轮继续使用", inst in t._range_risk_state)
# breakdown_rounds=0 / max_hold_bars=0 也同时验证：价在轨外 3 轮 + 已持仓
# 180 秒（>0 根），仍不触发破位/时间退出
check("breakdown_rounds=0：轨外 3 轮不判破位", not log.has('破位'), str(log.lines))
check("max_hold_bars=0：持仓超时不判时间退出",
      not log.has('时间退出'), str(log.lines))


# ================================================================
print("== 4. 对照组：mult=1.0 同场景立即触发硬止损（证明守卫未误伤）==")
clock = FakeClock(3000.0)
t = new_trader(held=5.0, avg_px=100.0, clock=clock)
inst = 'CTRL-USDT-SWAP'
cfg_on = {'risk': {'enabled': True, 'hard_stop_atr_mult': 1.0,
                   'max_hold_bars': 0, 'breakdown_rounds': 0,
                   'cooldown_bars': 0}}
t._check_range_risk_exit(inst, cfg_on, ctx(100.0))  # 锁定：hard_stop=101+1×2=103
st = t._range_risk_state.get(inst) or {}
check("对照组硬止损价=103（上轨+1×ATR0）",
      abs(float(st.get('hard_stop', 0)) - 103.0) < 1e-9, str(st))
clock.now += 60.0
c_ctrl = ctx(104.0)  # 104 ≥ 103 → 单轮触发
t._check_range_risk_exit(inst, cfg_on, c_ctrl)
log = trt.task_log
check("对照组单轮触发硬止损全平", len(t.close_calls) == 1, str(t.close_calls))
check("平仓原因为【区间硬止损】", log.has('区间硬止损'), str(log.lines))
check("平仓后风险状态被销号", inst not in t._range_risk_state,
      str(t._range_risk_state))
check("触发轮禁入场 range_allow_entry=False",
      c_ctrl.get('range_allow_entry') is False, str(c_ctrl))
check("cooldown_bars=0 不设新冷却", t._range_pause_until == {},
      str(t._range_pause_until))


# ================================================================
print()
print(f"总计: {PASS + FAIL} | 通过: {PASS} | 失败: {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
