#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLAlchemy 表模型定义（与 db_schema.sql 保持同步）
====================================================
按迁移批次增量扩充：
  批次1：meta + 随笔模块（journal_tags / journal_notes / note_tags）
  批次2：热量模块（calorie_* 4表）
  批次3：任务计划模块（plan_plans / plan_cards / plan_slots）
  批次4：账户余额历史（balance_history）
  批次5：交易运行时状态（trader_directions/reverse_guard/manual_pause/
                 tp_runtime_state/pos_book/pos_slot/pos_algo/pos_lev）
  批次6：结构化成交流水（trade_journal）
  批次7a：策略配置/币种自选整份存入 kv_store（无新表）
  批次7b：行情 CSV（crypto_coins / star_market）
  批次8：kv_cache 等
  批次9：监控告警历史（alert_log）
  批次10：定时任务实盘分析记录（task_analysis_records）
  批次11：分析纪律（task_analysis_records 增 source/hour_slot，
                 plan_slots 增分析关联列，新表 analysis_reminder_log）
  批次12：盘感模拟模块（instinct_corpus / instinct_wiki_rules /
                 instinct_predictions / instinct_embeddings，
                 详见 doc/RAG_LLM_Wiki模拟盘感落地方案.md）
  批次13：日记本模块（diary_settings / diary_entries / diary_goals）
  批次14：美食打卡点模块（calorie_food_spots / calorie_food_spot_photos，
                 挂在热量缺口页面下的探店记录，照片文件落盘 data/ 目录）
  批次15：每日多维打卡（daily_checkins / trading_daily_snapshots）

序列化约定：created_at / updated_at 在库中为 DATETIME，对外 API
序列化时统一转回 'YYYY-MM-DD HH:MM:SS' 字符串，保持与 JSON 版契约一致。
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    String, Text, DateTime, Boolean, Float, Double, Integer, BigInteger,
    ForeignKey, UniqueConstraint, Index, Date, CheckConstraint
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def _fmt_ts(ts) -> str:
    """DATETIME -> 'YYYY-MM-DD HH:MM:SS'（None 返回空串）"""
    return ts.strftime('%Y-%m-%d %H:%M:%S') if ts else ''


class Meta(Base):
    """元信息表：schema 版本 / 初始化时间等"""
    __tablename__ = 'meta'

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default='')
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class JournalTag(Base):
    """随笔标签字典（name 即主键，创建时间用于保持原 JSON 数组顺序）"""
    __tablename__ = 'journal_tags'

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default='#999')
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    def to_dict(self):
        return {'name': self.name, 'color': self.color}


class JournalNote(Base):
    """随笔 / 复盘条目（复盘四格拆为独立列，可按字段检索）"""
    __tablename__ = 'journal_notes'

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default='note')
    content: Mapped[str] = mapped_column(Text, nullable=False)
    review_subject: Mapped[str] = mapped_column(Text, nullable=True)
    review_decision: Mapped[str] = mapped_column(Text, nullable=True)
    review_outcome: Mapped[str] = mapped_column(Text, nullable=True)
    review_lesson: Mapped[str] = mapped_column(Text, nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    distilled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linked_from: Mapped[str] = mapped_column(String(32), nullable=False, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    def review_dict(self):
        """还原复盘四格结构；随笔（非 review 类型）返回 None，与 JSON 版一致"""
        if self.type != 'review':
            return None
        return {
            'subject': self.review_subject or '',
            'decision': self.review_decision or '',
            'outcome': self.review_outcome or '',
            'lesson': self.review_lesson or ''
        }

    def to_dict(self, tags=None):
        return {
            'id': self.id,
            'type': self.type,
            'content': self.content,
            'tags': list(tags or []),
            'review': self.review_dict(),
            'pinned': bool(self.pinned),
            'distilled': bool(self.distilled),
            'linked_from': self.linked_from or '',
            'created_at': _fmt_ts(self.created_at),
            'updated_at': _fmt_ts(self.updated_at)
        }


class NoteTag(Base):
    """笔记 ↔ 标签关联表（随主表级联删除：删笔记清关联、删标签摘引用）"""
    __tablename__ = 'note_tags'
    __table_args__ = (UniqueConstraint('note_id', 'tag_name', name='uk_note_tag'),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    note_id: Mapped[str] = mapped_column(
        ForeignKey('journal_notes.id', ondelete='CASCADE'), nullable=False, index=True)
    tag_name: Mapped[str] = mapped_column(
        ForeignKey('journal_tags.name', ondelete='CASCADE'), nullable=False, index=True)


# =============================================================================
# 批次2：热量缺口模块（calorie_food_db.json / calorie_records.json）
# =============================================================================

class CalorieFood(Base):
    """食物热量库（原 calorie_food_db.json -> foods[]）"""
    __tablename__ = 'calorie_foods'

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default='100克')
    calories: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default='其他')
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'unit': self.unit,
            'calories': self.calories,
            'category': self.category,
            'created_at': _fmt_ts(self.created_at)
        }


