# -*- coding: utf-8 -*-
"""只读：校验学习 r3 任务树与打卡的一致性——所有 task_links 命中本卡树、
所有 done 节点的 completed_by_slot 指向本卡已勾选格子。r2 应为空树且无残留关联。
用法: python data/_check_orphans.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'data'))

import merge_learn_card as mlc
from crypto import plan_routes as pr
from crypto.database import session_scope

mlc._CTYPE = 'learn'
with session_scope() as s:
    data = pr._load_plans(s)
    plan = next(p for p in data['plans'] if p['id'] == 'plan_learn_1000')

fails = []
for rnd in (2, 3):
    c = mlc.pick_by_round(plan, rnd)
    tree_ids = {n.get('id') for n in mlc.walk_nodes(c.get('tasks') or [])}
    by_idx = {int(sl.get('slot_index')): sl for sl in (c.get('slots') or [])
              if sl.get('slot_index') is not None}
    orph = 0
    for sl in mlc.filled_slots(c):
        for link in (sl.get('record') or {}).get('task_links') or []:
            if link.get('task_id') not in tree_ids:
                orph += 1
    bad_basis = 0
    for n in mlc.walk_nodes(c.get('tasks') or []):
        cbs = n.get('completed_by_slot')
        if n.get('status') == 'done' and cbs is not None:
            slot = by_idx.get(int(cbs))
            if slot is None or not slot.get('filled'):
                bad_basis += 1
    print('r%s status=%s 根=%s 节点=%s links命中失败=%s 完成依据坏指=%s' % (
        rnd, c.get('status'), len(c.get('tasks') or []), len(tree_ids), orph, bad_basis))
    if orph:
        fails.append('r%s 有 %s 处孤儿打卡关联' % (rnd, orph))
    if bad_basis:
        fails.append('r%s 有 %s 个完成依据指向空/非勾选格' % (rnd, bad_basis))

if mlc.pick_by_round(plan, 2).get('tasks'):
    fails.append('r2 仍残留任务树')

print('\n' + ('❌ ' + '；'.join(fails) if fails else '✅ 一致：无孤儿关联、无坏完成依据、r2 树已清空'))
