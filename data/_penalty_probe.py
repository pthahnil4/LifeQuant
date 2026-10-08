# -*- coding: utf-8 -*-
"""read-only probe: verify three-tier penalty on live cards."""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto import plan_routes as pr
from crypto.database import session_scope

with session_scope() as s:
    data = pr._load_plans(s)

out = os.path.join(ROOT, 'data', '_penalty_probe.txt')
with open(out, 'w', encoding='utf-8') as f:
    for p in data['plans']:
        for c in sorted(p['cards'], key=lambda x: x.get('round', 0)):
            ri = pr._calc_card_reward(p, c)
            if ri.get('penalty_enabled'):
                f.write('PENALTY %s r%s status=%s filled=%s start=%r -> penalty=%s (idle=%s normal=%s severe=%s judged=%s) gross_base+early=%s final=%s\n' % (
                    p['type'], c.get('round'), c.get('status'), ri['filled_count'],
                    c.get('start_time'), ri['total_penalty'], ri['penalty_idle_days'],
                    ri['penalty_normal_days'], ri['penalty_severe_days'],
                    ri['penalty_judged_days'], ri['base_reward'] + ri['early_bonus'],
                    ri['final_reward']))
    # synthetic: verify band math at hourly_rate=20
    rule = {'target_hours': 10, 'severe_hours': 6, 'normal_multiplier': 2, 'severe_multiplier': 4}
    for h in (12, 10, 8, 6, 5, 0):
        f.write('BAND day_hours=%s -> penalty=%s\n' % (h, pr._day_penalty_hours(h, rule, 20)))

# ---- 契约自检：长期主义计划 vs 旧版三档计划 ----
lt_plan = {'daily_rule': {'mode': 'longtermism'}}
lg_plan = {'daily_rule': {'mode': 'longtermism', 'target_hours': 10, 'severe_hours': 6,
                          'normal_multiplier': 2, 'severe_multiplier': 4}}
base_card = {'type': 'learn', 'reward': 2000, 'status': 'in_progress', 'start_time': '',
             'milestones': [], 'tasks': [], 'settlement': None,
             'slots': [{'filled': True, 'filled_at': '2026-08-07 10:00',
                        'record': {'duration_minutes': 60}}]}
import copy
ri_lt = pr._calc_card_reward(lt_plan, copy.deepcopy(base_card))
ri_lg = pr._calc_card_reward(lg_plan, copy.deepcopy(base_card))
with open(out, 'a', encoding='utf-8') as f:
    f.write('CONTRACT longtermism has total_penalty? %s (expect False)\n' % ('total_penalty' in ri_lt))
    f.write('CONTRACT longtermism final=%s (expect 3980: 2000+1980 early)\n' % ri_lt['final_reward'])
    f.write('CONTRACT legacy has total_penalty? %s enabled? %s (expect True/True)\n'
            % ('total_penalty' in ri_lg, ri_lg.get('penalty_enabled')))
    c_lt = copy.deepcopy(base_card); pr._settle_card(lt_plan, c_lt)
    f.write('CONTRACT longtermism settlement has penalty_total? %s (expect False)\n'
            % ('penalty_total' in c_lt['settlement']))
    c_lg = copy.deepcopy(base_card); c_lg['start_time'] = '2026-08-01 00:00:00'
    pr._settle_card(lg_plan, c_lg)
    f.write('CONTRACT legacy settlement has penalty_total? %s value=%s final=%s\n'
            % ('penalty_total' in c_lg['settlement'],
               c_lg['settlement'].get('penalty_total'), c_lg['settlement'].get('final_reward')))

print('written', out)
