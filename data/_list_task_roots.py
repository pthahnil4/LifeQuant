# -*- coding: utf-8 -*-
"""只读：打印学习任务卡 r2 / r3 的任务树顶层根节点，判断合并回滚后 r2 是否只剩无关联的占位节点。
用法: python data/_list_task_roots.py
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


def count_leaf_links(card):
    """本卡任务树被本卡打卡引用的节点数（判断能否安全搬走）"""
    return mlc.count_links(card)


with session_scope() as s:
    data = pr._load_plans(s)
    plan = next(p for p in data['plans'] if p['id'] == 'plan_learn_1000')

out = os.path.join(ROOT, 'data', '_task_roots.txt')
with open(out, 'w', encoding='utf-8') as f:
    for rnd in (2, 3):
        c = mlc.pick_by_round(plan, rnd)
        f.write('=== round=%s status=%s slots_filled=%s notes=%s links=%s basis_nodes=%s\n' % (
            rnd, c.get('status'), pr._calc_total_hours(c.get('slots', [])),
            len(c.get('notes') or []), mlc.count_links(c), mlc.nodes_with_basis(c)))
        for n in (c.get('tasks') or []):
            kids = len(n.get('children') or [])
            f.write('   ROOT %s [%s] est=%s cbs=%s kids=%s  %s\n' % (
                n.get('id'), n.get('status'), n.get('estimated_minutes'),
                n.get('completed_by_slot'), kids, n.get('title')))
print('written', out)
