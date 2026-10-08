#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志聚合采集（P2）：只读合并 task_scheduler.log + trade_operations.log。

设计取舍：
- 这两份日志**没有统一的 LEVEL 前缀**（是中文业务流水 + 异常栈混排），所以级别
  由内容关键字判定（ERROR/WARN/INFO 三档），判定口径在本模块显式列出，透明可查。
- 物理行以 `[时间戳]` 起头为一条记录；其后不以 `[` 起头的行是**续行**（Traceback、
  换行输出），并入上一条记录的正文，避免异常栈被拆成孤儿行。
- 只做 tail 读取 + 内存过滤 + 按 id 游标向更早翻页；不解析业务语义、不写任何文件。
"""
import os
import re
from datetime import datetime

from .. import config as C
from . import _io

# 记录起始：[YYYY-MM-DD HH:MM:SS] 正文
_LINE_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s?(.*)$')
# 币种代号（本系统全为 USDT 永续，形如 NEAR-USDT-SWAP；限定字母开头以免误捕日期）
_COIN_RE = re.compile(r'\b([A-Z][A-Z0-9]{1,15}-USDT-SWAP)\b')   # 如 NEAR-USDT-SWAP

# 级别判定口径（来自真实日志词频校准）
ERROR_TOKENS = ('失败', '异常', '错误', '拒绝', '无法', 'ERROR', 'CRITICAL',
                'Traceback', 'Exception', 'insufficient', '报错', '下单失败')
WARN_TOKENS = ('超时', '重试', '预警', '告警', 'WARNING', 'OOM', '追单', '风险')
# 其余（开仓/平仓/撤单/挂单/成交/心跳/巡检/方向锁定…）视为 INFO 正常流水


def _classify(msg):
    if any(t in msg for t in ERROR_TOKENS):
        return 'error'
    if any(t in msg for t in WARN_TOKENS):
        return 'warn'
    return 'info'


def _coin_of(msg):
    m = _COIN_RE.search(msg)
    return m.group(1) if m else ''


def _human_bytes(n):
    if n is None:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.1f%s' % (n, unit) if unit != 'B' else '%dB' % n
        n /= 1024.0
    return '%dB' % n


def _parse_source(key, name, path):
    """把单个日志文件解析成记录列表（旧→新），合并续行。"""
    raw = _io.read_text_lines(path, max_lines=C.LOG_MAX_SCAN, max_bytes=C.LOG_MAX_BYTES)
    recs = []
    for line in raw:
        m = _LINE_RE.match(line)
        if m:
            recs.append({'ts': m.group(1), 'msg': m.group(2), 'src_key': key, 'src_name': name})
        elif recs:
            recs[-1]['msg'] += '\n' + line        # 续行并入上一条
        else:
            recs.append({'ts': '', 'msg': line, 'src_key': key, 'src_name': name})
    for r in recs:
        r['level'] = _classify(r['msg'])
        r['coin'] = _coin_of(r['msg'])
    return recs, path


def _load_all():
    """合并全部来源，按时间升序，赋单调 id（游标翻页用）。"""
    merged = []
    meta = []
    for key, info in C.LOG_SOURCES.items():
        path = info['path']
        recs, _ = _parse_source(key, info['name'], path)
        merged.extend(recs)
        exists = os.path.isfile(path)
        size = os.path.getsize(path) if exists else None
        meta.append({
            'key': key, 'name': info['name'], 'path': path,
            'exists': exists, 'records': len(recs),
            'size_txt': _human_bytes(size),
            'last_ts': recs[-1]['ts'] if recs else None,
            'levels': {
                'error': sum(1 for r in recs if r['level'] == 'error'),
                'warn': sum(1 for r in recs if r['level'] == 'warn'),
                'info': sum(1 for r in recs if r['level'] == 'info'),
            },
        })
    # 时间升序（None/'' 排最前）；等时保持来源插入序
    merged.sort(key=lambda r: r['ts'] or '')
    for i, r in enumerate(merged):
        r['id'] = i
    level_counts = {
        'error': sum(1 for r in merged if r['level'] == 'error'),
        'warn': sum(1 for r in merged if r['level'] == 'warn'),
        'info': sum(1 for r in merged if r['level'] == 'info'),
    }
    return {'records': merged, 'sources': meta, 'level_counts': level_counts,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


@_io.cached('logs_all', C.CACHE_TTL_SEC)
def _cached_all():
    return _load_all()


def _matches(r, level_set, q, source, coin):
    if level_set and r['level'] not in level_set:
        return False
    if source and r['src_key'] != source:
        return False
    if coin and coin.upper() not in (r['coin'] or '').upper() and coin.upper() not in r['msg'].upper():
        return False
    if q:
        ql = q.lower()
        if ql not in r['msg'].lower() and ql not in (r['ts'] or '').lower():
            return False
    return True


def overview():
    """供页面首屏与元信息条使用：各源概况 + 级别分布。"""
    d = _cached_all()
    return {'sources': d['sources'], 'level_counts': d['level_counts'],
            'total': len(d['records']), 'generated_at': d['generated_at']}


def query(level='', q='', source='', coin='', limit=C.LOG_DEFAULT_LIMIT, before=None):
    """过滤 + 向更早翻页。返回 {records(新→旧), matched, next_before, has_more}。"""
    d = _cached_all()
    level_set = set(x for x in re.split(r'[,\s]+', (level or '').strip().lower()) if x)
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = C.LOG_DEFAULT_LIMIT
    try:
        before_i = int(before) if before not in (None, '', 'null') else None
    except (TypeError, ValueError):
        before_i = None

    filtered = [r for r in d['records'] if _matches(r, level_set, q, source, coin)]
    matched = len(filtered)

    window = filtered if before_i is None else [r for r in filtered if r['id'] < before_i]
    page = window[-limit:]
    page = list(reversed(page))                 # 展示：新→旧
    next_before = page[-1]['id'] if page else None
    has_more = bool(next_before is not None) and any(r['id'] < next_before for r in window)

    return {'records': page, 'matched': matched, 'next_before': next_before,
            'has_more': has_more, 'level_counts': d['level_counts']}
