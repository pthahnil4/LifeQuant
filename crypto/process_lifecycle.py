#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程生命周期归因与自愈策略（Process Lifecycle）
==============================================
要解决的问题
------------
内存看门狗撞到 kill 阈值时会 ``os._exit(1)`` 交给外部管理器（supervisord /
宝塔 Python 项目管理器）拉起，但**新起来的进程并不知道自己是怎么起来的**：
是第一次部署、被自己撞线杀掉、被内核 OOM ``kill -9``、还是人在面板点了重启。
而「重启后要不要自动恢复实盘调度、要不要先给人留 10 分钟否决窗」这个决定，
恰恰必须按原因分开处理 —— 全部当成同一种重启，要么半夜该起不起（静默停摆），
要么初次部署就自己开始下单（更糟）。

三件事实拼出原因
----------------
1. **退出标记** ``logs/proc_exit.json``：进程在**自己知道**要退出的那一刻
   （看门狗撞线、每日主动归零）先把原因落盘再 ``os._exit``。``os._exit`` 不跑
   ``atexit``，所以必须显式写在退出手之前；内容只有几十字节 JSON，同进程写自己
   目录下的文件，``fsync`` 后不依赖任何库。第三个写入点是 **SIGTERM**
   （``install_sigterm_marker``）：面板/守护进程优雅停止时先落 ``manual_exit``，
   然后把终止动作交还给系统。
2. **心跳** ``logs/memory_history.jsonl``：交易进程每 60s（系统检查 embedded）
   与每 120s（内存采样 app）各落一行。被内核 ``kill -9`` 时来不及写退出标记，
   但心跳会停在死亡那一刻 —— 「心跳很新、却没有退出标记」= 异常死亡。
   ⚠️ 只认 ``source in ('app', 'embedded')``：``guard`` 由独立 cron 写，交易进程
   早就死了它照样每分钟落盘，拿它当心跳会把「死了几小时」误判成「刚刚才死」。
3. **启动台账** ``logs/boot_ledger.jsonl``：每次归因后追加一行，既给本次启动
   编号（``boot_seq``），也是「重启风暴熔断」的计数依据。

判定顺序（自上而下，先命中先返回）
----------------------------------
- 退出标记新鲜（≤15min）且未被消费 → ``mem_restart`` / ``daily_restart``
  （其它原因的标记，如 SIGTERM 的 ``manual_exit`` → ``cold_start``）
- 无新鲜标记，但心跳距本次启动 < 7min：
    - 写这条心跳的 pid **还活着** → ``parallel_instance``（同机第二个实例，
      不计熔断额度、不自动拉起：死的是别人，凭什么按"暴死"扣额度？）
    - 那个 pid 已经没了（或心跳里压根没 pid） → ``abnormal_death``
      （崩溃 / OOM -9 / 强杀）
- 从来没有过本进程心跳、也没有退出标记 → ``first_run``（初次上线）
- 其余（停机很久后才起来）→ ``cold_start``（人工启动 / 面板重启 / 开机自启）

失败取向
--------
所有读写一律「异常吞掉 + 记日志 + 返回保守值」：归因层坏掉绝不拖垮启动流程。
``plan_resume`` 在拿不到归因结论时返回 ``allowed=False``（保持人工启动），与
改造前的行为完全一致 —— **任何一层失效都不会替用户做出自动下单的决定**。