class CalorieRecord(Base):
    """每日热量记录（原 calorie_records.json -> records[]；id 即日期）"""
    __tablename__ = 'calorie_records'

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    morning_weight: Mapped[float] = mapped_column(Float, nullable=True)
    evening_weight: Mapped[float] = mapped_column(Float, nullable=True)
    bmr: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    breakfast_food: Mapped[str] = mapped_column(Text, nullable=False, default='')
    breakfast_calories: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    lunch_food: Mapped[str] = mapped_column(Text, nullable=False, default='')
    lunch_calories: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    dinner_food: Mapped[str] = mapped_column(Text, nullable=False, default='')
    dinner_calories: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    intake_deficit: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    daily_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exercise_calories: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    calorie_deficit: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    cumulative_deficit: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class CalorieMealItem(Base):
    """三餐食物明细（原记录内 breakfast_foods/lunch_foods/dinner_foods 数组）

    每条明细结构：{name, calories(单位热量), quantity(数量，支持小数), unit(单位快照)}
    该行总摄入 = calories × quantity；存量行 quantity 缺省 1，语义与原绝对热量兼容
    """
    __tablename__ = 'calorie_meal_items'
    __table_args__ = (UniqueConstraint('record_id', 'meal', 'position', name='uk_meal_item'),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(
        ForeignKey('calorie_records.id', ondelete='CASCADE'), nullable=False, index=True)
    meal: Mapped[str] = mapped_column(String(10), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    calories: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default='')


class CalorieConfig(Base):
    """热量模块配置（原 calorie_records.json 内嵌 config，单行表）"""
    __tablename__ = 'calorie_config'

    id: Mapped[int] = mapped_column(primary_key=True)
    height: Mapped[float] = mapped_column(Float, nullable=False, default=169)
    age: Mapped[float] = mapped_column(Float, nullable=False, default=29)
    step_frequency: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    weight_factor: Mapped[float] = mapped_column(Float, nullable=False, default=55)
    target_deficit: Mapped[float] = mapped_column(Float, nullable=False, default=100000)

    def to_dict(self):
        return {
            'height': self.height,
            'age': self.age,
            'step_frequency': self.step_frequency,
            'weight_factor': self.weight_factor,
            'target_deficit': self.target_deficit
        }


# =============================================================================
# 批次3：任务计划模块（task_plans.json → plan_plans / plan_cards / plan_slots）
# -----------------------------------------------------------------------------
# 语义约定：整树读写（load_plans_data / save_plans_data 与原 JSON 版
# _load_plans/_save_plans 契约一致）；时间字段为业务字符串直接存 String；
# daily_rule/round_config/milestones/todos/notes/settlement 等整体读写
# 的小结构存 JSON 文本列。
# =============================================================================

class PlanPlan(Base):
    """任务计划（原 task_plans.json -> plans[]）"""
    __tablename__ = 'plan_plans'

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default='custom')
    name: Mapped[str] = mapped_column(String(64), nullable=False, default='')
    grand_goal: Mapped[str] = mapped_column(Text, nullable=False, default='')
    total_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    round_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    per_round_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    daily_rule: Mapped[str] = mapped_column(Text, nullable=False, default='{}')
    round_config: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(19), nullable=False, default='')


class PlanCard(Base):
    """任务卡（原 plans[].cards[]；sort_order 保持原数组顺序）"""
    __tablename__ = 'plan_cards'

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey('plan_plans.id', ondelete='CASCADE'), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default='learn')
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(128), nullable=False, default='')
    goal: Mapped[str] = mapped_column(Text, nullable=False, default='')
    reward: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_reward: Mapped[int] = mapped_column(Integer, nullable=True)
    hourly_rate: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='pending')
    start_time: Mapped[str] = mapped_column(String(19), nullable=False, default='')
    end_time: Mapped[str] = mapped_column(String(19), nullable=False, default='')
    milestones: Mapped[str] = mapped_column(Text, nullable=False, default='[]')
    todos: Mapped[str] = mapped_column(Text, nullable=False, default='[]')
    # 任务树（任务管理 v2）：JSON 整树 [{id,title,estimated_minutes,status,
    # children:[...],completed_at,completed_by_slot,created_at}, ...]。
    # NULL/'' = 旧数据未迁移（加载时由 _migrate_tasks 从 milestones+todos 生成）；
    # '[]' = 已迁移的空树
    tasks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default='[]')
    review: Mapped[str] = mapped_column(Text, nullable=False, default='')
    settlement: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(19), nullable=False, default='')
    updated_at: Mapped[str] = mapped_column(String(19), nullable=False, default='')


class PlanSlot(Base):
    """打卡格子（原 cards[].slots[]；record 拆列存储，has_record 区分 None/{}）"""
    __tablename__ = 'plan_slots'

    card_id: Mapped[str] = mapped_column(
        ForeignKey('plan_cards.id', ondelete='CASCADE'), primary_key=True)
    slot_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    filled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    filled_at: Mapped[str] = mapped_column(String(19), nullable=False, default='')
    has_record: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 学习卡记录字段
    content: Mapped[str] = mapped_column(Text, nullable=False, default='')
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 交易卡记录字段
    prediction: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    actual: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    hit: Mapped[bool] = mapped_column(Boolean, nullable=True)
    market_analysis: Mapped[str] = mapped_column(Text, nullable=False, default='')
    action_advice: Mapped[str] = mapped_column(Text, nullable=False, default='')
    account_balance: Mapped[str] = mapped_column(String(32), nullable=False, default='')
    # 分析纪律（批次11）：本次打卡依据的分析记录，用于「有/无分析支撑」命中率归因
    analysis_ids: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    analysis_hour: Mapped[str] = mapped_column(String(13), nullable=False, default='')
    bypass_analysis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 任务树关联（任务管理 v2）：本次打卡关联的任务 [{task_id, state:doing|done}]
    # JSON 数组；'' / NULL / [] = 待关联（可随时补选）
    task_links: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# =============================================================================
# 批次4：账户余额历史（account_balance_history.json → balance_history）
# -----------------------------------------------------------------------------
# 只增时序快照：PK (account_key, ts) 天然对同 ts 去重（重查覆盖余额）；
# 回溯点 source='backfill' 与真实快照 snapshot 区分。
# =============================================================================

class BalanceHistory(Base):
    """账户余额快照点（ts 为 UTC 毫秒时间戳）"""
    __tablename__ = 'balance_history'

    account_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # 金额列必须用 Double：SQLAlchemy Float 在 MySQL 默认映射为单精度
    # FLOAT，会把 123.4567 舍入成 123.457
    balance: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default='snapshot')


# =============================================================================
# 批次5：交易运行时状态（定时任务调度器持久化状态，全部关系化）
# -----------------------------------------------------------------------------
# 来源文件：scheduler_state.json / reverse_guard_state.json /
#           manual_pause_state.json / tp_runtime_state.json /
#           position_order_state.json
# 语义约定：与原 JSON 版完全一致 —— 内存字典缓存 + 变更立即落库；
# 落库失败仅告警不中断交易（与 JSON 版容错行为一致）。
# 金额/价格/时间戳类小数列一律 Double（避免 FLOAT 单精度舍入）。
# =============================================================================

class TraderDirection(Base):
    """各币种长短周期方向记录（scheduler_state.json）

    每轮调度完成后整体重写，重启后恢复以防漏判长周期方向反转。
    """
    __tablename__ = 'trader_directions'

    inst_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    short_dir: Mapped[str] = mapped_column(String(8), nullable=False)
    long_dir: Mapped[str] = mapped_column(String(8), nullable=False)


