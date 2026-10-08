#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4 渲染冒烟：运维工具箱三页（冒烟运行器 / 诊断脚本 / 配置总览）+ 安全红线。

重点不是"数据对不对"，而是"有没有越界"：
1) 三页 200 渲染 + 无 Jinja 残留（与交易系统是否在线无关）；
2) **脱敏不泄密**：运行时 ast 解析真实 api_config.py 抓出密钥明文，断言它们绝不出现在
   三页 HTML 及三个 /data JSON 中（含被截断的前缀也不允许整段命中）；
3) **运行白名单**：/tools/smoke/run/<key> 只认运维站自检键，交易脚本 / 路径穿越一律拒绝；
4) 未 import crypto（工具箱全用 stdlib：os/ast/json/re/subprocess）。

用法：python _smoke_opscenter_p4.py
"""
import ast
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
from opscenter import config as C  # noqa: E402

app = create_app()
client = app.test_client()

failures = []


def _fail(msg):
    print('  ✗', msg)
    failures.append(msg)


# ---- 运行时抓真实密钥（不硬编码），用于反泄密断言 ----
_secrets = []
try:
    with open(C.API_CONFIG_PY, encoding='utf-8', errors='replace') as f:
        _tree = ast.parse(f.read())
    for _n in _tree.body:
        if isinstance(_n, ast.Assign) and any(
                getattr(_t, 'id', '') == 'ACCOUNTS' for _t in _n.targets):
            _acc = ast.literal_eval(_n.value)
            for _a in _acc.values():
                for _k in ('api_key', 'secret_key', 'passphrase'):
                    _v = str(_a.get(_k) or '').strip()
                    if _v and _v not in C.OKX_PLACEHOLDER_VALUES and len(_v) >= 8:
                        _secrets.append(_v)
except Exception as e:
    print('  · 未能预读 api_config 密钥（跳过反泄密强断言）：', e.__class__.__name__)
print('[SET ] 抓待验密钥 %d 条（仅用于断言其不出现，不打印明文）' % len(_secrets))

# 1) 三页渲染
PAGES = [('/tools/smoke', '手动运行指引'),
         ('/tools/diag', '诊断脚本清单'),
         ('/tools/config', 'OKX 账号注册表')]
bodies = {}
for path, marker in PAGES:
    r = client.get(path)
    body = r.get_data(as_text=True)
    bodies[path] = body
    leftover = ('{{' in body) or ('{%' in body)
    ok = (r.status_code == 200) and (marker in body) and (not leftover)
    print('[PAGE] %-16s status=%s len=%-6d marker=%-5s jinja=%-5s -> %s'
          % (path, r.status_code, len(body), marker in body, leftover, 'OK' if ok else 'FAIL'))
    if not ok:
        _fail('render ' + path)

# 2) 只读 JSON 结构
for path, keys in [
    ('/tools/smoke/data', ('tiers', 'self_smokes', 'totals', 'index_present')),
    ('/tools/diag/data', ('scripts', 'count')),
    ('/tools/config/data', ('okx', 'webgate', 'coins', 'env_flags', 'paths')),
]:
    r = client.get(path)
    d = json.loads(r.get_data(as_text=True)).get('data', {})
    missing = [k for k in keys if k not in d]
    ok = (r.status_code == 200) and (not missing)
    print('[API ] %-18s status=%s 缺键=%s -> %s'
          % (path, r.status_code, missing or '无', 'OK' if ok else 'FAIL'))
    if not ok:
        _fail('api ' + path)

# 3) 反泄密：三页 HTML + 三 JSON 全扫，任何一条真实密钥明文都不得出现
blob = ''.join(bodies.values())
blob += client.get('/tools/smoke/data').get_data(as_text=True)
blob += client.get('/tools/diag/data').get_data(as_text=True)
blob += client.get('/tools/config/data').get_data(as_text=True)
leaked = 0
for s in _secrets:
    if s and s in blob:
        leaked += 1
        _fail('泄密：命中一条 %d 位密钥明文' % len(s))
# 也断言配置页确实做了掩码：不得出现原始密钥字符串字段；secret/passphrase 必须是脱敏状态 dict
cfg_json = json.loads(client.get('/tools/config/data').get_data(as_text=True))['data']
accs = cfg_json.get('okx', {}).get('accounts', [])
for a in accs:
    if 'api_key' in a or 'secret_key' in a:
        _fail('配置 JSON 出现原始密钥字段 api_key/secret_key')
    for fld in ('secret', 'passphrase'):
        if fld in a and not isinstance(a[fld], dict):
            _fail('%s 非脱敏状态结构（疑似明文）' % fld)
masked_ok = all(('***' in a.get('api_mask', '') or a.get('api_mask') == '未配置') for a in accs)
print('[MASK] 反泄密命中=%d 账号数=%d api_key 掩码齐=%s -> %s'
      % (leaked, len(accs), masked_ok,
         'OK' if (leaked == 0 and (not accs or masked_ok)) else 'FAIL'))
if accs and not masked_ok:
    _fail('api_key 未全部掩码')

# 4) 运行白名单：交易脚本名 / 未知键一律被处理器拒绝（kind=denied）
for bad in ('p9', 'dual_position', 'pos_history', 'boot_resume', 'fix_regression', 'trading_runtime'):
    r = client.post('/tools/smoke/run/' + bad)
    d = json.loads(r.get_data(as_text=True)).get('data', {})
    if d.get('kind') != 'denied':
        _fail('白名单未拒绝 key=%s（kind=%s）' % (bad, d.get('kind')))
# 含斜杠的路径穿越被 Flask 路由（<key> 不收 /）挡在处理器之外，绝不落到执行分支
r = client.post('/tools/smoke/run/../../crypto/task/_smoke_dual_position')
if r.status_code == 200:
    _fail('路径穿越竟返回 200（应 404，不进处理器）')
print('[GUARD] 非白名单运行一律拒绝、穿越不进处理器 -> %s'
      % ('OK' if not any(('白名单' in f) or ('穿越' in f) for f in failures) else 'FAIL'))

# 5) 安全红线：未 import crypto
crypto_mods = sorted(x for x in sys.modules if x == 'crypto' or x.startswith('crypto.'))
if crypto_mods:
    _fail('crypto-imported:%s' % crypto_mods)
print('[SEC ] import crypto 子模块 =', crypto_mods or '无（红线保持）')

print('=' * 56)
if failures:
    print('❌ P4 冒烟失败：')
    for f in failures:
        print('   -', f)
    sys.exit(1)
print('✅ P4 全部通过：三页渲染正常、脱敏不泄密、运行白名单收紧、未触碰 crypto。')
sys.exit(0)
