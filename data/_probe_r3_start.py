# -*- coding: utf-8 -*-
"""只读：打印学习/交易两计划 round=3 卡的 start_time 与关键状态。
用法: python data/_probe_r3_start.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto import plan_routes as pr
from crypto.database import session_scope

with session_scope() as s:
    data = pr._load_plans(s)

for pid in ('plan_learn_1000', 'plan_trade_1000'):
    plan = next((p for p in data['plans'] if p['id'] == pid), None)
    if not plan:
        print('缺计划', pid)
        continue
    for c in plan['cards']:
        if c.get('round') != 3:
            continue
        print('%s r3 type=%s status=%s start_time=%r end_time=%r filled=%s settlement=%s' % (
            pid, c.get('type'), c.get('status'), c.get('start_time'), c.get('end_time'),
            pr._calc_total_hours(c.get('slots', [])),
            'null' if not c.get('settlement') else (c.get('settlement') or {}).get('final_reward')))