class ReverseGuard(Base):
    """反向持仓风控计时器（reverse_guard_state.json）

    预警→强平倒计时跨重启不重置，避免反向持仓（程序旧方向残留仓/人工反向单）逃过强平时限。
    """
    __tablename__ = 'reverse_guard'

    inst_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    detected_ts: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    long_direction: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    reverse_side: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    reverse_mode: Mapped[str] = mapped_column(String(16), nullable=False, default='')
    reverse_amount: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    warned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ManualPause(Base):
    """人工强平冷却（manual_pause_state.json）：inst_id → 恢复自动开仓时间戳"""
    __tablename__ = 'manual_pause'

    inst_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    resume_ts: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)


class TpRuntimeState(Base):
    """止盈引擎运行时状态（tp_runtime_state.json）

    state_key 为 '{inst_id}:{long|short}'；ladder_done 为已触发分批档位 JSON 数组。
    extra 为组合出场(combo)等扩展状态 JSON（P0/ATR0/R0/protect/partial_done 等），
    空或 NULL 视为 {}。老库无该列时由 database._REQUIRED_COLUMNS 自动 ALTER 补齐。
    """
    __tablename__ = 'tp_runtime_state'

    state_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    entry_ts: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    peak: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    trough: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    avg_px: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    ladder_done: Mapped[str] = mapped_column(Text, nullable=False, default='[]')
    extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class PosBook(Base):
    """双仓位本地账本·篮子级（position_order_state.json 拆表）

    PK (inst_id, bucket)，bucket ∈ trend/range。held/avg_px 按方向分列。
    """
    __tablename__ = 'pos_book'

    inst_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(8), primary_key=True)
    held_long: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    held_short: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    avg_px_long: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    avg_px_short: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    prev_open_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prev_close_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_desired: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)


class PosSlot(Base):
    """双仓位本地账本·挂单槽位（position_order_state.json 拆表）

    PK (inst_id, bucket, slot)，slot ∈ entry/exit。
    状态机：IDLE → PENDING → FILLED / EXPIRED。
    """
    __tablename__ = 'pos_slot'

    inst_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(8), primary_key=True)
    slot: Mapped[str] = mapped_column(String(8), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default='IDLE')
    ord_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    price: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    amount: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    placed_ts: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    acc_filled: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    dir: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # 订单状态查询失败日志去重标记（持久化保持与 JSON 版 round-trip 一致）
    qfail_logged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PosAlgo(Base):
    """双仓位本地账本·交易所侧兜底委托记录（position_order_state.json 拆表）

    PK (inst_id, bucket, direction)；无记录即 algo=null（行不存在）。
    """
    __tablename__ = 'pos_algo'

    inst_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(8), primary_key=True)
    direction: Mapped[str] = mapped_column(String(8), primary_key=True)
    algo_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    amount: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    sl: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    tp: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    ts: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)


class PosLev(Base):
    """双仓位本地账本·杠杆设置缓存（position_order_state.json 拆表）

    行存在即 lev_set 为 dict（两列均空表示空 dict {}）；行不存在即 lev_set=None。
    """
    __tablename__ = 'pos_lev'

    inst_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    cross_lev: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    isolated_lev: Mapped[Optional[float]] = mapped_column(Double, nullable=True)


# =============================================================================
# 批次6：结构化成交流水（logs/trade_journal.jsonl → trade_journal）
# -----------------------------------------------------------------------------
# append-only 流水：自增 id 保留 JSONL 原文写入顺序（同一 ts 可能多条）；
# ts 保持 'YYYY-MM-DD HH:MM:SS' 业务字符串（读取端字符串闭区间筛选，
# 契约与 JSONL 版一致）；旁路记录，落库失败不阻断交易。
# =============================================================================

class TradeJournal(Base):
    """每笔实际成交的结构化记录（限价成交/市价平仓/跨仓位强平）"""
    __tablename__ = 'trade_journal'
    # 复合索引：按币种查时间段内流水（最高频查询）避免回表全扫
    __table_args__ = (Index('idx_tj_inst_ts', 'inst_id', 'ts'),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(19), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, default='')
    inst_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    bucket: Mapped[str] = mapped_column(String(16), nullable=False, default='')
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    action: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    price: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    amount: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    ord_id: Mapped[str] = mapped_column(String(40), nullable=False, default='')
    reason: Mapped[str] = mapped_column(Text, nullable=False, default='signal')

    def to_dict(self):
        """还原 JSONL 行记录形态（字段顺序与写入端一致）"""
        return {
            'ts': self.ts,
            'run_id': self.run_id or '',
            'inst_id': self.inst_id,
            'bucket': self.bucket,
            'direction': self.direction,
            'action': self.action,
            'price': self.price,
            'amount': self.amount,
            'ord_id': self.ord_id or '',
            'reason': self.reason or 'signal',
        }


# =============================================================================
# 通用 KV 存储（配置/缓存类整体读写型数据，后续批次使用）
# =============================================================================

class KVStore(Base):
    """通用键值表：value 为 JSON 文本，整体读写，updated_at 用于缓存 TTL 判断"""
    __tablename__ = 'kv_store'

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default='')
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


# =============================================================================
# 批次7b：行情 CSV（crypto_coins.csv / star币种行情.csv）
# =============================================================================
# 设计：全列 String 存储（与 CSV 单元格字符串契约一致，空值保持空串），
# 库内英文列名，to_dict/from_row 还原 CSV 中文列名，调用方零改动。

def _vc(n: int = 32):
    """行情表通用短文本列（非空，缺省空串）"""
    return mapped_column(String(n), nullable=False, default='')


