# -*- coding: utf-8 -*-
"""查询实盘调度器状态与期望运行状态（Bearer 口令只从文件读取，不打印）

用途：重启 app.py 后确认实盘是否在跑、重启自动拉起是否被熔断，避免"以为在跑其实停了"。
"""
import json
import os
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, 'data', 'web_token.txt'), encoding='utf-8') as f:
    TOKEN = f.read().strip()


def get(path):
    req = urllib.request.Request('http://127.0.0.1:7777' + path,
                                 headers={'Authorization': 'Bearer ' + TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


for p in ('/api/task/trading/status', '/api/task/trading/runtime'):
    try:
        b = get(p)
        print(f'--- {p} code={b.get("code")}')
        print(json.dumps(b.get('data'), ensure_ascii=False, indent=2)[:1200])
    except Exception as e:  # noqa: BLE001
        print(f'--- {p} 查询失败: {e}')
