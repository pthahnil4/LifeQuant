#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
B 仓位「keltner_boll 独立信号引擎」模式冒烟测试（纯内存、不触网、不碰业务库）
==============================================================================
验证 direction_source='keltner' 独立模式与详情页回测 simulate() 的信号级同源，
以及三处 Pro3 解耦点（额度池归属 / 反转清理 / 反向守卫）在调度器中的接线。

覆盖计划第五节的五项断言：
  1. 价位同源：current_range_signal 的 entry_px/exit_px == simulate 内部同一
     _bar_levels(最后一根短K top/bot/atr) 的输出（多头/空头各验一次）；
     并用真实触发的 simulate 成交单反查 _bar_levels，证明二者同一价位口径。
  2. 空头公式回归：_bar_levels 空头 entry=top+entry×a、exit=bot-exit×a，
     专门覆盖 0.7/0.3 非对称场景，锁死 expand_bands 对称边界把倍数用反的旧 bug。
  3. 方向一致性：current_range_signal().direction 与长周期 keltner 映射一致
     （升势→long、跌势→short），且 == simulate 最后一根短K 的 LONG_DIR 映射。
  4. 调度接线（离线）：用 fake pos_mgr 走 _run_range_position，断言 process_range
     收到的 plan['target_dir']==range_dir、entry/exit 来自信号、dir_source 标记；
     反向守卫 exclude_buckets=(BUCKET_RANGE,) 使 B 的合法反向持仓不计入强平；
     _run_range_independent_cleanup 按 keltner flip 强平旧方向 B 仓。
  5. fail-safe：current_range_signal 抛异常时 range_dir=None，主调度不崩、B 本轮不挂单。

隔离手法参照 crypto/task/_test_range_risk.py：import 被测模块前把持久化子模块换成
session_scope=None 的假模块，实例用 __new__ 绕过 __init__，只注入被测方法用到的
最小依赖；task_log 替换为记录器。合成数据带明确斜率（长周期 0.5*i），避免完美线性
数据导致 keltner 覆盖率为 0 的假失败。

