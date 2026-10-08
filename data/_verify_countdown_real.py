# -*- coding: utf-8 -*-
"""真实数据端到端校验：走序列化路径确认 r3 的 days_remaining 字段与罚款已归零。零写库。
用法: python data/_verify_countdown_real.py
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
        continue
    stats = pr._build_plan_stats(plan)
    print('==== %s ====  countdown_days=%s' % (pid, pr._countdown_days(plan)))
    for cinfo in stats['cards']:
        if cinfo['round'] not in (2, 3):
            continue
        ri = cinfo['reward_info']
        print('  r%s %s: start=%s' % (cinfo['round'], cinfo['status'], cinfo.get('start_time')))
        print('     days_remaining=%s days_elapsed=%s countdown_expired=%s countdown_active=%s' % (
            ri.get('days_remaining'), ri.get('days_elapsed'),
            ri.get('countdown_expired'), ri.get('countdown_active')))
        print('     penalty_enabled=%s total_penalty=%s judged_days=%s final_reward=%s' % (
            ri.get('penalty_enabled'), ri.get('total_penalty'),
            ri.get('penalty_judged_days'), ri.get('final_reward')))
