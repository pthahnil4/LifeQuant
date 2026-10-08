# -*- coding: utf-8 -*-
"""倒计时（第三个结束条件）纯函数探针 + 向后兼容契约自检。零写库。
用法: python data/_countdown_probe.py
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto import plan_routes as pr

TODAY = datetime.date.today()


def d(offset):
    """相对今天 offset 天的 'YYYY-MM-DD HH:MM:SS'"""
    return (TODAY + datetime.timedelta(days=offset)).strftime('%Y-%m-%d 00:00:00')


def mkcard(ctype, status, start, settled=False, filled=0):
    slots = [{'filled': True, 'filled_at': d(0), 'record': {'duration_minutes': 60}}
             for _ in range(filled)]
    c = {'id': ctype + '_x', 'type': ctype, 'status': status, 'round': 3,
         'start_time': start, 'slots': slots, 'tasks': [], 'notes': [], 'reward': 2000,
         'base_reward': 2000, 'hourly_rate': 20}
    if settled:
        c['settlement'] = {'final_reward': 1}
    return c


plan = {'daily_rule': {'countdown_days': 10}}
fails = []


def chk(name, cond, got=None):
    print(('  ok ' if cond else '  FAIL ') + name + ('' if cond else '  got=%r' % (got,)))
    if not cond:
        fails.append(name)


print('== 1. days_remaining / expired 数学（in_progress 未结算）==')
for off, exp_rem, exp_exp in [(0, 10, False), (5, 5, False), (9, 1, False),
                              (10, 0, True), (15, 0, True)]:
    c = mkcard('learn', 'in_progress', d(-off))
    ri = pr._calc_card_reward(plan, c)
    chk('start -%dd → remaining=%s expired=%s' % (off, exp_rem, exp_exp),
        ri['days_remaining'] == exp_rem and ri['countdown_expired'] == exp_exp
        and ri['countdown_active'] is True and ri['days_elapsed'] == min(off, off),
        (ri['days_remaining'], ri['countdown_expired'], ri['days_elapsed']))

print('== 2. 未来 start_time（elapsed 夹到 0，remaining=上限）==')
ri = pr._calc_card_reward(plan, mkcard('learn', 'in_progress', d(+3)))
chk('start +3d → elapsed=0 remaining=10 not expired',
    ri['days_elapsed'] == 0 and ri['days_remaining'] == 10 and ri['countdown_expired'] is False,
    (ri['days_elapsed'], ri['days_remaining']))

print('== 3. completed / 已结算 卡不受影响（remaining=0,active=False）==')
for c in (mkcard('learn', 'completed', d(-30), settled=True),
          mkcard('trade', 'completed', d(-30), settled=True),
          mkcard('learn', 'pending', '')):
    ri = pr._calc_card_reward(plan, c)
    chk('%s/%s 不纳入倒计时' % (c['type'], c['status']),
        ri['countdown_active'] is False and ri['days_remaining'] == 0
        and ri['countdown_expired'] is False,
        (ri['countdown_active'], ri['days_remaining'], ri['countdown_expired']))

print('== 4. daily_rule.countdown_days 覆盖 & 关闭 ==')
chk('覆盖为 5 天', pr._countdown_days({'daily_rule': {'countdown_days': 5}}) == 5)
chk('缺省用默认常量10', pr._countdown_days({'daily_rule': {}}) == pr.PLAN_COUNTDOWN_DEFAULT)
c5 = mkcard('learn', 'in_progress', d(-6))
ri5 = pr._calc_card_reward({'daily_rule': {'countdown_days': 5}}, c5)
chk('上限5天时 -6d 已到期', ri5['countdown_expired'] is True and ri5['days_remaining'] == 0,
    (ri5['countdown_expired'], ri5['days_remaining']))
rioff = pr._calc_card_reward({'daily_rule': {'countdown_days': 0}}, mkcard('learn', 'in_progress', d(-99)))
chk('countdown_days=0 关闭：不 active 不 expired',
    rioff['countdown_active'] is False and rioff['countdown_expired'] is False, rioff['countdown_expired'])

print('== 5. _apply_countdown_settlements 只结算到期卡并解锁下一张 ==')
p2 = {'type': 'learn', 'daily_rule': {'countdown_days': 10}, 'cards': [
    dict(mkcard('learn', 'in_progress', d(-12)), round=3, id='r3'),
    {'id': 'r4', 'type': 'learn', 'status': 'pending', 'round': 4, 'start_time': '',
     'slots': [], 'tasks': [], 'notes': [], 'reward': 2000, 'base_reward': 2000, 'hourly_rate': 20},
]}
settled = pr._apply_countdown_settlements(p2)
r3 = next(c for c in p2['cards'] if c['id'] == 'r3')
r4 = next(c for c in p2['cards'] if c['id'] == 'r4')
chk('到期 r3 被结算', 'r3' in settled and r3['status'] in ('completed', 'failed') and bool(r3.get('settlement')),
    (settled, r3['status']))
chk('r3 未满100/树未完 → 终止为 failed', r3['status'] == 'failed', r3['status'])
chk('下一张 r4 解锁为 in_progress', r4['status'] == 'in_progress', r4['status'])

print('== 5b. 到期交易卡：未满100格且树未完 → failed 终止（与学习卡同口径）==')
p2b = {'type': 'trade', 'daily_rule': {'countdown_days': 10}, 'cards': [
    dict(mkcard('trade', 'in_progress', d(-12)), round=3, id='t3'),
    {'id': 't4', 'type': 'trade', 'status': 'pending', 'round': 4, 'start_time': '',
     'slots': [], 'tasks': [], 'notes': [], 'reward': 2000, 'base_reward': 2000, 'hourly_rate': 20},
]}
settled_b = pr._apply_countdown_settlements(p2b)
t3 = next(c for c in p2b['cards'] if c['id'] == 't3')
t4 = next(c for c in p2b['cards'] if c['id'] == 't4')
chk('到期交易卡被结算', settled_b == ['t3'] and bool(t3.get('settlement')), (settled_b, t3.get('settlement')))
chk('未满100格且树未完 → failed', t3['status'] == 'failed', t3['status'])
chk('无余额时 growth_known=False 且 growth_pct=None（仅记录不参与判定）',
    t3['settlement'].get('growth_known') is False and t3['settlement'].get('growth_pct') is None,
    t3['settlement'])
chk('下一张 t4 解锁为 in_progress', t4['status'] == 'in_progress', t4['status'])

print('== 5c. 对照组：亏钱的到期交易卡只要树全完成仍 completed（增长不再判死）==')
tree_card = mkcard('trade', 'in_progress', d(-12))
tree_card['id'] = 't5'
tree_card['tasks'] = [{'id': 'n1', 'title': '任务A', 'status': 'done', 'children': []}]
tree_card['slots'] = [
    {'filled': True, 'filled_at': d(-10), 'record': {'duration_minutes': 60, 'account_balance': '1000'}},
    {'filled': True, 'filled_at': d(-1), 'record': {'duration_minutes': 60, 'account_balance': '930'}},
]
p2c = {'type': 'trade', 'daily_rule': {'countdown_days': 10}, 'cards': [tree_card]}
settled_c = pr._apply_countdown_settlements(p2c)
chk('树全完成的交易卡到期被结算', settled_c == ['t5'], settled_c)
chk('增长 -7% 仍判 completed（8% 线不是通关标准）', tree_card['status'] == 'completed', tree_card['status'])
chk('settlement 仍记录增长快照 -7%', tree_card['settlement'].get('growth_pct') == -7.0,
    tree_card['settlement'].get('growth_pct'))
chk('通关奖金不归零', tree_card['settlement'].get('final_reward') > 0,
    tree_card['settlement'].get('final_reward'))

print('== 6. 未到期不结算（幂等，不误伤进行中卡）==')
p3 = {'type': 'learn', 'daily_rule': {'countdown_days': 10}, 'cards': [
    dict(mkcard('learn', 'in_progress', d(-3)), round=3, id='r3')]}
chk('-3d 不结算', pr._apply_countdown_settlements(p3) == [] and p3['cards'][0]['status'] == 'in_progress')

print('== 7. 向后兼容：长期主义计划（无 target_hours）不含 penalty 字段，但含 days_remaining ==')
ltp = {'daily_rule': {'mode': 'longtermism'}}
liri = pr._calc_card_reward(ltp, mkcard('learn', 'in_progress', d(-2)))
chk('无 total_penalty 键', 'total_penalty' not in liri)
chk('仍返回 days_remaining', liri.get('days_remaining') == 8, liri.get('days_remaining'))

print('\n' + ('❌ 失败: ' + '；'.join(fails) if fails else '✅ 全部通过'))
sys.exit(1 if fails else 0)
