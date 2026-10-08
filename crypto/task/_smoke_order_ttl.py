# -*- coding: utf-8 -*-
"""临时冒烟：挂单 TTL（order_ttl_periods）到期撤销机制

覆盖：
1. 超龄**趋势仓**挂单（>TTL）在 poll_fills 被撤销并复位槽位
2. 趋势仓未超龄挂单不受影响
3. ttl_seconds=0（关闭）不撤销
4. TTL 撤销不动账本持仓（不触发手动平仓误判）
5. **区间波动仓挂单永不受 TTL 约束**（持久挂单，随 BOLL 边界改单追价）
6. 部分成交的超龄趋势仓挂单：已成交部分先入账再撤销
"""
import sys
import time
import threading

sys.path.insert(0, r'd:\python\cryptoTrade')
import crypto.task.utils.position_order_manager as pom
from crypto.task.utils.position_order_manager import (
    DualPositionOrderManager as M,
    BUCKET_TREND, BUCKET_RANGE, SLOT_ENTRY, ST_PENDING, ST_IDLE)

# 成交流水写库入口置空：本冒烟只验槽位与 TTL 行为，不得往业务库写 test 成交。
pom.journal_fill = lambda *a, **k: None

INST = 'X-USDT-SWAP'


class FakeExec:
    """最小交易所替身：只会回答挂单/撤单/查单三件事。

    V2 账本不再相信"撤单受理"，必须重查终态才释槽：所以 cancel 成功后
    probe 必须改口回 canceled，否则槽位停在 PENDING（撤单未确认）。
    """

    def __init__(self, acc_fill='0', cancel_ok=True, probe_status='ok'):
        self.cancelled = []
        self.acc_fill = acc_fill
        self.cancel_ok = cancel_ok
        self.probe_status = probe_status
        self.exchange_canceled = False

    def probe_order(self, inst_id, ord_id):
        if self.probe_status != 'ok':
            return (self.probe_status, {})
        state = 'canceled' if self.exchange_canceled else 'live'
        return ('ok', {'state': state, 'accFillSz': self.acc_fill,
                       'avgPx': '0'})

    def cancel_normal_order(self, inst_id, ord_id):
        self.cancelled.append(ord_id)
        if self.cancel_ok:
            self.exchange_canceled = True
        return self.cancel_ok

    def execute_trade(self, **kw):
        return {'success': True, 'order_id': f"ord-{len(self.cancelled)}-{time.time_ns()}"}

    def set_leverage(self, *a, **kw):
        return True


def new_mgr(acc_fill='0', cancel_ok=True, probe_status='ok'):
    m = M.__new__(M)
    m.executor = FakeExec(acc_fill=acc_fill, cancel_ok=cancel_ok,
                          probe_status=probe_status)
    m._lock = threading.RLock()
    m.state = {}
    m.state_file = None
    m._run_id = ''
    m._verbose = False
    # 合约规格缓存：走 __new__ 不会跑 __init__，缺这个属性会让 _q()/_steps()
    # 直接 AttributeError（合约规格功能上线后本桩未同步）。置 None 即按
    # 兜底步长 FALLBACK_LOT_SZ 取整，不碰网络。
    m.spec_cache = None
    # 账户/环境相关：账本改为按 (account, environment) 分行存储后新增的属性。
    # account=None 表示"纯内存不落库"，风控走本地集合判定；缺这些属性同样会
    # AttributeError（__init__ 不会被 __new__ 调用）。
    m.account = None
    m.environment = '1'
    m._entry_blocked = set()
    m._targets = {}
    m._fill_generation = 0
    m._lev_verified = {}
    m._saved_state = {}
    m._round_place_failures = []
    m._round_divergences = []
    m.manual_pause_seconds = 1800
    m.close_pause_seconds = 60
    # 持久化替身：G3 移除后 _save 失败不再拒发请求，但桩仍返回 True 以保持
    # “落库成功”这一常态路径，让 TTL 用例聚焦撤单逻辑本身。
    m._save = lambda inst_id=None: True
    return m


def place_entry(m, bucket):
    ok = m._place_entry(INST, bucket, 'long', 1.0, 100.0)
    assert ok, '挂单应成功'
    assert m.state[INST][bucket]['slots'][SLOT_ENTRY]['state'] == ST_PENDING
    return m.state[INST][bucket]['slots'][SLOT_ENTRY]


