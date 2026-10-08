#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘只读监控采集（P3 · ⑦）：只读交易系统四个 GET 接口聚合展示。

- /api/account/info     账户总权益 + 逐币种
- /api/account/positions 当前持仓
- /api/trade/open_orders 当前未成交委托
- /api/funds/balance    资金余额 / 资产估值

红线：只 GET、不 POST；不 import crypto；不自行下单/平仓。展示口径与交易系统页面
完全同源（同一个 Web 进程、同一套限频读），运维站只是"换一副眼睛看同一份数据"。

短路策略：先探 account_info。若连不上（offline）或被闸门拦下（auth），后续接口必然
同样结果，不再逐个空耗超时——直接把同一状态贴到各接口，整页优雅降级。
"""
from datetime import datetime

from .. import config as C
from . import _http, _io


def _f(x, default=None):
    """安全转 float（OKX 金额/盈亏都是字符串）。空/非法返回 default。"""
    try:
        s = str(x).strip().rstrip('%x')       # 去掉接口拼接的 % / x 后缀（uplRatio/lever）
        if s in ('', 'None'):
            return default
        return float(s)
    except (ValueError, TypeError):
        return default


def _num(x, nd=2):
    v = _f(x)
    if v is None:
        return '—'
    return ('%.' + str(nd) + 'f') % v


def _normalize_positions(rows):
    out = []
    total_upl, upl_known = 0.0, False
    for p in rows or []:
        upl = _f(p.get('upl'))
        if upl is not None:
            total_upl += upl
            upl_known = True
        out.append({
            'instId': p.get('instId', ''),
            'posSide': p.get('posSide', ''),
            'size': p.get('size', ''),
            'avgPx': p.get('avgPx', ''),
            'markPx': p.get('markPx', ''),
            'upl': upl,
            'upl_txt': _num(p.get('upl')),
            'uplRatio': p.get('uplRatio', ''),
            'lever': p.get('lever', ''),
            'margin': p.get('margin', ''),
            'cTime': p.get('cTime', ''),
            'level': ('success' if (upl or 0) > 0 else 'danger' if (upl or 0) < 0 else 'neutral'),
        })
    return out, (round(total_upl, 2) if upl_known else None)


def _normalize_orders(rows):
    out = []
    for o in rows or []:
        out.append({
            'ordId': o.get('ordId', ''),
            'instId': o.get('instId', ''),
            'side': o.get('side', ''),
            'posSide': o.get('posSide', ''),
            'ordType': o.get('ordType', ''),
            'px': o.get('px', ''),
            'sz': o.get('sz', ''),
            'accFillSz': o.get('accFillSz', ''),
            'state': o.get('state', ''),
            'cTime': o.get('cTime', ''),
            'clOrdId': o.get('clOrdId', ''),
            'lever': o.get('lever', ''),
        })
    return out


def _normalize_balances(data):
    val = (data or {}).get('valuation') or {}
    rows = []
    for b in (data or {}).get('balances') or []:
        rows.append({
            'ccy': b.get('ccy', ''),
            'bal': b.get('bal', ''),
            'availBal': b.get('availBal', ''),
            'frozenBal': b.get('frozenBal', ''),
            'eq': b.get('eq', ''),
        })
    return {'total_bal': val.get('totalBal', ''), 'ts': val.get('ts', ''), 'rows': rows}


def _normalize_account(data):
    details = []
    for d in (data or {}).get('details') or []:
        details.append({
            'ccy': d.get('ccy', ''),
            'eq': d.get('eq', ''),
            'eqUsd': d.get('eqUsd', ''),
            'availBal': d.get('availBal', ''),
            'frozenBal': d.get('frozenBal', ''),
        })
    return {
        'total_eq': _f((data or {}).get('totalEq')),
        'total_eq_txt': _num((data or {}).get('totalEq')),
        'total_eq_usd': (data or {}).get('totalEqUsd', ''),
        'margin_mode': (data or {}).get('marginMode', ''),
        'u_time': (data or {}).get('uTime', ''),
        'details': details,
    }


def _ep_state(env):
    return {'ok': bool(env['ok']), 'kind': env['kind'],
            'message': env['message'], 'elapsed_ms': env['elapsed_ms']}


def _build():
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    token_on = bool(_http.read_token())
    base = {
        'endpoint_base': C.TRADING_API_BASE,
        'checked_at': now,
        'token_configured': token_on,
        'reachable': False,
        'kind': 'offline',
        'message': '',
        'elapsed_ms': 0,
        'endpoints': {},
        'account': _normalize_account(None),
        'positions': [], 'pos_upl': None,
        'open_orders': [], 'balances': _normalize_balances(None),
        'partial': False,
        'summary': {'total_eq_txt': '—', 'margin_mode': '', 'pos_count': 0, 'pos_upl': None,
                    'pos_upl_txt': '—', 'open_order_count': 0, 'ccy_count': 0, 'avail_total_txt': '—'},
    }

    info = _http.api_get('account_info')
    base['endpoints']['account_info'] = _ep_state(info)
    base['elapsed_ms'] = info['elapsed_ms']

    # 短路：连不上 / 被闸门拦下 → 后续接口必同样，不再空耗超时
    if info['kind'] in ('offline', 'auth'):
        for name in ('positions', 'open_orders', 'balance'):
            base['endpoints'][name] = {'ok': False, 'kind': info['kind'],
                                       'message': '（随 account_info 短路，未单独请求）', 'elapsed_ms': 0}
        base['reachable'] = False
        base['kind'] = info['kind']
        base['message'] = info['message']
        return base

    base['reachable'] = True                    # 连上了（哪怕个别接口业务报错）
    base['kind'] = info['kind']
    base['message'] = info['message']
    if info['ok']:
        base['account'] = _normalize_account(info['data'])

    pos = _http.api_get('positions')
    base['endpoints']['positions'] = _ep_state(pos)
    if pos['ok']:
        base['positions'], base['pos_upl'] = _normalize_positions(pos['data'])

    orders = _http.api_get('open_orders')
    base['endpoints']['open_orders'] = _ep_state(orders)
    if orders['ok']:
        base['open_orders'] = _normalize_orders(orders['data'])

    bal = _http.api_get('balance')
    base['endpoints']['balance'] = _ep_state(bal)
    if bal['ok']:
        base['balances'] = _normalize_balances(bal['data'])

    # 汇总：只要核心接口有一个成功即视为可用快照
    ok_any = any(base['endpoints'][k]['ok'] for k in base['endpoints'])
    base['summary'] = {
        'total_eq_txt': base['account']['total_eq_txt'],
        'margin_mode': base['account']['margin_mode'],
        'pos_count': len(base['positions']),
        'pos_upl': base['pos_upl'],
        'pos_upl_txt': _num(base['pos_upl']) if base['pos_upl'] is not None else '—',
        'open_order_count': len(base['open_orders']),
        'ccy_count': len(base['account']['details']),
        'avail_total_txt': _num(base['balances']['total_bal']),
    }
    base['partial'] = ok_any and not all(base['endpoints'][k]['ok'] for k in base['endpoints'])
    return base


@_io.cached('live', C.LIVE_TTL_SEC)
def get_live():
    return _build()
