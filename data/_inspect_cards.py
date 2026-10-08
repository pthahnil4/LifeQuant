# -*- coding: utf-8 -*-
"""read-only: dump learn/trade cards round/status/filled/presence/reward/settlement."""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto import plan_routes as pr
from crypto.database import session_scope

out = os.path.join(ROOT, 'data', '_inspect_cards.txt')
with session_scope() as s:
    data = pr._load_plans(s)

with open(out, 'w', encoding='utf-8') as f:
    for p in data['plans']:
        f.write('==== plan=%s type=%s daily_rule=%s\n' % (
            p['id'], p['type'], json.dumps(p.get('daily_rule'), ensure_ascii=False)))
        for c in sorted(p['cards'], key=lambda x: x.get('round', 0)):
            ri = pr._calc_card_reward(p, c)
            f.write('  r%s %s status=%s filled=%s presence=%s reward=%s start=%r notes=%s reviewlen=%s settlement=%s\n' % (
                c.get('round'), c.get('type'), c.get('status'), ri['filled_count'],
                ri['presence_days'], ri['final_reward'], c.get('start_time'),
                len(c.get('notes') or []), len((c.get('review') or '').strip()),
                json.dumps(c.get('settlement'), ensure_ascii=False)))
print('written', out)
