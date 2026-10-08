# -*- coding: utf-8 -*-
"""只读路径回归验证：任务卡相关 GET 接口必须零 DML（不写库）

背景（故障复盘）：/plan/api/list、card-detail、today-status 曾在读路径里做
「milestones/todos → tasks」惰性迁移并整树回写，Flask threaded=True + 前端并发
轮询在远端 MySQL 上互相争 plan_cards 行锁，撞 1205 Lock wait timeout（50s），
接口 500 → 任务卡页面整体空白；且迁移写入失败导致每次加载重试，永久卡死。

断言：
  ①  三个 GET 接口执行期间 INSERT/UPDATE/DELETE 数量为 0（事件钩子捕获）
  ②  GET 接口返回 code=200 且耗时正常（不再撞锁等待）
  ③  接口跑完后全表数据快照逐列一致（读路径无副作用）
  ④  任务树推导确定性：同一份旧数据重复推导，节点 id 序列完全一致
"""
import hashlib as _hashlib
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import event, text  # noqa: E402
from flask import Flask  # noqa: E402

from crypto.database import get_engine, session_scope  # noqa: E402
from crypto import plan_routes as pr  # noqa: E402

DML = []


@event.listens_for(get_engine(), 'before_cursor_execute')
def _capture(conn, cursor, statement, parameters, context, executemany):
    head = statement.strip().upper()
    if head.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'ALTER', 'DROP', 'CREATE')):
        DML.append(head.split()[0] + ': ' + statement.strip()[:120])


app = Flask(__name__, template_folder=os.path.join(ROOT, 'crypto', 'templates'),
            static_folder=os.path.join(ROOT, 'crypto', 'static'))
app.register_blueprint(pr.plan_bp)
client = app.test_client()

fails = []


def check(name, ok, extra=''):
    print(('  [OK]   ' if ok else ' [FAIL] ') + name + ((' | ' + extra) if extra else ''))
    if not ok:
        fails.append(name)


def snapshot():
    """全表内容指纹（行ID + 该行所有列内容），用于校验读路径零副作用"""
    digest = {}
    with session_scope() as s:
        for tbl in ('plan_plans', 'plan_cards', 'plan_slots'):
            rows = s.execute(text(f'SELECT * FROM {tbl}')).fetchall()
            items = sorted(str(r) for r in rows)
            digest[tbl] = (len(rows), _hashlib.md5('\n'.join(items).encode('utf-8')).hexdigest())
    return digest


print('=== 1. 基线快照 ===')
base = snapshot()
for tbl, (n, md5) in base.items():
    print(f'  {tbl}: {n} 行 md5={md5[:12]}')
with session_scope() as s:
    rows = s.execute(text('SELECT id, tasks IS NULL AS is_null FROM plan_cards ORDER BY id')).fetchall()
null_cards = [r[0] for r in rows if r[1]]
print(f'  卡片 {len(rows)} 张，其中 tasks 为 NULL（待迁移）：{len(null_cards)} 张')

print('\n=== 2. 依次调用三个 GET 读接口（捕获 DML） ===')
DML.clear()
t0 = time.time()
resp_list = client.get('/plan/api/list')
t_list = time.time() - t0
body = resp_list.get_json()
check('GET /plan/api/list code=200', body.get('code') == 200,
      f'HTTP {resp_list.status_code} 耗时 {t_list:.2f}s')
check('GET /plan/api/list 耗时 < 5s（不再撞锁等待）', t_list < 5, f'{t_list:.2f}s')

plans = body.get('data') or []
card_ids = [c.get('id') for p in plans for c in ((p.get('stats') or {}).get('cards') or [])]
check('响应含卡片数据', len(card_ids) > 0, f'{len(plans)} 个计划 / {len(card_ids)} 张卡')

pid = (plans[0].get('id') if plans else '')
for cid in card_ids[:3]:   # 每张卡 detail 都要整树加载，抽样 3 张即可
    t1 = time.time()
    d = client.get(f'/plan/api/card-detail?plan_id={pid}&card_id={cid}')
    dj = d.get_json()
    dd = dj.get('data') or {}
    check(f'card-detail {cid} code=200 且含 tasks',
          dj.get('code') == 200 and isinstance(dd.get('tasks'), list),
          f'任务数 {len(dd.get("tasks") or [])} 耗时 {time.time()-t1:.2f}s')

t2 = time.time()
r_today = client.get('/plan/api/today-status')
check('GET /plan/api/today-status code=200', r_today.get_json().get('code') == 200,
      f'耗时 {time.time()-t2:.2f}s')

check('三个 GET 接口期间 DML 数量 = 0', len(DML) == 0, f'捕获 {len(DML)} 条')
for d in DML[:8]:
    print('    ->', d)

print('\n=== 3. 副作用校验：全表快照前后一致 ===')
after = snapshot()
for tbl in base:
    check(f'{tbl} 快照未变化', base[tbl] == after[tbl],
          f'前 {base[tbl]} 后 {after[tbl]}')

print('\n=== 4. 幂等校验：任务树推导确定性（纯内存，不碰库） ===')
legacy_card = {
    'id': 'learn_demo', 'created_at': '2026-09-01 00:00:00',
    'milestones': [{'content': '交接工作', 'done': True}, {'content': '写文档', 'done': False}],
    'todos': [{'content': '买咖啡', 'done': False}],
    'slots': [],
}


def derive_ids():
    data = {'plans': [{'type': 'learn', 'cards': [dict(legacy_card, milestones=legacy_card['milestones'],
                                                       todos=legacy_card['todos'])]}], 'initialized': True}
    pr._migrate_tasks(data)
    return [t['id'] for t in data['plans'][0]['cards'][0]['tasks']]


ids1, ids2 = derive_ids(), derive_ids()
check('同一份旧数据两次推导 id 序列一致', ids1 == ids2 and len(ids1) == 3, str(ids1))
check('迁移节点使用确定性前缀 task_m', all(i.startswith('task_m') for i in ids1))

with session_scope() as s:
    d1 = pr._load_plans(s)
    seq1 = [(c.get('id'), [t['id'] for t in (c.get('tasks') or [])])
            for p in d1.get('plans', []) for c in p.get('cards', [])]
    d2 = pr._load_plans(s)
    seq2 = [(c.get('id'), [t['id'] for t in (c.get('tasks') or [])])
            for p in d2.get('plans', []) for c in p.get('cards', [])]
check('真实数据两次加载 task id 序列一致', seq1 == seq2, f'{len(seq1)} 张卡')

print('\n=== 5. 写校验分支不落库（无效 task_id 预期 400） ===')
tgt = None
for p in plans:
    for c in (p.get('stats') or {}).get('cards') or []:
        if c.get('status') in ('pending', 'in_progress'):
            tgt = (p['id'], c['id'])
            break
    if tgt:
        break
if tgt:
    DML.clear()
    rr = client.post('/plan/api/task-delete', json={'plan_id': tgt[0], 'card_id': tgt[1],
                                                   'task_id': 'not_exist_xxx'})
    check('POST task-delete 无效 id → 400 且零 DML',
          rr.get_json().get('code') == 400 and len(DML) == 0,
          f'code {rr.get_json().get("code")}，DML {len(DML)} 条')
else:
    print('  无可编辑卡片，跳过')

print('\n=== 结论 ===')
if fails:
    print(f'失败 {len(fails)} 项：')
    for f in fails:
        print('  -', f)
    sys.exit(1)
print('全部通过：读路径零 DML，任务卡接口已恢复')
