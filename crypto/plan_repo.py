#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务计划模块数据访问层（MySQL 版）
====================================
替代原 plan_routes.py 中的 _load_plans/_save_plans（JSON 整文件读写）。

对外语义与原 JSON 版完全一致：
- load_plans_data(session)  → {'initialized': bool, 'plans': [...]}（整树）
- save_plans_data(session, data) ← 整树写回

内部实现为差量同步（非全表重写）：
- plans/cards 按 id upsert，消失的删除（外键级联清理子表）
- slots 按 (card_id, slot_index) 逐格比对，仅写入发生变化的格子
  （单次打卡只 UPDATE 1 行，避免整卡 100 格重写）

存储约定：
- sort_order 保持 plans/cards 原数组顺序
- daily_rule/round_config/milestones/todos/notes/settlement 为 JSON 文本列
- slot.record 拆列存储，has_record 区分 None 与 {}
- initialized 标记存 kv_store（key='task_plans_initialized'），
  保证"删光全部计划"后不会像空表那样重建默认计划
"""

import json
import logging

from sqlalchemy import select, update

from .models import PlanPlan, PlanCard, PlanSlot, KVStore

logger = logging.getLogger(__name__)

_INITIALIZED_KEY = 'task_plans_initialized'

# session.info 键：整树读取时登记下来的行对象，供同 session 的整树写入复用
_ROWS_CACHE_KEY = 'plan_repo_whole_tree_rows'


# =============================================================================
# 并发控制：写入口的整树读改写必须先取计划行锁
# =============================================================================

def lock_plan_rows(session, plan_ids=None):
    """同计划写操作串行化：先锁 plan_plans 行，再读整树做读改写。

    为什么必须加锁：任务计划的写入口都是"读整树 → 内存改 → 写整树"。两个
    并发请求（例如同一计划下两张卡各打一次卡）会各自读到同一份旧树，后提交
    的那笔把前一笔的格子原样覆盖回去——打卡记录直接丢失。加行锁后同计划排队
    执行，不同计划仍可并发（锁按计划 ID 分开取）。

    只在支持行锁的引擎（MySQL/MariaDB）上下发 FOR UPDATE；SQLite 等离线测试
    引擎直接跳过（单连接本就串行，且不支持该语法）。跨多计划的批量操作按 ID
    排序取锁，避免两笔事务以不同顺序加锁互相等待成死锁。

    plan_ids 为空表示"整树结构性改写"（新建/删除计划会改全部行的
    sort_order），此时锁定 plan_plans 全表。
    """
    ids = sorted({str(p) for p in (plan_ids or []) if p})
    if session.get_bind().dialect.name not in ('mysql', 'mariadb'):
        return []
    stmt = select(PlanPlan.id).with_for_update()
    if ids:
        stmt = stmt.where(PlanPlan.id.in_(ids)).order_by(PlanPlan.id)
    return list(session.execute(stmt).scalars().all())


def _row_registry(session, require_full: bool = True):
    """取回本 session 内 load_plans_data 登记的行对象；没有则返回 None。

    复用条件（require_full=True）：只在"同一 session 的读→改→写"里生效。
    写入前的重复全表读本来也只是把同一批行对象再查一遍（SQLAlchemy 身份映射
    保证是同一批 Python 对象），删掉这条 SQL 不改变差量比对的任何输入。
    """
    registry = session.info.get(_ROWS_CACHE_KEY) if session is not None else None
    if not registry:
        return None
    if require_full and set(registry) != {'plans', 'cards', 'slots'}:
        return None
    return registry


def _forget_row_registry(session):
    """整树写入后行集合已变（新增/删除），下一轮必须重新读"""
    session.info.pop(_ROWS_CACHE_KEY, None)


_SLOT_RECORD_COLUMNS = (
    'filled', 'filled_at', 'has_record', 'content', 'duration_minutes',
    'prediction', 'actual', 'hit', 'market_analysis', 'action_advice',
    'account_balance', 'analysis_ids', 'analysis_hour', 'bypass_analysis',
    'task_links'
)


def _json_loads(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# =============================================================================
# 读取：DB → 整树 dict
# =============================================================================

def _record_from_row(row, card_type):
    """slot 行 → record dict（None 表示空格子；按卡类型还原字段集）

    task_links（任务管理 v2）为学习/交易通用字段：[] 表示待关联。
    """
    if not row.has_record:
        return None
    links = _json_loads(row.task_links, [])
    if not isinstance(links, list):
        links = []
    if card_type == 'trade':
        record = {
            'prediction': row.prediction or '',
            'duration_minutes': row.duration_minutes or 0,
            'actual': row.actual or '',
            'market_analysis': row.market_analysis or '',
            'action_advice': row.action_advice or '',
            'account_balance': row.account_balance or '',
            # 分析纪律（批次11）：本次打卡依据的分析记录（库内逗号串 ↔ 对外 id 数组）
            'analysis_ids': _parse_analysis_ids(row.analysis_ids),
            'analysis_hour': row.analysis_hour or '',
            'bypass_analysis': bool(row.bypass_analysis),
            'task_links': links,
        }
        if row.hit is not None:
            record['hit'] = bool(row.hit)
        return record
    return {
        'content': row.content or '',
        'duration_minutes': row.duration_minutes or 0,
        'task_links': links,
    }


def _card_to_dict(row, slots_by_index):
    card = {
        'id': row.id,
        'type': row.type,
        'round': row.round,
        'title': row.title,
        'goal': row.goal,
        'reward': row.reward,
        'start_time': row.start_time,
        'end_time': row.end_time,
        'status': row.status,
        'milestones': _json_loads(row.milestones, []),
        'slots': [
            {
                'slot_index': idx,
                'filled': bool(s.filled),
                'filled_at': s.filled_at or '',
                'record': _record_from_row(s, row.type)
            }
            for idx, s in sorted(slots_by_index.items())
        ],
        'notes': _json_loads(row.notes, []),
        'review': row.review or '',
        'settlement': _json_loads(row.settlement, None),
        'created_at': row.created_at or '',
        'updated_at': row.updated_at or ''
    }
    if row.base_reward is not None:
        card['base_reward'] = row.base_reward
    if row.hourly_rate is not None:
        card['hourly_rate'] = row.hourly_rate
    # 任务树（任务管理 v2）：None 表示旧数据未迁移（由 routes 层 _migrate_tasks 处理）
    card['tasks'] = _json_loads(row.tasks, None)
    todos = _json_loads(row.todos, [])
    if todos:
        card['todos'] = todos
    return card


def _plan_to_dict(row, cards):
    plan = {
        'id': row.id,
        'type': row.type,
        'name': row.name,
        'grand_goal': row.grand_goal or '',
        'total_hours': row.total_hours,
        'round_count': row.round_count,
        'per_round_hours': row.per_round_hours,
        'daily_rule': _json_loads(row.daily_rule, {}),
        'cards': cards,
        'created_at': row.created_at or ''
    }
    if row.round_config is not None:
        plan['round_config'] = _json_loads(row.round_config, None)
    return plan


def load_plans_data(session):
    """整树读取；未初始化（无标记且表为空）时返回 initialized=False

    性能约定：全树仅 3 条 SQL（plans/cards/slots 各一条全表查询）
    + 内存分组，避免逐卡查询在远程 MySQL 上的大量网络往返。

    读取过程中把行对象登记进 session.info，供同一 session 内的
    save_plans_data 复用（见 _row_registry）：写路径原本是"读整树 → 改
    内存 → 存整树"，存前又要全表读一遍，一次打卡要拉两次全量长文本。
    """
    kv = session.get(KVStore, _INITIALIZED_KEY)
    plan_rows = session.execute(
        select(PlanPlan).order_by(PlanPlan.sort_order)).scalars().all()
    if kv is None and not plan_rows:
        # 未初始化：调用方接下来会走"预置默认计划"的写入路径，本函数没读到
        # 任何行，登记表必须为空，不能让上一次读的结果残留。
        session.info[_ROWS_CACHE_KEY] = {'plans': {}, 'cards': {}, 'slots': {}}
        return {'initialized': False, 'plans': []}

    registry = {'plans': {p.id: p for p in plan_rows}, 'cards': {}, 'slots': {}}
    cards_by_plan = {}
    # 一次性拉取全部 cards/slots，内存按外键分组
    for c in session.execute(
            select(PlanCard).order_by(PlanCard.sort_order)).scalars().all():
        registry['cards'][c.id] = c
        cards_by_plan.setdefault(c.plan_id, []).append(c)
    slots_by_card = {}
    for s in session.execute(
            select(PlanSlot).order_by(PlanSlot.slot_index)).scalars().all():
        registry['slots'][(s.card_id, s.slot_index)] = s
        slots_by_card.setdefault(s.card_id, {})[s.slot_index] = s
    session.info[_ROWS_CACHE_KEY] = registry

    plans = []
    for p in plan_rows:
        cards = []
        for c in cards_by_plan.get(p.id, []):
            cards.append(_card_to_dict(c, slots_by_card.get(c.id, {})))
        plans.append(_plan_to_dict(p, cards))
    return {'initialized': True, 'plans': plans}


# 同计划兄弟卡在单卡视图中只需要这几列（串行锁定判定 _calc_locked_ids 的输入）
_CARD_SUMMARY_COLUMNS = (PlanCard.id, PlanCard.type, PlanCard.round, PlanCard.status)


def load_card_view(session, plan_id: str, card_id: str):
    """单卡局部视图：目标计划 + 目标卡及其格子 + 同计划卡片摘要。

    与 load_plans_data 的整树读法相比，这里不读取其他卡的 100 格明细与
    长文本（market_analysis / content / notes 等），只取锁定判定必需的
    id/type/round/status 四列，因此返回的 plan['cards'] 中除目标卡外
    都是摘要 dict —— 调用方只能把它喂给仅依赖这几字段的构造器。

    返回：
        None                          计划不存在
        {'plan': {...}, 'card': None} 卡不存在或不属于该计划
        {'plan': {...}, 'card': {...}} 正常（card 同时出现在 plan['cards'] 原位置）
    """
    plan_row = session.get(PlanPlan, plan_id)
    if plan_row is None:
        return None
    card_row = session.execute(
        select(PlanCard).where(PlanCard.id == card_id,
                              PlanCard.plan_id == plan_id)).scalar_one_or_none()
    summaries = session.execute(
        select(*_CARD_SUMMARY_COLUMNS).where(PlanCard.plan_id == plan_id)
        .order_by(PlanCard.sort_order)).mappings().all()

    def _full_card():
        slot_rows = session.execute(
            select(PlanSlot).where(PlanSlot.card_id == card_id)
            .order_by(PlanSlot.slot_index)).scalars().all()
        return _card_to_dict(card_row, {s.slot_index: s for s in slot_rows})

    def _summary(row):
        return {'id': row['id'], 'type': row['type'],
                'round': row['round'], 'status': row['status']}

    target = None
    cards = []
    for row in summaries:
        if row['id'] != card_id:
            cards.append(_summary(row))
            continue
        # 卡片行读不到时退化为摘要：理论上不可达（查询已限定同计划），
        # 但保留占位可让 cards 顺序与整树读法完全一致
        target = _full_card() if card_row is not None else _summary(row)
        cards.append(target)

    if target is None and card_row is not None:
        target = _full_card()
        cards.append(target)
    return {'plan': _plan_to_dict(plan_row, cards), 'card': target}


# 列表页 / 今日统计所需的卡片列（其余列 goal/notes/review/milestones/todos
# 都是 Text，单卡可上百 KB，聚合统计一个都不读）
_CARD_LIST_COLUMNS = (
    PlanCard.id, PlanCard.plan_id, PlanCard.type, PlanCard.round, PlanCard.title,
    PlanCard.status, PlanCard.reward, PlanCard.base_reward, PlanCard.hourly_rate,
    PlanCard.start_time, PlanCard.end_time, PlanCard.created_at, PlanCard.updated_at,
    PlanCard.settlement, PlanCard.tasks,
)

# 统计用格子投影：在场日历/连续在场/打卡次数只看 filled + filled_at +
# duration_minutes；预测命中率只看 actual（String(8)，不是长文本）与 hit。
# content / market_analysis / action_advice / task_links 这几个真正的
# 大字段一律不读 —— 它们是列表接口 90% 以上的传输量。
_SLOT_SUMMARY_COLUMNS = (
    PlanSlot.card_id, PlanSlot.slot_index, PlanSlot.filled, PlanSlot.filled_at,
    PlanSlot.has_record, PlanSlot.duration_minutes, PlanSlot.actual, PlanSlot.hit,
)


def _summary_record_from_row(row, card_type):
    """统计用 record 投影：字段集按卡类型收敛，与 _record_from_row 的口径一致

    学习卡不带 actual 键（与整树读法相同），因此命中率统计天然为 0；
    交易卡只带 actual/hit/duration_minutes，长文本字段一律缺席。
    """
    if not row['has_record']:
        return None
    if card_type == 'trade':
        record = {
            'duration_minutes': row['duration_minutes'] or 0,
            'actual': row['actual'] or '',
        }
        if row['hit'] is not None:
            record['hit'] = bool(row['hit'])
        return record
    return {'duration_minutes': row['duration_minutes'] or 0}


def _summary_card_to_dict(row, slots_by_index, legacy_row=None):
    """统计用的卡片投影（字段集与 _card_to_dict 不同，只覆盖聚合入口）"""
    card = {
        'id': row['id'],
        'type': row['type'],
        'round': row['round'],
        'title': row['title'],
        'reward': row['reward'],
        'start_time': row['start_time'],
        'end_time': row['end_time'],
        'status': row['status'],
        'slots': [
            {
                'slot_index': idx,
                'filled': bool(s['filled']),
                'filled_at': s['filled_at'] or '',
                'record': _summary_record_from_row(s, row['type']),
            }
            for idx, s in sorted(slots_by_index.items())
        ],
        'settlement': _json_loads(row['settlement'], None),
        'created_at': row['created_at'] or '',
        'updated_at': row['updated_at'] or '',
        'tasks': _json_loads(row['tasks'], None),
    }
    if row['base_reward'] is not None:
        card['base_reward'] = row['base_reward']
    if row['hourly_rate'] is not None:
        card['hourly_rate'] = row['hourly_rate']
    if legacy_row is not None:
        # 仅未迁移卡补读 milestones/todos（_migrate_tasks 的唯一输入）
        card['milestones'] = _json_loads(legacy_row['milestones'], [])
        todos = _json_loads(legacy_row['todos'], [])
        if todos:
            card['todos'] = todos
    return card


def load_summary_tree(session):
    """列表 / 今日统计专用窄投影整树（结构与 load_plans_data 一致）。

    与整树读法的差别只在"读了哪些列"：plans 全列（都是短列）、cards/slots
    各一条投影查询，未迁移卡再补一条 milestones/todos 批量查询。
    200 卡 × 100 格时，整树读法要把每格的打卡正文、行情分析、操作建议、
    任务关联全部拉回内存和 Python 对象，而列表页一个都不显示。

    ⚠️ 返回的卡 dict 只包含聚合函数（_build_plan_stats / _build_today_entry /
    _build_plan_info 及其调用链）实际读取的字段，缺 goal/notes/review，
    milestones/todos 也仅对未迁移卡存在。除这两个入口外不要拿它当整树用，
    否则会得到"字段存在但为空"的假数据；需要完整卡数据请用 load_card_view。
    """
    kv = session.get(KVStore, _INITIALIZED_KEY)
    plan_rows = session.execute(
        select(PlanPlan).order_by(PlanPlan.sort_order)).scalars().all()
    if kv is None and not plan_rows:
        return {'initialized': False, 'plans': []}

    card_rows = session.execute(
        select(*_CARD_LIST_COLUMNS).order_by(PlanCard.sort_order)).mappings().all()
    slots_by_card = {}
    for s in session.execute(
            select(*_SLOT_SUMMARY_COLUMNS).order_by(PlanSlot.slot_index)
    ).mappings().all():
        slots_by_card.setdefault(s['card_id'], {})[s['slot_index']] = s

    # 未迁移卡（tasks 为 NULL/''/'null'）才需要 milestones+todos 推导任务树；
    # 已迁移的存量卡占绝大多数，通常这条查询根本不会发出。
    legacy_ids = [r['id'] for r in card_rows
                  if _json_loads(r['tasks'], None) is None]
    legacy_by_id = {}
    if legacy_ids:
        for row in session.execute(
                select(PlanCard.id, PlanCard.milestones, PlanCard.todos)
                .where(PlanCard.id.in_(legacy_ids))).mappings().all():
            legacy_by_id[row['id']] = row

    cards_by_plan = {}
    for row in card_rows:
        cards_by_plan.setdefault(row['plan_id'], []).append(
            _summary_card_to_dict(row, slots_by_card.get(row['id'], {}),
                                  legacy_by_id.get(row['id'])))

    plans = [_plan_to_dict(p, cards_by_plan.get(p.id, [])) for p in plan_rows]
    return {'initialized': True, 'plans': plans}


# 局部视图里兄弟卡可能被业务逻辑改写的列：只有「结算解锁下一张卡」和
# 「重算串行链」两处，动的都只有这几列标量。长文本列（goal/notes/review/
# milestones…）压根没读进来，绝不放进可写集合 —— 否则差量保存会把它当空值写回。
_SIBLING_WRITABLE_COLUMNS = ('status', 'start_time', 'updated_at')


def _sibling_field(card, col):
    """兄弟卡可写列取值口径（与 _apply_card_fields 对同名列的处理一致）"""
    if col == 'status':
        return card.get('status', 'pending')
    return card.get(col, '') or ''


def load_plan_write_view(session, plan_id: str, card_id: str):
    """单卡写入口的局部读：本计划窄投影 + 目标卡完整数据。

    打卡/改格子/撤销/补录这类操作原先都读整树：整库每张卡的 100 格正文、
    行情分析、操作建议全部拉进内存，只为改一行格子、再为聚合统计回一份局部
    刷新载荷。本视图按写入口实际需要的最小面读取：

    - 目标卡：完整行 + 全部格子（奖励计算、任务树联动、结算都要看正文与格数）
    - 同计划其他卡：列表统计用的窄投影（refresh 载荷要算全计划聚合数）
    - 其他计划：一行都不读

    ⚠️ 兄弟卡是投影数据，缺 goal/notes/review 等长文本列：只能交给
    save_card_slots / save_card / save_sibling_changes 这组局部写入口，
    绝不喂给 save_plans_data（整树差量会把"没读到的字段"当成用户清空了）。

    返回：
        None                                            计划行不存在
        {'plan', 'card', 'rows', 'plan_id', 'card_id'}   正常
      plan['cards'] 按 sort_order 排列，目标卡位置上是完整 dict（与
      card 同一对象），其余为窄投影 dict；rows 给出可写的 ORM 行与
      兄弟卡的列值快照（供 save_sibling_changes 比对）。
    """
    plan_row = session.get(PlanPlan, plan_id)
    if plan_row is None:
        return None
    card_rows = session.execute(
        select(*_CARD_LIST_COLUMNS).where(PlanCard.plan_id == plan_id)
        .order_by(PlanCard.sort_order)).mappings().all()

    target_row = session.execute(
        select(PlanCard).where(PlanCard.id == card_id,
                              PlanCard.plan_id == plan_id)
    ).scalar_one_or_none()
    other_ids = [r['id'] for r in card_rows if r['id'] != card_id]

    # 目标卡的格子按完整列读（record 正文要用），其余卡只读统计投影
    slots_by_card = {}
    if other_ids:
        for s in session.execute(
                select(*_SLOT_SUMMARY_COLUMNS).where(
                    PlanSlot.card_id.in_(other_ids)).order_by(PlanSlot.slot_index)
        ).mappings().all():
            slots_by_card.setdefault(s['card_id'], {})[s['slot_index']] = s
    slot_rows = {}
    if target_row is not None:
        slot_rows = {s.slot_index: s for s in session.execute(
            select(PlanSlot).where(PlanSlot.card_id == card_id)
            .order_by(PlanSlot.slot_index)).scalars().all()}

    # 未迁移旧卡要补读 milestones/todos（_migrate_tasks 的唯一输入）；
    # 目标卡读的是完整行，旧列本来就在
    legacy_ids = [r['id'] for r in card_rows
                  if r['id'] != card_id and _json_loads(r['tasks'], None) is None]
    legacy_by_id = {}
    if legacy_ids:
        for row in session.execute(
                select(PlanCard.id, PlanCard.milestones, PlanCard.todos)
                .where(PlanCard.id.in_(legacy_ids))).mappings().all():
            legacy_by_id[row['id']] = row

    target = None
    cards = []
    snapshots = {}
    for row in card_rows:
        if row['id'] == card_id:
            target = _card_to_dict(target_row, slot_rows) if target_row else \
                {'id': row['id'], 'type': row['type'], 'round': row['round'],
                 'title': row['title'], 'status': row['status']}
            cards.append(target)
            continue
        summary = _summary_card_to_dict(row, slots_by_card.get(row['id'], {}),
                                       legacy_by_id.get(row['id']))
        cards.append(summary)
        snapshots[row['id']] = {col: _sibling_field(summary, col)
                                for col in _SIBLING_WRITABLE_COLUMNS}

    return {
        'plan': _plan_to_dict(plan_row, cards),
        'card': target,
        'plan_id': plan_id,
        'card_id': card_id,
        'rows': {'plan': plan_row, 'card': target_row, 'slots': slot_rows,
                 'siblings': snapshots},
    }


# =============================================================================
# 写入：整树 dict → DB（差量同步）
# =============================================================================

def _parse_analysis_ids(text):
    """逗号串 → id 整数数组（非法段静默丢弃）"""
    return [int(x) for x in str(text or '').split(',') if x.strip().isdigit()]


def _format_analysis_ids(value) -> str:
    """id 数组/逗号串 → 库内逗号串（上限 255 字符，超出部分丢弃）"""
    if isinstance(value, str):
        parts = [x.strip() for x in value.split(',')]
    elif isinstance(value, (list, tuple)):
        parts = [str(x).strip() for x in value]
    else:
        parts = []
    ids = [p for p in parts if p.isdigit()]
    return ','.join(ids)[:255]


def _format_task_links(value) -> str:
    """task_links 数组 → 库内 JSON 文本（只保留规范字段，去重保序，限 50 条）"""
    if not isinstance(value, list):
        return '[]'
    links = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        tid = str(item.get('task_id') or '').strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        links.append({
            'task_id': tid,
            'state': 'done' if item.get('state') == 'done' else 'doing',
        })
        if len(links) >= 50:
            break
    return json.dumps(links, ensure_ascii=False)


def _slot_values(slot):
    """slot dict → plan_slots 列值 dict（record 拆平）"""
    record = slot.get('record')
    vals = {
        'filled': bool(slot.get('filled')),
        'filled_at': str(slot.get('filled_at') or ''),
        'has_record': record is not None,
        'content': '',
        'duration_minutes': 0,
        'prediction': '',
        'actual': '',
        'hit': None,
        'market_analysis': '',
        'action_advice': '',
        'account_balance': '',
        'analysis_ids': '',
        'analysis_hour': '',
        'bypass_analysis': False,
        'task_links': '[]'
    }
    if isinstance(record, dict):
        vals['content'] = str(record.get('content', '') or '')
        vals['duration_minutes'] = int(record.get('duration_minutes', 0) or 0)
        vals['prediction'] = str(record.get('prediction', '') or '')[:8]
        vals['actual'] = str(record.get('actual', '') or '')[:8]
        if 'hit' in record:
            vals['hit'] = bool(record['hit'])
        vals['market_analysis'] = str(record.get('market_analysis', '') or '')
        vals['action_advice'] = str(record.get('action_advice', '') or '')
        vals['account_balance'] = str(record.get('account_balance', '') or '')[:32]
        vals['analysis_ids'] = _format_analysis_ids(record.get('analysis_ids'))
        vals['analysis_hour'] = str(record.get('analysis_hour', '') or '')[:13]
        vals['bypass_analysis'] = bool(record.get('bypass_analysis'))
        vals['task_links'] = _format_task_links(record.get('task_links'))
    return vals


def _sync_slots(session, card_id, slots, existing_slots=None):
    """同步单张卡的格子（差量：新增/修改/删除）。

    existing_slots（{slot_index: row}）由调用方预取传入时不再查库；
    未传入时回退为按 card_id 查询（兼容单独调用）。
    """
    if existing_slots is None:
        existing_slots = {s.slot_index: s for s in session.execute(
            select(PlanSlot).where(PlanSlot.card_id == card_id)).scalars().all()}
    existing = existing_slots
    keep = set()
    for slot in slots or []:
        idx = slot.get('slot_index')
        if idx is None:
            continue
        keep.add(idx)
        vals = _slot_values(slot)
        row = existing.get(idx)
        if row is None:
            session.add(PlanSlot(card_id=card_id, slot_index=idx, **vals))
        else:
            for col, val in vals.items():
                if getattr(row, col) != val:
                    setattr(row, col, val)
    for idx, row in existing.items():
        if idx not in keep:
            session.delete(row)
    session.flush()


def _apply_card_fields(row, c):
    """把卡 dict 的非结构字段写回卡行（调用方负责 sort_order）

    整树差量同步与单卡局部写共用这一份字段口径：两条路径对同一张卡必须
    逐列等价，否则局部写会静默改字段语义（比如 tasks 键缺失时要保留库内值，
    少一句判断就会把"未迁移"旧卡写成已迁移空树）。
    """
    row.type = c.get('type', 'learn')
    row.round = int(c.get('round', 0) or 0)
    row.title = c.get('title', '') or ''
    row.goal = c.get('goal', '') or ''
    row.reward = int(c.get('reward', 0) or 0)
    row.base_reward = int(c['base_reward']) if c.get('base_reward') is not None else None
    row.hourly_rate = int(c['hourly_rate']) if c.get('hourly_rate') is not None else None
    row.status = c.get('status', 'pending')
    row.start_time = c.get('start_time', '') or ''
    row.end_time = c.get('end_time', '') or ''
    row.milestones = json.dumps(c.get('milestones') or [], ensure_ascii=False)
    row.todos = json.dumps(c.get('todos') or [], ensure_ascii=False)
    # 任务树：键缺失（None）时保留库内原值，避免把"未迁移"旧卡误写成已迁移空树
    tasks_val = c.get('tasks')
    if tasks_val is not None:
        row.tasks = json.dumps(tasks_val, ensure_ascii=False)
    row.notes = json.dumps(c.get('notes') or [], ensure_ascii=False)
    row.review = c.get('review', '') or ''
    settlement = c.get('settlement')
    row.settlement = json.dumps(settlement, ensure_ascii=False) if settlement is not None else None
    row.created_at = c.get('created_at', '') or ''
    row.updated_at = c.get('updated_at', '') or ''


def _sync_cards(session, plan_id, cards, existing_cards=None, slots_by_card=None):
    """同步单个计划下的卡片列表（差量：新增/修改/删除），并递归同步格子。

    existing_cards（{card_id: row}）/ slots_by_card（{card_id: {slot_index: row}}）
    由调用方预取传入时直接使用，消除逐计划/逐卡查询；未传入时回退为
    按 plan_id/card_id 查询。卡片/格子均为客户端生成的字符串 ID，
    无需逐卡 flush 获取自增主键，统一由末尾 flush 按外键顺序批量提交。
    """
    if existing_cards is None:
        existing_cards = {c.id: c for c in session.execute(
            select(PlanCard).where(PlanCard.plan_id == plan_id)).scalars().all()}
    existing = existing_cards
    keep = set()
    for idx, c in enumerate(cards or []):
        cid = c.get('id', '')
        if not cid:
            continue
        keep.add(cid)
        row = existing.get(cid)
        if row is None:
            row = PlanCard(id=cid, plan_id=plan_id)
            session.add(row)
        row.sort_order = idx
        _apply_card_fields(row, c)
        _sync_slots(session, cid, c.get('slots'),
                    existing_slots=(slots_by_card or {}).get(cid)
                    if slots_by_card is not None else None)
    for cid, row in existing.items():
        if cid not in keep:
            session.delete(row)  # 级联删除 slots
    session.flush()


def save_plans_data(session, data):
    """整树写回（差量同步）；同时维护 initialized 标记。

    性能约定：写阶段无论计划/卡片数量多少，最多 3 条 SELECT（plans/cards/slots
    各一次全表预取 + 内存分组），其余为差量 INSERT/UPDATE/DELETE，避免逐计划/
    逐卡查询在远端 MySQL 上产生大量串行网络往返（打卡慢的主因）。

    同一 session 内先调过 load_plans_data 时，这 3 条 SELECT 也省掉：直接复用
    读阶段登记的行对象。原先一次打卡要读两遍全树（读 → 改 → 存前再读），
    第二遍在 REPEATABLE READ 下看到的仍是同一批行，纯属重复传输。
    """
    kv = session.get(KVStore, _INITIALIZED_KEY)
    if kv is None:
        session.add(KVStore(key=_INITIALIZED_KEY,
                            value='true' if data.get('initialized', True) else 'false'))
    else:
        kv.value = 'true' if data.get('initialized', True) else 'false'
    session.flush()

    plans = data.get('plans', [])
    registry = _row_registry(session)
    if registry is not None:
        existing = dict(registry['plans'])
        cards_by_plan = {}
        for cid, card_row in registry['cards'].items():
            cards_by_plan.setdefault(card_row.plan_id, {})[cid] = card_row
        slots_by_card = {}
        for (card_id, slot_index), slot_row in registry['slots'].items():
            slots_by_card.setdefault(card_id, {})[slot_index] = slot_row
    else:
        existing = {p.id: p for p in session.execute(select(PlanPlan)).scalars().all()}
        # 一次性预取全部 cards/slots，内存按计划/卡片分组，供差量同步直接使用
        cards_by_plan = {}
        for c in session.execute(select(PlanCard)).scalars().all():
            cards_by_plan.setdefault(c.plan_id, {})[c.id] = c
        slots_by_card = {}
        for s in session.execute(select(PlanSlot)).scalars().all():
            slots_by_card.setdefault(s.card_id, {})[s.slot_index] = s
    keep = set()
    for idx, p in enumerate(plans):
        pid = p.get('id', '')
        if not pid:
            continue
        keep.add(pid)
        row = existing.get(pid)
        is_new = row is None
        if is_new:
            row = PlanPlan(id=pid)
            session.add(row)
        row.sort_order = idx
        row.type = p.get('type', 'custom')
        row.name = p.get('name', '') or ''
        row.grand_goal = p.get('grand_goal', '') or ''
        row.total_hours = int(p.get('total_hours', 100) or 0)
        row.round_count = int(p.get('round_count', 1) or 0)
        row.per_round_hours = int(p.get('per_round_hours', 100) or 0)
        row.daily_rule = json.dumps(p.get('daily_rule') or {}, ensure_ascii=False)
        round_config = p.get('round_config')
        row.round_config = json.dumps(round_config, ensure_ascii=False) if round_config is not None else None
        row.created_at = p.get('created_at', '') or ''
        if is_new:
            # 新建计划先落库再同步子表：批量 flush 只按 mapper 类名排序
            # （PlanCard 排在 PlanPlan 之前），空库预置/新建计划时会因子表
            # INSERT 先于父表而触发外键校验失败
            session.flush()
        _sync_cards(session, pid, p.get('cards'),
                    existing_cards=cards_by_plan.get(pid, {}),
                    slots_by_card=slots_by_card)
    for pid, row in existing.items():
        if pid not in keep:
            session.delete(row)  # 级联删除 cards/slots
    session.flush()
    # 行集合已被本次写入改变（新增/删除/级联清理），登记表就此作废：
    # 同一 session 里若还要再存一次，必须由 load_plans_data 重新读全树。
    _forget_row_registry(session)


# =============================================================================
# 局部写：单卡写入口（打卡 / 改格子 / 撤销 / 补录）
# =============================================================================
# 与 save_plans_data 的分工：整树入口负责"结构"（增删计划/卡/格子、重排
# sort_order），局部入口只负责"值"。打卡族不改变任何行的存在性与顺序，因此
# 走这里；数据面收敛到被改的那几行，长文本才不会被无谓读写。

def save_card(session, row, card):
    """单卡局部写：按完整卡 dict 差量更新该卡自己的列。

    调用方必须传入完整卡（load_plan_write_view 的 target），字段口径与整树
    差量同步完全一致（共用 _apply_card_fields）。不碰格子、不碰别的卡、
    不碰 sort_order —— 局部视图里的顺序就是库内顺序，重写一遍毫无意义。
    """
    _apply_card_fields(row, card)
    session.flush()


def save_card_slots(session, card_id, slots, rows_by_index=None):
    """局部写格子：只把值发生变化的行写回，不增删行结构。

    rows_by_index（{slot_index: PlanSlot}）由 load_plan_write_view 预取；
    没命中时按主键补查一次，补查也没有才新增（历史数据缺格时兜底，与
    _sync_slots 行为一致）。传进来的可以是整张卡的 100 格：未变化的格子
    逐列比对后不会产生任何 SQL，实际写量仍等于真正被改的行数。
    """
    rows_by_index = rows_by_index or {}
    for slot in slots or []:
        idx = slot.get('slot_index')
        if idx is None:
            continue
        vals = _slot_values(slot)
        row = rows_by_index.get(idx)
        if row is None:
            row = session.get(PlanSlot, (card_id, idx))
        if row is None:
            session.add(PlanSlot(card_id=card_id, slot_index=idx, **vals))
            continue
        for col, val in vals.items():
            if getattr(row, col) != val:
                setattr(row, col, val)
    session.flush()


def save_sibling_changes(session, plan_id, cards, snapshots):
    """兄弟卡局部写：只写被"解锁下一张卡 / 重算串行链"改动的标量列。

    结算一张卡会顺带改写同计划的下一张卡（pending → in_progress + 起始时间），
    撤销结算则可能重排整条串行链。这些改动全落在 _SIBLING_WRITABLE_COLUMNS
    里，其余列在局部视图里根本没读进来，写它们等于用空值覆盖真实数据。

    比对基准是读取时的列值快照：没被业务逻辑碰过的卡一条 SQL 都不发。
    兄弟卡在本 session 里没有 ORM 实体（读的是列投影），因此用 Core UPDATE
    直接下发，并关掉会话同步（无对象可同步）。
    """
    if not snapshots:
        return []
    updated = []
    for card in cards or []:
        snap = snapshots.get(card.get('id'))
        if not snap:
            continue
        dirty = {}
        for col in _SIBLING_WRITABLE_COLUMNS:
            val = _sibling_field(card, col)
            if snap.get(col) != val:
                dirty[col] = val
        if not dirty:
            continue
        session.execute(
            update(PlanCard)
            .where(PlanCard.id == card['id'], PlanCard.plan_id == plan_id)
            .values(**dirty)
            .execution_options(synchronize_session=False))
        updated.append(card['id'])
    if updated:
        session.flush()
    return updated


# =============================================================================
# 每日打卡（checkin 模块）的窄聚合读法
# -----------------------------------------------------------------------------
# 刻意不走整树读取：200 卡 × 100 格全量拉进内存只为算两个日聚合数字，
# 是任务计划模块已知的性能坑。这里只 SELECT 目标日期已打卡格子的两列。
# =============================================================================

def load_daily_learn_stats(session, date_str, include_trade=False):
    """按日聚合学习打卡指标（只读，供 /checkin 打卡采集）。

    - 时长：type='learn' 卡（include_trade=True 时含 trade 卡）中 filled_at
      前缀命中 date_str 的格子的 duration_minutes 求和，换算为小时；
    - 完成任务数：同日格子 record.task_links 里 state='done' 的 task_id
      去重个数（同一任务被多条打卡关联只算 1 个）。

    返回 {'hours': float, 'task_done_count': int}。
    """
    card_types = ('learn', 'trade') if include_trade else ('learn',)
    rows = session.execute(
        select(PlanSlot.duration_minutes, PlanSlot.task_links)
        .join(PlanCard, PlanSlot.card_id == PlanCard.id)
        .where(PlanCard.type.in_(card_types),
               PlanSlot.filled.is_(True),
               PlanSlot.filled_at.like(f'{date_str}%'))).all()
    total_minutes = 0
    done_tasks = set()
    for minutes, links_text in rows:
        try:
            total_minutes += int(minutes or 0)
        except (TypeError, ValueError):
            pass
        links = _json_loads(links_text, [])
        if not isinstance(links, list):
            continue
        for link in links:
            if isinstance(link, dict) and link.get('state') == 'done' and link.get('task_id'):
                done_tasks.add(str(link['task_id']))
    return {
        'hours': round(total_minutes / 60.0, 2),
        'task_done_count': len(done_tasks),
    }