class CryptoCoin(Base):
    """全币种多周期行情快照（迁移自 crypto_coins.csv；15m/1H/4H/1D 四周期）"""
    __tablename__ = 'crypto_coins'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rank_no: Mapped[str] = _vc(8)
    symbol: Mapped[str] = _vc(16)
    inst_id: Mapped[str] = mapped_column(String(32), nullable=False, default='', index=True)
    name_cn: Mapped[str] = _vc(32)
    # 1H 周期
    h1_trend: Mapped[str] = _vc(8)
    h1_price: Mapped[str] = _vc(24)
    h1_time: Mapped[str] = _vc(24)
    h1_profit: Mapped[str] = _vc(16)
    h1_close: Mapped[str] = _vc(24)
    h1_macd: Mapped[str] = _vc(24)
    h1_dif: Mapped[str] = _vc(24)
    h1_adx: Mapped[str] = _vc(24)
    h1_atr: Mapped[str] = _vc(24)
    h1_sar: Mapped[str] = _vc(24)
    h1_sar_color: Mapped[str] = _vc(8)
    h1_er: Mapped[str] = _vc(24)
    # 4H 周期
    h4_trend: Mapped[str] = _vc(8)
    h4_price: Mapped[str] = _vc(24)
    h4_time: Mapped[str] = _vc(24)
    h4_profit: Mapped[str] = _vc(16)
    h4_close: Mapped[str] = _vc(24)
    h4_macd: Mapped[str] = _vc(24)
    h4_dif: Mapped[str] = _vc(24)
    h4_adx: Mapped[str] = _vc(24)
    h4_atr: Mapped[str] = _vc(24)
    h4_sar: Mapped[str] = _vc(24)
    h4_sar_color: Mapped[str] = _vc(8)
    h4_er: Mapped[str] = _vc(24)
    # 1D 周期
    d1_trend: Mapped[str] = _vc(8)
    d1_price: Mapped[str] = _vc(24)
    d1_time: Mapped[str] = _vc(24)
    d1_profit: Mapped[str] = _vc(16)
    d1_close: Mapped[str] = _vc(24)
    d1_macd: Mapped[str] = _vc(24)
    d1_dif: Mapped[str] = _vc(24)
    d1_adx: Mapped[str] = _vc(24)
    d1_atr: Mapped[str] = _vc(24)
    d1_sar: Mapped[str] = _vc(24)
    d1_sar_color: Mapped[str] = _vc(8)
    d1_er: Mapped[str] = _vc(24)
    # 15m 周期（追加存储于末尾，展示顺序由前端控制为 15m→1H→4H→1D）
    m15_trend: Mapped[str] = _vc(8)
    m15_price: Mapped[str] = _vc(24)
    m15_time: Mapped[str] = _vc(24)
    m15_profit: Mapped[str] = _vc(16)
    m15_close: Mapped[str] = _vc(24)
    m15_macd: Mapped[str] = _vc(24)
    m15_dif: Mapped[str] = _vc(24)
    m15_adx: Mapped[str] = _vc(24)
    m15_atr: Mapped[str] = _vc(24)
    m15_sar: Mapped[str] = _vc(24)
    m15_sar_color: Mapped[str] = _vc(8)
    m15_er: Mapped[str] = _vc(24)

    # (属性名, CSV 列名) —— 与 crypto_coins.csv 表头逐列对应
    _FIELD_MAP = [
        ('rank_no', 'rank'), ('symbol', 'symbol'), ('inst_id', 'inst_id'), ('name_cn', 'name_cn'),
        ('h1_trend', '1H_趋势'), ('h1_price', '1H_交易价格'), ('h1_time', '1H_交易时间'),
        ('h1_profit', '1H_盈亏%'), ('h1_close', '1H_收盘价'),
        ('h1_macd', 'MACD_1H'), ('h1_dif', 'DIF_1H'), ('h1_adx', 'ADX_1H'),
        ('h1_atr', 'ATR_1H'), ('h1_sar', 'SAR_1H'), ('h1_sar_color', 'SAR颜色_1H'),
        ('h1_er', 'ER_1H'),
        ('h4_trend', '4H_趋势'), ('h4_price', '4H_交易价格'), ('h4_time', '4H_交易时间'),
        ('h4_profit', '4H_盈亏%'), ('h4_close', '4H_收盘价'),
        ('h4_macd', 'MACD_4H'), ('h4_dif', 'DIF_4H'), ('h4_adx', 'ADX_4H'),
        ('h4_atr', 'ATR_4H'), ('h4_sar', 'SAR_4H'), ('h4_sar_color', 'SAR颜色_4H'),
        ('h4_er', 'ER_4H'),
        ('d1_trend', '1D_趋势'), ('d1_price', '1D_交易价格'), ('d1_time', '1D_交易时间'),
        ('d1_profit', '1D_盈亏%'), ('d1_close', '1D_收盘价'),
        ('d1_macd', 'MACD_1D'), ('d1_dif', 'DIF_1D'), ('d1_adx', 'ADX_1D'),
        ('d1_atr', 'ATR_1D'), ('d1_sar', 'SAR_1D'), ('d1_sar_color', 'SAR颜色_1D'),
        ('d1_er', 'ER_1D'),
        ('m15_trend', '15m_趋势'), ('m15_price', '15m_交易价格'), ('m15_time', '15m_交易时间'),
        ('m15_profit', '15m_盈亏%'), ('m15_close', '15m_收盘价'),
        ('m15_macd', 'MACD_15m'), ('m15_dif', 'DIF_15m'), ('m15_adx', 'ADX_15m'),
        ('m15_atr', 'ATR_15m'), ('m15_sar', 'SAR_15m'), ('m15_sar_color', 'SAR颜色_15m'),
        ('m15_er', 'ER_15m'),
    ]

    def to_dict(self):
        """还原 CSV 行形态（中文列名键，全字符串）"""
        return {csv_col: str(getattr(self, attr) or '')
                for attr, csv_col in self._FIELD_MAP}

    @classmethod
    def from_row(cls, row: dict) -> 'CryptoCoin':
        """CSV 行 dict（中文列名键）→ 模型实例，缺失列归一为空串"""
        return cls(**{attr: str(row.get(csv_col) or '')
                      for attr, csv_col in cls._FIELD_MAP})


