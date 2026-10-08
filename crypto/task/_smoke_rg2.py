# -*- coding: utf-8 -*-
"""临时冒烟测试：反向持仓风控强平成功/失败分支（不连接真实API/邮件）。"""
import sys, os, time
sys.path.insert(0, 'd:/python/cryptoTrade')
sys.path.insert(0, 'd:/python/cryptoTrade/crypto/task')
import trend_range_trader
ML = trend_range_trader.TrendRangeTrader
o = ML.__new__(ML)

o._reverse_guard_file = os.path.join(os.path.dirname(__file__), '_smoke_rg2_state.json')
if os.path.exists(o._reverse_guard_file):
    os.remove(o._reverse_guard_file)
o._reverse_guard = {}
o._load_config = lambda: {'global_settings': {'reverse_position_guard': {'enabled': True, 'grace_minutes': 10}}}
# 计时器持久化已迁 MySQL（state_repo），冒烟测试必须打桩，否则会真写生产库
o._save_reverse_guard = lambda: None
o._load_reverse_guard = lambda: {}
# 合约规格缓存不初始化，打桩为 (lotSz, minSz)=(0.1, 0.1)
o._steps = lambda inst_id: (0.1, 0.1)

class _Notifier:
    def __init__(self):
        self.warn = 0; self.closed = 0; self.src = []
    # 观察模式分支按位置传参、实盘分支按关键字传参，桩两种都接得住
    def send_reverse_position_warning(self, *a, **k):
        self.warn += 1; self.src.append(k.get('source_note', '')); return True
    def send_reverse_position_closed(self, **k): self.closed += 1; return True
o.message_notifier = _Notifier()

# 双仓位账本：_ledger[(bucket, dir)] = 持仓张数，模拟“程序自身旧方向残留仓”
_ledger = {}
class _PosMgr:
    def get_position(self, inst_id, bucket, direction):
        return float(_ledger.get((bucket, direction), 0.0)), 0.0
o.pos_mgr = _PosMgr()

# 强平成功/失败可切换：账本外走 _close_amount（定量 reduce-only），账本内走
# _close_bucket_smart（scene='reverse_guard' 不在白名单 → 退化全量市价平并扣账本）
_close_ok = {'v': True, 'bucket': True}
_calls = {'iso': 0, 'cross': 0, 'bucket': []}
def _close_amount(inst_id, direction, amount, **k):
    # 约定：多头走全仓(cross)、空头走逐仓(isolated)
    _calls['iso' if direction == 'short' else 'cross'] += 1
    return _close_ok['v']
def _close_bucket_smart(inst_id, bucket, direction, reason='', run_id='',
                        short_period='', long_period='', price=0.0,
                        leverage=0.0, scene=''):
    _calls['bucket'].append((bucket, direction, round(amount_of(bucket, direction), 4), scene))
    return _close_ok['bucket']
def amount_of(bucket, direction):
    return float(_ledger.get((bucket, direction), 0.0))
o._close_amount = _close_amount
o._close_bucket_smart = _close_bucket_smart

TREND = trend_range_trader.BUCKET_TREND
RANGE = trend_range_trader.BUCKET_RANGE

INST = 'NEAR-USDT-SWAP'
def guard(long_dir, cross, iso, changed=False, observe=False):
    return o._check_reverse_position_guard(INST, long_dir, cross, iso, '5m', '4H',
                                           5.0, changed, 'RID', observe_only=observe)
def reset():
    o._reverse_guard = {}
    o.message_notifier = _Notifier()      # 邮件计数按场景隔离
    _ledger.clear()
    _calls['iso'] = _calls['cross'] = 0
    _calls['bucket'] = []
    _close_ok['v'] = _close_ok['bucket'] = True

reset()
print('--- A. 强平失败：保留计时器，不发已强平邮件，不置 closed ---')
guard('long', 0.0, -2.0)                                   # 首次检测→预警
assert o.message_notifier.warn == 1
o._reverse_guard[INST]['detected_ts'] = time.time() - 11 * 60  # 伪造超时
_close_ok['v'] = False
r = guard('long', 0.0, -2.0)
assert r.get('closed') is not True, r
assert _calls['iso'] == 1
assert o.message_notifier.closed == 0, '强平失败却发了已强平邮件'
assert INST in o._reverse_guard, '强平失败却解除了计时器'
ts_before = o._reverse_guard[INST]['detected_ts']
print('  OK 失败保留计时器, closed=0, warn=1')

print('--- B. 下一轮仍超时→立即重试(不重置倒计时)，这次成功 ---')
_close_ok['v'] = True
r = guard('long', 0.0, -2.0)
assert r.get('closed') is True and r.get('mode') == 'isolated', r
assert _calls['iso'] == 2, _calls
assert o.message_notifier.closed == 1
assert INST not in o._reverse_guard
# 重试期间未产生新的预警邮件（warn 仍为1）
assert o.message_notifier.warn == 1, o.message_notifier.warn
print('  OK 重试成功强平, closed=1, warn仍=1(未重置倒计时/未重复预警)')

reset()
print('--- C. cross 反向(长空+全仓多)强平成功 ---')
guard('short', 3.0, 0.0)
o._reverse_guard[INST]['detected_ts'] = time.time() - 11 * 60
r = guard('short', 3.0, 0.0)
assert r.get('closed') is True and r.get('mode') == 'cross', r
assert _calls['cross'] == 1
print('  OK cross 强平成功 closed=cross')

