#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统级崩溃监控（System Monitor）
=================================
独立于交易业务逻辑的系统健康检查，每 1 分钟一轮：

1. 交易进程存活状态（外部运行时扫描命令行关键字；进程内嵌入时自动跳过）
2. 系统整体内存使用率 / SWAP 使用率
3. 磁盘空间使用率
4. 系统负载（Linux）

每轮检查追加一条记录到 ``logs/memory_history.jsonl``（source='guard'），
即使交易进程被系统 OOM 杀掉，外部采样仍保留完整趋势证据。
告警遵循“分级监控、严重才报”原则（复用 crypto/task/notification/email_tool.py
邮箱推送；每类严重告警独立 30 分钟冷却，不会刷屏）：
- 仅严重异常发邮件：交易进程已退出 / 系统内存使用率 ≥ 90% / 磁盘剩余 ≤ 5%；
- 其余异常（进程 RSS 偏高、SWAP/负载偏高等）不发邮件，随采样记录写入
  历史 JSONL（issues 字段），由 /system-status 趋势图可视化排查。

两种运行方式：
- 进程内嵌入：app.py 启动时调用 start_system_monitor()，后台线程每 60s 一轮；
  进程崩溃时该线程随之消亡，由下方独立方式兜底。
- 独立脚本（推荐配合 cron，进程崩了也能告警）：
    * * * * * cd /项目路径 && /usr/bin/python3 crypto/system_monitor.py >> crypto/logs/system_monitor_cron.log 2>&1
  进程不存在且配置了 SUPERVISOR_NAME 时，会尝试 ``supervisorctl restart`` 拉起。
