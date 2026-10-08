# -*- coding: utf-8 -*-
"""
🔒 纯离线冒烟：双仓位策略·风控告警（P0 全项 + P1 盈亏双向/账户余额保证金/调度轮存活）
================================================
覆盖（全部不连 DB / 不连邮件 / 不连交易所 API）：
- 组1 DualPositionOrderManager：下单失败暂存 _note_place_failure /
  drain_place_failures（need_verify 透传、排空后清空）/ set_context 每轮复位。
- 组2 TrendRangeTrader._handle_place_failures：
  · P0-1 普通拒单连续 N 轮才升级告警（阈值可配），干净轮复位计数；
  · P0-3 need_verify 孤儿单首轮即发"请人工核实"，且不推进被拒计数；
  · enabled=False 时不发信但仍排空。
- 组3 TrendRangeTrader._alert_sl_tp_fail：
  · P0-2 止盈/止损平仓失败连续 N 轮升级（阈值可配）。
- 组4 alert_monitor 纯函数：
  · _position_liq_info 距离/方向计算与数据缺失回退；
  · _classify_liq_level 分级；
  · _transition 强平距离复用级别状态机（send/suppress/升级/降级/recover）。
- 组5 P1 盈亏双向（alert_monitor 纯函数）：
  · _classify_profit_level 正阈值分级 + profit_enabled/零值兜底；
  · 浮亏与浮盈按符号互斥（同一 pnl_pct 不同时命中两侧）；
  · 盈利侧独立状态机键位 profit|inst|dir，与亏损键 pnl|inst|dir 互不干扰。
- 组6 P1 账户余额/保证金率（alert_monitor 纯函数）：
  · _classify_margin_level 越近100%越危险、None 不命中；
  · _classify_avail_level 越少越危险、阈值≤0 关闭；
  · 保证金率/余额各用独立状态机键位 acct|margin|…/acct|avail|…。
- 组7 P1 调度轮存活（liveness）：
  · _liveness_thresholds 间隔×倍数与绝对下限取大、严重不低于预警；
  · _classify_liveness_level 线程死亡/停滞/停止/未出轮分级；
  · 独立键位 liveness；TaskScheduler.get_loop_liveness 裸桩只读、绝不抛。
- 组8 BOLL 出轨分级器（用户选跳过接线，保留分级器备用，纯函数）：
  · _classify_boll_level 越界幅度/band 分级、逆势破轨升 critical、轨内不命中；独立键位 boll|inst。
- 组9 P1 插针临时禁开仓（spike_guard，行为开关默认关闭）：
  · _classify_spike 单轮环比判定（无基准/间隔过小/未达阈值→不判，宁漏不误伤）；
  · _note_spike_guard 开关×发信×暂停×延长；_spike_pause_remaining 到期自动清除。

运行：python crypto/task/_smoke_risk_alerts.py   （须全绿）
"""
import sys
import os
import time
import threading

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, 'd:/python/cryptoTrade')
sys.path.insert(0, os.path.join('d:/python/cryptoTrade', 'crypto', 'task'))

import trend_range_trader                                   # noqa: E402
from utils.position_order_manager import (                  # noqa: E402
    DualPositionOrderManager, BUCKET_TREND, BUCKET_RANGE)
from crypto.task.monitor import alert_monitor as am         # noqa: E402

TREND = BUCKET_TREND
RANGE = BUCKET_RANGE
INST = 'NEAR-USDT-SWAP'

PASS, FAIL = [], []


def ck(name, cond):
    (PASS if cond else FAIL).append(name)
    print(('  ✓ ' if cond else '  ✗ ') + name)


# =============================================================================
# 组1 DualPositionOrderManager：下单失败暂存 / 排空 / 复位
# =============================================================================
print('--- 组1 DualPositionOrderManager._note_place_failure / drain / set_context ---')
m = DualPositionOrderManager.__new__(DualPositionOrderManager)
m._lock = threading.RLock()
m._round_place_failures = []
m.state = {}
m._run_id = ''
m._verbose = True
# V2 账本上线后 set_context 会清这几个容器（__new__ 桩不跑 __init__，
# 缺任何一个都会在 set_context 里 AttributeError，本组后续用例全部跑不到）
m._round_divergences = []
m._entry_blocked = set()
m._targets = {}

