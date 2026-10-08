#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程内存自监控（Memory Watchdog）
==================================
后台守护线程每 2 分钟检查一次本进程物理内存（RSS，跨平台）：

1. 每次检查输出结构化日志，并追加一条记录到本地 JSON 日志文件
   ``logs/memory_history.jsonl`` —— 即使进程被系统 OOM 杀掉，
   趋势证据仍保留在磁盘上，供 /system-status 页面回溯泄漏曲线。
   这份记录同时被 ``process_lifecycle`` 当作**心跳**使用：进程被外部强杀时
   来不及留任何退出说明，「心跳停在什么时候」就是它死掉的时间。
2. RSS 超过 warn_mb（默认 600MB）：发送告警邮件（30 分钟冷却防刷屏）。
3. RSS 超过 kill_mb（默认 800MB）：记录日志 + 发送告警邮件，再由
   ``process_lifecycle.hard_exit('mem_kill')`` 先落退出标记、然后
   ``os._exit(1)`` 强制退出，由外部管理器（supervisord，需配置
   autorestart=true）自动拉起，完成内存重置。

为什么检查间隔是 2 分钟而不是 5 分钟：pandas/backtrader 一轮批量分析能让
RSS 在一两分钟内跳 200~300MB，5 分钟粒度下「撞到 800 才发现」实际峰值往往
已经 1.1G 以上，在 2G 机器上这就到了内核 OOM killer 的射程内 —— 被 ``kill -9``
就轮不到我们体面地留下退出原因了。一次 ``/proc`` 读取的开销可以忽略。

环境变量（可选覆盖）：
- CRYPTO_MEM_WARN_MB    预警阈值（MB）
- CRYPTO_MEM_KILL_MB    强退阈值（MB）
- CRYPTO_MEM_CHECK_SEC  检查间隔（秒）
- CRYPTO_MEM_TRACE=1    启用 tracemalloc（排查内存泄漏时开启，有性能开销），
                        强退前自动导出内存占用 Top 列表到 logs/ 目录；
                        且超过预警阈值后每 30 分钟定期导出一份快照，
                        多份快照对比即可定位泄漏增长点。
                        等价文件开关（面板部署不便设环境变量时用）：
                        创建空文件 logs/memtrace.on 即启用，删除该文件并重启进程即关闭。
- 按需快照：trace 启用期间创建空文件 logs/memdump.now，
  监控线程会在 5 分钟内导出一份快照（用于基线/中途对比），并消耗该触发文件。
