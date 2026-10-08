#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析纪律（Analysis Discipline）- 判定与数据访问层（批次11）
==============================================================
把「分析记录」钉成「交易打卡」的准入凭证，并为每小时节拍提供统一的
合格判定口径。核心概念只有一个：

    小时槽 hour_slot = 'YYYY-MM-DD HH'，窗口 [HH:00:00, HH+1:00:00)
    与 task_analysis_records.ts 同口径（项目全链路东八区本地时钟）

【判定口径】
- 合格：该槽内分析记录数 >= required_count（默认 1 条）
- 覆盖收紧（可选）：require_cover_tracked=True 时还须覆盖交易配置全部跟踪币种
- 生效时段：只在 active_hours（默认 08:00-24:00）内要求与提醒。
  时段外的槽一律判 ok 且不追责——24 小时追责的必然结局是功能被整体关掉。
  右端是**开区间**：08:00-24:00 = 管 08:00 起每一小时到 23:59 为止（16 格）。

【source 语义】
服务端按 |now - ts| 自动判定 live / backfill，前端不可指定（防止为绕闸门
而伪造成"当时就分析了"）。闸门两者都接受（忘记录是人之常情，堵死只会让人
放弃整个功能），但看板把事后补记率作为诚实指标呈现——让作弊被看见，
比技术防作弊有意义。

【硬约束】本模块只读库、只判定，绝不调用 DualPeriodStrategyAdapter.analyze()
代生成分析记录：analyze() 会改模块级全局变量（FAST_MODE / PRINT_*）必须串行，
调度线程与实盘交易线程并发调用会互相污染；且代生成的记录里没有用户判断，
会把命中率统计稀释成噪声。提醒的目的是让人动手，不是替人动手。