m._note_place_failure(INST, TREND, 'entry', 2.0, 100.5, '51008 保证金不足')
m._note_place_failure(INST, RANGE, 'exit', 1.0, 99.0, '超时', need_verify=True)
fails = m.drain_place_failures()
ck('排空返回2笔', len(fails) == 2)
ck('首笔为普通拒单', fails[0]['need_verify'] is False and fails[0]['error'] == '51008 保证金不足')
ck('次笔 need_verify 透传', fails[1]['need_verify'] is True)
ck('篮子/槽位记录正确',
   fails[0]['bucket'] == TREND and fails[0]['slot'] == 'entry'
   and fails[1]['bucket'] == RANGE and fails[1]['slot'] == 'exit')
ck('数值转 float', isinstance(fails[0]['amount'], float) and fails[0]['price'] == 100.5)
ck('排空后为空', m.drain_place_failures() == [])

m._note_place_failure(INST, TREND, 'entry', 1.0, 1.0, 'x')
m.set_context('RID-2')                        # 新一轮上下文 → 复位暂存
ck('set_context 复位暂存', m._round_place_failures == [])


# =============================================================================
# 组2 _handle_place_failures（P0-1 连续拒单 / P0-3 孤儿单 / enabled 开关）
# =============================================================================
class _Notifier:
    def __init__(self):
        self.alerts = []                       # [(title, level)]
        self.sys_alerts = []                   # [{'symbol','title'}]（P2-d 聚合系统邮件）

    def send_risk_alert(self, inst_id, title, detail_rows=None, level='critical',
                        cooldown_key=None):
        self.alerts.append((title, level))
        return True

    def send_system_alert(self, symbol, title, detail):
        self.sys_alerts.append({'symbol': symbol, 'title': title})
        return True


class _FakePosMgr:
    def __init__(self):
        self.queue = []

    def drain_place_failures(self):
        out = self.queue
        self.queue = []
        return out


def _mk_fail(need_verify=False, bucket=TREND, slot='entry', err='51008 保证金不足'):
    return {'inst_id': INST, 'bucket': bucket, 'slot': slot, 'amount': 1.0,
            'price': 100.0, 'error': err, 'need_verify': need_verify}


def _new_trader(risk_alerts):
    o = trend_range_trader.TrendRangeTrader.__new__(trend_range_trader.TrendRangeTrader)
    o._place_fail_streak = {}
    o._sltp_fail_streak = {}
    o.pos_mgr = _FakePosMgr()
    o.message_notifier = _Notifier()
    o._load_config = lambda: {'global_settings': {'risk_alerts': risk_alerts}}
    return o


print('--- 组2a P0-1 连续拒单：阈值3轮才发一封，干净轮复位 ---')
o = _new_trader({'enabled': True, 'place_fail_rounds': 3, 'sl_tp_fail_rounds': 2})
for rnd in range(1, 3):
    o.pos_mgr.queue = [_mk_fail()]
    o._handle_place_failures(INST, f'R{rnd}')
    ck(f'第{rnd}轮不发信', len(o.message_notifier.alerts) == 0)
o.pos_mgr.queue = [_mk_fail()]
o._handle_place_failures(INST, 'R3')
ck('第3轮升级发信', len(o.message_notifier.alerts) == 1
   and '下单连续被拒' in o.message_notifier.alerts[0][0])
ck('连续计数=3', o._place_fail_streak.get(INST) == 3)
# 干净轮复位
o.pos_mgr.queue = []
o._handle_place_failures(INST, 'R4')
ck('干净轮复位计数', INST not in o._place_fail_streak)
ck('干净轮不新增邮件', len(o.message_notifier.alerts) == 1)
# 再连续3轮 → 第二封
for rnd in range(5, 8):
    o.pos_mgr.queue = [_mk_fail()]
    o._handle_place_failures(INST, f'R{rnd}')
ck('再满3轮发第二封', len(o.message_notifier.alerts) == 2
   and '已3轮' in o.message_notifier.alerts[1][0])

print('--- 组2b P0-3 need_verify 孤儿单：首轮即发，不推进被拒计数 ---')
o = _new_trader({'enabled': True, 'place_fail_rounds': 3, 'sl_tp_fail_rounds': 2})
o.pos_mgr.queue = [_mk_fail(need_verify=True, err='unknown 结果待核实')]
o._handle_place_failures(INST, 'RV')
ck('首轮即发核实信', len(o.message_notifier.alerts) == 1
   and '孤儿单' in o.message_notifier.alerts[0][0])
ck('need_verify 不计入被拒streak', INST not in o._place_fail_streak)

print('--- 组2c enabled=False：不发信但仍排空 ---')
o = _new_trader({'enabled': False, 'place_fail_rounds': 1, 'sl_tp_fail_rounds': 1})
o.pos_mgr.queue = [_mk_fail(), _mk_fail(need_verify=True)]
o._handle_place_failures(INST, 'ROFF')
ck('关闭时零邮件', len(o.message_notifier.alerts) == 0)
ck('关闭时仍排空队列', o.pos_mgr.queue == [])