"""

import os
import sys
import json
import time
import threading
import logging
import importlib.util
from datetime import datetime
from collections import deque

logger = logging.getLogger(__name__)

# 生命周期归因层：强退前必须把「我是被内存撞线杀掉的」这条因果落到磁盘上，
# 否则新进程起来只能靠猜。取不到时退化为直接 os._exit（丢失归因，不丢功能）。
try:
    from . import process_lifecycle as _lifecycle
except Exception:  # pragma: no cover - 包外裸模块身份时的兼容分支
    try:
        import process_lifecycle as _lifecycle
    except Exception:
        _lifecycle = None

_CRYPTO_DIR = os.path.dirname(os.path.abspath(__file__))
_TASK_DIR = os.path.join(_CRYPTO_DIR, 'task')
# 历史日志目录约定：crypto/logs/（与 task_scheduler.log 同级）
LOG_DIR = os.path.join(_CRYPTO_DIR, 'logs')
HISTORY_FILENAME = 'memory_history.jsonl'

DEFAULT_WARN_MB = 600
DEFAULT_KILL_MB = 800
DEFAULT_INTERVAL_SEC = 120      # 每 2 分钟检查一次（见文件头「为什么不是 5 分钟」）
EMAIL_COOLDOWN_SEC = 30 * 60    # 同类邮件冷却 30 分钟

# JSONL 体积控制：每 2 分钟 1 条（外加系统监控每 60s 一条），5 万条约可回溯数月
HISTORY_MAX_LINES = 50000
HISTORY_KEEP_LINES = 35000

_state = {
    'started': False,
    'started_at': None,
    'last_check': None,
    'last_rss_mb': None,
    'warn_mb': DEFAULT_WARN_MB,
    'kill_mb': DEFAULT_KILL_MB,
    'interval_sec': DEFAULT_INTERVAL_SEC,
}
_last_email_ts = 0.0
_last_dump_ts = 0.0
_lock = threading.Lock()


# =============================================================================
# glibc 分配器治理（仅 Linux）：切断 "RSS 只升不降" 的分配器棘轮
# -----------------------------------------------------------------------------
# CPython 没有 JVM 式压缩 GC，存在两个棘轮：
# 1) glibc 动态 mmap 阈值在大块 free 后被上调，之后同尺寸分配落入 brk 主堆，
#    brk 只升不降；
# 2) pymalloc 256KB arena 需整块全空才归还 OS，churn 后残留活对象即被永久扣住。
# 这些机制可以造成峰值残留，但不能据此排除业务强引用泄漏。
# Backtrader 动态类注册等强引用必须在源头修复，gc/trim 无法释放它们。
# 分配器侧对策：mallopt 固定 mmap 阈值禁用动态上调 + 限制 arena 数 +
#       周期 gc.collect / malloc_trim 把已释放堆归还 OS。
# =============================================================================

_M_MMAP_THRESHOLD = -3    # glibc mallopt 参数编号
_M_TRIM_THRESHOLD = -1
_M_ARENA_MAX = -8
_MMAP_THRESHOLD_BYTES = 128 * 1024

_libc = None


def _get_libc():
    global _libc
    if _libc is None and sys.platform.startswith('linux'):
        import ctypes
        _libc = ctypes.CDLL('libc.so.6')
    return _libc


def tune_glibc_allocator() -> bool:
    """固定 glibc mmap 阈值并限制 arena 数（进程启动时调用；非 Linux 无操作）"""
    libc = _get_libc()
    if libc is None:
        return False
    try:
        libc.mallopt(_M_MMAP_THRESHOLD, _MMAP_THRESHOLD_BYTES)
        libc.mallopt(_M_TRIM_THRESHOLD, _MMAP_THRESHOLD_BYTES)
        libc.mallopt(_M_ARENA_MAX, 2)
        logger.info("[内存监控] glibc 分配器治理已启用（mmap 阈值固定 128KB / arenas=2）")
        return True
    except Exception as e:
        logger.warning(f"[内存监控] glibc 分配器治理失败: {e}")
        return False


def release_free_memory() -> bool:
    """清理过期行情并执行 GC；Linux 额外用 malloc_trim 归还空闲堆。"""
    import gc
    # 只清理已加载的行情缓存，不因巡检导入策略或创建网络客户端。
    for name in ('pro3_singletimeframe', 'crypto.strategy.pro3_singletimeframe'):
        module = sys.modules.get(name)
        prune = getattr(module, 'prune_kline_caches', None)
        if prune is not None:
            try:
                prune()
            except Exception as exc:
                logger.warning('[内存监控] 行情缓存清理失败: %s', exc)
    gc.collect()
    libc = _get_libc()
    if libc is None:
        return False
    try:
        return bool(libc.malloc_trim(0))
    except Exception:
        return False


# =============================================================================
# RSS 读取（跨平台）
# =============================================================================

def get_process_rss_mb(pid=None):
    """读取指定进程（默认当前进程）物理内存（RSS），单位 MB；失败返回 None"""
    target_pid = pid or os.getpid()
    # Linux：读 /proc/<pid>/status 的 VmRSS
    try:
        with open(f'/proc/{target_pid}/status', encoding='utf-8') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    # Windows：psapi.GetProcessMemoryInfo（仅支持当前进程）
    if pid is None and sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PMC()
            pmc.cb = ctypes.sizeof(pmc)
            kernel32 = ctypes.windll.kernel32
            # GetCurrentProcess 返回伪句柄 -1，必须声明 restype 为 HANDLE（指针宽），
            # 否则默认 c_int 在 64 位下被截断，GetProcessMemoryInfo 会直接失败
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            handle = kernel32.GetCurrentProcess()
            gpmi = ctypes.windll.psapi.GetProcessMemoryInfo
            gpmi.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
            if gpmi(handle, ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize / (1024.0 * 1024.0)
        except Exception:
            pass
    return None


# =============================================================================
# 历史日志（JSONL，进程被杀也保留泄漏趋势证据）
# =============================================================================

def history_file_path():
    return os.path.join(LOG_DIR, HISTORY_FILENAME)


def append_history_record(record: dict):
    """追加一条采样记录到 memory_history.jsonl（单行 JSON，append 原子写）"""
    path = history_file_path()
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        _rotate_history_if_needed(path)
    except OSError as e:
        logger.warning(f"[内存监控] 历史记录写入失败: {e}")


def _rotate_history_if_needed(path):
    """超限时保留表头无关（JSONL 无表头），直接截留最近 KEEP 行"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            if sum(1 for _ in f) <= HISTORY_MAX_LINES:
                return
        with open(path, 'r', encoding='utf-8') as f:
            lines = deque(f, maxlen=HISTORY_KEEP_LINES)
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
    except OSError:
        pass


