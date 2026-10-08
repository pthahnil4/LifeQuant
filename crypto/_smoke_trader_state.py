#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易运行时状态冒烟测试（MySQL 版，迁移批次5）

测试对象：trader_state_repo 数据访问层 + DualPositionOrderManager /
TakeProfitEngine 的 MySQL 持久化链路（不连接真实交易所 API）

测试隔离策略（保护真实数据）：
  1. 运行前快照 8 张状态表全表
  2. 清空后执行全部用例
  3. finally 中无条件恢复快照（无论用例成败）

前置条件：环境变量 CRYPTO_DB_URL 已设置
  （mysql+pymysql://用户:密码@主机:端口/库名?charset=utf8mb4）
"""

import os
import sys
import time

# Windows 终端默认 GBK，强制 UTF-8 输出避免中文/emoji 乱码报错
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))   # crypto/
_ROOT = os.path.dirname(_HERE)                       # cryptoTrade/
_TASK = os.path.join(_HERE, 'task')
for p in (_ROOT, _TASK):
    if p not in sys.path:
        sys.path.insert(0, p)

if not os.environ.get('CRYPTO_DB_URL'):
    print('❌ 请先设置环境变量 CRYPTO_DB_URL 再运行本测试')
    sys.exit(2)

from sqlalchemy import select, delete  # noqa: E402

from crypto.database import session_scope  # noqa: E402
from crypto import trader_state_repo as repo  # noqa: E402
from crypto.models import (  # noqa: E402
    TraderDirection, ReverseGuard, ManualPause, TpRuntimeState,
    PosBook, PosSlot, PosAlgo, PosLev)

TABLES = [TraderDirection, ReverseGuard, ManualPause, TpRuntimeState,
          PosBook, PosSlot, PosAlgo, PosLev]

PASSED = []
FAILED = []


def check(name, cond, extra=''):
    if cond:
        PASSED.append(name)
        print(f'  ✅ {name}')
    else:
        FAILED.append(name)
        print(f'  ❌ {name} {extra}')


def _row_dict(row):
    d = dict(row.__dict__)
    d.pop('_sa_instance_state', None)
    return d


def _snapshot():
    snap = {}
    with session_scope() as s:
        for model in TABLES:
            snap[model.__tablename__] = [
                _row_dict(r) for r in s.execute(select(model)).scalars().all()]
    return snap


def _restore(snap):
    with session_scope() as s:
        for model in TABLES:
            s.execute(delete(model))
            for d in snap.get(model.__tablename__, []):
                s.add(model(**d))


# =====================================================================
# Mock 交易执行器（不触达真实交易所）
# =====================================================================
class MockExecutor:
    def __init__(self):
        self.order_info = {}
        self.cancel_ok = True
        self.algo_details = {}
        self.trade_result = {'success': True, 'order_id': 'MOCK-ORD'}

    def get_order_info(self, inst_id, ord_id):
        v = self.order_info.get(ord_id)
        if isinstance(v, list):
            return v.pop(0) if v else None
        return v

    def cancel_normal_order(self, inst_id, ord_id):
        return self.cancel_ok

    def execute_trade(self, **kw):
        return dict(self.trade_result)

    def execute_reduce_only_order(self, **kw):
        return dict(self.trade_result)

    def amend_order(self, *a, **kw):
        return True

    def set_leverage(self, *a, **kw):
        return True

    def get_algo_order_details(self, algo_id):
        return self.algo_details.get(algo_id, {})

    def cancel_algo_order(self, *a, **kw):
        return True

    def create_tp_sl_order(self, **kw):
        return {'success': True, 'algo_id': 'MOCK-ALGO'}


INST = 'SMK-USDT-SWAP'
INST2 = 'SMK2-USDT-SWAP'


def main():
    print('📸 快照真实数据（8 张状态表）...')
    snap = _snapshot()
    counts = {k: len(v) for k, v in snap.items()}
    print('   ', counts)

    try:
        with session_scope() as s:
            for model in TABLES:
                s.execute(delete(model))
        print('🧹 已清空测试场地\n')

        # =============================================================
        print('--- A. 方向记录 / 风控计时 / 强平冷却 整树读写 ---')
        dirs = {INST: {'short': 'long', 'long': 'short'},
                INST2: {'short': 'short', 'long': 'short'}}
        with session_scope() as s:
            repo.save_directions(s, dirs)
        with session_scope() as s:
            check('方向记录 round-trip', repo.load_directions(s) == dirs)
        # 覆盖语义：集合收缩后旧币种消失
        with session_scope() as s:
            repo.save_directions(s, {INST: {'short': 'short', 'long': 'long'}})
        with session_scope() as s:
            got = repo.load_directions(s)
            check('方向记录整体重写（旧币种清除）',
                  got == {INST: {'short': 'short', 'long': 'long'}})

        guard = {INST: {'detected_ts': 1787420832.9509723,
                        'long_direction': 'long', 'reverse_side': 'short',
                        'reverse_mode': 'isolated', 'reverse_amount': 230.0,
                        'warned': True}}
        with session_scope() as s:
            repo.save_reverse_guard(s, guard)
        with session_scope() as s:
            got = repo.load_reverse_guard(s)
            check('风控计时 round-trip（含高精度时间戳）', got == guard,
                  str(got))

        pause = {INST: time.time() + 1800.5}
        with session_scope() as s:
            repo.save_manual_pause(s, pause)
        with session_scope() as s:
            check('强平冷却 round-trip', repo.load_manual_pause(s) == pause)
        with session_scope() as s:
            repo.save_manual_pause(s, {})
        with session_scope() as s:
            check('强平冷却清空', repo.load_manual_pause(s) == {})

        # =============================================================
        print('--- B. 止盈运行时状态 按key upsert/delete ---')
        st1 = {'entry_ts': 1787420000.1234567, 'peak': 105.5,
               'trough': 98.25, 'avg_px': 100.0, 'ladder_done': [0, 1]}
        with session_scope() as s:
            repo.upsert_tp_state(s, f'{INST}:long', st1)
        with session_scope() as s:
            got = repo.load_tp_state(s)
            check('止盈状态 round-trip（ladder_done JSON）',
                  got == {f'{INST}:long': st1}, str(got))
        # upsert 覆盖
        st2 = dict(st1, peak=110.0, ladder_done=[0, 1, 2])
        with session_scope() as s:
            repo.upsert_tp_state(s, f'{INST}:long', st2)
        with session_scope() as s:
            check('止盈状态 upsert 覆盖',
                  repo.load_tp_state(s)[f'{INST}:long'] == st2)
        with session_scope() as s:
            repo.delete_tp_state(s, f'{INST}:long')
        with session_scope() as s:
            check('止盈状态 delete', repo.load_tp_state(s) == {})

        # =============================================================
        print('--- C. 双仓位账本 整段读写 ---')
        inst_state = {
            'trend': {
                'held': {'long': 0.3, 'short': 0.0},
                'avg_px': {'long': 1.603789, 'short': 0.0},
                'slots': {
                    'entry': {'state': 'FILLED', 'ord_id': None, 'price': 1.603789,
                              'amount': 0.3, 'placed_ts': 1786730236.8115647,
                              'acc_filled': 0.3, 'dir': 'long'},
                    'exit': {'state': 'PENDING', 'ord_id': '3832850104868294656',
                             'price': 1.701234, 'amount': 0.3,
                             'placed_ts': 1786730999.9999998,
                             'acc_filled': 0.1, 'dir': 'long',
                             'qfail_logged': True}},
                'prev_open_confirmed': True,
                'prev_close_confirmed': False,
                'last_desired': 'long',
                'algo': {'long': {'algo_id': 'ALGO-1', 'amount': 0.3,
                                  'sl': 1.5, 'tp': 1.9, 'ts': 1786730237.5},
                         'short': None},
            },
            'range': {
                'held': {'long': 0.0, 'short': 0.2},
                'avg_px': {'long': 0.0, 'short': 2.5},
                'slots': {
                    'entry': {'state': 'IDLE', 'ord_id': None, 'price': 0.0,
                              'amount': 0.0, 'placed_ts': 0.0,
                              'acc_filled': 0.0, 'dir': None},
                    'exit': {'state': 'IDLE', 'ord_id': None, 'price': 0.0,
                             'amount': 0.0, 'placed_ts': 0.0,
                             'acc_filled': 0.0, 'dir': None}},
                'prev_open_confirmed': False,
                'prev_close_confirmed': False,
                'last_desired': 'short',
                'algo': {'long': None, 'short': None},
            },
            'lev_set': {'cross': 10, 'isolated': 10},
        }
        with session_scope() as s:
            repo.save_inst_position(s, INST, inst_state)
        with session_scope() as s:
            got = repo.load_position_state(s)
            check('账本 round-trip（含 qfail_logged/algo/lev_set）',
                  got == {INST: inst_state}, str(got))
        # lev_set=None 形态
        inst_state2 = {k: (dict(v) if isinstance(v, dict) else v)
                       for k, v in inst_state.items()}
        inst_state2['lev_set'] = None
        with session_scope() as s:
            repo.save_inst_position(s, INST2, inst_state2)
        with session_scope() as s:
            got = repo.load_position_state(s)
            check('账本 lev_set=None 形态', got[INST2]['lev_set'] is None)
        # 整段重写：槽位从 PENDING 变 IDLE 后旧字段不残留
        inst_state['trend']['slots']['exit'] = {
            'state': 'IDLE', 'ord_id': None, 'price': 0.0, 'amount': 0.0,
            'placed_ts': 0.0, 'acc_filled': 0.0, 'dir': None}
        inst_state['trend']['algo']['long'] = None
        with session_scope() as s:
            repo.save_inst_position(s, INST, inst_state)
        with session_scope() as s:
            got = repo.load_position_state(s)
            check('账本整段重写（旧挂单/兜底委托不残留）',
                  got[INST] == inst_state, str(got[INST]))

        # =============================================================
        print('--- D. DualPositionOrderManager MySQL 持久化链路 ---')
        from utils.position_order_manager import (
            DualPositionOrderManager, BUCKET_TREND, BUCKET_RANGE)

        # 独立清场（前面用例已写入 INST，此处重新保证空库前提）
        with session_scope() as s:
            repo.clear_position_state(s)

        ex = MockExecutor()
        m = DualPositionOrderManager(ex)
        m.set_context('SMOKE', False)
        check('管理器启动加载空账本', m.state == {})
        # 下单成功 → 立即落库
        ok_ = m._place_entry(INST, BUCKET_TREND, 'long', 0.2, 1.234567,
                             leverage=10, cross_pos=0.0, isolated_pos=0.0)
        check('开仓挂单成功', ok_)
        with session_scope() as s:
            db_state = repo.load_position_state(s)
        slot = db_state[INST]['trend']['slots']['entry']
        check('下单立即落库（槽位 PENDING）',
              slot['state'] == 'PENDING' and slot['ord_id'] == 'MOCK-ORD'
              and slot['price'] == 1.234567 and slot['dir'] == 'long',
              str(slot))
        check('杠杆缓存落库', db_state[INST]['lev_set'] == {'cross': 10})
        # 模拟重启：新实例从 DB 恢复账本
        m2 = DualPositionOrderManager(MockExecutor())
        held, avg = m2.get_position(INST, BUCKET_TREND, 'long')
        check('重启后账本恢复（槽位/方向）',
              m2.state[INST]['trend']['slots']['entry']['ord_id'] == 'MOCK-ORD')
        # 成交入账（get_order_info 返回 filled）→ 持仓落库
        ex2 = m2.executor
        ex2.order_info['MOCK-ORD'] = {'state': 'filled', 'accFillSz': '0.2',
                                      'avgPx': '1.234567'}
        r = m2.poll_fills(INST)
        check('成交入账动作', any('成交' in a for a in r['actions']), str(r))
        m3 = DualPositionOrderManager(MockExecutor())
        held, avg = m3.get_position(INST, BUCKET_TREND, 'long')
        check('成交后持仓/均价落库', held == 0.2 and avg == 1.234567,
              f'held={held} avg={avg}')
        # 外部平仓扣减 → 落库
        m3.note_external_close(INST, BUCKET_TREND, 'long', 0.2, '测试')
        m4 = DualPositionOrderManager(MockExecutor())
        held, avg = m4.get_position(INST, BUCKET_TREND, 'long')
        check('外部平仓扣减落库', held == 0.0 and avg == 0.0)

        # =============================================================
        print('--- E. TakeProfitEngine MySQL 持久化链路 ---')
        from tp_engine import TakeProfitEngine

        eng = TakeProfitEngine()
        ctx = {'inst_id': INST, 'is_long': True, 'pos_size': 0.2,
               'avg_px': 100.0, 'current_price': 105.0, 'pnl_pct': 5.0,
               'atr_value': 1.0, 'short_period': '5m'}
        cfg = {'enabled': True, 'category': 'fixed_target',
               'fixed_target': {'mode': 'pct', 'pct': 50.0}}
        d = eng.evaluate(cfg, ctx)
        check('止盈评估未触发（pct 阈值未到）', d['action'] == 'none')
        # 模拟重启：新实例从 DB 恢复峰值
        eng2 = TakeProfitEngine()
        st = eng2._state.get(f'{INST}:long', {})
        check('重启后峰值保留', abs(st.get('peak', 0) - 105.0) < 1e-9, str(st))
        # 价格新高 → 峰值更新落库
        ctx2 = dict(ctx, current_price=108.0)
        eng2.evaluate(cfg, ctx2)
        eng3 = TakeProfitEngine()
        check('峰值更新落库',
              abs(eng3._state[f'{INST}:long']['peak'] - 108.0) < 1e-9)
        # 分批止盈 ladder_done 持久化
        cfg_l = {'enabled': True, 'category': 'ladder',
                 'ladder': {'levels': [{'pct': 2.0, 'close_ratio': 0.33},
                                       {'pct': 4.0, 'close_ratio': 0.5},
                                       {'pct': 6.0, 'close_ratio': 1.0}]}}
        d = eng3.evaluate(cfg_l, dict(ctx, pnl_pct=3.0, current_price=103.0))
        check('分批止盈部分触发', d['action'] == 'close_partial', str(d))
        eng4 = TakeProfitEngine()
        check('ladder_done 持久化',
              eng4._state[f'{INST}:long']['ladder_done'] == [0],
              str(eng4._state.get(f'{INST}:long')))
        # 持仓归零清理
        eng4.clear_state(INST, True)
        eng5 = TakeProfitEngine()
        check('clear_state 落库', f'{INST}:long' not in eng5._state)

    finally:
        print('\n♻️ 恢复真实数据快照...')
        _restore(snap)
        with session_scope() as s:
            ok_restore = all(
                len(s.execute(select(m)).scalars().all()) == len(snap[m.__tablename__])
                for m in TABLES)
        print(f"   恢复{'成功' if ok_restore else '❌失败'}: "
              + ', '.join(f'{k}={v}' for k, v in counts.items()))

    print('\n' + '=' * 60)
    print(f"结果: {len(PASSED)} 通过 / {len(FAILED)} 失败")
    if FAILED:
        for f in FAILED:
            print(f'  ❌ {f}')
        sys.exit(1)
    print('🎉 全部通过')


if __name__ == '__main__':
    main()
