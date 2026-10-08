# -*- coding: utf-8 -*-
"""执行器「轮初全账户快照缓存」（Option B）的离线回归。

运行：python -B crypto/task/_smoke_round_snapshot.py

只加载 trade_executor.py 的 TradeExecutor 类定义本体（load_definitions 跳过顶层
业务导入），用裸实例 `__new__` 跳过 __init__（不建 OKX 客户端、不联网），把
`_rl`/`_get_positions_raw`/`trade_api`/`account_api` 换成计数替身，验证：

  · 快照激活后，按 instId 读持仓/挂单/账户模式全部本地命中、零跨境 API；
  · 无该币事实时返回空视图（等价真实空仓），仍零 API；
  · invalidate_snapshot(inst) 只让该币回落 live，其它币继续命中；
  · inst_id=None（全账户）读永远走 live，绝不吃快照；
  · 任一写操作（_write）自动失效相关 inst；取不到 instId 时停用整轮快照；
  · end_round_snapshot / begin(None) 后读路径全部回落 live。

对应性能优化「改动1」：消除每币每轮 4-6 次 synchronize 里重复的
positions + orders(7来源) 跨境查询。
"""
import sys
import unittest

from _smoke_manual_override_regression import HERE, load_definitions

sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

TradeExecutor = load_definitions(
    HERE / 'utils/trade_executor.py', names={'TradeExecutor'}).TradeExecutor

A = 'AAA-USDT-SWAP'
B = 'BBB-USDT-SWAP'


class Counter:
    """记录 live 读被真正触发的次数（快照命中时应为 0）。"""

    def __init__(self):
        self.positions = 0
        self.orders = 0
        self.mode = 0


def _pos_row(inst, mode='cross', pos='1'):
    return {'instId': inst, 'instType': 'SWAP', 'pos': pos, 'mgnMode': mode,
            'avgPx': '5', 'posSide': 'net', 'uTime': '1', 'posId': mode + inst}


def _order(inst, oid, algo=False):
    key = 'algoId' if algo else 'ordId'
    return {'instId': inst, key: oid, '_algo': algo}


