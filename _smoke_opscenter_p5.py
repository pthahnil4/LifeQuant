#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P5 渲染冒烟：告警中心页 + 只读 API + 安全红线。

交易侧告警经只读 HTTP 调交易系统 alert_routes（默认 127.0.0.1:7777）；交易 Web
未启动时整页优雅降级（reachable=False、trade_alerts 空、但系统侧 sys_alerts 与
summary 结构齐全，页面仍 200）。并断言：未 import crypto、未暴露任何写操作端点。

用法：python _smoke_opscenter_p5.py
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


def _fail(m):
    print('  ✗', m)
    failures.append(m)


# 1) 页面渲染
r = client.get('/alerts')
body = r.get_data(as_text=True)
leftover = ('{{' in body) or ('{%' in body)
ok = (r.status_code == 200) and ('统一告警流' in body) and (not leftover)
print('[PAGE] /alerts status=%s len=%-6d marker=%-5s jinja=%-5s -> %s'
      % (r.status_code, len(body), '统一告警流' in body, leftover, 'OK' if ok else 'FAIL'))
if not ok:
    _fail('/alerts render')

# 2) 只读 JSON 结构齐全（与在线/离线无关）
d = json.loads(client.get('/alerts/data').get_data(as_text=True)).get('data', {})
need = ('reachable', 'kind', 'engine', 'rules', 'trade_alerts', 'sys_alerts', 'summary', 'endpoints')
missing = [k for k in need if k not in d]
ok = (not missing)
print('[API ] /alerts/data reachable=%-5s kind=%-8s 缺键=%s -> %s'
      % (d.get('reachable'), d.get('kind'), missing or '无', 'OK' if ok else 'FAIL'))
if not ok:
    _fail('/alerts/data keys')

# 3) summary 键齐全 + 不可达时 trade 空但 sys 仍可呈现
sm = d.get('summary', {})
need2 = ('critical', 'warning', 'sent', 'suppressed', 'trade_count', 'sys_count')
miss2 = [k for k in need2 if k not in sm]
if miss2:
    _fail('summary 缺键 %s' % miss2)
if not d.get('reachable'):
    ok = (d.get('trade_alerts') == [] and isinstance(d.get('sys_alerts'), list))
    print('[DEG ] 不可达降级：trade_alerts 空 + sys_alerts 为 list(len=%d) -> %s'
          % (len(d.get('sys_alerts') or []), 'OK' if ok else 'FAIL'))
    if not ok:
        _fail('/alerts degrade')

# 4) 红线：不提供写操作端点（存配置/立即检测是交易系统 POST，运维站绝不代理）
for w in ('/alerts/config', '/alerts/run-now', '/alerts/save'):
    rr = client.post(w)
    if rr.status_code != 404:
        _fail('竟存在写操作端点 %s（status=%s）' % (w, rr.status_code))
print('[GUARD] 无任何写操作端点（POST 全 404）-> %s'
      % ('OK' if not any('写操作' in f for f in failures) else 'FAIL'))

# 5) 安全红线：未 import crypto
crypto_mods = sorted(x for x in sys.modules if x == 'crypto' or x.startswith('crypto.'))
if crypto_mods:
    _fail('crypto-imported:%s' % crypto_mods)
print('[SEC ] import crypto 子模块 =', crypto_mods or '无（红线保持）')

print('=' * 56)
if failures:
    print('❌ P5 冒烟失败：')
    for f in failures:
        print('   -', f)
    sys.exit(1)
print('✅ P5 全部通过：告警中心渲染正常、降级自洽、无写操作端点、未触碰 crypto。')
sys.exit(0)