【失败取向】闸门自身异常一律 fail-open（放行 + 记日志）。
纪律功能绝不能因为自己的 bug 拦住正常打卡。
"""

import copy
import json
import logging
import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, func, case

from .models import TaskAnalysisRecord, AnalysisReminderLog, PlanSlot, PlanCard
from . import config_store_repo

logger = logging.getLogger(__name__)

_TS_FMT = '%Y-%m-%d %H:%M:%S'
_SLOT_FMT = '%Y-%m-%d %H'

# kv_store 键名
KEY_DISCIPLINE_CONFIG = 'analysis_discipline_config'
KEY_DISCIPLINE_STATE = 'analysis_discipline_state'   # 一次性豁免等运行时状态

# 判定/看板单次回看的最大槽数（巡检幂等窗口，防止历史全表扫）
LOOKBACK_SLOTS = 24

# 生效时段右端是开区间，故 24:00 = 午夜，表示「管到 23:59」
DAY_END_MINUTE = 24 * 60
DEFAULT_ACTIVE_HOURS = '08:00-24:00'

DEFAULT_DISCIPLINE_CONFIG = {
    'enabled': True,                 # 纪律引擎总开关（关闭后闸门放行、巡检跳过；与 /task 页任务开关共同构成 module_enabled 总闸）
    'active_hours': DEFAULT_ACTIVE_HOURS,  # 生效时段；只在此区间内要求与提醒
    'required_count': 1,             # 每小时槽要求的分析记录条数
    'require_cover_tracked': False,  # 收紧：是否要求覆盖交易配置全部跟踪币种
    'grace_minutes': 15,             # 宽限期：槽结束后延后补记仍算合格，也是邮件触发时点
    'strict_mode': 'strict',         # strict 硬阻断 / soft 留痕放行 / off 关闭闸门
    'email': {
        'enabled': True,             # 邮件打扰总开关（关=缺口/汇总/日报全不发；台账与闸门照常）
        'on_gap': True,              # 缺口邮件
        'merge_after': 2,            # 连续 >=N 个缺口合并为一封断档汇总（防轰炸）
        'daily_report': '23:00',     # 每日纪律日报时刻；空串=不发
    },
    'browser': {
        'banner': True,              # 贴顶横幅
        'sound': True,               # Web Audio 短提示音
        'desktop_notify': True,      # 桌面通知（需用户授权）
        'poll_seconds': 60,          # 前端轮询周期
        'banner_after_minutes': 20,  # 槽内已过 N 分钟仍未分析才出横幅（避免整点即打扰）
    },
}

# 状态取值
ST_SATISFIED = 'satisfied'
ST_MISSING = 'missing'
ST_SATISFIED_LATER = 'satisfied_later'
ST_EXEMPT = 'exempt'


# =============================================================================
# 配置 / 运行时状态
# =============================================================================

def _deep_merge(base: dict, override: dict) -> dict:
    """默认配置深合并用户配置，保证新增字段自动补齐（与 alert_monitor 同款）"""
    merged = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_config(session=None) -> dict:
    """读取纪律配置；可复用请求会话，不跨请求缓存闸门配置。"""
    try:
        saved = (config_store_repo.load_json_config(session, KEY_DISCIPLINE_CONFIG)
                 if session is not None else
                 config_store_repo.load_json_config_cached(KEY_DISCIPLINE_CONFIG))
        return _deep_merge(DEFAULT_DISCIPLINE_CONFIG, saved or {})
    except Exception as e:
        logger.warning(f'[Discipline] 读取配置失败，使用默认配置: {e}')
        return copy.deepcopy(DEFAULT_DISCIPLINE_CONFIG)


def save_config(cfg: dict):
    """整份覆盖保存纪律配置（写穿透失效缓存）"""
    from .database import session_scope
    with session_scope() as s:
        config_store_repo.save_json_config(s, KEY_DISCIPLINE_CONFIG, cfg)


def module_enabled(cfg: dict = None) -> bool:
    """分析纪律功能总闸：kv 业务开关 × 定时任务统一开关
    （task_switches.analysis_discipline，含 /task 页面 kv 开关与 config.json 文件急停）。

    任一层关闭 → 整个模块静默：后台巡检不跑、打卡闸门不拦、
    discipline/status 与 analysis-gate 返回 disabled（前端角标/横幅/倒计时随之全部消失）。
    这样 /task 页关掉「分析纪律巡检」后不会再被前台倒计时打扰。

    开关层自身异常按"开"处理（fail-open，与 task_switch 口径一致）。
    """
    if cfg is None:
        cfg = load_config()
    if not bool(cfg.get('enabled', True)):
        return False
    try:
        from .task.scheduler import task_switch
        return bool(task_switch('analysis_discipline'))
    except Exception as e:
        logger.warning(f'[Discipline] 读取任务开关失败，按开启处理: {e}')
        return True


def load_state(session=None) -> dict:
    """读取运行时状态；可复用请求会话，异常返回空。"""
    try:
        if session is not None:
            state = config_store_repo.load_json_config(session, KEY_DISCIPLINE_STATE)
        else:
            from .database import session_scope
            with session_scope() as s:
                state = config_store_repo.load_json_config(s, KEY_DISCIPLINE_STATE)
        return state if isinstance(state, dict) else {}
    except Exception as e:
        logger.warning(f'[Discipline] 读取运行时状态失败: {e}')
        return {}


def save_state(state: dict):
    try:
        from .database import session_scope
        with session_scope() as s:
            config_store_repo.save_json_config(s, KEY_DISCIPLINE_STATE, state)
    except Exception as e:
        logger.warning(f'[Discipline] 保存运行时状态失败: {e}')


def set_exempt(hours: float) -> str:
    """设置一次性豁免：从现在起 hours 小时内的槽不追责、不提醒。

    存在的意义是给出"出差/不盯盘"的泄压阀——有豁免可用，
    才不会因为几天的特殊情况而把整个功能关掉。返回豁免截止时刻字符串。
    """
    hours = max(0.0, float(hours or 0))
    until = datetime.datetime.now() + datetime.timedelta(hours=hours)
    until_str = until.strftime(_TS_FMT)
    state = load_state()
    if hours <= 0:
        state.pop('exempt_until', None)
    else:
        state['exempt_until'] = until_str
    save_state(state)
    return until_str if hours > 0 else ''


def exempt_until(cfg: dict = None, state=None) -> Optional[datetime.datetime]:
    """当前豁免截止时刻；未豁免/已过期返回 None"""
    state = load_state() if state is None else state
    raw = str(state.get('exempt_until') or '').strip()
    if not raw:
        return None
    try:
        until = datetime.datetime.strptime(raw, _TS_FMT)
    except ValueError:
        return None
    return until if until > datetime.datetime.now() else None


def ensure_epoch() -> str:
    """首次启用时记录「纪律生效起点」，已存在则原值返回。

    存在的理由：巡检每轮回看 LOOKBACK_SLOTS 个槽，若无起点护栏，
    功能上线首轮会把上线之前的那些小时全判成缺档——用户一开启就
    看到满屏断档与一堆缺口邮件，这是拿系统的历史空白制造愧疚，
    只会让功能在第一天就被关掉。起点之前开始的槽一律不判定、不写台账。
    """
    state = load_state()
    if str(state.get('epoch') or '').strip():
        return str(state['epoch'])
    state['epoch'] = datetime.datetime.now().strftime(_TS_FMT)
    save_state(state)
    return state['epoch']


def epoch_dt() -> Optional[datetime.datetime]:
    """纪律生效起点；未记录/非法返回 None（= 不设限）"""
    raw = str((load_state() or {}).get('epoch') or '').strip()
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(raw[:19], _TS_FMT)
    except ValueError:
        return None


# =============================================================================
# 维护暂停租约
# =============================================================================
# 存在的理由：HTTP 层冒烟必须往共享库里临时写试验配置（它测的就是配置存取
# 与巡检链路），而同时在跑的生产调度器会在同一个 DB 上读到它。实测已经因此
# 把“每小时要填 50 条”和凌晨 00~07 的睡觉时间写进真台账，并向生产收件箱
# 发出两封断档汇总信。本进程的 CRYPTO_NO_BACKGROUND 只能管住自己，管不住
# 另一个进程，所以需要一把跨进程可见的租约让巡检主动让位。
# 带过期时间：冒烟被强杀也不能把纪律永久停掉。
MAINTENANCE_DEFAULT_MINUTES = 10


def begin_maintenance(minutes: int = MAINTENANCE_DEFAULT_MINUTES, reason: str = '') -> str:
    """进入维护暂停，返回截止时刻字符串（巡检在此期间不判定、不发信）"""
    until = datetime.datetime.now() + datetime.timedelta(
        minutes=max(1, int(minutes or MAINTENANCE_DEFAULT_MINUTES)))
    until_str = until.strftime(_TS_FMT)
    state = load_state()
    state['maintenance_until'] = until_str
    state['maintenance_reason'] = str(reason or '')[:120]
    save_state(state)
    return until_str


def end_maintenance() -> bool:
    """退出维护暂停；本来就没在维护中返回 False"""
    state = load_state()
    if 'maintenance_until' not in state:
        return False
    state.pop('maintenance_until', None)
    state.pop('maintenance_reason', None)
    save_state(state)
    return True


def maintenance_until(state=None) -> Optional[datetime.datetime]:
    """维护截止时刻；未在维护/已过期返回 None（过期即自动失效，不依赖清理）"""
    state = load_state() if state is None else state
    raw = str(state.get('maintenance_until') or '').strip()
    if not raw:
        return None
    try:
        until = datetime.datetime.strptime(raw[:19], _TS_FMT)
    except ValueError:
        return None
    return until if until > datetime.datetime.now() else None


def maintenance_reason(state=None) -> str:
    """进入维护时留下的原因（只用于日志与状态展示）"""
    state = load_state() if state is None else state
    return str(state.get('maintenance_reason') or '').strip()


# =============================================================================
# 小时槽基础运算（判定 100% 以服务端 datetime.now() 为准，浏览器时间只用于展示）
# =============================================================================

def hour_slot_of(dt: datetime.datetime) -> str:
    """datetime → 'YYYY-MM-DD HH'"""
    return dt.strftime(_SLOT_FMT)


def slot_start(slot: str) -> datetime.datetime:
    """'YYYY-MM-DD HH' → 该槽起点 datetime（非法输入抛 ValueError）"""
    return datetime.datetime.strptime(slot, _SLOT_FMT)


def slot_end(slot: str) -> datetime.datetime:
    """该槽终点（= 下一槽起点，开区间边界）"""
    return slot_start(slot) + datetime.timedelta(hours=1)


def slot_from_str(text: str) -> str:
    """把 'YYYY-MM-DD HH:MM[:SS]' / 'YYYY-MM-DD HH' 归一为小时槽；非法返回 ''"""
    s = str(text or '').strip()
    if not s:
        return ''
    try:
        return hour_slot_of(datetime.datetime.strptime(s[:19], _TS_FMT))
    except ValueError:
        pass
    try:
        return hour_slot_of(datetime.datetime.strptime(s[:13], _SLOT_FMT))
    except ValueError:
        return ''


def parse_active_hours(text) -> Optional[Tuple[int, int]]:
    """'08:00-24:00' → (480, 1440) 分钟数；空/非法返回 None。

    右端为**开区间**：24:00 即午夜，所以「白天要求、夜里不管」的正写法是
    08:00-24:00。旧实现只认 0..23，把 24:00 判成非法，调用方一律当“未配置”
    处理，于是这一写看似最合理的填法反而变成全天追责——见 active_window。
    """
    s = str(text or '').strip()
    if not s or '-' not in s:
        return None
    left, _, right = s.partition('-')

    def _min(part, allow_midnight=False):
        part = part.strip()
        if ':' not in part:
            return None
        h, _, m = part.partition(':')
        try:
            h, m = int(h), int(m or 0)
        except ValueError:
            return None
        if not (0 <= h <= (24 if allow_midnight else 23) and 0 <= m <= 59):
            return None
        v = h * 60 + m
        return v if v <= DAY_END_MINUTE else None

    a, b = _min(left), _min(right, allow_midnight=True)
    if a is None or b is None or a == b:
        return None      # 是“没配”还是“写错”，由 active_window 区分
    return (a, b)


def active_window(cfg: dict) -> Optional[Tuple[int, int]]:
    """解析生效时段，把「未配置」与「写了但写错」分开处理。

    - 空/缺省 → None = 全天生效（这是使用者显式做的选择）
    - 写了但解析不出来 → 回退 DEFAULT_ACTIVE_HOURS 并记日志

    两者不能合成一个 None：原实现下 active_hours 只要不合法（包括把 23:59
    写成 24:00），is_slot_active 就会对每个槽返回 True，追责面从 16 小时
    静默放大到 24 小时——把用户睡觉的时间也算成缺档。手误应当缩小追责面。
    """
    raw = str((cfg or {}).get('active_hours') or '').strip()
    if not raw:
        return None
    win = parse_active_hours(raw)
    if win is None:
        logger.warning(f"[Discipline] active_hours={raw!r} 无法解析，"
                       f"按默认 {DEFAULT_ACTIVE_HOURS} 判定（而不是放开成全天）")
        return parse_active_hours(DEFAULT_ACTIVE_HOURS)
    return win


def is_slot_active(slot: str, cfg: dict) -> bool:
    """该小时槽是否落在生效时段内（时段外的槽不要求、不提醒、不追责）"""
    win = active_window(cfg)
    if win is None:
        return True
    start, end = win
    try:
        minute = int(slot[11:13]) * 60
    except (ValueError, IndexError):
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end     # 跨零点时段


def day_slots(date_str: str, cfg: dict) -> List[str]:
    """某日全部生效小时槽（升序）"""
    return [f'{date_str} {h:02d}' for h in range(24) if is_slot_active(f'{date_str} {h:02d}', cfg)]


def classify_source(ts_str: str, grace_minutes: int,
                    now: datetime.datetime = None) -> str:
    """服务端判定记录来源：|now - ts| <= 宽限期 → live，否则 backfill。

    ts 非法/为空时按 live 处理（此刻正在写入的记录）。
    """
    now = now or datetime.datetime.now()
    try:
        ts = datetime.datetime.strptime(str(ts_str or '').strip()[:19], _TS_FMT)
    except ValueError:
        return 'live'
    return 'live' if abs((now - ts).total_seconds()) <= max(0, int(grace_minutes)) * 60 else 'backfill'


def tracked_currencies(session=None) -> List[Dict]:
    """交易配置里的 currencies 数组原样（带周期/算法/取价/BOLL 等策略参数）。

    提醒邮件要算“当前策略行情”就得拿到这些参数——只用 instId 会退成默认
    5m/4H，与实盘不同源，发出来的方向也就不能当作参考。读取失败返回空列表。
    """
    try:
        if session is None:
            cfg = config_store_repo.load_live_strategy_config_cached()
        else:
            runtime = config_store_repo.load_json_config(
                session, config_store_repo.KEY_TRADING_RUNTIME) or {}
            account = runtime.get('account')
            cfg = config_store_repo.load_json_config(
                session, config_store_repo.strategy_config_key(account))
            if account and cfg is None:
                cfg = config_store_repo.load_json_config(session, config_store_repo.KEY_STRATEGY_CONFIG)
        return [c for c in (cfg or {}).get('currencies', [])
                if isinstance(c, dict) and str(c.get('instId') or '').strip()]
    except Exception as e:
        logger.warning(f'[Discipline] 读取交易配置失败: {e}')
        return []


def tracked_inst_ids(session=None) -> List[str]:
    """交易配置中当前跟踪的币种（require_cover_tracked 的覆盖基准）"""
    currencies = tracked_currencies() if session is None else tracked_currencies(session)
    return [str(c.get('instId') or '').strip() for c in currencies]


# =============================================================================
# 槽合格判定（闸门 / 巡检 / 看板共用同一口径）
# =============================================================================

def slot_records(session, slot: str) -> List[dict]:
    """取某小时槽内的全部分析记录（走 idx_tar_slot，ts 升序）"""
    rows = session.execute(
        select(TaskAnalysisRecord)
        .where(TaskAnalysisRecord.hour_slot == slot)
        .order_by(TaskAnalysisRecord.ts.asc(), TaskAnalysisRecord.id.asc())
    ).scalars().all()
    return [r.to_dict() for r in rows]


def evaluate_slot(session, slot: str, cfg: dict = None, tracked=None) -> dict:
    """判定单个小时槽是否合格。

    返回 dict：
        hour_slot / required / actual / ok / active / exempt /
        covered(已覆盖币种) / missing_coins(未覆盖币种) /
        live_count / backfill_count / records(明细)
    """
    cfg = cfg if cfg is not None else load_config()
    required = max(1, int(cfg.get('required_count', 1) or 1))
    active = is_slot_active(slot, cfg)
    records = slot_records(session, slot) if slot else []
    covered = sorted({str(r.get('inst_id') or '') for r in records if r.get('inst_id')})
    tracked = (tracked_inst_ids() if tracked is None else tracked) if cfg.get('require_cover_tracked') else []
    missing_coins = [c for c in tracked if c not in set(covered)]

    count_ok = len(records) >= required
    cover_ok = not missing_coins
    # 生效时段外一律合格：不追责是这套机制能长期活下来的前提
    ok = bool(count_ok and cover_ok) if active else True

    return {
        'hour_slot': slot,
        'required': required,
        'actual': len(records),
        'ok': ok,
        'active': active,
        'count_ok': count_ok,
        'cover_ok': cover_ok,
        'covered': covered,
        'missing_coins': missing_coins,
        'tracked': tracked,
        'live_count': sum(1 for r in records if (r.get('source') or 'live') == 'live'),
        'backfill_count': sum(1 for r in records if r.get('source') == 'backfill'),
        'records': records,
    }


def gate_check(session, target_dt: datetime.datetime, cfg: dict = None) -> dict:
    """打卡闸门判定（fill-slot / backfill-batch / analysis-gate 接口共用）。

    返回 dict：
        allowed     是否放行
        mode        strict / soft / off / disabled / exempt / inactive
        reason      不放行时的中文说明
        evaluation  evaluate_slot 的完整结果
    """
    cfg = cfg if cfg is not None else load_config()
    slot = hour_slot_of(target_dt)
    ev = evaluate_slot(session, slot, cfg)
    base = {'allowed': True, 'mode': 'ok', 'reason': '', 'hour_slot': slot, 'evaluation': ev}

    # 总闸（业务 kv enabled × 任务开关 task_switches）关闭：不做任何判定直接放行，
    # 打卡写入口 / analysis-gate / 弹窗状态条共用这一条静默路径
    if not module_enabled(cfg):
        base['mode'] = 'disabled'
        return base
    mode = str(cfg.get('strict_mode', 'strict') or 'strict').lower()
    if mode not in ('strict', 'soft'):
        base['mode'] = 'off'
        return base
    if not ev['active']:
        base['mode'] = 'inactive'
        return base
    until = exempt_until(cfg)
    if until is not None:
        base['mode'] = 'exempt'
        base['reason'] = f'已豁免至 {until.strftime(_TS_FMT)}'
        return base
    if ev['ok']:
        return base

    # 不合格：strict 硬阻断，soft 放行但要求调用方打 bypass 留痕
    detail = f"本小时（{slot}:00）分析记录 {ev['actual']} 条，要求 {ev['required']} 条"
    if ev['count_ok'] and not ev['cover_ok']:
        detail = f"本小时（{slot}:00）未覆盖币种：{'、'.join(ev['missing_coins'])}"
    if mode == 'soft':
        base.update({'allowed': True, 'mode': 'soft_bypass', 'reason': detail})
    else:
        base.update({'allowed': False, 'mode': 'strict_block', 'reason': detail})
    return base


def safe_gate_check(target_dt: datetime.datetime, cfg: dict = None) -> dict:
    """闸门判定的 fail-open 包装：自身异常一律放行，绝不因纪律功能拦住正常打卡"""
    try:
        from .database import session_scope
        with session_scope() as s:
            return gate_check(s, target_dt, cfg)
    except Exception as e:
        logger.error(f'[Discipline] 闸门判定异常，按放行处理: {e}', exc_info=True)
        return {'allowed': True, 'mode': 'error', 'reason': f'闸门判定异常已放行: {e}',
                'hour_slot': hour_slot_of(target_dt), 'evaluation': None}


def parse_dt(text: str, default: datetime.datetime = None) -> datetime.datetime:
    """解析 'YYYY-MM-DD HH:MM[:SS]'；非法时返回 default（缺省当前时间）"""
    s = str(text or '').strip().replace('T', ' ')
    for fmt in (_TS_FMT, '%Y-%m-%d %H:%M'):
        try:
            return datetime.datetime.strptime(s[:len(fmt) + 2], fmt)
        except ValueError:
            continue
    return default or datetime.datetime.now()


# =============================================================================
# 前端轮询状态（L1 角标 / L2 横幅 / L3 声音通知的唯一数据源）
# =============================================================================

def build_status(session, cfg: dict = None, now: datetime.datetime = None) -> dict:
    """构建 discipline/status 载荷：当小时进度 + 今日合规 + streak + 打扰开关。

    配置/状态/跟踪币种每请求仅取一次，当小时评估与今日汇总共用。
    """
    cfg = cfg if cfg is not None else load_config(session)
    state = load_state(session)
    tracked = tracked_inst_ids(session)
    now = now or datetime.datetime.now()
    slot = hour_slot_of(now)
    ev = evaluate_slot(session, slot, cfg, tracked=tracked)
    until = exempt_until(cfg, state=state)

    elapsed = int((now - slot_start(slot)).total_seconds() // 60)
    grace = max(0, int(cfg.get('grace_minutes', 15) or 0))
    banner_after = max(0, int((cfg.get('browser', {}) or {}).get('banner_after_minutes', 20) or 0))
    browser_cfg = cfg.get('browser', {}) or {}

    # 横幅触发：生效时段内、未合格、未豁免、且槽内已过 banner_after 分钟
    # （给每小时开头留出自然的分析窗口，避免整点即打扰）
    banner = bool(browser_cfg.get('banner', True) and ev['active'] and not ev['ok']
                  and until is None and elapsed >= banner_after)

    today = now.strftime('%Y-%m-%d')
    mu = maintenance_until(state=state)
    # enabled 走总闸口径（业务 kv × 任务开关）：/task 页关掉巡检后，
    # discipline.js 轮询到此值即摘掉角标/横幅/胶囊并停止倒计时（彻底静默）
    return {
        'enabled': module_enabled(cfg),
        'strict_mode': str(cfg.get('strict_mode', 'strict')),
        'now': now.strftime(_TS_FMT),
        'hour_slot': slot,
        'active': ev['active'],
        'required': ev['required'],
        'actual': ev['actual'],
        'ok': ev['ok'],
        'elapsed_minutes': elapsed,
        'grace_minutes': grace,
        'banner_after_minutes': banner_after,
        'banner': banner,
        'missing_coins': ev['missing_coins'],
        'covered': ev['covered'],
        'backfill_count': ev['backfill_count'],
        'records': [{
            'id': r['id'], 'ts': r['ts'], 'inst_id': r['inst_id'],
            'user_judgment': r.get('user_judgment', ''),
            'user_reason': r.get('user_reason', ''),
            'source': r.get('source', 'live'),
        } for r in ev['records']],
        'exempt_until': until.strftime(_TS_FMT) if until else '',
        # 维护暂停期间不判定也不提醒：得把这个状态一并给出，否则前端
        # 只会看到一个不动的缺口，容易被当成漏判。
        'maintenance_until': mu.strftime(_TS_FMT) if mu else '',
        'maintenance_reason': maintenance_reason(state=state) if mu else '',
        'today': today_summary(session, today, cfg, now, current_evaluation=ev),
        'streak': streak_summary(session, today),
        'browser': {k: browser_cfg.get(k) for k in
                    ('banner', 'sound', 'desktop_notify', 'poll_seconds')},
        'tracked': tracked,
    }


def _log_rows_by_date(session, date_str: str) -> Dict[str, dict]:
    """某日台账行 → {hour_slot: row_dict}"""
    rows = session.execute(
        select(AnalysisReminderLog).where(AnalysisReminderLog.stat_date == date_str)
    ).scalars().all()
    return {r.hour_slot: r.to_dict() for r in rows}


def today_summary(session, date_str: str, cfg: dict,
                  now: datetime.datetime = None, current_evaluation=None) -> dict:
    """今日合规概览：已完结槽取台账，当前槽实时判定（台账可能还没写到）。

    台账未覆盖的历史槽一律跳过不计入分母：功能上线首日、巡检停机期间
    都会出现台账空洞，把它们当缺档计会把合规率显示成 0%，
    这是把系统的不足算到用户头上——数据要诚实，不能拿空洞制造愧疚。
    """
    now = now or datetime.datetime.now()
    cur_slot = hour_slot_of(now)
    logs = _log_rows_by_date(session, date_str)
    slots = [s for s in day_slots(date_str, cfg) if s <= cur_slot]

    ok_n = miss_n = later_n = exempt_n = pending_n = 0
    gaps = []
    for s in slots:
        row = logs.get(s)
        if row is None:
            # 台账未覆盖：当前槽实时判定（还在进行中，用户看得到才有用），历史槽跳过
            if s == cur_slot:
                ev = current_evaluation
                if ev is None or ev.get('hour_slot') != s:
                    ev = evaluate_slot(session, s, cfg)
                status = ST_SATISFIED if ev['ok'] else ST_MISSING
            else:
                pending_n += 1
                continue
        else:
            status = row['status']
        if status == ST_SATISFIED:
            ok_n += 1
        elif status == ST_SATISFIED_LATER:
            later_n += 1
            ok_n += 1
        elif status == ST_EXEMPT:
            exempt_n += 1
        else:
            miss_n += 1
            gaps.append(s)
    judged = ok_n + miss_n
    return {
        'date': date_str,
        'slots': len(slots),
        'ok': ok_n,
        'missing': miss_n,
        'satisfied_later': later_n,
        'exempt': exempt_n,
        'pending': pending_n,
        'rate': round(ok_n / judged * 100, 1) if judged else 0.0,
        'gaps': gaps,
    }


def streak_summary(session, today: str) -> dict:
    """连续全合规天数（当日无 missing 即计入；今日未过完则从昨天起算）"""
    rows = session.execute(
        select(AnalysisReminderLog.stat_date,
               func.max(case((AnalysisReminderLog.status == ST_MISSING, 1), else_=0)))
        .group_by(AnalysisReminderLog.stat_date)
        .order_by(AnalysisReminderLog.stat_date.asc())
    ).all()
    bad_days = {d for d, missing in rows if missing}
    all_days = [d for d, _ in rows]

    def _prev(day_str):
        return (datetime.datetime.strptime(day_str, '%Y-%m-%d')
                - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    # 今日若已有缺档则从今日起算（current=0），否则从昨天往前数
    start = today if today in bad_days else _prev(today)
    current = 0
    cursor = start
    while cursor in all_days and cursor not in bad_days:
        current += 1
        cursor = _prev(cursor)

    best = run = 0
    for d in all_days:
        if d in bad_days:
            run = 0
        else:
            run += 1
            best = max(best, run)
    return {'current': current, 'best': max(best, current)}


# =============================================================================
# 看板（反懈怠度量层）
# =============================================================================

def _attribution(session, start_date: str) -> dict:
    """归因对比：有分析支撑的打卡 vs 无分析打卡的命中率。

    判定"有分析支撑"：slot.analysis_ids 非空，或 filled_at 所在小时槽
    存在分析记录（后者让存量历史数据也能立即参与对比，不必等关联列攒够样本）。
    """
    start_slot = f'{start_date} 00'
    slots_with = {r[0] for r in session.execute(
        select(TaskAnalysisRecord.hour_slot)
        .where(TaskAnalysisRecord.hour_slot >= start_slot)
        .distinct()).all() if r[0]}

    rows = session.execute(
        select(PlanSlot.filled_at, PlanSlot.hit, PlanSlot.prediction,
               PlanSlot.actual, PlanSlot.analysis_ids)
        .join(PlanCard, PlanSlot.card_id == PlanCard.id)
        .where(PlanCard.type == 'trade', PlanSlot.filled == True,   # noqa: E712
               PlanSlot.filled_at >= start_date)
    ).all()

    buckets = {'with': {'total': 0, 'hit': 0}, 'without': {'total': 0, 'hit': 0}}
    for slot in rows:
        fa = str(slot.filled_at or '')
        if not fa or fa[:10] < start_date:
            continue
        hit = slot.hit
        if hit is None:
            if not (slot.prediction and slot.actual):
                continue
            hit = (slot.prediction == slot.actual)
        supported = bool((slot.analysis_ids or '').strip()) or (fa[:13] in slots_with)
        b = buckets['with' if supported else 'without']
        b['total'] += 1
        b['hit'] += 1 if hit else 0

    for b in buckets.values():
        b['rate'] = round(b['hit'] / b['total'] * 100, 1) if b['total'] else 0.0
    diff = None
    if buckets['with']['total'] and buckets['without']['total']:
        diff = round(buckets['with']['rate'] - buckets['without']['rate'], 1)
    return {'with_analysis': buckets['with'], 'without_analysis': buckets['without'],
            'rate_diff': diff}


def build_board(session, days: int = 30, cfg: dict = None) -> dict:
    """看板数据：合规率曲线 / 24×7 断档热力 / streak / 补记率 / 归因对比。

    全部指标只"如实呈现"，不做罚款与惩罚性展示——与任务计划模块
    长期主义 v1.1 的既有理念一致。
    """
    cfg = cfg if cfg is not None else load_config()
    days = max(1, min(int(days or 30), 180))
    today = datetime.datetime.now()
    start_date = (today - datetime.timedelta(days=days - 1)).strftime('%Y-%m-%d')

    logs = session.execute(
        select(AnalysisReminderLog)
        .where(AnalysisReminderLog.stat_date >= start_date)
        .order_by(AnalysisReminderLog.hour_slot.asc())
    ).scalars().all()

    daily_map: Dict[str, dict] = {}
    # 热力图：weekday(0=周一) × hour(0~23) 的缺档次数
    heat = [[0] * 24 for _ in range(7)]
    heat_total = [[0] * 24 for _ in range(7)]
    tot_ok = tot_miss = tot_later = tot_exempt = 0

    for r in logs:
        d = daily_map.setdefault(r.stat_date, {
            'date': r.stat_date, 'slots': 0, 'ok': 0, 'missing': 0,
            'satisfied_later': 0, 'exempt': 0, 'rate': 0.0})
        d['slots'] += 1
        try:
            st = slot_start(r.hour_slot)
        except ValueError:
            st = None
        if r.status == ST_SATISFIED:
            d['ok'] += 1
            tot_ok += 1
        elif r.status == ST_SATISFIED_LATER:
            d['satisfied_later'] += 1
            d['ok'] += 1
            tot_later += 1
            tot_ok += 1
        elif r.status == ST_EXEMPT:
            d['exempt'] += 1
            tot_exempt += 1
        else:
            d['missing'] += 1
            tot_miss += 1
            if st:
                heat[st.weekday()][st.hour] += 1
        if st and r.status != ST_EXEMPT:
            heat_total[st.weekday()][st.hour] += 1
        judged = d['ok'] + d['missing']
        d['rate'] = round(d['ok'] / judged * 100, 1) if judged else 0.0

    daily = [daily_map[k] for k in sorted(daily_map)]

    # 补记率（诚实指标）：统计区间内 backfill 记录占比
    start_slot = f'{start_date} 00'
    rec_total, rec_backfill = session.execute(
        select(func.count(), func.coalesce(func.sum(
            case((TaskAnalysisRecord.source == 'backfill', 1), else_=0)), 0))
        .select_from(TaskAnalysisRecord)
        .where(TaskAnalysisRecord.hour_slot >= start_slot)
    ).one()
    rec_total, rec_backfill = int(rec_total), int(rec_backfill)

    judged_total = tot_ok + tot_miss
    return {
        'days': days,
        'start_date': start_date,
        'end_date': today.strftime('%Y-%m-%d'),
        'config': cfg,
        'daily': daily,
        'heatmap': heat,
        'heatmap_total': heat_total,
        'summary': {
            'slots': judged_total + tot_exempt,
            'ok': tot_ok,
            'missing': tot_miss,
            'satisfied_later': tot_later,
            'exempt': tot_exempt,
            'rate': round(tot_ok / judged_total * 100, 1) if judged_total else 0.0,
        },
        'streak': streak_summary(session, today.strftime('%Y-%m-%d')),
        'backfill': {
            'total': rec_total,
            'backfill': rec_backfill,
            'rate': round(rec_backfill / rec_total * 100, 1) if rec_total else 0.0,
        },
        'attribution': _attribution(session, start_date),
    }


def gap_digest(session, days: int = 7, cfg: dict = None) -> dict:
    """断档画像：最近 N 天最长连续断档时段 + 高发小时（供日报邮件引用）

    必须按「当前生效时段 + 生效起点」复核台账行：巡检写行用的是当时的时段，
    时段一改窄，旧行就会把用户从来没被要求的时间算成断档（验证期间就现场
    产生过一行 23:00 缺档，当时时段被撑到 00:00-23:59）。拿口径变化
    去制造愧疚，是数据诚实里最不该犯的一种。
    """
    cfg = cfg if cfg is not None else load_config()
    epoch = epoch_dt()
    start_date = (datetime.datetime.now() - datetime.timedelta(days=days - 1)).strftime('%Y-%m-%d')
    rows = session.execute(
        select(AnalysisReminderLog)
        .where(AnalysisReminderLog.stat_date >= start_date,
               AnalysisReminderLog.status == ST_MISSING)
        .order_by(AnalysisReminderLog.hour_slot.asc())
    ).scalars().all()

    kept = []
    for r in rows:
        if not is_slot_active(r.hour_slot, cfg):
            continue                       # 现行时段不要求这一格，不计入断档
        try:
            begin = slot_start(r.hour_slot)
        except ValueError:
            continue
        if epoch is not None and begin < epoch:
            continue                       # 上线之前的槽：当时根本没这套规则，不追责
        kept.append(r)
    rows = kept

    runs, cur = [], []
    for r in rows:
        if cur:
            prev_end = slot_start(cur[-1].hour_slot) + datetime.timedelta(hours=1)
            if slot_start(r.hour_slot) != prev_end:
                runs.append(cur)
                cur = []
        cur.append(r)
    if cur:
        runs.append(cur)

    longest = max(runs, key=len) if runs else []
    hour_hits: Dict[int, int] = {}
    for r in rows:
        try:
            h = slot_start(r.hour_slot).hour
        except ValueError:
            continue
        hour_hits[h] = hour_hits.get(h, 0) + 1
    top_hours = sorted(hour_hits.items(), key=lambda x: (-x[1], x[0]))[:3]

    def _fmt(run):
        if not run:
            return ''
        s = slot_start(run[0].hour_slot)
        e = slot_start(run[-1].hour_slot) + datetime.timedelta(hours=1)
        return f'{s.strftime("%m-%d %H:%M")}–{e.strftime("%m-%d %H:%M")}（{len(run)} 小时）'

    return {
        'days': days,
        'gap_count': len(rows),
        'longest_gap': _fmt(longest),
        'top_hours': [{'hour': h, 'count': c} for h, c in top_hours],
    }


def missing_slots(session, since_date: str) -> List[dict]:
    """未发送缺口邮件的 missing 台账行（升序），供巡检合并发送"""
    rows = session.execute(
        select(AnalysisReminderLog)
        .where(AnalysisReminderLog.stat_date >= since_date,
               AnalysisReminderLog.status == ST_MISSING,
               AnalysisReminderLog.notified == False)   # noqa: E712
        .order_by(AnalysisReminderLog.hour_slot.asc())
    ).scalars().all()
    return [r.to_dict() for r in rows]