class StarMarketRow(Base):
    """星标币种行情（迁移自 star币种行情.csv，14 列；id 顺序即拖拽排序后的行序）"""
    __tablename__ = 'star_market'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = _vc(32)
    code: Mapped[str] = mapped_column(String(16), nullable=False, default='', index=True)
    cur_price: Mapped[str] = _vc(24)
    t_15m: Mapped[str] = _vc(8)
    t_60m: Mapped[str] = _vc(8)
    t_4h: Mapped[str] = _vc(8)
    t_1d: Mapped[str] = _vc(8)
    last_trade_time: Mapped[str] = _vc(24)
    direction: Mapped[str] = _vc(8)
    last_trade_price: Mapped[str] = _vc(24)
    profit_1h: Mapped[str] = _vc(16)
    hold_time: Mapped[str] = _vc(24)
    predict: Mapped[str] = _vc(16)
    advice: Mapped[str] = _vc(16)

    # (属性名, CSV 列名) —— 与 star币种行情.csv 表头逐列对应
    _FIELD_MAP = [
        ('name', '名称'), ('code', '代码'), ('cur_price', '现价'),
        ('t_15m', '15分钟'), ('t_60m', '60分钟'), ('t_4h', '4小时'), ('t_1d', '日线'),
        ('last_trade_time', '上次交易时间(1H)'), ('direction', '方向(1H)'),
        ('last_trade_price', '上次交易价格(1H)'), ('profit_1h', '策略盈亏(1H)'),
        ('hold_time', '持仓时间'), ('predict', '预测涨跌'), ('advice', '建议操作'),
    ]

    def to_dict(self):
        """还原 CSV 行形态（中文列名键，全字符串）"""
        return {csv_col: str(getattr(self, attr) or '')
                for attr, csv_col in self._FIELD_MAP}

    @classmethod
    def from_row(cls, row: dict) -> 'StarMarketRow':
        """CSV 行 dict（中文列名键）→ 模型实例，缺失列归一为空串"""
        return cls(**{attr: str(row.get(csv_col) or '')
                      for attr, csv_col in cls._FIELD_MAP})


# =============================================================================
# 批次9：监控告警历史（异常行情 / 持仓盈亏极端值）
# =============================================================================
# 旁路留档：落库失败只记日志，不阻断监控主循环与交易主流程。

class AlertLog(Base):
    """监控告警历史（含被冷却/递进规则抑制未发出的命中，notify_sent 区分）"""
    __tablename__ = 'alert_log'
    # 复合索引：按币种查时间段内告警（与 alert_history 筛选条件对齐）
    __table_args__ = (Index('idx_alert_inst_time', 'inst_id', 'created_at'),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False, default='')
    inst_id: Mapped[str] = mapped_column(String(32), nullable=False, default='', index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default='')
    metric_value: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    threshold: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    message: Mapped[str] = mapped_column(Text, nullable=False, default='')
    notify_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, index=True)


# =============================================================================
# 批次10：定时任务实盘分析记录（手动快照 + 个人判断 + 事后复盘回填）
# =============================================================================

class TaskAnalysisRecord(Base):
    """实盘分析记录：手动快照（价格/长短周期方向）+ 个人判断与原因，
    满 1H/4H 后由查询端惰性回填后续价格供命中率复盘"""
    __tablename__ = 'task_analysis_records'
    # 复合索引：按币种查时间段内记录（列表筛选最高频）
    # idx_tar_slot：按小时槽聚合（分析纪律闸门/巡检最高频）
    __table_args__ = (Index('idx_tar_inst_ts', 'inst_id', 'ts'),
                      Index('idx_tar_slot', 'hour_slot'))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(19), nullable=False)
    inst_id: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    short_period: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    long_period: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    short_dir: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    long_dir: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    long_dir_prev: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    atr_pct: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    user_judgment: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    user_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 分析纪律（批次11）：
    #   hour_slot  冗余存一份 'YYYY-MM-DD HH'，避免对 ts 做函数运算导致全表扫
    #   source     live=当时记录 / backfill=事后补记（服务端按 |now-ts| 自动判定，前端不可指定）
    hour_slot: Mapped[str] = mapped_column(String(13), nullable=False, default='')
    source: Mapped[str] = mapped_column(String(16), nullable=False, default='live')
    price_1h: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    ts_1h: Mapped[Optional[str]] = mapped_column(String(19), nullable=True)
    price_4h: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    ts_4h: Mapped[Optional[str]] = mapped_column(String(19), nullable=True)
    # 批次16-A：先触碰判定（窗口内短周期 K 线逐根扫描，谁先触 ±θ 带判谁）
    #   touch_near/touch_far: up/down/none(全程未触带=真横盘)/conflict(同根双触=未兑现)
    #   touch_*_ts: 首触(或冲突) bar 起点时间；窗口未到齐时为 NULL 待回填
    touch_near: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    touch_near_ts: Mapped[Optional[str]] = mapped_column(String(19), nullable=True)
    touch_far: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    touch_far_ts: Mapped[Optional[str]] = mapped_column(String(19), nullable=True)
    # 批次16-B：结构价计划（仅 rise/fall；无固定时间窗，先触目标=win / 先触失效=loss，
    #   双触同根保守记 loss；始终未触=open 继续观察；超出K线深度无法观测=gap 终局）
    target_price: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    invalid_price: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    plan_result: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    plan_r: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    plan_touch_ts: Mapped[Optional[str]] = mapped_column(String(19), nullable=True)

    def to_dict(self):
        """前端列表形态（复盘字段未回填时为 None）"""
        return {
            'id': self.id,
            'ts': self.ts,
            'inst_id': self.inst_id,
            'price': self.price,
            'short_period': self.short_period,
            'long_period': self.long_period,
            'short_dir': self.short_dir,
            'long_dir': self.long_dir,
            'long_dir_prev': self.long_dir_prev,
            'atr_pct': self.atr_pct,
            'user_judgment': self.user_judgment,
            'user_reason': self.user_reason or '',
            'hour_slot': self.hour_slot or '',
            'source': self.source or 'live',
            'price_1h': self.price_1h,
            'ts_1h': self.ts_1h,
            'price_4h': self.price_4h,
            'ts_4h': self.ts_4h,
            'touch_near': self.touch_near,
            'touch_near_ts': self.touch_near_ts,
            'touch_far': self.touch_far,
            'touch_far_ts': self.touch_far_ts,
            'target_price': self.target_price,
            'invalid_price': self.invalid_price,
            'plan_result': self.plan_result,
            'plan_r': self.plan_r,
            'plan_touch_ts': self.plan_touch_ts,
        }


# =============================================================================
# 批次11：分析纪律（Analysis Discipline）
# -----------------------------------------------------------------------------
# 小时槽合格台账：巡检 job 按 hour_slot 幂等 upsert，既是邮件防重发依据，
# 也是看板「合规率/断档热力/streak/补记率」的唯一数据源。
# status: satisfied（槽内当时就合格）/ missing（过宽限期仍缺）/
#         satisfied_later（先缺后补记补齐）/ exempt（人工豁免）
# =============================================================================

