#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调度可视化采集（P3 · ⑥）：只读交易系统 /api/task/status。

口径说明（透明）：
- APScheduler 的任务态（下次触发 next_run_time / 是否入队 active）只存在于运行进程
  内存，**没有持久 jobstore**，所以本功能不读文件、只经 HTTP 向交易系统 Web 取快照，
  绝不 import crypto.task.scheduler（导入即会拉起后台调度器）。
- 「最近执行结果」APScheduler 不落盘，交易系统的执行流水已在 P2 日志聚合中呈现；
  这里聚焦「调度器是否在跑 / 有哪些任务 / 下次何时触发 / 实盘调度器线程状态」。
- 交易 Web 未启动时整页优雅降级为「不可达」，不报错、不崩溃。
"""
from datetime import datetime

from .. import config as C
from . import _http, _io


def _trigger_txt(job):
    """把触发器参数压成一行可读文本（如 interval seconds=60 / cron hour=4）。"""
    kw = job.get('trigger_kwargs') or {}
    if not kw:
        return str(job.get('trigger') or '')
    parts = ['%s=%s' % (k, v) for k, v in kw.items()]
    return '%s · %s' % (job.get('trigger', ''), ' '.join(parts))


def _level_of(job, trading):
    """单条任务的展示分级。"""
    if job.get('id') == 'trading_scheduler':
        if trading.get('running'):
            return 'success'
        if trading.get('finishing'):
            return 'warning'
        return 'neutral'
    if job.get('active'):
        return 'success'
    return 'warning'


def _normalize_status(data):
    """把 /api/task/status 的 data 规整成页面直接消费的_jobs 列表 + 实盘摘要。"""
    jobs_raw = (data or {}).get('jobs') or []
    trading = (data or {}).get('trading_scheduler') or {}

    jobs = []
    for j in jobs_raw:
        item = {
            'id': j.get('id', ''),
            'name': j.get('name') or j.get('id', ''),
            'trigger': j.get('trigger', ''),
            'trigger_txt': _trigger_txt(j),
            'func_name': j.get('func_name', ''),
            'registered_at': j.get('registered_at', ''),
            'next_run_time': j.get('next_run_time') or '',
            'active': bool(j.get('active')),
            'is_trading': j.get('id') == 'trading_scheduler',
        }
        item['level'] = _level_of(j, trading)
        jobs.append(item)

    # 有下次触发的排前面并按时间升序，其余（实盘线程/未排队）按名称兜底
    jobs.sort(key=lambda x: (x['next_run_time'] == '', x['next_run_time'] or '', x['name']))

    active_count = sum(1 for x in jobs if x['active'])
    trigger_summary = {}
    for x in jobs:
        trigger_summary[x['trigger'] or '-'] = trigger_summary.get(x['trigger'] or '-', 0) + 1

    trading_level = ('success' if trading.get('running')
                     else 'warning' if trading.get('finishing') else 'neutral')
    return {
        'scheduler_running': bool((data or {}).get('running')),
        'job_count': (data or {}).get('job_count', len(jobs)),
        'active_count': active_count,
        'jobs': jobs,
        'trigger_summary': trigger_summary,
        'trading': {
            'present': any(x['is_trading'] for x in jobs) or bool(trading),
            'running': bool(trading.get('running')),
            'thread_alive': bool(trading.get('thread_alive')),
            'finishing': bool(trading.get('finishing')),
            'account': trading.get('account') or '',
            'account_name': trading.get('account_name') or '',
            'level': trading_level,
        },
    }


def _build():
    env = _http.api_get('task_status')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    base = {
        'reachable': bool(env['ok']),
        'kind': env['kind'],
        'message': env['message'],
        'endpoint': C.TRADING_API_BASE + C.API_ENDPOINTS['task_status'],
        'elapsed_ms': env['elapsed_ms'],
        'checked_at': now,
        'token_configured': bool(_http.read_token()),
    }
    if env['ok']:
        base.update(_normalize_status(env['data']))
    else:
        base.update({
            'scheduler_running': None, 'job_count': None, 'active_count': 0,
            'jobs': [], 'trigger_summary': {},
            'trading': {'present': False, 'running': False, 'thread_alive': False,
                        'finishing': False, 'account': '', 'account_name': '', 'level': 'neutral'},
        })
    return base


@_io.cached('schedule', C.SCHEDULE_TTL_SEC)
def get_schedule():
    return _build()
