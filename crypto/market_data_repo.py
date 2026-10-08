#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行情 CSV - 数据访问层（迁移批次7b）
==========================================
替代两份行情 CSV（均为整表覆盖写语义）：

    crypto_coins 表 ← crypto_coins.csv（批量趋势分析结果，37 列）
    star_market  表 ← star币种行情.csv（星标行情，14 列，行序有意义）

API：
    load_coin_rows(session)        读取全币种行情（rank 数值升序）
    save_coin_rows(session, rows)  整表覆盖写入（rows 为 CSV 中文列名 dict 列表）
    coin_inst_ids(session)         仅取 inst_id 列表（保持 CSV 行序）
    load_star_rows(session)        读取星标行情（id 升序 = 拖拽排序后行序）
    save_star_rows(session, rows)  整表覆盖写入（重建行序）
    count_coin_rows / count_star_rows   迁移校验用

契约与 CSV 版一致：所有单元格均为字符串，空值保持空串 ''。
"""

from sqlalchemy import select, func, delete

from .models import CryptoCoin, StarMarketRow


# ---------------------------------------------------------------------------
# crypto_coins
# ---------------------------------------------------------------------------

def load_coin_rows(session) -> list:
    """读取全币种行情，返回 CSV 中文列名 dict 列表。

    排序与 CSV 一致：按 rank 数值升序（rank 非数字的行排最后，保持相对插入序）。
    """
    rows = session.execute(select(CryptoCoin).order_by(CryptoCoin.id)).scalars().all()
    rows.sort(key=lambda r: (int(r.rank_no) if r.rank_no.isdigit() else 10 ** 9, r.id))
    return [r.to_dict() for r in rows]


def save_coin_rows(session, rows: list):
    """整表覆盖写入（与 CSV 整份重写语义一致）"""
    session.execute(delete(CryptoCoin))
    for row in rows:
        session.add(CryptoCoin.from_row(row))


def coin_inst_ids(session) -> list:
    """仅取 inst_id 列表（过滤空值，rank 序与 load_coin_rows 一致）——get_csv_coins 切库用"""
    rows = session.execute(select(
        CryptoCoin.id, CryptoCoin.rank_no, CryptoCoin.inst_id
    ).order_by(CryptoCoin.id)).all()
    rows.sort(key=lambda r: (int(r.rank_no) if r.rank_no.isdigit() else 10 ** 9, r.id))
    return [r.inst_id.strip() for r in rows if r.inst_id.strip()]


def count_coin_rows(session) -> int:
    return int(session.execute(select(func.count()).select_from(CryptoCoin)).scalar() or 0)


# ---------------------------------------------------------------------------
# star_market
# ---------------------------------------------------------------------------

def load_star_rows(session) -> list:
    """读取星标行情（id 升序 = 当前行序），返回 CSV 中文列名 dict 列表"""
    stmt = select(StarMarketRow).order_by(StarMarketRow.id)
    rows = session.execute(stmt).scalars().all()
    return [r.to_dict() for r in rows]


def save_star_rows(session, rows: list):
    """整表覆盖写入（重建行序：拖拽排序/同步增删后 id 按新行序重分配）"""
    session.execute(delete(StarMarketRow))
    for row in rows:
        session.add(StarMarketRow.from_row(row))


def count_star_rows(session) -> int:
    return int(session.execute(select(func.count()).select_from(StarMarketRow)).scalar() or 0)