"""

import os
import sys
import json
import time
import shutil
import logging
import subprocess
from datetime import datetime

# 兼容包内导入（进程内嵌入）与独立运行（cron）两种模式
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if __package__ in (None, ''):
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)
    from crypto.memory_watchdog import (          # noqa: E402
        append_history_record, get_process_rss_mb, send_alert_email)
else:
    from .memory_watchdog import (
        append_history_record, get_process_rss_mb, send_alert_email)

logger = logging.getLogger(__name__)

LOG_DIR = os.path.join(_SCRIPT_DIR, 'logs')
STATE_FILENAME = 'system_monitor_state.json'

# =============================================================================
# 配置区（按部署环境修改）
# =============================================================================
DEFAULT_INTERVAL_SEC = 60              # 检查周期（秒）
APP_CMD_KEYWORDS = ['app.py']          # 交易进程命令行匹配关键字（须全部包含）
PROCESS_WARN_MB = 800                  # 交易进程 RSS 关注阈值（仅记录，不发邮件）
MEM_USAGE_PCT = 90                     # 系统内存使用率严重阈值（≥ 则邮件告警）
SWAP_USAGE_PCT = 80                    # SWAP 使用率关注阈值（仅记录，不发邮件）
DISK_FREE_PCT = 5                      # 磁盘剩余空间严重阈值（≤ 则邮件告警）
LOAD_PER_CPU = 2.0                     # 平均每核负载关注阈值（仅记录，不发邮件）
EMAIL_COOLDOWN_SEC = 30 * 60           # 同类告警冷却（秒）
SUPERVISOR_NAME = ''                   # supervisord 程序名；填写后进程消失时自动尝试重启

_state = {
    'started': False,
    'last_result': None,
}


# =============================================================================
# 检查项
# =============================================================================

def find_app_pid():
    """按命令行关键字查找交易进程 PID（Linux 扫 /proc；不支持/未找到返回 None）"""
    if not os.path.isdir('/proc'):
        return None
    self_pid = os.getpid()
    for entry in os.listdir('/proc'):
        if not entry.isdigit() or int(entry) == self_pid:
            continue
        try:
            with open(f'/proc/{entry}/cmdline', 'rb') as f:
                cmd = f.read().decode('utf-8', errors='replace').replace('\x00', ' ')
            if (cmd and all(k in cmd for k in APP_CMD_KEYWORDS)
                    and 'system_monitor' not in cmd):
                return int(entry)
        except (OSError, ValueError):
            continue
    return None


def read_system_memory():
    """返回 (内存使用率%, SWAP使用率%)；无法获取时对应项为 None"""
    # Linux：/proc/meminfo
    try:
        info = {}
        with open('/proc/meminfo', encoding='utf-8') as f:
            for line in f:
                k, v = line.split(':', 1)
                info[k.strip()] = int(v.strip().split()[0])
        total, avail = info['MemTotal'], info.get('MemAvailable', 0)
        mem_pct = (total - avail) / total * 100 if total else None
        st, sf = info.get('SwapTotal', 0), info.get('SwapFree', 0)
        swap_pct = (st - sf) / st * 100 if st else 0.0
        return mem_pct, swap_pct
    except (OSError, ValueError, KeyError):
        pass
    # Windows：GlobalMemoryStatusEx（无独立 SWAP 口径，返回 None）
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes

            class _MSE(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            mse = _MSE()
            mse.dwLength = ctypes.sizeof(mse)
            gms = ctypes.windll.kernel32.GlobalMemoryStatusEx
            gms.argtypes = [ctypes.c_void_p]
            if gms(ctypes.byref(mse)):
                return float(mse.dwMemoryLoad), None
        except Exception:
            pass
    return None, None


def read_disk_usage():
    """返回 (磁盘使用率%, 剩余空间GB)"""
    path = '/' if sys.platform != 'win32' else os.path.splitdrive(_PROJECT_ROOT)[0] + '\\'
    try:
        du = shutil.disk_usage(path)
        return du.used / du.total * 100, du.free / (1024 ** 3)
    except OSError:
        return None, None


def read_load_average():
    """返回 1 分钟负载；不支持返回 None"""
    if hasattr(os, 'getloadavg'):
        try:
            return os.getloadavg()[0]
        except OSError:
            pass
    return None


# =============================================================================
# 告警冷却（状态文件持久化，进程重启/独立运行均有效）
# =============================================================================

def _state_path():
    return os.path.join(LOG_DIR, STATE_FILENAME)


def _load_cooldown_state():
    try:
        with open(_state_path(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cooldown_state(state):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(_state_path(), 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError as e:
        logger.warning(f"[系统监控] 冷却状态写入失败: {e}")


# =============================================================================
# 主检查流程
# =============================================================================

def run_check(source: str = 'embedded') -> dict:
    """执行一轮完整检查，返回结果摘要。

    Args:
        source: 'embedded' = 进程内嵌入（跳过进程存活检查）；
                'guard' = 独立运行（检查交易进程是否存活）
    """
    now = time.time()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cooldown = _load_cooldown_state()
    severe = []        # [(冷却key, 文本)] 严重异常 → 邮件告警（进程退出/内存≥90%/磁盘剩余≤5%）
    minor = []         # [文本] 一般异常 → 仅写入历史 JSONL，不发邮件
    snapshot = {}      # 系统快照（随邮件附送 + 写入历史）

    # --- 1. 进程存活（仅独立运行时检查；无 /proc 的平台不支持扫描，跳过）---
    process_check_supported = source == 'embedded' or os.path.isdir('/proc')
    app_pid = os.getpid() if source == 'embedded' else find_app_pid()
    if source == 'guard' and app_pid is None and process_check_supported:
        restart_info = ''
        if SUPERVISOR_NAME:
            try:
                r = subprocess.run(
                    ['supervisorctl', 'restart', SUPERVISOR_NAME],
                    capture_output=True, text=True, timeout=60)
                ok = r.returncode == 0
                detail = (r.stderr or r.stdout or '').strip()[:200]
                restart_info = f'，已尝试 supervisorctl restart {SUPERVISOR_NAME}' \
                               f'（{"成功" if ok else "失败: " + detail}）'
            except Exception as e:
                restart_info = f'，自动重启失败: {e}'
        severe.append(('process_dead',
                       f'交易进程已退出（命令行关键字 {APP_CMD_KEYWORDS}）{restart_info}'))

    app_rss = get_process_rss_mb(app_pid) if app_pid else None
    if app_rss is not None:
        snapshot['app_pid'] = app_pid
        snapshot['app_rss_mb'] = round(app_rss, 1)
        if source == 'guard' and app_rss > PROCESS_WARN_MB:
            minor.append(f'交易进程内存 {app_rss:.1f}MB 超过关注阈值 {PROCESS_WARN_MB}MB')

    # --- 2. 系统内存 / SWAP ---
    mem_pct, swap_pct = read_system_memory()
    if mem_pct is not None:
        snapshot['mem_usage_pct'] = round(mem_pct, 1)
        if mem_pct >= MEM_USAGE_PCT:
            severe.append(('mem_high',
                           f'系统内存使用率 {mem_pct:.1f}% ≥ {MEM_USAGE_PCT}%，OOM 风险'))
    if swap_pct is not None:
        snapshot['swap_usage_pct'] = round(swap_pct, 1)
        if swap_pct > SWAP_USAGE_PCT:
            minor.append(f'SWAP 使用率 {swap_pct:.1f}% > {SWAP_USAGE_PCT}%')

    # --- 3. 磁盘 ---
    disk_pct, disk_free_gb = read_disk_usage()
    if disk_pct is not None:
        snapshot['disk_usage_pct'] = round(disk_pct, 1)
        snapshot['disk_free_gb'] = round(disk_free_gb, 1)
        disk_free_pct = 100.0 - disk_pct
        if disk_free_pct <= DISK_FREE_PCT:
            severe.append(('disk_low',
                           f'磁盘可用空间仅剩 {disk_free_pct:.1f}%'
                           f'（{disk_free_gb:.1f}GB）≤ {DISK_FREE_PCT}%'))

    # --- 4. 负载 ---
    load1 = read_load_average()
    if load1 is not None:
        cpus = os.cpu_count() or 1
        snapshot['load_1min'] = round(load1, 2)
        snapshot['cpu_count'] = cpus
        if load1 / cpus > LOAD_PER_CPU:
            minor.append(f'系统负载过高: {load1:.2f}（{cpus} 核）')
    issues = [t for _, t in severe] + minor

    # --- 历史采样（进程被杀也保留趋势证据；一般异常随记录落盘供趋势图排查）---
    record = {'timestamp': now_str, 'source': source}
    if app_rss is not None:
        record['rss_mb'] = round(app_rss, 1)
    record.update({k: v for k, v in snapshot.items()
                   if k in ('mem_usage_pct', 'swap_usage_pct',
                            'disk_usage_pct', 'load_1min')})
    if issues:
        record['issues'] = issues
    append_history_record(record)

    # --- 严重异常才发邮件（冷却过滤后合并为一封；一般异常仅落盘）---
    emailed = False
    pending = [(k, t) for k, t in severe
               if now - float(cooldown.get(k, 0)) > EMAIL_COOLDOWN_SEC]
    for text in issues:
        logger.warning(f"[系统监控] {text}")
    if pending:
        content = ('检测到以下严重异常：\n'
                   + '\n'.join(f'{i}. {t}' for i, (_, t) in enumerate(pending, 1)))
        snap_lines = [f'{k}: {v}' for k, v in snapshot.items()]
        if snap_lines:
            content += '\n\n系统快照：\n' + '\n'.join(snap_lines)
        content += (f'\n\n检测时间: {now_str}（来源: {source}）\n'
                    f'（同类告警 {EMAIL_COOLDOWN_SEC // 60} 分钟内不重复发送）')
        emailed = send_alert_email(
            f'🚨 服务器严重告警（{len(pending)}项异常）', content)
        if emailed:
            for key, _ in pending:
                cooldown[key] = now
    if severe:
        _save_cooldown_state(cooldown)

    result = {
        'checked_at': now_str,
        'source': source,
        'process_alive': None if not process_check_supported else bool(app_pid),
        'app_pid': app_pid,
        'snapshot': snapshot,
        'issues': issues,
        'emailed': emailed,
    }
    _state['last_result'] = result
    if not issues:
        logger.info(f"[系统监控] 检查正常（来源: {source}，"
                    f"RSS: {app_rss:.1f}MB）" if app_rss else
                    f"[系统监控] 检查正常（来源: {source}）")
    return result


# =============================================================================
# 进程内嵌入：后台线程
# =============================================================================

def _loop(interval_sec):
    while True:
        try:
            run_check(source='embedded')
        except Exception as e:
            logger.error(f"[系统监控] 检查异常: {e}")
        time.sleep(interval_sec)


def start_system_monitor(interval_sec=None):
    """启动系统监控后台线程（幂等）。进程崩溃时线程随之消亡，
    完整兜底需按文件头说明配置 cron 独立运行。"""
    global _state
    if _state['started']:
        return
    interval = int(os.environ.get(
        'CRYPTO_SYSMON_SEC', interval_sec or DEFAULT_INTERVAL_SEC))
    import threading
    threading.Thread(target=_loop, args=(interval,), daemon=True,
                     name='system-monitor').start()
    _state['started'] = True
    logger.info(f"[系统监控] 已启动（间隔 {interval}s，嵌入模式）")


def get_last_result():
    """最近一轮检查结果（供 /api/system/status 接口使用）"""
    return _state['last_result']


# =============================================================================
# 独立运行入口（cron 每分钟调用）
# =============================================================================

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='[%(asctime)s] %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    try:
        _result = run_check(source='guard')
        if _result['issues']:
            print(f"[系统监控] 发现 {len(_result['issues'])} 项异常，"
                  f"严重告警邮件{'已发送' if _result['emailed'] else '被冷却抑制或无严重项'}，"
                  f"其余异常已写入历史日志")
    except Exception as _e:
        logger.error(f"监控脚本自身异常: {_e}")
    sys.exit(0)
