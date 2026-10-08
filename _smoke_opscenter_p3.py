#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 渲染冒烟：调度可视化 + 实盘只读监控两页及其只读 API；并断言未 import crypto。

这两页的数据来自**只读 HTTP** 调用交易系统 Web（默认 127.0.0.1:7777）。交易 Web
未启动时，采集层必须优雅降级（reachable=False、结构完整、页面正常渲染 200），本冒烟
只验证结构与渲染，不依赖交易系统是否在线。

用法：python _smoke_opscenter_p3.py
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

# 1) 页面渲染：200 + 正文独有标记 + 无 Jinja 残留
PAGES = [('/schedule', 'APScheduler BackgroundScheduler'), ('/live', '只读声明')]
for path, marker in PAGES:
    r = client.get(path)
    body = r.get_data(as_text=True)
    leftover = ('{{' in body) or ('{%' in body)
    ok = (r.status_code == 200) and (marker in body) and (not leftover)
    print('[PAGE] %-10s status=%s len=%-6d marker=%-5s jinja_leftover=%-5s -> %s'
          % (path, r.status_code, len(body), marker in body, leftover, 'OK' if ok else 'FAIL'))
    if not ok:
        failures.append(path)

# 2) 只读 JSON API：200 + 结构键齐全（与在线/离线无关）
for path, keys in [
    ('/schedule/data', ('reachable', 'kind', 'jobs', 'trading', 'checked_at')),
    ('/live/data', ('reachable', 'kind', 'summary', 'positions', 'open_orders', 'endpoints')),
]:
    r = client.get(path)
    d = json.loads(r.get_data(as_text=True)).get('data', {})
    missing = [k for k in keys if k not in d]
    ok = (r.status_code == 200) and (not missing)
    print('[API ] %-14s status=%s reachable=%-5s kind=%-8s 缺键=%s -> %s'
          % (path, r.status_code, d.get('reachable'), d.get('kind'), missing or '无',
             'OK' if ok else 'FAIL'))
    if not ok:
        failures.append(path)

# 3) 离线/在线两态都要结构自洽：不可达时列表为空但 summary/trading 键齐全
rs = json.loads(client.get('/schedule/data').get_data(as_text=True))['data']
rl = json.loads(client.get('/live/data').get_data(as_text=True))['data']
if not rs['reachable']:
    ok = (rs['jobs'] == [] and 'level' in rs['trading'])
    print('[DEG ] /schedule 不可达降级：jobs空+trading.level齐 -> %s' % ('OK' if ok else 'FAIL'))
    if not ok:
        failures.append('/schedule degrade')
if not rl['reachable']:
    ok = (rl['positions'] == [] and 'pos_count' in rl['summary'])
    print('[DEG ] /live 不可达降级：空仓+summary齐 -> %s' % ('OK' if ok else 'FAIL'))
    if not ok:
        failures.append('/live degrade')

# 4) 安全红线：绝不 import crypto（本只读客户端只用 stdlib urllib）
crypto_mods = sorted(x for x in sys.modules if x == 'crypto' or x.startswith('crypto.'))
if crypto_mods:
    failures.append('crypto-imported:%s' % crypto_mods)
print('[SEC ] import crypto 子模块 =', crypto_mods or '无（红线保持）')

print('=' * 56)
if failures:
    print('❌ P3 冒烟失败：', failures)
    sys.exit(1)
print('✅ P3 全部通过：调度可视化 + 实盘只读监控渲染正常、降级自洽，未触碰 crypto。')
sys.exit(0)