# 场景1：区间仓超龄挂单（存续62分钟 > TTL 60分钟）→ **不撤销**（TTL 只约束趋势仓）
m1 = new_mgr()
pt = place_entry(m1, BUCKET_RANGE)
pt['placed_ts'] = time.time() - 62 * 60
r = m1.poll_fills(INST, ttl_seconds=3600)
assert r['actions'] == [], f"场景1失败：区间仓挂单不应被TTL撤销: {r['actions']}"
assert m1.state[INST][BUCKET_RANGE]['slots'][SLOT_ENTRY]['state'] == ST_PENDING
assert m1.executor.cancelled == []
print('场景1 通过：超龄区间仓挂单不受TTL约束（持久挂单）')

# 场景2：趋势仓未超龄挂单（存续10分钟 < TTL 60分钟）→ 保持
m2 = new_mgr()
pt2 = place_entry(m2, BUCKET_TREND)
pt2['placed_ts'] = time.time() - 10 * 60
r2 = m2.poll_fills(INST, ttl_seconds=3600)
assert r2['actions'] == [], f"场景2失败: {r2['actions']}"
assert m2.state[INST][BUCKET_TREND]['slots'][SLOT_ENTRY]['state'] == ST_PENDING
assert m2.executor.cancelled == []
print('场景2 通过：未超龄挂单保持不动')

# 场景3：ttl_seconds=0（关闭TTL）→ 趋势仓超龄也不撤
m3 = new_mgr()
pt3 = place_entry(m3, BUCKET_TREND)
pt3['placed_ts'] = time.time() - 120 * 60
r3 = m3.poll_fills(INST, ttl_seconds=0)
assert r3['actions'] == []
assert m3.executor.cancelled == []
print('场景3 通过：TTL关闭(0)不撤销')

# 场景4：TTL撤销不动账本（挂单未成交，持仓为0；撤单后仍为0）
m4 = new_mgr()
pt4 = place_entry(m4, BUCKET_TREND)
pt4['placed_ts'] = time.time() - 65 * 60
book_before = m4.get_book(INST, BUCKET_TREND)
m4.poll_fills(INST, ttl_seconds=3600)
assert m4.get_book(INST, BUCKET_TREND) == book_before, '场景4失败：账本被改动'
print('场景4 通过：TTL撤销不改动账本持仓')

# 场景5：趋势仓超龄挂单同样受约束
m5 = new_mgr()
pt5 = place_entry(m5, BUCKET_TREND)
pt5['placed_ts'] = time.time() - 61 * 60
r5 = m5.poll_fills(INST, ttl_seconds=3600)
assert any(a == 'trend.entryTTL撤销' for a in r5['actions']), f"场景5失败: {r5['actions']}"
print('场景5 通过：趋势仓超龄挂单 -> TTL撤销')

# 场景6：部分成交(0.3张)的超龄**趋势仓**挂单 → 已成交部分先入账再TTL撤销
m6 = new_mgr(acc_fill='0.3')
pt6 = place_entry(m6, BUCKET_TREND)
pt6['placed_ts'] = time.time() - 70 * 60
r6 = m6.poll_fills(INST, ttl_seconds=3600)
held_after = m6.get_book(INST, BUCKET_TREND)['held']['long']
assert abs(held_after - 0.3) < 1e-6, f"场景6失败：部分成交未入账({held_after})"
assert m6.state[INST][BUCKET_TREND]['slots'][SLOT_ENTRY]['state'] == ST_IDLE
print('场景6 通过：部分成交先入账再TTL撤销（趋势仓）')

# 场景7：撤单失败且订单状态查询也失败（结果未知）→ 保持 PENDING 下轮再试，不误复位
m7 = new_mgr(cancel_ok=False, probe_status='error')
pt7 = place_entry(m7, BUCKET_TREND)
pt7['placed_ts'] = time.time() - 80 * 60
r7 = m7.poll_fills(INST, ttl_seconds=3600)
assert m7.state[INST][BUCKET_TREND]['slots'][SLOT_ENTRY]['state'] == ST_PENDING, \
    '场景7失败：撤单未确认却复位了槽位'
print('场景7 通过：撤单未确认时保持挂单不误复位')

print('全部冒烟通过')