# =============================================================================
# 组3 _alert_sl_tp_fail（P0-2 止盈/止损平仓失败升级）
# =============================================================================
print('--- 组3 P0-2 平仓失败：阈值2轮，第1轮不发第2轮发 ---')
o = _new_trader({'enabled': True, 'place_fail_rounds': 3, 'sl_tp_fail_rounds': 2})
o._alert_sl_tp_fail(INST, '止损', '多头', '固定止损', 2.0, 100.0, 88.0, -12.0, 'R1')
ck('第1轮不发信', len(o.message_notifier.alerts) == 0)
ck('streak=1', o._sltp_fail_streak.get(INST) == 1)
o._alert_sl_tp_fail(INST, '止损', '多头', '固定止损', 2.0, 100.0, 87.0, -13.0, 'R2')
ck('第2轮升级发信', len(o.message_notifier.alerts) == 1
   and '平仓未成功' in o.message_notifier.alerts[0][0]
   and '止损' in o.message_notifier.alerts[0][0])
ck('streak=2', o._sltp_fail_streak.get(INST) == 2)


# =============================================================================
# 组4 alert_monitor 纯函数：强平距离计算 / 分级 / 级别状态机
# =============================================================================
print('--- 组4a _position_liq_info ---')
r = am._position_liq_info({'liqPx': '49000', 'markPx': '50000', 'posSide': 'net', 'pos': '2'})
ck('多头距离2%', r is not None and abs(r[0] - 2.0) < 1e-9 and r[3] == '多头')
ck('返回强平价/标记价', abs(r[1] - 49000) < 1e-6 and abs(r[2] - 50000) < 1e-6)
r2 = am._position_liq_info({'liqPx': '51000', 'markPx': '50000', 'posSide': 'net', 'pos': '-2'})
ck('净持仓负量判空头', r2 is not None and r2[3] == '空头' and abs(r2[0] - 2.0) < 1e-9)
r3 = am._position_liq_info({'markPx': '50000', 'posSide': 'long', 'pos': '2'})
ck('无强平价返回None', r3 is None)
r4 = am._position_liq_info({'liqPx': '49000', 'last': '50000', 'posSide': 'long', 'pos': '1'})
ck('标记价缺失回退last', r4 is not None and abs(r4[2] - 50000) < 1e-6)

print('--- 组4b _classify_liq_level ---')
liq_cfg = {'critical_pct': 2.0, 'warning_pct': 5.0}
ck('距离2%→critical', am._classify_liq_level(2.0, liq_cfg) == 'critical')
ck('距离3%→warning', am._classify_liq_level(3.0, liq_cfg) == 'warning')
ck('距离8%→不命中', am._classify_liq_level(8.0, liq_cfg) == '')

print('--- 组4c _transition（强平距离复用级别状态机）---')
st = {}
key = f'liq|{INST}|多头'
ck('新命中warning→send', am._transition(st, key, 'warning') == ('send', 'warning'))
ck('同级重复→suppress', am._transition(st, key, 'warning') == ('suppress', 'warning'))
ck('升级critical→send', am._transition(st, key, 'critical') == ('send', 'critical'))
ck('降级warning→suppress', am._transition(st, key, 'warning') == ('suppress', 'warning'))
ck('回落→recover', am._transition(st, key, '') == ('recover', 'recover'))
ck('recover后清除状态', key not in st)
ck('清除后再回落→none', am._transition(st, key, '') == ('none', ''))


# =============================================================================
# 组5 P1 盈亏双向：盈利侧分级 / 与浮亏互斥 / 独立状态机键位
# =============================================================================
print('--- 组5a _classify_profit_level（正阈值）---')
pcfg = {'warning_pct': -10.0, 'critical_pct': -20.0,
        'profit_enabled': True, 'profit_warning_pct': 10.0, 'profit_critical_pct': 20.0}
ck('盈利25%→critical', am._classify_profit_level(25.0, pcfg) == 'critical')
ck('盈利15%→warning', am._classify_profit_level(15.0, pcfg) == 'warning')
ck('盈利5%→不命中', am._classify_profit_level(5.0, pcfg) == '')
ck('亏损不触发盈利侧', am._classify_profit_level(-15.0, pcfg) == '')
ck('profit_enabled=False→关闭', am._classify_profit_level(99.0, {**pcfg, 'profit_enabled': False}) == '')
ck('阈值为0视为未配置', am._classify_profit_level(99.0, {**pcfg, 'profit_warning_pct': 0, 'profit_critical_pct': 0}) == '')