class SnapshotCacheTest(unittest.TestCase):
    def setUp(self):
        self.ex = TradeExecutor.__new__(TradeExecutor)   # 裸实例，跳过 __init__
        self.cnt = Counter()

        # live 读替身：记录调用次数并返回可辨识的最小结构
        def _raw_positions(inst_id=None):
            self.cnt.positions += 1
            return {'code': '0', 'data': [_pos_row(inst_id or A, 'cross', '9')]}

        def _rl(group, func, *a, **k):
            if group == 'positions':
                self.cnt.mode += 1
                return {'code': '0', 'data': [{'posMode': 'net_mode'}]}
            self.cnt.orders += 1
            return {'code': '0', 'data': []}

        self.ex._get_positions_raw = _raw_positions
        self.ex._rl = _rl
        # try_get_open_orders 的 sources 引用这两个句柄，仅需存在即可（_rl 已被替身拦截）
        self.ex.trade_api = type('T', (), {'get_order_list': None, 'order_algos_list': None})()
        self.ex.account_api = type('Ac', (), {'get_account_config': None})()

        # 轮初全账户快照：A 有 1 全仓多 + 1 普通挂单；B 无任何事实
        self.positions = {'cross': [_pos_row(A, 'cross', '2')], 'isolated': []}
        self.orders = [_order(A, 'o1', algo=False), _order(A, 'a1', algo=True)]

    def _begin(self):
        ok = self.ex.begin_round_snapshot(
            positions=self.positions, orders=self.orders, mode='net_mode')
        self.assertTrue(ok)

    # ---- 命中：零 API ----
    def test_snapshot_hit_zero_api(self):
        self._begin()
        pos = self.ex.try_get_positions_by_mode(A)
        self.assertEqual(self.cnt.positions, 0)
        self.assertEqual(len(pos['cross']), 1)
        self.assertEqual(pos['cross'][0]['instId'], A)
        self.assertEqual(pos['isolated'], [])

        orders = self.ex.try_get_open_orders(A)
        self.assertEqual(self.cnt.orders, 0)
        self.assertEqual(len(orders), 2)          # 普通单 + 算法单都被本地过滤回来
        self.assertTrue(any(o.get('_algo') for o in orders))

        mode = self.ex.try_get_position_mode()
        self.assertEqual(self.cnt.mode, 0)
        self.assertEqual(mode, 'net_mode')

    def test_snapshot_empty_inst_is_local(self):
        self._begin()
        pos = self.ex.try_get_positions_by_mode(B)   # B 无事实 → 空仓视图
        self.assertEqual(self.cnt.positions, 0)
        self.assertEqual(pos, {'cross': [], 'isolated': []})
        self.assertEqual(self.ex.try_get_open_orders(B), [])
        self.assertEqual(self.cnt.orders, 0)

    # ---- 失效：单币回落 live，其它币仍命中 ----
    def test_invalidate_single_inst(self):
        self._begin()
        self.ex.invalidate_snapshot(A)
        pos = self.ex.try_get_positions_by_mode(A)   # 走 live
        self.assertEqual(self.cnt.positions, 1)
        self.assertEqual(pos['cross'][0]['pos'], 9.0)  # live 替身返回的值
        # B 未失效，仍本地命中
        self.ex.try_get_positions_by_mode(B)
        self.assertEqual(self.cnt.positions, 1)

    def test_none_instid_always_live(self):
        self._begin()
        self.ex.try_get_positions_by_mode(None)      # 全账户 → 永远 live
        self.assertEqual(self.cnt.positions, 1)
        self.ex.try_get_open_orders(None)
        self.assertGreaterEqual(self.cnt.orders, 1)  # 全账户挂单走 live（7 来源逐一查询）

    # ---- 写操作自动失效 ----
    def test_write_invalidates_related_inst(self):
        self._begin()
        calls = []
        self.ex._write(lambda **kw: calls.append(kw) or {'code': '0'}, instId=A, sz='1')
        self.assertEqual(calls and calls[0]['instId'], A)
        self.ex.try_get_positions_by_mode(A)         # A 已失效 → live
        self.assertEqual(self.cnt.positions, 1)
        self.ex.try_get_positions_by_mode(B)         # B 不受影响 → 命中
        self.assertEqual(self.cnt.positions, 1)

    def test_write_batch_arg_invalidates_each(self):
        self._begin()
        self.ex._write(lambda *a, **k: {'code': '0'},
                       [{'instId': A, 'algoId': 'a1'}, {'instId': B, 'algoId': 'b1'}])
        self.ex.try_get_positions_by_mode(A)
        self.ex.try_get_positions_by_mode(B)
        self.assertEqual(self.cnt.positions, 2)      # 两币都被失效 → 各走一次 live

    def test_write_without_instid_disables_whole_snapshot(self):
        self._begin()
        self.ex._write(lambda *a, **k: {'code': '0'}, sz='1')  # 无 instId → 保守停用整轮
        self.ex.try_get_positions_by_mode(A)
        self.ex.try_get_positions_by_mode(B)
        self.assertEqual(self.cnt.positions, 2)      # 全部回落 live

    # ---- 生命周期 ----
    def test_end_round_snapshot_restores_live(self):
        self._begin()
        self.ex.end_round_snapshot()
        self.ex.try_get_positions_by_mode(A)
        self.assertEqual(self.cnt.positions, 1)

    def test_begin_none_disables(self):
        ok = self.ex.begin_round_snapshot(positions=None, orders=self.orders, mode='net_mode')
        self.assertFalse(ok)
        self.ex.try_get_positions_by_mode(A)
        self.assertEqual(self.cnt.positions, 1)      # 未启用快照 → live

    def test_no_snapshot_is_live_by_default(self):
        # 从未 begin：裸实例读路径必须照常 live，绝不因缺快照而报错
        self.ex.try_get_positions_by_mode(A)
        self.assertEqual(self.cnt.positions, 1)
        self.assertEqual(self.ex.try_get_position_mode(), 'net_mode')
        self.assertEqual(self.cnt.mode, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
