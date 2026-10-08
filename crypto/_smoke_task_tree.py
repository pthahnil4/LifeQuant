# -*- coding: utf-8 -*-
"""任务树（任务管理 v2）冒烟测试：Flask test_client 验证任务树 + 打卡关联全链路

覆盖范围：
  1. 旧卡惰性迁移（milestones → tasks，预估待补为 0）
  2. task-add / task-update / task-delete / task-cancel-done 四个节点接口
  3. 打卡关联校验（容器 / 待补预估 / 已完成任务 三类拒绝）与状态联动
  4. 叶子加权进度（4h+5h+6h 场景 → 完成两个 60%）、父任务逐级上卷
  5. update-slot 差量回退 / unfill-slot 回退 / backfill-batch 统一 doing
  6. 任务树全部完成 → 学习卡提前通关；删除打卡 → 撤销结算
  7. reset-card 清空任务树；tasks/task_links 真实落库持久化
  8. 交易卡任务树可用性

测试隔离策略（保护真实数据）：
  1. 必须设置 CRYPTO_TEST_DB_URL 指向专用测试库（库名含 test/smoke/ci/sandbox），
     守卫会把它接成 CRYPTO_DB_URL 并把数据目录换到临时目录
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
    _DB_URL = require_isolated_test_db()
except TestDbNotConfigured as e:
    print(f'❌ {e}')
    sys.exit(2)

from crypto.database import session_scope, init_db  # noqa: E402

from sqlalchemy import select, delete  # noqa: E402
from crypto import plan_routes as pr  # noqa: E402
from crypto.models import PlanPlan, PlanCard, PlanSlot, KVStore  # noqa: E402
from flask import Flask  # noqa: E402

print(f"[Smoke] 目标数据库（隔离测试库）: {_DB_URL.split('@')[-1]}")

# 表结构补齐：全新隔离库（或本地 SQLite 文件）首次跑时表还不存在；只建表不动数据
ensure_test_schema()

# 列补丁：确保存量库已补上 tasks / task_links 列（与线上启动同机制）
init_db()

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


def detail(plan_id, card_id):
    r = client.get(f"/plan/api/card-detail?plan_id={plan_id}&card_id={card_id}")
    return r.get_json()['data']


def find_node(tasks, tid):
    """按 id 查找节点（深搜），未找到返回 None"""
    for t in tasks or []:
        if t.get('id') == tid:
            return t
        hit = find_node(t.get('children') or [], tid)
        if hit:
            return hit
    return None


def node_of(tasks, tid):
    return find_node(tasks, tid) or {}


def add_task(plan_id, card_id, title, est, parent_id=''):
    return post('/plan/api/task-add', {
        'plan_id': plan_id, 'card_id': card_id, 'title': title,
        'estimated_minutes': est, 'parent_id': parent_id})


try:
    print('\n=== 0. 空库预置与任务树基础字段 ===')
    r = client.get('/plan/api/list')
    data = r.get_json()
    check('list 返回 200 且预置 2 计划', data['code'] == 200, str(data)[:300])
    plans = data['data'] or []
    check('预置 2 个默认计划', len(plans) == 2, str(len(plans)))
    if len(plans) < 2:
        sys.exit(1)
    learn_plan, trade_plan = plans[0], plans[1]
    learn_cards = learn_plan['stats']['cards']
    active = next(c for c in learn_cards if c['status'] == 'in_progress')
    trade_active = next(c for c in trade_plan['stats']['cards'] if c['status'] == 'in_progress')
    check('列表摘要含 task_progress', 'task_progress' in learn_cards[0])
    dt = detail(learn_plan['id'], active['id'])
    check('新卡 tasks 为空数组（区别未迁移 NULL）', dt['tasks'] == [])
    check('card-detail 含任务树聚合字段',
          dt['tasks_all_done'] is False and dt['task_progress']['total_count'] == 0)

    print('\n=== 1. 旧卡惰性迁移（milestones → tasks）===')
    with session_scope() as s:
        row = s.get(PlanCard, active['id'])
        row.tasks = None
        row.milestones = json.dumps([
            {'content': '老目标A', 'done': False},
            {'content': '老目标B', 'done': True},
        ], ensure_ascii=False)
    dt = detail(learn_plan['id'], active['id'])
    tasks = dt['tasks']
    check('迁移生成 2 个顶层任务', len(tasks) == 2 and tasks[0]['title'] == '老目标A',
          str([t.get('title') for t in tasks]))
    check('旧A→todo / 旧B→done，预估待补为 0',
          tasks[0]['status'] == 'todo' and tasks[1]['status'] == 'done'
          and tasks[0]['estimated_minutes'] == 0 and tasks[1]['estimated_minutes'] == 0)
    check('待补预估不计入进度（pct=0）', dt['task_progress']['pending_estimate'] == 2
          and dt['task_progress']['pct'] == 0)
    dt2 = detail(learn_plan['id'], active['id'])
    check('迁移幂等（再次加载不重复生成）', len(dt2['tasks']) == 2)
    A_id, B_id = tasks[0]['id'], tasks[1]['id']
    r = post('/plan/api/fill-slot', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': 0,
        'record': {'content': 'x', 'duration_minutes': 60,
                   'task_links': [{'task_id': A_id, 'state': 'doing'}]}})
    check('关联待补预估任务 → 400', r.get_json()['code'] == 400)
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'task_id': A_id, 'estimated_minutes': 60})
    check('补全预估值成功', r.get_json()['code'] == 200)
    post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'task_id': B_id, 'estimated_minutes': 60})
    dt = detail(learn_plan['id'], active['id'])
    check('补全后进度 50%（B done 60/120）',
          dt['task_progress']['pct'] == 50 and dt['task_progress']['total_minutes'] == 120)
    r = post('/plan/api/task-delete', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'task_id': B_id})
    check('删除已完成任务成功', r.get_json()['code'] == 200)
    r = post('/plan/api/task-delete', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'task_id': A_id})
    check('清空迁移任务后树为空', r.get_json()['data']['tasks'] == [])

    print('\n=== 2. task-add（新增与校验）===')
    r = add_task(learn_plan['id'], active['id'], '寻找建站工具', 0)
    check('预估为 0 → 400', r.get_json()['code'] == 400)
    r = add_task(learn_plan['id'], active['id'], '   ', 60)
    check('空标题 → 400', r.get_json()['code'] == 400)
    r = add_task(learn_plan['id'], active['id'], '孤儿任务', 60, parent_id='task_nope')
    check('父任务不存在 → 400', r.get_json()['code'] == 400)
    r = add_task(learn_plan['id'], active['id'], '寻找建站工具', 240)
    d = r.get_json()
    T1 = d['data']['tasks'][0]['id']
    check('新增顶层任务成功（todo）', d['code'] == 200
          and d['data']['tasks'][0]['status'] == 'todo')
    r = add_task(learn_plan['id'], active['id'], '确定网站结构', 300)
    T2 = r.get_json()['data']['tasks'][1]['id']
    r = add_task(learn_plan['id'], active['id'], '部署上线', 60)  # 先作叶子占位
    T3 = r.get_json()['data']['tasks'][2]['id']
    r = add_task(learn_plan['id'], active['id'], '域名注册及解析', 120, parent_id=T3)
    d = r.get_json()
    t3n = node_of(d['data']['tasks'], T3)
    check('父任务转容器：预估 = Σ叶子（120）', t3n.get('estimated_minutes') == 120)
    r = add_task(learn_plan['id'], active['id'], '项目打包与部署配置', 240, parent_id=T3)
    d = r.get_json()
    t3n = node_of(d['data']['tasks'], T3)
    T31, T32 = t3n['children'][0]['id'], t3n['children'][1]['id']
    check('容器预估自动累加（120+240=360）',
          t3n.get('estimated_minutes') == 360 and len(t3n.get('children') or []) == 2)

    print('\n=== 3. 树形状与加权进度（4h+5h+6h=15h）===')
    dt = detail(learn_plan['id'], active['id'])
    tp = dt['task_progress']
    check('叶子口径：4 个叶子共 900 分钟（15h）',
          tp['total_minutes'] == 900 and tp['total_count'] == 4 and tp['pct'] == 0, str(tp))

    print('\n=== 4. task-update（重命名/移动/防成环/删除）===')
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'task_id': T1, 'title': '寻找建站工具与开源模板'})
    d = r.get_json()
    check('重命名成功', d['code'] == 200
          and node_of(d['data']['tasks'], T1).get('title') == '寻找建站工具与开源模板')
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'task_id': T3, 'estimated_minutes': 300})
    check('容器预估不可直接修改 → 400', r.get_json()['code'] == 400)
    r = add_task(learn_plan['id'], active['id'], '撰写文档', 60)
    T4 = r.get_json()['data']['tasks'][3]['id']
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'task_id': T4, 'move': 'up'})
    order = [t['id'] for t in r.get_json()['data']['tasks']]
    check('上移一位成功', order == [T1, T2, T4, T3], str(order))
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'task_id': T4, 'move': 'down'})
    order = [t['id'] for t in r.get_json()['data']['tasks']]
    check('下移复位', order == [T1, T2, T3, T4], str(order))
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'task_id': T4, 'parent_id': T3})
    d = r.get_json()
    t3n = node_of(d['data']['tasks'], T3)
    check('移动到容器下（预估 420，顶层减为 3）',
          t3n.get('estimated_minutes') == 420 and len(t3n.get('children') or []) == 3
          and len(d['data']['tasks']) == 3, str(d.get('data', {}).get('tasks')))
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'task_id': T3, 'parent_id': T4})
    check('移动到自身子任务下 → 400（防成环）', r.get_json()['code'] == 400)
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'task_id': T3, 'parent_id': T3})
    check('移动到自身 → 400', r.get_json()['code'] == 400)
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'task_id': T4, 'parent_id': ''})
    d = r.get_json()
    t3n = node_of(d['data']['tasks'], T3)
    check('移回顶层（容器预估回落 360）',
          t3n.get('estimated_minutes') == 360 and len(d['data']['tasks']) == 4)
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'task_id': T4})
    check('空字段更新幂等 200', r.get_json()['code'] == 200)
    r = post('/plan/api/task-update', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'task_id': 'task_nope', 'title': 'x'})
    check('任务不存在 → 400', r.get_json()['code'] == 400)
    r = post('/plan/api/task-delete', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'task_id': T4})
    d = r.get_json()
    check('删除 T4 后回到 3 顶层 / 总预估 900',
          len(d['data']['tasks']) == 3 and d['data']['task_progress']['total_minutes'] == 900)

    print('\n=== 5. 打卡关联（校验/联动/回退）===')

    def fill(slot, content, minutes, links):
        return post('/plan/api/fill-slot', {
            'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': slot,
            'record': {'content': content, 'duration_minutes': minutes, 'task_links': links}})

    r = fill(0, 'x', 60, [{'task_id': T3, 'state': 'doing'}])
    check('关联容器任务 → 400', r.get_json()['code'] == 400)
    r = fill(0, 'x', 60, [{'task_id': 'task_nope', 'state': 'done'}])
    check('关联不存在任务 → 400', r.get_json()['code'] == 400)
    dt = detail(learn_plan['id'], active['id'])
    check('校验失败不写数据（slot0 仍空）', dt['slots'][0]['filled'] is False)

    r = fill(0, '调研建站工具', 60, [{'task_id': T1, 'state': 'doing'}])
    d = r.get_json()
    check('打卡关联 doing 成功', d['code'] == 200)
    check('T1 置为进行中且记录回读',
          node_of(d['data']['refresh']['card']['tasks'], T1).get('status') == 'doing'
          and d['data']['slot']['record']['task_links']
          == [{'task_id': T1, 'state': 'doing'}])

    r = fill(1, '建站工具对比', 60, [{'task_id': T1, 'state': 'done'}])
    d = r.get_json()
    t1 = node_of(d['data']['refresh']['card']['tasks'], T1)
    check('打卡标记完成（completed_by_slot=1）',
          t1.get('status') == 'done' and t1.get('completed_by_slot') == 1)
    tp = d['data']['refresh']['card']['task_progress']
    check('进度 27%（240/900）', tp['pct'] == 27, str(tp))
    check('实际投入全额计入（T1 actual=120）', t1.get('actual_minutes') == 120)

    r = fill(2, '网站结构设计', 60, [{'task_id': T2, 'state': 'done'}])
    tp = r.get_json()['data']['refresh']['card']['task_progress']
    check('完成 T1+T2 → 进度 60%（9/15h）',
          tp['pct'] == 60 and tp['done_count'] == 2, str(tp))

    r = fill(5, 'x', 60, [{'task_id': T1, 'state': 'doing'}])
    check('关联已完成任务 → 400', r.get_json()['code'] == 400)

    r = post('/plan/api/update-slot', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': 0,
        'record': {'content': '调研建站工具', 'duration_minutes': 60,
                   'task_links': [{'task_id': T1, 'state': 'done'}]}})
    check('非完成依据槽标记 done → 400（完成依据唯一）', r.get_json()['code'] == 400)

    r = post('/plan/api/update-slot', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': 0,
        'record': {'content': '调研建站工具', 'duration_minutes': 60, 'task_links': []}})
    d = r.get_json()
    check('清空本槽关联成功且 T1 保持完成',
          d['code'] == 200
          and node_of(d['data']['refresh']['card']['tasks'], T1).get('status') == 'done')

    r = post('/plan/api/update-slot', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': 1,
        'record': {'content': '建站工具对比', 'duration_minutes': 60, 'task_links': []}})
    d = r.get_json()
    t1 = node_of(d['data']['refresh']['card']['tasks'], T1)
    check('删除完成依据 → T1 回退进行中',
          d['code'] == 200 and t1.get('status') == 'doing'
          and 'completed_by_slot' not in t1)
    check('回退后进度 33%（300/900）',
          d['data']['refresh']['card']['task_progress']['pct'] == 33)

    r = post('/plan/api/update-slot', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': 1,
        'record': {'content': '建站工具对比', 'duration_minutes': 60,
                   'task_links': [{'task_id': T1, 'state': 'done'}]}})
    check('重新标记完成（进度回 60%）',
          r.get_json()['data']['refresh']['card']['task_progress']['pct'] == 60)

    r = post('/plan/api/unfill-slot', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': 2})
    d = r.get_json()
    check('删除打卡 → T2 回退 doing',
          d['code'] == 200
          and node_of(d['data']['refresh']['card']['tasks'], T2).get('status') == 'doing')
    r = fill(2, '网站结构设计', 60, [{'task_id': T2, 'state': 'done'}])
    check('重打后 T2 恢复完成', r.get_json()['code'] == 200)

    print('\n=== 6. backfill-batch 批量补录（统一 doing）===')
    r = post('/plan/api/backfill-batch', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'entries': [
            {'filled_at': '2026-08-01 10:00',
             'record': {'content': '补录1', 'duration_minutes': 60}},
            {'filled_at': '2026-08-01 11:00',
             'record': {'content': '补录2', 'duration_minutes': 60}},
        ],
        'task_links': [{'task_id': T31, 'state': 'done'}]})
    d = r.get_json()
    check('批量补录成功 2 条', d['code'] == 200 and d['data']['filled'] == 2, str(d))
    dt = detail(learn_plan['id'], active['id'])
    check('补录记录携带统一关联（状态强制 doing）',
          (dt['slots'][3]['record'] or {}).get('task_links')
          == [{'task_id': T31, 'state': 'doing'}])
    check('T3.1 推进为进行中', node_of(dt['tasks'], T31).get('status') == 'doing')
    check('父任务随子推进 → doing', node_of(dt['tasks'], T3).get('status') == 'doing')

    r = post('/plan/api/backfill-batch', {
        'plan_id': learn_plan['id'], 'card_id': active['id'],
        'entries': [{'filled_at': '2026-08-02 10:00',
                     'record': {'content': 'x', 'duration_minutes': 60}}],
        'task_links': [{'task_id': 'task_nope'}]})
    check('补录关联不存在任务 → 400（整批拒绝）', r.get_json()['code'] == 400)

    for si in (3, 4):
        post('/plan/api/unfill-slot', {
            'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': si})
    dt = detail(learn_plan['id'], active['id'])
    check('删除补录打卡后 T3.1 保持进行中（doing 不回退）',
          node_of(dt['tasks'], T31).get('status') == 'doing')

    print('\n=== 7. task-cancel-done（取消完成）===')
    r = post('/plan/api/task-cancel-done', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'task_id': T3})
    check('容器任务不能单独取消 → 400', r.get_json()['code'] == 400)
    r = post('/plan/api/task-cancel-done', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'task_id': T1})
    d = r.get_json()
    check('取消 T1 完成 → 回退 doing', d['code'] == 200
          and node_of(d['data']['tasks'], T1).get('status') == 'doing')
    dt = detail(learn_plan['id'], active['id'])
    check('完成依据打卡的关联同步改回 doing',
          (dt['slots'][1]['record'] or {}).get('task_links')
          == [{'task_id': T1, 'state': 'doing'}])
    check('取消后进度 33%（300/900）', dt['task_progress']['pct'] == 33)
    r = post('/plan/api/task-cancel-done', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'task_id': T1})
    check('重复取消（未完成）→ 400', r.get_json()['code'] == 400)
    r = post('/plan/api/update-slot', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': 1,
        'record': {'content': '建站工具对比', 'duration_minutes': 60,
                   'task_links': [{'task_id': T1, 'state': 'done'}]}})
    check('重新打卡标记完成（进度回 60%）',
          r.get_json()['data']['refresh']['card']['task_progress']['pct'] == 60)

    print('\n=== 8. 全部完成上卷 + 提前通关 + 撤销结算 ===')
    r = fill(3, '部署收尾', 60,
             [{'task_id': T31, 'state': 'done'}, {'task_id': T32, 'state': 'done'}])
    d = r.get_json()
    card_d = d['data']['refresh']['card']
    check('T3.1/T3.2 完成', node_of(card_d['tasks'], T31).get('status') == 'done'
          and node_of(card_d['tasks'], T32).get('status') == 'done')
    check('子全完成 → 父 T3 自动上卷 done',
          node_of(card_d['tasks'], T3).get('status') == 'done')
    check('任务树全部完成（tasks_all_done）',
          card_d['tasks_all_done'] is True and card_d['task_progress']['pct'] == 100)

    r = post('/plan/api/settle-round', {
        'plan_id': learn_plan['id'], 'card_id': active['id']})
    d = r.get_json()
    check('学习卡提前通关（任务树全完成）',
          d['code'] == 200 and d['data']['status'] == 'completed'
          and d['data']['settlement']['early_bonus'] > 0, str(d))
    r = post('/plan/api/settle-round', {
        'plan_id': learn_plan['id'], 'card_id': active['id']})
    check('重复结算 → 400', r.get_json()['code'] == 400)
    r = client.get('/plan/api/list')
    l_cards = r.get_json()['data'][0]['stats']['cards']
    unlocked = next(c for c in l_cards if c['round'] == active['round'] + 1)
    check('结算后解锁下一张卡', unlocked['status'] == 'in_progress')

    print('\n=== 9. 落库持久化 + 撤销结算 + reset ===')
    with session_scope() as s:
        row = s.get(PlanCard, active['id'])
        card_json = json.loads(row.tasks or '[]')
        check('tasks 整树已落库（含建站工具任务）',
              any(t.get('title') == '寻找建站工具与开源模板' for t in card_json))
        slot_row = s.get(PlanSlot, (active['id'], 3))
        links = json.loads(slot_row.task_links or '[]')
    # slot3 一次打卡同时完成 T3.1/T3.2，落库应为两条（保序）
    check('task_links 已落库（slot3 关联 T3.1/T3.2 done）',
          links == [{'task_id': T31, 'state': 'done'},
                    {'task_id': T32, 'state': 'done'}], str(links))

    r = post('/plan/api/unfill-slot', {
        'plan_id': learn_plan['id'], 'card_id': active['id'], 'slot_index': 3})
    d = r.get_json()
    check('撤销结算成功（删除完成依据打卡）', d['code'] == 200)
    dt = detail(learn_plan['id'], active['id'])
    check('T3.1/T3.2 回退 doing，T3 上卷回退',
          node_of(dt['tasks'], T31).get('status') == 'doing'
          and node_of(dt['tasks'], T32).get('status') == 'doing'
          and node_of(dt['tasks'], T3).get('status') == 'doing')
    check('结算被撤销（settlement 清空、卡恢复进行中）',
          dt['settlement'] is None and dt['status'] == 'in_progress')
    r = post('/plan/api/settle-round', {
        'plan_id': learn_plan['id'], 'card_id': active['id']})
    check('未完成状态下再次结算 → 400', r.get_json()['code'] == 400)
    r = fill(3, '部署收尾', 60,
             [{'task_id': T31, 'state': 'done'}, {'task_id': T32, 'state': 'done'}])
    check('重新完成全部任务', r.get_json()['code'] == 200)
    r = post('/plan/api/settle-round', {
        'plan_id': learn_plan['id'], 'card_id': active['id']})
    check('重新结算成功', r.get_json()['code'] == 200)

    r = post('/plan/api/reset-card', {
        'plan_id': learn_plan['id'], 'card_id': active['id']})
    check('重置卡片成功', r.get_json()['code'] == 200)
    dt = detail(learn_plan['id'], active['id'])
    check('重置清空任务树与打卡',
          dt['tasks'] == [] and not any(s['filled'] for s in dt['slots']))
    dt2 = detail(learn_plan['id'], active['id'])
    check('重置后不触发重复迁移（[] 与 NULL 语义区分）', dt2['tasks'] == [])

    print('\n=== 10. 交易卡任务树 ===')
    r = add_task(trade_plan['id'], trade_active['id'], '量化研究：策略回测', 120)
    d = r.get_json()
    check('交易卡可建任务树', d['code'] == 200 and len(d['data']['tasks']) == 1)
    tid = d['data']['tasks'][0]['id']
    r = post('/plan/api/task-delete', {
        'plan_id': trade_plan['id'], 'card_id': trade_active['id'], 'task_id': tid})
    check('交易卡可删除任务', r.get_json()['code'] == 200)

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
print('ALL SMOKE TESTS PASSED (task tree v2 on MySQL)')
