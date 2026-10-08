#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：启动归因 + 自愈决策矩阵 + 每日归零判定
================================================
安全等级：🔒 纯离线 —— 状态文件全部落在临时目录（CRYPTO_LIFECYCLE_DIR），
记账层/邮件层/引擎锁全部打桩：不连 DB、不调 OKX、不发信、不退出进程。

被测行为（对应《实盘自愈与内存治理方案》P0-B / P0-C / P1-B）：
- 进程起来之后必须能回答「我是怎么起来的」：初次上线 / 内存超限自愈 /
  每日计划内重启 / 异常死亡（崩溃或被内核 kill -9）/ 冷启动。这五种情况的
  正确处置完全不同，全部当成同一种重启，要么半夜该起不起（静默停摆），
  要么初次部署就自己开始下单（更糟）。
- 归因之后按时间窗决定「起不起、等多久、先不发信」，并按启动台账熔断
  「窗口内反复自愈」这种救不回来 loop。
- 每日内存主动归零的拒绝执行条件（读不到水位 / 开关没开但实盘本应在跑 /
  水位太低 / 引擎忙）。

关键陷阱（本脚本专门盯着它们）：
1. 心跳来源必须过滤：guard 由独立 cron 写，交易进程早死了它照样每分钟落盘，
   拿它当心跳会把「死了几小时」判成「刚刚才死」。
2. 本进程自己刚写的心跳必须排除，否则初次启动会被自己那一行误判成异常死亡。
3. 退出标记只能用一次（consumed_at），否则撞内存起来后紧接着再崩一次，
   第二次仍会读到同一份新鲜标记，被误判成「又是内存超限」。
4. 拿不到归因结论时必须倒向「不自动拉起」，绝不能倒向「自动下单」。
5. SIGTERM handler 里不能打日志、不能读 RSS：它跑在主线程字节码边界上，恰好
   打断另一处写日志的动作时，再进 logging 就是拿不可重入锁会当场挂死。
6. 注册了 SIGTERM handler 之后，Python 默认的「立即终止」不再发生 —— 必须自己
   恢复默认处理并重发一次同信号，否则面板根本停不掉进程（比误判严重得多）。
7. **心跳很新 ≠ 上个进程刚死**：同机再起一个实例时，它读到的新鲜心跳是那个
   活着实例写的。旧口径把它判成 abnormal_death，每并发一次吃一格自愈额度，
   额度吃完后真需要自愈时不再自动接回（2026-09-11 实测：4 分钟内两格、熔断跳闸）。
   现在按心跳里的 pid 问一句"作者还活着吗"，活着就归 parallel_instance（不占额度、
   不自动拉起）。探活只用 psutil，不用 ``os.kill(pid, 0)``：本机 3.13.1 实测它既不
   终止子进程、也判不出 PID 复用和"已退出未回收"，而 os.kill 文档给的说法与此相反
   （语义跨版本变过）—— 细节记在 ``_pid_alive`` 的注释里。

场景清单：
A1~A9  退出标记写入/读取/原子覆盖/消费戳 + SIGTERM 捕获（写标记且不吞停请求）
B1~B23 归因五态 + 并发实例（第七个陷阱，含"最新一条是无 pid 的 embedded"这一生产常态）
       + 真探活/PID 复用 + 其余陷阱 + 过期标记 + 面板重启不当事故
C1~C16 决策矩阵（含熔断、并发实例不占额度、窗口外不熔断、计划内重启不计熔断、熔断信带排障指引、台账留心跳作者 pid）
D1~D10 调度器 worker 集成（拦下不发信 / 拦下要发信 / 预告 + 延时接回 / 静默接回 / 并发实例不起但发信）
E1~E7  每日归零判定与时刻解析
F1~F6  启动期 DB 预热退避重试（不阻塞主线程 / 中途成功即停 / 计满只报一行 /
       默认不打栈；F6 同时是 F5 的反向验证 —— 开了开关就必须给栈，否则 F5 是死传感器）