def read_history(limit: int = 1000, source: str = None):
    """读取最近 limit 条历史记录（时间正序），可按 source 过滤：app | guard"""
    if limit <= 0:
        return []
    path = history_file_path()
    records = deque(maxlen=limit)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if source and rec.get('source') != source:
                    continue
                records.append(rec)
    except OSError as e:
        if os.path.exists(path):
            logger.warning(f"[内存监控] 历史记录读取失败: {e}")
        return []
    return list(records)


# =============================================================================
# 邮件告警（复用项目 EmailTool；按文件路径加载，避免拖入业务包依赖）
# =============================================================================

def send_alert_email(subject: str, content: str) -> bool:
    """发送告警邮件；失败仅记日志，绝不影响监控主流程"""
    try:
        if _TASK_DIR not in sys.path:
            sys.path.insert(0, _TASK_DIR)
        spec = importlib.util.spec_from_file_location(
            'crypto_email_tool',
            os.path.join(_TASK_DIR, 'notification', 'email_tool.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return bool(mod.EmailTool().send_system_notification(
            content=content, subject=subject))
    except Exception as e:
        logger.error(f"[内存监控] 告警邮件发送失败: {e}")
        return False


def _try_warn_email(cooldown_ok: bool, subject: str, content: str) -> bool:
    """受冷却约束的预警邮件"""
    global _last_email_ts
    with _lock:
        if not cooldown_ok or time.time() - _last_email_ts < EMAIL_COOLDOWN_SEC:
            return False
        ok = send_alert_email(subject, content)
        if ok:
            _last_email_ts = time.time()
        return ok


# =============================================================================
# tracemalloc 泄漏快照（CRYPTO_MEM_TRACE=1 或 logs/memtrace.on 时启用）
# =============================================================================

# 按需快照触发文件：trace 启用期间创建该空文件，5 分钟内导出一份快照
DUMP_TRIGGER_FILENAME = 'memdump.now'


def _tracemalloc_enabled():
    if os.environ.get('CRYPTO_MEM_TRACE', '').strip() == '1':
        return True
    # 文件开关（面板部署不便设环境变量时用）：创建 logs/memtrace.on 即启用
    try:
        return os.path.exists(os.path.join(LOG_DIR, 'memtrace.on'))
    except OSError:
        return False