# ============================================================
# 2026-09-02 LIT 事故回归：账本内“程序自身旧方向残留仓”也必须被风控覆盖
# （旧实现扣除账本 owned 后 excess=0 → 永久漏检）
# ============================================================
reset()
print('--- D. 账本内旧方向多头(LIT 场景)：长周期转空→预警→超时按篮子强平 ---')
_ledger[(TREND, 'long')] = 2.0                      # 全仓多头，账本已记
r = guard('short', 2.0, 0.0)                        # 长周期已转空
assert r.get('closed') is not True and o.message_notifier.warn == 1, r
assert '程序旧方向持仓' in (o.message_notifier.src[-1] or ''), o.message_notifier.src
assert '2' in r['text'], r['text']
o._reverse_guard[INST]['detected_ts'] = time.time() - 11 * 60
r = guard('short', 2.0, 0.0)
assert r.get('closed') is True and r.get('mode') == 'cross', r
assert _calls['bucket'] == [(TREND, 'long', 2.0, 'reverse_guard')], _calls['bucket']
assert _calls['cross'] == 0, '账本内持仓不应再走 _close_amount 超量下单'
assert o.message_notifier.closed == 1
assert INST not in o._reverse_guard
print('  OK 账本内旧方向仓已按篮子强平(扣账本), 未超量走定量平仓')

reset()
print('--- E. 账本内+账本外混合：篮子平仓与定量平仓各按份额，合计不超真实持仓 ---')
_ledger[(TREND, 'long')] = 2.0
_ledger[(RANGE, 'long')] = 0.5
r = guard('short', 4.0, 0.0)                        # 真实全仓多 4.0 = 账本 2.5 + 人工 1.5
assert '程序旧方向' in (o.message_notifier.src[-1] or '') \
    and '账本外人工' in (o.message_notifier.src[-1] or ''), o.message_notifier.src
o._reverse_guard[INST]['detected_ts'] = time.time() - 11 * 60
r = guard('short', 4.0, 0.0)
assert r.get('closed') is True and r.get('mode') == 'cross', r
assert _calls['bucket'] == [(TREND, 'long', 2.0, 'reverse_guard'),
                            (RANGE, 'long', 0.5, 'reverse_guard')], _calls['bucket']
assert _calls['cross'] == 1, '账本外人工单未定量强平'
print('  OK 混合来源分别强平（账本2.5张按篮子 + 账本外1.5张定量）')

reset()
print('--- F. 混合来源强平失败：任一部分失败即保留计时器重试，不误报已强平 ---')
_ledger[(TREND, 'long')] = 2.0
_close_ok['bucket'] = False
guard('short', 3.0, 0.0)
o._reverse_guard[INST]['detected_ts'] = time.time() - 11 * 60
r = guard('short', 3.0, 0.0)
assert r.get('closed') is not True, r
assert INST in o._reverse_guard, '强平失败却解除了计时器'
assert o.message_notifier.closed == 0, '强平失败却发了已强平邮件'
_close_ok['bucket'] = True
r = guard('short', 3.0, 0.0)                        # 下轮立即重试
assert r.get('closed') is True, r
print('  OK 篮子强平失败保留计时器, 下轮重试成功')

reset()
print('--- G. 观察模式：账本内旧方向仓只预警不强平 ---')
_ledger[(TREND, 'long')] = 2.0
r = guard('short', 2.0, 0.0, observe=True)
assert r.get('closed') is not True and _calls['bucket'] == [], r
assert trend_range_trader.WATCH_MODE_NOTICE in r['text'], r['text']
assert o.message_notifier.warn == 1, '观察模式仍须发预警留痕'
# 观察模式不创建实盘强平倒计时（否则会给“到期强平”留下钩子）
assert INST not in o._reverse_guard, o._reverse_guard
# 再跑一轮：仍只预警、不建计时器、不强平（不会因轮数推进而升级为强平）
r = guard('short', 2.0, 0.0, observe=True)
assert INST not in o._reverse_guard and _calls['bucket'] == [], r
print('  OK 观察模式不执行强平、不推进强平计时器')

reset()
print('--- H. 粉尘：账本外不足 minSz 且无账本持仓 → 不触发 ---')
assert guard('short', 0.05, 0.0)['text'] == '无反向持仓'
assert o.message_notifier.warn == 0
_ledger[(TREND, 'long')] = 0.005                    # 不足 minSz 的真实残渣
guard('short', 0.05, 0.0)
assert o.message_notifier.warn == 1, '低于 minSz 的真实仓位仍算冲突仓（粉尘阈值只忽略浮点残差）'
assert _calls['bucket'] == [], '首检只预警，不立即强平'
print('  OK 账本外零头不足 minSz 不触发，账本内真实残渣不被当成浮点误差抹掉')

reset()
print('--- H2. 真·浮点残渣（<POS_DUST）不当冲突仓，避免检测→拒单→重试死循环 ---')
_ledger[(TREND, 'long')] = 1e-12
guard('short', 0.05, 0.0)
assert o.message_notifier.warn == 0, '浮点残渣被误判为冲突仓'
print('  OK 浮点残渣不触发')

reset()
print('--- I. 方向反转轮跳过检测（旧方向仓本轮由步骤6清理，避免重复下单） ---')
_ledger[(TREND, 'long')] = 2.0
r = guard('short', 2.0, 0.0, changed=True)
assert '跳过' in r['text'] and _calls['bucket'] == [], r
print('  OK 反转轮跳过')

reset()
print('--- J. 与长周期同向持仓不误伤（长空+全仓多合法，逐仓空不动） ---')
_ledger[(TREND, 'short')] = 3.0
r = guard('short', 0.0, -3.0)
assert r['text'] == '无反向持仓', r
assert _calls['iso'] == 0 and _calls['bucket'] == []
print('  OK 同向持仓不触发风控')

if os.path.exists(o._reverse_guard_file):
    os.remove(o._reverse_guard_file)
print('\nALL SMOKE TESTS PASSED')
