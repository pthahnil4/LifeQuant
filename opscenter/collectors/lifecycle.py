#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程生命周期采集：只读 boot_ledger.jsonl + proc_exit.json。

复用交易系统 process_lifecycle 的"六态归因"口径，但只读其落盘结果，
不 import 该模块（其 classify_and_record_boot 会写台账）。
"""
import json
import os
from datetime import datetime

from .. import config as C
from . import _io

# 归因 reason → (中文标签, 语义级别, 说明)
REASON_META = {
    'first_run':     ('初次上线', 'info', '首次部署，无历史心跳可比'),
    'mem_restart':   ('内存超限重启', 'warning', 'RSS 触及危险线，看门狗主动退出后被拉起'),
    'daily_restart': ('每日内存归零', 'info', '计划内每日重启，释放累积内存'),
    'abnormal_death': ('异常退出', 'danger', '无退出标记即消失，判为崩溃/被系统杀'),
    'cold_start':    ('冷启动/手动重启', 'success', '收到 SIGTERM 优雅停止或面板重启后接回'),
    'manual_exit':   ('手动停止', 'neutral', '人工优雅停止'),
    'unknown':       ('未知', 'warning', '无法归因，按保守处理'),
}

PREV_EXIT_META = {
    'manual_exit': '手动停止', 'mem_restart': '内存退出', 'daily_restart': '每日归零',
}


def _humanize(dt_seconds):
    if dt_seconds is None:
        return '—'
    s = int(dt_seconds)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return '%d天%d小时' % (d, h)
    if h:
        return '%d小时%d分' % (h, m)
    return '%d分%d秒' % (m, s % 60)


def _build():
    now = datetime.now()
    rows = _io.read_jsonl(C.BOOT_LEDGER)          # 旧→新
    events = []
    reason_counts = {}
    today_str = now.strftime('%Y-%m-%d')
    today_restarts = 0

    for r in rows:
        reason = r.get('reason') or 'unknown'
        label, level, desc = REASON_META.get(reason, (reason, 'neutral', ''))
        ts = r.get('ts')
        if ts and str(ts).startswith(today_str):
            today_restarts += 1
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        pe = r.get('prev_exit')
        events.append({
            'ts': ts, 'seq': r.get('seq'), 'reason': reason,
            'label': label, 'level': level, 'desc': desc,
            'prev_exit': pe, 'prev_exit_label': PREV_EXIT_META.get(pe, pe),
            'lag_sec': r.get('heartbeat_lag_sec'), 'pid': r.get('pid'),
        })
    events.reverse()                              # 新→旧

    # 运行时长：距最近一次启动（rows 末条 ts）
    running_since = running_for = None
    if rows:
        last_ts = rows[-1].get('ts')
        dt = _io.parse_ts(last_ts)
        if dt:
            running_since = last_ts
            running_for = _humanize((now - dt).total_seconds())

    # 最近退出标记
    latest_exit = None
    try:
        if C.PROC_EXIT and os.path.isfile(C.PROC_EXIT):
            with open(C.PROC_EXIT, encoding='utf-8') as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                reason = obj.get('reason') or 'unknown'
                label, level, desc = REASON_META.get(reason, (reason, 'neutral', ''))
                latest_exit = {
                    'reason': reason, 'label': label, 'level': level,
                    'ts': obj.get('ts'), 'pid': obj.get('pid'),
                    'source': obj.get('source'), 'note': obj.get('note'),
                    'consumed_at': obj.get('consumed_at'),
                    'consumed': bool(obj.get('consumed_at')),
                }
    except (OSError, ValueError):
        latest_exit = None

    # 归因分布（供条形展示，按出现次数降序）
    distribution = sorted(
        ({'reason': k, 'label': REASON_META.get(k, (k,))[0],
          'level': REASON_META.get(k, ('', 'neutral'))[1], 'count': v}
         for k, v in reason_counts.items()),
        key=lambda x: x['count'], reverse=True)

    return {
        'events': events, 'reason_counts': reason_counts, 'distribution': distribution,
        'total': len(rows), 'today_restarts': today_restarts,
        'running_since': running_since, 'running_for': running_for,
        'latest_exit': latest_exit,
        'source_available': bool(rows),
    }


@_io.cached('lifecycle', C.CACHE_TTL_SEC)
def get_lifecycle():
    return _build()
