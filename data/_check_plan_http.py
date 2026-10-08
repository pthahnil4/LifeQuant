# -*- coding: utf-8 -*-
"""通过 HTTP 复测任务卡读接口（带 Bearer 口令，口令只从文件读取、不打印）

用途：验证 /plan 页面依赖的接口在修复后恢复——code=200、耗时正常、卡片有数据。
"""
import json
import os
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = 'http://127.0.0.1:7777'
with open(os.path.join(ROOT, 'data', 'web_token.txt'), encoding='utf-8') as f:
    TOKEN = f.read().strip()


def get(path, timeout=90):
    req = urllib.request.Request(BASE + path, headers={'Authorization': 'Bearer ' + TOKEN})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode('utf-8')
    dt = time.time() - t0
    body = json.loads(raw)
    print(f'{path:60s} HTTP {r.status} code={body.get("code")} {dt:5.2f}s {len(raw)}B')
    return body


print('--- 读接口复测（连续两轮，看冷/热） ---')
for i in range(2):
    print(f'[{i + 1}]')
    b = get('/plan/api/list')
    plans = b.get('data') or []
    cards = [c for p in plans for c in ((p.get('stats') or {}).get('cards') or [])]
    prog = [(c.get('id'), c.get('round'), c.get('status'), c.get('task_progress')) for c in cards[:3]]
    print('    计划数', len(plans), '| 卡片数', len(cards))
    print('    样例卡片 task_progress:', json.dumps(prog, ensure_ascii=False)[:300])
    if plans:
        pid = plans[0]['id']
        cid = cards[0]['id'] if cards else ''
        d = get(f'/plan/api/card-detail?plan_id={pid}&card_id={cid}')
        dd = d.get('data') or {}
        print('    card-detail tasks:', json.dumps(dd.get('tasks'), ensure_ascii=False)[:300])
    get('/plan/api/today-status')
