#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内存治理采集（P2）：全部只读 memory_history.jsonl + boot_ledger + memdump 快照。

聚焦"排障主力"三问：
1. RSS / 系统内存的趋势与水位是否逼近阈值线？
2. 触发过哪些阈值告警（OOM 风险 / RSS 撞线）？
3. 发生过哪些"自动清理 / 自愈"事件（内存超限重启、每日内存归零、泄漏快照）？

绝不 import crypto.memory_watchdog（其 start_* 会拉起守护线程、release/dump 有副作用）。
"""
import glob
import os
from datetime import datetime

from .. import config as C
from . import _io
from .lifecycle import get_lifecycle


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _mean(nums):
    return round(sum(nums) / len(nums), 1) if nums else None


def _parse_memdump(path):
    """解析看门狗快照头部：'时间: ...' / '当前RSS: ..MB'。失败返回 None。"""
    try:
        ts = rss = None
        with open(path, encoding='utf-8', errors='replace') as f:
            for _ in range(4):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith('时间:'):
                    ts = line.split(':', 1)[1].strip()
                elif line.startswith('当前RSS:'):
                    num = ''.join(ch for ch in line.split(':', 1)[1] if ch.isdigit() or ch == '.')
                    rss = num.strip('.') or None
        return {'ts': ts or os.path.basename(path), 'rss': rss,
                'file': os.path.basename(path)}
    except OSError:
        return None


def _build():
    now = datetime.now()
    rows = _io.read_jsonl(C.MEMORY_HISTORY, max_lines=4000)

    # ---- 拆分：app（进程 RSS） vs embedded/guard（系统内存/磁盘心跳） ----
    app_pts, sys_pts = [], []
    for r in rows:
        src = r.get('source')
        ts = r.get('timestamp')
        if src == 'app':
            v = _num(r.get('rss_mb'))
            if v is not None:
                app_pts.append({'ts': ts, 'v': v})
        elif src in ('embedded', 'guard'):
            mp = _num(r.get('mem_usage_pct'))
            dp = _num(r.get('disk_usage_pct'))
            if mp is not None or dp is not None:
                sys_pts.append({'ts': ts, 'mem': mp, 'disk': dp})

    stale = not app_pts and not sys_pts
    checked_at = (app_pts[-1]['ts'] if app_pts else None) or \
                 (sys_pts[-1]['ts'] if sys_pts else None)

    # ---- RSS 统计（整体历史） + 趋势窗口（最近 N 点） ----
    rss_vals = [p['v'] for p in app_pts]
    rss_win = app_pts[-C.MEM_RSS_WINDOW:]
    rss_stats = {
        'n': len(rss_vals),
        'cur': rss_vals[-1] if rss_vals else None,
        'peak': max(rss_vals) if rss_vals else None,
        'min': min(rss_vals) if rss_vals else None,
        'avg_win': _mean([p['v'] for p in rss_win]),
        'over_warn': sum(1 for v in rss_vals if v >= C.RSS_WARN_MB),
        'over_kill': sum(1 for v in rss_vals if v >= C.RSS_KILL_MB),
        'win_from': rss_win[0]['ts'] if rss_win else None,
        'win_to': rss_win[-1]['ts'] if rss_win else None,
        'level': _io.level_for(rss_vals[-1] if rss_vals else None, C.RSS_WARN_MB, C.RSS_KILL_MB),
    }

    # ---- 系统内存 / 磁盘统计 + 窗口 ----
    mem_vals = [p['mem'] for p in sys_pts if p['mem'] is not None]
    disk_vals = [p['disk'] for p in sys_pts if p['disk'] is not None]
    sys_win = sys_pts[-C.MEM_SYS_WINDOW:]
    sys_stats = {
        'n': len(mem_vals),
        'cur': mem_vals[-1] if mem_vals else None,
        'peak': max(mem_vals) if mem_vals else None,
        'avg_win': _mean([p['mem'] for p in sys_win if p['mem'] is not None]),
        'over_danger': sum(1 for v in mem_vals if v >= C.MEM_PCT_DANGER),
        'disk_cur': disk_vals[-1] if disk_vals else None,
        'disk_peak': max(disk_vals) if disk_vals else None,
        'level': _io.level_for(mem_vals[-1] if mem_vals else None, C.MEM_PCT_WARN, C.MEM_PCT_DANGER),
    }

    # ---- 阈值告警（issues 文本 + 系统内存撞 90% + RSS 撞线，去重、新→旧） ----
    alerts, seen = [], set()
    for r in reversed(rows):
        for text in (r.get('issues') or []):
            key = ('issue', text)
            if key in seen:
                continue
            seen.add(key)
            lvl = 'danger' if ('OOM' in text or '90' in text or '超限' in text) else 'warning'
            alerts.append({'ts': r.get('timestamp'), 'text': text, 'level': lvl,
                           'kind': '系统内存'})
    # 系统内存逐点撞 90%（若上游没写 issues 也补捕）
    for p in reversed(sys_pts[-400:]):
        if p['mem'] is not None and p['mem'] >= C.MEM_PCT_DANGER:
            key = ('mem', p['ts'])
            if key not in seen:
                seen.add(key)
                alerts.append({'ts': p['ts'], 'text': '系统内存 %.0f%% ≥ %.0f%%（OOM 风险）'
                                                    % (p['mem'], C.MEM_PCT_DANGER),
                               'level': 'danger', 'kind': '系统内存'})
    # RSS 撞危险线
    for p in reversed(app_pts[-800:]):
        if p['v'] >= C.RSS_KILL_MB:
            alerts.append({'ts': p['ts'], 'text': '进程 RSS %.0fMB ≥ 危险线 %.0fMB（将触发重启）'
                                                   % (p['v'], C.RSS_KILL_MB),
                           'level': 'danger', 'kind': '进程内存'})
    alerts.sort(key=lambda a: str(a['ts']), reverse=True)
    alerts = alerts[:30]

    # ---- 自动清理 / 自愈事件：内存重启归因 + 泄漏快照 ----
    events = []
    try:
        for e in get_lifecycle().get('events', []):
            if e['reason'] in ('mem_restart', 'daily_restart'):
                events.append({'ts': e['ts'], 'kind': 'restart', 'label': e['label'],
                               'level': e['level'],
                               'detail': 'seq %s · %s' % (e['seq'], e['desc'])})
    except Exception:
        pass
    try:
        dumps = sorted(glob.glob(os.path.join(C.LOG_DIR, C.MEM_DUMP_GLOB)),
                       key=lambda p: os.path.getmtime(p), reverse=True)
        for path in dumps[:10]:
            info = _parse_memdump(path)
            if info:
                events.append({'ts': info['ts'], 'kind': 'snapshot', 'label': '泄漏快照导出',
                               'level': 'info',
                               'detail': 'RSS %sMB · %s' % (info['rss'] or '?', info['file'])})
    except OSError:
        pass
    events.sort(key=lambda x: str(x['ts']), reverse=True)
    events = events[:24]

    return {
        'checked_at': checked_at, 'stale': stale, 'now': now.strftime('%Y-%m-%d %H:%M:%S'),
        'thresholds': {'warn_mb': C.RSS_WARN_MB, 'kill_mb': C.RSS_KILL_MB,
                       'oom_pct': C.MEM_PCT_DANGER},
        'rss_stats': rss_stats, 'sys_stats': sys_stats,
        'rss_series': rss_win, 'sys_series': sys_win,
        'alerts': alerts, 'events': events,
    }


@_io.cached('memory', C.CACHE_TTL_SEC)
def get_memory():
    return _build()
