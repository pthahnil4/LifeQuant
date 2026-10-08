#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性真实服务核验：每日评级总控台改名 + 日记关联入口在实盘页面可见。"""
import sys
from pathlib import Path

import requests

BASE = 'http://127.0.0.1:5112'
s = requests.Session()
tf = Path('data/web_token.txt')
if tf.exists():
    tok = tf.read_text(encoding='utf-8').strip()
    if tok:
        s.headers['Authorization'] = 'Bearer ' + tok

fails = []


def ck(name, cond, extra=''):
    print(('OK ' if cond else 'XX ') + name + ((' | ' + str(extra)) if extra else ''))
    if not cond:
        fails.append(name)


r = s.get(BASE + '/', timeout=15)
idx = r.text
ck('GET / 200', r.status_code == 200, r.status_code)
ck('index 有「每日评级总控台」入口', '每日评级总控台' in idx and 'href="/checkin"' in idx)
ck('index 有日记本入口', 'href="/diary"' in idx)
ck('index 状态条标题=今日评级', '今日评级' in idx)

r = s.get(BASE + '/checkin', timeout=15)
h = r.text
ck('GET /checkin 200', r.status_code == 200, r.status_code)
ck('checkin 页 h1=每日评级总控台', '每日评级总控台' in h)
ck('checkin 页有日记提示元素',
   all(x in h for x in ('ck-diary-tip', 'ck-diary-entry', 'ck-diary-jump')))
ck('checkin 页有评级框 id', 'id="ck-rating"' in h)

r = s.get(BASE + '/checkin/api/today', timeout=15)
j = r.json()
ck('api/today code=200', j.get('code') == 200, j.get('code'))
d = j.get('data') or {}
diary = d.get('diary')
ck('api/today 带 diary 块(has_entry)',
   isinstance(diary, dict) and 'has_entry' in diary and 'entry_id' in diary, diary)

# 真实导航快照渲染（nav.html 走 kv 整份快照）：应含新名
ck('渲染导航含每日评级总控台(快照生效)', '每日评级总控台' in h)

print('FAILS=', fails)
sys.exit(1 if fails else 0)