print('--- 组5b 盈亏互斥：同一 pnl_pct 不会同时命中亏损与盈利侧 ---')
ck('浮亏-15%：亏损侧warning、盈利侧空', am._classify_pnl_level(-15.0, pcfg) == 'warning'
   and am._classify_profit_level(-15.0, pcfg) == '')
ck('浮盈+15%：盈利侧warning、亏损侧空', am._classify_profit_level(15.0, pcfg) == 'warning'
   and am._classify_pnl_level(15.0, pcfg) == '')

print('--- 组5c 盈利侧独立状态机键位（与亏损键互不干扰）---')
st = {}
lk, pk = f'pnl|{INST}|多头', f'profit|{INST}|多头'
ck('亏损命中→send', am._transition(st, lk, 'warning') == ('send', 'warning'))
ck('盈利命中→独立send(不受亏损键影响)', am._transition(st, pk, 'warning') == ('send', 'warning'))
ck('盈利同级→suppress', am._transition(st, pk, 'warning') == ('suppress', 'warning'))
ck('盈利升级critical→send', am._transition(st, pk, 'critical') == ('send', 'critical'))
ck('盈利回落→recover', am._transition(st, pk, '') == ('recover', 'recover'))
ck('盈利recover仅清除盈利键', pk not in st and lk in st)

print('--- 组6a _classify_margin_level（越接近100%越危险）---')
acfg = {'margin_warning_pct': 200.0, 'margin_critical_pct': 120.0,
        'min_avail_warning': 100.0, 'min_avail_critical': 20.0}
ck('保证金率None→不命中', am._classify_margin_level(None, acfg) == '')
ck('100%→critical', am._classify_margin_level(100.0, acfg) == 'critical')
ck('150%→warning', am._classify_margin_level(150.0, acfg) == 'warning')
ck('300%→健康不命中', am._classify_margin_level(300.0, acfg) == '')
print('--- 组6b _classify_avail_level（越少越危险，阈值≤0关闭）---')
ck('余额None→不命中', am._classify_avail_level(None, acfg) == '')
ck('5U→critical', am._classify_avail_level(5.0, acfg) == 'critical')
ck('50U→warning', am._classify_avail_level(50.0, acfg) == 'warning')
ck('500U→不命中', am._classify_avail_level(500.0, acfg) == '')
ck('预警阈值0→关闭余额检查', am._classify_avail_level(1.0, {**acfg, 'min_avail_warning': 0}) == '')
print('--- 组6c 保证金率/余额各用独立状态机键位 ---')
st2 = {}
mk, ak = f'acct|margin|{INST}', f'acct|avail|{INST}'
ck('保证金率命中→send', am._transition(st2, mk, 'critical') == ('send', 'critical'))
ck('余额命中→独立send', am._transition(st2, ak, 'warning') == ('send', 'warning'))
ck('保证金率同级→suppress', am._transition(st2, mk, 'critical') == ('suppress', 'critical'))
ck('保证金率缓解→recover', am._transition(st2, mk, '') == ('recover', 'recover'))
ck('缓解仅清除保证金率键', mk not in st2 and ak in st2)

print('--- 组7a _liveness_thresholds（间隔×倍数 与 绝对下限取大）---')
lcfg = {'enabled': True, 'grace_multiplier': 5,
        'min_stale_seconds': 900, 'critical_stale_seconds': 2400}
ck('interval=60→预警900/严重2400', am._liveness_thresholds(lcfg, 60) == (900.0, 2400.0))
ck('interval=300→预警抬到1500', am._liveness_thresholds(lcfg, 300) == (1500.0, 2400.0))
ck('绝对下限超大时严重不低于预警',
   am._liveness_thresholds({**lcfg, 'min_stale_seconds': 3000}, 60) == (3000.0, 3000.0))

print('--- 组7b _classify_liveness_level（线程死亡/轮次停滞/停止/未出轮）---')
ck('running=False→不判停滞', am._classify_liveness_level(False, None, False, lcfg, 60) == '')
ck('期望在跑但线程死→critical', am._classify_liveness_level(False, None, True, lcfg, 60) == 'critical')
ck('线程活但无轮次(lag None)→不命中', am._classify_liveness_level(True, None, True, lcfg, 60) == '')
ck('lag=899<预警→不命中', am._classify_liveness_level(True, 899, True, lcfg, 60) == '')
ck('lag=950→warning', am._classify_liveness_level(True, 950, True, lcfg, 60) == 'warning')
ck('lag=2500→critical', am._classify_liveness_level(True, 2500, True, lcfg, 60) == 'critical')
ck('interval=300时lag=1000<1500→不命中', am._classify_liveness_level(True, 1000, True, lcfg, 300) == '')
ck('enabled=False→一律不命中', am._classify_liveness_level(False, None, True, {**lcfg, 'enabled': False}, 60) == '')