class AnalysisReminderLog(Base):
    """分析纪律小时槽台账（hour_slot 唯一，巡检幂等 upsert）"""
    __tablename__ = 'analysis_reminder_log'
    __table_args__ = (Index('idx_arl_date', 'stat_date'),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hour_slot: Mapped[str] = mapped_column(String(13), nullable=False, unique=True)
    stat_date: Mapped[str] = mapped_column(String(10), nullable=False, default='')
    required_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    actual_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_insts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='missing')
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notified_at: Mapped[str] = mapped_column(String(19), nullable=False, default='')
    resolved_at: Mapped[str] = mapped_column(String(19), nullable=False, default='')
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'hour_slot': self.hour_slot,
            'stat_date': self.stat_date,
            'required_count': self.required_count,
            'actual_count': self.actual_count,
            'missing_insts': self.missing_insts or '',
            'status': self.status,
            'notified': bool(self.notified),
            'notified_at': self.notified_at or '',
            'resolved_at': self.resolved_at or '',
        }


# =============================================================================
# 批次12：盘感模拟模块（RAG + LLM Wiki）
# -----------------------------------------------------------------------------
# 防泄漏核心不变式：ctx_* 列 = 决策当时可见字段（允许进检索与 prompt）；
# outcome_*/hit_*/chg_* 列 = 事后才知字段（只做统计计分，永不进 prompt）。
# 详见 doc/RAG_LLM_Wiki模拟盘感落地方案.md §1.2
# =============================================================================

class InstinctCorpus(Base):
    """盘感语料库（情景记忆）：三源统一压缩成一行一样本，幂等键 (source, source_ref)"""
    __tablename__ = 'instinct_corpus'
    __table_args__ = (
        UniqueConstraint('source', 'source_ref', name='uk_ic_ref'),
        Index('idx_ic_inst_ts', 'inst_id', 'ts'),
        Index('idx_ic_labeled', 'labeled', 'ts'),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[str] = mapped_column(String(19), nullable=False)
    inst_id: Mapped[str] = mapped_column(String(32), nullable=False, default='')
    short_period: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    long_period: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    # ▼ 当时可见上下文（防泄漏边界：只有这些字段允许进入检索与 prompt）
    ctx_short_dir: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    ctx_long_dir: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    ctx_long_dir_prev: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    ctx_atr_pct: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    ctx_atr_pctile: Mapped[float] = mapped_column(Double, nullable=False, default=-1.0)
    ctx_dir_flipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ctx_price: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    ctx_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # ▼ 当时的决策
    judgment: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    decision_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # ▼ 事后结果（永不进 prompt；仅统计与展示标签）
    outcome_near: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    outcome_far: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    chg_near_pct: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    chg_far_pct: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    hit_near: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    hit_far: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    labeled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    _CTX_FIELDS = ('short_period', 'long_period', 'ctx_short_dir', 'ctx_long_dir',
                   'ctx_long_dir_prev', 'ctx_atr_pct', 'ctx_atr_pctile',
                   'ctx_dir_flipped', 'ctx_price', 'ctx_text', 'ts', 'inst_id', 'source')

    def ctx_dict(self):
        """当时可见字段（检索/prompt 渲染唯一入口，物理上取不到 outcome 列）"""
        return {f: getattr(self, f) for f in self._CTX_FIELDS}

    def to_dict(self):
        d = self.ctx_dict()
        d.update({'id': self.id, 'source_ref': self.source_ref,
                  'judgment': self.judgment, 'decision_text': self.decision_text or '',
                  'outcome_near': self.outcome_near, 'outcome_far': self.outcome_far,
                  'chg_near_pct': self.chg_near_pct, 'chg_far_pct': self.chg_far_pct,
                  'hit_near': self.hit_near, 'hit_far': self.hit_far,
                  'labeled': bool(self.labeled)})
        return d


class InstinctWikiRule(Base):
    """盘感 Wiki 规则卡（语义记忆）：candidate→active 状态机，rule_key 幂等合并"""
    __tablename__ = 'instinct_wiki_rules'
    __table_args__ = (
        UniqueConstraint('rule_key', name='uk_wiki_key'),
        Index('idx_wiki_status', 'status'),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default='scenario')
    condition_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stat_basis: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    evidence_refs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='candidate')
    created_by: Mapped[str] = mapped_column(String(16), nullable=False, default='distiller')
    supersedes_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    valid_until: Mapped[Optional[str]] = mapped_column(String(19), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    def to_dict(self):
        return {
            'id': self.id, 'rule_key': self.rule_key, 'statement': self.statement,
            'kind': self.kind, 'condition_json': self.condition_json or '',
            'stat_basis': self.stat_basis, 'evidence_refs': self.evidence_refs or '',
            'status': self.status, 'created_by': self.created_by,
            'supersedes_id': self.supersedes_id, 'valid_until': self.valid_until or '',
        }


class InstinctPrediction(Base):
    """LLM 影子预测流水与结算（工作记忆）：结算口径 = analysis_record_repo.classify_move"""
    __tablename__ = 'instinct_predictions'
    __table_args__ = (
        Index('idx_ip_inst_ts', 'inst_id', 'ts'),
        Index('idx_ip_status', 'status', 'ts'),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(19), nullable=False)
    inst_id: Mapped[str] = mapped_column(String(32), nullable=False)
    ctx_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    judgment: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    confidence: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cited_rule_ids: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    cited_corpus_ids: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    wiki_ids: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    retrieved_refs_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default='')
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_at_pred: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    # ▼ 结算列（由回填任务写）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='pending')
    price_near: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    price_far: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    actual_near: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    actual_far: Mapped[str] = mapped_column(String(8), nullable=False, default='')
    hit_near: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    hit_far: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    user_trusted: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    user_agree: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class InstinctEmbedding(Base):
    """语料文本向量（v2 启用）：向量 JSON 存 TEXT，换模型全量重刷（PK 含 model）"""
    __tablename__ = 'instinct_embeddings'

    corpus_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    # MEDIUMTEXT：与 db_schema.sql 对齐（向量 JSON 可远超 TEXT 64KB 上限风险）
    vec_json: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now)


# 支出管理：金额一律保存整数分，独立于交易账户与热量记录。
def expense_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ExpenseVersionMixin:
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_bin'}
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=expense_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=expense_utcnow)


class ExpenseSettings(ExpenseVersionMixin, Base):
    __tablename__ = 'expense_settings'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default='CNY')
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default='Asia/Shanghai')
    tracking_start_date = mapped_column(Date, nullable=False)
    initialized_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    seed_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default='{}')