环境变量（全部可选）
--------------------
- CRYPTO_LIFECYCLE_DIR        三个状态文件的目录（冒烟测试用临时目录，绝不误写生产 logs/）
- CRYPTO_EXIT_FRESH_SEC       退出标记算"新鲜"的窗口，默认 900
- CRYPTO_HB_FRESH_SEC         心跳算"刚死"的窗口，默认 420
- CRYPTO_RESUME_NIGHT_START_HOUR / _END_HOUR  夜间窗口，默认 0 与 8
- CRYPTO_RESUME_NIGHT_DELAY_SEC 夜间拉起延时，默认 20
- CRYPTO_RESUME_DAY_DELAY_SEC   白天拉起延时（人工否决窗），默认 600
- CRYPTO_RESUME_MAX             窗口内自愈次数上限（含本次），默认 3
- CRYPTO_RESUME_WINDOW_SEC      熔断统计窗口，默认 21600（6 小时）
"""

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

_CRYPTO_DIR = os.path.dirname(os.path.abspath(__file__))

# 状态文件目录：默认与内存历史同级的 crypto/logs/，可用环境变量整体改址
LOG_DIR = (os.environ.get('CRYPTO_LIFECYCLE_DIR', '').strip()
           or os.path.join(_CRYPTO_DIR, 'logs'))

EXIT_MARKER_NAME = 'proc_exit.json'
BOOT_LEDGER_NAME = 'boot_ledger.jsonl'
HEARTBEAT_FILE_NAME = 'memory_history.jsonl'

# 只认这两种来源当心跳（guard 是独立进程写的，见文件头说明）
HEARTBEAT_SOURCES = ('app', 'embedded')

# 本进程"出生"时刻。归因时用它把「本进程自己刚写的心跳」排除掉 —— 否则初次
# 启动会被自己写下的那一行误判成「上一轮刚刚异常死亡」。
_BOOT_TS = time.time()
# 排除自身时留的余量（秒）。取值远小于 HB_FRESH_SEC，所以只会让心跳龄差几秒，
# 不影响分类结论；supervisord 秒级拉起也不会因此漏判。
_SELF_RECORD_GRACE_SEC = 3.0

TS_FMT = '%Y-%m-%d %H:%M:%S'

# --------------------------------------------------------------- 退出原因
REASON_MEM_KILL = 'mem_kill'              # 看门狗判定内存超限，主动自杀
REASON_DAILY_RESTART = 'daily_restart'    # 每日主动归零重启（计划内）
REASON_MANUAL_EXIT = 'manual_exit'        # 代码里其它主动退出（预留）

# --------------------------------------------------------------- 启动归因
BOOT_FIRST = 'first_run'          # 初次上线
BOOT_MEM = 'mem_restart'          # 内存超限自愈
BOOT_DAILY = 'daily_restart'      # 每日计划内重启
BOOT_ABNORMAL = 'abnormal_death'  # 崩溃 / 被内核 OOM 杀 / 外部强杀
BOOT_COLD = 'cold_start'          # 停机很久后的人工/开机启动
BOOT_PARALLEL = 'parallel_instance'  # 同机已有实盘实例在跑（本次是并发的第二个）
BOOT_UNKNOWN = 'unknown'

# 计入「重启风暴熔断」的归因：非计划内的自愈型重启
SELFHEAL_REASONS = (BOOT_MEM, BOOT_ABNORMAL)

BOOT_LABELS = {
    BOOT_FIRST: '初次启动',
    BOOT_MEM: '内存超限自愈重启',
    BOOT_DAILY: '每日主动归零重启',
    BOOT_ABNORMAL: '异常死亡后重启（崩溃/被系统杀）',
    BOOT_COLD: '冷启动（人工/面板/开机自启）',
    BOOT_PARALLEL: '并发实例（上一个实盘进程还活着）',
    BOOT_UNKNOWN: '启动原因未知',
}


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, '') or default))
    except ValueError:
        logger.warning(f'[生命周期] 环境变量 {name} 不是数字，按默认 {default} 处理')
        return default


# 归因窗口
EXIT_FRESH_SEC = _env_int('CRYPTO_EXIT_FRESH_SEC', 900)
HB_FRESH_SEC = _env_int('CRYPTO_HB_FRESH_SEC', 420)

# 自愈策略参数
NIGHT_START_HOUR = _env_int('CRYPTO_RESUME_NIGHT_START_HOUR', 0)
NIGHT_END_HOUR = _env_int('CRYPTO_RESUME_NIGHT_END_HOUR', 8)
NIGHT_DELAY_SEC = float(_env_int('CRYPTO_RESUME_NIGHT_DELAY_SEC', 20))
DAY_DELAY_SEC = float(_env_int('CRYPTO_RESUME_DAY_DELAY_SEC', 600))
RESUME_MAX = _env_int('CRYPTO_RESUME_MAX', 3)
RESUME_WINDOW_SEC = _env_int('CRYPTO_RESUME_WINDOW_SEC', 6 * 3600)

_boot_lock = threading.Lock()
_BOOT_INFO = None
_SIGTERM_INSTALLED = False


# =============================================================================
# 路径（一律用函数取，便于测试运行时改 LOG_DIR）
# =============================================================================

def exit_marker_path():
    return os.path.join(LOG_DIR, EXIT_MARKER_NAME)


def boot_ledger_path():
    return os.path.join(LOG_DIR, BOOT_LEDGER_NAME)


def heartbeat_path():
    return os.path.join(LOG_DIR, HEARTBEAT_FILE_NAME)


# =============================================================================
# 时间工具
# =============================================================================

def _now_str(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime(TS_FMT)


def _to_epoch(text):
    if not text:
        return None
    try:
        return datetime.strptime(str(text), TS_FMT).timestamp()
    except (TypeError, ValueError):
        return None


def is_night_window(now=None) -> bool:
    """当前钟点是否落在「夜间免打扰窗口」内（默认 00:00~08:00，支持跨零点配置）"""
    hour = datetime.fromtimestamp(now if now is not None else time.time()).hour
    if NIGHT_START_HOUR <= NIGHT_END_HOUR:
        return NIGHT_START_HOUR <= hour < NIGHT_END_HOUR
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


# =============================================================================
# 退出标记：写、读、消费、硬退
# =============================================================================

def _atomic_write_json(path, payload, fsync=True):
    """临时文件 + os.replace 原子落盘；崩溃在写一半时只会留下 .tmp，不会毁掉旧内容"""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
        f.flush()
        if fsync:
            os.fsync(f.fileno())
    os.replace(tmp, path)


def write_exit_marker(reason, quiet=False, **extra) -> bool:
    """退出前落原因。失败只记日志（后果是本次重启被归因成 abnormal_death，可接受）

    ``quiet=True`` 供信号上下文使用：那里一行日志都不能打 —— 信号 handler 是在
    主线程字节码边界上跑的，若它恰好打断了另一处正在写日志的动作，再进 logging
    就是拿不可重入锁，会当场挂死（连标记都写不出去）。内存读也一样：
    信号上下文只做“写一个几百字节 JSON”这一件最小动作。
    """
    payload = {
        'reason': str(reason),
        'ts': _now_str(),
        'epoch': time.time(),
        'pid': os.getpid(),
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    try:
        _atomic_write_json(exit_marker_path(), payload)
        return True
    except Exception as e:
        if not quiet:
            logger.error(f'[生命周期] 退出标记写入失败（重启后归因会退化成异常死亡）: {e}')
        return False


def read_exit_marker():
    """读退出标记；不存在/损坏一律返回 None（不抛）"""
    try:
        with open(exit_marker_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f'[生命周期] 退出标记读取失败，按无标记处理: {e}')
        return None


def _marker_ts(marker):
    epoch = marker.get('epoch')
    if isinstance(epoch, (int, float)):
        return float(epoch)
    return _to_epoch(marker.get('ts'))


def _consume_marker(marker, now):
    """标记一旦被本次启动用上就打 consumed_at 戳。

    不打这个戳的话：撞内存起来（消费掉标记）后紧接着又崩一次，第二次重启仍会
    在那 15 分钟新鲜窗口里读到同一份标记，被误判成「又是内存超限」。
    """
    try:
        merged = dict(marker)
        merged['consumed_at'] = _now_str(now)
        _atomic_write_json(exit_marker_path(), merged, fsync=False)
    except Exception as e:
        logger.warning(f'[生命周期] 退出标记打消费戳失败（不影响本次归因）: {e}')


def _flush_all_logging():
    """os._exit 不给 logging 自动收尾的机会，逐 handler 强制 flush"""
    handlers = list(logging.getLogger().handlers)
    for lg in list(logging.Logger.manager.loggerDict.values()):
        handlers.extend(list(getattr(lg, 'handlers', None) or []))
    for h in handlers:
        try:
            h.flush()
        except Exception:
            pass


def hard_exit(reason, extra=None, exit_code=1):
    """写退出标记 → 刷日志 → ``os._exit(exit_code)``，交给外部管理器拉起。

    ⚠️ ``os._exit`` 跳过 atexit、线程 join、Flask 收尾：调用前必须把该记的账、
    该发的信做完。本函数正常情况下不返回。
    """
    write_exit_marker(reason, **(extra or {}))
    _flush_all_logging()
    os._exit(exit_code)


# =============================================================================
# 外部管理器优雅停止（SIGTERM）：把“人去面板点了重启”从“异常死亡”里分出来
# =============================================================================

def _restore_default_and_reraise(signum):
    """把终止动作交还给系统：绝不允许因为我们的 handler 而把停止请求吞掉

    supervisord / 宝塔停止、systemctl stop、容器 stop 都靠 SIGTERM。一旦注册了
    handler，Python 默认的“立即终止”就不再发生，所以必须自己恢复默认处理后
    重新发一次同信号：第一次之后 handler 已是 SIG_DFL，第二次催停直接终止。
    """
    try:
        signal.signal(signum, signal.SIG_DFL)
    except Exception:
        pass
    try:
        os.kill(os.getpid(), int(signum))
    except Exception:
        os._exit(0)


def _sigterm_handler(signum, frame):
    """信号上下文：只写标记（quiet，不打日志、不读 RSS、不发信），然后让系统收尾"""
    try:
        write_exit_marker(REASON_MANUAL_EXIT, quiet=True, source='sigterm',
                          signal_no=int(signum))
    except BaseException:
        pass
    _restore_default_and_reraise(signum)


def install_sigterm_marker() -> bool:
    """在主线程挂 SIGTERM 捕获（只能在主线程，app.py 导入阶段调用）

    装了之后，“面板重启”会留下 ``manual_exit`` 标记，下次启动归因为
    ``cold_start``（人工/面板重启）而不是 ``abnormal_death``（崩溃/被系统杀）——
    两者处置相同（按时间窗接回），但标签差得远：前者是“人自己干的”，后者会让人
    以“又崩了”去查日志。没装成功（非主线程 / Windows 部分环境）也不报错，
    代价回到原来的保守路径。

    ⚠ 只抓 SIGTERM，不抓 SIGINT：Ctrl+C 在本地开发里靠 KeyboardInterrupt 收尾，
    抢掉它会让“停不掉开发服务器”变成新问题。
    """
    global _SIGTERM_INSTALLED
    if _SIGTERM_INSTALLED:
        return True
    try:
        old = signal.getsignal(signal.SIGTERM)
        if old not in (signal.SIG_DFL, signal.SIG_IGN):
            # 已经有别人（如 WSGI 服务器）占了 SIGTERM：不抢它的语义，只放弃自己
            logger.info('[生命周期] SIGTERM 已被其他组件接管，不抢处理器')
            return False
        signal.signal(signal.SIGTERM, _sigterm_handler)
        _SIGTERM_INSTALLED = True
        logger.info('[生命周期] 已捕获 SIGTERM：优雅停止会留下 manual_exit 退出标记')
        return True
    except Exception as e:
        # 非主线程会抛 ValueError（signal only works in main thread），属于正常回退
        logger.info(f'[生命周期] SIGTERM 捕获未安装（不影响其余功能）: {e}')
        return False


# =============================================================================
# 心跳与启动台账
# =============================================================================

def _tail_lines(path, max_bytes=262144):
    """读文件末尾若干行（jsonl 会滚动到几万行，不能整文件读进内存）"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
    except OSError as e:
        logger.warning(f'[生命周期] 尾部读取失败 {path}: {e}')
        return []
    lines = raw.decode('utf-8', errors='replace').splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]   # 从中间截断的首行不完整，丢掉
    return lines


