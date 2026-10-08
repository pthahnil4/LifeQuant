# -*- coding: utf-8 -*-
"""轮初对账范围划分的离线回归。

运行：python -B crypto/task/_smoke_sync_scope.py

只加载 trend_range_trader.py 的类定义本体（load_definitions 跳过顶层导入），
账本/合约规格/日志全用内存替身，并禁止 socket 联网：不碰 OKX、不碰业务库。

覆盖的判定口径（对应"一个幽灵币种冻结全账户开仓"的修复）：
  · 在配置里 或 出现在交易所持仓/挂单目录里 → 无条件同步；
  · 账本记着持仓/在途委托 → 同步（合约不存在也不跳过，只报错）；
  · FILLED/EXPIRED 终态残留不算敞口，unknown/cancel_requested 算；
  · 账本干净且交易所侧无它 → 跳过（且不必联网问存在性）；
  · 仅带风险标记 + 交易所明确无此合约 → 跳过并从内存账本丢弃；
  · 存在性未知（查询失败）→ 保守照旧同步。
"""
import socket
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from _smoke_manual_override_regression import HERE, load_definitions

sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

GHOST = 'GHOST-USDT-SWAP'        # 交易所已无此合约（误配 ID 留下的账本）
REMOVED = 'REMOVED-USDT-SWAP'    # 币种存在，但已从配置移除、账本已空
ALIVE = 'ALIVE-USDT-SWAP'        # 仍在配置里
STRAY = 'STRAY-USDT-SWAP'        # 不在配置，但交易所目录里有它的持仓
UNKNOWN = 'UNKNOWN-USDT-SWAP'    # 合约存在性查不出来（网络失败）


def make_bucket(**over):
    """一个篮子的最小账本结构（默认全空：held=0、槽位 IDLE、无兜底委托）"""
    bk = {'held': {'long': 0.0, 'short': 0.0},
          'avg_px': {'long': 0.0, 'short': 0.0},
          'slots': {'entry': {'state': 'IDLE'}, 'exit': {'state': 'IDLE'}},
          'algo': {'long': None, 'short': None}}
    bk.update(over)
    return bk


class FakeSpec:
    """合约规格替身：只回答存在性，并记录被问了几次（用于断言不白打接口）"""

    def __init__(self, exists):
        self._exists = dict(exists)
        self.asked = []

    def instrument_exists(self, inst_id):
        self.asked.append(inst_id)
        return self._exists.get(inst_id)


class LogCollector:
    """接住模块级 task_log：既能断言刷屏去重，又不污染生产日志"""

    def __init__(self):
        self.lines = []

    def info(self, msg, *a, **k):
        self.lines.append(('info', str(msg)))

    def warning(self, msg, *a, **k):
        self.lines.append(('warning', str(msg)))

    def error(self, msg, *a, **k):
        self.lines.append(('error', str(msg)))


