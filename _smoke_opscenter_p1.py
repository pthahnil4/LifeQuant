#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 渲染冒烟：证明三页 + 三 API 正常渲染，且未 import crypto（安全红线）。

用法：python _smoke_opscenter_p1.py
退出码 0 = 全绿；非 0 = 有失败。
"""
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

PAGES = [
    ('/', '系统健康概览'),
    ('/health', '系统健康检查'),
    ('/lifecycle', '进程生命周期'),
]
APIS = ['/api/dashboard', '/health/data', '/lifecycle/data']

failures = []

for path, marker in PAGES:
    r = client.get(path)
    body = r.get_data(as_text=True)
    leftover = ('{{' in body) or ('{%' in body)
    ok = (r.status_code == 200) and (marker in body) and (not leftover)
    print('[PAGE] %-11s status=%s len=%-6d marker=%-5s jinja_leftover=%-5s -> %s'
          % (path, r.status_code, len(body), marker in body, leftover, 'OK' if ok else 'FAIL'))
    if not ok:
        failures.append(path)

for path in APIS:
    r = client.get(path)
    ok = r.status_code == 200
    print('[API ] %-16s status=%s ct=%s -> %s'
          % (path, r.status_code, r.headers.get('Content-Type', ''), 'OK' if ok else 'FAIL'))
    if not ok:
        failures.append(path)

# 安全红线：整个过程不得 import crypto 任何子模块
crypto_mods = sorted(m for m in sys.modules if m == 'crypto' or m.startswith('crypto.'))
if crypto_mods:
    failures.append('crypto-imported:%s' % crypto_mods)
print('[SEC ] import crypto 子模块 =', crypto_mods or '无（红线保持）')

print('=' * 56)
if failures:
    print('❌ P1 冒烟失败：', failures)
    sys.exit(1)
print('✅ P1 全部通过：三页渲染正常，未触碰 crypto。')
sys.exit(0)
