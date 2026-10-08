#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性核验：总控台入口三件套在真实服务上可见（读闸门口令走回环）。"""
import sys
from pathlib import Path

import requests

BASE = 'http://127.0.0.1:5111'
token_file = Path(__file__).resolve().parent / 'web_token.txt'
sess = requests.Session()
if token_file.exists():
    tok = token_file.read_text(encoding='utf-8').strip()
    if tok:
        sess.headers['Authorization'] = f'Bearer {tok}'

fails = []


def check(name, cond, extra=''):
    print(('✓ ' if cond else '✗ ') + name + (f' | {extra}' if extra else ''))
    if not cond:
        fails.append(name)


r = sess.get(BASE + '/', timeout=15)
check('GET / 200', r.status_code == 200, f'status={r.status_code}')
html = r.text
check('首页 header 有「每日打卡」入口按钮', '📋 每日打卡' in html and 'href="/checkin"' in html)
check('首页有状态条挂载点 + checkin.js', 'id="checkin-bar"' in html and 'js/checkin.js' in html)
check('导航渲染出「每日打卡」（快照迁移生效）', html.count('每日打卡') >= 2)

r = sess.get(BASE + '/checkin', timeout=15)
check('GET /checkin 200', r.status_code == 200, f'status={r.status_code}')
check('打卡页渲染指标卡', 'id="checkin-page"' in r.text and 'ck-card-trade' in r.text)

r = sess.get(BASE + '/checkin/api/today', timeout=30)
j = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
m = (j.get('data') or {}).get('metrics') or {}
check('GET api/today 200 且交易项不触 OKX（status=no_data 或 cache 系）',
      j.get('code') == 200 and str(m.get('trade', {}).get('status')) in ('no_data', 'ok', 'api_error', 'no_base'),
      f"trade={m.get('trade', {}).get('status')} src={m.get('trade', {}).get('src')}")

print('RESULT:', 'ALL-OK' if not fails else f'FAILS={fails}')
sys.exit(1 if fails else 0)
