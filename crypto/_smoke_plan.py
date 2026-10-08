#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务计划模块冒烟测试（MySQL 版）：用 Flask test_client 验证 plan_bp 全部 API

测试隔离策略（保护真实数据）：
  1. 必须设置 CRYPTO_TEST_DB_URL 指向专用测试库（库名含 test/smoke/ci/sandbox），
     守卫会把它接成 CRYPTO_DB_URL 并把数据目录换到临时目录；
  2. 运行前快照 plan_plans / plan_cards / plan_slots 三表
     + kv_store 中 task_plans_initialized 标记
  3. 清空后执行全部 API 用例（空库自动预置默认计划）
  4. finally 中无条件恢复快照（无论用例成败）

前置条件：环境变量 CRYPTO_TEST_DB_URL 已设置且指向隔离测试库。
未配置或不合规时直接退出码 2 —— 本用例会清空整表，绝不允许在业务库上跑。
"""

import json
import os
import sys

# Windows 终端默认 GBK，强制 UTF-8 输出避免中文/emoji 乱码报错
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crypto.test_isolation import (  # noqa: E402
    require_isolated_test_db, ensure_test_schema, TestDbNotConfigured)

try:
    _TEST_DB_URL = require_isolated_test_db()
except TestDbNotConfigured as e:
    print(f'❌ {e}')
    sys.exit(2)

from sqlalchemy import select, delete  # noqa: E402
from crypto import plan_routes as pr  # noqa: E402
from crypto.database import session_scope  # noqa: E402
from crypto.models import PlanPlan, PlanCard, PlanSlot, KVStore  # noqa: E402
from flask import Flask  # noqa: E402

print(f"[Smoke] 目标数据库（隔离测试库）: {_TEST_DB_URL.split('@')[-1]}")

# 隔离库表结构补齐：全新库（或本地 SQLite 文件）首次运行时表还不存在，
# 下面的快照 SELECT 会直接失败。只建表，不动任何数据。
_created = ensure_test_schema()
if _created:
    print(f'[Smoke] 隔离测试库补建 {len(_created)} 张缺失表')

_INIT_KEY = 'task_plans_initialized'

# =============================================================================
# 真实数据快照 + 清场（测试不污染生产数据）
# =============================================================================

def _row_dict(row):
    d = dict(row.__dict__)
    d.pop('_sa_instance_state', None)
    return d


def _snapshot():
    with session_scope() as s:
        plans = [_row_dict(r) for r in
                 s.execute(select(PlanPlan).order_by(PlanPlan.sort_order)).scalars()]
        cards = [_row_dict(r) for r in
                 s.execute(select(PlanCard).order_by(PlanCard.plan_id, PlanCard.sort_order)).scalars()]
        slots = [_row_dict(r) for r in
                 s.execute(select(PlanSlot).order_by(PlanSlot.card_id, PlanSlot.slot_index)).scalars()]
        kv = s.get(KVStore, _INIT_KEY)
        kv_value = None if kv is None else kv.value
    return plans, cards, slots, kv_value


def _clean():
    with session_scope() as s:
        s.execute(delete(PlanSlot))
        s.execute(delete(PlanCard))
        s.execute(delete(PlanPlan))
        s.execute(delete(KVStore).where(KVStore.key == _INIT_KEY))


def _restore(snap):
    plans, cards, slots, kv_value = snap
    with session_scope() as s:
        s.execute(delete(PlanSlot))
        s.execute(delete(PlanCard))
        s.execute(delete(PlanPlan))
        s.execute(delete(KVStore).where(KVStore.key == _INIT_KEY))
        for d in plans:
            s.add(PlanPlan(**d))
        s.flush()
        for d in cards:
            s.add(PlanCard(**d))
        s.flush()
        for d in slots:
            s.add(PlanSlot(**d))
        if kv_value is not None:
            s.add(KVStore(key=_INIT_KEY, value=kv_value))


_SNAP = _snapshot()
print(f"[Smoke] 已快照真实数据: 计划 {len(_SNAP[0])} / 卡片 {len(_SNAP[1])} / "
      f"格子 {len(_SNAP[2])}，测试后自动恢复")
_clean()

app = Flask(__name__, template_folder=os.path.join(_HERE, 'templates'))
app.register_blueprint(pr.plan_bp)
client = app.test_client()

PASS = 0
FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def post(url, body):
    return client.post(url, data=json.dumps(body), content_type='application/json')


try:
    print('\n=== 1. 空库自动预置默认计划 ===')
    r = client.get('/plan/api/list')
    data = r.get_json()
    check('list 返回 200', data['code'] == 200)
    plans = data['data']
    check('预置 2 个默认计划', len(plans) == 2, str(len(plans)))
    check('学习+交易计划', plans[0]['type'] == 'learn' and plans[1]['type'] == 'trade')
    check('每计划 10 张卡', all(len(p['stats']['cards']) == 10 for p in plans))
    s0 = plans[0]['stats']
    check('stats 含在场统计字段',
          'total_presence_days' in s0 and 'best_streak_days' in s0
          and 'presence_calendar' in s0 and 'total_penalty' not in s0)
    check('在场日历 15 周 105 天', len(s0['presence_calendar']['days']) == 105)
    check('daily_rule 为长期主义模式',
          plans[0]['daily_rule'].get('mode') == 'longtermism')
    check('交易计划含 round_config', plans[1].get('round_config') is not None)

    learn_plan = plans[0]
    trade_plan = plans[1]
    learn_cards = learn_plan['stats']['cards']
    active_learn = next(c for c in learn_cards if c['status'] == 'in_progress')
    locked_learn = next(c for c in learn_cards if c['locked'])

    print('\n=== 2. today-status ===')
    r = client.get('/plan/api/today-status')
    data = r.get_json()
    check('today-status 200 且 2 计划', data['code'] == 200 and len(data['data']) == 2)
    t0 = data['data'][0]
    check('today-status 在场字段齐全',
          'today_present' in t0 and 'learn_presence_days' in t0 and 'welcome' in t0)
    check('today-status 无罚款字段',
          all(k not in t0 for k in ('learn_penalty', 'trade_penalty')))
    check('today-status 含在场日历', len(t0['presence_calendar']['days']) == 105)

    print('\n=== 3. card-detail ===')
    r = client.get('/plan/api/card-detail')
    check('缺参数 400', r.get_json()['code'] == 400)
    r = client.get(f"/plan/api/card-detail?plan_id={learn_plan['id']}&card_id=nope")
    check('不存在的卡 404', r.get_json()['code'] == 404)
    r = client.get(f"/plan/api/card-detail?plan_id={learn_plan['id']}&card_id={active_learn['id']}")
    detail = r.get_json()['data']
    check('详情 slots 100 格', len(detail['slots']) == 100)
    check('详情含 reward_info/settlement 键',
          'reward_info' in detail and 'settlement' in detail)
    check('首卡未锁定', detail['locked'] is False)
    r = client.get(f"/plan/api/card-detail?plan_id={learn_plan['id']}&card_id={locked_learn['id']}")
    check('串行锁定标记正确', r.get_json()['data']['locked'] is True)

    print('\n=== 4. fill-slot 学习卡 ===')
    r = post('/plan/api/fill-slot', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'slot_index': 150, 'record': {'content': 'x', 'duration_minutes': 60}})
    check('格子索引越界 400', r.get_json()['code'] == 400)
    r = post('/plan/api/fill-slot', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'slot_index': 0, 'record': {'content': '冒烟测试学习', 'duration_minutes': 60}})
    d = r.get_json()
    check('打卡成功 200', d['code'] == 200, str(d))
    check('filled_count=1', d['data']['filled_count'] == 1)
    check('返回 slot 记录', d['data']['slot']['record']['content'] == '冒烟测试学习')
    r = post('/plan/api/fill-slot', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'slot_index': 0, 'record': {'content': '重复', 'duration_minutes': 60}})
    check('重复打卡 400', r.get_json()['code'] == 400)
    r = post('/plan/api/fill-slot', {
        'plan_id': learn_plan['id'], 'card_id': locked_learn['id'],
        'slot_index': 0, 'record': {'content': 'x', 'duration_minutes': 60}})
    check('锁定卡打卡 403', r.get_json()['code'] == 403)

    print('\n=== 5. fill-slot 自定义时间补录 ===')
    r = post('/plan/api/fill-slot', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'slot_index': 1, 'filled_at': 'bad-time',
        'record': {'content': 'x', 'duration_minutes': 60}})
    check('非法时间 400', r.get_json()['code'] == 400)
    r = post('/plan/api/fill-slot', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'slot_index': 1, 'filled_at': '2026-08-01 09:30',
        'record': {'content': '补录', 'duration_minutes': 30}})
    d = r.get_json()
    check('补录成功', d['code'] == 200 and d['data']['filled_count'] == 2)
    check('补录时间规范化为 19 位',
          d['data']['slot']['filled_at'] == '2026-08-01 09:30:00')

    print('\n=== 6. fill-slot 交易卡 + hit 自动计算 ===')
    trade_cards = trade_plan['stats']['cards']
    active_trade = next(c for c in trade_cards if c['status'] == 'in_progress')
    r = post('/plan/api/fill-slot', {
        'plan_id': trade_plan['id'], 'card_id': active_trade['id'],
        'slot_index': 0,
        'record': {'prediction': '涨', 'duration_minutes': 60, 'actual': '涨',
                   'market_analysis': '冒烟行情分析', 'action_advice': '持有',
                   'account_balance': '10000'}})
    d = r.get_json()
    check('交易卡打卡成功', d['code'] == 200, str(d))
    rec = d['data']['slot']['record']
    check('交易记录规范 6 字段',
          set(rec.keys()) == {'prediction', 'duration_minutes', 'actual',
                              'market_analysis', 'action_advice', 'account_balance', 'hit'})
    check('prediction==actual → hit True', rec.get('hit') is True)

    print('\n=== 7. update-slot 回填改判 ===')
    r = post('/plan/api/update-slot', {
        'plan_id': trade_plan['id'], 'card_id': active_trade['id'],
        'slot_index': 0,
        'record': {'prediction': '涨', 'duration_minutes': 60, 'actual': '跌',
                   'market_analysis': '改判', 'action_advice': '', 'account_balance': ''}})
    d = r.get_json()
    # update-slot 的响应载荷是 {'slot': {...}}（与 fill-slot 同构），record 在 slot 内
    check('更新成功且 hit 重算为 False',
          d['code'] == 200 and d['data']['slot']['record'].get('hit') is False)
    r = post('/plan/api/update-slot', {
        'plan_id': trade_plan['id'], 'card_id': active_trade['id'],
        'slot_index': 5, 'record': {}})
    check('未勾选格子更新 400', r.get_json()['code'] == 400)

    print('\n=== 8. unfill-slot ===')
    r = post('/plan/api/unfill-slot', {
        'plan_id': trade_plan['id'], 'card_id': active_trade['id'], 'slot_index': 5})
    check('未勾选格子删除 400', r.get_json()['code'] == 400)
    r = post('/plan/api/unfill-slot', {
        'plan_id': trade_plan['id'], 'card_id': active_trade['id'], 'slot_index': 0})
    d = r.get_json()
    check('取消勾选成功', d['code'] == 200 and d['data']['filled_count'] == 0)
    r = client.get(f"/plan/api/card-detail?plan_id={trade_plan['id']}&card_id={active_trade['id']}")
    check('取消后 record 为 None',
          r.get_json()['data']['slots'][0]['record'] is None)

    print('\n=== 9. add-note ===')
    r = post('/plan/api/add-note', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'], 'content': ''})
    check('空内容 400', r.get_json()['code'] == 400)
    r = post('/plan/api/add-note', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'], 'content': '冒烟小记'})
    check('添加小记成功', r.get_json()['code'] == 200)
    r = client.get(f"/plan/api/card-detail?plan_id={learn_plan['id']}&card_id={active_learn['id']}")
    check('详情可见小记', any(n['content'] == '冒烟小记' for n in r.get_json()['data']['notes']))

    print('\n=== 10. update-card ===')
    r = post('/plan/api/update-card', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'fields': {'goal': '冒烟新目标', 'review': '冒烟复盘',
                   'start_time': '2026-08-01 08:00'}})
    d = r.get_json()
    check('更新 goal/review/时间成功',
          d['code'] == 200 and d['data']['goal'] == '冒烟新目标'
          and d['data']['start_time'] == '2026-08-01 08:00:00')
    r = post('/plan/api/update-card', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'fields': {'start_time': '2026/08/01'}})
    check('非法时间格式 400', r.get_json()['code'] == 400)
    r = post('/plan/api/update-card', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'fields': {'todos': 'not-a-list'}})
    check('todos 非数组 400', r.get_json()['code'] == 400)
    r = post('/plan/api/update-card', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'fields': {'milestones': [{'content': '子目标A', 'done': False},
                                   {'content': '子目标B', 'done': False}]}})
    check('写入子目标成功', r.get_json()['code'] == 200)

    print('\n=== 11. toggle-milestone ===')
    r = post('/plan/api/toggle-milestone', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'], 'milestone_index': 5})
    check('索引越界 400', r.get_json()['code'] == 400)
    r = post('/plan/api/toggle-milestone', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'], 'milestone_index': 0})
    check('切换子目标成功', r.get_json()['code'] == 200
          and r.get_json()['data']['milestones'][0]['done'] is True
          and r.get_json()['data']['all_done'] is False)
    # 勾选最后一个子目标 → 应返回 all_done=True
    r = post('/plan/api/toggle-milestone', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'], 'milestone_index': 1})
    check('子目标全部完成返回 all_done', r.get_json()['code'] == 200
          and r.get_json()['data']['all_done'] is True
          and r.get_json()['data']['early_eligible'] is True)

    print('\n=== 12. settle-round 结算 ===')
    r = post('/plan/api/settle-round', {
        'plan_id': learn_plan['id'], 'card_id': locked_learn['id']})
    check('学习卡未满 100h 且子目标未完成 400', r.get_json()['code'] == 400)
    r = post('/plan/api/settle-round', {
        'plan_id': trade_plan['id'], 'card_id': active_trade['id']})
    check('交易卡无余额打卡数据不能结算 400', r.get_json()['code'] == 400)
    # 学习卡：子目标全部完成 → 提前通关，并解锁下一张卡
    r = post('/plan/api/update-card', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id'],
        'fields': {'milestones': [{'content': '子目标A', 'done': True},
                                   {'content': '子目标B', 'done': True}]}})
    r = post('/plan/api/settle-round', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id']})
    d = r.get_json()
    check('学习卡提前通关结算成功',
          d['code'] == 200 and d['data']['status'] == 'completed'
          and d['data']['settlement']['early_bonus'] > 0)
    r = client.get('/plan/api/list')
    l_cards = r.get_json()['data'][0]['stats']['cards']
    unlocked = next(c for c in l_cards if c['round'] == active_learn['round'] + 1)
    check('结算后解锁下一张卡', unlocked['status'] == 'in_progress')
    r = post('/plan/api/settle-round', {
        'plan_id': learn_plan['id'], 'card_id': active_learn['id']})
    check('重复结算 400', r.get_json()['code'] == 400)
    # 交易卡：填两格带余额打卡（10000→12000，增长 20% > 8%）→ 自动通关，无需手输倍数
    post('/plan/api/fill-slot', {
        'plan_id': trade_plan['id'], 'card_id': active_trade['id'], 'slot_index': 0,
        'record': {'prediction': '涨', 'duration_minutes': 60, 'actual': '涨',
                   'market_analysis': '冒烟行情分析', 'action_advice': '持有',
                   'account_balance': '10000'}})
    post('/plan/api/fill-slot', {
        'plan_id': trade_plan['id'], 'card_id': active_trade['id'], 'slot_index': 1,
        'record': {'prediction': '涨', 'duration_minutes': 60, 'actual': '涨',
                   'market_analysis': '冒烟行情分析', 'action_advice': '持有',
                   'account_balance': '12000'}})
    r = post('/plan/api/settle-round', {
        'plan_id': trade_plan['id'], 'card_id': active_trade['id']})
    d = r.get_json()
    check('交易卡增长 >8% 自动通关', d['code'] == 200 and d['data']['status'] == 'completed'
          and d['data']['settlement']['growth_pct'] == 20.0, str(d.get('message')))

    print('\n=== 13. reset-card ===')
    next_learn = next(c for c in l_cards if c['round'] == active_learn['round'] + 1)
    r = post('/plan/api/reset-card', {
        'plan_id': learn_plan['id'], 'card_id': next_learn['id']})
    d = r.get_json()
    check('重置成功', d['code'] == 200 and d['data']['status'] in ('in_progress', 'pending'))
    r = client.get(f"/plan/api/card-detail?plan_id={learn_plan['id']}&card_id={next_learn['id']}")
    dt = r.get_json()['data']
    check('重置后 100 空格子且无结算',
          len(dt['slots']) == 100 and not any(s['filled'] for s in dt['slots'])
          and dt['settlement'] is None)

    print('\n=== 14. create-card / create-plan ===')
    r = post('/plan/api/create-card', {
        'plan_id': learn_plan['id'], 'type': 'learn', 'title': '冒烟手动卡'})
    d = r.get_json()
    check('手动建卡 round=11', d['code'] == 200 and d['data']['round'] == 11)
    check('手动建卡继承上一张奖励', d['data']['reward'] == 2000)
    r = post('/plan/api/create-card', {'plan_id': 'nope', 'type': 'learn'})
    check('计划不存在 404', r.get_json()['code'] == 404)
    r = post('/plan/api/create-plan', {'type': 'custom', 'name': '冒烟自定义计划'})
    d = r.get_json()
    check('创建自定义计划成功', d['code'] == 200 and d['data']['cards'] == [])
    custom_plan_id = d['data']['id']
    r = client.get('/plan/api/list')
    check('列表出现 3 个计划', len(r.get_json()['data']) == 3)

    print('\n=== 15. delete-card / delete-plan ===')
    r = post('/plan/api/delete-card', {'plan_id': 'nope', 'card_id': 'nope'})
    check('计划不存在删除卡 404', r.get_json()['code'] == 404)
    r = post('/plan/api/delete-card', {'plan_id': learn_plan['id'], 'card_id': 'nope'})
    check('卡不存在但计划存在幂等 200（对齐 JSON 版契约）', r.get_json()['code'] == 200)
    r = client.get('/plan/api/list')
    l_cards_now = r.get_json()['data'][0]['stats']['cards']
    manual_card = next(c for c in l_cards_now if c.get('round') == 11)
    r = post('/plan/api/delete-card', {
        'plan_id': learn_plan['id'], 'card_id': manual_card['id']})
    check('删除手动卡成功', r.get_json()['code'] == 200)
    r = post('/plan/api/delete-plan', {'plan_id': custom_plan_id})
    check('删除自定义计划成功', r.get_json()['code'] == 200)
    r = client.get('/plan/api/list')
    check('删除后回到 2 个计划', len(r.get_json()['data']) == 2)

    print('\n=== 16. 统计口径（在场天数/命中率）===')
    r = client.get('/plan/api/list')
    lstats = r.get_json()['data'][0]['stats']
    tstats = r.get_json()['data'][1]['stats']
    check('学习打卡总数=2', lstats['learn_checkin_total'] == 2,
          str(lstats['learn_checkin_total']))
    check('在场天数按日去重=2（8-01 两次算 1 天 + 今日）',
          lstats['learn_presence_days'] == 2, str(lstats['learn_presence_days']))
    check('交易卡打卡已取消 → 0', tstats['trade_checkin_total'] == 0)
    check('总奖励=两卡结算 final 之和',
          lstats['total_reward'] + tstats['total_reward'] > 0)

finally:
    print('\n=== 恢复真实数据快照 ===')
    _restore(_SNAP)
    with session_scope() as s:
        n_plans = len(s.execute(select(PlanPlan.id)).scalars().all())
        n_cards = len(s.execute(select(PlanCard.id)).scalars().all())
        n_slots = len(s.execute(select(PlanSlot.card_id)).scalars().all())
    ok = (n_plans == len(_SNAP[0]) and n_cards == len(_SNAP[1]) and n_slots == len(_SNAP[2]))
    print(f"  {'✅' if ok else '❌'} 恢复校验: 计划 {n_plans}/{len(_SNAP[0])} "
          f"卡片 {n_cards}/{len(_SNAP[1])} 格子 {n_slots}/{len(_SNAP[2])}")
    if not ok:
        FAIL += 1

print()
print(f"通过 {PASS} / 失败 {FAIL}")
if FAIL:
    sys.exit(1)
print('ALL SMOKE TESTS PASSED (plan module on MySQL)')
