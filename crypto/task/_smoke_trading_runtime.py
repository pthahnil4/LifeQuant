#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：实盘调度器启停竞态（#6）+ 重启自愈记账（#5）
====================================================
安全等级：🔒 纯离线 —— 假 trader 顶包、记账层换内存桩、邮件通知打桩，
不连 DB、不调 OKX、不发信。

被测行为：
- stop 只置标志即返回，而真实 TrendRangeTrader 一轮里的 analyze()（3~50s）
  不可中断，标志只在轮次边界被检查。这个「收尾中」窗口如果不去挡，用户
  点完停止再点启动，就会有两个 trader 各自按自己的内存账本判断"有没有
  持仓/有没有挂单"并发下单 → 重复开仓。
- 进程重启（含内存看门狗 os._exit 自愈退出）后，原设计"只注册不启动"，
  实盘会静默停摆、持仓无人管。改为 kv_store 记「期望运行状态」+ 页面开关
  控制自动拉起；开关没开但本应在跑时也必须发信提醒，不能再次静默。

场景清单：
A1~A3 启动成功 / 期望状态记账 / 运行中重复启动被拒
A4~A6 停止限时 join 超时 → 如实提示"收尾中"、不拖长 HTTP、期望状态清 False
A7~A9 finishing 第三态、收尾期间启动被拒（双 trader 竞态已挡）、旧线程自行退出
A10~A11 线程退出后可正常启动；未运行时停止只提示不报错
B1~B6 自愈：开关关闭不动作但发告警、开关打开+期望运行才拉起、来源记
       auto_resume、拉起结果留痕、上次是停止状态则绝不自作主张下单
B12   显式传 delay_seconds 而不传 boot_info = 调用方自己拿定主意，
       不走启动归因策略（归因策略的覆盖在 _smoke_boot_resume.py）