何时重跑：改 crypto/process_lifecycle.py 的判定或策略、改 crypto/task/scheduler.py
的 schedule_auto_resume/_auto_resume_worker、改 crypto/task/memory_daily_reset.py、
或改 crypto/database.py 的 warmup_async 重试策略。
"""
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    sys.path.insert(0, _p)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 状态文件目录必须在导入被测模块之前指到临时目录，绝不允许写进生产 crypto/logs/
_TMP = tempfile.mkdtemp(prefix='smoke_lifecycle_')
os.environ['CRYPTO_LIFECYCLE_DIR'] = _TMP

from crypto import process_lifecycle as lc  # noqa: E402

PASS, FAIL = [], []
TS_FMT = '%Y-%m-%d %H:%M:%S'
NOW = time.time()


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


def clean_dir():
    """每个归因场景都要干净现场：三个状态文件全清"""
    for fn in (lc.EXIT_MARKER_NAME, lc.BOOT_LEDGER_NAME):
        p = os.path.join(_TMP, fn)
        if os.path.exists(p):
            os.remove(p)
    hb = lc.heartbeat_path()
    if os.path.exists(hb):
        os.remove(hb)


def _make_live_and_dead_pids():
    """搞一个真活着的 pid 和一个真死掉的 pid，供「上个进程还活着吗」两侧用。

    不能像以前那样随手写个 pid=999：那在别的机器上可能正好被占用，测试结论
    就跟着机器变了。子进程退出并被 wait 回收后的 pid 才是可信的"死 pid"。
    """
    live = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    dead_child = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead = dead_child.pid
    dead_child.wait()          # 回收掉，确保不是僵尸
    time.sleep(0.2)
    return live, live.pid, dead


_LIVE_CHILD, _LIVE_PID, _DEAD_PID = _make_live_and_dead_pids()


def write_hb(records, pid=None):
    """伪造心跳文件。records: [(epoch, source, rss_mb)] 或 [(epoch, source, rss_mb, pid)]

    pid 默认给一个真实已回收的 pid ⇒ "作者已死"，与改动前一样走 abnormal_death；
    要测「并发实例」那条分支时显式传 ``pid=_LIVE_PID``。
    四元组里的第 4 位为 ``None`` 时**整个 pid 字段不写** —— 这就是生产上
    ``embedded`` 来源的真实形状（它不带 pid，且每分钟都比 ``app`` 心跳晚几秒落盘，
    所以"最新一条"基本永远是它）。
    """
    default_pid = _DEAD_PID if pid is None else pid
    with open(lc.heartbeat_path(), 'w', encoding='utf-8') as f:
        for rec in records:
            epoch, source, rss = rec[0], rec[1], rec[2]
            row = {'timestamp': datetime.fromtimestamp(epoch).strftime(TS_FMT),
                   'source': source, 'rss_mb': rss}
            if len(rec) > 3:
                if rec[3] is not None:
                    row['pid'] = rec[3]
            else:
                row['pid'] = default_pid
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def write_marker(reason, age_sec=60, **extra):
    payload = {'reason': reason, 'ts': datetime.fromtimestamp(NOW - age_sec).strftime(TS_FMT),
               'epoch': NOW - age_sec, 'pid': 999, 'rss_mb': 812.4, 'kill_mb': 800}
    payload.update(extra)
    with open(lc.exit_marker_path(), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)


def write_ledger(entries):
    """entries: [(reason, age_sec)] → 直接构造台账，控制熔断计数"""
    with open(lc.boot_ledger_path(), 'w', encoding='utf-8') as f:
        for i, (reason, age) in enumerate(entries, 1):
            f.write(json.dumps({'ts': datetime.fromtimestamp(NOW - age).strftime(TS_FMT),
                                'seq': i, 'reason': reason}, ensure_ascii=False) + '\n')


# ============================================================================
print('\n[A] 退出标记')
clean_dir()
check('A1 无标记时读取返回 None', lc.read_exit_marker() is None)
ok = lc.write_exit_marker(lc.REASON_MEM_KILL, rss_mb=812.4, kill_mb=800)
mk = lc.read_exit_marker()
check('A2 写入后可读回原因', ok and mk and mk['reason'] == 'mem_kill', mk)
check('A3 附带 RSS/阈值一并留痕',
      mk and abs(mk['rss_mb'] - 812.4) < 0.01 and mk['kill_mb'] == 800, mk)
lc.write_exit_marker(lc.REASON_DAILY_RESTART)
mk2 = lc.read_exit_marker()
check('A4 覆盖写不留半截文件（原子 replace）',
      mk2['reason'] == 'daily_restart' and not os.path.exists(lc.exit_marker_path() + '.tmp'), mk2)

clean_dir()
ok = lc.write_exit_marker(lc.REASON_MANUAL_EXIT, quiet=True, source='sigterm', signal_no=15)
mk = lc.read_exit_marker()
check('A5 quiet 写入（信号上下文专用）：不打日志但照样落盘，quiet 本身不入库',
      ok and mk['reason'] == 'manual_exit' and mk['source'] == 'sigterm'
      and mk['signal_no'] == 15 and 'quiet' not in mk, mk)

clean_dir()
RAISE = []
_real_restore = lc._restore_default_and_reraise
lc._restore_default_and_reraise = lambda signum: RAISE.append(signum)
try:
    lc._sigterm_handler(15, None)
finally:
    lc._restore_default_and_reraise = _real_restore
mk = lc.read_exit_marker()
check('A6 ⚠️ SIGTERM：留下 manual_exit 标记，且绝不吞掉停止请求',
      bool(mk) and mk['reason'] == 'manual_exit' and RAISE == [15], (mk, RAISE))

_real_sig = signal.getsignal(signal.SIGTERM)
signal.signal(signal.SIGTERM, lambda s, f: None)
check('A7 SIGTERM 已被其他组件接管时不抢语义', lc.install_sigterm_marker() is False)
signal.signal(signal.SIGTERM, _real_sig)      # 还回测试进程原来的处理器
lc._SIGTERM_INSTALLED = False
check('A8 主线程可安装且幂等',
      lc.install_sigterm_marker() is True and lc.install_sigterm_marker() is True)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
lc._SIGTERM_INSTALLED = False
_box = {}
_t = threading.Thread(target=lambda: _box.setdefault('r', lc.install_sigterm_marker()))
_t.start()
_t.join()
check('A9 非主线程安装 → 返回 False 不抛（冒烟/CLI 导入不得弄坏测试进程）',
      _box.get('r') is False and lc._SIGTERM_INSTALLED is False, _box)

# ============================================================================
print('\n[B] 启动归因')
clean_dir()
info = lc.classify_boot(NOW)
check('B1 现场全空 → first_run', info['reason'] == lc.BOOT_FIRST, info['detail'])

clean_dir()
write_marker(lc.REASON_MEM_KILL, age_sec=90)
write_hb([(NOW - 120, 'app', 806.2)])
info = lc.classify_boot(NOW)
check('B2 新鲜 mem_kill 标记 → mem_restart', info['reason'] == lc.BOOT_MEM, info['detail'])
check('B3 归因带出上次 RSS 供邮件展示',
      (info.get('prev_exit') or {}).get('rss_mb') == 812.4, info.get('prev_exit'))
info2 = lc.classify_boot(NOW)
check('B4 标记只用一次（再用就当没标过，避免连环误判）',
      info2['reason'] == lc.BOOT_ABNORMAL, f"{info2['reason']} / {info2['detail']}")

clean_dir()
write_marker(lc.REASON_DAILY_RESTART, age_sec=60)
info = lc.classify_boot(NOW)
check('B5 daily_restart 标记 → daily_restart', info['reason'] == lc.BOOT_DAILY, info['reason'])

clean_dir()
write_hb([(NOW - 45, 'app', 300.0), (NOW - 30, 'embedded', 301.0)])
info = lc.classify_boot(NOW)
check('B6 无标记但心跳很新 → abnormal_death（含被 OOM -9）',
      info['reason'] == lc.BOOT_ABNORMAL, info['detail'])

clean_dir()
write_hb([(NOW - 7200, 'app', 300.0)])
info = lc.classify_boot(NOW)
check('B7 心跳停在两小时前 → cold_start', info['reason'] == lc.BOOT_COLD, info['reason'])

clean_dir()
write_hb([(NOW - 7200, 'app', 300.0), (NOW - 20, 'guard', None)])
info = lc.classify_boot(NOW)
check('B8 ⚠️ guard 心跳不算数（独立 cron 写的，进程早死了它还在落）',
      info['reason'] == lc.BOOT_COLD, info['reason'])

clean_dir()
write_hb([(NOW - 7200, 'app', 300.0), (NOW + 1, 'app', 260.0)])
info = lc.classify_boot(NOW)
check('B9 ⚠️ 本进程自己刚写的心跳被排除（否则初次启动会被判成刚死）',
      info['reason'] == lc.BOOT_COLD, info['reason'])

clean_dir()
write_marker(lc.REASON_MEM_KILL, age_sec=7200)
write_hb([(NOW - 7200, 'app', 300.0)])
info = lc.classify_boot(NOW)
check('B10 过期标记不认领（那是上次重启已消费过的）',
      info['reason'] == lc.BOOT_COLD, info['reason'])

clean_dir()
write_marker(lc.REASON_MANUAL_EXIT, age_sec=45)
write_hb([(NOW - 20, 'app', 300.0)])
info = lc.classify_boot(NOW)
check('B11 ⚠️ 面板/守护进程 SIGTERM 重启 → cold_start，不被当成事故',
      info['reason'] == lc.BOOT_COLD and 'manual_exit' in info['detail'], info['detail'])

# ---- B12~B21：并发第二个实例不能被误判成「上个进程暴死」（陷阱 7） ----
_SAVED_BOOT_TS = lc._BOOT_TS


def _pretend_we_are_the_later_one():
    """把 _BOOT_TS 抬到"现在 + 40s"，等价于"那个活着的进程比我们更早出生"。

    心跳文件的过滤条件是 ``ts > _BOOT_TS - 3`` 才丢弃，NOW-45 的心跳照样保留，
    所以这一改只会影响存活判定，不会把心跳本身滤没。真实并发场景本来就是这个
    先后关系（旧实例先起、新实例后起），这里只是把时间轴搬到测试现场。
    """
    lc._BOOT_TS = NOW + 40


try:
    clean_dir()
    write_hb([(NOW - 45, 'app', 300.0)], pid=_LIVE_PID)
    _pretend_we_are_the_later_one()
    info = lc.classify_boot(NOW)
    check('B12 ⚠️ 心跳作者仍存活且更早出生 → parallel_instance，不再判暴死',
          info['reason'] == lc.BOOT_PARALLEL, f"{info['reason']} / {info['detail']}")
    check('B13 归因把作者 pid 摊出来（事后能核对是谁还活着）',
          info.get('heartbeat_pid') == _LIVE_PID, info.get('heartbeat_pid'))
    check('B14 结论点名「并发实例」而不是「重启」',
          '并发' in info['detail'] and str(_LIVE_PID) in info['detail'], info['detail'])

    # ---- B22/B23：生产的真实心跳形状（最新一条是不带 pid 的 embedded）----
    clean_dir()
    write_hb([(NOW - 50, 'app', 300.0, _LIVE_PID),
              (NOW - 45, 'embedded', None, None)])
    _pretend_we_are_the_later_one()
    info = lc.classify_boot(NOW)
    check('B22 ⚠️ 最新一条是无 pid 的 embedded（生产每分钟都这样）→ 仍回看带 pid 那条判活',
          info['reason'] == lc.BOOT_PARALLEL, f"{info['reason']} / {info['detail']}")

    clean_dir()
    write_hb([(NOW - 900, 'app', 300.0, _LIVE_PID),
              (NOW - 30, 'embedded', None, None)])
    _pretend_we_are_the_later_one()
    info = lc.classify_boot(NOW)
    check('B23 带 pid 的心跳本身已过期（只剩 embedded 在续命）→ 不认领并发，回落旧口径',
          info['reason'] == lc.BOOT_ABNORMAL, f"{info['reason']} / {info['detail']}")

    clean_dir()
    write_hb([(NOW - 45, 'app', 300.0)], pid=os.getpid())
    _pretend_we_are_the_later_one()
    info = lc.classify_boot(NOW)
    check('B15 ⚠️ 作者就是本进程 → 不虚构出一个"并发实例"（那是自己写的心跳）',
          info['reason'] == lc.BOOT_ABNORMAL, f"{info['reason']}")

    lc._BOOT_TS = _SAVED_BOOT_TS
    clean_dir()
    write_hb([(NOW - 45, 'app', 300.0)], pid=_DEAD_PID)
    info = lc.classify_boot(NOW)
    check('B16 作者确实已退出 → 仍按 abnormal_death（真暴死的口径没被削弱）',
          info['reason'] == lc.BOOT_ABNORMAL, info['reason'])

    clean_dir()
    with open(lc.heartbeat_path(), 'w', encoding='utf-8') as f:
        f.write(json.dumps({'timestamp': datetime.fromtimestamp(NOW - 45).strftime(TS_FMT),
                            'source': 'embedded', 'rss_mb': 300.0}) + '\n')
    info = lc.classify_boot(NOW)
    check('B17 心跳里没有 pid（embedded 来源/历史数据）→ 判不了就不认领，回落旧口径',
          info['reason'] == lc.BOOT_ABNORMAL, info['reason'])

    check('B18 _pid_alive 真探活：活进程判活',
          lc._pid_alive(_LIVE_PID, born_before=NOW + 40) is True)
    check('B19 ⚠️ _pid_alive 挡 PID 复用：比本进程晚出生的不算"上个实例还活着"',
          lc._pid_alive(_LIVE_PID, born_before=time.time() - 3600) is False)
    check('B20 _pid_alive 真探活：已回收进程判死', lc._pid_alive(_DEAD_PID) is False)
    check('B21 拿不到 pid 时返回 False（宁可沿用旧口径，不凭空取消自愈）',
          lc._pid_alive(None) is False and lc._pid_alive(0) is False)
finally:
    lc._BOOT_TS = _SAVED_BOOT_TS

# ============================================================================
print('\n[C] 自愈决策矩阵')


def plan(reason, night, ledger=None, now=None, hb_pid=None):
    info = {'reason': reason, 'night_window': night, 'detail': '', 'boot_ts': lc._now_str(now or NOW)}
    if hb_pid is not None:
        info['heartbeat_pid'] = hb_pid
    clean_dir()
    if ledger:
        write_ledger(ledger)
    return lc.plan_resume(info, now=now or NOW)


p = plan(lc.BOOT_FIRST, False)
check('C1 first_run → 不起也不发信', not p['allowed'] and p['silent'], p['why'])

p = plan(lc.BOOT_UNKNOWN, False)
check('C2 原因未知 → 倒向不自动拉起（保守方向）', not p['allowed'], p['why'])

p = plan(lc.BOOT_MEM, True)
check('C3 撞内存 + 夜间 → 20s 直接接回，不预告',
      p['allowed'] and p['delay_seconds'] == lc.NIGHT_DELAY_SEC and not p['pre_notify'], p)

p = plan(lc.BOOT_MEM, False)
check('C4 撞内存 + 白天 → 先发预告再等 600s',
      p['allowed'] and p['delay_seconds'] == lc.DAY_DELAY_SEC and p['pre_notify'], p)

p = plan(lc.BOOT_ABNORMAL, False)
check('C5 异常死亡 + 白天 → 同一套（这正是原来漏掉的那一类）',
      p['allowed'] and p['pre_notify'], p)

p = plan(lc.BOOT_DAILY, False)
check('C6 每日归零 → 立刻接回且静默（天天发信等于噪声）',
      p['allowed'] and p['silent'] and not p['pre_notify']
      and p['delay_seconds'] == lc.NIGHT_DELAY_SEC, p)

p = plan(lc.BOOT_COLD, True)
check('C7 冷启动（面板重启/发版）→ 按时间窗处理', p['allowed'], p)

p = plan(lc.BOOT_MEM, True, ledger=[(lc.BOOT_MEM, 60), (lc.BOOT_MEM, 120), (lc.BOOT_MEM, 180)])
check('C8 窗口内第 3 次自愈 → 熔断，只发信不起',
      not p['allowed'] and p['selfheal_count'] >= lc.RESUME_MAX, p['why'])

p = plan(lc.BOOT_MEM, True, ledger=[(lc.BOOT_MEM, 60), (lc.BOOT_MEM, 120), (lc.BOOT_MEM, 9 * 3600)])
check('C9 窗口外的历史重启不计入熔断', p['allowed'] and p['selfheal_count'] == 2, p)

p = plan(lc.BOOT_MEM, True, ledger=[(lc.BOOT_DAILY, 60), (lc.BOOT_DAILY, 120), (lc.BOOT_DAILY, 180)])
check('C10 计划内每日重启不占熔断额度', p['allowed'] and p['selfheal_count'] == 0, p)

p = plan(lc.BOOT_MEM, True, ledger=[(lc.BOOT_MEM, 60), (lc.BOOT_MEM, 120), (lc.BOOT_MEM, 180)])
check('C11 熔断信得带排障指引（只说“不起了”等于把锅丢回给用户）',
      '/system-status' in p['why'] and 'memtrace.on' in p['why'], p['why'])

p = plan(lc.BOOT_PARALLEL, False, hb_pid=_LIVE_PID)
check('C12 并发实例 → 不自动拉起（两个调度器对同一账户会重复开仓）',
      not p['allowed'], p['why'])
check('C13 并发实例不发预告信（本就不打算起），但不静默（得让人知道多起了一个）',
      not p['pre_notify'] and not p['silent'], p)
check('C14 拒绝理由里点名是哪个活进程，并给出"先停旧再起新"的处置',
      str(_LIVE_PID) in p['why'] and '并发' in p['why'] and '停掉旧实例' in p['why'], p['why'])

p = plan(lc.BOOT_MEM, True, ledger=[(lc.BOOT_PARALLEL, 60), (lc.BOOT_PARALLEL, 120),
                                    (lc.BOOT_PARALLEL, 180), (lc.BOOT_MEM, 240),
                                    (lc.BOOT_MEM, 300)])
check('C15 ⚠️ 并发实例不占自愈熔断额度（2026-09-11 就是被 4 条误判记录吃光额度、'
      '真崩了反而不再自动接回）',
      p['allowed'] and p['selfheal_count'] == 2, p)

clean_dir()
seq = lc.record_boot({'boot_ts': lc._now_str(NOW), 'reason': lc.BOOT_PARALLEL,
                      'prev_exit': None, 'heartbeat_lag_sec': 45,
                      'heartbeat_pid': _LIVE_PID})
_recs = lc.read_ledger()
check('C16 台账记下"最后那条心跳是谁写的"（事后不必靠猜区分暴死/并发）',
      seq == 1 and _recs and _recs[-1].get('heartbeat_pid') == _LIVE_PID, _recs[-1:])
clean_dir()

# ============================================================================
print('\n[D] 调度器 worker 集成')

# 假 trader：只复刻「单轮不可中断」这一个关键特性
class FakeTrader:
    def __init__(self, manual_direction_config=None, account=None):
        self.account = account or 'main'
        self.account_name = self.account + '-name'
        self.stop_event = __import__('threading').Event()
        self.running = False

    def start_real_scheduler(self):
        self.running = True
        while self.running and not self.stop_event.is_set():
            time.sleep(0.2)
        self.running = False


_stub = types.ModuleType('crypto.task.trend_range_trader')
_stub.TrendRangeTrader = FakeTrader
sys.modules['crypto.task.trend_range_trader'] = _stub

from crypto.task import scheduler as sched_mod  # noqa: E402

RT = {'desired_running': True, 'account': 'acctX', 'auto_resume': True,
      'updated_at': '', 'last_event': 'manual_start'}


class FakeRepo:
    @staticmethod
    def load_runtime():
        return dict(RT)

    @staticmethod
    def set_desired_running(running, account=None, event=''):
        RT['desired_running'] = bool(running)
        if account is not None:
            RT['account'] = account
        RT['last_event'] = event
        return True


sched_mod._runtime_repo = FakeRepo

MAILS = []
sched_mod.TaskScheduler._notify_auto_resume_result = lambda self, ok, msg, account, rt: \
    MAILS.append(('result', ok, msg))
sched_mod.TaskScheduler._notify_auto_resume_skipped = lambda self, rt, why='': \
    MAILS.append(('skipped', rt.get('account'), why))
sched_mod.TaskScheduler._notify_resume_pending = lambda self, plan, delay: \
    MAILS.append(('pending', plan.get('reason'), delay))


class FakeLifecycle:
    """worker 只跟 plan_resume 要结论，这里直接给结论，避免依赖真实时钟"""
    @staticmethod
    def plan_resume(info, now=None):
        return dict(info['__plan'])


sched_mod._lifecycle = FakeLifecycle


def run_worker(plan, delay=None, boot_info=None, desired=True):
    """走真实 schedule_auto_resume → _auto_resume_worker 链路

    每次跑前把期望状态归位：上一组的 stop_trading_scheduler 会把
    desired_running 清成 False（这正是它该做的），不归位就会把下一组
    “应该拉起”的用例跑成“不拉”。
    """
    ts = sched_mod.TaskScheduler()
    MAILS.clear()
    RT.update({'desired_running': bool(desired), 'account': 'acctX', 'auto_resume': True})
    info = boot_info if boot_info is not None else {'reason': plan['reason'], '__plan': plan}
    info.setdefault('__plan', plan)
    ts.schedule_auto_resume(delay_seconds=delay, boot_info=info)
    time.sleep(0.6)
    return ts


FULL = {'allowed': True, 'delay_seconds': 0.1, 'pre_notify': False, 'silent': False,
        'window': 'night', 'why': '夜间直接接回', 'selfheal_count': 1, 'resume_at': '',
        'reason': lc.BOOT_MEM}

ts = run_worker(dict(FULL, allowed=False, silent=True, why='初次上线，不自动下单'))
check('D1 初次上线：不起、也不发信（没有期望运行这回事）',
      not ts._trading_running and not MAILS, MAILS)

ts = run_worker(dict(FULL, allowed=False, silent=False, why='窗口内第 3 次自愈 → 熔断'))
check('D2 被拦下但实盘本应在跑 → 必须发信（不能再静默停摆）',
      not ts._trading_running and any(m[0] == 'skipped' and '熔断' in m[2] for m in MAILS), MAILS)

ts = run_worker(dict(FULL))
check('D3 计划通过 → 到期自动拉起', ts._trading_running and ts._trading_account == 'acctX',
      ts.get_trading_status())
check('D4 夜间不预告', not any(m[0] == 'pending' for m in MAILS), MAILS)
ts.stop_trading_scheduler(wait_seconds=2)

ts = run_worker(dict(FULL, pre_notify=True, delay_seconds=0.1, window='day'))
check('D5 白天先发预告信再接回（人工否决窗）',
      any(m[0] == 'pending' and m[2] == 0.1 for m in MAILS) and ts._trading_running, MAILS)
ts.stop_trading_scheduler(wait_seconds=2)

RT['desired_running'] = False
ts = run_worker(dict(FULL), desired=False)
check('D6 上次就是停着的 → 归因通过也不自作主张下单', not ts._trading_running, MAILS)

ts = run_worker(dict(FULL, silent=True, reason=lc.BOOT_DAILY))
check('D7 计划内每日归零：接回成功不发信',
      ts._trading_running and not any(m[0] == 'result' for m in MAILS), MAILS)
ts.stop_trading_scheduler(wait_seconds=2)

st = sched_mod.TaskScheduler()
st._boot_info = {'reason': lc.BOOT_MEM, 'label': '内存超限自愈重启'}
st._resume_plan = {'allowed': True, 'selfheal_count': 2}
rs = st.get_runtime_status()
check('D8 归因与计划随 runtime 状态上报（页面/排障用）',
      rs.get('boot', {}).get('reason') == lc.BOOT_MEM and rs.get('resume_plan') is not None, rs)
check('D9 邮件正文含归因行（否则收信人无法判断该不该担心）',
      'mem_restart' in st._format_boot_line(), st._format_boot_line())

ts = run_worker(dict(FULL, allowed=False, silent=False, pre_notify=False,
                     reason=lc.BOOT_PARALLEL,
                     why=f'并发实例（pid={_LIVE_PID}）→ 不自动拉起'))
check('D10 ⚠️ 并发实例：本进程不起调度器，但必须发信说明"实盘没停，是多开了一个"',
      not ts._trading_running
      and any(m[0] == 'skipped' and '并发实例' in m[2] for m in MAILS), MAILS)
ts.stop_trading_scheduler(wait_seconds=2)

# ============================================================================
print('\n[E] 每日内存主动归零')

from crypto.task import memory_daily_reset as mdr  # noqa: E402

_null = logging.getLogger('smoke_null')
_null.addHandler(logging.NullHandler())
_null.propagate = False
mdr.task_log = _null          # 冒烟不往生产 task_scheduler.log 里灌噪声

ok, why = mdr.evaluate_daily_reset(None, {'desired_running': True, 'auto_resume': True})
check('E1 读不到水位 → 不做不可逆动作', not ok, why)
ok, why = mdr.evaluate_daily_reset(600, {'desired_running': True, 'auto_resume': False})
check('E2 实盘本应在跑但自动拉起开关没开 → 拒绝（否则重启=静默停摆）', not ok and '停摆' in why, why)
ok, why = mdr.evaluate_daily_reset(120, {'desired_running': True, 'auto_resume': True})
check('E3 水位低于线 → 不折腾', not ok, why)
ok, why = mdr.evaluate_daily_reset(600, {'desired_running': True, 'auto_resume': True})
check('E4 水位够且开关就绪 → 执行', ok, why)
ok, why = mdr.evaluate_daily_reset(600, {'desired_running': False, 'auto_resume': False}, busy=True)
check('E5 策略引擎忙（等超时后）→ 让位给明天', not ok and '让位' in why, why)
ok, why = mdr.evaluate_daily_reset(600, {'desired_running': False, 'auto_resume': False})
check('E6 实盘本来就没跑 → 可以清零（没有停摆风险）', ok, why)
check('E7 默认时刻 04:03，写坏就退回默认',
      mdr.parse_reset_at('05:30') == (5, 30) and mdr.parse_reset_at('25:99') == (4, 3)
      and mdr.cron_kwargs() == {'hour': 4, 'minute': 3}, mdr.cron_kwargs())

# ============================================================================
print('\n[F] 启动期 DB 预热重试（远端 MySQL 抖动不得让进程带未初始化状态长跑）')

from crypto import database as db  # noqa: E402

_CALLS, _DONE = [], threading.Event()
_real_init_db = db.init_db


class _Cap(logging.Handler):
    """抓 crypto.database 这个 logger 的日志记录（含 exc_info，用来验"不打栈"）"""
    rows = []

    def emit(self, record):
        _Cap.rows.append((record.levelno, record.getMessage(), record.exc_info))


_DB_LOG = logging.getLogger('crypto.database')
_H = _Cap()
_DB_PROPAGATE = _DB_LOG.propagate
_DB_LOG.addHandler(_H)
_DB_LOG.propagate = False


def run_warm(raises, **kw):
    _CALLS.clear(); _Cap.rows.clear(); _DONE.clear()
    box = {'left': list(raises)}

    def fake_init_db(*a, **k):
        _CALLS.append(1)
        if box['left']:
            raise OSError(box['left'].pop(0))
        _DONE.set()

    db.init_db = fake_init_db
    t0 = time.time()
    db.warmup_async(**kw)
    elapsed = time.time() - t0                     # 必须是立刻返回，不许阻塞
    for _ in range(200):                            # 最多等 2s 让后台线程跑完
        if _DONE.is_set() or len(_CALLS) >= len(raises) + 1:
            break
        time.sleep(0.01)
    return elapsed


try:
    elapsed = run_warm(['(2013) Lost connection', '(2013) Lost connection'],
                       attempts=4, first_wait=0.01)
    texts = ' | '.join(m for _, m, _x in _Cap.rows)
    check('F1 预热失败要退避重试，第 3 次成功即停（不空转）',
          len(_CALLS) == 3 and _DONE.is_set(), f'{len(_CALLS)} 次 / {_DONE.is_set()}')
    check('F2 主线程绝不被拖住（app.run 不等 DB）', elapsed < 0.1, f'{elapsed:.3f}s')
    check('F3 中途成功就不该报"均未成功"', '均未成功' not in texts, texts)

    elapsed = run_warm(['boom1', 'boom2', 'boom3', 'boom4'], attempts=4, first_wait=0.01)
    lvl_last = _Cap.rows[-1][0] if _Cap.rows else 0
    exc_last = _Cap.rows[-1][2] if _Cap.rows else None
    check('F4 全部失败 → 计满次数并只报一条 error（首个请求仍会再试）',
          len(_CALLS) == 4 and lvl_last == logging.ERROR, f'{len(_CALLS)} 次 / {lvl_last}')
    check('F5 ⚠️ 默认不打 traceback（一坨 pymysql 栈会把同屏真故障淹没）',
          bool(_Cap.rows) and not exc_last, f'{len(_Cap.rows)} 条 / exc_info={exc_last!r}')

    _Cap.rows.clear()
    _box = {'left': ['x', 'y', 'z', 'w']}

    def _fail(*a, **k):
        _CALLS.append(1)
        if _box['left']:
            raise OSError(_box['left'].pop(0))
    db.init_db = _fail
    os.environ['CRYPTO_DB_WARM_TRACE'] = '1'
    db.warmup_async(attempts=1, first_wait=0.01)
    for _ in range(200):
        if _Cap.rows:
            break
        time.sleep(0.01)
    check('F6 要看栈时 CRYPTO_DB_WARM_TRACE=1 仍然给栈',
          bool(_Cap.rows) and _Cap.rows[-1][2] is not None, _Cap.rows)
finally:
    db.init_db = _real_init_db
    os.environ.pop('CRYPTO_DB_WARM_TRACE', None)
    _DB_LOG.removeHandler(_H)
    _DB_LOG.propagate = _DB_PROPAGATE

# ============================================================================
print('\n' + '=' * 60)
print(f'  PASS {len(PASS)}  /  FAIL {len(FAIL)}')
if FAIL:
    print('  失败项: ' + ', '.join(FAIL))
# 收掉为测试真起的那个"活进程"，别留一堆睡 120s 的孤儿 python
try:
    _LIVE_CHILD.terminate()
    _LIVE_CHILD.wait(timeout=5)
except Exception:
    pass
try:
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
except Exception:
    pass
sys.exit(1 if FAIL else 0)