运行：python crypto/_smoke_range_independent.py
"""

import os
import sys
import types

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))          # .../crypto
_ROOT = os.path.dirname(_HERE)                               # .../cryptoTrade
_TASK = os.path.join(_HERE, 'task')
_STRAT = os.path.join(_HERE, 'strategy')
for _d in (_ROOT, _HERE, _TASK, _STRAT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import keltner_boll as kb  # noqa: E402

# ---- 在 import trend_range_trader 前把持久化依赖断掉（同 _test_range_risk）----
import crypto  # noqa: E402  真实包，轻量（__init__ 仅文档字符串）

_fake_db = types.ModuleType('crypto.database')
_fake_db.session_scope = None
_fake_db.db_health = None
sys.modules['crypto.database'] = _fake_db
sys.modules['crypto.trader_state_repo'] = types.ModuleType('crypto.trader_state_repo')
sys.modules['crypto.config_store_repo'] = types.ModuleType('crypto.config_store_repo')

import trend_range_trader as trt  # noqa: E402
from trend_range_trader import (  # noqa: E402
    TrendRangeTrader, BUCKET_RANGE, BUCKET_TREND)

# 确认真连了库说明隔离失败，测试不可信
assert trt.session_scope is None, '数据库未被隔离，禁止继续（防止触碰业务库）'

_PASS = 0
_FAIL = 0


def check(name, cond, detail=''):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def _close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# ---------------------------------------------------------------------------
# 合成行情工具（长周期用明确斜率保证 keltner 非零覆盖率）
# ---------------------------------------------------------------------------

def _mk_df(closes, spread=0.5, freq='1h'):
    idx = pd.date_range('2024-01-01', periods=len(closes), freq=freq)
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({'open': c, 'high': c + spread,
                         'low': c - spread, 'close': c}, index=idx)


def _range_osc(n=600, base=100.0, amp=6.0, spread=0.8):
    """区间震荡短周期：适合反复触发 BOLL 两端成交"""
    return _mk_df([base + amp * np.sin(i / 8.0) for i in range(n)], spread=spread)


def _up_long(n=600, start=100.0, step=0.5):
    return _mk_df([start + step * i for i in range(n)], freq='4h')


def _down_long(n=600, start=400.0, step=0.5):
    return _mk_df([start - step * i for i in range(n)], freq='4h')


def _cfg(entry=0.7, exit_=0.3, indicator='keltner'):
    return kb.KeltnerBollConfig(trend_indicator=indicator, entry_atr_mult=entry,
                                exit_atr_mult=exit_, atr_period=14,
                                boll_period=20, boll_dev=2.0)


def _fetcher_for(df_short, df_long):
    """按 bar 分派合成数据的取数函数（长周期用 4h，短周期用其余）"""
    def _f(inst_id, bar):
        return df_long if str(bar).lower().endswith('h') and bar != '1h' else df_short
    return _f


# ---------------------------------------------------------------------------
# 1 & 2. 价位同源 + 空头公式回归
# ---------------------------------------------------------------------------

def test_price_parity():
    print("\n[1] current_range_signal 与 _bar_levels/simulate 价位同源")
    cfg = _cfg(entry=0.7, exit_=0.3)

    for label, df_long, want_dir in (('多头', _up_long(), 'long'),
                                     ('空头', _down_long(), 'short')):
        df_short = _range_osc()
        sig = kb.current_range_signal('X-USDT-SWAP', '1h', '4h', cfg,
                                      fetcher=_fetcher_for(df_short, df_long))
        # 用同一份 df 手工复算最后一根短K 的 top/bot/atr → _bar_levels
        bands = kb.boll_bands(df_short, cfg.boll_period, cfg.boll_dev)
        atr_s = kb.atr(df_short, cfg.atr_period)
        top = float(bands['top'].values[-1])
        bot = float(bands['bot'].values[-1])
        a = float(atr_s.values[-1])
        lv = kb._bar_levels(top, bot, a, cfg.entry_atr_mult, cfg.exit_atr_mult)
        check(f"{label}：方向={want_dir}", sig['direction'] == want_dir,
              str(sig['direction']))
        exp_entry = lv['long_entry'] if want_dir == 'long' else lv['short_entry']
        exp_exit = lv['long_exit'] if want_dir == 'long' else lv['short_exit']
        check(f"{label}：entry_px == _bar_levels 同源触发价",
              _close(sig['entry_px'], exp_entry), f"{sig['entry_px']} vs {exp_entry}")
        check(f"{label}：exit_px == _bar_levels 同源触发价",
              _close(sig['exit_px'], exp_exit), f"{sig['exit_px']} vs {exp_exit}")
        check(f"{label}：band_upper/lower 为触发价的上/下包络",
              _close(sig['band_upper'], max(exp_entry, exp_exit))
              and _close(sig['band_lower'], min(exp_entry, exp_exit)))

    print("\n[2] _bar_levels 空头侧公式回归（0.7/0.3 非对称，锁死旧 bug）")
    top, bot, a = 110.0, 90.0, 4.0
    lv = kb._bar_levels(top, bot, a, 0.7, 0.3)
    check("空头 entry = top + entry_mult×ATR = 110+0.7×4 = 112.8",
          _close(lv['short_entry'], 112.8), str(lv['short_entry']))
    check("空头 exit = bot − exit_mult×ATR = 90−0.3×4 = 88.8",
          _close(lv['short_exit'], 88.8), str(lv['short_exit']))
    check("多头 entry = bot − entry_mult×ATR = 90−0.7×4 = 87.2",
          _close(lv['long_entry'], 87.2), str(lv['long_entry']))
    check("多头 exit = top + exit_mult×ATR = 110+0.3×4 = 111.2",
          _close(lv['long_exit'], 111.2), str(lv['long_exit']))
    # 旧 expand_bands 对称边界会把空头 entry/exit 倍数用反：验证二者确实不等价
    strat = kb.KeltnerBollStrategy(cfg)
    sym = strat.expand_bands(top, bot, a)
    check("对称边界(旧口径)空头 entry=range_upper 与 _bar_levels 不等价（bug 已规避）",
          not _close(sym['range_upper'], lv['short_entry']),
          f"sym={sym['range_upper']} vs bar={lv['short_entry']}")
    # ATR 无效 fail-safe
    lv_nan = kb._bar_levels(top, bot, float('nan'), 0.7, 0.3)
    check("ATR=NaN → 按 0 外扩（退化纯 BOLL 触发价）",
          _close(lv_nan['short_entry'], top) and _close(lv_nan['long_entry'], bot))

    print("\n[1b] simulate 成交价反查 _bar_levels（证明回测同一价位口径）")
    df_short = _range_osc(amp=8.0, spread=1.0)
    for label, df_long, want in (('多头', _up_long(), 'rise'),
                                 ('空头', _down_long(), 'fall')):
        sym_cfg = _cfg(entry=0.0, exit_=0.0)  # mult=0 保证震荡市稳定触发成交
        res = kb.simulate(df_short, df_long, sym_cfg)
        recs = [r for r in res['trade_records'] if r['dir'] == want]
        if not recs:
            check(f"{label}：simulate 产生 {want} 成交", False, "无成交记录")
            continue
        check(f"{label}：simulate 产生 {want} 成交", True)
        bands = kb.boll_bands(df_short, sym_cfg.boll_period, sym_cfg.boll_dev)
        atr_s = kb.atr(df_short, sym_cfg.atr_period)
        tops, bots, atrs = bands['top'].values, bands['bot'].values, atr_s.values
        ok = True
        for r in recs[:5]:  # 抽验前 5 笔
            ei = df_short.index.get_loc(r['entry_time'])
            xi = df_short.index.get_loc(r['exit_time'])
            lve = kb._bar_levels(tops[ei], bots[ei], atrs[ei], 0.0, 0.0)
            lvx = kb._bar_levels(tops[xi], bots[xi], atrs[xi], 0.0, 0.0)
            if want == 'rise':
                ok = ok and _close(r['open_price'], lve['long_entry']) \
                    and _close(r['close_price'], lvx['long_exit'])
            else:
                ok = ok and _close(r['open_price'], lve['short_entry']) \
                    and _close(r['close_price'], lvx['short_exit'])
        check(f"{label}：每笔 open/close 价 == 对应 bar 的 _bar_levels 触发价", ok)


# ---------------------------------------------------------------------------
# 3. 方向一致性
# ---------------------------------------------------------------------------

def test_direction_consistency():
    print("\n[3] current_range_signal 方向与 simulate LONG_DIR 一致")
    cfg = _cfg()
    for label, df_long, want in (('升势', _up_long(), 'long'),
                                 ('跌势', _down_long(), 'short')):
        df_short = _range_osc()
        sig = kb.current_range_signal('X-USDT-SWAP', '1h', '4h', cfg,
                                      fetcher=_fetcher_for(df_short, df_long))
        # simulate 内部把长周期方向 merge_asof 到短周期 LONG_DIR，取最后一根比对
        dirs = kb.compute_direction_series(df_long, cfg.trend_indicator)
        long_ts = pd.DataFrame({'ts': pd.to_datetime(df_long.index), 'dir': dirs})
        short_ts = pd.DataFrame({'ts': pd.to_datetime(df_short.index)})
        aligned = pd.merge_asof(short_ts.sort_values('ts'), long_ts.sort_values('ts'),
                                on='ts', direction='backward')
        last_dir = {'rise': 'long', 'fall': 'short'}.get(
            kb.value_to_dir(float(aligned['dir'].values[-1])))
        check(f"{label}：signal.direction == {want}", sig['direction'] == want,
              str(sig['direction']))
        check(f"{label}：signal.direction == simulate 最后短K LONG_DIR 映射",
              sig['direction'] == last_dir, f"{sig['direction']} vs {last_dir}")

    # 方向为中性时 entry/exit 必须为 None（fail-safe 契约）
    df_flat_long = _mk_df([100.0] * 600, freq='4h')  # 恒定价格 → keltner 中性
    sig_none = kb.current_range_signal('X-USDT-SWAP', '1h', '4h', cfg,
                                       fetcher=_fetcher_for(_range_osc(), df_flat_long))
    check("长周期中性 → direction=None 且 entry/exit=None",
          sig_none['direction'] is None and sig_none['entry_px'] is None
          and sig_none['exit_px'] is None, str(sig_none['direction']))


# ---------------------------------------------------------------------------
# 调度器离线替身
# ---------------------------------------------------------------------------

class LogRecorder:
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


class FakePosMgr:
    """支持 get_position/process_range/cancel_bucket_orders/get_book 的替身"""

    def __init__(self, positions=None):
        # positions: {(bucket, direction): held}
        self.positions = positions or {}
        self.range_plans = []

    def get_position(self, inst_id, bucket, direction):
        return self.positions.get((bucket, direction), 0.0), 0.0

    def process_range(self, inst_id, plan, cross_pos, isolated_pos):
        self.range_plans.append(plan)
        return {'actions': [], 'fills': []}

    def cancel_bucket_orders(self, inst_id, bucket, reason):
        return 0

    def get_book(self, inst_id, bucket):
        return {'held': {'long': 0.0, 'short': 0.0}}


def new_trader(pos_mgr=None):
    t = TrendRangeTrader.__new__(TrendRangeTrader)
    t.pos_mgr = pos_mgr if pos_mgr is not None else FakePosMgr()
    t.close_calls = []
    t._close_bucket_smart = lambda *a, **kw: (t.close_calls.append((a, kw)), True)[1]
    t._check_range_risk_exit = lambda *a, **kw: None
    t._resolve_size = lambda *a, **kw: 1.0
    t._notify_fills = lambda *a, **kw: None
    t._kb_adapter = object()  # 非 None，令 _compute_range_independent_signal 继续
    trt.task_log = LogRecorder()
    return t


# ---------------------------------------------------------------------------
# 4. 调度接线（离线）
# ---------------------------------------------------------------------------

def test_scheduler_wiring():
    print("\n[4a] _run_range_position 独立模式：plan 来自 keltner 信号")
    pm = FakePosMgr()
    t = new_trader(pm)
    sig = {'direction': 'long', 'entry_px': 95.5, 'exit_px': 106.2,
           'band_upper': 106.2, 'band_lower': 95.5, 'top': 105.0, 'bot': 96.0,
           'atr': 2.0, 'indicator': 'keltner', 'conf': 1.0}
    ctx = {'rid': '', 'leverage': 5.0, 'cross_pos': 0.0, 'isolated_pos': 0.0,
           'range_independent': True, 'range_dir': 'long', 'range_signal': sig,
           'range_allow_entry': True}
    out = t._run_range_position('WIR-USDT-SWAP', {'enabled': True}, ctx)
    check("独立模式返回挂单结果结构", isinstance(out, dict) and 'actions' in out)
    check("process_range 被调用一次", len(pm.range_plans) == 1, str(pm.range_plans))
    plan = pm.range_plans[0] if pm.range_plans else {}
    check("plan['target_dir'] == range_dir(long)", plan.get('target_dir') == 'long',
          str(plan.get('target_dir')))
    check("plan['entry_px'] 来自信号(95.5)", _close(plan.get('entry_px'), 95.5))
    check("plan['exit_px'] 来自信号(106.2)", _close(plan.get('exit_px'), 106.2))
    check("plan['dir_source'] 标记 keltner_boll独立",
          plan.get('dir_source') == 'keltner_boll独立', str(plan.get('dir_source')))
    check("打【区间独立挂单】日志记录目标价", trt.task_log.has('区间独立挂单'))

    # range_dir=None → 不挂单直接返回
    pm2 = FakePosMgr()
    t2 = new_trader(pm2)
    ctx2 = dict(ctx, range_dir=None, range_signal={'direction': None,
                                                   'entry_px': None, 'exit_px': None})
    out2 = t2._run_range_position('WIR-USDT-SWAP', {'enabled': True}, ctx2)
    check("range_dir=None → process_range 未被调用（本轮不挂单）",
          pm2.range_plans == [] and out2 == {'actions': [], 'fills': []})
    check("range_dir=None → 打【区间跳过·独立】日志",
          trt.task_log.has('区间跳过·独立'))

    print("\n[4b] 反向守卫 exclude_buckets：B 的合法反向持仓不计入强平")
    # Pro3 long_direction=long → rev_dir=short（读 isolated 净空）
    # B(BUCKET_RANGE) 持 5 张空头（keltner 独立方向合法），A(BUCKET_TREND) 无持仓
    pm3 = FakePosMgr({(BUCKET_RANGE, 'short'): 5.0})
    t3 = new_trader(pm3)
    # 不排除 → B 被判为账本内反向冲突仓
    res_no_ex = t3._detect_reverse_position('G-U-SWAP', 'long', 0.0, -5.0,
                                            exclude_buckets=())
    check("未排除时 B 空头被计入反向冲突 ledger",
          res_no_ex is not None
          and any(b == BUCKET_RANGE for b, _ in res_no_ex['ledger']),
          str(res_no_ex))
    # 排除 BUCKET_RANGE → owned 仍抵扣 5 张（unbooked=0），ledger 为空 → None
    res_ex = t3._detect_reverse_position('G-U-SWAP', 'long', 0.0, -5.0,
                                         exclude_buckets=(BUCKET_RANGE,))
    check("排除后 B 空头不列入 ledger（不被强平）",
          res_ex is None or all(b != BUCKET_RANGE for b, _ in res_ex['ledger']),
          str(res_ex))
    check("排除后 owned 仍抵扣真实净持仓（不误判为账本外人工单）",
          res_ex is None, str(res_ex))

    # A 趋势仓的反向持仓仍须被抓（exclude 只放 B，不放 A）
    pm4 = FakePosMgr({(BUCKET_TREND, 'short'): 5.0})
    t4 = new_trader(pm4)
    res_a = t4._detect_reverse_position('G-U-SWAP', 'long', 0.0, -5.0,
                                        exclude_buckets=(BUCKET_RANGE,))
    check("A 趋势仓反向持仓仍被守卫捕获（exclude 不误伤 A）",
          res_a is not None and any(b == BUCKET_TREND for b, _ in res_a['ledger']),
          str(res_a))

    print("\n[4c] _run_range_independent_cleanup：按 keltner flip 强平旧方向 B 仓")
    # range_dir=long，B 仍持旧方向空头 5 张 → 应全平空头
    pm5 = FakePosMgr({(BUCKET_RANGE, 'short'): 5.0})
    t5 = new_trader(pm5)
    ctx5 = {'rid': '', 'range_dir': 'long', 'run_id': '', 'short_period': '1m',
            'long_period': '4H', 'price': 100.0, 'leverage': 5.0}
    did = t5._run_range_independent_cleanup('CLN-USDT-SWAP', ctx5)
    check("方向翻多→强平旧空头 B 仓（返回 True）", did is True)
    check("调用 _close_bucket_smart 平 BUCKET_RANGE 空头",
          len(t5.close_calls) == 1
          and t5.close_calls[0][0][1] == BUCKET_RANGE
          and t5.close_calls[0][0][2] == 'short', str(t5.close_calls))
    check("平仓 scene='reversal'", t5.close_calls[0][1].get('scene') == 'reversal',
          str(t5.close_calls[0][1]))

    # range_dir=None → 不清理
    pm6 = FakePosMgr({(BUCKET_RANGE, 'short'): 5.0})
    t6 = new_trader(pm6)
    did6 = t6._run_range_independent_cleanup('CLN-USDT-SWAP',
                                             {'rid': '', 'range_dir': None})
    check("range_dir=None → 不清理（持仓保留）",
          did6 is False and t6.close_calls == [])

    # 无旧方向持仓 → 不清理
    pm7 = FakePosMgr()
    t7 = new_trader(pm7)
    did7 = t7._run_range_independent_cleanup('CLN-USDT-SWAP',
                                             {'rid': '', 'range_dir': 'long'})
    check("无旧方向 B 持仓 → 不清理", did7 is False and t7.close_calls == [])

    # 清理失败 → 置 range_allow_entry=False（本轮禁开新仓，下轮重试）
    pm8 = FakePosMgr({(BUCKET_RANGE, 'short'): 5.0})
    t8 = new_trader(pm8)
    t8._close_bucket_smart = lambda *a, **kw: (t8.close_calls.append((a, kw)), False)[1]
    ctx8 = {'rid': '', 'range_dir': 'long', 'run_id': '', 'short_period': '1m',
            'long_period': '4H', 'price': 100.0, 'leverage': 5.0}
    did8 = t8._run_range_independent_cleanup('CLN-USDT-SWAP', ctx8)
    check("清理失败 → 返回 False 且置 range_allow_entry=False",
          did8 is False and ctx8.get('range_allow_entry') is False, str(ctx8))


# ---------------------------------------------------------------------------
# 5. fail-safe
# ---------------------------------------------------------------------------

def test_failsafe():
    print("\n[5] current_range_signal 异常 → range_dir=None，主调度不崩、B 不挂单")
    orig = kb.current_range_signal

    def boom(*a, **kw):
        raise RuntimeError("网络中断")
    kb.current_range_signal = boom  # _compute_range_independent_signal 内部按需 import
    try:
        t = new_trader()
        ctx = {'rid': '', 'short_period': '1m', 'long_period': '4H',
               'range_independent': True, 'range_dir': 'x', 'range_signal': 'x'}
        raised = False
        try:
            t._compute_range_independent_signal('FS-USDT-SWAP',
                                                {'strategy': 'keltner_boll',
                                                 'direction_source': 'keltner'}, ctx)
        except Exception:
            raised = True
        check("信号计算异常不向上抛（fail-safe）", not raised)
        check("异常后 range_dir=None", ctx['range_dir'] is None, str(ctx['range_dir']))
        check("异常后 range_signal=None", ctx['range_signal'] is None)
        check("异常打 warning 日志", trt.task_log.has('计算失败')
              or trt.task_log.has('fail-safe'))

        # 该 ctx 直接进 _run_range_position → 不挂单
        pm = FakePosMgr()
        t.pos_mgr = pm
        ctx.update({'leverage': 5.0, 'cross_pos': 0.0, 'isolated_pos': 0.0,
                    'range_allow_entry': True})
        out = t._run_range_position('FS-USDT-SWAP', {'enabled': True}, ctx)
        check("fail-safe 后 B 本轮不挂单（process_range 未调用）",
              pm.range_plans == [] and out == {'actions': [], 'fills': []})
    finally:
        kb.current_range_signal = orig

    # _range_is_independent 判定口径
    t = new_trader()
    check("strategy=keltner_boll + direction_source=keltner → 独立模式",
          t._range_is_independent({'strategy': 'keltner_boll',
                                   'direction_source': 'keltner'}) is True)
    check("缺省 direction_source → pro3 存量模式（非独立）",
          t._range_is_independent({'strategy': 'keltner_boll'}) is False)
    check("direction_source=keltner 但 strategy=boll → 非独立（须搭配 keltner_boll）",
          t._range_is_independent({'strategy': 'boll',
                                   'direction_source': 'keltner'}) is False)


def main():
    print("=" * 70)
    print("  B 仓位 keltner_boll 独立信号引擎 冒烟测试")
    print("=" * 70)
    test_price_parity()
    test_direction_consistency()
    test_scheduler_wiring()
    test_failsafe()
    print("\n" + "=" * 70)
    print(f"  结果：{_PASS} 通过 / {_FAIL} 失败")
    print("=" * 70)
    return 0 if _FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
