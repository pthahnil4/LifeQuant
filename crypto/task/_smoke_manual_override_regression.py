# -*- coding: utf-8 -*-
"""人工/自动交易一致性离线回归。

运行：python -B crypto/task/_smoke_manual_override_regression.py
仅加载源文件中的类/常量定义，跳过业务模块导入副作用；所有交易、持久化、
邮件和流水均为内存替身。socket 连接直接报错，不读取 API 密钥、不接触业务库。
"""
import ast
import copy
import datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import socket
import sys
import threading
import time
import types
import unittest
import uuid
from contextlib import contextmanager, ExitStack
from utils.watch_mode import WATCH_MODE_NOTICE, watch_mode, watch_mode_active
from typing import Dict, List, Optional, Tuple
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
INST = 'TEST-USDT-SWAP'
BUCKET_TREND, BUCKET_RANGE = 'trend', 'range'
LOG = logging.getLogger('isolated_sync_test')
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def load_definitions(path, names=None, **extra):
    """执行原始类/函数体，不执行顶层业务导入、初始化或日志创建。"""
    tree = ast.parse(Path(path).read_text(encoding='utf-8-sig'), str(path))
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))
             or isinstance(n, ast.Assign) and all(isinstance(t, ast.Name) and t.id.isupper()
                                                 for t in n.targets)]
    if names is not None:
        nodes = [n for n in nodes if getattr(n, 'name', None) in names]
    ns = dict(time=time, math=math, copy=copy, uuid=uuid, threading=threading,
              datetime=datetime, hashlib=hashlib, json=json, os=os, sys=sys,
              Dict=Dict, List=List, Optional=Optional, Tuple=Tuple,
              ExitStack=ExitStack, watch_mode=watch_mode, watch_mode_active=watch_mode_active,
              WATCH_MODE_NOTICE=WATCH_MODE_NOTICE,
              task_log=LOG, trade_log=LOG, logger=LOG, journal_fill=lambda *a, **k: None,
              BUCKET_TREND=BUCKET_TREND, BUCKET_RANGE=BUCKET_RANGE,
              DualPeriodStrategyAdapter=object, fmt_dir=lambda d: d,
              fmt_qty=lambda q: str(q), fmt_pct=lambda p: str(p))
    ns.update(extra)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), ns)
    return types.SimpleNamespace(**ns)


class MemoryRepo:
    def __init__(self):
        self.rows = {}
        self.fail = False

    def load_position_state(self, session, account=None, environment='0'):
        if self.fail:
            raise RuntimeError('模拟恢复失败')
        return copy.deepcopy(self.rows.get((account, environment), {}))

    def save_inst_position(self, session, inst_id, state, account=None, environment='0'):
        if self.fail:
            raise RuntimeError('模拟持久化失败')
        rows = self.rows.setdefault((account, environment), {})
        rev = int(rows.get(inst_id, {}).get('_revision', 0))
        if int(state.get('_revision', 0)) != rev:
            raise RuntimeError('模拟并发版本冲突')
        rows[inst_id] = copy.deepcopy(dict(state, _revision=rev + 1))
        return rev + 1

    def load_inst_revision(self, session, inst_id, account=None, environment='0'):
        st = self.rows.get((account, environment), {}).get(inst_id)
        if st is None:
            return 0
        return int(st.get('_revision', 0))


@contextmanager
def memory_session():
    yield None


