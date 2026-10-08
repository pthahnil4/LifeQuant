#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警中心采集（P5 · 统一告警流）。

两路数据，一路走只读 HTTP、一路走本地文件，交易 Web 离线时系统类告警仍照常显示：

- 交易侧（行情异动 / 持仓盈亏）：alert_monitor 引擎写入 alert_log，经交易系统
  alert_routes 的三个**只读 GET** 暴露：
    /alert/api/status  引擎开关/周期 + 最近一轮检测摘要（内存态，仅运行进程可见）
    /alert/api/config  阈值规则（只读展示，绝不回存）
    /alert/api/history 告警历史（alert_log，支持 type/level/inst 过滤）
  绝不触碰 POST 的存配置 / 手动检测（那会改调度、可能发信）。
- 系统侧（内存/磁盘/OOM/RSS/自愈）：直接复用 P2 的 memory 采集结果，读本地
  memory_history.jsonl，无需交易 Web。

交易 Web 未启动时整页优雅降级：trade 侧为空 + 明确原因，系统侧照常呈现。
"""
from datetime import datetime

from .. import config as C
from . import _http, _io


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# 交易告警级别 → 展示分级（critical 最高、recover 为缓解=好）
_TRADE_LEVEL = {'critical': 'danger', 'warning': 'warning', 'recover': 'success'}


def _norm_history(rows):
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        level = str(r.get('level') or '')
        out.append({
            'ts': r.get('created_at') or '',
            'type': r.get('type') or '',
            'inst_id': r.get('inst_id') or '',
            'level': level,
            'level_disp': {'critical': '严重', 'warning': '预警', 'recover': '缓解'}.get(level, level or '—'),
            'level_cls': _TRADE_LEVEL.get(level, 'neutral'),
            'value': _f(r.get('metric_value')),
            'threshold': _f(r.get('threshold')),
            'message': r.get('message') or '',
            'sent': bool(r.get('notify_sent')),
            'action': '已发信' if r.get('notify_sent') else '抑制',
        })
    return out


def _norm_status(data):
    data = data or {}
    job = data.get('job') or {}
    lr = data.get('last_run') or {}
    return {
        'enabled': bool(data.get('enabled')),
        'interval_seconds': data.get('interval_seconds'),
        'job_present': bool(job),
        'job_active': bool(job.get('active')),
        'job_next': job.get('next_run_time') or '',
        'last_run': {
            'finished_at': lr.get('finished_at') or '',
            'duration_ms': lr.get('duration_ms'),
            'hit_count': lr.get('hit_count'),
            'sent_count': lr.get('sent_count'),
            'email_ok': lr.get('email_ok'),
            'error': lr.get('error') or '',
        },
    }


def _norm_rules(cfg):
    cfg = cfg or {}
    price = cfg.get('price') or {}
    pnl = cfg.get('pnl') or {}
    return {
        'enabled': bool(cfg.get('enabled')),
        'account': cfg.get('account') or '',
        'price': {
            'enabled': bool(price.get('enabled')),
            'window_minutes': price.get('window_minutes'),
            'warning_pct': price.get('warning_pct'),
            'critical_pct': price.get('critical_pct'),
            'inst_count': len(price.get('inst_ids') or []),
        },
        'pnl': {
            'enabled': bool(pnl.get('enabled')),
            'warning_pct': pnl.get('warning_pct'),
            'critical_pct': pnl.get('critical_pct'),
        },
    }


def _sys_alerts():
    """复用 P2 内存采集的阈值/系统告警（本地文件，交易 Web 离线也有效）。"""
    try:
        from .memory import get_memory
        rows = get_memory().get('alerts') or []
        return [{'ts': a.get('ts'), 'text': a.get('text'),
                 'level': a.get('level') or 'warning',
                 'kind': a.get('kind') or '系统'} for a in rows[:40]]
    except Exception:
        return []


def _ep_state(env):
    return {'ok': bool(env['ok']), 'kind': env['kind'],
            'message': env['message'], 'elapsed_ms': env['elapsed_ms']}


def _build():
    now = _now()
    status_env = _http.api_get('alert_status')
    base = {
        'checked_at': now,
        'reachable': bool(status_env['ok']),
        'kind': status_env['kind'],
        'message': status_env['message'],
        'endpoint': C.TRADING_API_BASE + C.API_ENDPOINTS['alert_status'],
        'elapsed_ms': status_env['elapsed_ms'],
        'token_configured': bool(_http.read_token()),
        'engine': _norm_status(None),
        'rules': _norm_rules(None),
        'trade_alerts': [],
        'sys_alerts': _sys_alerts(),
        'endpoints': {'alert_status': _ep_state(status_env)},
    }

    # 离线/被拦：config/history 必同样，单探即短路，不空耗超时
    if status_env['kind'] in ('offline', 'auth'):
        for name in ('alert_config', 'alert_history'):
            base['endpoints'][name] = {'ok': False, 'kind': status_env['kind'],
                                       'message': '（随 status 短路，未单独请求）', 'elapsed_ms': 0}
        base['summary'] = _summary([], base['sys_alerts'])
        base['partial'] = False
        return base

    cfg_env = _http.api_get('alert_config')
    hist_env = _http.api_get('alert_history', params={'limit': C.ALERT_HISTORY_LIMIT})
    base['endpoints']['alert_config'] = _ep_state(cfg_env)
    base['endpoints']['alert_history'] = _ep_state(hist_env)
    # 连上了但个别接口业务报错 → partial
    base['partial'] = not (cfg_env['ok'] and hist_env['ok'])

    if status_env['ok']:
        base['engine'] = _norm_status(status_env['data'])
    if cfg_env['ok']:
        base['rules'] = _norm_rules(cfg_env['data'])
    trade = _norm_history(hist_env['data']) if hist_env['ok'] else []
    base['trade_alerts'] = trade
    base['summary'] = _summary(trade, base['sys_alerts'])
    return base


def _summary(trade, sys_alerts):
    return {
        'critical': sum(1 for t in trade if t['level'] == 'critical'),
        'warning': sum(1 for t in trade if t['level'] == 'warning'),
        'recover': sum(1 for t in trade if t['level'] == 'recover'),
        'sent': sum(1 for t in trade if t['sent']),
        'suppressed': sum(1 for t in trade if not t['sent']),
        'trade_count': len(trade),
        'sys_count': len(sys_alerts),
        'sys_danger': sum(1 for a in sys_alerts if a['level'] == 'danger'),
    }


@_io.cached('alerts', C.ALERT_TTL_SEC)
def get_alerts():
    return _build()
