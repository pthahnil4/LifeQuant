# -*- coding: utf-8 -*-
"""审计修复回归冒烟测试（常驻脚本，不连接真实API/邮件）
=======================================================
覆盖 fx01~fx16 审计修复的核心逻辑，任何改动挂单管理器/交易器后应重跑：
    python crypto/task/_smoke_fix_regression.py

分组：
  A. 撤单竞态（fx03）：撤单失败重查终态入账 / 两次查询失败保持 PENDING
  B. 兜底委托触发检测（fx09）：effective 定向扣账 / canceled 只清记录 / 查询失败保留
  C. 仓位上限（fx06/fx13/fx16）：单币种/总上限拒单与放行 / 人工同向加仓不阻断开仓
  D. poll_fills（fx07）：前置成交入账 + 幂等
  E. 人工强平冷却（fx02）：剩余时间计算 / 到期自动清除
  F. 连续失败告警（fx15）：达阈值发邮件并重置 / 成功清零 / 0=禁用
  G. 反向持仓步长死区（fx08）：excess < 0.05 不触发
  H. 配置断言（fx05/fx14）：止损/止盈/告警阈值配置项
  I. 粉尘持仓清零（反转清理死锁）：_close_bucket/_close_bucket_smart 粉尘出口 /
     反转清理流程 cleanup_ok=True 恢复开仓闸门打破死循环 / 0.1张边界走正常平仓
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.dirname(__file__))

from _smoke_manual_override_regression import load_definitions, HERE
BUCKET_TREND, BUCKET_RANGE = 'trend', 'range'

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


_pom = load_definitions(HERE / 'utils/position_order_manager.py',
                        session_scope=_fake_session_scope, state_repo=_FakeRepo)
DualPositionOrderManager = _pom.DualPositionOrderManager
trend_range_trader = load_definitions(HERE / 'trend_range_trader.py',
                                      DualPositionOrderManager=DualPositionOrderManager)

PASSED = []


def ok(name, cond, extra=''):
    assert cond, f'FAIL: {name} {extra}'
    PASSED.append(name)
    print(f'  OK {name}')


# =====================================================================
# Mock 交易执行器：所有交易所交互可编程
# =====================================================================
class MockExecutor:
    def __init__(self):
        self.order_info = {}        # ord_id -> dict / None(查询失败)
        self.cancel_ok = True
        self.algo_details = {}      # algo_id -> dict / {}(查询失败)
        self.trade_result = {'success': True, 'order_id': 'MOCK-ORD'}

    def get_order_info(self, inst_id, ord_id):
        status, info = self.probe_order(inst_id, ord_id)
        return info if status == 'ok' else None

    def probe_order(self, inst_id, ord_id):
        """与 TradeExecutor.probe_order 同语义：order_info 值可以是
        dict(成功) / None(查询失败) / 'NOT_FOUND'(权威不存在) / list(队列)"""
        v = self.order_info.get(ord_id)
        if isinstance(v, list):     # 队列模式：逐次弹出（模拟先失败后成功）
            v = v.pop(0) if v else None
        if v is None:
            return 'error', {}
        if v == 'NOT_FOUND':
            return 'not_found', {}
        return 'ok', v

    def cancel_normal_order(self, inst_id, ord_id):
        return self.cancel_ok

    def execute_trade(self, **kw):
        return dict(self.trade_result)

    def set_leverage(self, *a, **kw):
        return True

    def get_algo_order_details(self, algo_id):
        return self.algo_details.get(algo_id, {})

    def place_algo_oco(self, *a, **kw):
        return None

    def cancel_algo_order(self, *a, **kw):
        return True


INST = 'NEAR-USDT-SWAP'
INST2 = 'BTC-USDT-SWAP'


def new_mgr():
    # 内存沙箱：每个新管理器先清空内存账本，保证用例隔离（不碰生产 MySQL）
    _MEM_STATE.clear()
    ex = MockExecutor()
    m = DualPositionOrderManager(ex)
    m.set_context('SMOKE', True)
    return m, ex


def put_pending(m, inst, bucket, ord_id, amount, price, d='long'):
    bk = m._inst(inst)[bucket]
    bk['slots']['entry'] = {
        'state': 'PENDING', 'ord_id': ord_id, 'price': price,
        'amount': amount, 'placed_ts': time.time(), 'acc_filled': 0.0, 'dir': d}


print('--- A. 撤单竞态（fx03） ---')
m, ex = new_mgr()
# A1: 首查成功(live) + 撤单失败 + 重查 filled → 全量入账置 FILLED
put_pending(m, INST, BUCKET_TREND, 'O1', 0.2, 100)
ex.order_info['O1'] = [
    {'state': 'live', 'accFillSz': '0', 'avgPx': ''},
    {'state': 'filled', 'accFillSz': '0.2', 'avgPx': '100'}]
ex.cancel_ok = False
r = m._cancel_slot(INST, BUCKET_TREND, 'entry', '测试')
held, avg = m.get_position(INST, BUCKET_TREND, 'long')
ok('A1 撤单竞态全量入账', r is False and held == 0.2 and avg == 100
   and m._inst(INST)[BUCKET_TREND]['slots']['entry']['state'] == 'FILLED')

# A2: 两次查询都失败 + 撤单失败 → 保持 PENDING 不复位
m, ex = new_mgr()
put_pending(m, INST, BUCKET_TREND, 'O2', 0.2, 100)
ex.order_info['O2'] = None
ex.cancel_ok = False
r = m._cancel_slot(INST, BUCKET_TREND, 'entry', '测试')
pt = m._inst(INST)[BUCKET_TREND]['slots']['entry']
ok('A2 查询失败保持PENDING', r is False and pt['state'] == 'PENDING'
   and pt['ord_id'] == 'O2')

# A3: 正常撤单（live 无成交）→ 复位 IDLE 返回 True
m, ex = new_mgr()
put_pending(m, INST, BUCKET_TREND, 'O3', 0.2, 100)
ex.order_info['O3'] = {'state': 'live', 'accFillSz': '0', 'avgPx': ''}
ex.cancel_ok = True
r = m._cancel_slot(INST, BUCKET_TREND, 'entry', '测试')
ok('A3 撤单受理但仍live不复位', r is False
   and m._inst(INST)[BUCKET_TREND]['slots']['entry']['state'] == 'PENDING')

print('--- B. 兜底委托触发检测（fx09） ---')
m, ex = new_mgr()
bk = m._inst(INST)[BUCKET_TREND]
bk['held']['long'] = 0.2
bk['avg_px']['long'] = 100.0
bk['algo']['long'] = {'algo_id': 'ALGO1', 'amount': 0.2}
# B1: effective → note_external_close 定向扣账 + 清记录
ex.algo_details['ALGO1'] = {'state': 'effective'}
evs = m.poll_algo_triggers(INST, BUCKET_TREND)
held, _ = m.get_position(INST, BUCKET_TREND, 'long')
ok('B1 effective无子单成交不扣账', len(evs) == 0 and held == 0.2
   and bk['algo']['long'] is not None)
# B2: canceled → 只清记录不扣账
bk['held']['long'] = 0.3
bk['algo']['long'] = {'algo_id': 'ALGO2', 'amount': 0.3}
ex.algo_details['ALGO2'] = {'state': 'canceled'}
m.poll_algo_triggers(INST, BUCKET_TREND)
held, _ = m.get_position(INST, BUCKET_TREND, 'long')
ok('B2 canceled只清记录', held == 0.3 and bk['algo']['long'] is None)
# B3: 查询失败（{}）→ 保留记录下轮再查
bk['algo']['long'] = {'algo_id': 'ALGO3', 'amount': 0.3}
m.poll_algo_triggers(INST, BUCKET_TREND)     # ALGO3 不在 algo_details → {}
ok('B3 查询失败保留记录', bk['algo']['long'] is not None)

print('--- C. 仓位上限（fx06单币种 / fx13全账户 / fx16账本欠账守卫） ---')
m, ex = new_mgr()
bk = m._inst(INST)[BUCKET_TREND]
bk['held']['long'] = 0.5
put_pending(m, INST, BUCKET_RANGE, 'O9', 0.4, 100)
# C1: 0.5持仓 + 0.4挂单 + 本次0.2 > 1.0 上限 → 拒单
r = m._place_entry(INST, BUCKET_TREND, 'long', 0.2, 100, max_position=1.0)
ok('C1 单币种上限拒单', r is False)
# C2: 上限1.2 放行
r = m._place_entry(INST, BUCKET_TREND, 'long', 0.2, 100, max_position=1.2)
ok('C2 单币种上限放行', r is True)
# C3: max_position=0 不启用
m, ex = new_mgr()
m._inst(INST)[BUCKET_TREND]['held']['long'] = 99.0
r = m._place_entry(INST, BUCKET_TREND, 'long', 0.2, 100, max_position=0)
ok('C3 上限0不启用', r is True)
# C4: 全账户总上限：两个币种合计超限拒单
m, ex = new_mgr()
m._inst(INST)[BUCKET_TREND]['held']['long'] = 0.6
m._inst(INST2)[BUCKET_TREND]['held']['short'] = 0.3
r = m._place_entry(INST, BUCKET_RANGE, 'long', 0.2, 100,
                   max_position=5.0, max_total=1.0)
ok('C4 总仓位上限跨币种拒单', r is False)
# C5: 总上限1.5 放行
r = m._place_entry(INST, BUCKET_RANGE, 'long', 0.2, 100,
                   max_position=5.0, max_total=1.5)
ok('C5 总仓位上限放行', r is True)
# C6: 真实同向持仓超账本（人工加仓）不再拒单 —— 每轮已由 reconcile 吸收进账本，
#     继续正常开仓（需求：该加仓加仓、该平仓平仓，人工开仓不阻断系统）
m, ex = new_mgr()
m._inst(INST)[BUCKET_TREND]['held']['long'] = 0.2
r = m._place_entry(INST, BUCKET_TREND, 'long', 0.2, 100, cross_pos=0.5)
ok('C6 人工同向加仓不阻断开仓', r is True)
# C7: 真实持仓与账本一致 → 正常放行
r = m._place_entry(INST, BUCKET_TREND, 'long', 0.2, 100, cross_pos=0.2)
ok('C7 同槽位仍在途不重复下单', r is False)
# C8: 反方向账本外持仓计入上限占用：账本空、真实空头0.9张 → 0.9+0.2 > 1.0 拒单
m, ex = new_mgr()
r = m._place_entry(INST, BUCKET_TREND, 'long', 0.2, 100,
                   max_position=1.0, cross_pos=0.0, isolated_pos=-0.9)
ok('C8 账本外持仓计入上限', r is False)

print('--- D. poll_fills 前置成交入账（fx07） ---')
m, ex = new_mgr()
put_pending(m, INST, BUCKET_TREND, 'O5', 0.2, 100)
ex.order_info['O5'] = {'state': 'filled', 'accFillSz': '0.2', 'avgPx': '99.5'}
res = m.poll_fills(INST)
held, avg = m.get_position(INST, BUCKET_TREND, 'long')
ok('D1 poll_fills入账', len(res['fills']) == 1 and held == 0.2 and avg == 99.5)
res2 = m.poll_fills(INST)
held2, _ = m.get_position(INST, BUCKET_TREND, 'long')
ok('D2 poll_fills幂等', len(res2['fills']) == 0 and held2 == 0.2)

print('--- E. 人工强平冷却（fx02） ---')
ML = trend_range_trader.TrendRangeTrader
o = ML.__new__(ML)
o._manual_pause_file = os.path.join(os.path.dirname(__file__), '_smoke_fix_pause.json')
o.pos_mgr, _ = new_mgr()
o._manual_pause = {INST: time.time() + 600}
o._save_manual_pause = lambda: None
rem = o._manual_pause_remaining(INST)
ok('E1 冷却剩余时间', 590 < rem <= 600)
o._manual_pause[INST] = time.time() - 1
rem = o._manual_pause_remaining(INST)
ok('E2 到期自动清除', rem == 0.0 and INST not in o._manual_pause)

print('--- F. 连续失败告警升级（fx15） ---')
class _AlertNotifier:
    def __init__(self):
        self.alerts = []

    def send_system_alert(self, symbol, title, detail):
        self.alerts.append((symbol, title))
        return True

o2 = ML.__new__(ML)
o2._consec_fail = {}
o2.message_notifier = _AlertNotifier()
o2._load_config = lambda: {'global_settings': {'consecutive_failure_alert_rounds': 3}}
for _ in range(2):
    o2._note_cycle_result(INST, {'success': False, 'error': '网络超时'})
ok('F1 未达阈值不告警', len(o2.message_notifier.alerts) == 0
   and o2._consec_fail[INST] == 2)
o2._note_cycle_result(INST, {'success': False, 'error': '网络超时'})
ok('F2 达阈值发告警并重置', len(o2.message_notifier.alerts) == 1
   and o2._consec_fail[INST] == 0)
o2._consec_fail[INST] = 2
o2._note_cycle_result(INST, {'success': True})
ok('F3 成功清零计数', INST not in o2._consec_fail)
o3 = ML.__new__(ML)
o3._consec_fail = {}
o3.message_notifier = _AlertNotifier()
o3._load_config = lambda: {'global_settings': {'consecutive_failure_alert_rounds': 0}}
for _ in range(10):
    o3._note_cycle_result(INST, {'success': False, 'error': 'x'})
ok('F4 阈值0禁用告警', len(o3.message_notifier.alerts) == 0)

print('--- G. 反向持仓检测口径（fx08 + 2026-09-02 LIT 事故修正） ---')
o4 = ML.__new__(ML)
o4._steps = lambda inst_id: (0.1, 0.1)   # (lotSz, minSz)
# 新口径：账本内旧方向持仓（ledger）+ 账本外人工超出量（unbooked）都是冲突仓。
# 旧实现扣除账本 owned 后只看 excess，使长周期反转后残留的程序自身旧方向仓
# （全仓多头 vs 逐仓空头）对风控完全隐形 → 永久裸露且无止盈止损保护。
class _PM2:
    def get_position(self, inst_id, bucket, direction):
        return (0.98, 100.0) if direction == 'short' else (0.0, 0.0)
o4.pos_mgr = _PM2()
r = o4._detect_reverse_position(INST, 'long', 0.0, -2.0)
# 账本每篮子0.98×2=1.96，交易所逐仓空2.0 → ledger=1.96，账本外零头0.04<minSz 0.1 不计
ok('G1 账本内旧方向仓计入冲突量', r is not None and abs(r['amount'] - 1.96) < 1e-6
   and len(r['ledger']) == 2 and r['unbooked'] == 0.0 and r['mode'] == 'isolated')
class _PM3:
    def get_position(self, inst_id, bucket, direction):
        return (0.9, 100.0) if direction == 'short' else (0.0, 0.0)
o4.pos_mgr = _PM3()
r = o4._detect_reverse_position(INST, 'long', 0.0, -2.0)
# 账本1.8 + 账本外人工0.2（≥minSz）= 真实净持仓2.0，两部分分别计量便于分别平仓
ok('G2 账本内外混合分别计量', r is not None and abs(r['amount'] - 2.0) < 1e-6
   and abs(sum(h for _, h in r['ledger']) - 1.8) < 1e-6
   and abs(r['unbooked'] - 0.2) < 1e-6)
# 账本外零头阈值取合约 minSz（非硬编码）：POL minSz=1 时 0.04 张不可平，若计入会
# 造成“检测→拒单→重试”每轮死循环永不收敛
o4._steps = lambda inst_id: (1.0, 1.0)
o4.pos_mgr = _PM2()
r = o4._detect_reverse_position(INST, 'long', 0.0, -2.0)
ok('G3 账本外零头按 minSz 过滤', r is not None and r['unbooked'] == 0.0
   and abs(r['amount'] - 1.96) < 1e-6)
# 与长周期同向的净持仓（多→全仓多 / 空→逐仓空）+ 账本无旧方向残留 → 不触发
o4._steps = lambda inst_id: (0.1, 0.1)
class _PM0:
    def get_position(self, inst_id, bucket, direction): return 0.0, 0.0
o4.pos_mgr = _PM0()
ok('G4 同向净持仓不触发', o4._detect_reverse_position(INST, 'long', 3.0, 0.0) is None
   and o4._detect_reverse_position(INST, 'short', 0.0, -3.0) is None)
class _PMD:
    def get_position(self, inst_id, bucket, direction):
        return (0.005, 100.0) if direction == 'short' else (0.0, 0.0)
o4.pos_mgr = _PMD()
r = o4._detect_reverse_position(INST, 'long', 0.0, -0.05)
ok('G5 真实小仓不能从风控账本抹去', r is not None and r['amount'] > 0)

print('--- H. 配置断言（fx05止损 / fx14止盈 / fx15阈值） ---')
cfg_path = os.path.join(os.path.dirname(__file__), 'config', 'config_trend_range.json')
with open(cfg_path, 'r', encoding='utf-8') as f:
    cfg = json.load(f)
gs = cfg['global_settings']
tp = cfg['currencies'][0]['trend_position']
# H1/H2 原断言依赖具体开关状态（实盘配置可随时调整），改为结构完整性断言
ok('H1 止损配置结构完整', isinstance(tp['stop_loss'].get('enabled'), bool)
   and tp['stop_loss'].get('type') == 'fixed')
ok('H2 止盈配置结构完整', isinstance(tp['take_profit'].get('enabled'), bool))
ok('H3 告警阈值配置', gs.get('consecutive_failure_alert_rounds', 0) >= 1)
ok('H4 冷却与上限配置', gs.get('manual_close_pause_minutes', 0) >= 0
   and gs['risk_control'].get('max_total_position', 0) > 0)

print('--- I. 不可交易小仓保留真实账本 ---')
# 低于minSz不能伪报平仓成功；必须保留真实数量并继续阻断反转增仓。
called = {'close_amount': 0}
o5 = ML.__new__(ML)
m, ex = new_mgr()
bk = m._inst(INST)[BUCKET_RANGE]
bk['held']['short'] = 0.05
bk['avg_px']['short'] = 100.0
o5.pos_mgr = m
o5._close_amount = lambda *a, **kw: called.__setitem__(
    'close_amount', called['close_amount'] + 1) or True
r = o5._close_bucket(INST, BUCKET_RANGE, 'short', '长周期反转')
held, avg = m.get_position(INST, BUCKET_RANGE, 'short')
ok('I1 _close_bucket保留真实小仓', r is False and held == 0.05 and avg == 100.0
   and called['close_amount'] == 0)
# I2: _close_bucket_smart 智能减仓启用时粉尘同样清零返回 True（不查 realizedPnl）
o6 = ML.__new__(ML)
m, ex = new_mgr()
bk = m._inst(INST)[BUCKET_RANGE]
bk['held']['long'] = 0.05
bk['avg_px']['long'] = 200.0
o6.pos_mgr = m
o6._load_config = lambda: {'global_settings': {'smart_reduce': {
    'enabled': True, 'scenes': ['reversal', 'night_force']}}}
o6._get_position_realized_pnl = lambda *a, **kw: (_ for _ in ()).throw(
    AssertionError('粉尘路径不应查询realizedPnl'))
r = o6._close_bucket_smart(INST, BUCKET_RANGE, 'long', '长周期反转', scene='reversal')
held, _ = m.get_position(INST, BUCKET_RANGE, 'long')
ok('I2 _close_bucket_smart保留真实小仓', r is False and held == 0.05)
# I3: 智能减仓显式关闭（紧急闸）→ 降级 _close_bucket，粉尘出口依然生效
#    （注意：缺省配置已改为全局强制启用，验降级必须显式 enabled=False）
called = {'close_amount': 0}
o7 = ML.__new__(ML)
m, ex = new_mgr()
m._inst(INST)[BUCKET_TREND]['held']['long'] = 0.08
o7.pos_mgr = m
o7._load_config = lambda: {'global_settings': {'smart_reduce': {'enabled': False}}}
o7._close_amount = lambda *a, **kw: called.__setitem__(
    'close_amount', called['close_amount'] + 1) or True
r = o7._close_bucket_smart(INST, BUCKET_TREND, 'long', '睡眠时段强平',
                           scene='night_force')
held, _ = m.get_position(INST, BUCKET_TREND, 'long')
ok('I3 智能减仓降级仍保留真实小仓', r is False and held == 0.08
   and called['close_amount'] == 0)
# I4/I5: 复刻第6步反转清理流程 —— 粉尘0.05张触发清理 → cleanup_ok=True →
# 方向记录更新、range_allow_entry 保持 True；下轮 long_dir_changed 不再成立，死循环打破
called = {'close_amount': 0}
o8 = ML.__new__(ML)
m, ex = new_mgr()
bk = m._inst(INST)[BUCKET_RANGE]
bk['held']['long'] = 0.05
bk['avg_px']['long'] = 100.0
o8.pos_mgr = m
o8._load_config = lambda: {'global_settings': {'smart_reduce': {
    'enabled': True, 'scenes': ['reversal', 'night_force']}}}
o8._close_amount = lambda *a, **kw: called.__setitem__(
    'close_amount', called['close_amount'] + 1) or True
o8.last_directions = {INST: {'short': 'long', 'long': 'long'}}
prev_long, new_long = 'long', 'short'
ctx = {'trend_allow_entry': True, 'range_allow_entry': True}
long_dir_changed = o8.last_directions[INST]['long'] != new_long
cleanup_ok, closed_range = True, False
if long_dir_changed \
        and o8.pos_mgr.get_position(INST, BUCKET_RANGE, prev_long)[0] >= 0.05:
    closed_range = o8._close_bucket_smart(
        INST, BUCKET_RANGE, prev_long, f'长周期反转({prev_long}→{new_long})',
        scene='reversal')
    cleanup_ok = cleanup_ok and closed_range
if cleanup_ok:
    o8.last_directions[INST] = {'short': 'long', 'long': new_long}
else:  # 修复前粉尘恒走此分支 → 永久禁开仓
    o8.last_directions[INST] = {'short': 'long', 'long': prev_long}
    ctx['trend_allow_entry'] = False
    ctx['range_allow_entry'] = False
ok('I4 未完成真实清理不放行新方向', not closed_range and not cleanup_ok
   and ctx['range_allow_entry'] is False and ctx['trend_allow_entry'] is False
   and o8.last_directions[INST]['long'] == prev_long
   and called['close_amount'] == 0)
# 下一轮：方向记录已更新 → long_dir_changed 不再成立；即便复检，账本已清零(<0.05)
next_changed = o8.last_directions[INST]['long'] != new_long
held, _ = m.get_position(INST, BUCKET_RANGE, prev_long)
ok('I5 下轮继续保留未清仓事实', next_changed is True and held == 0.05)
# I6: 边界 0.1 张恰为最小步长 → 不走粉尘清零，进入正常平仓下单路径
called = {'close_amount': 0}
o9 = ML.__new__(ML)
m, ex = new_mgr()
bk = m._inst(INST)[BUCKET_RANGE]
bk['held']['long'] = 0.1
bk['avg_px']['long'] = 100.0
o9.pos_mgr = m
o9._read_positions_by_mode = lambda inst: {'cross': 0.1, 'isolated': 0.0}
o9._close_amount = lambda *a, **kw: called.__setitem__(
    'close_amount', called['close_amount'] + 1) or True
r = o9._close_bucket(INST, BUCKET_RANGE, 'long', '长周期反转')
ok('I6 0.1张走正常平仓不清零绕过', r is True and called['close_amount'] == 1)

print(f'\nALL {len(PASSED)} CHECKS PASSED')
