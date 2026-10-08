#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务计划系统 - Flask 蓝图（长期主义版本 Longtermism v1.1）
============================================================
所有 /plan 和 /plan/api/* 路由
包括：计划管理、任务卡管理、100格打卡、核算引擎

【长期主义设计理念】
- 低门槛启动：随时想开始就开始，几分钟也算在场，没有任何心理负担
- 坚持比强度更重要：核心指标是"在场天数"，而非"每日时长"
- 不惧中断：允许任意中断与回归，回来永远被欢迎，绝不罚款
- 今天在场，比今天做多少更重要
- 身份认同：你是一个长期主义者——行为由身份驱动，而非压力驱动
- 数据诚实、情绪友好：断档如实呈现在场日历，大字只展示历史最佳连续

【存储】MySQL（迁移批次3）：数据访问统一走 plan_repo（整树语义，
与原 JSON 版 _load_plans/_save_plans 契约一致），连接配置见 database.py。
对外 API 契约与 JSON 文件版完全一致。
"""

import datetime
import hashlib
import json
import uuid
import logging
import os
import sys
from flask import Blueprint, jsonify, request, render_template

from .database import session_scope
from . import plan_repo as repo

# =============================================================================
# 【关键】确保优先加载项目根目录的 api_config.py（与 api_routes 同款守卫）
# 策略模块会向 sys.path 头部插入含另一份 api_config.py 的子目录，
# 若不加守卫，懒加载导入时可能取到错误密钥导致 OKX 签名失败。
# =============================================================================
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT in sys.path:
    sys.path.remove(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger(__name__)

plan_bp = Blueprint('plan_bp', __name__)

# =============================================================================
# 【已全面取消罚款】任务卡不再有任何形式的经济惩罚（旧版三档罚款与长期主义扣减
# 一并移除）。每日时长仅作"在场"记录，与奖金解耦。
# 下面两个阈值用于【完成标准判定】与【达标率统计】（纯记录/展示，不扣钱）：
#   - DAILY_TARGET_HOURS：学习任务单日达标线（默认 10h，可被 daily_rule.target_hours 覆盖）
#   - TRADE_GROWTH_THRESHOLD：交易任务【单日达标线】（账户资金较初始本金增长 > 8%）。
#     仅用于达标率评级与展示，【不是】通关/结算判定标准——通关标准与学习卡
#     同口径：满 100 格或任务树全部完成（2026-09-27 纠正：旧版误把本阈值当作
#     交易卡通关线，导致刚亏 7% 就被手动结算判死）
# =============================================================================
DAILY_TARGET_HOURS = float(os.environ.get('CRYPTO_PLAN_DAILY_TARGET_HOURS', '10') or 10)
TRADE_GROWTH_THRESHOLD = float(os.environ.get('CRYPTO_PLAN_TRADE_GROWTH', '0.08') or 0.08)

# =============================================================================
# 「10 天倒计时」——任务卡的第三个结束条件（与 ①满 100 格 ②任务树全部完成 并列）：
# 每张未结算的 in_progress 卡，自 start_time 起满 N 个日历日即触发结算/终止；
# 任一条件命中即视为本轮结束。completed/已结算卡不受影响。
# 天数可被 plan.daily_rule.countdown_days 覆盖；环境变量 CRYPTO_PLAN_COUNTDOWN_DAYS
# 改默认值；结果 <=0 视为关闭倒计时。
# =============================================================================
PLAN_COUNTDOWN_DEFAULT = int(os.environ.get('CRYPTO_PLAN_COUNTDOWN_DAYS', '10') or 10)

# =============================================================================
# 数据库初始化已收敛到 database.init_db() 进程内单例，由应用启动时后台
# 预热（warmup_async）完成，蓝图导入期不再同步建表，避免阻塞启动。
# =============================================================================


# =============================================================================
# 数据操作层（整树读写语义，底层为 MySQL 差量同步，见 plan_repo）
# =============================================================================

def _load_plans(session):
    """读取整树数据；未初始化（空库）时自动预置默认计划

    【读路径不写库】除首次初始化外，本函数内的各类惰性迁移一律只在内存里
    归一化，不回写数据库。原因：/plan/api/list、card-detail、today-status 都是
    GET，前端页面加载与轮询会并发触发（Flask threaded=True）；一旦读路径带全树
    写事务，远端 MySQL 上多个大事务互相争 plan_cards/plan_slots 行锁，实测会撞
    1205 Lock wait timeout（等满 50s）并把整个任务卡页面打成 500 空白。
    迁移结果由后续任意一次真实写操作自然落库。
    """
    data = repo.load_plans_data(session)
    if not data.get('initialized', False):
        return _create_default_plans(session)
    # 数据迁移：每日达标线 12→10，严重线 8→6
    _migrate_daily_rule(data)
    # 数据迁移：交易打卡记录字段重构（operation_notes/reflection → market_analysis/action_advice）
    _migrate_record_fields(data)
    # 数据迁移：任务树（任务管理 v2）——旧卡 milestones+todos → card['tasks']
    _migrate_tasks(data)
    return data


def _load_plans_for_write(session, plan_id: str = ''):
    """写入口统一读法：先锁计划行，再读整树。

    写路径不能沿用普通读法：两个并发请求各自读同一份旧树、各自改一点再整树
    写回，后提交那笔会把前一笔的改动抹掉（打卡丢失就是这么来的）。锁住计划行
    之后同计划请求排队，等价于合法的串行执行；不同计划互不阻塞。

    传空 plan_id 表示整树结构性改写（新建/删除计划要重排所有 sort_order），
    此时锁定 plan_plans 全表。加锁只在 MySQL 上生效，离线 SQLite 回归自动跳过。
    """
    repo.lock_plan_rows(session, [plan_id] if plan_id else None)
    return _load_plans(session)


def _load_summary_plans(session):
    """/plan/api/list 与 /plan/api/today-status 的窄投影读法。

    返回结构与 _load_plans 完全一致（同样只读不写、同样做内存归一化），
    差别只在读取列：见 plan_repo.load_summary_tree。整树读法会把每个格子的
    打卡正文、行情分析、操作建议、任务关联都拉进内存，而这两个接口只输出
    聚合数字 —— 200 卡 × 100 格的库上，这部分传输与反序列化是纯浪费。

    这里刻意不跑 _migrate_record_fields：该迁移只补齐 market_analysis /
    action_advice / account_balance 三个长文本键，聚合链路一个都不读它们；
    daily_rule 与 tasks 两项迁移则确实是 stats 的输入，必须保留。
    """
    data = repo.load_summary_tree(session)
    if not data.get('initialized', False):
        return _create_default_plans(session)
    _migrate_daily_rule(data)
    _migrate_tasks(data)
    return data


def _migrate_daily_rule(data):
    """迁移为长期主义模式：在场优先，取消强制每日时长与罚款机制（仅内存归一化）"""
    for plan in data.get('plans', []):
        rule = plan.get('daily_rule', {})
        if rule.get('mode') != 'longtermism':
            # 旧字段（target_hours / severe_hours / 罚款倍数）语义废弃，仅保留作参考
            rule['mode'] = 'longtermism'
            rule.pop('window', None)
            rule['min_record_minutes'] = 1   # 最低记录 1 分钟：几分钟也算在场
        # v1.1：提前通关奖励不再翻倍（×1）——提前通关本身就是奖励（时间自由）
        if rule.get('early_bonus_multiplier', 1) != 1:
            rule['early_bonus_multiplier'] = 1


# 交易打卡记录规范字段（v2）：行情分析 / 操作建议 / 账户金额
_TRADE_RECORD_FIELDS = ('market_analysis', 'action_advice', 'account_balance')


def _migrate_record_fields(data):
    """迁移交易打卡记录字段（v2 重构）——仅内存归一化，不写库

    - 旧「深度思考/复盘」(reflection) 内容映射为「行情分析」(market_analysis)
    - 旧「具体操作内容」(operation_notes) 字段废弃移除
    - 新增「操作建议」(action_advice)，缺失时补空字符串
    - 「账户金额」(account_balance) 缺失时补空字符串
    旧数据在加载时自动完成重命名与补齐，与前端提交格式完全一致；
    归一化结果由后续任意一次真实写操作自然落库。
    """
    for plan in data.get('plans', []):
        if plan.get('type') != 'trade':
            continue
        for card in plan.get('cards', []):
            for slot in card.get('slots', []):
                if not slot.get('filled') or not slot.get('record'):
                    continue
                record = slot['record']
                # reflection → market_analysis（行情分析取代深度思考/复盘）
                if 'market_analysis' not in record:
                    record['market_analysis'] = record.get('reflection', '') or ''
                # 补齐操作建议 / 账户金额默认空值
                for field in ('action_advice', 'account_balance'):
                    if field not in record:
                        record[field] = ''
                # 移除废弃旧字段
                for legacy in ('operation_notes', 'reflection'):
                    if legacy in record:
                        record.pop(legacy)


def _migrated_task_id(card_id, kind, idx, title):
    """迁移节点的确定性 ID：同一卡片的同一条旧条目永远推导出同一 id

    读路径不回写数据库，因此每次加载都要重新推导任务树。若用随机 uuid，
    历史 task_links 里存的 id 会在下一次加载后失配（关联标签全部变"已删除任务"、
    完成状态无法汇总）。用「卡片ID + 来源类型 + 序号 + 标题」做哈希，保证幂等。
    """
    raw = f'{card_id}|{kind}|{idx}|{title}'.encode('utf-8')
    return 'task_m' + hashlib.md5(raw).hexdigest()[:10]


def _migrate_tasks(data):
    """任务树迁移（任务管理 v2）——只推导不写库，重复推导结果完全一致

    卡片无 tasks 键（None，旧数据）→ 将 milestones + todos 平铺清单转为
    任务树顶层节点（content → title；done → status；预估留空=待补，
    不计入进度、不可被打卡关联，补全后自动恢复）。旧列保留只读归档。
    """
    for plan in data.get('plans', []):
        for card in plan.get('cards', []):
            if card.get('tasks') is not None:
                continue
            tasks = []
            sources = (('ms', card.get('milestones') or []),
                       ('td', card.get('todos') or []))
            for kind, items in sources:
                for idx, item in enumerate(items):
                    title = str(item.get('content') or '').strip()
                    if not title:
                        continue
                    tasks.append({
                        'id': _migrated_task_id(card.get('id'), kind, idx, title),
                        'title': title,
                        'estimated_minutes': 0,
                        'status': 'done' if item.get('done') else 'todo',
                        'children': [],
                        'created_at': card.get('created_at') or '',
                    })
            card['tasks'] = tasks


def _normalize_trade_record(record, analysis=None):
    """规范化交易打卡记录，保证后端存储结构与前端提交格式完全一致

    仅保留规范字段：prediction / duration_minutes / actual /
    market_analysis / action_advice / account_balance /
    analysis_ids / analysis_hour / bypass_analysis / task_links，
    丢弃旧字段（operation_notes / reflection 等），缺失字段补空字符串。

    analysis：服务端闸门判定结果（{analysis_ids, analysis_hour, bypass_analysis}）。
    fill/backfill 路径一律由服务端显式传入覆盖，客户端无法伪造；
    为 None 时沿用 record 内已有值（update-slot 局部更新场景，避免抹除关联）。
    """
    if not isinstance(record, dict):
        record = {}
    normalized = {
        'prediction': str(record.get('prediction', '') or ''),
        'duration_minutes': record.get('duration_minutes', 0) or 0,
        'actual': str(record.get('actual', '') or ''),
    }
    for field in _TRADE_RECORD_FIELDS:
        normalized[field] = str(record.get(field, '') or '')

    src = analysis if isinstance(analysis, dict) else record
    raw_ids = src.get('analysis_ids') or []
    if isinstance(raw_ids, str):
        raw_ids = raw_ids.split(',')
    ids = [int(str(x).strip()) for x in raw_ids if str(x).strip().isdigit()]
    normalized['analysis_ids'] = ids[:40]
    normalized['analysis_hour'] = str(src.get('analysis_hour') or '')[:13]
    normalized['bypass_analysis'] = bool(src.get('bypass_analysis'))
    # 任务树关联（任务管理 v2）：表单显式携带则采用，否则空数组
    # （update-slot 的部分更新保护由调用方处理——缺键时回填库内旧值）
    normalized['task_links'] = _normalize_task_links(record.get('task_links'))
    return normalized


# =============================================================================
# 分析纪律闸门（批次11）：先分析记录，然后才允许交易打卡
# -----------------------------------------------------------------------------
# 只约束 card['type'] == 'trade'，学习卡不受影响。
# 闸门自身异常一律 fail-open：纪律功能绝不能因为自己的 bug 拦住正常打卡。
# =============================================================================

def _discipline_gate(session, filled_at_str: str = '', cfg: dict = None):
    """判定一个打卡时点的小时槽是否允许交易打卡。

    返回 (allowed, analysis, block_data)：
      allowed    是否放行
      analysis   服务端判定的关联载荷（直接交给 _normalize_trade_record）
      block_data 不放行时回给前端的结构化数据（need_analysis 等）；放行时为 None
    闸门不可用/已关闭时返回 (True, None, None)，行为与改造前完全一致。
    """
    try:
        from . import discipline_repo as disc
    except Exception as e:
        logger.warning(f'[Plan] 分析纪律模块不可用，闸门跳过: {e}')
        return True, None, None
    try:
        cfg = cfg if cfg is not None else disc.load_config()
        # 总闸（业务 kv enabled × /task 页任务开关）关闭：跳过全部分析校验
        if not disc.module_enabled(cfg):
            return True, None, None
        target = disc.parse_dt(filled_at_str) if filled_at_str else datetime.datetime.now()
        gate = disc.gate_check(session, target, cfg)
    except Exception as e:
        logger.error(f'[Plan] 分析闸门判定异常，按放行处理: {e}', exc_info=True)
        return True, None, None

    ev = gate.get('evaluation') or {}
    records = ev.get('records') or []
    analysis = {
        'analysis_ids': [r['id'] for r in records],
        'analysis_hour': gate.get('hour_slot') or '',
        # soft 模式：放行但留痕，看板单列“无分析打卡”
        'bypass_analysis': gate.get('mode') == 'soft_bypass',
    }
    if gate.get('allowed'):
        return True, analysis, None

    block_data = {
        'need_analysis': True,
        'strict_mode': cfg.get('strict_mode', 'strict'),
        'hour_slot': gate.get('hour_slot') or '',
        'required': ev.get('required', 1),
        'actual': ev.get('actual', 0),
        'missing_coins': ev.get('missing_coins') or [],
        'is_history': bool(filled_at_str),
        'records': [{
            'id': r['id'], 'ts': r['ts'], 'inst_id': r['inst_id'],
            'user_judgment': r.get('user_judgment', ''),
            'source': r.get('source', 'live'),
        } for r in records],
    }
    return False, analysis, block_data


def _gate_block_response(block_data: dict, message: str):
    """闸门拦截的统一 403 响应"""
    return jsonify({'code': 403, 'message': message, 'data': block_data})


def _gate_message(block_data: dict) -> str:
    """拦截文案：区分当小时与历史补录，直接告诉用户下一步该做什么"""
    slot = block_data.get('hour_slot') or ''
    miss = block_data.get('missing_coins') or []
    if miss:
        detail = f'未覆盖币种：{"、".join(miss)}'
    else:
        detail = f"分析记录 {block_data.get('actual', 0)} 条，要求 {block_data.get('required', 1)} 条"
    if block_data.get('is_history'):
        return (f'📝 历史槽 {slot}:00 缺分析记录（{detail}）\n'
                f'请先为这些小时补上回溯分析记录，再重新补录打卡')
    return (f'📝 先分析，再打卡\n本小时（{slot}:00）{detail}\n'
            f'弹窗内可一键批量快照并录入判断，完成后自动放行')


def _save_plans(session, data):
    """整树写回 MySQL（plan_repo 内部差量同步）"""
    repo.save_plans_data(session, data)


def _normalize_view_plan(plan, card):
    """局部视图的内存归一化：与 _load_plans 用同一批迁移函数、同一顺序。

    只把「目标计划 + 目标卡」组成一棵最小树喂给迁移函数：迁移函数就地修改
    daily_rule / slots / tasks 对象，改动自然反映到 plan 与 card 本身，
    而同计划其他卡的摘要（仅供 _calc_locked_ids 使用）不会被污染。
    """
    projection = {'plans': [dict(plan, cards=[card])]}
    _migrate_daily_rule(projection)
    _migrate_record_fields(projection)
    _migrate_tasks(projection)


# =============================================================================
# 单卡写入口的局部读写（打卡 / 改格子 / 撤销 / 补录）
# =============================================================================

def _load_card_for_write(session, plan_id: str, card_id: str):
    """打卡族统一读法：先锁计划行，再只读「本计划投影 + 目标卡完整数据」。

    与 _load_plans_for_write 的差别只在读多少：写入口的并发保护完全相同
    （同计划串行、跨计划并发）。少读的部分是其他计划的全部数据，以及本计划
    其他卡的打卡正文/行情分析 —— 这些在整树读法里每格都要拉，而打卡只改
    一个格子。返回值交给 repo.save_card_slots / save_card / save_sibling_changes
    这组局部写入口，绝不进整树差量保存。

    返回 None 表示计划行不存在（含空库未初始化），调用方按原语义报 404；
    空库自动预置默认计划仍由 list / create-plan 等整树入口负责。
    """
    repo.lock_plan_rows(session, [plan_id] if plan_id else None)
    return repo.load_plan_write_view(session, plan_id, card_id)


def _normalize_write_plan(view):
    """局部写视图的内存归一化：与 _load_plans 同批迁移、同结果。

    目标卡按整树读法的完整口径迁移（含交易记录字段补齐，直接决定回传给
    前端的 slots 内容）；兄弟卡只做统计必需的迁移：daily_rule 影响计划级
    奖励与欢迎语，tasks 推导影响每张卡的任务进度。长文本字段补齐不喂给
    兄弟卡 —— 它们的投影里本来就没有这些键，聚合统计也读不到。
    """
    plan, card = view['plan'], view['card']
    tree = {'plans': [plan]}
    _migrate_daily_rule(tree)
    _migrate_tasks(tree)
    if card is not None:
        _migrate_record_fields({'plans': [dict(plan, cards=[card])]})


def _save_view_writes(session, view):
    """局部写收尾：目标格 → 目标卡 → 被牵连改写的兄弟卡。

    三步都只写自己读过的行与列。整树差量保存在这条路径上不参与：兄弟卡是
    窄投影，缺 goal/notes/review/milestones 等列，喂进整树保存会被当成
    "用户把这些清空了"，不报错、不崩，只是数据没了。
    """
    plan, card = view['plan'], view['card']
    rows = view['rows']
    repo.save_card_slots(session, view['card_id'], card.get('slots'),
                         rows.get('slots'))
    repo.save_card(session, rows['card'], card)
    repo.save_sibling_changes(session, view['plan_id'], plan.get('cards'),
                              rows.get('siblings'))


def _new_id(prefix='card'):
    """生成唯一 ID"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _now_str():
    """当前时间字符串"""
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _ts_to_str(ts_ms):
    """将13位毫秒时间戳转为可读字符串"""
    try:
        ts_int = int(ts_ms)
        if ts_int <= 0:
            return 'N/A'
        return datetime.datetime.fromtimestamp(ts_int / 1000).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError, TypeError):
        return 'N/A'


def _today_str():
    """今天日期字符串"""
    return datetime.datetime.now().strftime('%Y-%m-%d')


def _normalize_datetime_str(value):
    """校验并规范化时间字符串为 YYYY-MM-DD HH:MM:SS

    接受 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS；空值返回 ''；非法返回 None。
    供开始/结束时间、补录打卡时间统一使用，保证存储格式一致。
    """
    if not value:
        return ''
    value = str(value).strip()
    try:
        if len(value) == 16:
            dt = datetime.datetime.strptime(value, '%Y-%m-%d %H:%M')
        elif len(value) == 19:
            dt = datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        else:
            return None
    except ValueError:
        return None
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _create_empty_slots(count=100):
    """创建 N 个空格子"""
    return [
        {
            "slot_index": i,
            "filled": False,
            "filled_at": "",
            "record": None
        }
        for i in range(count)
    ]


def _create_learn_cards(count=10, base_reward=2000):
    """预置学习任务卡"""
    cards = []
    for i in range(1, count + 1):
        reward = base_reward
        cards.append({
            "id": _new_id('learn'),
            "type": "learn",
            "round": i,
            "title": f"学习任务{i} · 100小时",
            "goal": f"第{i}个100小时学习任务",
            "reward": reward,
            "start_time": "",
            "end_time": "",
            "status": "pending" if i > 1 else "in_progress",
            "milestones": [],
            "tasks": [],
            "slots": _create_empty_slots(100),
            "notes": [],
            "review": "",
            "settlement": None,
            "created_at": _now_str(),
            "updated_at": _now_str()
        })
    return cards


def _create_trade_cards(count=10):
    """预置交易轮次卡"""
    cards = []
    for i in range(1, count + 1):
        base_reward = 2000 * i
        hourly_rate = base_reward // 100
        cards.append({
            "id": _new_id('trade'),
            "type": "trade",
            "round": i,
            "title": f"第{i}轮 · 翻倍挑战",
            "goal": f"100交易小时内平均每小时盈利1%，本金翻倍（第{i}轮）",
            "reward": base_reward,
            "base_reward": base_reward,
            "hourly_rate": hourly_rate,
            "start_time": "",
            "end_time": "",
            "status": "pending" if i > 1 else "in_progress",
            "milestones": [],
            "tasks": [],
            "slots": _create_empty_slots(100),
            "notes": [],
            "review": "",
            "settlement": None,
            "created_at": _now_str(),
            "updated_at": _now_str()
        })
    return cards


def _create_default_plans(session):
    """创建默认数据：学习计划 + 交易计划"""
    data = {
        "initialized": True,
        "plans": [
            {
                "id": "plan_learn_1000",
                "type": "learn",
                "name": "1000小时学习计划",
                "grand_goal": "彻底做出稳定运行的量化交易系统",
                "total_hours": 1000,
                "round_count": 10,
                "per_round_hours": 100,
                # 长期主义模式：无强制每日时长、无罚款，只有欢迎
                "daily_rule": {
                    "mode": "longtermism",
                    "min_record_minutes": 1,
                    "early_bonus_multiplier": 1,
                    "welcome_message": "今天也想开始了吗？随时欢迎 👋"
                },
                "cards": _create_learn_cards(10, 2000),
                "created_at": _now_str()
            },
            {
                "id": "plan_trade_1000",
                "type": "trade",
                "name": "1000小时翻10番交易计划",
                "grand_goal": "资金翻10番达到1000倍（10万U）",
                "total_hours": 1000,
                "round_count": 10,
                "per_round_hours": 100,
                # 长期主义模式：无强制每日时长、无罚款，只有欢迎
                "daily_rule": {
                    "mode": "longtermism",
                    "min_record_minutes": 1,
                    "early_bonus_multiplier": 1,
                    "welcome_message": "今天也想开始了吗？随时欢迎 👋"
                },
                "round_config": {
                    "base_reward_first": 2000,
                    "base_reward_step": 2000,
                    "early_bonus_multiplier": 1,
                    "pass_avg_hourly_profit_pct": 1.0
                },
                "cards": _create_trade_cards(10),
                "created_at": _now_str()
            }
        ]
    }
    _save_plans(session, data)
    return data


# =============================================================================
# 任务树（任务管理 v2）
# -----------------------------------------------------------------------------
# 结构：card['tasks'] = [TaskNode, ...]
#   TaskNode = {id, title, estimated_minutes, status: todo|doing|done,
#               children: [TaskNode...], completed_at, completed_by_slot, created_at}
# 铁律：
#   - 一切进度/汇总只计算叶子节点（父节点不重复计入）；
#   - 父节点预估 = Σ叶子（自动重算、只读）；父任务只是任务名；
#   - 完成只能由打卡标记（含子任务全完成后的逐级上卷），树上无勾选；
#   - 已完成的任务不能再被打卡关联。
# =============================================================================

def _iter_tasks(tasks):
    """深度优先遍历任务树，yield (node, siblings, parent_node)"""
    stack = [(t, tasks, None) for t in reversed(tasks or [])]
    while stack:
        node, siblings, parent = stack.pop()
        yield node, siblings, parent
        children = node.get('children') or []
        for ch in reversed(children):
            stack.append((ch, children, node))


def _find_task(tasks, task_id):
    """按 id 查找节点，返回 (node, siblings, parent_node)；未找到返回 None"""
    if not task_id:
        return None
    for node, siblings, parent in _iter_tasks(tasks):
        if node.get('id') == task_id:
            return node, siblings, parent
    return None


def _leaf_tasks(tasks):
    """全部叶子节点（无 children）"""
    return [n for n, _, _ in _iter_tasks(tasks) if not (n.get('children') or [])]


def _task_is_container(node):
    return bool(node and (node.get('children') or []))


def _recompute_task_estimates(tasks):
    """容器预估 = Σ叶子（后序就地写回）；叶子预估值保持不变"""
    def _rec(node):
        children = node.get('children') or []
        if not children:
            node['estimated_minutes'] = int(node.get('estimated_minutes') or 0)
            return node['estimated_minutes']
        total = 0
        for ch in children:
            total += _rec(ch)
        node['estimated_minutes'] = total
        return total
    for t in tasks or []:
        _rec(t)


def _rollup_task(node):
    """自底向上重算父节点状态：子全 done → 父自动 done；否则父从 done 回退"""
    children = node.get('children') or []
    if not children:
        return node.get('status') or 'todo'
    for ch in children:
        _rollup_task(ch)
    if all((ch.get('status') or 'todo') == 'done' for ch in children):
        if node.get('status') != 'done':
            node['status'] = 'done'
            node['completed_at'] = _now_str()
            node.pop('completed_by_slot', None)  # 上卷完成不属于任何一条打卡
    else:
        if node.get('status') == 'done':
            node['status'] = 'doing'
            node.pop('completed_at', None)
            node.pop('completed_by_slot', None)
        elif node.get('status') == 'todo' and any(
                (ch.get('status') or 'todo') != 'todo' for ch in children):
            node['status'] = 'doing'
    return node.get('status')


def _rollup_tasks(tasks):
    for t in tasks or []:
        _rollup_task(t)


def _calc_tasks_all_done(card):
    """任务树是否全部完成（存在叶子且所有叶子 done）——学习卡提前通关资格判定"""
    leaves = _leaf_tasks(card.get('tasks') or [])
    return len(leaves) > 0 and all(n.get('status') == 'done' for n in leaves)


def _calc_task_progress(card):
    """任务进度（叶子加权口径）

    任务进度 = Σ(done 叶子预估分钟) ÷ Σ(叶子预估分钟)
    - 只统计叶子；待补预估（estimated_minutes<=0）的叶子不计入分子/分母；
    - done_count/total_count 按叶子计数，并额外返回待补预估数量。
    """
    leaves = _leaf_tasks(card.get('tasks') or [])
    total_minutes = 0
    done_minutes = 0
    done_count = 0
    pending_estimate = 0
    for n in leaves:
        est = int(n.get('estimated_minutes') or 0)
        if est <= 0:
            pending_estimate += 1
        else:
            total_minutes += est
            if n.get('status') == 'done':
                done_minutes += est
        if n.get('status') == 'done':
            done_count += 1
    pct = round(done_minutes * 100 / total_minutes) if total_minutes > 0 else 0
    return {
        'done_minutes': done_minutes,
        'total_minutes': total_minutes,
        'pct': pct,
        'done_count': done_count,
        'total_count': len(leaves),
        'pending_estimate': pending_estimate,
    }


def _normalize_task_links(raw):
    """打卡 task_links 规范化：仅保留 {task_id, state∈(doing,done)}，去重保序"""
    links = []
    seen = set()
    for item in raw or []:
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
    return links


def _validate_task_links(tasks, links, slot_index):
    """校验打卡关联（task_links 非空时严格执行）

    - 任务必须存在、是叶子、预估已填（待补预估不可关联）；
    - 已完成的任务不能再被关联（除非它正是由当前这条打卡标记完成的——
      修改本条打卡的关联状态时允许）。
    返回错误消息字符串；通过时返回 None。
    """
    for link in links:
        found = _find_task(tasks, link['task_id'])
        if not found:
            return f"任务不存在或已被删除：{link['task_id']}"
        node = found[0]
        title = node.get('title') or ''
        if _task_is_container(node):
            return f"「{title}」是父任务（纯容器），只能关联叶子任务"
        if int(node.get('estimated_minutes') or 0) <= 0:
            return f"「{title}」尚未填写预估时长，补全后才能关联打卡"
        if (node.get('status') == 'done'
                and node.get('completed_by_slot') != slot_index):
            return f"「{title}」已完成，不能继续打卡"
    return None


def _apply_task_links(tasks, links, slot_index, filled_at, prev_links=None):
    """把一条打卡的 task_links 应用到任务树（差量回退 + 状态联动 + 上卷）

    调用前须已通过 _validate_task_links。prev_links 为该打卡的旧关联
    （修改打卡场景），用于回退"本次不再标记完成"的任务。
    """
    prev_map = {l.get('task_id'): l.get('state') for l in (prev_links or [])}
    new_map = {l['task_id']: l['state'] for l in links}
    # 1) 回退：旧版本标记 done，本次不再是 done → 完成依据消失，回退进行中
    for tid, state in prev_map.items():
        if state != 'done' or new_map.get(tid) == 'done':
            continue
        found = _find_task(tasks, tid)
        if found and found[0].get('completed_by_slot') == slot_index:
            node = found[0]
            node['status'] = 'doing'
            node.pop('completed_at', None)
            node.pop('completed_by_slot', None)
    # 2) 应用本次关联：done 写完成依据；doing 首次推进置进行中（不打断已完成）
    for link in links:
        found = _find_task(tasks, link['task_id'])
        if not found:
            continue
        node = found[0]
        if link['state'] == 'done':
            node['status'] = 'done'
            node['completed_at'] = filled_at or _now_str()
            node['completed_by_slot'] = slot_index
        elif node.get('status') != 'done':
            node['status'] = 'doing'
    # 3) 全树上卷（子全完成 → 父自动完成；回退同步传导）
    _rollup_tasks(tasks)


def _revert_slot_task_links(tasks, links, slot_index):
    """删除（或清空）一条打卡时回退其完成依据：done → doing（含上卷父任务）"""
    for link in links or []:
        if link.get('state') != 'done':
            continue
        found = _find_task(tasks, link.get('task_id'))
        if not found:
            continue
        node = found[0]
        if node.get('completed_by_slot') == slot_index:
            node['status'] = 'doing'
            node.pop('completed_at', None)
            node.pop('completed_by_slot', None)
    _rollup_tasks(tasks)


def _touch_task_links_doing(tasks, links):
    """批量补录统一关联：任务置进行中（todo→doing），不涉及完成标记"""
    for link in links or []:
        found = _find_task(tasks, link['task_id'])
        if found and found[0].get('status') == 'todo':
            found[0]['status'] = 'doing'
    _rollup_tasks(tasks)


def _annotate_tasks(card):
    """深拷贝任务树并附加计算属性（不入库，仅供 card-detail 响应）

    - actual_minutes：该任务实际投入（Σ关联打卡时长，全额口径，多任务不摊)
    - link_count：关联打卡次数
    - sub_done_minutes/sub_total_minutes/sub_done_count/sub_total_count：子树聚合
    """
    tasks = json.loads(json.dumps(card.get('tasks') or [], ensure_ascii=False))
    actual = {}
    counts = {}
    for slot in card.get('slots') or []:
        rec = slot.get('record') or {}
        minutes = int(rec.get('duration_minutes') or 0)
        for link in rec.get('task_links') or []:
            tid = link.get('task_id')
            if not tid:
                continue
            actual[tid] = actual.get(tid, 0) + minutes
            counts[tid] = counts.get(tid, 0) + 1

    def _agg(node):
        children = node.get('children') or []
        node['actual_minutes'] = actual.get(node.get('id'), 0)
        node['link_count'] = counts.get(node.get('id'), 0)
        if children:
            dm = tm = dc = tc = 0
            for ch in children:
                _agg(ch)
                dm += ch.get('sub_done_minutes', 0)
                tm += ch.get('sub_total_minutes', 0)
                dc += ch.get('sub_done_count', 0)
                tc += ch.get('sub_total_count', 0)
            node['sub_done_minutes'] = dm
            node['sub_total_minutes'] = tm
            node['sub_done_count'] = dc
            node['sub_total_count'] = tc
        else:
            est = int(node.get('estimated_minutes') or 0)
            done = node.get('status') == 'done'
            node['sub_done_minutes'] = est if done else 0
            node['sub_total_minutes'] = est
            node['sub_done_count'] = 1 if done else 0
            node['sub_total_count'] = 1
        return node

    for t in tasks:
        _agg(t)
    return tasks


# =============================================================================
# 核算引擎（长期主义版本：无罚款，核心指标=在场天数）
# =============================================================================

def _calc_presence_days(slots):
    """统计在场天数：有任意打卡记录（filled）的日期数量

    长期主义核心指标——今天在场，比今天做多少更重要。
    只要当天勾选了 1 格（哪怕几分钟），就算"在场一天"。
    """
    days = set()
    for s in slots:
        if s.get('filled') and s.get('filled_at'):
            days.add(s['filled_at'][:10])
    return len(days)


def _calc_streak_days(all_slots):
    """计算连续在场天数（streak）：从今天/最近在场日往前连续不中断

    中断不产生任何惩罚：重新开始计数即可，回来本身就该被庆祝。
    """
    days = set()
    for slots in all_slots:
        for s in slots:
            if s.get('filled') and s.get('filled_at'):
                days.add(s['filled_at'][:10])
    if not days:
        return 0
    today = datetime.date.today()
    # 今天还没打卡不算断档：从昨天开始往前数
    cursor = today if today.isoformat() in days else today - datetime.timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= datetime.timedelta(days=1)
    return streak


def _early_multiplier(plan):
    """提前通关奖励倍率（v1.1 起固定 ×1）

    提前通关本身就是奖励——省下的时间比翻倍奖金更值钱。
    读取顺序：计划 round_config → daily_rule → 默认 1。
    """
    if plan:
        rc = plan.get('round_config', {})
        if rc.get('early_bonus_multiplier') is not None:
            return rc.get('early_bonus_multiplier')
        rule = plan.get('daily_rule', {})
        if rule.get('early_bonus_multiplier') is not None:
            return rule.get('early_bonus_multiplier')
    return 1


def _calc_best_streak_days(all_slots):
    """计算历史最佳连续在场天数（v1.1：展示最佳而非当前连续）

    断档不归零、不惩罚——大字永远显示"历史上最长的坚持段"；
    断档如实记录在在场日历里，随时可以续上。
    """
    days = set()
    for slots in all_slots:
        for s in slots:
            if s.get('filled') and s.get('filled_at'):
                days.add(s['filled_at'][:10])
    if not days:
        return 0
    best = 1
    cur = 1
    prev = None
    for ds in sorted(days):
        d = datetime.date.fromisoformat(ds)
        if prev is not None and (d - prev).days == 1:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 1
        prev = d
    return best


_CALENDAR_WEEKS = 15  # 在场日历显示近 15 周（GitHub 贡献图风格）


def _calendar_level(minutes):
    """在场时长分级（0-4）：0 无在场 → 4 两小时以上"""
    if minutes <= 0:
        return 0
    if minutes < 30:
        return 1
    if minutes < 60:
        return 2
    if minutes < 120:
        return 3
    return 4


def _build_presence_calendar(all_slots, weeks=_CALENDAR_WEEKS):
    """构建在场日历：从今天所在周的周一起，往前 weeks 周

    每天返回 {date, minutes, level, future}——断档如实呈现（level 0），
    与"最佳连续在场"配合：数据诚实，情绪友好。
    """
    minutes_by_day = {}
    for s in all_slots:
        if s.get('filled') and s.get('filled_at'):
            key = s['filled_at'][:10]
            minutes_by_day[key] = minutes_by_day.get(key, 0) + ((s.get('record') or {}).get('duration_minutes') or 0)
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    start = monday - datetime.timedelta(weeks=weeks - 1)
    days = []
    for i in range(weeks * 7):
        d = start + datetime.timedelta(days=i)
        ds = d.isoformat()
        minutes = minutes_by_day.get(ds, 0)
        days.append({
            'date': ds,
            'minutes': minutes,
            'level': _calendar_level(minutes),
            'future': d > today
        })
    return {'start_date': start.isoformat(), 'weeks': weeks, 'days': days}


def _calc_early_eligible(card):
    """是否具备提前通关资格（学习/交易卡同口径）：
    任务树全部完成（任务完成即通关，无需硬耗 100 小时）
    """
    return _calc_tasks_all_done(card)


def _calc_slot_hours_by_date(slots, date_str):
    """统计某日 slots 中 filled 的总时长（小时数）"""
    total_minutes = 0
    for slot in slots:
        if not slot.get('filled') or not slot.get('filled_at'):
            continue
        if slot['filled_at'].startswith(date_str):
            record = slot.get('record')
            if record:
                total_minutes += record.get('duration_minutes', 0)
    return total_minutes / 60.0


def _calc_checkin_count(slots, date_str=None):
    """统计打卡次数（filled 格子数）；传 date_str 时只统计当日"""
    count = 0
    for slot in slots:
        if not slot.get('filled') or not slot.get('filled_at'):
            continue
        if date_str and not slot['filled_at'].startswith(date_str):
            continue
        count += 1
    return count


def _build_checkin_daily(learn_slots, trade_slots):
    """按日聚合学习/交易打卡次数，从首次打卡日到今日逐日补齐（无打卡日补 0），
    供前端绘制每日/累积打卡双曲线折线图"""
    learn_by_day = {}
    trade_by_day = {}
    for s in learn_slots:
        if s.get('filled') and s.get('filled_at'):
            d = s['filled_at'][:10]
            learn_by_day[d] = learn_by_day.get(d, 0) + 1
    for s in trade_slots:
        if s.get('filled') and s.get('filled_at'):
            d = s['filled_at'][:10]
            trade_by_day[d] = trade_by_day.get(d, 0) + 1
    all_dates = set(learn_by_day) | set(trade_by_day)
    if not all_dates:
        return []
    fmt = '%Y-%m-%d'
    start = datetime.datetime.strptime(min(all_dates), fmt)
    end = datetime.datetime.strptime(max(_today_str(), max(all_dates)), fmt)
    series = []
    cur = start
    while cur <= end:
        d = cur.strftime(fmt)
        series.append({'date': d, 'learn': learn_by_day.get(d, 0), 'trade': trade_by_day.get(d, 0)})
        cur += datetime.timedelta(days=1)
    return series


def _calc_total_hours(slots):
    """统计 slots 中 filled 的总格子数（即总小时数）"""
    return sum(1 for s in slots if s.get('filled'))


def _daily_target_hours(plan):
    """学习任务单日达标线（小时）：daily_rule.target_hours 优先，否则默认 10。"""
    rule = (plan or {}).get('daily_rule', {}) or {}
    raw = rule.get('target_hours')
    try:
        val = float(raw) if raw else DAILY_TARGET_HOURS
    except (TypeError, ValueError):
        val = DAILY_TARGET_HOURS
    return val if val > 0 else DAILY_TARGET_HOURS


def _parse_balance(value):
    """把打卡记录里的账户余额（字符串/数字）安全解析为 float；无效返回 None。"""
    if value is None or value == '':
        return None
    try:
        num = float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _trade_growth(card):
    """交易卡资金增长（自动口径：首/末打卡余额）。

    初始本金 = 首次打卡（按 filled_at 升序）携带有效 account_balance 的格子；
    当前余额 = 末次打卡携带有效余额的格子。增长率 = (当前-初始)/初始。
    is_pass 仅表示「增长率 > TRADE_GROWTH_THRESHOLD（默认 8%）」——该阈值是
    【单日达标线】（供 _calc_achievement 评级与展示），不是通关标准；
    结算通关另按 _settle_card 统一口径判定。

    无有效余额数据时 known=False（列表窄投影读不到 account_balance 即此情形，
    详情/结算走整树读法能拿到余额）。
    """
    filled = [(s.get('filled_at') or '', (s.get('record') or {}).get('account_balance'))
              for s in card.get('slots', []) if s.get('filled') and s.get('filled_at')]
    filled.sort(key=lambda x: x[0])
    balances = [(dt, _parse_balance(bal)) for dt, bal in filled]
    balances = [(dt, b) for dt, b in balances if b is not None]
    if not balances:
        return {'known': False, 'principal': None, 'current': None,
                'growth_pct': 0.0, 'is_pass': False,
                'threshold_pct': round(TRADE_GROWTH_THRESHOLD * 100, 1)}
    principal = balances[0][1]
    current = balances[-1][1]
    growth = (current - principal) / principal if principal else 0.0
    return {'known': True, 'principal': round(principal, 2), 'current': round(current, 2),
            'growth_pct': round(growth * 100, 2),
            'is_pass': growth > TRADE_GROWTH_THRESHOLD,
            'threshold_pct': round(TRADE_GROWTH_THRESHOLD * 100, 1)}


def _judged_window(card):
    """达标率统计窗口：返回 (start_d, last_d) 或 None。

    - 已结算卡：起点 = start_time，判到结算日（含当天）；
    - 进行中卡：起点 = start_time，判到昨天（今天尚未过完，不计入"已消耗"）。
    """
    start_str = (card.get('start_time') or '')[:10]
    if not start_str:
        return None
    try:
        start_d = datetime.date.fromisoformat(start_str)
    except ValueError:
        return None
    settle = card.get('settlement')
    if settle and settle.get('settled_at'):
        try:
            last_d = datetime.date.fromisoformat(settle['settled_at'][:10])
        except ValueError:
            last_d = datetime.date.today() - datetime.timedelta(days=1)
    else:
        last_d = datetime.date.today() - datetime.timedelta(days=1)
    if last_d < start_d:
        return None
    return start_d, last_d


def _calc_achievement(plan, card):
    """达标率：已达标天数 / 当前已消耗天数（纯展示，不关联经济惩罚）。

    单日达标标准按卡类型区分：
    - 学习卡：当日学习时长 ≥ DAILY_TARGET_HOURS（默认 10h）
    - 交易卡：当日账户余额较初始本金增长 > 8%（余额沿时间结转；无余额数据 known=False）
    返回 {achieved_days, consumed_days, rate, known}。
    """
    window = _judged_window(card)
    if not window:
        return {'achieved_days': 0, 'consumed_days': 0, 'rate': 0.0, 'known': False}
    start_d, last_d = window

    slots = card.get('slots', [])
    if card.get('type') == 'trade':
        g = _trade_growth(card)
        if not g['known']:
            return {'achieved_days': 0, 'consumed_days': (last_d - start_d).days + 1,
                    'rate': 0.0, 'known': False}
        # 逐日取"截至当日最近一次余额"，判定较初始本金是否增长 > 8%
        bal_by_day = {}
        for s in sorted((x for x in slots if x.get('filled') and x.get('filled_at')),
                        key=lambda x: x['filled_at']):
            b = _parse_balance((s.get('record') or {}).get('account_balance'))
            if b is not None:
                bal_by_day[s['filled_at'][:10]] = b
        principal = g['principal']
        achieved = consumed = 0
        carry = None
        cur = start_d
        while cur <= last_d:
            consumed += 1
            ds = cur.isoformat()
            if ds in bal_by_day:
                carry = bal_by_day[ds]
            if carry is not None and principal and \
                    (carry - principal) / principal > TRADE_GROWTH_THRESHOLD:
                achieved += 1
            cur += datetime.timedelta(days=1)
        rate = round(achieved / consumed * 100, 1) if consumed else 0.0
        return {'achieved_days': achieved, 'consumed_days': consumed, 'rate': rate, 'known': True}

    target = _daily_target_hours(plan)
    achieved = consumed = 0
    cur = start_d
    while cur <= last_d:
        consumed += 1
        if _calc_slot_hours_by_date(slots, cur.isoformat()) >= target:
            achieved += 1
        cur += datetime.timedelta(days=1)
    rate = round(achieved / consumed * 100, 1) if consumed else 0.0
    return {'achieved_days': achieved, 'consumed_days': consumed, 'rate': rate, 'known': True}


def _countdown_days(plan):
    """本计划每张卡的倒计时天数上限：daily_rule.countdown_days 优先，否则默认常量。
    返回 <=0 表示该计划关闭倒计时。
    """
    rule = (plan or {}).get('daily_rule', {}) or {}
    raw = rule.get('countdown_days')
    if raw is None:
        return PLAN_COUNTDOWN_DEFAULT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return PLAN_COUNTDOWN_DEFAULT


def _calc_days_elapsed(card):
    """自 start_time 起已过的整日历天数（最小 0）；无 start_time / 非法日期返回 None。"""
    start_str = (card.get('start_time') or '')[:10]
    if not start_str:
        return None
    try:
        start_d = datetime.date.fromisoformat(start_str)
    except ValueError:
        return None
    return max(0, (datetime.date.today() - start_d).days)


def _countdown_view(plan, card):
    """倒计时三态。返回 dict：
      limit / elapsed / remaining / expired / active
    active=True 仅当该卡「未结算 且 in_progress 且计划开启倒计时(>0) 且设有 start_time」。
    completed / 已结算 / 锁定(pending) 卡一律 active=False，不参与倒计时（不受影响）。
    """
    limit = _countdown_days(plan)
    elapsed = _calc_days_elapsed(card)
    active = (card.get('status') == 'in_progress' and not card.get('settlement')
              and limit > 0 and elapsed is not None)
    if not active:
        return {'limit': limit, 'elapsed': elapsed if elapsed is not None else 0,
                'remaining': 0, 'expired': False, 'active': False}
    return {'limit': limit, 'elapsed': elapsed,
            'remaining': max(0, limit - elapsed), 'expired': elapsed >= limit, 'active': True}


def _is_countdown_expired(plan, card):
    """第三个结束条件是否已触发：未结算 in_progress 卡自 start_time 起满倒计时天数。"""
    v = _countdown_view(plan, card)
    return bool(v['active'] and v['expired'])


def _calc_card_reward(plan, card):
    """计算任务卡的奖励预估（无罚款版：仅"基础 + 提前"，达标率纯展示）

    奖励构成 = 基础奖励 + 提前完成奖励
    - 学习/交易卡同口径：任务树全部完成后具备提前通关资格，此时才计入提前奖励
    达标率（已达标天数 / 已消耗天数）与交易增长一并注入，供前端进度面板展示。
    """
    filled_count = _calc_total_hours(card.get('slots', []))
    presence_days = _calc_presence_days(card.get('slots', []))

    if card['type'] == 'trade':
        base_reward = card.get('base_reward', 2000)
        hourly_rate = card.get('hourly_rate', 20)
    else:
        base_reward = card.get('reward', 2000)
        hourly_rate = base_reward // 100

    # 提前奖励：每提前 1 小时 = 时薪 × 1（v1.1 起不再翻倍——提前通关本身就是奖励：时间自由）
    early_eligible = _calc_early_eligible(card)
    early_hours = max(0, 100 - filled_count) if early_eligible else 0
    early_bonus = early_hours * hourly_rate * _early_multiplier(plan) if early_eligible else 0
    final_reward = base_reward + early_bonus

    info = {
        'filled_count': filled_count,
        'total_hours': round(filled_count, 1),
        'presence_days': presence_days,
        'base_reward': base_reward,
        'early_eligible': early_eligible,
        'early_hours': early_hours,
        'early_bonus': round(early_bonus, 1),
        'final_reward': round(final_reward, 1)
    }

    # 达标率：已达标天数 / 已消耗天数（按卡类型的完成标准判定，纯记录不扣钱）
    ach = _calc_achievement(plan, card)
    info['achievement_days'] = ach['achieved_days']
    info['achievement_consumed_days'] = ach['consumed_days']
    info['achievement_rate'] = ach['rate']
    info['achievement_known'] = ach['known']

    # 交易卡资金增长（首/末打卡余额自动口径）：详情/结算能拿到余额时 known=True
    if card['type'] == 'trade':
        g = _trade_growth(card)
        info['trade_growth'] = g

    # 「10 天倒计时」完成期限：始终返回，供前端展示"剩余 X 天"与判定到期。
    # 仅未结算 in_progress 卡 active/expired 有意义；completed/已结算卡 remaining=0。
    cd = _countdown_view(plan, card)
    info['countdown_days'] = cd['limit']
    info['days_elapsed'] = cd['elapsed']
    info['days_remaining'] = cd['remaining']
    info['countdown_expired'] = cd['expired']
    info['countdown_active'] = cd['active']
    return info


def _settle_card(plan, card):
    """正式结算一张任务卡（生成 settlement）——无罚款，按统一完成标准判定通关。

    完成标准（学习/交易卡同口径）：满 100 小时 或 任务树全部完成 → 通关。
    TRADE_GROWTH_THRESHOLD（8%）是交易卡的【单日达标线】（只进达标率评级与
    展示），不参与通关判定；资金增长仅作记录快照写入 settlement。
    奖金熔断（完成期限约束）：通关才发奖，基础+提前一并计入；未通关则全额归零
    （final_reward=0，取消该任务全部奖金）。结算后落库达标率快照（含结算当天）。
    """
    reward_info = _calc_card_reward(plan, card)
    filled_count = reward_info['filled_count']

    if card['type'] == 'trade':
        base_reward = card.get('base_reward', 2000)
    else:
        base_reward = card.get('reward', 2000)

    # 通关判定两类卡统一：满 100 格 或 任务树全部完成
    is_pass = filled_count >= 100 or _calc_tasks_all_done(card)
    early_bonus = reward_info['early_bonus'] if is_pass else 0
    final = (base_reward + early_bonus) if is_pass else 0
    settlement = {
        "base_reward": base_reward,
        "total_hours_used": filled_count,
        "early_hours": max(0, 100 - filled_count),
        "early_bonus": early_bonus,
        "final_reward": round(final, 1),
        "settled_at": _now_str()
    }
    if card['type'] == 'trade':
        # 资金增长仅作记录快照（known=False 时写 None），不参与 is_pass
        g = _trade_growth(card)
        settlement.update({
            "growth_pct": g['growth_pct'] if g['known'] else None,
            "growth_threshold_pct": g['threshold_pct'],
            "principal": g['principal'],
            "current_balance": g['current'],
            "growth_known": g['known'],
        })

    card['settlement'] = settlement
    card['status'] = 'completed' if is_pass else 'failed'

    # 达标率快照落库（含结算当天）：settlement 写入后重算窗口即覆盖到结算日
    ach = _calc_achievement(plan, card)
    settlement['achievement_days'] = ach['achieved_days']
    settlement['achievement_consumed_days'] = ach['consumed_days']
    settlement['achievement_rate'] = ach['rate']

    # 解锁下一张卡
    _unlock_next_card(plan, card)
    card['updated_at'] = _now_str()
    return card


def _next_pending_card(plan, current_card):
    """找出结算后应当被解锁的下一张卡（round+1 且 pending）；纯查询不改数据。

    单卡局部写路径需要在结算前先知道会牵连到哪张兄弟卡，才能把它那一行的
    变更写回去，因此把「找谁」和「改谁」拆成两步。
    """
    current_round = current_card.get('round', 0)
    for c in plan.get('cards', []):
        if c.get('round') == current_round + 1 and c.get('status') == 'pending':
            return c
    return None


def _unlock_next_card(plan, current_card):
    """解锁下一张卡；返回被解锁的卡（没有则 None）"""
    nxt = _next_pending_card(plan, current_card)
    if nxt is None:
        return None
    nxt['status'] = 'in_progress'
    nxt['start_time'] = _now_str()
    nxt['updated_at'] = _now_str()
    return nxt


def _recompute_serial_chain(plan):
    """按 round 顺序重算串行链：第一张未结束的卡为进行中，其后未结束的卡全部锁定为待开始"""
    cards = sorted(plan.get('cards', []), key=lambda x: x.get('round', 0))
    active_found = False
    for c in cards:
        if c.get('status') in ('completed', 'failed', 'abandoned'):
            continue
        if not active_found:
            active_found = True
            if c.get('status') != 'in_progress':
                c['status'] = 'in_progress'
                if not c.get('start_time'):
                    c['start_time'] = _now_str()
        elif c.get('status') != 'pending':
            c['status'] = 'pending'


def _apply_countdown_settlements(plan):
    """第三个结束条件落库：对某计划内所有"未结算 in_progress 且已满倒计时天数"的卡
    执行结算/终止，随后重算串行链。返回被结算的卡 id 列表（无则空）。
    通关标准两类卡统一（满 100 格或任务树全部完成），倒计时到期未满即 failed
    终止——余额未知不再阻塞结算（8% 线仅作每日评级，不参与通关判定）。
    纯内存整树操作，加锁读写与落库由调用方（POST 写路径）负责。
    """
    settled = []
    for c in sorted(plan.get('cards', []), key=lambda x: x.get('round', 0)):
        if _is_countdown_expired(plan, c):
            _settle_card(plan, c)
            settled.append(c['id'])
    if settled:
        _recompute_serial_chain(plan)
    return settled


# =============================================================================
# 统计助手
# =============================================================================

def _calc_card_hit_rate(card):
    """单卡预测命中率（仅交易卡有 actual/hit 概念；学习卡返回 0）"""
    total_hit = 0
    total_filled = 0
    for s in card.get('slots', []):
        r = s.get('record')
        if r and r.get('actual') is not None and r.get('actual') != '':
            total_filled += 1
            if r.get('hit'):
                total_hit += 1
    return round(total_hit / total_filled * 100, 1) if total_filled > 0 else 0


def _calc_locked_ids(plan):
    """串行锁定：第一张未结束的卡为当前卡，其后未结束卡全部锁定"""
    locked = set()
    active_found = False
    for c in sorted(plan.get('cards', []), key=lambda x: x.get('round', 0)):
        if c['status'] in ('pending', 'in_progress'):
            if not active_found:
                active_found = True
            else:
                locked.add(c['id'])
    return locked


def _is_card_locked(plan, card_id):
    """判断指定卡是否被锁定（供 card-detail 接口使用）"""
    return card_id in _calc_locked_ids(plan)


def _build_plan_stats(plan):
    """构建计划级别的统计数据"""
    cards = plan.get('cards', [])
    learn_cards = [c for c in cards if c['type'] == 'learn']
    trade_cards = [c for c in cards if c['type'] == 'trade']

    learn_completed = sum(1 for c in learn_cards if c['status'] == 'completed')
    trade_completed = sum(1 for c in trade_cards if c['status'] == 'completed')
    learn_total_hours = sum(_calc_total_hours(c.get('slots', [])) for c in learn_cards)
    trade_total_hours = sum(_calc_total_hours(c.get('slots', [])) for c in trade_cards)

    # 长期主义核心指标：在场天数（按日期去重合计）与连续在场天数
    learn_presence_days = _calc_presence_days([s for c in learn_cards for s in c.get('slots', [])])
    trade_presence_days = _calc_presence_days([s for c in trade_cards for s in c.get('slots', [])])
    all_slots = [s for c in cards for s in c.get('slots', [])]
    total_presence_days = _calc_presence_days(all_slots)
    # v1.1：展示"最佳连续在场"（历史最长段），断档不归零——数据诚实，情绪友好
    best_streak_days = _calc_best_streak_days([c.get('slots', []) for c in cards])
    presence_calendar = _build_presence_calendar(all_slots)

    total_reward = sum(
        (c.get('settlement', {}) or {}).get('final_reward', 0)
        for c in cards if c.get('settlement')
    )

    # 预测命中率（交易卡）
    total_hit = 0
    total_filled_predict = 0
    for c in trade_cards:
        for s in c.get('slots', []):
            r = s.get('record')
            if r and r.get('actual') is not None and r.get('actual') != '':
                total_filled_predict += 1
                if r.get('hit'):
                    total_hit += 1

    hit_rate = round(total_hit / total_filled_predict * 100, 1) if total_filled_predict > 0 else 0

    # 当前进行中的卡片 + 串行锁定
    locked_ids = _calc_locked_ids(plan)
    active_card = None
    for c in sorted(cards, key=lambda x: x.get('round', 0)):
        if c['status'] in ('pending', 'in_progress') and c['id'] not in locked_ids:
            active_card = {
                'id': c['id'],
                'title': c['title'],
                'round': c['round'],
                'type': c['type'],
                'filled_count': _calc_total_hours(c.get('slots', [])),
            }
            break

    # 列表页只需摘要字段；完整 slots/notes/tasks 走 /plan/api/card-detail 按需加载
    result = []
    for c in sorted(cards, key=lambda x: x.get('round', 0)):
        c_info = {
            'id': c['id'],
            'type': c['type'],
            'round': c.get('round', 0),
            'title': c.get('title', ''),
            'reward': c.get('reward', 0),
            'base_reward': c.get('base_reward', c.get('reward', 0)),
            'hourly_rate': c.get('hourly_rate', 0),
            'status': c.get('status', 'pending'),
            'filled_count': _calc_total_hours(c.get('slots', [])),
            'locked': c['id'] in locked_ids,
            'hit_rate': _calc_card_hit_rate(c),
            'start_time': c.get('start_time', ''),
            'end_time': c.get('end_time', ''),
            'created_at': c.get('created_at', ''),
            'reward_info': _calc_card_reward(plan, c),
            'task_progress': _calc_task_progress(c),
        }
        result.append(c_info)

    # 打卡次数统计：严格区分学习/交易，累计与当日分开计数
    today = _today_str()
    learn_slots_all = [s for c in learn_cards for s in c.get('slots', [])]
    trade_slots_all = [s for c in trade_cards for s in c.get('slots', [])]
    learn_checkin_total = _calc_checkin_count(learn_slots_all)
    trade_checkin_total = _calc_checkin_count(trade_slots_all)
    learn_checkin_today = _calc_checkin_count(learn_slots_all, today)
    trade_checkin_today = _calc_checkin_count(trade_slots_all, today)
    # 每日打卡历史序列（供前端绘制每日/累积双曲线）
    checkin_daily = _build_checkin_daily(learn_slots_all, trade_slots_all)

    return {
        'learn_completed': learn_completed,
        'learn_total': len(learn_cards),
        'trade_completed': trade_completed,
        'trade_total': len(trade_cards),
        'learn_total_hours': learn_total_hours,
        'trade_total_hours': trade_total_hours,
        'learn_checkin_total': learn_checkin_total,
        'trade_checkin_total': trade_checkin_total,
        'learn_checkin_today': learn_checkin_today,
        'trade_checkin_today': trade_checkin_today,
        'checkin_daily': checkin_daily,
        'learn_presence_days': learn_presence_days,
        'trade_presence_days': trade_presence_days,
        'total_presence_days': total_presence_days,
        'best_streak_days': best_streak_days,
        'presence_calendar': presence_calendar,
        'total_reward': round(total_reward, 1),
        'hit_rate': hit_rate,
        'active_card': active_card,
        'cards': result,
    }


def _build_plan_info(plan):
    """构建 /plan/api/list 的单个计划返回结构（含 stats）。

    抽取为独立函数，供列表接口与打卡变更接口（fill/update/unfill/backfill）
    复用，保证局部刷新返回的 plan 结构与整表拉取完全一致。
    """
    return {
        'id': plan['id'],
        'type': plan['type'],
        'name': plan['name'],
        'grand_goal': plan.get('grand_goal', ''),
        'total_hours': plan.get('total_hours', 1000),
        'round_count': plan.get('round_count', 10),
        'per_round_hours': plan.get('per_round_hours', 100),
        'daily_rule': plan.get('daily_rule', {}),
        'round_config': plan.get('round_config'),
        'stats': _build_plan_stats(plan),
        'created_at': plan.get('created_at', '')
    }


def _build_today_entry(plan):
    """构建 /plan/api/today-status 的单个计划返回结构（今日在场 + 在场日历）。"""
    today = _today_str()
    learn_hours = 0
    learn_presence = 0
    trade_hours = 0
    trade_presence = 0
    for c in plan.get('cards', []):
        if c['type'] == 'learn':
            learn_hours += _calc_slot_hours_by_date(c.get('slots', []), today)
            learn_presence += _calc_presence_days(c.get('slots', []))
        elif c['type'] == 'trade':
            trade_hours += _calc_slot_hours_by_date(c.get('slots', []), today)
            trade_presence += _calc_presence_days(c.get('slots', []))
    all_slots = [s for c in plan.get('cards', []) for s in c.get('slots', [])]
    daily_rule = plan.get('daily_rule', {})
    return {
        'plan_id': plan['id'],
        'plan_name': plan['name'],
        'today_learn_hours': round(learn_hours, 1),
        'today_trade_hours': round(trade_hours, 1),
        'today_present': (learn_hours + trade_hours) > 0,
        'learn_presence_days': learn_presence,
        'trade_presence_days': trade_presence,
        'welcome': daily_rule.get('welcome_message', '今天也想开始了吗？随时欢迎 👋'),
        'presence_calendar': _build_presence_calendar(all_slots)
    }


def _build_card_detail(plan, card):
    """构建 /plan/api/card-detail 的返回结构（单卡完整数据）。"""
    return {
        'id': card['id'],
        'plan_id': plan['id'],
        'type': card['type'],
        'round': card.get('round', 0),
        'title': card.get('title', ''),
        'goal': card.get('goal', ''),
        'reward': card.get('reward', 0),
        'base_reward': card.get('base_reward', card.get('reward', 0)),
        'hourly_rate': card.get('hourly_rate', 0),
        'status': card.get('status', 'pending'),
        'filled_count': _calc_total_hours(card.get('slots', [])),
        'locked': _is_card_locked(plan, card['id']),
        'hit_rate': _calc_card_hit_rate(card),
        'milestones': card.get('milestones', []),
        'todos': card.get('todos', []),
        # 任务树（任务管理 v2）：深拷贝 + 计算属性（实际投入/子聚合），不入库
        'tasks': _annotate_tasks(card),
        'task_progress': _calc_task_progress(card),
        'tasks_all_done': _calc_tasks_all_done(card),
        'slots': card.get('slots', []),
        'notes': card.get('notes', []),
        'review': card.get('review', ''),
        'settlement': card.get('settlement'),
        'start_time': card.get('start_time', ''),
        'end_time': card.get('end_time', ''),
        'created_at': card.get('created_at', ''),
        'reward_info': _calc_card_reward(plan, card),
    }


def _build_refresh_payload(plan, card):
    """打卡变更后一次性返回前端局部刷新所需数据（纯内存计算，零额外 DB 往返）。

    fill/update/unfill/backfill 接口内部已 _load_plans + _save_plans，变更后的完整
    数据已在内存，直接复用上述构造器返回 plan/today/card 三块，前端据此就地更新
    总览、趋势图、计划详情、今日面板与卡片弹窗，无需再发 list/today-status/card-detail 三次请求。

    防御性降级：统计构造若因异常数据报错，返回 None，前端自动退回整表刷新，
    确保已成功保存的打卡操作不会因附带载荷构建失败而退化成 500。
    """
    try:
        return {
            'plan': _build_plan_info(plan),
            'today': _build_today_entry(plan),
            'card': _build_card_detail(plan, card),
        }
    except Exception:
        logger.warning('[Plan] refresh 载荷构建失败，前端将退回整表刷新', exc_info=True)
        return None


# =============================================================================
# API 路由
# =============================================================================

@plan_bp.route('/plan/api/list', methods=['GET'])
def api_list():
    """获取所有计划 + 统计数据 + 当日状态（窄投影读法，不拉长文本）"""
    try:
        with session_scope() as session:
            data = _load_summary_plans(session)
            plans = data.get('plans', [])
            result = []
            for plan in plans:
                result.append(_build_plan_info(plan))
        return jsonify({"code": 200, "message": "success", "data": result})
    except Exception as e:
        logger.error(f"[Plan] api_list 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/card-detail', methods=['GET'])
def api_card_detail():
    """按需获取单张任务卡的完整数据（slots/notes/milestones/review/settlement）

    列表接口只返回摘要，点击卡片打开详情弹窗时调本接口取完整数据，
    避免列表页一次性传输所有卡的 100 格明细。

    读取范围同样收敛到「本卡」：只取目标计划行、目标卡行、本卡格子，
    外加同计划卡片的 id/type/round/status 摘要（算串行锁定用）。
    整树读法在 200 张卡 × 100 格时会把别人的打卡正文一起拖进内存。
    """
    try:
        plan_id = request.args.get('plan_id', '')
        card_id = request.args.get('card_id', '')
        if not plan_id or not card_id:
            return jsonify({"code": 400, "message": "plan_id 和 card_id 不能为空", "data": None})
        with session_scope() as session:
            view = repo.load_card_view(session, plan_id, card_id)
            if view is not None and view.get('card') is None:
                return jsonify({"code": 404, "message": "任务卡不存在", "data": None})
            if view is None:
                # 计划行不存在：走整树确认一次，覆盖「库尚未初始化需预置默认计划」
                # 的历史语义（冷路径，正常查询不会到这里）
                data = _load_plans(session)
                plan = next((p for p in data.get('plans', []) if p['id'] == plan_id), None)
                if not plan:
                    return jsonify({"code": 404, "message": "计划不存在", "data": None})
                card = next((c for c in plan.get('cards', []) if c['id'] == card_id), None)
                if not card:
                    return jsonify({"code": 404, "message": "任务卡不存在", "data": None})
            else:
                plan, card = view['plan'], view['card']
                _normalize_view_plan(plan, card)
            detail = _build_card_detail(plan, card)
        return jsonify({"code": 200, "message": "success", "data": detail})
    except Exception as e:
        logger.error(f"[Plan] api_card_detail 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/create-plan', methods=['POST'])
def api_create_plan():
    """创建新计划"""
    try:
        body = request.get_json() or {}
        plan_type = body.get('type', 'custom')
        name = body.get('name', '自定义计划')

        new_plan = {
            "id": _new_id('plan'),
            "type": plan_type,
            "name": name,
            "grand_goal": body.get('grand_goal', ''),
            "total_hours": body.get('total_hours', 100),
            "round_count": body.get('round_count', 1),
            "per_round_hours": body.get('per_round_hours', 100),
            "daily_rule": {
                # 长期主义模式：无强制每日时长、无罚款，只有欢迎
                "mode": "longtermism",
                "min_record_minutes": 1,
                "early_bonus_multiplier": 1,
                "welcome_message": "今天也想开始了吗？随时欢迎 👋"
            },
            "cards": [],
            "created_at": _now_str()
        }

        # 如果是学习/交易模板，自动生成卡片
        if plan_type == 'learn':
            new_plan['cards'] = _create_learn_cards(new_plan['round_count'], body.get('base_reward', 2000))
        elif plan_type == 'trade':
            new_plan['round_config'] = {
                "base_reward_first": 2000,
                "base_reward_step": 2000,
                "early_bonus_multiplier": 1,
                "pass_avg_hourly_profit_pct": 1.0
            }
            new_plan['cards'] = _create_trade_cards(new_plan['round_count'])

        with session_scope() as session:
            data = _load_plans_for_write(session)
            data['plans'].append(new_plan)
            _save_plans(session, data)
        return jsonify({"code": 200, "message": "创建成功", "data": new_plan})
    except Exception as e:
        logger.error(f"[Plan] api_create_plan 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/delete-plan', methods=['POST'])
def api_delete_plan():
    """删除计划"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        with session_scope() as session:
            # 删除会重排其余所有计划的 sort_order，属于整树结构性改写，
            # 与 create-plan 一致锁 plan_plans 全表，不能只锁被删的那一行。
            data = _load_plans_for_write(session)
            data['plans'] = [p for p in data['plans'] if p['id'] != plan_id]
            _save_plans(session, data)
        return jsonify({"code": 200, "message": "删除成功", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/create-card', methods=['POST'])
def api_create_card():
    """在指定计划下创建任务卡（手动创建，round 从 11 开始）"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_type = body.get('type', 'learn')

        with session_scope() as session:
            data = _load_plans_for_write(session, plan_id)
            plan = None
            for p in data['plans']:
                if p['id'] == plan_id:
                    plan = p
                    break
            if not plan:
                return jsonify({"code": 404, "message": "计划不存在", "data": None})

            # 计算 round：找到最大 round + 1（至少 11）
            max_round = max((c.get('round', 0) for c in plan.get('cards', [])), default=10)
            new_round = max(max_round + 1, 11)

            if card_type == 'learn':
                # 默认继承上一张学习卡奖励（不传 reward 时）
                prev_rewards = [c.get('reward', 2000) for c in plan.get('cards', []) if c['type'] == 'learn']
                reward = body.get('reward', prev_rewards[-1] if prev_rewards else 2000)
                new_card = {
                    "id": _new_id('learn'),
                    "type": "learn",
                    "round": new_round,
                    "title": body.get('title', f'学习任务{new_round} · 100小时'),
                    "goal": body.get('goal', ''),
                    "reward": reward,
                    "start_time": _normalize_datetime_str(body.get('start_time', '')) or '',
                    "end_time": _normalize_datetime_str(body.get('end_time', '')) or '',
                    "status": "pending",
                    "milestones": [],
                    "tasks": [],
                    "slots": _create_empty_slots(100),
                    "notes": [],
                    "review": "",
                    "settlement": None,
                    "created_at": _now_str(),
                    "updated_at": _now_str()
                }
            else:
                # 默认继承上一张交易卡基础奖金 + 2000（不传 base_reward 时）
                prev_trades = [c for c in plan.get('cards', []) if c['type'] == 'trade']
                if prev_trades:
                    prev_base = prev_trades[-1].get('base_reward') or prev_trades[-1].get('reward', 2000)
                    default_base = prev_base + 2000
                else:
                    default_base = 2000
                base_reward = body.get('base_reward', default_base)
                hourly_rate = base_reward // 100
                new_card = {
                    "id": _new_id('trade'),
                    "type": "trade",
                    "round": new_round,
                    "title": body.get('title', f'第{new_round}轮 · 翻倍挑战'),
                    "goal": body.get('goal', ''),
                    "reward": base_reward,
                    "base_reward": base_reward,
                    "hourly_rate": hourly_rate,
                    "start_time": _normalize_datetime_str(body.get('start_time', '')) or '',
                    "end_time": _normalize_datetime_str(body.get('end_time', '')) or '',
                    "status": "pending",
                    "milestones": [],
                    "tasks": [],
                    "slots": _create_empty_slots(100),
                    "notes": [],
                    "review": "",
                    "settlement": None,
                    "created_at": _now_str(),
                    "updated_at": _now_str()
                }

            # 如果当前没有进行中的卡，新卡直接进入进行中（否则永远无法打卡）
            if not any(c.get('status') == 'in_progress' for c in plan.get('cards', [])):
                new_card['status'] = 'in_progress'
                if not new_card['start_time']:
                    new_card['start_time'] = _now_str()

            plan['cards'].append(new_card)
            _save_plans(session, data)
        return jsonify({"code": 200, "message": "创建成功", "data": new_card})
    except Exception as e:
        logger.error(f"[Plan] api_create_card 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/update-card', methods=['POST'])
def api_update_card():
    """更新任务卡（目标/状态/复盘/奖励等）"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        fields = body.get('fields', {})

        with session_scope() as session:
            # 局部读写：改卡字段只动本卡；放弃任务时解锁的下一张卡按列补写
            view = _load_card_for_write(session, plan_id, card_id)
            card = view['card'] if view else None
            if card is None:
                return jsonify({"code": 404, "message": "卡片不存在", "data": None})
            _normalize_write_plan(view)
            plan = view['plan']

            for key, value in fields.items():
                if key in ('milestones', 'todos', 'start_time', 'end_time', 'status', 'goal', 'reward', 'review', 'notes'):
                    # TodoList 只接受列表结构，防止脏数据写入
                    if key == 'todos' and not isinstance(value, list):
                        return jsonify({"code": 400, "message": "todos 字段必须为数组", "data": None})
                    # 开始/结束时间：校验并统一存储为 YYYY-MM-DD HH:MM:SS
                    if key in ('start_time', 'end_time'):
                        normalized = _normalize_datetime_str(value)
                        if normalized is None:
                            return jsonify({"code": 400, "message": "时间格式不正确，应为 YYYY-MM-DD HH:MM", "data": None})
                        value = normalized
                    card[key] = value

            # 交易卡修改奖励时同步基础奖金与时薪
            if card['type'] == 'trade' and 'reward' in fields:
                card['base_reward'] = fields['reward']
                card['hourly_rate'] = fields['reward'] // 100

            # 放弃任务时解锁下一张卡
            if fields.get('status') == 'abandoned' and card.get('status') == 'abandoned':
                _unlock_next_card(plan, card)

            card['updated_at'] = _now_str()
            _save_view_writes(session, view)
            return jsonify({"code": 200, "message": "更新成功", "data": card})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/delete-card', methods=['POST'])
def api_delete_card():
    """删除任务卡"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')

        with session_scope() as session:
            data = _load_plans_for_write(session, plan_id)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                plan['cards'] = [c for c in plan['cards'] if c['id'] != card_id]
                _save_plans(session, data)
                return jsonify({"code": 200, "message": "删除成功", "data": None})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 任务树节点操作（任务管理 v2）
# -----------------------------------------------------------------------------
# 卡片进入结算（completed/failed/abandoned）后任务树只读；编辑仅限
# pending/in_progress。全部接口遵循"局部读→校验→变更→局部保存→返回 refresh
# 载荷"，一切错误返回都发生在任何内存变更之前（session_scope 正常退出即
# commit）。
# =============================================================================

_TASK_EDITABLE_STATUS = ('pending', 'in_progress')


def _find_plan_card(data, plan_id, card_id):
    """从整树数据中定位 (plan, card)；未找到返回 (None, None)"""
    for plan in data.get('plans', []):
        if plan.get('id') != plan_id:
            continue
        for card in plan.get('cards', []):
            if card.get('id') == card_id:
                return plan, card
    return None, None


def _view_tree(view):
    """把单卡局部写视图包成整树定位函数认识的结构；计划不存在时等价于空树。

    只用于任务树写入口复用 _find_plan_card / _load_editable_card 的定位与
    校验文案，不产生新的数据面：view['plan']['cards'] 里目标卡是完整数据，
    其余卡是窄投影（定位与状态校验只用 id/status）。
    """
    return {'plans': [view['plan']]} if view and view.get('plan') else {'plans': []}


def _load_editable_card(data, plan_id, card_id):
    """定位可编辑卡片；成功返回 ((plan, card), None)，失败返回 (None, 错误响应)"""
    plan, card = _find_plan_card(data, plan_id, card_id)
    if not card:
        return None, jsonify({"code": 404, "message": "卡片不存在", "data": None})
    if card.get('status') not in _TASK_EDITABLE_STATUS:
        return None, jsonify({"code": 403, "message": "卡片已结束，任务树只读", "data": None})
    return (plan, card), None


def _task_save_response(session, view, plan, card):
    """任务树变更统一收尾：局部保存 + 返回任务树与局部刷新载荷

    任务树只长在本卡（card.tasks）与关联打卡的 task_links 上，改动范围天然是
    单卡；兄弟卡仅在同一次调用里被结算/解锁逻辑顺带碰到（本路径不会）。
    """
    card['updated_at'] = _now_str()
    _save_view_writes(session, view)
    return jsonify({"code": 200, "message": "success", "data": {
        "tasks": _annotate_tasks(card),
        "task_progress": _calc_task_progress(card),
        "tasks_all_done": _calc_tasks_all_done(card),
        "refresh": _build_refresh_payload(plan, card)
    }})


@plan_bp.route('/plan/api/task-add', methods=['POST'])
def api_task_add():
    """新增任务节点

    参数: plan_id, card_id, title, estimated_minutes(>0), parent_id(可选)
    挂到 parent_id 下（该父任务变为容器，预估自动=Σ叶子）；parent_id 为空则
    追加到顶层末尾。新增节点一律为"待开始"。
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        title = str(body.get('title') or '').strip()
        parent_id = str(body.get('parent_id') or '').strip()
        try:
            estimated = int(body.get('estimated_minutes') or 0)
        except (TypeError, ValueError):
            estimated = 0

        if not title:
            return jsonify({"code": 400, "message": "任务名称不能为空", "data": None})
        if estimated <= 0:
            return jsonify({"code": 400, "message": "请填写预估时长（分钟，需大于 0）", "data": None})

        with session_scope() as session:
            view = _load_card_for_write(session, plan_id, card_id)
            if view is not None:
                _normalize_write_plan(view)
            found_card, err_resp = _load_editable_card(_view_tree(view), plan_id, card_id)
            if err_resp:
                return err_resp
            plan, card = found_card

            tasks = card.get('tasks') or []
            if parent_id:
                parent_found = _find_task(tasks, parent_id)
                if not parent_found:
                    return jsonify({"code": 400, "message": "父任务不存在", "data": None})
                siblings = parent_found[0].setdefault('children', [])
            else:
                siblings = tasks
            siblings.append({
                'id': _new_id('task'),
                'title': title,
                'estimated_minutes': estimated,
                'status': 'todo',
                'children': [],
                'created_at': _now_str(),
            })
            card['tasks'] = tasks
            _recompute_task_estimates(tasks)
            _rollup_tasks(tasks)
            return _task_save_response(session, view, plan, card)
    except Exception as e:
        logger.error(f"[Plan] api_task_add 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/task-update', methods=['POST'])
def api_task_update():
    """更新任务节点：重命名 / 改预估（仅叶子）/ 移动父级 / 同级上移下移

    参数: plan_id, card_id, task_id,
          title(可选), estimated_minutes(可选，仅叶子),
          parent_id(可选；键存在即视为移动请求，空值=移到顶层),
          move(可选, 'up'|'down')
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        task_id = body.get('task_id', '')
        move = body.get('move') or ''

        # 先完成全部参数校验，再统一变更（session_scope 正常退出即 commit，
        # 必须避免"校验失败但已发生部分变更"）
        title = None
        if 'title' in body:
            title = str(body.get('title') or '').strip()
            if not title:
                return jsonify({"code": 400, "message": "任务名称不能为空", "data": None})
        estimated = None
        if 'estimated_minutes' in body:
            try:
                estimated = int(body.get('estimated_minutes') or 0)
            except (TypeError, ValueError):
                estimated = 0
            if estimated <= 0:
                return jsonify({"code": 400, "message": "请填写预估时长（分钟，需大于 0）", "data": None})

        with session_scope() as session:
            view = _load_card_for_write(session, plan_id, card_id)
            if view is not None:
                _normalize_write_plan(view)
            found_card, err_resp = _load_editable_card(_view_tree(view), plan_id, card_id)
            if err_resp:
                return err_resp
            plan, card = found_card

            tasks = card.get('tasks') or []
            task_found = _find_task(tasks, task_id)
            if not task_found:
                return jsonify({"code": 400, "message": "任务不存在", "data": None})
            node, siblings, parent = task_found

            if estimated is not None and _task_is_container(node):
                return jsonify({"code": 400, "message": "父任务预估由子任务自动累加，不能直接修改", "data": None})

            # 移动目标解析（防成环：目标不能是自身或其子孙）
            target_children = None
            if 'parent_id' in body:
                target_id = str(body.get('parent_id') or '').strip()
                if target_id == task_id:
                    return jsonify({"code": 400, "message": "不能移动到自身", "data": None})
                if target_id and _find_task(node.get('children') or [], target_id):
                    return jsonify({"code": 400, "message": "不能移动到自己的子任务下", "data": None})
                if target_id:
                    target_found = _find_task(tasks, target_id)
                    if not target_found:
                        return jsonify({"code": 400, "message": "目标父任务不存在", "data": None})
                    target_children = target_found[0].setdefault('children', [])
                else:
                    target_children = tasks

            # ---- 校验通过，开始变更 ----
            if title is not None:
                node['title'] = title
            if estimated is not None:
                node['estimated_minutes'] = estimated
            if move in ('up', 'down'):
                idx = siblings.index(node)
                swap = idx - 1 if move == 'up' else idx + 1
                if 0 <= swap < len(siblings):
                    siblings[idx], siblings[swap] = siblings[swap], siblings[idx]
            # parent_id 指向当前父级自身时视为无移动（保持原顺序）
            if target_children is not None and target_children is not siblings:
                siblings.remove(node)
                target_children.append(node)

            _recompute_task_estimates(tasks)
            _rollup_tasks(tasks)
            return _task_save_response(session, view, plan, card)
    except Exception as e:
        logger.error(f"[Plan] api_task_update 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/task-delete', methods=['POST'])
def api_task_delete():
    """删除任务节点（连同其全部子任务）；已关联的打卡保留作为历史痕迹

    参数: plan_id, card_id, task_id
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        task_id = body.get('task_id', '')

        with session_scope() as session:
            view = _load_card_for_write(session, plan_id, card_id)
            if view is not None:
                _normalize_write_plan(view)
            found_card, err_resp = _load_editable_card(_view_tree(view), plan_id, card_id)
            if err_resp:
                return err_resp
            plan, card = found_card

            tasks = card.get('tasks') or []
            task_found = _find_task(tasks, task_id)
            if not task_found:
                return jsonify({"code": 400, "message": "任务不存在", "data": None})
            node, siblings, parent = task_found
            siblings.remove(node)
            _recompute_task_estimates(tasks)
            _rollup_tasks(tasks)
            return _task_save_response(session, view, plan, card)
    except Exception as e:
        logger.error(f"[Plan] api_task_delete 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/task-cancel-done', methods=['POST'])
def api_task_cancel_done():
    """取消任务完成：把完成依据打卡中的该任务改回"进行中"，任务状态回退

    容器任务（父任务）是子任务完成的上卷结果，不能单独取消——返回 400，
    随子任务的取消自动传导。参数: plan_id, card_id, task_id
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        task_id = body.get('task_id', '')

        with session_scope() as session:
            view = _load_card_for_write(session, plan_id, card_id)
            if view is not None:
                _normalize_write_plan(view)
            found_card, err_resp = _load_editable_card(_view_tree(view), plan_id, card_id)
            if err_resp:
                return err_resp
            plan, card = found_card

            tasks = card.get('tasks') or []
            task_found = _find_task(tasks, task_id)
            if not task_found:
                return jsonify({"code": 400, "message": "任务不存在", "data": None})
            node = task_found[0]
            if _task_is_container(node):
                return jsonify({"code": 400, "message": "父任务状态由子任务自动汇总，请对子任务操作", "data": None})
            if node.get('status') != 'done':
                return jsonify({"code": 400, "message": "该任务当前未完成，无需取消", "data": None})

            # 完成依据（唯一）：关联打卡中该任务 state 从 done 改回 doing
            for slot in card.get('slots', []):
                rec = slot.get('record')
                if not isinstance(rec, dict):
                    continue
                for link in rec.get('task_links') or []:
                    if link.get('task_id') == task_id and link.get('state') == 'done':
                        link['state'] = 'doing'
            # 任务本体回退（防御性兜底：done 必有完成依据，但不依赖它成立）
            node['status'] = 'doing'
            node.pop('completed_at', None)
            node.pop('completed_by_slot', None)
            _rollup_tasks(tasks)
            return _task_save_response(session, view, plan, card)
    except Exception as e:
        logger.error(f"[Plan] api_task_cancel_done 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/fill-slot', methods=['POST'])
def api_fill_slot():
    """勾选格子（填写小时记录）

    参数:
        plan_id, card_id, slot_index,
        record: 学习卡 {content, duration_minutes, task_links}；
                交易卡 {prediction, duration_minutes, actual,
                        market_analysis, action_advice, account_balance, task_links}
        task_links: [{task_id, state: doing|done}] 任务树关联；空数组=待关联
                    （开始时允许不选，结束时由前端引导补选；填了则严格校验）
        filled_at: 可选，自定义打卡时间（补录历史记录），格式 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        slot_index = body.get('slot_index')
        record = body.get('record') or {}
        custom_filled_at = _normalize_datetime_str(body.get('filled_at'))

        if slot_index is None or slot_index < 0 or slot_index >= 100:
            return jsonify({"code": 400, "message": "格子索引无效(0~99)", "data": None})

        # 验证自定义时间格式（统一规范化为 YYYY-MM-DD HH:MM:SS）
        if custom_filled_at is None:
            return jsonify({"code": 400, "message": "时间格式不正确，应为 YYYY-MM-DD HH:MM", "data": None})
        if body.get('fill_mode') == 'backfill' and not custom_filled_at:
            return jsonify({"code": 400, "message": "历史补录必须选择打卡时间", "data": None})

        with session_scope() as session:
            # 局部读写：只锁本计划、只读本计划投影 + 目标卡完整数据，
            # 写回也只碰目标格子、目标卡与被解锁的那张兄弟卡
            view = _load_card_for_write(session, plan_id, card_id)
            card = view['card'] if view else None
            if card is None:
                # 计划不存在 / 卡不属于该计划：与整树定位路径同样的 404 文案
                return jsonify({"code": 404, "message": "卡片不存在", "data": None})
            _normalize_write_plan(view)
            plan = view['plan']

            # 检查串行锁定：只有 in_progress 的卡才能勾选
            if card['status'] != 'in_progress':
                return jsonify({"code": 403, "message": "当前卡不可勾选（状态: " + card['status'] + "）", "data": None})

            slots = card.get('slots', [])
            if slot_index >= len(slots):
                return jsonify({"code": 400, "message": "格子索引超出范围", "data": None})

            slot = slots[slot_index]
            if slot.get('filled'):
                return jsonify({"code": 400, "message": "该格子已勾选", "data": None})

            # 交易卡：先过分析纪律闸门（先分析记录，然后才允许打卡），
            # 再按新规范字段结构化，与前端提交格式保持一致；
            # 学习卡记录同样按规范字段重建（content/duration_minutes/task_links）
            analysis = None
            if card['type'] == 'trade':
                allowed, analysis, block = _discipline_gate(session, custom_filled_at)
                if not allowed:
                    return _gate_block_response(block, _gate_message(block))
                record = _normalize_trade_record(record, analysis)
            else:
                record = {
                    'content': str(record.get('content', '') or ''),
                    'duration_minutes': record.get('duration_minutes', 0) or 0,
                    'task_links': _normalize_task_links(record.get('task_links')),
                }

            # 任务树关联校验（任务管理 v2）：不通过则不写入任何数据
            links = record.get('task_links') or []
            err = _validate_task_links(card.get('tasks') or [], links, slot_index)
            if err:
                return jsonify({"code": 400, "message": err, "data": None})

            # 填写记录（支持自定义时间补录，默认当前时间）
            slot['filled'] = True
            slot['filled_at'] = custom_filled_at if custom_filled_at else _now_str()
            slot['record'] = record

            # 任务树联动：标记完成/进行中 + 逐级上卷
            _apply_task_links(card.get('tasks') or [], links, slot_index, slot['filled_at'])

            # 交易回填后的自动计算
            if card['type'] == 'trade' and record.get('prediction') and record.get('actual'):
                record['hit'] = (record['prediction'] == record['actual'])

            card['updated_at'] = _now_str()

            # 检查是否满 100 格（学习/交易卡同口径自动完成；未满时不自动结算，
            # 倒计时到期由 settle-expired 惰性结算）
            filled_count = _calc_total_hours(slots)
            if filled_count >= 100 or _is_countdown_expired(plan, card):
                _settle_card(plan, card)

            _save_view_writes(session, view)
            return jsonify({"code": 200, "message": "✅ 勾选成功 · 欢迎回来，长期主义者 🌱", "data": {
                "slot": slot,
                "card_status": card['status'],
                "filled_count": filled_count,
                "refresh": _build_refresh_payload(plan, card)
            }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        logger.error(f"[Plan] api_fill_slot 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/update-slot', methods=['POST'])
def api_update_slot():
    """更新格子记录（如回填实际涨跌；也可修改任务关联 task_links）"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        slot_index = body.get('slot_index')
        record = body.get('record') or {}
        # 未传时间保留原值；显式改期必须为有效非空时间，不能默默退回今天。
        custom_filled_at = None
        if 'filled_at' in body:
            custom_filled_at = _normalize_datetime_str(body['filled_at'])
            if not custom_filled_at:
                return jsonify({"code": 400, "message": "请选择有效的打卡时间（YYYY-MM-DD HH:MM）", "data": None})
        if not isinstance(slot_index, int) or isinstance(slot_index, bool):
            return jsonify({"code": 400, "message": "格子索引无效", "data": None})

        with session_scope() as session:
            # 局部读写：见 fill-slot 同款注释（回填实际涨跌只改一个格子）
            view = _load_card_for_write(session, plan_id, card_id)
            card = view['card'] if view else None
            if card is None:
                return jsonify({"code": 404, "message": "卡片不存在", "data": None})
            _normalize_write_plan(view)
            plan = view['plan']

            slots = card.get('slots', [])
            if slot_index < 0 or slot_index >= len(slots):
                return jsonify({"code": 400, "message": "格子索引无效", "data": None})
            slot = slots[slot_index]
            if not slot.get('filled'):
                return jsonify({"code": 400, "message": "该格子未勾选，无法更新", "data": None})

            time_changed = custom_filled_at is not None and custom_filled_at != slot.get('filled_at')
            analysis = None
            if time_changed:
                if card['status'] != 'in_progress':
                    return jsonify({"code": 403, "message": "当前卡不可修改打卡时间（状态: " + card['status'] + "）", "data": None})
                if card['type'] == 'trade':
                    allowed, analysis, block = _discipline_gate(session, custom_filled_at)
                    if not allowed:
                        return _gate_block_response(block, _gate_message(block))

            prev = slot.get('record') or {}
            if custom_filled_at is not None:
                # 允许只改时间，不清空正文、余额、预测及任务关联。
                record = {**prev, **record}
            prev_links = _normalize_task_links(prev.get('task_links'))
            # 任务树关联（任务管理 v2）：表单未携带 task_links 键时沿用库内
            # 旧值（局部更新保护）；携带时严格校验（含差量回退）后应用
            raw_links = record.get('task_links') if 'task_links' in record else prev_links
            new_links = _normalize_task_links(raw_links)
            err = _validate_task_links(card.get('tasks') or [], new_links, slot_index)
            if err:
                return jsonify({"code": 400, "message": err, "data": None})
            record = dict(record)
            record['task_links'] = new_links

            # 交易卡记录按新规范字段结构化，与前端提交格式保持一致
            if card['type'] == 'trade':
                # 分析关联字段以库内既有值为准（表单不回传这些字段，
                # 避免“回填实际涨跌”时把打卡与分析记录的关联抹除）
                keep_analysis = {
                    'analysis_ids': prev.get('analysis_ids') or [],
                    'analysis_hour': prev.get('analysis_hour') or '',
                    'bypass_analysis': bool(prev.get('bypass_analysis')),
                }
                if time_changed:
                    # 新时刻只能采用服务端判定；纪律关闭时也不能沿用旧小时或客户端关联。
                    keep_analysis = analysis or {}
                slot['record'] = _normalize_trade_record(record, keep_analysis)
            elif slot['record'] is None:
                slot['record'] = record
            else:
                slot['record'].update(record)

            if time_changed:
                slot['filled_at'] = custom_filled_at

            # 任务树联动：差量回退（旧 done 且本次非 done 的关联）
            # + 应用新关联（done 写完成依据）+ 逐级上卷
            _apply_task_links(card.get('tasks') or [], new_links, slot_index,
                              slot.get('filled_at') or _now_str(), prev_links)

            # 重新计算命中
            r = slot['record']
            if card['type'] == 'trade' and r.get('prediction') and r.get('actual'):
                r['hit'] = (r['prediction'] == r['actual'])

            card['updated_at'] = _now_str()
            _save_view_writes(session, view)
            return jsonify({"code": 200, "message": "更新成功", "data": {
                "slot": slot,
                "refresh": _build_refresh_payload(plan, card)
            }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/unfill-slot', methods=['POST'])
def api_unfill_slot():
    """删除单个打卡记录（取消勾选格子）

    参数: plan_id, card_id, slot_index
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        slot_index = body.get('slot_index')

        if slot_index is None or slot_index < 0 or slot_index >= 100:
            return jsonify({"code": 400, "message": "格子索引无效(0~99)", "data": None})

        with session_scope() as session:
            # 局部读写：撤销一个格子只改这一格 + 本卡；若因此撤销结算，
            # 串行链重算牵连到的兄弟卡也只补写 status/start_time 等被改的列
            view = _load_card_for_write(session, plan_id, card_id)
            card = view['card'] if view else None
            if card is None:
                return jsonify({"code": 404, "message": "卡片不存在", "data": None})
            _normalize_write_plan(view)
            plan = view['plan']

            slots = card.get('slots', [])
            if slot_index >= len(slots):
                return jsonify({"code": 400, "message": "格子索引超出范围", "data": None})
            slot = slots[slot_index]
            if not slot.get('filled'):
                return jsonify({"code": 400, "message": "该格子未勾选，无需删除", "data": None})

            # 任务树回退（任务管理 v2）：删除打卡 → 本打卡标记完成的任务
            # 回退"进行中"（含上卷父任务传导）；旧关联需在清空 record 前取出
            old_links = _normalize_task_links((slot.get('record') or {}).get('task_links'))
            _revert_slot_task_links(card.get('tasks') or [], old_links, slot_index)

            slot['filled'] = False
            slot['filled_at'] = ''
            slot['record'] = None

            # 若已结算且不满足通关条件（不足 100 小时且任务树未全部完成），撤销结算并重算串行链
            if card.get('settlement') and _calc_total_hours(slots) < 100 and not _calc_tasks_all_done(card):
                card['settlement'] = None
                card['status'] = 'pending'
                _recompute_serial_chain(plan)

            card['updated_at'] = _now_str()
            _save_view_writes(session, view)
            return jsonify({"code": 200, "message": "删除成功", "data": {
                "card_status": card['status'],
                "filled_count": _calc_total_hours(slots),
                "refresh": _build_refresh_payload(plan, card)
            }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        logger.error(f"[Plan] api_unfill_slot 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/reset-card', methods=['POST'])
def api_reset_card():
    """重置任务卡：清空所有打卡记录、小记、目标拆解/任务树、结算，恢复到初始状态

    参数: plan_id, card_id
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')

        with session_scope() as session:
            # 局部读写：重置只清空本卡的过程数据（格子值 + 卡字段），
            # 串行链重算牵连的兄弟卡由 save_sibling_changes 按列差量补写
            view = _load_card_for_write(session, plan_id, card_id)
            card = view['card'] if view else None
            if card is None:
                return jsonify({"code": 404, "message": "卡片不存在", "data": None})
            _normalize_write_plan(view)
            plan = view['plan']

            # 保留配置（标题/目标/奖励），清空全部过程数据
            card['slots'] = _create_empty_slots(100)
            card['milestones'] = []
            card['todos'] = []
            card['tasks'] = []
            card['notes'] = []
            card['review'] = ''
            card['settlement'] = None
            card['start_time'] = ''
            card['end_time'] = ''
            card['status'] = 'pending'
            # 重算串行链（若前面卡都已结束，本卡自动恢复为进行中）
            _recompute_serial_chain(plan)
            card['updated_at'] = _now_str()
            _save_view_writes(session, view)
            return jsonify({"code": 200, "message": "重置成功", "data": card})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        logger.error(f"[Plan] api_reset_card 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/add-note', methods=['POST'])
def api_add_note():
    """添加过程小记"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        content = body.get('content', '')

        if not content:
            return jsonify({"code": 400, "message": "内容不能为空", "data": None})

        with session_scope() as session:
            # 局部读写：加一条小记只改本卡 notes 一列，格子一个都不动
            view = _load_card_for_write(session, plan_id, card_id)
            card = view['card'] if view else None
            if card is None:
                return jsonify({"code": 404, "message": "卡片不存在", "data": None})
            _normalize_write_plan(view)

            note = {
                "time": _now_str(),
                "content": content
            }
            card['notes'].append(note)
            card['updated_at'] = _now_str()
            _save_view_writes(session, view)
            return jsonify({"code": 200, "message": "添加成功", "data": note})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/toggle-milestone', methods=['POST'])
def api_toggle_milestone():
    """切换子目标完成状态"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        milestone_index = body.get('milestone_index')

        with session_scope() as session:
            # 局部读写：切一个子目标只改本卡 milestones 一列
            view = _load_card_for_write(session, plan_id, card_id)
            card = view['card'] if view else None
            if card is None:
                return jsonify({"code": 404, "message": "卡片不存在", "data": None})
            _normalize_write_plan(view)

            milestones = card.get('milestones', [])
            if milestone_index < 0 or milestone_index >= len(milestones):
                return jsonify({"code": 400, "message": "子目标索引无效", "data": None})
            milestones[milestone_index]['done'] = not milestones[milestone_index]['done']
            card['updated_at'] = _now_str()
            _save_view_writes(session, view)
            # 返回任务树完成状态与提前通关资格，供前端引导结算
            all_done = _calc_tasks_all_done(card)
            early_eligible = _calc_early_eligible(card)
            return jsonify({"code": 200, "message": "切换成功", "data": {
                "milestones": card['milestones'],
                "all_done": all_done,
                "early_eligible": early_eligible,
                "card_status": card['status'],
                "filled_count": _calc_total_hours(card.get('slots', []))
            }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/settle-round', methods=['POST'])
def api_settle_round():
    """手动触发轮次结算（学习/交易卡同口径：满 100 小时或任务树全部完成才允许；
    未达标时只能等倒计时到期自动结算，防止误触提前判死）"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')

        with session_scope() as session:
            # 局部读写：结算改本卡 status/settlement，并解锁下一张卡
            view = _load_card_for_write(session, plan_id, card_id)
            card = view['card'] if view else None
            if card is None:
                return jsonify({"code": 404, "message": "卡片不存在", "data": None})
            _normalize_write_plan(view)
            plan = view['plan']

            if card['status'] == 'completed' or card['status'] == 'failed':
                return jsonify({"code": 400, "message": "已结算，无需重复结算", "data": card.get('settlement')})

            # 完成标准学习/交易卡统一：满 100 小时 或 任务树全部完成才允许手动结算。
            # 8% 增长线只是交易卡每日达标评级，不是通关标准；未达标时倒计时到期
            # 由 settle-expired 自动结算，不允许提前手动判死（2026-09-27 纠正：
            # 旧版交易卡按余额增长判定，第 3 轮刚亏 7% 就被手动结算成 failed）
            if _calc_total_hours(card.get('slots', [])) < 100 and not _calc_tasks_all_done(card):
                return jsonify({"code": 400, "message": "未满 100 小时且任务树未全部完成，暂不能结算（倒计时到期将自动结算）", "data": None})

            _settle_card(plan, card)
            _save_view_writes(session, view)
            return jsonify({"code": 200, "message": "结算成功", "data": card})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/settle-expired', methods=['POST'])
def api_settle_expired():
    """惰性落库"10 天倒计时"到期结算（第三个结束条件）。

    GET 读接口一律不写库（见 _load_plans 锁超时说明），故到期结算改由前端在拿到
    列表后、发现某卡 reward_info.countdown_expired 为真时发起本 POST：走加锁的
    _load_plans_for_write 全树写路径结算并解锁下一张，返回最新计划结构与被结算的卡。
    可选 body.plan_id 限定单个计划；缺省扫描全部计划。幂等：已结算卡不再处理。
    """
    try:
        body = request.get_json(silent=True) or {}
        plan_id = (body.get('plan_id') or '').strip()
        with session_scope() as session:
            data = _load_plans_for_write(session, plan_id)
            targets = [p for p in data.get('plans', []) if (not plan_id or p['id'] == plan_id)]
            settled = []
            for plan in targets:
                for cid in _apply_countdown_settlements(plan):
                    settled.append({'plan_id': plan['id'], 'card_id': cid})
            if settled:
                _save_plans(session, data)
            refreshed = [_build_plan_info(p) for p in targets]
        msg = (f"已结算 {len(settled)} 张到期卡" if settled else "无到期卡")
        return jsonify({"code": 200, "message": msg,
                        "data": {"settled": settled, "plans": refreshed}})
    except Exception as e:
        logger.error(f"[Plan] api_settle_expired 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/account-balance', methods=['GET', 'POST'])
def api_account_balance():
    """获取当前 OKX 账户总权益（USDT），供交易打卡时自动填入

    后端代理调用 OKX /api/v5/account/balance（密钥仅存服务端，不暴露给前端）：
    默认读取主账号凭证，可通过 account 参数指定其他账号。
    返回: { totalEq: '12345.67', available: '8901.23', updateTime: '2026-08-22 12:00:00' }
    查询失败时返回 code=500 并附带错误信息，前端可降级为空值。
    """
    try:
        body = (request.get_json(silent=True) or {}) if request.method == 'POST' else {}
        acct = str(body.get('account') or request.args.get('account') or '').strip()

        from api_config import get_api_config
        config = get_api_config(acct if acct else None)
        import okx.Account as Account
        account_api = Account.AccountAPI(
            config['api_key'], config['secret_key'],
            config['passphrase'], False, config['flag']
        )
        # 只读接口统一限频/退避（问题#8）：复用 api_routes 的节流出口，
        # 不另写一份；延迟导入避开蓝图互相依赖，导入失败则直连原接口
        try:
            from .api_routes import _rl_call as _rl
        except ImportError:
            _rl = None
        result = (_rl(account_api, 'get_account_balance') if _rl
                  else account_api.get_account_balance())
        if result.get('code') != '0' or not result.get('data'):
            return jsonify({
                'code': 500,
                'message': result.get('msg', 'OKX API 查询失败'),
                'data': None
            })
        acct_data = result['data'][0]
        total_eq_raw = acct_data.get('totalEq', '0')

        # 查询成功后追加余额快照（与 /api/account/balance-history 一致），
        # 为「历史补录」的范围余额数据源持续积累本地快照
        try:
            from .api_routes import _append_balance_snapshot
            now_ms = int(datetime.datetime.now().timestamp() * 1000)
            _append_balance_snapshot(config['account'], now_ms, round(float(total_eq_raw or 0), 4))
        except Exception as snap_err:
            logger.warning(f"[Plan] 追加余额快照失败（不影响余额返回）: {snap_err}")

        return jsonify({
            'code': 200,
            'message': 'success',
            'data': {
                'totalEq': total_eq_raw,
                'available': acct_data.get('adjEqy', '0'),
                'updateTime': _ts_to_str(acct_data.get('uTime', '0')),
                'account': config['account'],
                'account_name': config.get('account_name', config['account'])
            }
        })
    except Exception as e:
        logger.error(f"[Plan] api_account_balance 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@plan_bp.route('/plan/api/backfill-balances', methods=['GET'])
def api_backfill_balances():
    """历史补录数据源：按日期范围返回本地余额快照的每日余额点

    仅查询本地 balance_history 快照表，不实时调用 OKX 接口，响应快且无签名风险。
    参数: start=YYYY-MM-DD, end=YYYY-MM-DD（必填，范围 ≤62 天）
    返回: days: [{date, balance, source}]，每天一条（取当日最后一个快照点），
          当日无快照时 balance 为 null。
    """
    try:
        start = (request.args.get('start') or '').strip()
        end = (request.args.get('end') or '').strip()
        try:
            d_start = datetime.datetime.strptime(start, '%Y-%m-%d')
            d_end = datetime.datetime.strptime(end, '%Y-%m-%d')
        except ValueError:
            return jsonify({'code': 400, 'message': '日期格式不正确，应为 YYYY-MM-DD', 'data': None})
        if d_start > d_end:
            return jsonify({'code': 400, 'message': '开始日期不能晚于结束日期', 'data': None})
        if (d_end - d_start).days > 62:
            return jsonify({'code': 400, 'message': '单次补录范围不能超过 62 天', 'data': None})

        from api_config import get_api_config
        from . import balance_repo
        config = get_api_config()
        account_key = config['account']

        start_ms = int(d_start.timestamp() * 1000)
        end_ms = int((d_end + datetime.timedelta(days=1)).timestamp() * 1000)

        with session_scope() as session:
            points = balance_repo.load_account_points(session, account_key,
                                                      start_ms=start_ms, end_ms=end_ms)

        # 按日聚合：每天取时间戳最晚的一个快照点
        daily = {}
        for p in points:
            try:
                ts = int(p.get('ts') or 0)
            except (TypeError, ValueError):
                continue
            if ts < start_ms or ts >= end_ms:
                continue
            date_str = datetime.datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d')
            prev = daily.get(date_str)
            if prev is None or ts > prev['ts']:
                daily[date_str] = {'ts': ts, 'balance': p.get('balance'), 'source': p.get('source') or 'snapshot'}

        # 逐日生成结果（无快照的日期 balance=None，前端提示「无余额数据」）
        days = []
        cursor = d_start
        while cursor <= d_end:
            date_str = cursor.strftime('%Y-%m-%d')
            p = daily.get(date_str)
            balance_val = None
            if p and p.get('balance') is not None:
                try:
                    balance_val = round(float(p['balance']), 2)
                except (TypeError, ValueError):
                    balance_val = None
            days.append({
                'date': date_str,
                'balance': balance_val,
                'source': p['source'] if p else None
            })
            cursor += datetime.timedelta(days=1)

        return jsonify({'code': 200, 'message': 'success', 'data': {
            'account': account_key,
            'account_name': config.get('account_name', account_key),
            'days': days
        }})
    except Exception as e:
        logger.error(f"[Plan] api_backfill_balances 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@plan_bp.route('/plan/api/backfill-batch', methods=['POST'])
def api_backfill_batch():
    """批量历史补录：将多条记录按时间先后依次填入空格子

    参数: plan_id, card_id,
          entries: [{filled_at: 'YYYY-MM-DD HH:MM', record: {...}}],
          task_links(可选): [{task_id, state}] 整批统一关联同一组任务，
                            状态固定"进行中"（历史补录只推进，不标完成）
    依次占用空格子（0→99），空格子不足时多余条目跳过并在 skipped 中返回；
    交易卡记录走 _normalize_trade_record 规范化字段，prediction+actual 齐全时自动计算 hit。

    分析纪律（批次11）：交易卡逐条按各自历史槽独立判定闸门。
    不合格的条目不写入，汇总到 blocked/blocked_slots 返回（而不是整批 403），
    前端据此引导“为这些小时生成回溯分析”后重提，避免补一次历史被打回几十次。
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        entries = body.get('entries') or []
        if not isinstance(entries, list) or not entries:
            return jsonify({"code": 400, "message": "补录列表不能为空", "data": None})
        if len(entries) > 100:
            return jsonify({"code": 400, "message": "单次补录不能超过 100 条", "data": None})

        # 任务树关联（任务管理 v2）：整批统一关联同一组任务，状态固定"进行中"
        batch_links = [{'task_id': x['task_id'], 'state': 'doing'}
                       for x in _normalize_task_links(body.get('task_links'))]

        # 逐条校验并规范化时间，随后按时间升序填入
        norm_entries = []
        for e in entries:
            if not isinstance(e, dict):
                return jsonify({"code": 400, "message": "补录条目格式不正确", "data": None})
            fa = _normalize_datetime_str(e.get('filled_at') or '')
            if not fa:
                return jsonify({"code": 400, "message": "补录时间格式不正确，应为 YYYY-MM-DD HH:MM", "data": None})
            norm_entries.append((fa, e.get('record') or {}))
        norm_entries.sort(key=lambda x: x[0])

        with session_scope() as session:
            # 局部读写：整批补录只改本卡的空格子（差量写，未动的格子零 SQL）
            view = _load_card_for_write(session, plan_id, card_id)
            card = view['card'] if view else None
            if card is None:
                return jsonify({"code": 404, "message": "卡片不存在", "data": None})
            _normalize_write_plan(view)
            plan = view['plan']

            # 与 fill-slot 保持一致的串行锁定校验
            if card['status'] != 'in_progress':
                return jsonify({"code": 403, "message": "当前卡不可补录（状态: " + card['status'] + "）", "data": None})

            # 任务树关联校验：整批同一组关联，任一项不合法则整批拒绝
            if batch_links:
                err = _validate_task_links(card.get('tasks') or [], batch_links, -1)
                if err:
                    return jsonify({"code": 400, "message": err, "data": None})

            slots = card.get('slots', [])
            free_indexes = [i for i, s in enumerate(slots) if not s.get('filled')]
            is_trade = card['type'] == 'trade'

            # 闸门开启时才逐条判定；同一小时槽只判一次（范围补录常多日同一时刻）
            gate_cfg = None
            if is_trade:
                try:
                    from . import discipline_repo as _disc
                    _cfg = _disc.load_config()
                    if _cfg.get('enabled', True):
                        gate_cfg = _cfg
                except Exception as e:
                    logger.warning(f'[Plan] 补录闸门不可用，按无闸门处理: {e}')
            gate_cache = {}

            filled = 0
            blocked = []
            for fa, record in norm_entries:
                if filled >= len(free_indexes):
                    break
                analysis = None
                if gate_cfg is not None:
                    slot_key = fa[:13]
                    if slot_key not in gate_cache:
                        gate_cache[slot_key] = _discipline_gate(session, fa, gate_cfg)
                    allowed, analysis, block = gate_cache[slot_key]
                    if not allowed:
                        blocked.append({
                            'filled_at': fa,
                            'hour_slot': slot_key,
                            'required': block.get('required'),
                            'actual': block.get('actual'),
                            'missing_coins': block.get('missing_coins') or [],
                        })
                        continue
                slot = slots[free_indexes[filled]]
                if is_trade:
                    record['task_links'] = batch_links
                    record = _normalize_trade_record(record, analysis)
                    if record.get('prediction') and record.get('actual'):
                        record['hit'] = (record['prediction'] == record['actual'])
                else:
                    record = {
                        'content': str(record.get('content', '') or ''),
                        'duration_minutes': record.get('duration_minutes', 0) or 0,
                        'task_links': batch_links,
                    }
                slot['filled'] = True
                slot['filled_at'] = fa
                slot['record'] = record
                filled += 1

            # 全部条目都被闸门挡住：整批 403，直接引导去补回溯分析
            if blocked and filled == 0:
                blocked_slots = sorted({b['hour_slot'] for b in blocked})
                return jsonify({"code": 403, "message": (
                    f"📝 先分析，再打卡：{len(blocked)} 条补录全部被拦（缺 {len(blocked_slots)} 个小时的分析记录）"),
                    "data": {
                        "need_analysis": True,
                        "filled": 0,
                        "blocked": blocked,
                        "blocked_slots": blocked_slots,
                    }})

            # 任务树联动：整批关联统一推进为"进行中"
            if batch_links:
                _touch_task_links_doing(card.get('tasks') or [], batch_links)

            card['updated_at'] = _now_str()
            filled_count = _calc_total_hours(slots)
            # 学习/交易卡同口径：满 100 格或倒计时到期自动结算（与 fill-slot 一致）
            if filled_count >= 100 or _is_countdown_expired(plan, card):
                _settle_card(plan, card)
            _save_view_writes(session, view)
            blocked_slots = sorted({b['hour_slot'] for b in blocked})
            msg = f"补录成功，共 {filled} 条"
            if blocked:
                msg += f"；{len(blocked)} 条因缺分析记录被拦（涉 {len(blocked_slots)} 个小时）"
            return jsonify({"code": 200, "message": msg, "data": {
                "filled": filled,
                "skipped": len(norm_entries) - filled - len(blocked),
                "blocked": blocked,
                "blocked_slots": blocked_slots,
                "filled_count": filled_count,
                "card_status": card['status'],
                "refresh": _build_refresh_payload(plan, card)
            }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        logger.error(f"[Plan] api_backfill_batch 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/list-accounts', methods=['GET'])
def api_list_accounts():
    """获取可选 OKX 账号列表（供前端账号选择器使用）"""
    try:
        from api_config import list_accounts
        return jsonify({'code': 200, 'message': 'success', 'data': list_accounts()})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@plan_bp.route('/plan/api/today-status', methods=['GET'])
def api_today_status():
    """获取今日状态汇总（长期主义版：在场状态 + 累计在场天数，无罚款）

    只报告三件事：今天来了吗、来了多久、已经坚持了几天。
    绝不显示"缺口/达标/罚款"——那些只会带来心理负担。
    """
    try:
        result = []

        with session_scope() as session:
            data = _load_summary_plans(session)
            for plan in data.get('plans', []):
                result.append(_build_today_entry(plan))

        return jsonify({"code": 200, "message": "success", "data": result})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})