def _scan_heartbeat(before_ts=None, need_pid=False):
    """从心跳文件尾部挑"上一条有效心跳"，返回 (epoch, 记录)；没有则 (None, None)。

    ``before_ts`` 默认取本进程出生时刻再减一点余量，把自己刚写的行排除掉。

    ``need_pid=True`` 时只挑带 pid 的那条。这不是锦上添花：``embedded`` 来源
    （应用内的系统监控写的内存水位）根本不带 pid，而它每分钟都比带 pid 的 ``app``
    心跳晚几秒落盘 —— 生产上"最新一条"几乎总是 embedded。若判存活只看最新一条，
    那条分支等于没接线（2026-09-11 第一版就是这么废的，拿真实心跳文件做 A/B 才暴露）。
    """
    cutoff = (before_ts if before_ts is not None else _BOOT_TS) - _SELF_RECORD_GRACE_SEC
    best_ts, best_rec = None, None
    for line in _tail_lines(heartbeat_path()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get('source') not in HEARTBEAT_SOURCES:
            continue
        if need_pid and not rec.get('pid'):
            continue
        ts = _to_epoch(rec.get('timestamp'))
        if ts is None or ts > cutoff:
            continue
        if best_ts is None or ts > best_ts:
            best_ts, best_rec = ts, rec
    return best_ts, best_rec


def last_heartbeat(before_ts=None):
    """上一个进程留下的最后一行心跳（不论来源），返回 (epoch, 记录)"""
    return _scan_heartbeat(before_ts)


def last_heartbeat_with_pid(before_ts=None):
    """最后一行**带作者 pid** 的心跳 —— 判"上个进程还活着吗"只能用这条"""
    return _scan_heartbeat(before_ts, need_pid=True)


def _append_ledger(entry: dict) -> bool:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(boot_ledger_path(), 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        return True
    except Exception as e:
        logger.warning(f'[生命周期] 启动台账写入失败（不影响本次归因）: {e}')
        return False


def read_ledger(max_lines=400):
    """读最近若干条启动台账（时间正序）；读不到返回空列表"""
    out = []
    for line in _tail_lines(boot_ledger_path()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[-max_lines:]


def selfheal_boots(window_sec=None, now=None):
    """窗口内自愈类启动次数（含本次）。

    台账读不到就返回 0 —— 这里选择「不因缺数据而熔断」，因为熔断的目的是拦
    「反复撞同一堵墙」，而拦错的代价是本来该恢复的实盘不恢复了（静默停摆，
    正是要修的那个毛病）。是否自动拉起的总闸门仍是页面开关 + 期望运行状态。
    """
    window_sec = window_sec or RESUME_WINDOW_SEC
    now = now if now is not None else time.time()
    count = 0
    for rec in read_ledger():
        if rec.get('reason') not in SELFHEAL_REASONS:
            continue
        ts = _to_epoch(rec.get('ts'))
        if ts is None or now - ts > window_sec:
            continue
        count += 1
    return count


# =============================================================================
# 归因主流程
# =============================================================================

def _pid_alive(pid, born_before=None):
    """判断某个 pid 现在是否还活着（并且是在 ``born_before`` 之前就存在的）。

    用途：区分「上一个进程暴死」与「上一个进程活得好好的、本次是并发的第二
    个实例」。心跳记录里带着写它的 ``pid``，所以作者是否存活是可以直接问系统的
    —— 不必再靠"心跳很新 ⇒ 有人死了"这种推断。

    为什么不用 ``os.kill(pid, 0)`` 这种 POSIX 惯用探活写法：本机 Python 3.13.1
    实测 ``os.kill(pid, 0)`` 与 ``os.kill(pid, 1)`` 都没有终止子进程，而 ``os.kill``
    的官方文档写的是「Windows 上除 CTRL_C_EVENT/CTRL_BREAK_EVENT 外任意值都会经
    TerminateProcess 无条件杀进程」——**说法与实测不一致，说明该语义跨版本变过**，
    一个语义不稳的 API 不该拿来探测正在实盘下单的进程。况且就算能判存在，它也
    给不出出生时刻（挡不住 PID 复用）、分不清"已退出但句柄未回收"。psutil 三样都有。

    ``born_before`` 用来挡 PID 复用：系统重启后 pid 会被重新分配，一个刚出生的
    无关进程顶着老 pid 不算"作者还活着"。

    取不到结论时一律返回 False —— 保持本次改动之前的行为（按异常死亡处理）。
    反方向猜错的代价是"本该自愈的没自愈"（静默停摆），正是这套机制要防的毛病。
    """
    if not pid:
        return False
    try:
        import psutil
    except ImportError:
        logger.warning('[生命周期] 未安装 psutil，无法判断上个进程是否存活，按异常死亡处理')
        return False
    try:
        if not psutil.pid_exists(int(pid)):
            return False
        if born_before is None:
            return True
        return psutil.Process(int(pid)).create_time() <= float(born_before)
    except Exception:
        # 进程刚好退出 / pid 越界 / 无权限查询：都按"不存活"处理
        return False


def classify_boot(now=None):
    """判定本次启动是怎么起来的（纯读，不写台账）"""
    now = now if now is not None else time.time()
    marker = read_exit_marker()
    hb_ts, hb_rec = last_heartbeat(now)
    # 判"上个进程还活着吗"必须看带 pid 的那条心跳，不能用 hb_rec：见 _scan_heartbeat
    pid_ts, pid_rec = last_heartbeat_with_pid(now)

    info = {
        'reason': BOOT_UNKNOWN,
        'label': BOOT_LABELS[BOOT_UNKNOWN],
        'detail': '',
        'boot_ts': _now_str(now),
        'boot_seq': 0,
        'prev_exit': None,
        'last_heartbeat': _now_str(hb_ts) if hb_ts else None,
        'heartbeat_lag_sec': int(now - hb_ts) if hb_ts else None,
        'heartbeat_rss_mb': (hb_rec or {}).get('rss_mb'),
        # 写最近一条带 pid 心跳的进程号：判断"上个进程是否真的死了"全靠它
        'heartbeat_pid': (pid_rec or {}).get('pid'),
        'heartbeat_pid_age_sec': int(now - pid_ts) if pid_ts else None,
        'night_window': is_night_window(now),
    }

    fresh_marker = None
    if marker:
        mts = _marker_ts(marker)
        if (mts is not None and not marker.get('consumed_at')
                and now - mts <= EXIT_FRESH_SEC):
            fresh_marker = marker

    if fresh_marker:
        reason = str(fresh_marker.get('reason') or '')
        info['prev_exit'] = {
            'reason': reason,
            'ts': str(fresh_marker.get('ts') or ''),
            'pid': fresh_marker.get('pid'),
            'rss_mb': fresh_marker.get('rss_mb'),
            'kill_mb': fresh_marker.get('kill_mb'),
        }
        if reason == REASON_MEM_KILL:
            info['reason'] = BOOT_MEM
            info['detail'] = (f"上次由内存看门狗强退（RSS {fresh_marker.get('rss_mb')}MB"
                              f" > 阈值 {fresh_marker.get('kill_mb')}MB）")
        elif reason == REASON_DAILY_RESTART:
            info['reason'] = BOOT_DAILY
            info['detail'] = '上次是每日主动归零重启（计划内动作）'
        else:
            info['reason'] = BOOT_COLD
            info['detail'] = f'上次以 {reason or "未记录"} 原因主动退出'
        _consume_marker(fresh_marker, now)
        info['label'] = BOOT_LABELS[info['reason']]
        return info

    if hb_ts is None:
        info['reason'] = BOOT_FIRST
        info['detail'] = '本机从未有过交易进程心跳记录，按初次上线处理'
        info['label'] = BOOT_LABELS[BOOT_FIRST]
        return info

    lag = int(now - hb_ts)
    if lag <= HB_FRESH_SEC:
        hb_pid = (pid_rec or {}).get('pid')
        pid_age = int(now - pid_ts) if pid_ts is not None else None
        if (hb_pid and str(hb_pid) != str(os.getpid())
                and pid_age is not None and pid_age <= HB_FRESH_SEC
                and _pid_alive(hb_pid, born_before=_BOOT_TS)):
            # 心跳作者仍存活 → 死的不是它，本次是「同机第二个实例」。
            # 旧口径只看"心跳新不新"，把并发启动直接读成暴死：每并发一次就吃
            # 一格自愈熔断额度（6h/3 次），额度用完后真需要自愈时不再自动接回，
            # 整台机器的实盘静默停摆 —— 2026-09-11 就是这么在 4 分钟内烧掉两格的。
            info['reason'] = BOOT_PARALLEL
            info['detail'] = (f'没有退出标记，且 {lag}s 前还在写心跳的进程 pid={hb_pid} '
                              f'仍然存活 → 本次启动是并发的第二个实例，不是重启；'
                              f'不计入熔断额度，也不自动拉起交易（避免两个调度器对同一'
                              f'账户重复下单）。请确认只保留一个 app.py 实例')
            info['label'] = BOOT_LABELS[BOOT_PARALLEL]
            return info
        info['reason'] = BOOT_ABNORMAL
        info['detail'] = (f'没有退出标记，但 {lag}s 前还在写心跳 → 判定为异常死亡'
                          f'（代码崩溃 / 被内核 OOM kill -9 / 外部强杀）')
        info['label'] = BOOT_LABELS[BOOT_ABNORMAL]
        return info

    info['reason'] = BOOT_COLD
    info['detail'] = (f'距上次心跳已 {lag // 60} 分钟，判定为停机后冷启动'
                      f'（人工启动 / 面板重启 / 开机自启）')
    info['label'] = BOOT_LABELS[BOOT_COLD]
    return info


def record_boot(info: dict) -> int:
    """追加启动台账并回填 boot_seq；失败只记日志，返回 0"""
    seq = len(read_ledger()) + 1
    info['boot_seq'] = seq
    _append_ledger({
        'ts': info.get('boot_ts'),
        'seq': seq,
        'reason': info.get('reason'),
        'prev_exit': (info.get('prev_exit') or {}).get('reason'),
        'heartbeat_lag_sec': info.get('heartbeat_lag_sec'),
        # 一并记下"最后那条心跳是谁写的"：事后要区分「暴死」还是「并发实例」，
        # 光看 lag 是猜不出来的，别留着下次还得靠查进程列表反推
        'heartbeat_pid': info.get('heartbeat_pid'),
        'pid': os.getpid(),
    })
    return seq


def classify_and_record_boot(force=False):
    """每个进程生命周期只归因一次（结果缓存），供调度器与状态接口共用"""
    global _BOOT_INFO
    with _boot_lock:
        if _BOOT_INFO is not None and not force:
            return _BOOT_INFO
        try:
            info = classify_boot()
        except Exception as e:
            logger.error(f'[生命周期] 启动归因异常，按 unknown 处理（不自动拉起）: {e}')
            info = {'reason': BOOT_UNKNOWN, 'label': BOOT_LABELS[BOOT_UNKNOWN],
                    'detail': f'归因异常: {e}', 'boot_ts': _now_str(), 'boot_seq': 0,
                    'prev_exit': None, 'last_heartbeat': None,
                    'heartbeat_lag_sec': None, 'night_window': is_night_window()}
        try:
            record_boot(info)
        except Exception as e:
            logger.warning(f'[生命周期] 启动台账登记失败（不影响本次自愈判断）: {e}')
        _BOOT_INFO = info
        logger.info(f"[生命周期] 本次启动归因: {info['reason']} | {info['detail']}")
        return info


def get_boot_info():
    """已算好的归因结果；未算过返回 None（不在此处触发落账，避免重复记账）"""
    return _BOOT_INFO


def set_boot_info(info):
    """外部（app.py 启动线程）算好后回填缓存，让状态接口能读到"""
    global _BOOT_INFO
    with _boot_lock:
        _BOOT_INFO = info
    return info


# =============================================================================
# 自愈策略：按归因结论 + 时间窗决定「起不起、等多久、先不发信」
# =============================================================================

def plan_resume(info=None, now=None):
    """返回本次是否自动拉起实盘、延时多少、要不要先发预告信。

    决策口径（与《实盘自愈与内存治理方案》一致）：
    - ``first_run``      → 不起也不发信：初次部署绝不自动开始下单；
    - ``daily_restart``  → 立刻接回且不发信：计划内动作，天天发信等于制造噪音；
    - ``mem_restart`` / ``abnormal_death`` / ``cold_start`` → 看钟点：
        夜间窗口（默认 00:00~08:00）只等 20s 直接接回（人在睡觉，早接早少漏）；
        白天先寄一封预告信再等 600s（这 10 分钟是留给"先看持仓挂单对不对"的
        人工否决窗，期间在页面点了「启动交易」则以人工为准）。
    - 熔断：窗口内自愈类重启达到上限就只发信不起（反复重启说明环境没修好）。
    - 归因缺失/未知 → allowed=False（保守方向：与改造前"必须人工点启动"一致）。
    """
    info = info if isinstance(info, dict) else (get_boot_info() or {})
    now = now if now is not None else time.time()
    reason = info.get('reason') or BOOT_UNKNOWN
    night = bool(info.get('night_window')) if 'night_window' in info else is_night_window(now)
    delay = NIGHT_DELAY_SEC if night else DAY_DELAY_SEC

    plan = {
        'reason': reason,
        'allowed': False,
        'delay_seconds': delay,
        'pre_notify': not night,
        'silent': False,
        'window': 'night' if night else 'day',
        'selfheal_count': 0,
        'why': '',
        'resume_at': '',
    }

    if reason == BOOT_FIRST:
        plan['silent'] = True
        plan['why'] = '初次上线，实盘一律等人工点「启动交易」，不自动下单'
        return plan

    if reason == BOOT_PARALLEL:
        # 并发实例：真正在跑的那个没死，交易状态由它负责，本进程绝不能再起一个
        # 调度器（两个调度器对同一账户下单会重复开仓）。不发预告信（本就不打算拉起），
        # 但不静默 —— 信里要说清"实盘没停，只是多开了一个实例"，否则运维会以为停摆了。
        plan['pre_notify'] = False   # 显式清掉默认值：别让计划字段声称会预告
        plan['why'] = (
            f"检测到同机已有实盘进程在跑（最后心跳作者 pid={info.get('heartbeat_pid')} 仍存活），"
            f"本次启动按「并发第二个实例」处理：不自动拉起交易，也不计入重启熔断额度。"
            f"原本那个实例不受影响、继续在跑；若你本意是重启实盘，请先停掉旧实例"
            f"（持有 Web 端口的那个）再起新的，别用「再起一个」代替「重启」")
        return plan

    if reason not in (BOOT_MEM, BOOT_ABNORMAL, BOOT_DAILY, BOOT_COLD):
        plan['why'] = f'启动原因未查明（{reason}），按保守处理：不自动拉起'
        return plan

    count = selfheal_boots(now=now)
    plan['selfheal_count'] = count
    if reason in SELFHEAL_REASONS and count >= RESUME_MAX:
        plan['why'] = (f'{RESUME_WINDOW_SEC // 3600} 小时内已是第 {count} 次自愈类重启'
                       f'（上限 {RESUME_MAX} 次）→ 停止自动拉起：反复重启说明环境本身'
                       f'没修好，继续自动起等于在坏环境里反复真实下单。'
                       f'请打开 /system-status 看内存增长趋势定位泄漏点（必要时建空文件 '
                       f'logs/memtrace.on 抓强退前的分配快照，多份快照对比即可锁定增长点），'
                       f'确认修好后再在 /task 页面手动点「启动交易」')
        return plan

    plan['allowed'] = True
    if reason == BOOT_DAILY:
        plan['delay_seconds'] = NIGHT_DELAY_SEC
        plan['pre_notify'] = False
        plan['silent'] = True
        plan['why'] = '每日主动归零重启，起来直接接回实盘（计划内动作，不发信）'
    elif night:
        plan['pre_notify'] = False
        plan['why'] = (f'夜间窗口 {NIGHT_START_HOUR:02d}:00-{NIGHT_END_HOUR:02d}:00，'
                       f'{NIGHT_DELAY_SEC:g}s 后自动接回实盘')
    else:
        plan['why'] = (f'白天窗口：先发预告邮件，等 {DAY_DELAY_SEC:g}s 再自动接回；'
                       f'这期间你若在页面点了「启动交易」，则以人工启动为准')
    plan['resume_at'] = _now_str(now + plan['delay_seconds'])
    return plan
