#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导航自定义管理 —— 数据访问层
================================
在 kv_store 的固定键 navigation_config:default 上，做「带 revision 的原子读改写」。

口径（见设计文档第 4/6 章）：
- 权威 GET 绕过 config_store 的 30s TTL，直接读一个 KV 行，且必须能区分
  「缺记录（从未保存）」/「损坏数据」/「数据库不可达」三种状态，绝不混为一谈；
- 写：用 patch_json_config(strict=True) 在同一事务里 INSERT 占位 + SELECT FOR UPDATE
  锁行，锁内校验 revision 与结构，冲突/损坏分别抛出，事务提交后才由外层失效缓存；
- 损坏数据不得被普通保存静默覆盖，仅允许「携带 GET 返回的原始指纹」的修复流程覆盖；
- 读路径不建表、不补种子、不写库。
"""

import json
import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from .database import session_scope
from .models import KVStore
from . import config_store_repo
from . import navigation_service as ns

logger = logging.getLogger(__name__)

CONFIG_KEY = ns.CONFIG_KEY
SUPPORTED_SCHEMA = ns.SCHEMA_VERSION


class RevisionConflict(Exception):
    """base_revision 与库中当前 revision 不一致：另一设备/标签已改。"""

    def __init__(self, current_revision):
        super().__init__(f'revision 冲突，当前为 {current_revision}')
        self.current_revision = current_revision


class StoredCorrupt(Exception):
    """库中存量数据损坏/未知结构版本，普通写入被拒绝。"""

    def __init__(self, fingerprint):
        super().__init__('导航配置已损坏，需在管理页确认后修复')
        self.fingerprint = fingerprint


class DbUnavailable(Exception):
    """数据库不可达（连接/引擎层故障）。"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def fingerprint_of(raw_value: str) -> str:
    return hashlib.sha256((raw_value or '').encode('utf-8')).hexdigest()


def _is_blank(value) -> bool:
    v = (value or '').strip()
    return v == '' or v == '{}'


# =============================================================================
# 权威读取（不写库、不补种子）
# =============================================================================
def read_config() -> dict:
    """读取权威配置，返回：
      {status:'ok',  source:'db'|'default', config:<dict>}
      {status:'corrupt', fingerprint:<str>}
      {status:'db_error', error:<str>}

    source='default' 表示从未保存（返回内存默认，revision=0，不写库）。
    """
    try:
        with session_scope() as s:
            row = s.get(KVStore, CONFIG_KEY)
            raw = row.value if row is not None else None
    except Exception as e:                      # 连不上库
        logger.warning('[Nav] 读取配置失败（DB 不可达）: %s', e)
        return {'status': 'db_error', 'error': str(e)}

    if raw is None or _is_blank(raw):
        return {'status': 'ok', 'source': 'default', 'config': ns.build_default_config()}

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {'status': 'corrupt', 'fingerprint': fingerprint_of(raw)}

    if not isinstance(data, dict) or data.get('schema_version') != SUPPORTED_SCHEMA \
            or not isinstance(data.get('items'), list):
        # 未知结构版本 / 缺关键结构 → 视为损坏，禁用普通写入，交由修复流程
        return {'status': 'corrupt', 'fingerprint': fingerprint_of(raw)}

    data.setdefault('revision', 0)
    data.setdefault('settings', ns.default_settings())
    data.setdefault('updated_at', None)
    data.setdefault('last_mutation_id', None)
    return {'status': 'ok', 'source': 'db', 'config': data}


# =============================================================================
# 原子写入（管理页整份保存 / 拖拽移动 / 恢复默认 / 修复损坏）
# =============================================================================
def _finalize(existing: dict, base_revision, mutation_id, new_items, new_settings):
    """锁内公共尾流程：校验 base_revision → 规范化 → 生成新配置。

    existing 为 strict 读到的当前配置 dict（或 None 表示从未保存）。
    new_items/new_settings 已由调用方在事务内用 service 完成校验与规范化。
    """
    current_rev = int((existing or {}).get('revision', 0)) if existing else 0
    if int(base_revision) != current_rev:
        raise RevisionConflict(current_rev)
    return {
        'schema_version': SUPPORTED_SCHEMA,
        'revision': current_rev + 1,
        'updated_at': _utcnow_iso(),
        'last_mutation_id': (str(mutation_id).strip()[:64] if mutation_id else None),
        'settings': new_settings,
        'items': new_items,
    }