def dump_tracemalloc_to_file():
    """导出内存占用 Top30 到 logs/memdump_*.txt，返回文件路径；未启用返回 None"""
    if not _tracemalloc_enabled():
        return None
    try:
        import tracemalloc
        if not tracemalloc.is_tracing():
            return None
        top = tracemalloc.take_snapshot().statistics('lineno')[:30]
        path = os.path.join(
            LOG_DIR, f'memdump_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt')
        rss = get_process_rss_mb()
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"当前RSS: {rss:.1f}MB\n\n" if rss else "\n")
            for i, stat in enumerate(top, 1):
                f.write(f"{i:>3}. {stat}\n")
        logger.warning(f"[内存监控] tracemalloc 快照已导出: {path}")
        return path
    except Exception as e:
        logger.error(f"[内存监控] tracemalloc 导出失败: {e}")
        return None


def _consume_dump_trigger() -> bool:
    """检查并消耗按需快照触发文件（logs/memdump.now）"""
    path = os.path.join(LOG_DIR, DUMP_TRIGGER_FILENAME)
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False


# =============================================================================
# 监控主循环
# =============================================================================

def _monitor_loop():
    global _last_dump_ts
    while True:
        try:
            # 周期回收：先把已释放堆归还 OS，再读 RSS，读数才反映真实水位
            release_free_memory()
            rss = get_process_rss_mb()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _state['last_check'] = now_str
            _state['last_rss_mb'] = rss
            if rss is None:
                logger.warning("[内存监控] 无法读取进程内存，跳过本轮")
                time.sleep(_state['interval_sec'])
                continue

            # 趋势记录：即使进程随后被杀，磁盘上仍保留完整增长曲线
            append_history_record({
                'timestamp': now_str, 'rss_mb': round(rss, 1),
                'source': 'app', 'pid': os.getpid(),
            })
            logger.info(
                f"[内存监控] 当前RSS: {rss:.1f}MB / "
                f"预警: {_state['warn_mb']}MB / 强退: {_state['kill_mb']}MB")

            # 按需快照：trace 启用期间创建 logs/memdump.now 即可在 5 分钟内取一份 Top30
            if _tracemalloc_enabled() and _consume_dump_trigger():
                dump_tracemalloc_to_file()

            if rss > _state['kill_mb']:
                # 强退路径：记录 → 导出快照 → 邮件 → os._exit(1) 交由外部管理器重启
                logger.critical(
                    f"[内存监控] 内存超限 {rss:.1f}MB > {_state['kill_mb']}MB，"
                    f"强制退出进程，等待外部管理器自动重启")
                dump_path = dump_tracemalloc_to_file()
                extra = (f"\n内存快照文件: {dump_path}" if dump_path else
                         "\n提示: 设置环境变量 CRYPTO_MEM_TRACE=1 可在下次超限时导出泄漏快照")
                send_alert_email(
                    subject=f"🚨 内存超限自重启 - RSS {rss:.0f}MB",
                    content=(f"交易进程内存超过强退阈值，已主动退出并等待自动重启。\n"
                             f"时间: {now_str}\n"
                             f"当前RSS: {rss:.1f}MB（强退阈值 {_state['kill_mb']}MB）"
                             f"{extra}\n"
                             f"若频繁触发，请打开 /system-status 页面结合历史趋势排查内存泄漏。"))
                if _lifecycle is not None:
                    # 先落退出标记再退：重启后的归因全靠这一行
                    # （hard_exit 内部已 flush 全部 handler 并 os._exit，正常不返回）
                    _lifecycle.hard_exit(_lifecycle.REASON_MEM_KILL, {
                        'rss_mb': round(rss, 1),
                        'kill_mb': _state['kill_mb'],
                        'warn_mb': _state['warn_mb'],
                        'interval_sec': _state['interval_sec'],
                    })
                # 兜底：归因层不可用时仍按老路径退出（本次重启会被归因成异常死亡）
                _handlers = list(logging.getLogger().handlers)
                for _lg in list(logging.Logger.manager.loggerDict.values()):
                    _handlers.extend(list(getattr(_lg, 'handlers', None) or []))
                for _h in _handlers:
                    try:
                        _h.flush()
                    except Exception:
                        pass
                os._exit(1)

            if rss > _state['warn_mb']:
                logger.warning(
                    f"[内存监控] 内存预警 {rss:.1f}MB > {_state['warn_mb']}MB")
                # 泄漏排查模式：超预警后每 30 分钟导出一份快照，
                # 沿增长曲线多份对比定位增长点，不必等强退
                dump_path = None
                if _tracemalloc_enabled() and \
                        time.time() - _last_dump_ts >= EMAIL_COOLDOWN_SEC:
                    _last_dump_ts = time.time()
                    dump_path = dump_tracemalloc_to_file()
                extra = (f"\n泄漏快照: {dump_path}（将该文件发回分析可定位泄漏点）"
                         if dump_path else '')
                _try_warn_email(
                    True,
                    subject=f"⚠️ 内存预警 - RSS {rss:.0f}MB",
                    content=(f"交易进程内存超过预警阈值，请关注是否存在内存泄漏。\n"
                             f"时间: {now_str}\n"
                             f"当前RSS: {rss:.1f}MB（预警阈值 {_state['warn_mb']}MB，"
                             f"强退阈值 {_state['kill_mb']}MB）"
                             f"{extra}\n"
                             f"可访问 /system-status 页面查看内存历史趋势。"))
        except Exception as e:
            logger.error(f"[内存监控] 检查异常: {e}")
        time.sleep(_state['interval_sec'])


