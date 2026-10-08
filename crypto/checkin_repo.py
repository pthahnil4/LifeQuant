#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日多维打卡 - 数据访问层（批次15）
====================================
两张表的读写：

- daily_checkins          打卡主记录（date 主键，当天重复提交 upsert 覆盖）
- trading_daily_snapshots 交易指标按日缓存（OKX 拉取结果，控制只读限频）

不变量（见 doc/每日多维打卡设计文档.md）：
- 本模块只写自己的两张表，对 calorie / plan / balance_history 零写入；
- 打卡记录是提交时的定格快照，源数据后续变化不回改历史行。
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from .models import DailyCheckin, TradingDailySnapshot

RATINGS = ('E', 'D', 'C', 'B', 'A', 'S')
MAX_COMMENT_CHARS = 2000
# 缓存表按账号保留上限（防极端场景无限增长；正常每天 1~2 行，366 约一年）
_SNAPSHOT_RETAIN_DAYS = 366


def _valid_date(date_str) -> bool:
    try:
        datetime.strptime(str(date_str or ''), '%Y-%m-%d')
        return True
    except ValueError:
        return False


# =============================================================================
# 打卡主记录
# =============================================================================

def load_checkin(session, date_str):
    """读取单日打卡；未打卡返回 None"""
    return session.get(DailyCheckin, date_str)


def load_history(session, start, end):
    """区间历史（含端点），按日期降序"""
    rows = session.execute(
        select(DailyCheckin)
        .where(DailyCheckin.date >= start, DailyCheckin.date <= end)
        .order_by(DailyCheckin.date.desc())).scalars().all()
    return rows


def upsert_checkin(session, record: dict):
    """写入/覆盖单日打卡。

    record 为列名字典（date 必填且为 YYYY-MM-DD）；created_at 只在首次
    插入时写入，覆盖更新保留原创建时间（与 calorie 记录同款语义）。
    返回落库后的行对象。
    """
    date_str = record.get('date') or ''
    if not _valid_date(date_str):
        raise ValueError(f'打卡日期非法: {date_str!r}')
    row = session.get(DailyCheckin, date_str)
    now = datetime.now()
    if row is None:
        row = DailyCheckin(date=date_str, created_at=now, updated_at=now)
        session.add(row)
    else:
        row.updated_at = now
    skip = {'date', 'created_at', 'updated_at'}
    for col, val in record.items():
        if col in skip or not hasattr(row, col):
            continue
        setattr(row, col, val)
    session.flush()
    return row


def normalize_rating(raw):
    """评级归一：非法/空 → None（可选字段）"""
    val = str(raw or '').strip().upper()
    return val if val in RATINGS else None


def normalize_comment(raw):
    """评价归一：非空串强制、长度截断"""
    text = str(raw or '').strip()
    return text[:MAX_COMMENT_CHARS]


# =============================================================================
# 交易指标按日缓存（trading_daily_snapshots）
# =============================================================================

def load_snapshot(session, account_key, date_str):
    return session.get(TradingDailySnapshot, (account_key, date_str))


def snapshot_is_fresh(row, ttl_sec: float) -> bool:
    """缓存新鲜度：fetched_at 距今 < TTL"""
    if row is None or not row.fetched_at:
        return False
    try:
        fetched = datetime.strptime(row.fetched_at, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return False
    return (datetime.now() - fetched).total_seconds() < float(ttl_sec)


def upsert_snapshot(session, account_key, date_str, values: dict):
    """覆盖写入当日交易指标缓存（同账号同日只留最新一次拉取结果）"""
    row = session.get(TradingDailySnapshot, (account_key, date_str))
    if row is None:
        row = TradingDailySnapshot(account_key=account_key, date=date_str)
        session.add(row)
    for col, val in values.items():
        if hasattr(row, col):
            setattr(row, col, val)
    session.flush()
    _trim_snapshots(session, account_key)
    return row


def _trim_snapshots(session, account_key):
    """按日期裁剪：只保留该账号最近 _SNAPSHOT_RETAIN_DAYS 个日历日内的行"""
    cutoff = (datetime.now() - timedelta(days=_SNAPSHOT_RETAIN_DAYS)).strftime('%Y-%m-%d')
    rows = session.execute(
        select(TradingDailySnapshot)
        .where(TradingDailySnapshot.account_key == account_key,
               TradingDailySnapshot.date < cutoff)).scalars().all()
    for r in rows:
        session.delete(r)