print('--- 组7c liveness 独立状态机键位（单键 send/升级/suppress/recover）---')
st3 = {}
ck('liveness 首次→send', am._transition(st3, 'liveness', 'warning') == ('send', 'warning'))
ck('liveness 升级critical→send', am._transition(st3, 'liveness', 'critical') == ('send', 'critical'))
ck('liveness 同级→suppress', am._transition(st3, 'liveness', 'critical') == ('suppress', 'critical'))
ck('liveness 恢复→recover', am._transition(st3, 'liveness', '') == ('recover', 'recover'))
ck('liveness 恢复后清键', 'liveness' not in st3)

print('--- 组7d TaskScheduler.get_loop_liveness（裸桩只读、绝不抛）---')
try:
    from crypto.task.scheduler import TaskScheduler
    _sch = TaskScheduler.__new__(TaskScheduler)
    _sch._trading_running = False
    _sch._trading_thread = None
    _sch._trading_scheduler = None
    _lv = _sch.get_loop_liveness()
    ck('未启动→running False', _lv['running'] is False)
    ck('未启动→thread_alive False', _lv['thread_alive'] is False)
    ck('未启动→last_batch None', _lv['last_batch_ts'] is None)

    class _FakeTrader:
        last_execution = {'batch': 1000.0}
        _default_interval = 45
    _sch._trading_scheduler = _FakeTrader()
    _sch._trading_running = True
    _lv2 = _sch.get_loop_liveness()
    ck('读 trader 轮次时间戳', _lv2['last_batch_ts'] == 1000.0)
    ck('读 trader 执行间隔', _lv2['interval_seconds'] == 45)
    ck('线程 None→thread_alive False', _lv2['thread_alive'] is False)
except Exception as _e:
    ck(f'get_loop_liveness 导入/执行异常: {_e}', False)

print('--- 组8 _classify_boll_level（出轨强度：越界幅度/band 分级 + 逆势升 critical）---')
bcfg = {'warning_band_ratio': 0.5, 'critical_band_ratio': 1.5}
# 上轨110 下轨90 → band=20；越界阈值：warning≥10、critical≥30
ck('轨内(105)→不命中', am._classify_boll_level(105, 110, 90, bcfg) == '')
ck('冲破上轨刚达预警(120)→warning', am._classify_boll_level(120, 110, 90, bcfg) == 'warning')
ck('越界不足(118→0.4band)→不命中', am._classify_boll_level(118, 110, 90, bcfg) == '')
ck('冲破上轨极强(140→1.5band)→critical', am._classify_boll_level(140, 110, 90, bcfg) == 'critical')
ck('跌破下轨(80→0.5band)→warning', am._classify_boll_level(80, 110, 90, bcfg) == 'warning')
ck('逆势破上轨却看空(short,+120)→critical', am._classify_boll_level(120, 110, 90, {**bcfg, 'direction': 'short'}) == 'critical')
ck('逆势破下轨却看多(long,-80)→critical', am._classify_boll_level(80, 110, 90, {**bcfg, 'direction': 'long'}) == 'critical')
ck('顺势破上轨看多(long,+120)→warning', am._classify_boll_level(120, 110, 90, {**bcfg, 'direction': 'long'}) == 'warning')
ck('数据缺失(上轨0)→不命中', am._classify_boll_level(120, 0, 90, bcfg) == '')
ck('轨道倒挂(上<下)→不命中', am._classify_boll_level(120, 90, 110, bcfg) == '')
print('--- 组8b BOLL 独立状态机键位 boll|inst ---')
stb = {}
bk = f'boll|{INST}'
ck('boll 首次→send', am._transition(stb, bk, 'warning') == ('send', 'warning'))
ck('boll 升级critical→send', am._transition(stb, bk, 'critical') == ('send', 'critical'))
ck('boll 同级→suppress', am._transition(stb, bk, 'critical') == ('suppress', 'critical'))
ck('boll 回落轨内→recover', am._transition(stb, bk, '') == ('recover', 'recover'))
ck('boll 恢复后清键', bk not in stb)

