#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
结构化成交流水 - 数据访问层（迁移批次6）
==========================================
替代 logs/trade_journal.jsonl（append-only JSONL）：

    append_fill(session, rec)        追加一条成交记录
    query_fills(session, ...)        按 inst_id/bucket/ts 区间筛选（闭区间）
    count_fills(session)             迁移校验用计数

语义与 JSONL 版完全一致：
- 读取结果按 ts 升序（同 ts 按写入顺序），字段形态与 JSONL 行记录逐键相等
- 时间筛选为 'YYYY-MM-DD HH:MM:SS' 字符串闭区间比较
"""

from sqlalchemy import select, func

from .models import TradeJournal


def append_fill(session, rec: dict):
    """追加一条成交记录（rec 为 record_fill 组装好的 10 字段 dict）"""
    session.add(TradeJournal(
        ts=str(rec.get('ts') or ''),
        run_id=str(rec.get('run_id') or ''),
        inst_id=str(rec.get('inst_id') or ''),
        bucket=str(rec.get('bucket') or ''),
        direction=str(rec.get('direction') or ''),
        action=str(rec.get('action') or ''),
        price=float(rec.get('price') or 0),
        amount=float(rec.get('amount') or 0),
        ord_id=str(rec.get('ord_id') or ''),
        reason=str(rec.get('reason') or 'signal'),
    ))


def query_fills(session, inst_id: str = None, bucket: str = None,
                start: str = None, end: str = None) -> list:
    """按条件查询成交流水，返回 dict 列表（ts 升序，同 ts 按写入顺序）。

    筛选语义与 JSONL 版一致：inst_id/bucket 精确匹配；
    start/end 为 'YYYY-MM-DD HH:MM:SS' 字符串闭区间。
    """
    stmt = select(TradeJournal)
    if inst_id:
        stmt = stmt.where(TradeJournal.inst_id == inst_id)
    if bucket:
        stmt = stmt.where(TradeJournal.bucket == bucket)
    if start:
        stmt = stmt.where(TradeJournal.ts >= start)
    if end:
        stmt = stmt.where(TradeJournal.ts <= end)
    stmt = stmt.order_by(TradeJournal.ts, TradeJournal.id)
    rows = session.execute(stmt).scalars().all()
    return [r.to_dict() for r in rows]


def count_fills(session) -> int:
    return int(session.execute(select(func.count()).select_from(TradeJournal)).scalar() or 0)
