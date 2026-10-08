#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
双仓位架构离线冒烟测试（不联网、不下真单）
============================================
用 Mock 交易执行器驱动 DualPositionOrderManager，验证两个相互独立仓位的
挂单生命周期与本地账本，并回归交易配置的读写校验：

  1. 趋势跟踪 dual（双周期单向）：开仓窗口挂单 → 成交入账 → 平仓窗口挂单 → 平仓成交
  2. 趋势跟踪 single（单周期双向）：方向翻转撤旧挂单并反向重挂
  3. 区间波动：下轨挂开仓 → 边界漂移改价 → 成交转挂上轨平仓 → 平仓成交后循环重挂
  4. reduce-only 双层封顶：本篮子可挂平仓量扣除另一篮子已挂量
  5. 账本对账：真实持仓小于账本合计时按比例缩减
  6. clear_bucket：撤单 + 清空账本
  7. 配置校验：实盘配置通过 / 非法双仓位配置被拦截
  8. trend_range_trader 接口面：新签名与 force_close_manual 就位
  9. 前端配置页：JS 引用的控件 id 与 HTML 一致，旧分批控件已删尽

运行: python crypto/task/_smoke_dual_position.py
"""

import os
import sys
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crypto.task.utils.position_order_manager import (  # noqa: E402
    DualPositionOrderManager, BUCKET_TREND, BUCKET_RANGE)
import crypto.task.utils.position_order_manager as _pom  # noqa: E402

# ---- 内存沙箱：账本持久化层打桩，不碰生产 MySQL ----
# （生产调度器可能正在并发读写 pos_* 表，旧版“快照→清场→恢复”会丢生产写入）
import copy as _copy  # noqa: E402

_MEM_STATE = {}


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeRepo:
    @staticmethod
    def load_position_state(session):
        return _copy.deepcopy(_MEM_STATE)

    @staticmethod
    def save_position_state(session, data):
        _MEM_STATE.clear()
        _MEM_STATE.update(data or {})

    @staticmethod
    def save_inst_position(session, inst_id, s):
        _MEM_STATE[inst_id] = _copy.deepcopy(s or {})


def _fake_session_scope():
    return _FakeSession()


_pom.session_scope = _fake_session_scope
_pom.state_repo = _FakeRepo

INST = 'NEAR-USDT-SWAP'

_passed = 0
_failed = []


def check(name, cond, detail=''):
    global _passed
    if cond:
        _passed += 1
        print(f'  [OK] {name}')
    else:
        _failed.append(name)
        print(f'  [FAIL] {name} {detail}')


class MockExecutor:
    """最小可用的交易执行器替身：内存记账，支持成交/撤销/改单模拟"""

    def __init__(self):
        self.seq = 0
        self.orders = {}
        self.lev_calls = []

    def _new(self, inst_id, side, amount, price, mode, reduce_only):
        self.seq += 1
        oid = f'O{self.seq}'
        self.orders[oid] = {'inst': inst_id, 'side': side, 'amount': float(amount),
                            'price': float(price or 0), 'mode': mode,
                            'reduce': reduce_only, 'state': 'live',
                            'accFillSz': 0.0, 'avgPx': 0.0}
        return oid

    def execute_trade(self, inst_id, side, amount, price=None, order_type='limit',
                      trading_mode='cross', pos_side=None, cl_ord_id=None, **_kw):
        oid = self._new(inst_id, side, amount, price, trading_mode, False)
        self.orders[oid]['cl_ord_id'] = cl_ord_id
        return {'success': True, 'order_id': oid}

    def execute_reduce_only_order(self, inst_id, side, amount, trading_mode='cross',
                                  order_type='market', price=None, cl_ord_id=None,
                                  pos_side=None, **_kw):
        oid = self._new(inst_id, side, amount, price, trading_mode, True)
        self.orders[oid]['cl_ord_id'] = cl_ord_id
        return {'success': True, 'order_id': oid}

    def get_order_info(self, inst_id, ord_id):
        o = self.orders.get(ord_id)
        if not o:
            return None
        return {'state': o['state'], 'accFillSz': o['accFillSz'], 'avgPx': o['avgPx']}

    def probe_order(self, inst_id, ord_id):
        """与 TradeExecutor.probe_order 同语义；本 Mock 的订单都在当前账号，
        不存在跨账号场景，查不到按瞬时失败处理"""
        info = self.get_order_info(inst_id, ord_id)
        return ('ok', info) if info else ('error', {})

    def probe_order_by_cl_id(self, inst_id, cl_ord_id, tries=1):
        """客户号反查（打标机制）：内存里按 cl_ord_id 扫描，找不到算 absent。"""
        for oid, o in self.orders.items():
            if o.get('cl_ord_id') == cl_ord_id:
                return ('found', {'ordId': oid, 'state': o['state'],
                                  'accFillSz': o['accFillSz'], 'avgPx': o['avgPx']})
        return ('absent', {})

    def cancel_normal_order(self, inst_id, ord_id):
        o = self.orders.get(ord_id)
        if o and o['state'] == 'live':
            o['state'] = 'canceled'
        return True

    def amend_order(self, inst_id, ord_id, new_price=None, new_size=None):
        o = self.orders.get(ord_id)
        if not o:
            return False
        if new_price:
            o['price'] = float(new_price)
        if new_size:
            o['amount'] = float(new_size)
        return True

    def set_leverage(self, inst_id, leverage, trading_mode, pos_side=None):
        self.lev_calls.append((leverage, trading_mode))
        return True

    # ---- 测试辅助 ----
    def fill(self, ord_id, px=None):
        o = self.orders[ord_id]
        o['state'] = 'filled'
        o['accFillSz'] = o['amount']
        o['avgPx'] = float(px if px is not None else o['price'])

    def live_orders(self):
        return {k: v for k, v in self.orders.items() if v['state'] == 'live'}


def new_mgr():
    # 内存沙箱：每个场景先清空内存账本保证隔离（不碰生产 MySQL）
    _MEM_STATE.clear()
    ex = MockExecutor()
    mgr = DualPositionOrderManager(ex)
    mgr.set_context('SMOKE', False)
    return ex, mgr, None


def cleanup(path):
    # 状态已迁 MySQL，path 恒为 None，保留签名兼容旧 finally 块
    if path and os.path.exists(path):
        os.remove(path)


def slot(mgr, bucket, name):
    return mgr.state[INST][bucket]['slots'][name]


def trend_plan(**kw):
    p = {'period_mode': 'dual', 'target_dir': 'long', 'entry_px': 100.0,
         'exit_px': 110.0, 'contracts': 0.2, 'leverage': 10,
         'allow_entry': True, 'open_window': False, 'close_window': False}
    p.update(kw)
    return p


def range_plan(**kw):
    p = {'target_dir': 'long', 'entry_px': 95.0, 'exit_px': 105.0,
         'contracts': 0.3, 'leverage': 10, 'amend_min_pct': 0.001,
         'allow_entry': True}
    p.update(kw)
    return p


# ================================================================
# 1. 趋势跟踪 dual：开仓窗口 → 成交 → 平仓窗口 → 平仓成交
# ================================================================
def test_trend_dual():
    print('\n=== 1. 趋势跟踪 dual（双周期单向）===')
    ex, mgr, path = new_mgr()
    try:
        r = mgr.process_trend(INST, trend_plan(open_window=True), 0.0, 0.0)
        en = slot(mgr, BUCKET_TREND, 'entry')
        oid = en['ord_id']
        o = ex.orders.get(oid, {})
        check('开仓窗口挂出限价开多单', en['state'] == 'PENDING' and o.get('side') == 'buy'
              and o.get('mode') == 'cross' and abs(o.get('price', 0) - 100.0) < 1e-9
              and abs(o.get('amount', 0) - 0.2) < 1e-9, f'{r} {o}')
        check('多头下单前设置全仓杠杆', ('cross' in [m for _, m in ex.lev_calls]),
              str(ex.lev_calls))

        # 成交 → 下一轮入账（窗口已过，不应补挂）
        ex.fill(oid, 100.0)
        r = mgr.process_trend(INST, trend_plan(open_window=False), 0.2, 0.0)
        held, avg = mgr.get_position(INST, BUCKET_TREND, 'long')
        check('成交入账：持仓0.2张 均价100', abs(held - 0.2) < 1e-9 and abs(avg - 100.0) < 1e-6,
              f'held={held} avg={avg}')
        check('成交事件上报 fills', any(f['slot'] == 'entry' and f['dir'] == 'long'
                                       for f in r['fills']), str(r['fills']))
        check('窗口结束后不补挂开仓单', not ex.live_orders(), str(ex.live_orders()))

        # 平仓窗口 → 挂 reduce-only 限价平多
        mgr.process_trend(INST, trend_plan(close_window=True), 0.2, 0.0)
        exs = slot(mgr, BUCKET_TREND, 'exit')
        eo = ex.orders.get(exs['ord_id'], {})
        check('平仓窗口挂出 reduce-only 限价平多单',
              exs['state'] == 'PENDING' and eo.get('side') == 'sell'
              and eo.get('reduce') is True and abs(eo.get('price', 0) - 110.0) < 1e-9
              and abs(eo.get('amount', 0) - 0.2) < 1e-9, str(eo))

        ex.fill(exs['ord_id'], 110.0)
        mgr.process_trend(INST, trend_plan(), 0.0, 0.0)
        held, avg = mgr.get_position(INST, BUCKET_TREND, 'long')
        check('平仓成交后账本归零', held == 0.0 and avg == 0.0, f'held={held} avg={avg}')
    finally:
        cleanup(path)


# ================================================================
# 2. 趋势跟踪 single：方向翻转撤旧单反向重挂
# ================================================================
def test_trend_single():
    print('\n=== 2. 趋势跟踪 single（单周期双向）===')
    ex, mgr, path = new_mgr()
    try:
        mgr.process_trend(INST, trend_plan(period_mode='single', target_dir='long'), 0.0, 0.0)
        first = slot(mgr, BUCKET_TREND, 'entry')['ord_id']
        check('单周期模式挂出开多单', ex.orders[first]['side'] == 'buy', str(ex.orders[first]))

        # 未成交时同方向再跑一轮：不追价、不重挂
        mgr.process_trend(INST, trend_plan(period_mode='single', target_dir='long',
                                           entry_px=99.0), 0.0, 0.0)
        check('同向未成交不追价（挂单价不变）',
              abs(ex.orders[first]['price'] - 100.0) < 1e-9 and len(ex.orders) == 1,
              f"px={ex.orders[first]['price']} n={len(ex.orders)}")

        # 方向翻转 → 撤旧单，反向挂空单（逐仓）
        mgr.process_trend(INST, trend_plan(period_mode='single', target_dir='short',
                                           entry_px=120.0), 0.0, 0.0)
        check('方向翻转撤销旧开多单', ex.orders[first]['state'] == 'canceled',
              ex.orders[first]['state'])
        new = slot(mgr, BUCKET_TREND, 'entry')
        no = ex.orders.get(new['ord_id'], {})
        check('反向挂出开空单（逐仓）', no.get('side') == 'sell' and no.get('mode') == 'isolated'
              and abs(no.get('price', 0) - 120.0) < 1e-9, str(no))
    finally:
        cleanup(path)


# ================================================================
# 3. 区间波动：下轨开仓 → 改价追随 → 成交转挂上轨 → 平仓成交后循环
# ================================================================
def test_range_cycle():
    print('\n=== 3. 区间波动（BOLL 边界往复循环）===')
    ex, mgr, path = new_mgr()
    try:
        mgr.process_range(INST, range_plan(), 0.0, 0.0)
        en = slot(mgr, BUCKET_RANGE, 'entry')
        oid = en['ord_id']
        check('长多在下轨挂限价开多单',
              ex.orders[oid]['side'] == 'buy' and abs(ex.orders[oid]['price'] - 95.0) < 1e-9
              and abs(ex.orders[oid]['amount'] - 0.3) < 1e-9, str(ex.orders[oid]))

        # 下轨上移 → amend 追价（同一张挂单，不新建）
        mgr.process_range(INST, range_plan(entry_px=96.5), 0.0, 0.0)
        check('下轨移动自动改单追价',
              abs(ex.orders[oid]['price'] - 96.5) < 1e-9 and len(ex.orders) == 1,
              f"px={ex.orders[oid]['price']} n={len(ex.orders)}")

        # 变动幅度低于 amend_min_pct → 不改单
        mgr.process_range(INST, range_plan(entry_px=96.5005, amend_min_pct=0.01), 0.0, 0.0)
        check('变动小于最小幅度不改单', abs(ex.orders[oid]['price'] - 96.5) < 1e-9,
              str(ex.orders[oid]['price']))

        # 成交 → 立即在上轨挂平仓单
        ex.fill(oid, 96.5)
        mgr.process_range(INST, range_plan(entry_px=96.0), 0.3, 0.0)
        held, avg = mgr.get_position(INST, BUCKET_RANGE, 'long')
        exs = slot(mgr, BUCKET_RANGE, 'exit')
        eo = ex.orders.get(exs['ord_id'], {})
        check('开仓成交入账 0.3 张 @96.5', abs(held - 0.3) < 1e-9 and abs(avg - 96.5) < 1e-6,
              f'held={held} avg={avg}')
        check('立即在上轨挂 reduce-only 平仓单',
              eo.get('side') == 'sell' and eo.get('reduce') is True
              and abs(eo.get('price', 0) - 105.0) < 1e-9, str(eo))
        check('已持仓时不再挂开仓单',
              slot(mgr, BUCKET_RANGE, 'entry')['state'] != 'PENDING',
              slot(mgr, BUCKET_RANGE, 'entry')['state'])

        # 平仓成交 → 下一轮立刻重挂开仓单（无限循环）
        ex.fill(exs['ord_id'], 105.0)
        mgr.process_range(INST, range_plan(entry_px=97.0), 0.0, 0.0)
        held, _ = mgr.get_position(INST, BUCKET_RANGE, 'long')
        en2 = slot(mgr, BUCKET_RANGE, 'entry')
        check('平仓成交后账本归零', held == 0.0, str(held))
        check('平仓成交后立即重挂新一轮开仓单',
              en2['state'] == 'PENDING' and abs(ex.orders[en2['ord_id']]['price'] - 97.0) < 1e-9,
              str(en2))

        # 长周期反转 → 未成交开仓单撤销换向重挂
        mgr.process_range(INST, range_plan(target_dir='short', entry_px=108.0,
                                           exit_px=98.0), 0.0, 0.0)
        check('长周期反转撤销旧向开仓单', ex.orders[en2['ord_id']]['state'] == 'canceled',
              ex.orders[en2['ord_id']]['state'])
        en3 = slot(mgr, BUCKET_RANGE, 'entry')
        check('反向在上轨挂开空单（逐仓）',
              ex.orders[en3['ord_id']]['side'] == 'sell'
              and ex.orders[en3['ord_id']]['mode'] == 'isolated', str(en3))
    finally:
        cleanup(path)


# ================================================================
# 4. reduce-only 双层封顶 + 5. 账本对账 + 6. clear_bucket
# ================================================================
def test_ledger_guards():
    print('\n=== 4~6. 平仓量封顶 / 账本对账 / 清空篮子 ===')
    ex, mgr, path = new_mgr()
    try:
        # 手工造账：趋势 0.2 张多 + 区间 0.3 张多，真实全仓 0.5 张
        bk_t = mgr.state.setdefault(INST, mgr._inst(INST))[BUCKET_TREND]
        bk_r = mgr.state[INST][BUCKET_RANGE]
        bk_t['held']['long'], bk_t['avg_px']['long'] = 0.2, 100.0
        bk_r['held']['long'], bk_r['avg_px']['long'] = 0.3, 96.0

        # 趋势仓先挂 0.2 张平仓单
        mgr.process_trend(INST, trend_plan(close_window=True), 0.5, 0.0)
        t_exit = slot(mgr, BUCKET_TREND, 'exit')
        check('趋势仓挂出 0.2 张平仓单', abs(ex.orders[t_exit['ord_id']]['amount'] - 0.2) < 1e-9,
              str(t_exit))

        # 区间仓可挂量 = 真实0.5 − 趋势已挂0.2 = 0.3
        mgr.process_range(INST, range_plan(), 0.5, 0.0)
        r_exit = slot(mgr, BUCKET_RANGE, 'exit')
        check('区间仓平仓量扣除趋势仓已挂量后为 0.3',
              abs(ex.orders[r_exit['ord_id']]['amount'] - 0.3) < 1e-9,
              str(ex.orders.get(r_exit['ord_id'])))

        # 真实持仓被人工砍到 0.4 → 区间仓可挂量降为 0.2。改量走「撤后重建」：
        # 改量结果未知时不能提前释放预占，故先撤销旧单，下一轮按新量重挂。
        mgr.process_range(INST, range_plan(), 0.4, 0.0)
        check('缩减后旧 0.3 张平仓单被撤销（改量=撤后重建）',
              ex.orders[r_exit['ord_id']]['state'] == 'canceled',
              str(ex.orders[r_exit['ord_id']]))
        mgr.process_range(INST, range_plan(), 0.4, 0.0)
        r_exit = slot(mgr, BUCKET_RANGE, 'exit')
        check('下一轮重建为 0.2 张（真实0.4 − 趋势已挂0.2）',
              r_exit['state'] == 'PENDING'
              and abs(ex.orders[r_exit['ord_id']]['amount'] - 0.2) < 1e-9,
              str(r_exit))

        # 账本对账：真实 0.25 < 账本 0.5 → 按比例缩减
        mgr.reconcile(INST, 0.25, 0.0)
        ht, _ = mgr.get_position(INST, BUCKET_TREND, 'long')
        hr, _ = mgr.get_position(INST, BUCKET_RANGE, 'long')
        check('账本按比例缩减至真实持仓合计',
              abs((ht + hr) - 0.25) < 0.02 and ht < 0.2 and hr < 0.3, f'{ht}+{hr}')

        # clear_bucket：撤单 + 清空账本
        n = mgr.clear_bucket(INST, BUCKET_RANGE, '冒烟测试', 'long')
        hr2, ar2 = mgr.get_position(INST, BUCKET_RANGE, 'long')
        check('clear_bucket 撤单并清空账本',
              n >= 1 and hr2 == 0.0 and ar2 == 0.0
              and ex.orders[r_exit['ord_id']]['state'] == 'canceled', f'n={n} held={hr2}')
    finally:
        cleanup(path)


# ================================================================
# 7. 配置校验回归
# ================================================================
def test_config_validation():
    print('\n=== 7. 交易配置校验（双仓位 v3.0）===')
    from crypto.app import _validate_trading_config

    cfg_path = os.path.join(_HERE, 'config', 'config_trend_range.json')
    with open(cfg_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    errs = _validate_trading_config(cfg)
    check('实盘配置通过校验', not errs, str(errs))

    cur = cfg['currencies'][0]
    check('配置含 trend_position / range_position 双仓位',
          isinstance(cur.get('trend_position'), dict)
          and isinstance(cur.get('range_position'), dict), str(list(cur.keys())))
    check('旧三开三平字段已清除',
          'batch_trading' not in cur and 'fixed_position' not in cur, str(list(cur.keys())))
    check('止盈止损已迁入趋势仓',
          'take_profit' in cur['trend_position'] and 'stop_loss' in cur['trend_position']
          and 'take_profit' not in cur and 'stop_loss' not in cur,
          str(list(cur['trend_position'].keys())))

    # 非法配置应被拦截
    bad = json.loads(json.dumps(cfg))
    bad['currencies'][0]['trend_position']['period_mode'] = 'triple'
    bad['currencies'][0]['trend_position']['contracts'] = -1
    bad['currencies'][0]['range_position']['boll_dev'] = 0
    bad['currencies'][0]['leverage'] = 0
    bad['global_settings']['night_force_close']['start_hour'] = 26
    e2 = _validate_trading_config(bad)
    joined = ' | '.join(e2)
    check('非法 period_mode 被拦截', 'period_mode' in joined, joined)
    check('负数 contracts 被拦截', 'contracts' in joined, joined)
    check('boll_dev=0 被拦截', 'boll_dev' in joined, joined)
    check('leverage=0 被拦截', 'leverage' in joined, joined)
    check('start_hour 越界被拦截', 'start_hour' in joined, joined)


# ================================================================
# 8. trend_range_trader 接口面
# ================================================================
def test_trader_surface():
    print('\n=== 8. trend_range_trader 接口面 ===')
    import inspect
    from crypto.task.trend_range_trader import TrendRangeTrader as C

    params = inspect.signature(C.analyze_and_trade_real).parameters
    for p in ('trend_cfg', 'range_cfg', 'leverage', 'verbose_lifecycle'):
        check(f'analyze_and_trade_real 有 {p} 参数', p in params, str(list(params)))
    for p in ('fixed_position', 'batch_cfg', 'take_profit', 'stop_loss'):
        check(f'旧参数 {p} 已移除', p not in params, str(list(params)))
    for m in ('force_close_manual', '_run_trend_position', '_run_range_position',
              '_sync_trend_algo', '_refresh_positions', '_in_night_window',
              '_notify_long_reversal', '_close_bucket'):
        check(f'方法 {m} 就位', hasattr(C, m))
    for m in ('_run_batch_trading', '_close_all_positions', '_open_cross_long',
              '_open_isolated_short', '_partial_close'):
        check(f'旧方法 {m} 已删除', not hasattr(C, m))


def test_web_config_ids():
    """前端配置页：JS 引用的控件 id 必须在 HTML 中存在（避免报错卡死保存）"""
    print('\n=== 9. 前端配置页控件与字段 ===')
    import re
    html_path = os.path.join(_ROOT, 'crypto', 'templates', 'task.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        s = f.read()
    defined = set(re.findall(r'id="(cfg-[a-z0-9-]+)"', s))
    used = set(re.findall(r"getElementById\('(cfg-[a-z0-9-]+)'\)", s))
    # 动态拼接引用：getElementById('cfg-' + prefix + '-xxx')，prefix 取 trend/range
    for suf in re.findall(r"getElementById\('cfg-' \+ prefix \+ '-([a-z0-9-]+)'\)", s):
        used.update(f'cfg-{p}-{suf}' for p in ('trend', 'range'))
    check('JS 引用的控件 id 全部在 HTML 中存在', not (used - defined),
          str(sorted(used - defined)))
    check('HTML 控件全部被 JS 读写（无死控件）', not (defined - used),
          str(sorted(defined - used)))
    for i in ('cfg-trend-enabled', 'cfg-trend-mode', 'cfg-trend-contracts',
              'cfg-trend-algo-backup', 'cfg-range-enabled', 'cfg-range-contracts',
              'cfg-range-boll-period', 'cfg-range-boll-dev', 'cfg-range-amend-pct',
              'cfg-leverage', 'cfg-verbose-lifecycle'):
        check(f'新增控件 {i} 已就位', i in defined and i in used)
    check('旧分批限价控件已全部删除',
          not [i for i in defined if i.startswith('cfg-bt-')]
          and 'cfg-fixed-position' not in s, str(sorted(defined)))


def main():
    print('=' * 64)
    print('双仓位架构离线冒烟测试（Mock 执行器，不触碰真实账户）')
    print('=' * 64)
    test_trend_dual()
    test_trend_single()
    test_range_cycle()
    test_ledger_guards()
    test_config_validation()
    test_trader_surface()
    test_web_config_ids()
    print('\n' + '=' * 64)
    print(f'通过 {_passed} 项' + (f'，失败 {len(_failed)} 项: {_failed}' if _failed else '，全部通过 ✓'))
    print('=' * 64)
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
