#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统健康采集：全部来自 memory_history.jsonl（只读）+ stdlib 磁盘现算。

不调用 crypto.system_monitor.run_check()——它会写心跳文件、可能发告警邮件，
违反"运维站不污染交易系统"的红线。这里只读它已经写好的采样结果。
"""
import os
import shutil
from datetime import datetime

from .. import config as C
from . import _io


def _clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def _fresh_disk():
    """本机磁盘（stdlib，跨平台，不依赖 crypto）：返回 (usage_pct, free_gb, free_pct)。"""
    try:
        path = ('\\' if os.name == 'nt' else '/')
        if os.name == 'nt':
            drive = os.path.splitdrive(C.PROJECT_ROOT)[0]
            path = (drive + '\\') if drive else 'C:\\'
        du = shutil.disk_usage(path)
        usage_pct = round(du.used / du.total * 100, 1)
        return usage_pct, round(du.free / (1024 ** 3), 1), round(100 - usage_pct, 1)
    except OSError:
        return None, None, None


def _heartbeat_interval_sec(records):
    """估算 embedded 心跳的采样间隔（秒）：取相邻时间差的中位数。"""
    ts = [_io.parse_ts(r.get('timestamp')) for r in records if r.get('source') == 'embedded']
    ts = [t for t in ts if isinstance(t, datetime)]
    ts.sort()
    diffs = [int((ts[i] - ts[i - 1]).total_seconds()) for i in range(1, len(ts))]
    diffs = [d for d in diffs if 0 < d < 600]
    if not diffs:
        return None
    diffs.sort()
    return diffs[len(diffs) // 2]


def _load_tail(n=240):
    return _io.read_jsonl(C.MEMORY_HISTORY, max_lines=n)


def _build():
    now = datetime.now()
    tail = _load_tail()

    latest_app = None
    latest_emb = None
    for r in reversed(tail):                     # 从新到旧
        if r.get('source') == 'app' and latest_app is None:
            latest_app = r
        if r.get('source') == 'embedded' and latest_emb is None:
            latest_emb = r
        if latest_app and latest_emb:
            break

    # 进程存活：任一来源心跳足够新鲜即视为活
    cand_ts = [r.get('timestamp') for r in (latest_app, latest_emb) if r and r.get('timestamp')]
    newest = max(cand_ts) if cand_ts else None
    lag = _io.lag_seconds(newest, now)
    alive = (newest is not None and lag is not None and lag <= C.HEARTBEAT_FRESH_SEC)
    stale = newest is None

    rss = latest_app.get('rss_mb') if latest_app else None
    mem_pct = latest_emb.get('mem_usage_pct') if latest_emb else None
    hb_disk_pct = latest_emb.get('disk_usage_pct') if latest_emb else None

    disk_usage_pct, disk_free_gb, disk_free_pct = _fresh_disk()
    load_1min = latest_emb.get('load_1min') if latest_emb else None
    cpu_count = os.cpu_count() or 1
    interval = _heartbeat_interval_sec(tail)

    # 近期异常（从采样记录的 issues 字段收集，新→旧，去重，限量）
    issues, seen = [], set()
    for r in reversed(tail):
        for text in (r.get('issues') or []):
            if text not in seen:
                seen.add(text)
                issues.append({'ts': r.get('timestamp'), 'text': text})
        if len(issues) >= 12:
            break

    # ---- 服务列表（只展示真正可观测项，口径透明） ----
    services = [
        {
            'icon': '🐍', 'name': '交易进程',
            'meta': ('source=%s · 心跳延迟 %ss' % (latest_app and 'app' or (latest_emb and 'embedded' or '-'),
                                                   lag if lag is not None else '-')),
            'stat': '存活' if alive else '离线',
            'level': 'success' if alive else 'danger',
            'pct': 100 if alive else 0,
        },
        {
            'icon': '🧠', 'name': '进程内存 RSS',
            'meta': '关注 %dMB · 危险 %dMB' % (C.RSS_WARN_MB, C.RSS_KILL_MB),
            'stat': ('%s MB' % rss) if rss is not None else 'N/A',
            'level': _io.level_for(rss, C.RSS_WARN_MB, C.RSS_KILL_MB),
            'pct': round(_clamp((rss / C.RSS_KILL_MB * 100))) if rss is not None else 0,
        },
        {
            'icon': '💾', 'name': '系统内存使用率',
            'meta': '关注 %.0f%% · 危险 %.0f%%' % (C.MEM_PCT_WARN, C.MEM_PCT_DANGER),
            'stat': ('%s%%' % mem_pct) if mem_pct is not None else 'N/A',
            'level': _io.level_for(mem_pct, C.MEM_PCT_WARN, C.MEM_PCT_DANGER),
            'pct': round(_clamp(mem_pct)) if mem_pct is not None else 0,
        },
        {
            'icon': '🗄️', 'name': '磁盘空间（本机）',
            'meta': ('剩余 %s GB' % disk_free_gb) if disk_free_gb is not None else '无法读取',
            'stat': ('%s%% 用' % disk_usage_pct) if disk_usage_pct is not None else 'N/A',
            'level': _io.level_for(disk_free_pct, C.DISK_FREE_WARN_PCT, C.DISK_FREE_DANGER_PCT,
                                   higher_worse=False),
            'pct': round(_clamp(disk_usage_pct)) if disk_usage_pct is not None else 0,
        },
        {
            'icon': '📡', 'name': '监控采样频率',
            'meta': 'embedded 心跳间隔（中位数）',
            'stat': ('%ds' % interval) if interval else 'N/A',
            'level': 'success' if (interval and interval <= 90) else 'warning',
            'pct': 100 if (interval and interval <= 90) else 60,
        },
        {
            'icon': '⚖️', 'name': '系统负载 1min',
            'meta': ('%s 核' % cpu_count),
            'stat': ('%.2f' % load_1min) if load_1min is not None else 'N/A',
            'level': 'success' if load_1min is not None else 'neutral',
            'pct': round(_clamp(load_1min / cpu_count * 50)) if load_1min is not None else 0,
        },
    ]

    # ---- 分维评分 + 综合分 ----
    score_parts = [
        {'label': '进程存活', 'score': 100 if alive else 0,
         'level': 'success' if alive else 'danger'},
        {'label': '内存水位(RSS)', 'score': {'success': 100, 'warning': 75, 'danger': 45, 'neutral': 85}[
            services[1]['level']], 'level': services[1]['level']},
        {'label': '系统内存', 'score': round(_clamp(100 - max(0, (mem_pct - 70) * 3))) if mem_pct is not None else 85,
         'level': services[2]['level']},
        {'label': '磁盘', 'score': round(_clamp(disk_free_pct * 3)) if disk_free_pct is not None else 85,
         'level': services[3]['level']},
        {'label': '采集心跳', 'score': 100 if (interval and interval <= 90) else 70,
         'level': services[4]['level']},
    ]
    weights = [0.35, 0.2, 0.15, 0.15, 0.15]
    score = int(round(sum(p['score'] * w for p, w in zip(score_parts, weights))))

    # ---- 内存趋势点（app 心跳的 rss，旧→新） ----
    series = []
    for r in tail:
        if r.get('source') == 'app' and r.get('rss_mb') is not None:
            series.append({'ts': r.get('timestamp'), 'rss': r['rss_mb']})
    series = series[-60:]

    return {
        'checked_at': newest, 'stale': stale, 'lag_sec': lag,
        'process': {'alive': alive, 'pid': (latest_app or {}).get('pid'),
                    'level': 'success' if alive else 'danger', 'lag': lag},
        'rss_mb': rss, 'mem_usage_pct': mem_pct,
        'disk': {'usage_pct': disk_usage_pct, 'free_gb': disk_free_gb, 'free_pct': disk_free_pct},
        'hb_disk_pct': hb_disk_pct,
        'load_1min': load_1min, 'cpu_count': cpu_count, 'interval': interval,
        'services': services, 'issues': issues, 'memory_series': series,
        'score': _clamp(score), 'score_parts': score_parts,
    }


@_io.cached('health', C.CACHE_TTL_SEC)
def get_health():
    return _build()