def save_full(base_revision, mutation_id, payload) -> dict:
    """整份保存（管理页草稿一次性提交 / 批量增删改）。锁内校验结构 + revision。"""
    def transform(current):
        if current is not None and current.get('schema_version') not in (None, SUPPORTED_SCHEMA):
            raise StoredCorrupt(fingerprint_of(json.dumps(current, ensure_ascii=False)))
        canonical, warnings = ns.validate_and_normalize(payload)
        result = _finalize(current, base_revision, mutation_id, canonical['items'], canonical['settings'])
        result['_warnings'] = warnings
        return result

    with session_scope() as s:
        saved = config_store_repo.patch_json_config(s, CONFIG_KEY, transform, strict=True)
    saved = dict(saved)
    warnings = saved.pop('_warnings', [])
    return {'config': saved, 'warnings': warnings}


def move_item(base_revision, mutation_id, item_id, parent_id, before_id) -> dict:
    """导航直接拖拽的一次原子移动。锁内基于最新 items 计算目标顺序。"""
    def transform(current):
        items = (current or {}).get('items', []) if current else ns.build_default_config()['items']
        moved = ns.apply_move(items, item_id, parent_id, before_id)
        settings = (current or {}).get('settings') or ns.default_settings()
        return _finalize(current, base_revision, mutation_id, moved, settings)

    with session_scope() as s:
        saved = config_store_repo.patch_json_config(s, CONFIG_KEY, transform, strict=True)
    return {'config': saved, 'warnings': []}


def reset_default(base_revision, mutation_id) -> dict:
    """明确确认后恢复默认布局（显式写操作）。"""
    def transform(current):
        canonical, warnings = ns.validate_and_normalize(
            {'settings': ns.default_settings(), 'items': ns.build_default_config()['items']})
        result = _finalize(current, base_revision, mutation_id, canonical['items'], canonical['settings'])
        result['_warnings'] = warnings
        return result

    with session_scope() as s:
        saved = config_store_repo.patch_json_config(s, CONFIG_KEY, transform, strict=True)
    saved = dict(saved)
    warnings = saved.pop('_warnings', [])
    return {'config': saved, 'warnings': warnings}


def repair_save(mutation_id, payload, expected_fingerprint) -> dict:
    """损坏配置的显式修复：仅当当前存量指纹与 GET 返回的 expected_fingerprint 一致时才覆盖。

    普通 save_full 遇损坏会被读阶段拒（不静默覆盖）；修复需要显式指纹前置确认，
    锁内再核对一次，避免并发下把别人刚修复好的配置又盖掉。修复后 revision 从 1 起。
    """
    # 预检（不写）：确认目前仍是损坏态且指纹匹配，否则按对应语义处理
    state = read_config()
    if state['status'] == 'ok':
        # 已不再是损坏态（可能并发已修复）→ 走普通整份保存
        return save_full(state['config'].get('revision', 0), mutation_id, payload)
    if state['status'] != 'corrupt':
        raise DbUnavailable(state.get('error', '数据库不可用'))
    if not expected_fingerprint or state['fingerprint'] != expected_fingerprint:
        raise StoredCorrupt(state['fingerprint'])

    canonical, warnings = ns.validate_and_normalize(payload)

    with session_scope() as s:
        row = s.execute(select(KVStore).where(KVStore.key == CONFIG_KEY)
                        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        cur_raw = row.value if row is not None else None
        # 锁内核对指纹：指纹变了说明并发已改动，拒绝覆盖
        if fingerprint_of(cur_raw) != expected_fingerprint:
            raise StoredCorrupt(fingerprint_of(cur_raw))
        built = {
            'schema_version': SUPPORTED_SCHEMA,
            'revision': 1,
            'updated_at': _utcnow_iso(),
            'last_mutation_id': (str(mutation_id).strip()[:64] if mutation_id else None),
            'settings': canonical['settings'],
            'items': canonical['items'],
        }
        text = json.dumps(built, ensure_ascii=False)
        if row is None:
            s.add(KVStore(key=CONFIG_KEY, value=text))
        else:
            row.value = text
        config_store_repo._invalidate_after_commit(s, CONFIG_KEY)
    return {'config': built, 'warnings': warnings}
