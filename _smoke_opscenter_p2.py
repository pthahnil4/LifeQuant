#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 渲染冒烟：内存治理 + 日志聚合两页及其只读 API；并断言未 import crypto。

用法：python _smoke_opscenter_p2.py
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from opscenter import create_app  # noqa: E402

app = create_app()
client = app.test_client()

failures = []

PAGES = [('/memory', '内存治理'), ('/logs', '日志聚合中心')]
for path, marker in PAGES:
    r = client.get(path)
    body = r.get_data(as_text=True)
    leftover = ('{{' in body) or ('{%' in body)
    ok = (r.status_code == 200) and (marker in body) and (not leftover)
    print('[PAGE] %-9s status=%s len=%-6d marker=%-5s jinja_leftover=%-5s -> %s'
          % (path, r.status_code, len(body), marker in body, leftover, 'OK' if ok else 'FAIL'))
    if not ok:
        failures.append(path)

# 只读 API
for path in ['/memory/data', '/logs/meta']:
    r = client.get(path)
    ok = r.status_code == 200
    print('[API ] %-12s status=%s -> %s' % (path, r.status_code, 'OK' if ok else 'FAIL'))
    if not ok:
        failures.append(path)

# /logs/data 默认 + 过滤：确认返回真实结构化记录
r = client.get('/logs/data?limit=5')
d = json.loads(r.get_data(as_text=True))['data']
sample = d['records'][0] if d['records'] else {}
ok = (r.status_code == 200 and 'records' in d and 'id' in sample and 'level' in sample)
print('[DATA] /logs/data matched=%s first=%s' % (d.get('matched'),
      {k: sample.get(k) for k in ('ts', 'level', 'src_name', 'coin')}))
if not ok:
    failures.append('/logs/data')

r = client.get('/logs/data?level=error&limit=5')
d2 = json.loads(r.get_data(as_text=True))['data']
all_err = all(x['level'] == 'error' for x in d2['records'])
ok = r.status_code == 200 and (all_err or not d2['records'])
print('[DATA] /logs/data?level=error matched=%s 过滤纯净=%s -> %s'
      % (d2.get('matched'), all_err, 'OK' if ok else 'FAIL'))
if not ok:
    failures.append('/logs/data?level=error')

# /memory/data 关键结构
r = client.get('/memory/data')
m = json.loads(r.get_data(as_text=True))['data']
ok = ('rss_stats' in m and 'sys_stats' in m and 'events' in m and 'alerts' in m)
print('[DATA] /memory/data rss.cur=%s peak=%s | sys.cur=%s over_danger=%s | alerts=%s events=%s -> %s'
      % (m['rss_stats'].get('cur'), m['rss_stats'].get('peak'),
         m['sys_stats'].get('cur'), m['sys_stats'].get('over_danger'),
         len(m['alerts']), len(m['events']), 'OK' if ok else 'FAIL'))
if not ok:
    failures.append('/memory/data')

crypto_mods = sorted(x for x in sys.modules if x == 'crypto' or x.startswith('crypto.'))
if crypto_mods:
    failures.append('crypto-imported:%s' % crypto_mods)
print('[SEC ] import crypto 子模块 =', crypto_mods or '无（红线保持）')

print('=' * 56)
if failures:
    print('❌ P2 冒烟失败：', failures)
    sys.exit(1)
print('✅ P2 全部通过：内存治理 + 日志聚合渲染正常，未触碰 crypto。')
sys.exit(0)
