#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易运行时状态 - 数据访问层（迁移批次5）
==========================================
替代 5 个调度器状态 JSON，语义与 JSON 版完全一致：

    scheduler_state.json        → trader_directions   整树读写
    reverse_guard_state.json    → reverse_guard       整树读写
    manual_pause_state.json     → manual_pause        整树读写
    tp_runtime_state.json       → tp_runtime_state    按 key upsert/delete
    position_order_state.json   → pos_book/pos_slot/pos_algo/pos_lev
                                  按 inst_id 整段重写（单次落库 ≤9 行）

round-trip 约定（迁移校验用）：从 DB 重组的 dict 与源 JSON 逐字段相等：
- slot 的 qfail_logged 仅 True 时输出该键（对齐 JSON 版临时字段形态）
- algo 恒输出 {'long':..,'short':..} 双键（值为 rec dict 或 None）
- lev_set 行不存在 → None；行存在 → dict（两列均空即空 dict {}）
"""

import json
import hashlib
import copy
import os
import socket
import time
import uuid

from sqlalchemy import select, delete

from .models import (
    TraderDirection, ReverseGuard, ManualPause, TpRuntimeState,
    PosBook, PosSlot, PosAlgo, PosLev, KVStore)

_BUCKETS = ('trend', 'range')
_SLOTS = ('entry', 'exit')
_DIRECTIONS = ('long', 'short')

_SLOT_FIELDS = ('state', 'ord_id', 'price', 'amount', 'placed_ts',
                'acc_filled', 'dir')


# =============================================================================
# 方向记录 / 反向风控 / 人工冷却（整树读写，数据量极小）
# =============================================================================

def load_directions(session) -> dict:
    rows = session.execute(select(TraderDirection)).scalars().all()
    return {r.inst_id: {'short': r.short_dir, 'long': r.long_dir} for r in rows}


def save_directions(session, data: dict):
    session.execute(delete(TraderDirection))
    for inst_id, dirs in (data or {}).items():
        session.add(TraderDirection(
            inst_id=inst_id,
            short_dir=str((dirs or {}).get('short') or ''),
            long_dir=str((dirs or {}).get('long') or '')))


def load_reverse_guard(session) -> dict:
    rows = session.execute(select(ReverseGuard)).scalars().all()
    return {r.inst_id: {'detected_ts': r.detected_ts,
                        'long_direction': r.long_direction,
                        'reverse_side': r.reverse_side,
                        'reverse_mode': r.reverse_mode,
                        'reverse_amount': r.reverse_amount,
                        'warned': bool(r.warned)} for r in rows}


def save_reverse_guard(session, data: dict):
    session.execute(delete(ReverseGuard))
    for inst_id, g in (data or {}).items():
        g = g or {}
        session.add(ReverseGuard(
            inst_id=inst_id,
            detected_ts=float(g.get('detected_ts', 0) or 0),
            long_direction=str(g.get('long_direction') or ''),
            reverse_side=str(g.get('reverse_side') or ''),
            reverse_mode=str(g.get('reverse_mode') or ''),
            reverse_amount=float(g.get('reverse_amount', 0) or 0),
            warned=bool(g.get('warned', False))))


def load_manual_pause(session) -> dict:
    rows = session.execute(select(ManualPause)).scalars().all()
    return {r.inst_id: r.resume_ts for r in rows}


def save_manual_pause(session, data: dict):
    session.execute(delete(ManualPause))
    for inst_id, ts in (data or {}).items():
        session.add(ManualPause(inst_id=inst_id, resume_ts=float(ts or 0)))


# =============================================================================
# 止盈引擎运行时状态（按 key upsert/delete，evaluate 高频调用）
# =============================================================================

def load_tp_state(session) -> dict:
    rows = session.execute(select(TpRuntimeState)).scalars().all()
    out = {}
    for r in rows:
        try:
            ladder = json.loads(r.ladder_done) if r.ladder_done else []
        except (TypeError, ValueError):
            ladder = []
        try:
            extra = json.loads(r.extra) if getattr(r, 'extra', None) else {}
            if not isinstance(extra, dict):
                extra = {}
        except (TypeError, ValueError):
            extra = {}
        out[r.state_key] = {'entry_ts': r.entry_ts, 'peak': r.peak,
                            'trough': r.trough, 'avg_px': r.avg_px,
                            'ladder_done': ladder, 'extra': extra}
    return out


def upsert_tp_state(session, key: str, st: dict):
    row = session.get(TpRuntimeState, key)
    if row is None:
        row = TpRuntimeState(state_key=key)
        session.add(row)
    row.entry_ts = float(st.get('entry_ts', 0) or 0)
    row.peak = float(st.get('peak', 0) or 0)
    row.trough = float(st.get('trough', 0) or 0)
    row.avg_px = float(st.get('avg_px', 0) or 0)
    row.ladder_done = json.dumps(st.get('ladder_done') or [])
    ex = st.get('extra')
    row.extra = json.dumps(ex) if isinstance(ex, dict) and ex else None


def delete_tp_state(session, key: str):
    session.execute(delete(TpRuntimeState).where(TpRuntimeState.state_key == key))


# =============================================================================
# 双仓位本地账本（position_order_state.json 拆 4 表）
# =============================================================================

def _slot_from_row(row) -> dict:
    d = {k: getattr(row, k) for k in _SLOT_FIELDS}
    if row.qfail_logged:
        d['qfail_logged'] = True
    return d


def _algo_from_row(row) -> dict:
    return {'algo_id': row.algo_id, 'amount': row.amount,
            'sl': row.sl, 'tp': row.tp, 'ts': row.ts}


def _position_prefix(account, environment='0'):
    # KVStore.key 上限 64；账号与环境共同分区，不能回退到无账号的旧账本。
    scope = hashlib.sha256(f'{environment}:{account}'.encode()).hexdigest()[:20]
    return f'pos_v2:{scope}:'


def load_position_state(session, account=None, environment='0') -> dict:
    """有账号时读取完整 V2 状态；无账号保留旧表读取供迁移工具使用。"""
    if account is not None:
        prefix = _position_prefix(account, environment)
        rows = session.execute(select(KVStore).where(
            KVStore.key.startswith(prefix))).scalars()
        out = {}
        for row in rows:
            data = json.loads(row.value)
            if (not isinstance(data, dict) or data.get('schema') != 2
                    or not isinstance(data.get('state'), dict)):
                raise ValueError('交易账本损坏，禁止按空仓启动')
            out[row.key[len(prefix):]] = data['state']
        return out
    books = session.execute(select(PosBook)).scalars().all()
    slots = session.execute(select(PosSlot)).scalars().all()
    algos = session.execute(select(PosAlgo)).scalars().all()
    levs = session.execute(select(PosLev)).scalars().all()

    slots_map = {}
    for r in slots:
        slots_map.setdefault((r.inst_id, r.bucket), {})[r.slot] = r
    algos_map = {}
    for r in algos:
        algos_map.setdefault((r.inst_id, r.bucket), {})[r.direction] = r
    lev_map = {r.inst_id: r for r in levs}

    state = {}
    for b in books:
        s = state.setdefault(b.inst_id, {})
        bk = {
            'held': {'long': b.held_long, 'short': b.held_short},
            'avg_px': {'long': b.avg_px_long, 'short': b.avg_px_short},
            'slots': {sl: _slot_from_row(r)
                      for sl, r in slots_map.get((b.inst_id, b.bucket), {}).items()},
            'prev_open_confirmed': bool(b.prev_open_confirmed),
            'prev_close_confirmed': bool(b.prev_close_confirmed),
            'last_desired': b.last_desired,
            'algo': {d: (_algo_from_row(r) if r else None)
                     for d, r in ((d, algos_map.get((b.inst_id, b.bucket), {}).get(d))
                                  for d in _DIRECTIONS)},
        }
        s[b.bucket] = bk
    # lev_set 与 algo 独立于 book 行存在性：补齐只有 lev/algo 记录的 inst
    for inst_id, r in lev_map.items():
        state.setdefault(inst_id, {})
    for (inst_id, _bucket) in algos_map:
        state.setdefault(inst_id, {})
    for inst_id, s in state.items():
        r = lev_map.get(inst_id)
        if r is None:
            s['lev_set'] = None
        else:
            lev = {}
            if r.cross_lev is not None:
                lev['cross'] = r.cross_lev
            if r.isolated_lev is not None:
                lev['isolated'] = r.isolated_lev
            s['lev_set'] = lev
    return state


def load_inst_revision(session, inst_id: str, account=None, environment='0'):
    """读取单币账本在库内的权威 _revision（KV 路径），供 _save 撞版本冲突时
    重新同步内存版本号、修复版本链后重试。

    返回值：int=库内版本号（0 表示该币尚无记录）；None=无账号路径/数据损坏，
    调用方据此判定不可安全重试。直连 select + populate_existing 绕过进程内
    KV 缓存，确保读到的是提交后的权威值而非缓存旧值。
    """
    if account is None:
        return None
    key = _position_prefix(account, environment) + inst_id
    row = session.execute(
        select(KVStore).where(KVStore.key == key)
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None:
        return 0
    try:
        data = json.loads(row.value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('state'), dict):
        return None
    return int(data['state'].get('_revision', 0) or 0)


def save_inst_position(session, inst_id: str, s: dict, account=None, environment='0'):
    """V2 将成交水位、未知订单、冷却和账本同事务保存，并校验写入版本。"""
    if account is not None:
        from .config_store_repo import patch_json_config
        key = _position_prefix(account, environment) + inst_id
        if len(key) > 64:
            raise ValueError('交易状态键超长')
        expected = int(s.get('_revision', 0))

        def update(current):
            current = current or {}
            if current and (current.get('schema') != 2 or not isinstance(current.get('state'), dict)):
                raise ValueError('交易账本格式损坏，禁止覆盖')
            revision = int((current.get('state') or {}).get('_revision', 0))
            if revision != expected:
                raise RuntimeError('交易账本版本冲突：存在其他写入者，停止交易并重新恢复状态')
            state = copy.deepcopy(s)
            state['_revision'] = expected + 1
            return {'schema': 2, 'state': state}

        patch_json_config(session, key, update, strict=True)
        # 由调用方在事务提交成功后更新内存版本，提交失败不能提前推进。
        return expected + 1
    s = s or {}
    session.execute(delete(PosBook).where(PosBook.inst_id == inst_id))
    session.execute(delete(PosSlot).where(PosSlot.inst_id == inst_id))
    session.execute(delete(PosAlgo).where(PosAlgo.inst_id == inst_id))
    session.execute(delete(PosLev).where(PosLev.inst_id == inst_id))

    for bucket in _BUCKETS:
        bk = s.get(bucket)
        if not isinstance(bk, dict):
            continue
        held = bk.get('held') or {}
        avg = bk.get('avg_px') or {}
        session.add(PosBook(
            inst_id=inst_id, bucket=bucket,
            held_long=float(held.get('long', 0) or 0),
            held_short=float(held.get('short', 0) or 0),
            avg_px_long=float(avg.get('long', 0) or 0),
            avg_px_short=float(avg.get('short', 0) or 0),
            prev_open_confirmed=bool(bk.get('prev_open_confirmed', False)),
            prev_close_confirmed=bool(bk.get('prev_close_confirmed', False)),
            last_desired=bk.get('last_desired')))
        for slot, pt in (bk.get('slots') or {}).items():
            if not isinstance(pt, dict):
                continue
            session.add(PosSlot(
                inst_id=inst_id, bucket=bucket, slot=slot,
                state=str(pt.get('state') or 'IDLE'),
                ord_id=pt.get('ord_id'),
                price=float(pt.get('price', 0) or 0),
                amount=float(pt.get('amount', 0) or 0),
                placed_ts=float(pt.get('placed_ts', 0) or 0),
                acc_filled=float(pt.get('acc_filled', 0) or 0),
                dir=pt.get('dir'),
                qfail_logged=bool(pt.get('qfail_logged', False))))
        for direction, rec in (bk.get('algo') or {}).items():
            if not isinstance(rec, dict):
                continue
            session.add(PosAlgo(
                inst_id=inst_id, bucket=bucket, direction=direction,
                algo_id=rec.get('algo_id'),
                amount=float(rec.get('amount', 0) or 0),
                sl=float(rec.get('sl', 0) or 0),
                tp=float(rec.get('tp', 0) or 0),
                ts=float(rec.get('ts', 0) or 0)))

    lev = s.get('lev_set')
    if isinstance(lev, dict):
        session.add(PosLev(
            inst_id=inst_id,
            cross_lev=lev.get('cross'),
            isolated_lev=lev.get('isolated')))


def save_position_state(session, data: dict, account=None, environment='0'):
    """整树重写（迁移/兜底用；日常保存走 save_inst_position）"""
    if account is not None:
        return {inst: save_inst_position(session, inst, state, account, environment)
                for inst, state in (data or {}).items()}
    for model in (PosBook, PosSlot, PosAlgo, PosLev):
        session.execute(delete(model))
    for inst_id, s in (data or {}).items():
        save_inst_position(session, inst_id, s)


def clear_position_state(session):
    for model in (PosBook, PosSlot, PosAlgo, PosLev):
        session.execute(delete(model))


# =============================================================================
# 跨进程调度单实例租约（防“两个写入者”）
# -----------------------------------------------------------------------------
# 背景：账本用 _revision 乐观锁（save_inst_position）。单进程内版本号自洽；
# 但若两个调度进程（常见于重启时旧进程未死干净、或误开第二个 app.py）
# 同写一份 (account, environment) 账本，乐观锁会抛“交易账本版本冲突：
# 存在其他写入者” → _storage_ok=False → G3 全局停摆（交易瘫痪）。
# 本租约在启动时抢占、每轮续约：第二个进程检测到存活持有者则拒绝启动，
# 从根上杜绝双写。租约带 TTL，崩溃/卡死的旧进程超时后自动被接管，
# 绝不会反过来把新进程永久锁死。
# =============================================================================

LEASE_DEFAULT_TTL = 180.0   # 秒；未续约超过此时长视为陈旧租约，可被接管


class LeaseRefusedError(RuntimeError):
    """另一存活调度进程持有本账本租约（拒绝重复启动/本机已被接管）。"""

    def __init__(self, holder):
        self.holder = holder or {}
        super().__init__('另一存活调度进程持有租约')


def make_lease_holder_id() -> str:
    """本进程唯一持有者标识（主机:进程号:随机后缀），用于区分同机多进程。"""
    return f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'


def _lease_key(account, environment='0') -> str:
    """租约键，按 (environment, account) 分区（与账本同口径）；≤ KVStore.key 上限 64。"""
    scope = hashlib.sha256(f'{environment}:{account}'.encode()).hexdigest()[:20]
    return f'trader_lease:{scope}'


def _lease_decide(current, holder_id, ttl, now):
    """纯决策（无 I/O，供 patch_json_config 事务内调用，也供离线冒烟直接验证）：

    返回要写入的新租约 dict；若检测到**其他存活持有者**（未过期）则抛
    LeaseRefusedError（携带对方 holder）。TTL 内未续约的租约视为陈旧（持有者
    已崩溃/卡死）→ 允许接管。同一 holder_id 重入为续约（保留 acquired_ts）。
    """
    cur = current if isinstance(current, dict) else {}
    cur_holder = cur.get('holder_id')
    if cur_holder and cur_holder != holder_id:
        cur_ttl = float(cur.get('ttl', ttl) or ttl)
        expires = float(cur.get('renewed_ts', 0) or 0) + cur_ttl
        if expires > now:
            raise LeaseRefusedError(cur)
    acquired = float(cur.get('acquired_ts', 0) or 0) if cur_holder == holder_id else 0.0
    return {'holder_id': holder_id, 'pid': os.getpid(), 'host': socket.gethostname(),
            'acquired_ts': acquired or now, 'renewed_ts': now, 'ttl': float(ttl)}


def acquire_trader_lease(session, account, environment, holder_id,
                         ttl=LEASE_DEFAULT_TTL, now=None):
    """抢占/续约本 (account, environment) 的调度单实例租约（行锁保证跨进程原子）。

    成功返回本进程租约 dict；检测到其他存活持有者抛 LeaseRefusedError。
    """
    from .config_store_repo import patch_json_config
    now = float(now if now is not None else time.time())
    return patch_json_config(
        session, _lease_key(account, environment),
        lambda cur: _lease_decide(cur, holder_id, ttl, now), strict=False)


def renew_trader_lease(session, account, environment, holder_id,
                       ttl=LEASE_DEFAULT_TTL, now=None):
    """续约：本进程仍持有则续期；租约已被他人接管则抛 LeaseRefusedError（调用方应 fence 自停）。"""
    return acquire_trader_lease(session, account, environment, holder_id, ttl, now)


def release_trader_lease(session, account, environment, holder_id) -> bool:
    """释放租约：仅当仍由本进程持有时删除，绝不误删接管者的租约。返回是否删除。"""
    from .config_store_repo import load_json_config, delete_json_config
    key = _lease_key(account, environment)
    cur = load_json_config(session, key)
    if isinstance(cur, dict) and cur.get('holder_id') == holder_id:
        delete_json_config(session, key)
        return True
    return False