print('--- 组9a _classify_spike（单轮环比插针判定，纯静态函数）---')
TT = trend_range_trader.TrendRangeTrader
sg = {'spike_change_pct': 5.0, 'spike_min_gap_seconds': 30}
ck('无基准(首轮)→(False,0)', TT._classify_spike(None, 105, 1000, sg) == (False, 0.0))
ck('cur_price<=0→不判', TT._classify_spike({'price': 100.0, 'ts': 900}, 0, 1000, sg) == (False, 0.0))
ck('间隔过小(10s<30)→不判', TT._classify_spike({'price': 100.0, 'ts': 1000}, 106, 1010, sg) == (False, 0.0))
_r = TT._classify_spike({'price': 100.0, 'ts': 1000}, 106.0, 1040, sg)
ck('涨6%(间隔40s)→True', _r[0] is True and abs(_r[1] - 6.0) < 1e-6)
ck('涨3%<阈值→False', TT._classify_spike({'price': 100.0, 'ts': 1000}, 103.0, 1040, sg)[0] is False)
ck('跌6%→True(取绝对值)', TT._classify_spike({'price': 100.0, 'ts': 1000}, 94.0, 1040, sg)[0] is True)

print('--- 组9b _note_spike_guard / _spike_pause_remaining（裸桩，开关+发信+延长+到期）---')


def _new_spike_trader(spike_cfg):
    o = trend_range_trader.TrendRangeTrader.__new__(trend_range_trader.TrendRangeTrader)
    o.message_notifier = _Notifier()
    o._spike_prev = {}
    o._spike_until = {}
    o._spike_logged = set()
    o._load_config = lambda: {'global_settings': {'spike_guard': spike_cfg}}
    return o


# 关闭开关：命中也不暂停、不发信，但仍刷新环比基准（开启瞬间基准新鲜）
o = _new_spike_trader({'spike_enabled': False, 'spike_change_pct': 5.0, 'spike_pause_minutes': 5})
o._spike_prev[INST] = {'price': 100.0, 'ts': time.time() - 100}
o._note_spike_guard(INST, 106.0, 'RS')
ck('关闭→不发信', len(o.message_notifier.alerts) == 0)
ck('关闭→不设暂停', INST not in o._spike_until)
ck('关闭→仍刷新环比基准', o._spike_prev[INST]['price'] == 106.0)

# 开启：命中→暂停+发1信+剩余>0
oncfg = {'spike_enabled': True, 'spike_change_pct': 5.0, 'spike_min_gap_seconds': 30, 'spike_pause_minutes': 5}
o = _new_spike_trader(oncfg)
o._spike_prev[INST] = {'price': 100.0, 'ts': time.time() - 100}
o._note_spike_guard(INST, 106.0, 'RS')
ck('开启命中→发1信', len(o.message_notifier.alerts) == 1)
ck('开启命中→设暂停', INST in o._spike_until)
ck('暂停剩余>0', o._spike_pause_remaining(INST) > 0)

# 未达阈值→不发信不暂停
o = _new_spike_trader(oncfg)
o._spike_prev[INST] = {'price': 100.0, 'ts': time.time() - 100}
o._note_spike_guard(INST, 102.0, 'RS')
ck('未达阈值→不发信不暂停', len(o.message_notifier.alerts) == 0 and INST not in o._spike_until)

# 暂停中二次命中→延长但不重发
o = _new_spike_trader(oncfg)
o._spike_prev[INST] = {'price': 100.0, 'ts': time.time() - 100}
o._note_spike_guard(INST, 106.0, 'R1')
_u1 = o._spike_until[INST]
o._spike_prev[INST] = {'price': 106.0, 'ts': time.time() - 100}
o._note_spike_guard(INST, 113.0, 'R2')
ck('二次命中→暂停中不重发(仍1信)', len(o.message_notifier.alerts) == 1)
ck('二次命中→到期被延长', o._spike_until[INST] >= _u1)

# 到期自动清除
o._spike_until[INST] = time.time() - 1
ck('到期→剩余0且清除记录', o._spike_pause_remaining(INST) == 0.0 and INST not in o._spike_until)


# =============================================================================
# 组10 发信失败兜底（死信落盘 / 去重 / 补发 / 节流 / 关闭 / 裁剪）
#   纯临时文件 + 假 email_tool，绝不连 SMTP；用 CRYPTO_PLAN_DATA_DIR 指向临时目录隔离
# =============================================================================
print('--- 组10 发信失败兜底（死信落盘/去重/补发/健康度）---')
import tempfile  # noqa: E402
from crypto.task.notification.message_notifier import MessageNotifier  # noqa: E402

_orig_data_dir = os.environ.get('CRYPTO_PLAN_DATA_DIR')
_tmp_dl = tempfile.mkdtemp(prefix='dl_smoke_')
os.environ['CRYPTO_PLAN_DATA_DIR'] = _tmp_dl