class ExpenseCategory(ExpenseVersionMixin, Base):
    __tablename__ = 'expense_categories'
    __table_args__ = (UniqueConstraint('parent_key', 'normalized_name', name='uk_exp_category_name'),
                      ExpenseVersionMixin.__table_args__)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    parent_id: Mapped[Optional[str]] = mapped_column(ForeignKey('expense_categories.id'), nullable=True)
    parent_key: Mapped[str] = mapped_column(String(32), nullable=False, default='')
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default='#4f7b70')
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ExpenseTag(ExpenseVersionMixin, Base):
    __tablename__ = 'expense_tags'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default='#4f7b70')
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ExpensePeriod(ExpenseVersionMixin, Base):
    __tablename__ = 'expense_periods'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    start_date = mapped_column(Date, nullable=False, unique=True)
    end_date_exclusive = mapped_column(Date, nullable=False)
    budget_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    rule_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)


class ExpenseBillTemplate(ExpenseVersionMixin, Base):
    __tablename__ = 'expense_bill_templates'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_id: Mapped[str] = mapped_column(ForeignKey('expense_categories.id'), nullable=False)
    tag_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default='[]')
    repeat_rule_json: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from = mapped_column(Date, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ExpenseBillOccurrence(ExpenseVersionMixin, Base):
    __tablename__ = 'expense_bill_occurrences'
    __table_args__ = (
        UniqueConstraint('template_id', 'due_date', name='uk_exp_bill_due'),
        Index('idx_exp_bill_due_status', 'due_date', 'status'),
        ExpenseVersionMixin.__table_args__,
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    template_id: Mapped[Optional[str]] = mapped_column(ForeignKey('expense_bill_templates.id'), nullable=True)
    due_date = mapped_column(Date, nullable=False)
    expected_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='open')
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)


class ExpenseRecord(ExpenseVersionMixin, Base):
    __tablename__ = 'expense_records'
    __table_args__ = (
        CheckConstraint('amount_minor > 0', name='ck_exp_positive'),
        Index('idx_exp_date_deleted', 'business_date', 'deleted_at', 'id'),
        Index('idx_exp_category_date', 'category_id', 'business_date'),
        ExpenseVersionMixin.__table_args__,
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    business_date = mapped_column(Date, nullable=False)
    category_id: Mapped[str] = mapped_column(ForeignKey('expense_categories.id'), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False, default='')
    merchant: Mapped[str] = mapped_column(String(128), nullable=False, default='')
    note: Mapped[str] = mapped_column(Text, nullable=False, default='')
    payment_method: Mapped[str] = mapped_column(String(64), nullable=False, default='')
    original_record_id: Mapped[Optional[str]] = mapped_column(ForeignKey('expense_records.id'), nullable=True, index=True)
    unlinked_refund_reason: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    bill_occurrence_id: Mapped[Optional[str]] = mapped_column(ForeignKey('expense_bill_occurrences.id'), nullable=True, index=True)
    client_request_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ExpenseRecordTag(Base):
    __tablename__ = 'expense_record_tags'
    record_id: Mapped[str] = mapped_column(ForeignKey('expense_records.id'), primary_key=True)
    tag_id: Mapped[str] = mapped_column(ForeignKey('expense_tags.id'), primary_key=True)
    __table_args__ = (Index('idx_exp_tag_record', 'tag_id', 'record_id'), ExpenseVersionMixin.__table_args__)


class ExpenseDailyCheck(ExpenseVersionMixin, Base):
    __tablename__ = 'expense_daily_checks'
    business_date = mapped_column(Date, primary_key=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='pending')


class ExpenseChangeLog(Base):
    __tablename__ = 'expense_change_logs'
    __table_args__ = (Index('idx_exp_log_entity', 'entity_type', 'entity_id', 'created_at'),
                      ExpenseVersionMixin.__table_args__)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before_json: Mapped[str] = mapped_column(Text, nullable=False, default='{}')
    after_json: Mapped[str] = mapped_column(Text, nullable=False, default='{}')
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=expense_utcnow)


# =============================================================================
# 日记本模块（diary_settings / diary_entries / diary_goals）
# 按天记录 + 公开/隐私双模式 + 密码保护（argon2/bcrypt 哈希落库，永不存明文）
# =============================================================================

class DiarySetting(Base):
    """日记本全局设置（单行 id=1）：可见性模式 / 访问密码哈希 / 生日"""
    __tablename__ = 'diary_settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # public=公开可浏览；private=隐私模式（全部条目需密码查看）
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default='public')
    # 密码哈希串（含算法前缀，如 argon2id$... / bcrypt$... / pbkdf2$...）；空=未设置密码
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    # 出生年月日（YYYY-MM-DD，人生计数器用）；空=未设置
    birth_date: Mapped[str] = mapped_column(String(10), nullable=False, default='')
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    def to_dict(self):
        """对外序列化：绝不带出密码哈希，只给出是否已设置的布尔"""
        return {
            'visibility': self.visibility,
            'has_password': bool(self.password_hash),
            'birth_date': self.birth_date or '',
            'updated_at': _fmt_ts(self.updated_at),
        }