class FakeExecutor:
    def __init__(self):
        self.positions = {'long': 0.0, 'short': 0.0}
        self.orders = {}
        self.algos = {}
        self.calls = []
        self.cancels = []
        self.cancel_confirms = True
        self.mode = 'net_mode'
        self.fail_positions = False
        self.fail_orders = False
        self.unknown_submit = False
        self.on_positions = None
        self.on_submit = None

    def try_get_position_mode(self):
        return self.mode

    def try_get_positions_by_mode(self, inst_id=None):
        if self.fail_positions:
            return None
        if self.on_positions:
            self.on_positions()
        out = {'cross': [], 'isolated': []}
        for d, qty in self.positions.items():
            if qty:
                mode = 'cross' if d == 'long' else 'isolated'
                out[mode].append(dict(instId=inst_id or INST, pos=qty if d == 'long' else -qty,
                                     avgPx='100', posSide='net', mgnMode=mode, posId=d))
        return out

    def try_get_open_orders(self, inst_id=None):
        if self.fail_orders:
            return None
        return [dict(o, _algo=False) for o in self.orders.values()
                if o['state'] in ('live', 'partially_filled')]

    # 轮初全账户快照缓存（Option B）契约替身：本替身不缓存，按 instId 读时直接
    # 走上面的 live 逻辑，等价“快照未命中”，保持既有断言语义不变。
    def begin_round_snapshot(self, positions=None, orders=None, mode=None):
        return positions is not None and orders is not None

    def invalidate_snapshot(self, inst_id=None):
        pass

    def end_round_snapshot(self):
        pass

    def probe_order(self, inst_id, oid):
        o = self.orders.get(oid)
        return ('ok', dict(o)) if o else ('not_found', {})

    def probe_order_by_cl_id(self, inst_id, cid, tries=1):
        for o in self.orders.values():
            if o.get('clOrdId') == cid:
                return 'found', dict(o)
        return 'absent', {}

    def add(self, oid='O1', amount=1.0, side='buy', reduce=False, cid=''):
        self.orders[oid] = dict(ordId=oid, instId=INST, sz=str(amount), side=side,
                                tdMode='cross', reduceOnly=str(reduce).lower(),
                                clOrdId=cid, state='live', accFillSz='0', avgPx='')
        return self.orders[oid]

    def _submit(self, reduce=False, **params):
        if self.on_submit:
            self.on_submit(params)
        self.calls.append(dict(params, reduce=reduce))
        if self.unknown_submit:
            return dict(success=False, need_verify=True)
        oid = 'N' + str(len(self.calls))
        self.add(oid, params['amount'], params['side'], reduce, params['cl_ord_id'])
        return dict(success=True, order_id=oid)

    def execute_trade(self, **params):
        return self._submit(**params)

    def execute_reduce_only_order(self, **params):
        return self._submit(True, **params)

    def cancel_normal_order(self, inst_id, oid):
        self.cancels.append(oid)
        if self.cancel_confirms and oid in self.orders:
            self.orders[oid]['state'] = 'canceled'
        return True

    def get_algo_order_details(self, oid):
        return self.algos.get(oid, {})

    def probe_algo_by_client_id(self, cid, tries=1):
        return 'unknown', {}

    def cancel_algo_order(self, oid, inst_id):
        if self.cancel_confirms and oid in self.algos:
            self.algos[oid]['state'] = 'canceled'
        return self.cancel_confirms

    def set_leverage(self, *a, **k):
        return True