class _FakeEmailTool:
    def __init__(self):
        self.ok = False
        self.sent = []

    def send_html_email(self, to_emails, subject, html_content):
        if self.ok:
            self.sent.append(subject)
            return True
        return False


def _reset_dl(n):
    p = n._dead_letter_path()
    with MessageNotifier._DL_LOCK:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _new_notifier():
    n = MessageNotifier.__new__(MessageNotifier)
    n.email_tool = _FakeEmailTool()
    MessageNotifier._DL_LAST_FLUSH = 0.0
    _reset_dl(n)
    return n


n = _new_notifier()
ck('死信路径落在临时目录', n._dead_letter_path().startswith(_tmp_dl))

# 落盘 + 指纹去重
n._dead_letter_store(['a@x.com'], 'S1', '<h>1', '测试')
n._dead_letter_store(['a@x.com'], 'S2', '<h>2', '测试')
ck('落盘2条→pending=2', n.email_channel_health()['pending'] == 2)
n._dead_letter_store(['a@x.com'], 'S1', '<h>1', '测试')
ck('同指纹去重→仍2条', n.email_channel_health()['pending'] == 2)

# SMTP 仍不通 → 补发0、积压保留
r = n.flush_dead_letters(force=True)
ck('SMTP仍失败→补发0仍积压2', r['flushed'] == 0 and n.email_channel_health()['pending'] == 2)

# 恢复后按时间正序补发 → 清空
n.email_tool.ok = True
r = n.flush_dead_letters(force=True)
ck('恢复后补发→全清空', r['flushed'] == 2 and n.email_channel_health()['pending'] == 0)
ck('补发顺序最早先(S1→S2)', n.email_tool.sent == ['S1', 'S2'])

# 节流：非 force 且刚补发过 → skip
n._dead_letter_store(['a@x.com'], 'S3', '<h>3', '测试')
MessageNotifier._DL_LAST_FLUSH = time.time()
r = n.flush_dead_letters(force=False)
ck('节流窗口内→skip不补发', r['skipped'] is True and r['flushed'] == 0)

# enabled=False：不落盘、补发 skip
_reset_dl(n)
os.environ['CRYPTO_DL_ENABLED'] = '0'
n._dead_letter_store(['a@x.com'], 'S4', '<h>4', '测试')
ck('关闭兜底→不落盘', n.email_channel_health()['pending'] == 0)
r = n.flush_dead_letters(force=True)
ck('关闭兜底→补发skip', r['skipped'] is True)
del os.environ['CRYPTO_DL_ENABLED']

# max_pending 裁剪保留最新
_reset_dl(n)
os.environ['CRYPTO_DL_MAX_PENDING'] = '3'
for i in range(6):
    n._dead_letter_store(['a@x.com'], f'X{i}', f'<h{i}>', '测试')
_recs = n._dl_read(n._dead_letter_path())
ck('超限裁剪→仅保留最新3条(X3,X4,X5)',
   n.email_channel_health()['pending'] == 3 and
   sorted(r['subject'] for r in _recs) == ['X3', 'X4', 'X5'])
del os.environ['CRYPTO_DL_MAX_PENDING']

# 还原数据目录，避免污染其它脚本
_reset_dl(n)
if _orig_data_dir is None:
    os.environ.pop('CRYPTO_PLAN_DATA_DIR', None)
else:
    os.environ['CRYPTO_PLAN_DATA_DIR'] = _orig_data_dir


# =============================================================================
# 组11 P2-a 账本大幅背离（_handle_ledger_divergence）
# =============================================================================
class _FakePosMgr2:
    """P2 用桩：账本背离事件 + 兜底委托在位状态。"""

    def __init__(self):
        self.divs = []
        self.algo_ok = {}                    # {(inst,bucket,dir): bool}

    def drain_divergences(self):
        out = self.divs
        self.divs = []
        return out

    def algo_in_place(self, inst_id, bucket, direction):
        return bool(self.algo_ok.get((inst_id, bucket, direction), False))


def _new_trader_p2(risk_alerts):
    o = trend_range_trader.TrendRangeTrader.__new__(trend_range_trader.TrendRangeTrader)
    o._place_fail_streak = {}
    o._sltp_fail_streak = {}
    o._algo_fail_streak = {}
    o.pos_mgr = _FakePosMgr2()
    o.message_notifier = _Notifier()
    o._load_config = lambda: {'global_settings': {'risk_alerts': risk_alerts}}
    return o


def _shrink(scale, total=10.0, real=5.0, d='long'):
    return {'inst_id': INST, 'kind': 'shrink', 'direction': d, 'scale': scale,
            'total': total, 'real': real, 'detail': '趋势跟踪 10.0→5.0'}