何时重跑：改 crypto/task/scheduler.py 的 start/stop/get_trading_status/
schedule_auto_resume 任一逻辑，或改 trading_runtime_repo.py。
"""
import os
import sys
import types
import time
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    sys.path.insert(0, _p)

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


# ---------------------------------------------------------------- 沙箱装配
# 用假 trader 顶掉真模块：绝不能碰真实下单链路
class FakeTrader:
    """复刻真实调度器的关键特性：单轮工作不可中断，标志只在轮次边界检查。

    桩要是每 10ms 就让步，stop 后瞬间退出，「收尾中」窗口根本不存在，
    竞态防护就测了个寂寞（这是本脚本第一版踩过的坑）。
    """
    CHUNK = 0.8

    def __init__(self, manual_direction_config=None, account=None):
        self.account = account or "main"
        self.account_name = (account or "main") + "-name"
        self.running = False
        self.stop_event = threading.Event()

    def start_real_scheduler(self):
        self.running = True
        while self.running and not self.stop_event.is_set():
            time.sleep(self.CHUNK)
        self.running = False


_stub_mod = types.ModuleType("crypto.task.trend_range_trader")
_stub_mod.TrendRangeTrader = FakeTrader
sys.modules["crypto.task.trend_range_trader"] = _stub_mod

from crypto.task import scheduler as sched_mod  # noqa: E402

# 记账层换成内存桩，不碰 DB
CALLS = []
RT = {"desired_running": False, "account": None, "auto_resume": False,
      "updated_at": "", "last_event": ""}


class FakeRepo:
    @staticmethod
    def load_runtime():
        return dict(RT)

    @staticmethod
    def set_desired_running(running, account=None, event=""):
        CALLS.append(("desired", running, account, event))
        RT["desired_running"] = bool(running)
        if account is not None:
            RT["account"] = account
        RT["last_event"] = event
        return True

    @staticmethod
    def set_auto_resume(enabled, event="auto_resume_toggle"):
        CALLS.append(("auto_resume", enabled))
        RT["auto_resume"] = bool(enabled)
        return dict(RT)


sched_mod._runtime_repo = FakeRepo

# 邮件通知打桩：冒烟阶段绝不能往真实收件箱发信
# （_notify_* 现在都是实例方法，桩用普通函数接 self；why 为拦下原因）
NOTIFY = []
sched_mod.TaskScheduler._notify_auto_resume_result = \
    lambda self, ok, msg, account, rt: NOTIFY.append(('result', ok, msg))
sched_mod.TaskScheduler._notify_auto_resume_skipped = \
    lambda self, rt, why='': NOTIFY.append(('skipped', rt.get('account'), why))
sched_mod.TaskScheduler._notify_resume_pending = \
    lambda self, plan, delay: NOTIFY.append(('pending', plan.get('reason'), delay))

ts = sched_mod.TaskScheduler()

# ---------------------------------------------------------------- A 组：启停竞态
print("\n[A] 启停竞态防护")
ok, msg = ts.start_trading_scheduler("acctA")
check("A1 首次启动成功", ok and ts._trading_running, msg)
check("A2 期望运行状态记为 True+账号",
      RT["desired_running"] and RT["account"] == "acctA", RT)
ok2, msg2 = ts.start_trading_scheduler("acctB")
check("A3 运行中再次启动被拒", (not ok2) and "已在运行" in msg2, msg2)

t0 = time.time()
ok3, msg3 = ts.stop_trading_scheduler(wait_seconds=0.2)
dt = time.time() - t0
check("A4 停止返回成功但提示收尾中", ok3 and "收尾" in msg3, msg3)
check("A5 限时 join 未把整轮耗时拖进 HTTP", 0.15 <= dt < 1.0, round(dt, 3))
check("A6 人工停止清除期望运行状态", not RT["desired_running"], RT)
st = ts.get_trading_status()
check("A7 状态含 finishing=True", st["finishing"] and not st["running"], st)
ok4, msg4 = ts.start_trading_scheduler("acctC")
check("A8 收尾期间启动被拒（双 trader 竞态已挡）", (not ok4) and "收尾" in msg4, msg4)

ts._trading_thread.join(5)
check("A9 旧线程最终自行退出", not ts._trading_thread.is_alive())
ok5, msg5 = ts.start_trading_scheduler("acctD")
check("A10 线程退出后可正常启动", ok5, msg5)
ts.stop_trading_scheduler(wait_seconds=3)
ts._trading_thread.join(5)

ok6, msg6 = ts.stop_trading_scheduler()
check("A11 未运行时停止返回未运行", (not ok6) and "未运行" in msg6, msg6)

# ---------------------------------------------------------------- B 组：重启自愈
print("\n[B] 重启自动拉起")
RT.update({"desired_running": True, "account": "acctE", "auto_resume": False})
ts.schedule_auto_resume(delay_seconds=0.1)
time.sleep(0.6)
check("B1 开关关闭时不自动拉起", not ts._trading_running)
check("B5 开关关闭但本应在跑 → 发告警（不再静默停摆）",
      any(c[0] == 'skipped' and c[1] == 'acctE' for c in NOTIFY), NOTIFY)
check("B12 不传 boot_info 即走老语义（不做归因策略，无拦下原因）",
      any(c[0] == 'skipped' and c[2] == '' for c in NOTIFY), NOTIFY)

RT.update({"desired_running": True, "account": "acctF", "auto_resume": True})
ts.schedule_auto_resume(delay_seconds=0.1)
time.sleep(0.8)
check("B2 开关打开+期望运行 → 自动拉起",
      ts._trading_running and ts._trading_account == "acctF", ts.get_trading_status())
check("B3 自动拉起记为 auto_resume 来源",
      any(c[0] == "desired" and c[1] and "auto_resume" in str(c[3]) for c in CALLS),
      CALLS[-3:])
ts.stop_trading_scheduler(wait_seconds=3)
check("B6 自动拉起结果有通知留痕",
      any(c[0] == 'result' and c[1] for c in NOTIFY), NOTIFY)

RT.update({"desired_running": False, "account": "acctG", "auto_resume": True})
ts.schedule_auto_resume(delay_seconds=0.1)
time.sleep(0.6)
check("B4 上次就是停止状态则绝不自作主张下单", not ts._trading_running)

print("\n" + "=" * 52)
print(f"  PASS {len(PASS)}  /  FAIL {len(FAIL)}")
if FAIL:
    print("  失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
