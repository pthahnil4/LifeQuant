#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账户余额历史 - 数据访问层（迁移批次4）
========================================
替代 account_balance_history.json，语义与 JSON 版保持一致：

- append 快照：同 ts 去重（重查覆盖余额），超上限自动裁剪最旧点
- 回溯点 source='backfill' 与真实快照 snapshot 区分

序列化约定（round-trip 兼容）：重组 dict 时 snapshot 点不带 'source' 键，
backfill 点带 source='backfill'，与原 JSON 文件形态完全一致。
"""

from sqlalchemy import select, delete, func

from .models import BalanceHistory


def _row_to_dict(row):
    """DB 行 → 原 JSON 形态的点 dict"""
    d = {'ts': row.ts, 'balance': row.balance}
    if row.source == 'backfill':
        d['source'] = 'backfill'
    return d


def load_all(session):
    """读取全部账号快照，返回 {'accounts': {key: [points...]}}（点按 ts 升序）"""
    rows = session.execute(
        select(BalanceHistory).order_by(
            BalanceHistory.account_key, BalanceHistory.ts)).scalars().all()
    accounts = {}
    for r in rows:
        accounts.setdefault(r.account_key, []).append(_row_to_dict(r))
    return {'accounts': accounts}


def load_account_points(session, account_key, start_ms=None, end_ms=None):
    """读取单账号快照，按 ts 升序；可选时间范围为 [start_ms, end_ms)。"""
    stmt = select(BalanceHistory.ts, BalanceHistory.balance, BalanceHistory.source).where(
        BalanceHistory.account_key == account_key)
    if start_ms is not None:
        stmt = stmt.where(BalanceHistory.ts >= start_ms)
    if end_ms is not None:
        stmt = stmt.where(BalanceHistory.ts < end_ms)
    rows = session.execute(stmt.order_by(BalanceHistory.ts)).all()
    return [_row_to_dict(r) for r in rows]


def count_points(session, account_key):
    """单账号快照点数量"""
    return session.execute(
        select(func.count()).select_from(BalanceHistory)
        .where(BalanceHistory.account_key == account_key)).scalar_one()


def upsert_point(session, account_key, ts_ms, balance, source='snapshot'):
    """插入/覆盖 (account_key, ts) 点（同 ts 重查时覆盖余额）"""
    row = session.get(BalanceHistory, (account_key, ts_ms))
    if row is None:
        session.add(BalanceHistory(
            account_key=account_key, ts=ts_ms, balance=balance, source=source))
    else:
        row.balance = balance
        row.source = source


def insert_points(session, account_key, points):
    """批量插入点（调用方保证 ts 不重复），points 元素含 ts/balance[/source]"""
    for p in points:
        session.add(BalanceHistory(
            account_key=account_key, ts=p['ts'], balance=p['balance'],
            source=p.get('source', 'snapshot')))


def trim_oldest(session, account_key, max_points):
    """超保留上限时删除最旧的点（返回删除行数）"""
    total = count_points(session, account_key)
    if total <= max_points:
        return 0
    overflow = total - max_points
    oldest_ts = session.execute(
        select(BalanceHistory.ts)
        .where(BalanceHistory.account_key == account_key)
        .order_by(BalanceHistory.ts).limit(overflow)).scalars().all()
    if oldest_ts:
        session.execute(
            delete(BalanceHistory)
            .where(BalanceHistory.account_key == account_key)
            .where(BalanceHistory.ts.in_(oldest_ts)))
    return len(oldest_ts)