class SyncRegression(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('禁止联网'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.repo = MemoryRepo()
        self.module = load_definitions(HERE / 'utils/position_order_manager.py',
                                      session_scope=memory_session, state_repo=self.repo)
        self.cls = self.module.DualPositionOrderManager
        self.ex = FakeExecutor()
        self.m = self.cls(self.ex, account='test-a', environment='1')
        self.m.set_context('round-1', False)
        self.m.set_entry_targets(INST, {'long': 1.0, 'short': 0.0})

    def restart(self, account='test-a', environment='1'):
        self.m = self.cls(self.ex, account=account, environment=environment)
        self.m.set_context('round-2', False)
        self.m.set_entry_targets(INST, {'long': 1.0, 'short': 0.0})

    def seed(self, qty, bucket='trend', direction='long'):
        bk = self.m._inst(INST)[bucket]
        bk['held'][direction] = qty
        bk['avg_px'][direction] = 100.0
        self.ex.positions[direction] = qty
        self.m._save(INST)

    def pending(self, amount=1, bucket='range', slot='entry', oid='O1'):
        pt = dict(self.m._new_slot(), state='PENDING', ord_id=oid, amount=amount,
                  dir='long', price=100.0, placed_ts=time.time() - 10000)
        self.m._inst(INST)[bucket]['slots'][slot] = pt
        self.ex.add(oid, amount, 'buy' if slot == 'entry' else 'sell', slot == 'exit')
        self.m._save(INST)
        return pt

    def fill(self, qty, oid='O1', state='filled', avg=100):
        self.ex.orders[oid].update(state=state, accFillSz=str(qty), avgPx=str(avg))

    def held(self, bucket='trend'):
        return self.m.get_position(INST, bucket, 'long')[0]

    def open_range(self, amount=1):
        return self.m._place_entry(INST, 'range', 'long', amount, 99)

    def test_manual_entry_at_target_blocks_both_baskets_and_restart(self):
        self.ex.positions['long'] = 1
        self.assertTrue(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held(), 1)
        self.assertFalse(self.open_range())
        self.restart()
        self.assertFalse(self.open_range())
        self.assertEqual(self.ex.calls, [])

    def test_manual_entry_only_remaining_room_is_sent(self):
        self.ex.positions['long'] = .7
        self.assertTrue(self.open_range())
        self.assertAlmostEqual(self.ex.calls[-1]['amount'], .3)

    def test_small_real_positions_never_disappear(self):
        for qty in (.000003, .005, .01, .03, .05):
            with self.subTest(qty=qty):
                self.ex.positions['long'] = qty
                self.assertTrue(self.m.synchronize(INST)['success'])
                self.assertAlmostEqual(self.held(), qty)

    def test_manual_shrink_cancels_entry_and_cooldown_survives_restart(self):
        self.seed(.8)
        self.pending(.2)
        self.ex.positions['long'] = .6
        self.assertTrue(self.m.synchronize(INST)['success'])
        self.assertAlmostEqual(self.held(), .6)
        self.assertIn('O1', self.ex.cancels)
        self.restart()
        self.assertFalse(self.open_range())
        self.assertGreater(self.m.state[INST]['pause_until'], time.time())

    def test_manual_close_removes_actual_book_without_reopening(self):
        self.seed(1)
        self.ex.positions['long'] = 0
        self.assertTrue(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held(), 0)
        self.assertFalse(self.open_range())

    def test_partial_fill_increment_and_weighted_cost(self):
        self.pending()
        self.fill(.2, state='partially_filled', avg=100)
        self.m.poll_fills(INST)
        self.m.poll_fills(INST)
        self.fill(.6, state='partially_filled', avg=110)
        self.m.poll_fills(INST)
        self.restart()
        self.fill(1, avg=120)
        self.m.poll_fills(INST)
        self.m.poll_fills(INST)
        self.assertEqual(self.m.get_position(INST, 'range', 'long'), (1, 120))

    def test_order_first_stale_positions_block_and_recover(self):
        self.pending()
        self.fill(1)
        self.assertFalse(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held('range'), 1)
        self.assertFalse(self.open_range())
        self.restart()
        self.assertFalse(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held('range'), 1)
        self.ex.positions['long'] = 1
        self.assertTrue(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held(), 0)
        self.assertEqual(self.held('range'), 1)

    def test_newer_exchange_timestamp_resolves_concurrent_manual_shrink(self):
        self.pending()
        self.fill(1)
        self.ex.orders['O1']['fillTime'] = '1000'
        self.ex.try_get_positions_by_mode = lambda inst: {'cross': [
            {'pos': .6, 'avgPx': 100, 'uTime': '2000', 'posSide': 'net'}], 'isolated': []}
        self.assertTrue(self.m.synchronize(INST)['success'])
        self.assertAlmostEqual(self.held('range'), .6)
        self.assertGreater(self.m.state[INST]['pause_until'], time.time())
        self.assertFalse(self.open_range())

    def test_equal_or_older_position_timestamp_does_not_override_fill(self):
        self.pending()
        self.fill(1)
        self.ex.orders['O1']['fillTime'] = '2000'
        for stamp in ('1000', '2000'):
            self.ex.try_get_positions_by_mode = lambda inst: {'cross': [
                {'pos': 0, 'avgPx': 0, 'uTime': stamp, 'posSide': 'net'}], 'isolated': []}
            self.assertFalse(self.m.synchronize(INST)['success'])
            self.assertEqual(self.held('range'), 1)

    def test_position_first_does_not_absorb_then_credit_twice(self):
        self.pending()
        self.ex.positions['long'] = 1
        self.assertFalse(self.m.synchronize(INST, cancel_entries=False)['success'])
        self.assertEqual(self.held(), 0)
        self.assertEqual(self.held('range'), 0)
        self.fill(1)
        self.assertTrue(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held('range'), 1)
        self.assertEqual(self.held(), 0)

    def test_exit_first_old_position_cannot_restore_closed_book(self):
        self.seed(1, 'range')
        self.pending(bucket='range', slot='exit')
        self.fill(1)
        self.assertFalse(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held('range'), 0)
        self.assertFalse(self.m._place_exit(INST, 'range', 'long', 1, 110))
        self.ex.positions['long'] = 0
        self.assertTrue(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held(), 0)
        self.assertNotIn('pause_until', self.m.state[INST])

    def test_new_fill_marks_settlement_pending(self):
        self.pending()
        self.assertTrue(self.m.synchronize(INST)['success'])
        self.fill(.2, state='partially_filled')
        self.m.poll_fills(INST)
        # 【2026-09-28 按用户要求移除 G1/G3】不再断言 _sync_ok（该状态已删除）；
        # 新成交仍会把该币标记为“结算待确认”这一风险态，供后续对账/告警使用。
        self.assertTrue(self.m.state[INST]['settlement_pending'])

    def test_cancel_ack_live_keeps_reservation(self):
        pt = self.pending()
        self.ex.cancel_confirms = False
        self.assertFalse(self.m._cancel_slot(INST, 'range', 'entry', '测试'))
        self.assertEqual(pt['state'], 'PENDING')
        self.assertTrue(pt['cancel_requested'])
        self.restart()
        self.assertFalse(self.open_range())
        self.assertEqual(self.ex.calls, [])

    def test_partial_cancel_tail_is_credited_once(self):
        self.pending()
        self.fill(.2, state='partially_filled')
        self.m.poll_fills(INST)
        self.fill(.3, state='partially_filled')
        self.assertTrue(self.m._cancel_slot(INST, 'range', 'entry', '测试'))
        self.m.poll_fills(INST)
        self.assertAlmostEqual(self.held('range'), .3)

    def test_absent_order_never_replaced_after_grace_or_restart(self):
        pt = self.pending()
        self.ex.orders.clear()
        self.assertFalse(self.open_range())
        self.assertEqual(pt['state'], 'PENDING')
        self.restart()
        self.assertFalse(self.open_range())
        self.assertEqual(self.ex.calls, [])

    def test_write_ahead_intent_and_unknown_submit(self):
        def assert_durable(params):
            saved = self.repo.rows[('test-a', '1')][INST]['range']['slots']['entry']
            self.assertEqual(saved['cl_ord_id'], params['cl_ord_id'])
            self.assertTrue(saved['unknown'])
        self.ex.on_submit = assert_durable
        self.ex.unknown_submit = True
        self.assertFalse(self.open_range())
        self.restart()
        self.assertFalse(self.open_range())
        self.assertEqual(len(self.ex.calls), 1)

    def test_unknown_exit_absent_expires_after_window(self):
        # 回归 LIT 事故：只带 clOrdId、从没落地(交易所权威查无此单)的平仓孤儿单，
        # 超传播窗口后必须自动过期释放，不能把币种永久锁死在"禁止交易"。
        bk = self.m._inst(INST)['range']
        pt = dict(self.m._new_slot(), state='PENDING', dir='short', amount=2.0,
                  price=100.0, cl_ord_id='ctX', unknown=True, qfail_logged=True,
                  placed_ts=time.time() - (self.m.UNKNOWN_ABSENT_SETTLE_SEC + 100))
        bk['slots']['exit'] = pt
        self.ex.orders.clear()          # 交易所查无此单 → probe_order_by_cl_id 返回 absent
        act = self.m._poll_order(INST, 'range', 'exit', pt, [])
        self.assertEqual(pt['state'], 'EXPIRED')
        self.assertFalse(pt['unknown'])
        self.assertIsNone(pt.get('qfail_logged'))
        self.assertIn('absent-expired', act or '')

    def test_unknown_exit_absent_kept_within_grace(self):
        # 传播/归档宽限期内即便查无此单也不得提前认定未落地（保守停单，防误重发）。
        bk = self.m._inst(INST)['range']
        pt = dict(self.m._new_slot(), state='PENDING', dir='short', amount=2.0,
                  price=100.0, cl_ord_id='ctX', unknown=True, placed_ts=time.time() - 1)
        bk['slots']['exit'] = pt
        self.ex.orders.clear()
        self.m._poll_order(INST, 'range', 'exit', pt, [])
        self.assertEqual(pt['state'], 'PENDING')
        self.assertTrue(pt['unknown'])

    def test_unknown_entry_absent_never_auto_expires(self):
        # 开仓(entry)侧安全不变量：absent 永不自动过期/重发（防重复开仓）。
        bk = self.m._inst(INST)['range']
        pt = dict(self.m._new_slot(), state='PENDING', dir='long', amount=1.0,
                  price=100.0, cl_ord_id='ctE', unknown=True,
                  placed_ts=time.time() - (self.m.UNKNOWN_ABSENT_SETTLE_SEC + 100))
        bk['slots']['entry'] = pt
        self.ex.orders.clear()
        self.m._poll_order(INST, 'range', 'entry', pt, [])
        self.assertEqual(pt['state'], 'PENDING')
        self.assertTrue(pt['unknown'])

    def test_storage_failure_no_longer_blocks_request(self):
        # 【2026-09-28 按用户要求移除 G3】账本落库失败不再拒发请求：_save 失败
        # 只记日志、返回 False，发单按内存账本照常进行（DB 恢复后自动补写）。
        self.repo.fail = True
        self.assertTrue(self.open_range())
        self.assertEqual(len(self.ex.calls), 1)
        # 但重启需要从 DB 重载账本，落库/读取不可用时重载仍失败（_load_state 保留
        # “禁止按空仓启动”的安全语义，与闸门无关）。
        with self.assertRaises(RuntimeError):
            self.restart()

    def test_concurrent_writer_revision_no_longer_blocks_request(self):
        # 【2026-09-28 按用户要求移除 G3】并发写入者导致的账本版本冲突不再停摆：
        # 陈旧 manager 的 _save 撞版本冲突只返回 False，发单照常按内存账本进行。
        self.m._save(INST)
        other = self.cls(self.ex, account='test-a', environment='1')
        other.hold_entries(INST)
        self.assertTrue(self.open_range())
        self.assertEqual(len(self.ex.calls), 1)

    def test_stale_revision_self_heals_and_persists(self):
        # 【2026-09-28 根治版本冲突刷屏】单币内存版本号陈旧（库被外部写入者
        # 推进）→ _save 撞冲突后自愈：从库读回权威 _revision、重试落库成功，
        # 版本链修复、后续保存不再冲突（不再每轮刷屏）。
        self.seed(1)
        self.assertTrue(self.m._save(INST))
        self.assertEqual(self.repo.rows[('test-a', '1')][INST]['_revision'], 1)
        # 模拟另一写入者把库内版本号推进到 5（本进程内存仍停在 1）
        self.repo.rows[('test-a', '1')][INST]['_revision'] = 5
        self.m._inst(INST)['trend']['held']['long'] = 2.0   # 制造内容变更触发落库
        self.assertTrue(self.m._save(INST))                 # 自愈后落库成功
        row = self.repo.rows[('test-a', '1')][INST]
        self.assertEqual(row['_revision'], 6)               # 版本链已续接（5→6）
        self.assertEqual(row['trend']['held']['long'], 2.0) # 内存内容已权威落库
        self.assertEqual(self.m.state[INST]['_revision'], 6)
        # 再次变更 → 正常落库（不再冲突），证明已自愈
        self.m._inst(INST)['trend']['held']['long'] = 3.0
        self.assertTrue(self.m._save(INST))
        self.assertEqual(self.repo.rows[('test-a', '1')][INST]['_revision'], 7)

    def test_reload_state_adopts_db_revision(self):
        # reload_state 将内存基线对齐到库内权威版本号（启动取得租约后调用）。
        self.seed(1)
        self.assertTrue(self.m._save(INST))
        self.repo.rows[('test-a', '1')][INST]['_revision'] = 9
        self.m.reload_state()
        self.assertEqual(self.m.state[INST]['_revision'], 9)

    def test_account_and_environment_isolation(self):
        self.seed(1)
        self.restart('test-b')
        self.assertEqual(self.m.state, {})
        self.restart(environment='0')
        self.assertEqual(self.m.state, {})
        self.restart()
        self.assertEqual(self.held(), 1)

    def test_pending_exit_cannot_free_entry_capacity(self):
        self.seed(1)
        self.pending(slot='exit', bucket='trend')
        self.assertFalse(self.open_range())
        self.assertEqual(self.ex.calls, [])

    def test_market_ack_and_partial_do_not_clear_book_or_resubmit(self):
        self.seed(1)
        self.assertFalse(self.m.close_market(INST, 'trend', 'long', .5))
        self.assertEqual(self.held(), 1)
        self.fill(.2, 'N1', 'partially_filled')
        self.ex.positions['long'] = .8
        self.assertFalse(self.m.close_market(INST, 'trend', 'long', .5))
        self.assertAlmostEqual(self.held(), .8)
        self.restart()
        self.fill(.5, 'N1')
        self.ex.positions['long'] = .5
        self.m.poll_fills(INST)
        self.assertTrue(self.m.close_market(INST, 'trend', 'long', .5))
        self.assertEqual(len(self.ex.calls), 1)
        self.assertAlmostEqual(self.held(), .5)

    def test_market_and_normal_exit_share_reservation(self):
        self.seed(1)
        self.assertFalse(self.m.close_market(INST, 'trend', 'long', 1))
        self.assertFalse(self.m._place_exit(INST, 'trend', 'long', 1, 110))
        self.assertFalse(self.m.close_market(INST, 'account', 'long', 1))
        self.assertEqual(len(self.ex.calls), 1)

    def test_algo_effective_requires_actual_child_fill(self):
        self.seed(1)
        bk = self.m._inst(INST)['trend']
        bk['algo']['long'] = dict(algo_id='A1', amount=1)
        self.ex.algos['A1'] = {'state': 'effective'}
        self.m.poll_algo_triggers(INST, 'trend')
        self.assertEqual(self.held(), 1)
        self.assertIsNotNone(bk['algo']['long'])
        self.ex.algos['A1']['ordIdList'] = ['C1']
        self.ex.add('C1', 1, 'sell', True)
        self.fill(.4, 'C1', 'partially_filled')
        self.m.poll_algo_triggers(INST, 'trend')
        self.m.poll_algo_triggers(INST, 'trend')
        self.assertAlmostEqual(self.held(), .6)
        self.fill(1, 'C1')
        self.m.poll_algo_triggers(INST, 'trend')
        self.assertEqual(self.held(), 0)
        self.assertIsNone(bk['algo']['long'])

    def test_external_order_disappearance_block_survives_context_restart(self):
        self.ex.add('MANUAL', 1)
        self.assertFalse(self.m.synchronize(INST)['entry_allowed'])
        self.assertEqual(self.ex.cancels, [])
        self.fill(1, 'MANUAL')
        self.restart()
        # 【2026-09-28 按用户要求移除 G1】外部单已成交但交易所持仓快照尚未反映时，
        # 不再用“对账未决”闸门拦截开仓；真正的防重复开仓由 _entry_room 的
        # max(交易所真实持仓, 账本合计) 保证——一旦快照显示已持有 1 张即 room=0。
        self.ex.positions['long'] = 1
        self.assertTrue(self.m.synchronize(INST)['success'])
        self.assertFalse(self.open_range())
        self.assertEqual(self.held(), 1)

    def test_position_or_order_failure_is_not_empty_position(self):
        self.seed(1)
        self.ex.fail_positions = True
        self.assertFalse(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held(), 1)
        self.ex.fail_positions = False
        self.ex.fail_orders = True
        self.assertFalse(self.open_range())
        self.assertEqual(self.ex.calls, [])

    def test_unsupported_mode_records_facts_and_keeps_trading(self):
        self.ex.mode = 'long_short_mode'
        self.ex.positions['long'] = .5
        self.assertFalse(self.m.synchronize(INST)['success'])
        self.assertEqual(self.held(), .5)
        # 【2026-09-28 按用户要求移除 G2】账户形状未知（如 long_short_mode）不再
        # 一票停摆交易：synchronize 仍如实记录持仓事实，开仓额度按
        # max(交易所快照, 账本) 算账（此处 .5），不再因“账户形状未知”归零。
        self.assertTrue(self.open_range())

    def test_observation_sync_does_not_reissue_cancellation(self):
        pt = self.pending()
        pt['cancel_requested'] = True
        self.m.synchronize(INST, cancel_entries=False)
        self.assertEqual(self.ex.cancels, [])
        self.assertEqual(self.ex.calls, [])

    def test_online_book_cannot_be_cleared_by_legacy_helpers(self):
        self.seed(1)
        with self.assertRaises(RuntimeError):
            self.m.clear_bucket(INST, 'trend')
        with self.assertRaises(RuntimeError):
            self.m.note_external_close(INST, 'trend', 'long', 1)
        self.assertEqual(self.held(), 1)

    def test_disabled_strategy_still_syncs_account_facts(self):
        trader = load_definitions(HERE / 'trend_range_trader.py', DualPositionOrderManager=self.cls,
                                  generate_run_id=lambda: 'batch')
        obj = trader.TrendRangeTrader.__new__(trader.TrendRangeTrader)
        obj.pos_mgr, obj.trade_executor = self.m, self.ex
        obj._manual_pause, obj._run_count = {}, 0
        obj._trade_lock = threading.RLock()
        obj._load_config = lambda: {'global_settings': {'enabled': False}}
        self.ex.positions['long'] = .8
        obj.run_real_trading_batch()
        self.assertEqual(self.held(), .8)
        self.assertEqual(self.ex.calls, [])

    def test_manual_force_close_waits_for_cancel_confirmation(self):
        trader = load_definitions(HERE / 'trend_range_trader.py', DualPositionOrderManager=self.cls)
        obj = trader.TrendRangeTrader.__new__(trader.TrendRangeTrader)
        obj.pos_mgr = self.m
        obj._manual_pause = {}
        obj._trade_lock = threading.RLock()
        obj._load_config = lambda: {}
        obj._close_amount = lambda *a, **k: self.fail('未确认撤单不能发送平仓')
        self.seed(.5)
        self.pending(.5)
        self.ex.cancel_confirms = False
        msgs = obj.force_close_manual(INST)
        self.assertIn('尚未确认', msgs[0])
        self.assertEqual(self.held(), .5)

    def test_same_round_context_does_not_drop_manual_hold(self):
        self.m.hold_entries(INST, 0)
        self.m.set_context('round-1', False)
        self.assertFalse(self.open_range())

    def test_scheduler_stale_pause_cache_cannot_erase_manager_pause(self):
        trader = load_definitions(HERE / 'trend_range_trader.py', DualPositionOrderManager=self.cls)
        obj = trader.TrendRangeTrader.__new__(trader.TrendRangeTrader)
        obj.pos_mgr = self.m
        obj._manual_pause = {}
        self.m.hold_entries(INST, 600)
        obj._save_manual_pause()
        self.assertGreater(obj._manual_pause_remaining(INST), 590)
        self.restart()
        self.assertGreater(self.m.state[INST]['pause_until'], time.time())

    def test_analysis_failure_still_synchronizes_manual_position(self):
        trader = load_definitions(HERE / 'trend_range_trader.py', DualPositionOrderManager=self.cls)
        obj = trader.TrendRangeTrader.__new__(trader.TrendRangeTrader)
        obj.pos_mgr = self.m
        obj._manual_pause = {}
        obj._trade_lock = threading.RLock()
        obj._load_config = lambda: {}
        obj._get_strategy_adapter = lambda: types.SimpleNamespace(analyze=lambda *a, **k: None)
        self.ex.positions['long'] = .8
        result = obj.analyze_and_trade_real(INST, '15m', '4H', run_id='test')
        self.assertEqual(result['error'], '双周期趋势分析失败')
        self.assertEqual(self.held(), .8)


class ExecutorRegression(unittest.TestCase):
    def setUp(self):
        cls = load_definitions(HERE / 'utils/trade_executor.py', names={'TradeExecutor'}).TradeExecutor
        self.ex = cls.__new__(cls)
        self.ex._rl = lambda category, api, **params: api(**params)
        self.ex.get_last_price = lambda inst: 100

    def test_malformed_timeout_and_duplicate_responses_are_not_rejections(self):
        for result in ({}, {'code': '0', 'data': [{}]}, {'code': '50004'},
                       {'code': '1', 'data': [{'sCode': '50004'}]}, {'code': '51011'}):
            with self.subTest(result=result):
                self.ex.trade_api = types.SimpleNamespace(place_order=lambda **k: result)
                response = self.ex.execute_trade(INST, 'buy', .03, 100, cl_ord_id='stable')
                self.assertFalse(response.get('rejected', False))
        self.assertTrue(self.ex._place_rejected({'code': '1', 'data': [{'sCode': '51008'}]}))

    def test_client_id_and_small_reduce_size_preserved(self):
        sent = []
        self.ex.trade_api = types.SimpleNamespace(place_order=lambda **k: sent.append(k) or
                                                  {'code': '0', 'data': [{'sCode': '0', 'ordId': 'x'}]})
        self.ex.try_get_positions_by_mode = lambda inst: {'cross': [
            {'pos': .03, 'posSide': 'net', 'mgnMode': 'cross'}], 'isolated': []}
        response = self.ex.execute_reduce_only_order(INST, 'sell', .03, 'cross', cl_ord_id='stable')
        self.assertTrue(response['success'])
        self.assertEqual(sent[0]['sz'], '0.03')
        self.assertEqual(sent[0]['clOrdId'], 'stable')

    def test_zero_or_wrong_direction_position_does_not_send_reduce(self):
        self.ex.trade_api = types.SimpleNamespace(place_order=lambda **k: self.fail('不得发送减仓'))
        for qty in (0, -.3):
            self.ex.try_get_positions_by_mode = lambda inst: {'cross': [
                {'pos': qty, 'mgnMode': 'cross', 'posSide': 'net'}], 'isolated': []}
            result = self.ex.execute_reduce_only_order(INST, 'sell', .3, 'cross', cl_ord_id='stable')
            self.assertFalse(result['success'])
            self.assertTrue(result['rejected'])

    def test_order_directory_pagination_and_fail_closed(self):
        def normal(**params):
            if params.get('after'):
                return {'code': '0', 'data': [{'ordId': 'last'}]}
            return {'code': '0', 'data': [{'ordId': str(i)} for i in range(100)]}
        self.ex.trade_api = types.SimpleNamespace(get_order_list=normal,
                                                  order_algos_list=lambda **k: {'code': '0', 'data': []})
        self.assertEqual(len(self.ex.try_get_open_orders(INST)), 101)
        self.ex.trade_api.order_algos_list = lambda **k: {'code': '50004', 'data': []}
        self.assertIsNone(self.ex.try_get_open_orders(INST))

    def test_non_swap_positions_are_excluded_from_strategy_catalog(self):
        self.ex._get_positions_raw = lambda inst: {'code': '0', 'data': [
            {'instType': 'FUTURES', 'pos': '1', 'mgnMode': 'cross'},
            {'instType': 'SWAP', 'pos': '.03', 'mgnMode': 'cross'}]}
        result = self.ex.try_get_positions_by_mode()
        self.assertEqual(len(result['cross']), 1)
        self.assertEqual(result['cross'][0]['pos'], .03)


class StateRepoRegression(unittest.TestCase):
    """真实 V2 repo + 真实 KV 模型/原子合并，在 SQLite 内存库验证事务语义。"""
    def setUp(self):
        from sqlalchemy import create_engine, select, String, Text, DateTime
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
        class Base(DeclarativeBase):
            pass
        self.KV = load_definitions(ROOT / 'crypto/models.py', names={'KVStore'},
                                  Base=Base, Mapped=Mapped, mapped_column=mapped_column,
                                  String=String, Text=Text, DateTime=DateTime,
                                  datetime=datetime.datetime).KVStore
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.Session = Session
        config = load_definitions(ROOT / 'crypto/config_store_repo.py',
                                  names={'patch_json_config', 'CorruptConfigError'},
                                  KVStore=self.KV, _invalidate_after_commit=lambda *a: None)
        package = types.ModuleType('isolated_sync_repo')
        package.__path__ = []
        module = types.ModuleType('isolated_sync_repo.config_store_repo')
        module.patch_json_config = config.patch_json_config
        modules = patch.dict(sys.modules, {'isolated_sync_repo': package,
                                          'isolated_sync_repo.config_store_repo': module})
        modules.start()
        self.addCleanup(modules.stop)
        self.repo = load_definitions(ROOT / 'crypto/trader_state_repo.py',
                                     KVStore=self.KV, select=select,
                                     __package__='isolated_sync_repo')

    def save(self, state, account='a', env='1'):
        with self.Session(self.engine) as session, session.begin():
            return self.repo.save_inst_position(session, INST, state, account, env)

    def load(self, account='a', env='1'):
        with self.Session(self.engine) as session:
            return self.repo.load_position_state(session, account, env)

    def test_full_state_roundtrip_and_partition(self):
        state = dict(settlement_pending=True, pause_until=1000,
                     market_orders={'trend:long': {'state': 'PENDING', 'cl_ord_id': 'stable',
                                                   'acc_filled': .3, 'fill_notional': 30}},
                     external_order_watch={'manual': {'baseline': {'long': 2}}})
        self.assertEqual(self.save(state), 1)
        self.assertEqual(self.load()[INST], dict(state, _revision=1))
        self.assertEqual(self.load('b'), {})
        self.assertEqual(self.load(env='0'), {})

    def test_revision_conflict_preserves_original(self):
        self.save({'pause_until': 1000})
        with self.assertRaises(RuntimeError):
            self.save({'pause_until': 0})
        self.assertEqual(self.load()[INST]['pause_until'], 1000)

    def test_rollback_does_not_advance_memory_revision(self):
        state = {'pause_until': 1000}
        with self.Session(self.engine) as session:
            self.repo.save_inst_position(session, INST, state, 'a', '1')
            session.rollback()
        self.assertEqual(state, {'pause_until': 1000})
        self.assertEqual(self.load(), {})

    def test_corrupt_state_cannot_start_empty_or_be_overwritten(self):
        key = self.repo._position_prefix('a', '1') + INST
        with self.Session(self.engine) as session, session.begin():
            session.add(self.KV(key=key, value='broken-json'))
        with self.assertRaises(ValueError):
            self.load()
        with self.assertRaises(Exception):
            self.save({})
        with self.Session(self.engine) as session:
            self.assertEqual(session.get(self.KV, key).value, 'broken-json')


if __name__ == '__main__':
    unittest.main(verbosity=2)