class DiaryEntry(Base):
    """日记条目：以日期为维度组织，一天可有多条；private=1 的单条隐私条目
    在公开模式下也需解锁密码才可见正文"""
    __tablename__ = 'diary_entries'
    __table_args__ = (Index('idx_diary_entries_date', 'date'),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mood: Mapped[str] = mapped_column(String(16), nullable=False, default='')
    private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 跨模块关联来源（如 calorie:YYYY-MM-DD / journal_note:xxx），可空
    linked_from: Mapped[str] = mapped_column(String(64), nullable=False, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'date': self.date,
            'content': self.content,
            'mood': self.mood,
            'private': bool(self.private),
            'linked_from': self.linked_from,
            'created_at': _fmt_ts(self.created_at),
            'updated_at': _fmt_ts(self.updated_at),
        }

    def to_locked_dict(self):
        """未解锁时的隐私占位结构：只暴露存在性（日期/条数由前端聚合），绝不带出正文"""
        return {
            'id': self.id,
            'date': self.date,
            'content': '',
            'mood': '',
            'private': True,
            'locked': True,
            'created_at': '',
            'updated_at': '',
        }


class FoodSpot(Base):
    """美食打卡点：店铺名称/详细地址/美食种类/个人点评心得"""
    __tablename__ = 'calorie_food_spots'
    __table_args__ = (Index('idx_food_spot_create', 'created_at'),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    shop_name: Mapped[str] = mapped_column(String(128), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    category: Mapped[str] = mapped_column(String(64), nullable=False, default='')
    review: Mapped[str] = mapped_column(Text, nullable=False, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    def base_dict(self):
        return {
            'id': self.id,
            'shop_name': self.shop_name,
            'address': self.address or '',
            'category': self.category or '',
            'review': self.review or '',
            'created_at': _fmt_ts(self.created_at),
            'updated_at': _fmt_ts(self.updated_at),
        }


class FoodSpotPhoto(Base):
    """打卡点照片（美食特写/店铺环境/地图截图等），文件落盘 data/food_spot_photos/"""
    __tablename__ = 'calorie_food_spot_photos'
    __table_args__ = (Index('idx_food_spot_photo_pos', 'spot_id', 'position'),)

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    spot_id: Mapped[str] = mapped_column(
        ForeignKey('calorie_food_spots.id', ondelete='CASCADE'), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filename: Mapped[str] = mapped_column(String(128), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'spot_id': self.spot_id,
            'position': self.position,
            'url': f'/calorie/api/food-spot/photo/{self.id}',
            'original_name': self.original_name or '',
            'file_size': self.file_size or 0,
            'created_at': _fmt_ts(self.created_at),
        }


# =============================================================================
# 批次15：每日多维打卡（checkin）
# -----------------------------------------------------------------------------
# 一天一条记录：热量/交易/学习三项自动指标在提交时定格（源数据后续变化不回改），
# 外加手动自我评级（E~S 可空）与纯文本评价。本模块对 calorie/plan/balance 零写入，
# 唯一写目标是下面两张新表。交易指标另有按日缓存表（OKX 拉取结果，控制只读限频）。
# 详见 doc/每日多维打卡设计文档.md
# =============================================================================

class DailyCheckin(Base):
    """每日打卡记录（date 为主键，当天重复提交 upsert 覆盖）"""
    __tablename__ = 'daily_checkins'

    date: Mapped[str] = mapped_column(String(10), primary_key=True)
    # ── 热量项（来源 calorie_records.calorie_deficit，只读定格）──
    cal_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cal_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cal_status: Mapped[str] = mapped_column(String(16), nullable=False, default='no_data')
    # ── 交易项（当日总权益涨幅，剔除净入金；来源 OKX + balance_history 快照）──
    trade_base: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    trade_base_date: Mapped[str] = mapped_column(String(10), nullable=False, default='')
    trade_live: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    trade_transfer: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    trade_pct: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    trade_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ok / no_base / api_error / no_data（补录历史缺快照等）
    trade_status: Mapped[str] = mapped_column(String(16), nullable=False, default='no_data')
    trade_src: Mapped[str] = mapped_column(String(16), nullable=False, default='')
    # ── 学习项（plan_slots 窄聚合：learn 卡当日时长 + done 任务去重数）──
    learn_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    task_done_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    learn_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    learn_status: Mapped[str] = mapped_column(String(16), nullable=False, default='ok')
    # ── 手动项 ──
    rating: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)  # E/D/C/B/A/S
    comment: Mapped[str] = mapped_column(Text, nullable=False, default='')
    all_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 交易项取数账号（审计用）
    account_key: Mapped[str] = mapped_column(String(32), nullable=False, default='')
    # 关联的日记条目 id（可空）：提交打卡时若当日已有日记则定格回填，用于「是否
    # 已写日记」的落库标记；页面上的实时判定仍以 diary_entries 当日存在性为准
    # （日记可能晚于打卡补写，diary 侧回指格式 linked_from='checkin:YYYY-MM-DD'）
    diary_entry_id: Mapped[str] = mapped_column(String(32), nullable=False, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    def to_dict(self):
        return {
            'date': self.date,
            'calorie': {
                'value': self.cal_value, 'passed': bool(self.cal_passed),
                'status': self.cal_status,
            },
            'trade': {
                'base': self.trade_base, 'base_date': self.trade_base_date or '',
                'live': self.trade_live, 'transfer': self.trade_transfer,
                'pct': self.trade_pct, 'passed': bool(self.trade_passed),
                'status': self.trade_status, 'src': self.trade_src or '',
                'account_key': self.account_key or '',
            },
            'learn': {
                'hours': self.learn_hours, 'task_done_count': self.task_done_count,
                'passed': bool(self.learn_passed), 'status': self.learn_status,
            },
            'rating': self.rating or '',
            'comment': self.comment or '',
            'all_passed': bool(self.all_passed),
            'diary_entry_id': self.diary_entry_id or '',
            'created_at': _fmt_ts(self.created_at),
            'updated_at': _fmt_ts(self.updated_at),
        }


class TradingDailySnapshot(Base):
    """交易打卡指标的按日缓存（OKX 拉取结果，避免页面加载反复打接口）。

    fetched_at 为最近一次实拉时间（'YYYY-MM-DD HH:MM:SS'），TTL 判断依据；
    pct 为修正后（剔除净入金）的当日收益率；transfer 为当日净入金（USDT）。
    """
    __tablename__ = 'trading_daily_snapshots'

    account_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[str] = mapped_column(String(10), primary_key=True)
    base_balance: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    base_date: Mapped[str] = mapped_column(String(10), nullable=False, default='')
    live_balance: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    net_transfer: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    pct: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    # ok / no_base / api_error
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='')
    fetched_at: Mapped[str] = mapped_column(String(19), nullable=False, default='')

    def to_dict(self):
        return {
            'account_key': self.account_key,
            'date': self.date,
            'base': self.base_balance, 'base_date': self.base_date or '',
            'live': self.live_balance, 'transfer': self.net_transfer,
            'pct': self.pct, 'status': self.status or '',
            'fetched_at': self.fetched_at or '',
        }


class DiaryGoal(Base):
    """目标倒计时器：标题 + 截止日期，可多个并存，按截止日期升序展示"""
    __tablename__ = 'diary_goals'
    __table_args__ = (Index('idx_diary_goals_due', 'due_date'),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    due_date: Mapped[str] = mapped_column(String(10), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default='')
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'due_date': self.due_date,
            'note': self.note,
            'done': bool(self.done),
            'created_at': _fmt_ts(self.created_at),
            'updated_at': _fmt_ts(self.updated_at),
        }