def _absorb(ratio, total=10.0, excess=10.0, d='short'):
    return {'inst_id': INST, 'kind': 'absorb', 'direction': d, 'ratio': ratio,
            'excess': excess, 'total': total, 'real': total + excess, 'detail': 'x'}


print('--- 组11 P2-a 账本背离：缩减critical/吸收warning/阈值/从零吸收/开关 ---')
o = _new_trader_p2({}); o.pos_mgr.divs = [_shrink(0.3)]
o._handle_ledger_divergence(INST, 'RID')
ck('缩减保留30%→critical一封', o.message_notifier.alerts == [('账本大幅背离·多头持仓被系统外削减', 'critical')])

o = _new_trader_p2({}); o.pos_mgr.divs = [_shrink(0.8)]
o._handle_ledger_divergence(INST, 'RID')
ck('缩减保留80%>阈值0.5→不发', o.message_notifier.alerts == [])

o = _new_trader_p2({}); o.pos_mgr.divs = [_absorb(1.2)]
o._handle_ledger_divergence(INST, 'RID')
ck('吸收超120%→warning一封', o.message_notifier.alerts == [('账本大幅背离·空头持仓被大幅吸收', 'warning')])

o = _new_trader_p2({}); o.pos_mgr.divs = [_absorb(0.0, total=0.0, excess=20.0)]
o._handle_ledger_divergence(INST, 'RID')
ck('从零吸收(total=0)→不发避重启误报', o.message_notifier.alerts == [])

o = _new_trader_p2({'enabled': False}); o.pos_mgr.divs = [_shrink(0.1)]
o._handle_ledger_divergence(INST, 'RID')
ck('总开关关→不发但仍排空', o.message_notifier.alerts == [] and o.pos_mgr.divs == [])

o = _new_trader_p2({'ledger_divergence_enabled': False}); o.pos_mgr.divs = [_shrink(0.1)]
o._handle_ledger_divergence(INST, 'RID')
ck('背离子开关关→不发', o.message_notifier.alerts == [])

o = _new_trader_p2({'shrink_alert_frac': 0.9}); o.pos_mgr.divs = [_shrink(0.85)]
o._handle_ledger_divergence(INST, 'RID')
ck('阈值可配0.9→命中0.85', len(o.message_notifier.alerts) == 1)


# =============================================================================
# 组12 P2-b 兜底委托不在位（_note_algo_backup）
# =============================================================================
print('--- 组12 P2-b 兜底委托不在位：连续计数/恢复复位/无触发价不判/每阈值轮 ---')
o = _new_trader_p2({'algo_fail_rounds': 3}); o.pos_mgr.algo_ok = {}
o._note_algo_backup(INST, 'long', 5.0, 100.0, 95.0, None, 'RID')
ck('缺失streak1<阈值→未发', o._algo_fail_streak[INST + '|long'] == 1 and o.message_notifier.alerts == [])
o._note_algo_backup(INST, 'long', 5.0, 100.0, 95.0, None, 'RID')
o._note_algo_backup(INST, 'long', 5.0, 100.0, 95.0, None, 'RID')
ck('连续3轮缺失→critical一封',
   len(o.message_notifier.alerts) == 1 and o.message_notifier.alerts[0][1] == 'critical')
o.pos_mgr.algo_ok[(INST, TREND, 'long')] = True
o._note_algo_backup(INST, 'long', 5.0, 100.0, 95.0, None, 'RID')
ck('恢复在位→复位计数', o._algo_fail_streak.get(INST + '|long') is None)

o = _new_trader_p2({}); o.pos_mgr.algo_ok = {}; o._algo_fail_streak[INST + '|short'] = 2
o._note_algo_backup(INST, 'short', 5.0, 100.0, 0, None, 'RID')
ck('无可表达触发价→不判且复位',
   o._algo_fail_streak.get(INST + '|short') is None and o.message_notifier.alerts == [])

o = _new_trader_p2({'algo_fail_rounds': 3}); o.pos_mgr.algo_ok = {}
for _ in range(4):
    o._note_algo_backup(INST, 'long', 5.0, 100.0, 95.0, None, 'RID')
ck('每阈值轮才提醒(streak4不重复)', len(o.message_notifier.alerts) == 1)


# =============================================================================
print('\n' + '=' * 56)
if FAIL:
    print(f'FAILED  {len(FAIL)} 项：')
    for f in FAIL:
        print('   - ' + f)
    sys.exit(1)
print(f'ALL SMOKE TESTS PASSED  ({len(PASS)} 项)')
