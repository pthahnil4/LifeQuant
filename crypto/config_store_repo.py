#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置/缓存整份存取层（迁移批次7a + 批次8）
==========================================
"整份读写型" JSON 存入通用 kv_store 表（value 为 JSON 文本）：

    key='strategy_config'      task/config/config_trend_range.json（实盘交易策略配置）
    key='coin_selection'       config.json（币种自选 all_coins/starred/floating）
    key='instrument_spec_cache' 合约规格缓存（24h TTL，可重建）        ← 批次8
    key='market_scan_cache'     市场扫描榜单缓存（可重建）             ← 批次8

API：
    load_json_config(session, key)   读取整份配置（不存在/损坏 → None）
    load_json_config_cached(key)     带 TTL 的缓存读（高频读路径推荐）
    save_json_config(session, key, data)   整份覆盖写入（写穿透失效缓存）
    delete_json_config(session, key)   删除（测试清场用，同步失效缓存）
    invalidate_config_cache(key)     手工失效缓存（外部直接改库后调用）

调用方约定（与 JSON 版一致的容错语义）：
- 读：DB 优先，DB 不可用/无数据时回退本地文件，保证交易定时任务不受影响
- 写：DB 主存 + 本地文件双写，文件保留为兜底数据源
"""

import json
import copy
import time
import threading

from sqlalchemy import event
from sqlalchemy.orm import Session

from .models import KVStore


class CorruptConfigError(Exception):
    """严格模式下 kv 行存在但 value 无法解析为合法 JSON 对象。

    与「从未保存」（缺记录）区分开：损坏数据不得被普通保存静默覆盖，
    调用方须据此返回明确的错误码并禁用普通写入。"""

KEY_STRATEGY_CONFIG = 'strategy_config'
KEY_COIN_SELECTION = 'coin_selection'
KEY_INSTRUMENT_SPEC_CACHE = 'instrument_spec_cache'
KEY_MARKET_SCAN_CACHE = 'market_scan_cache'
# 智能减仓残留仓记录（key='instId|bucket|direction'，value=减仓快照）：
# 供 /api/task/residual-positions 接口查询，方便人工扭亏为盈操作
KEY_SMART_REDUCE_RESIDUALS = 'smart_reduce_residuals'
# 定时任务历史币种库：{instId: {first_used, last_used, use_count}}
# 每次保存交易配置时 upsert；从配置移除的币种不删除（变为"已停用"留存）
KEY_TASK_COIN_LIBRARY = 'task_coin_history_library'
# 实盘调度器「期望运行状态」（desired_running/account/auto_resume）：
# 供进程重启后判断是否该自动拉起实盘，详见 trading_runtime_repo.py
KEY_TRADING_RUNTIME = 'trading_runtime'


def load_json_config(session, key: str):
    """读取整份配置 JSON；键不存在或 value 非合法 JSON 时返回 None"""
    kv = session.get(KVStore, key)
    if kv is None or not kv.value:
        return None
    try:
        data = json.loads(kv.value)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# =============================================================================
# 进程内 TTL 缓存：配置类数据变更频率极低，高频读路径无需每请求打库。
# 单进程 Flask 部署下内存缓存即可；将来多 worker 时可无缝替换为 Redis。
# =============================================================================
_kv_cache = {}          # key -> (monotonic 时间戳, 配置 dict)
_kv_cache_lock = threading.Lock()
_KV_CACHE_TTL = 30.0    # 秒；仅用于允许短时陈旧的非关键配置
_cache_generation = 0   # 失效屏障，防止提交前发起的查询在提交后重新填入旧缓存
_PENDING_INVALIDATIONS = '_crypto_config_invalidations'


def _critical_key(key):
    return (key == KEY_TRADING_RUNTIME or key == KEY_STRATEGY_CONFIG
            or key.startswith(KEY_STRATEGY_CONFIG + ':')
            or key in ('analysis_discipline_config', 'analysis_discipline_state'))


def _invalidate_after_commit(session, key):
    tx = session.get_nested_transaction() or session.get_transaction()
    session.info.setdefault(_PENDING_INVALIDATIONS, {}).setdefault(tx, set()).add(key)


@event.listens_for(Session, 'after_commit')
def _config_committed(session):
    pending = session.info.get(_PENDING_INVALIDATIONS, {})
    tx = session.get_nested_transaction() or session.get_transaction()
    keys = pending.pop(tx, set())
    if tx is not None and tx.nested:
        pending.setdefault(tx.parent, set()).update(keys)
    else:
        for key in keys:
            invalidate_config_cache(key)


@event.listens_for(Session, 'after_rollback')
def _config_rolled_back(session):
    tx = session.get_nested_transaction() or session.get_transaction()
    session.info.get(_PENDING_INVALIDATIONS, {}).pop(tx, None)


@event.listens_for(Session, 'after_transaction_end')
def _config_transaction_ended(session, transaction):
    if transaction.parent is None:
        session.info.pop(_PENDING_INVALIDATIONS, None)


def load_json_config_cached(key: str, ttl: float = None):
    """带 TTL 的整份配置读取；TTL 内直接命中内存，无 DB 往返。

    返回深拷贝，防止调用方修改返回值污染缓存（与原每次 json.loads
    返回新对象的契约一致）；DB 不可用时返回 None，由调用方文件兜底。
    """
    now = time.monotonic()
    critical = _critical_key(key)
    with _kv_cache_lock:
        generation = _cache_generation
        ent = None if critical else _kv_cache.get(key)
        if ent is not None and now - ent[0] < (ttl if ttl is not None else _KV_CACHE_TTL):
            return copy.deepcopy(ent[1])
    from .database import session_scope
    try:
        with session_scope() as session:
            data = load_json_config(session, key)
    except Exception:
        return None
    with _kv_cache_lock:
        if not critical and generation == _cache_generation:
            _kv_cache[key] = (now, data)
    return copy.deepcopy(data)


def invalidate_config_cache(key: str = None):
    """失效缓存；key 为 None 时清空全部（外部绕过 repo 直改库后需调用）"""
    global _cache_generation
    with _kv_cache_lock:
        _cache_generation += 1
        if key is None:
            _kv_cache.clear()
        else:
            _kv_cache.pop(key, None)


def save_json_config(session, key: str, data: dict):
    """整份覆盖写入（utf-8 原文，中文不转义，与原文件保持一致）；
    写穿透失效对应缓存键，后续读回源 DB 重建"""
    text = json.dumps(data, ensure_ascii=False)
    kv = session.get(KVStore, key)
    if kv is None:
        session.add(KVStore(key=key, value=text))
    else:
        kv.value = text
    _invalidate_after_commit(session, key)


def patch_json_config(session, key: str, transform, strict: bool = False):
    """同一事务锁定后合并 JSON；首次插入同样串行，不丢并发字段更新。

    transform 仅做内存计算，不允许外部 I/O；事务提交由调用方负责。

    strict=False（默认，保持原语义）：损坏/缺记录都当作空 dict 传给 transform。
    strict=True（导航等需区分缺记录与损坏数据的调用方）：
      - 缺记录 / 占位 '{}' / 空串 → 传 None 给 transform（表示从未保存）；
      - value 存在但无法解析为 JSON 对象 → 抛 CorruptConfigError（事务回滚，
        绝不以空对象覆盖损坏数据）。
    """
    from sqlalchemy import select
    dialect = session.get_bind().dialect.name
    if dialect == 'mysql':
        from sqlalchemy.dialects.mysql import insert
        stmt = insert(KVStore).values(key=key, value='{}')
        stmt = stmt.on_duplicate_key_update(key=key)
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
        stmt = insert(KVStore).values(key=key, value='{}').on_conflict_do_nothing()
    else:
        raise RuntimeError('配置原子合并仅支持 MySQL 和离线 SQLite 测试')
    session.execute(stmt)
    row = session.execute(select(KVStore).where(KVStore.key == key).with_for_update()
                          .execution_options(populate_existing=True)).scalar_one()
    raw = row.value
    if strict:
        if raw is None or raw == '' or raw == '{}':
            current = None
        else:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError) as e:
                raise CorruptConfigError(f'{key} 存量数据无法解析为 JSON: {e}')
            if not isinstance(parsed, dict):
                raise CorruptConfigError(f'{key} 存量数据不是 JSON 对象')
            current = parsed
    else:
        try:
            current = json.loads(raw or '{}')
        except (TypeError, ValueError):
            current = {}
        current = current if isinstance(current, dict) else {}
    result = transform(current)
    row.value = json.dumps(result, ensure_ascii=False)
    _invalidate_after_commit(session, key)
    return result


def delete_json_config(session, key: str):
    """删除指定配置键（不存在时静默），同步失效缓存"""
    kv = session.get(KVStore, key)
    if kv is not None:
        session.delete(kv)
    _invalidate_after_commit(session, key)


# =============================================================================
# 交易配置按账号隔离（多账号共用一个库时，各账号配置互不串味）
# -----------------------------------------------------------------------------
# 背景：本地测试账号（如 StageOne，1U 小额）与服务器实盘主账号（如 Hunter，
# 20U）共用同一个 MySQL。若 strategy_config 只有一份全局记录，实盘主账号写
# 进去的 20U 会被本地测试账号「DB 优先」读到，用测试账号去下 20U 的单 →
# 可用保证金不足 51008。按账号拆分 key 后各读各的，永不串味。
#
# 兜底链：专属 strategy_config:{account} → 全局 strategy_config → 调用方文件。
# 保留全局兜底是为「部署时序容错」：新代码先上、专属 key 尚未迁移时，实盘
# 主账号仍能回退到全局那份（=它自己写的 20U），绝不会骤降到文件的 1U。
# =============================================================================

def strategy_config_key(account=None):
    """账号专属交易配置 key：'strategy_config:{account}'。

    account 为空 → 返回全局 KEY_STRATEGY_CONFIG（向后兼容/默认账号兜底）。
    """
    acct = str(account or '').strip()
    return f'{KEY_STRATEGY_CONFIG}:{acct}' if acct else KEY_STRATEGY_CONFIG


def load_strategy_config_cached(account=None, ttl: float = None):
    """按账号读交易配置（带 TTL 缓存）：账号专属 → 全局兜底。

    - 专属 'strategy_config:{account}' 命中即返回（各账号互不串味）；
    - 专属缺失回退全局 'strategy_config'（迁移过渡期/未拆分账号）；
    - 仍缺失返回 None，由调用方回退本地文件。
    """
    if account:
        cfg = load_json_config_cached(strategy_config_key(account), ttl)
        if cfg is not None:
            return cfg
    return load_json_config_cached(KEY_STRATEGY_CONFIG, ttl)


def save_strategy_config(session, account, data: dict):
    """按账号整份覆盖写交易配置（写穿透失效对应缓存键）。

    account 为空时写全局 KEY_STRATEGY_CONFIG（向后兼容）。
    """
    save_json_config(session, strategy_config_key(account), data)


def load_live_strategy_config_cached(ttl: float = None):
    """读「当前实盘运行账号」的交易配置（专属 → 全局兜底）。

    供无账号上下文的全局模块（行情监控 alert_monitor / 分析纪律 discipline）
    定位该读哪份配置：账号取自 trading_runtime（实盘期望运行状态）。
    延迟导入 trading_runtime_repo 规避循环依赖（它反向 import 本模块）。
    """
    acct = None
    try:
        from . import trading_runtime_repo
        acct = (trading_runtime_repo.load_runtime() or {}).get('account')
    except Exception:
        acct = None
    return load_strategy_config_cached(acct, ttl)