class SyncScopeRegression(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, 'connect',
                               side_effect=AssertionError('本冒烟禁止联网'))
        network.start()
        self.addCleanup(network.stop)

        self.collector = LogCollector()
        self.logs = self.collector.lines

        ns = load_definitions(HERE / 'trend_range_trader.py',
                              names=['TrendRangeTrader'],
                              task_log=self.collector,
                              # 类体仅取 POS_DUST 常量，不必加载真实账本管理器
                              DualPositionOrderManager=types.SimpleNamespace(
                                  POS_DUST=1e-10),
                              generate_run_id=lambda: 'SMOKE')
        cls = ns.TrendRangeTrader
        self.t = cls.__new__(cls)
        self.t.pos_mgr = types.SimpleNamespace(
            POS_DUST=1e-10, state={})
        self.t.spec_cache = FakeSpec({GHOST: False, REMOVED: True, ALIVE: True,
                                      STRAY: True, UNKNOWN: None})
        self.t._sync_skip_logged = None
        self.t._trade_lock = threading.RLock()
        self.t.stop_event = threading.Event()

    def _skip_lines(self):
        return [m for _lv, m in self.logs if '轮初对账跳过' in m]

    def run_partition(self, instruments, configured, real_ids=()):
        return self.t._partition_sync_scope('SMOKE', list(instruments),
                                            set(configured), set(real_ids))

    # ------------------------------------------------------------------
    # 1. 无条件同步的范围
    # ------------------------------------------------------------------
    def test_configured_always_synced(self):
        self.t.pos_mgr.state = {ALIVE: {'trend': make_bucket(), 'range': make_bucket()}}
        to_sync, skipped = self.run_partition([ALIVE], configured=[ALIVE])
        self.assertEqual(to_sync, [ALIVE])
        self.assertEqual(skipped, [])
        self.assertIn(ALIVE, self.t.pos_mgr.state, '配置中的币种绝不被摘出账本')

    def test_configured_even_with_risk_flags(self):
        self.t.pos_mgr.state = {ALIVE: {'sync_error': 'x', 'settlement_pending': True}}
        to_sync, _ = self.run_partition([ALIVE], configured=[ALIVE])
        self.assertEqual(to_sync, [ALIVE])
        self.assertNotIn(ALIVE, self.t.spec_cache.asked,
                         '配置内币种不该被问存在性（用不到）')

    def test_exchange_catalog_forces_sync(self):
        """不在配置，但交易所目录里有它的持仓/挂单 → 必须同步"""
        self.t.pos_mgr.state = {STRAY: {'trend': make_bucket(), 'range': make_bucket()}}
        to_sync, skipped = self.run_partition([STRAY], configured=[], real_ids=[STRAY])
        self.assertEqual(to_sync, [STRAY])
        self.assertEqual(skipped, [])

    # ------------------------------------------------------------------
    # 2. 真实敞口永不被跳过
    # ------------------------------------------------------------------
    def test_ghost_with_position_still_synced(self):
        """合约不存在但账本记着持仓：保留同步（脏账本必须人工核实），不得静默放行"""
        self.t.pos_mgr.state = {GHOST: {'trend': make_bucket(held={'long': 3.0, 'short': 0.0})}}
        to_sync, skipped = self.run_partition([GHOST], configured=[])
        self.assertEqual(to_sync, [GHOST])
        self.assertEqual(skipped, [])
        self.assertIn(GHOST, self.t.pos_mgr.state, '有敞口时不得从内存账本丢弃')

    def test_pending_slot_blocks_skip_even_for_ghost(self):
        pend = make_bucket(slots={'entry': {'state': 'PENDING'}, 'exit': {'state': 'IDLE'}})
        self.t.pos_mgr.state = {GHOST: {'trend': pend, 'range': make_bucket()}}
        to_sync, skipped = self.run_partition([GHOST], configured=[])
        self.assertEqual(to_sync, [GHOST])
        self.assertEqual(skipped, [])

    def test_algo_backup_blocks_skip(self):
        algo = make_bucket(algo={'long': {'algo_id': 'a1'}, 'short': None})
        self.t.pos_mgr.state = {REMOVED: {'trend': algo, 'range': make_bucket()}}
        to_sync, skipped = self.run_partition([REMOVED], configured=[])
        self.assertEqual(to_sync, [REMOVED])
        self.assertEqual(skipped, [])

    def test_exchange_totals_count_as_open_interest(self):
        self.t.pos_mgr.state = {REMOVED: {'exchange_totals': {'long': 0.5, 'short': 0.0}}}
        to_sync, _ = self.run_partition([REMOVED], configured=[])
        self.assertEqual(to_sync, [REMOVED])

    def test_dust_level_not_counted(self):
        """粉尘级残渣低于 POS_DUST，不算敞口（否则对账浮点残渣会永久锁住跳过逻辑）"""
        empty = {'trend': make_bucket(held={'long': 1e-14, 'short': 0.0}),
                 'range': make_bucket()}
        self.t.pos_mgr.state = {REMOVED: empty}
        _to_sync, skipped = self.run_partition([REMOVED], configured=[])
        self.assertEqual(skipped, [REMOVED])

    # ------------------------------------------------------------------
    # 2b. 终态残留不算敞口（已移除币种的账本常年拖着 FILLED/EXPIRED）
    # ------------------------------------------------------------------
    def test_filled_slot_residue_is_not_open_interest(self):
        """成交入账后的 FILLED 残留：持仓已为 0 就没有待解释事实"""
        self.t.pos_mgr.state = {REMOVED: {
            'trend': make_bucket(),
            'range': make_bucket(slots={'entry': {'state': 'FILLED', 'amount': 4.3},
                                        'exit': {'state': 'IDLE'}})}}
        to_sync, skipped = self.run_partition([REMOVED], configured=[])
        self.assertEqual(to_sync, [])
        self.assertEqual(skipped, [REMOVED])

    def test_expired_market_order_residue_is_not_open_interest(self):
        self.t.pos_mgr.state = {REMOVED: {
            'trend': make_bucket(slots={'entry': {'state': 'EXPIRED'},
                                        'exit': {'state': 'IDLE'}}),
            'range': make_bucket(),
            'market_orders': {'range:long': {'state': 'FILLED'},
                              'trend:short': {'state': 'EXPIRED'}}}}
        _to_sync, skipped = self.run_partition([REMOVED], configured=[])
        self.assertEqual(skipped, [REMOVED])

    def test_filled_but_unknown_slot_still_synced(self):
        """终态并不意味着问清楚了：带 unknown/cancel_requested 标记时照样同步"""
        self.t.pos_mgr.state = {REMOVED: {
            'trend': make_bucket(slots={'entry': {'state': 'FILLED', 'unknown': True},
                                        'exit': {'state': 'IDLE'}}),
            'range': make_bucket()}}
        to_sync, _skipped = self.run_partition([REMOVED], configured=[])
        self.assertEqual(to_sync, [REMOVED])

    def test_unsettled_market_order_blocks_skip(self):
        """市价平仓意图还停在 PENDING → 必须同步，不能被终态豁免误伤"""
        self.t.pos_mgr.state = {REMOVED: {
            'trend': make_bucket(), 'range': make_bucket(),
            'market_orders': {'range:long': {'state': 'PENDING'}}}}
        to_sync, skipped = self.run_partition([REMOVED], configured=[])
        self.assertEqual(to_sync, [REMOVED])
        self.assertEqual(skipped, [])

    def test_unrecognized_slot_state_syncs(self):
        """不认识的槽位状态（不在终态表里）一律当在途处理"""
        self.t.pos_mgr.state = {REMOVED: {
            'trend': make_bucket(slots={'entry': {'state': 'PARTIALLY_FILLED'},
                                        'exit': {'state': 'IDLE'}}),
            'range': make_bucket()}}
        to_sync, _ = self.run_partition([REMOVED], configured=[])
        self.assertEqual(to_sync, [REMOVED])

    # ------------------------------------------------------------------
    # 3. 幽灵/空账本被摘出，且不再污染全账户
    # ------------------------------------------------------------------
    def test_ghost_with_risk_flags_is_dropped(self):
        """本次事故的正解：sync_error 挂在不存在合约上 → 跳过 + 丢弃内存条目"""
        self.t.pos_mgr.state = {GHOST: {'trend': make_bucket(), 'range': make_bucket(),
                                         'sync_error': '持仓、订单或账户模式查询不完整'}}
        to_sync, skipped = self.run_partition([GHOST], configured=[])
        self.assertEqual(to_sync, [])
        self.assertEqual(skipped, [GHOST])
        self.assertNotIn(GHOST, self.t.pos_mgr.state,
                         '幽灵条目必须离开内存账本，否则总额度计算会把它算进去')

    def test_idle_removed_entry_skipped_without_network(self):
        """账本干净的空条目：跳过，且不去问存在性（不白打 OKX 接口）"""
        self.t.pos_mgr.state = {REMOVED: {'trend': make_bucket(), 'range': make_bucket()}}
        self.t.spec_cache.asked.clear()
        to_sync, skipped = self.run_partition([REMOVED], configured=[])
        self.assertEqual(to_sync, [])
        self.assertEqual(skipped, [REMOVED])
        self.assertEqual(self.t.spec_cache.asked, [], '无事实可核对时不该联网')

    def test_unknown_existence_keeps_risky_entry_synced(self):
        """查询失败（None）不等于不存在：带风险标记的条目照旧同步，保守不误放行"""
        self.t.pos_mgr.state = {UNKNOWN: {'trend': make_bucket(), 'range': make_bucket(),
                                          'settlement_pending': True}}
        to_sync, skipped = self.run_partition([UNKNOWN], configured=[])
        self.assertEqual(to_sync, [UNKNOWN])
        self.assertEqual(skipped, [])
        self.assertIn(UNKNOWN, self.t.pos_mgr.state)

    def test_active_manual_pause_on_existing_coin_syncs(self):
        """人工冷却未消化 + 合约确实存在 → 照常同步（冷却语义不能被跳过打穿）"""
        self.t.pos_mgr.state = {REMOVED: {'trend': make_bucket(), 'range': make_bucket(),
                                           'pause_until': time_now() + 600}}
        to_sync, _ = self.run_partition([REMOVED], configured=[])
        self.assertEqual(to_sync, [REMOVED])

    def test_expired_pause_does_not_block_skip(self):
        self.t.pos_mgr.state = {REMOVED: {'trend': make_bucket(), 'range': make_bucket(),
                                           'pause_until': time_now() - 1}}
        _to_sync, skipped = self.run_partition([REMOVED], configured=[])
        self.assertEqual(skipped, [REMOVED])

    # ------------------------------------------------------------------
    # 4. 混合场景：一个幽灵不能拖累其他币
    # ------------------------------------------------------------------
    def test_mixed_round_keeps_trading_coins_in_scope(self):
        self.t.pos_mgr.state = {
            ALIVE: {'trend': make_bucket(), 'range': make_bucket()},
            GHOST: {'sync_error': 'boom', 'trend': make_bucket(), 'range': make_bucket()},
            REMOVED: {'trend': make_bucket(), 'range': make_bucket()},
        }
        to_sync, skipped = self.run_partition(
            [ALIVE, GHOST, REMOVED], configured=[ALIVE])
        self.assertEqual(to_sync, [ALIVE], '在交易的币种必须照常同步')
        self.assertEqual(sorted(skipped), [GHOST, REMOVED])

    def test_log_dedup_until_set_changes(self):
        self.t.pos_mgr.state = {GHOST: {'sync_error': 'boom'}}
        self.logs.clear()
        self.run_partition([GHOST], configured=[])
        self.assertEqual(len(self._skip_lines()), 1)
        self.run_partition([GHOST], configured=[])
        self.assertEqual(len(self._skip_lines()), 1,
                         '集合未变化不该每轮重复刷屏')
        self.t.pos_mgr.state = {REMOVED: {'trend': make_bucket(), 'range': make_bucket()}}
        self.run_partition([REMOVED], configured=[])
        self.assertEqual(len(self._skip_lines()), 2,
                         '集合变化后应重新通报一次')

    def test_missing_state_entry_treated_as_empty(self):
        """instruments 里有、账本里没有（纯交易所目录外的合约）：跳过且不报错"""
        to_sync, skipped = self.run_partition(['NEW-USDT-SWAP'], configured=[])
        self.assertEqual(to_sync, [])
        self.assertEqual(skipped, ['NEW-USDT-SWAP'])

    def test_bare_stub_without_spec_cache_still_syncs(self):
        """__new__ 裸实例桩没有 spec_cache 也不该报错，并保守地继续同步"""
        del self.t.spec_cache
        self.t.pos_mgr.state = {GHOST: {'trend': make_bucket(), 'range': make_bucket(),
                                         'sync_error': 'boom'}}
        to_sync, skipped = self.run_partition([GHOST], configured=[])
        self.assertEqual(to_sync, [GHOST])
        self.assertEqual(skipped, [])

    def test_spec_probe_exception_is_unknown(self):
        """存在性查询抛异常 = 未知，不能当成"不存在"而跳过对账"""
        self.t.spec_cache = types.SimpleNamespace(
            instrument_exists=lambda _i: (_ for _ in ()).throw(RuntimeError('网络断')))
        self.t.pos_mgr.state = {GHOST: {'trend': make_bucket(), 'range': make_bucket(),
                                         'sync_error': 'boom'}}
        to_sync, skipped = self.run_partition([GHOST], configured=[])
        self.assertEqual(to_sync, [GHOST])
        self.assertEqual(skipped, [])