def start_memory_watchdog(warn_mb=None, kill_mb=None, interval_sec=None):
    """启动内存监控线程（幂等，重复调用不生效）"""
    with _lock:
        if _state['started']:
            return
        tune_glibc_allocator()
        _state['warn_mb'] = int(os.environ.get(
            'CRYPTO_MEM_WARN_MB', warn_mb or DEFAULT_WARN_MB))
        _state['kill_mb'] = int(os.environ.get(
            'CRYPTO_MEM_KILL_MB', kill_mb or DEFAULT_KILL_MB))
        _state['interval_sec'] = int(os.environ.get(
            'CRYPTO_MEM_CHECK_SEC', interval_sec or DEFAULT_INTERVAL_SEC))
        if _tracemalloc_enabled():
            try:
                import tracemalloc
                # 仅采集 1 层调用栈：泄漏报告用 statistics('lineno') 按分配行聚合，1 层足矣。
                # 曾误用 10 层导致 pandas/backtrader 每次内存分配都记录深栈，
                # 整进程性能塌陷约 14 倍（实测快照 analyze() 从 3s 涨到 46s）。
                tracemalloc.start(1)
                logger.warning("[内存监控] ⚠️ tracemalloc 已启用（泄漏排查模式，有性能开销；"
                               "排查完毕请删除 logs/memtrace.on 并重启以关闭）")
            except Exception as e:
                logger.warning(f"[内存监控] tracemalloc 启用失败: {e}")
        threading.Thread(target=_monitor_loop, daemon=True,
                         name='memory-watchdog').start()
        _state['started'] = True
        _state['started_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        logger.info(f"[内存监控] 已启动（预警 {_state['warn_mb']}MB / "
                    f"强退 {_state['kill_mb']}MB / 间隔 {_state['interval_sec']}s）")


def get_memory_status():
    """当前监控状态摘要（供 /api/system/status 接口使用）"""
    trace = sys.modules.get('tracemalloc')
    tracing = bool(trace and trace.is_tracing())
    return {
        'running': _state['started'],
        'pid': os.getpid(),
        'rss_mb': _state['last_rss_mb'] if _state['last_rss_mb'] is not None
                  else get_process_rss_mb(),
        'warn_mb': _state['warn_mb'],
        'kill_mb': _state['kill_mb'],
        'interval_sec': _state['interval_sec'],
        'started_at': _state['started_at'],
        'last_check': _state['last_check'],
        'tracemalloc_enabled': tracing,
        'tracemalloc_configured': _tracemalloc_enabled(),
        'tracemalloc_traceback_limit': trace.get_traceback_limit() if tracing else 0,
        'history_file': history_file_path(),
    }