class NotExistVerdictRegression(unittest.TestCase):
    """合约"不存在"判定口径：只有交易所明确否定才能放行跳过"""

    def setUp(self):
        ns = load_definitions(HERE / 'utils/instrument_spec.py',
                              names=['_is_not_exist_response'])
        self.fn = ns._is_not_exist_response

    def test_51001_is_not_exist(self):
        self.assertTrue(self.fn({'code': '51001',
                                 'msg': "Instrument ID, Instrument ID code, or "
                                        "Spread ID doesn't exist."}))

    def test_throttle_is_not_not_exist(self):
        """限频/超时/格式错误都是临时失败，绝不能被当成合约不存在"""
        for resp in ({'code': '50011', 'msg': 'Too Many Requests'},
                     {'code': '', 'msg': ''}, {'code': '0', 'msg': ''},
                     None, 'not-a-dict', {}):
            self.assertFalse(self.fn(resp), f'{resp!r} 不该被判为不存在')

    def test_msg_only_fallback(self):
        self.assertTrue(self.fn({'code': '1', "msg": "Instrument ID doesn't exist."}))


def time_now():
    import time as _t
    return _t.time()


if __name__ == '__main__':
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(SyncScopeRegression))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(NotExistVerdictRegression))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    ok = result.wasSuccessful()
    print(f'\n{"ALL PASS" if ok else "FAILED"} '
          f'pass={result.testsRun - len(result.failures) - len(result.errors)} '
          f'fail={len(result.failures)} error={len(result.errors)}')
    sys.exit(0 if ok else 1)
