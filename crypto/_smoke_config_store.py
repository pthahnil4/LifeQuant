#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试 — 批次7a：策略配置 / 币种自选整份存入 kv_store
=========================================================
覆盖：
  A 组  config_store_repo 存取语义（round-trip / 缺省 / 损坏 / 覆盖 / 删除）
  B 组  real_strategy_adapter 切库（DB 优先 + 文件兜底）
  C 组  trend_range_trader._load_config 切库（DB 优先 + 文件兜底）
  D 组  迁移结果完整性（DB 数据与源文件深度相等）

沙箱约定：先快照 kv_store 中 strategy_config / coin_selection 两行，
清场跑用例，finally 无条件恢复。
"""

import os
import sys
import json
import types
import atexit

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import select, delete  # noqa: E402

from crypto.database import init_db, session_scope  # noqa: E402
from crypto.models import KVStore  # noqa: E402
from crypto import config_store_repo as repo  # noqa: E402

init_db()

_PASSED, _FAILED = 0, 0


def ok(name, cond):
    global _PASSED, _FAILED
    if cond:
        _PASSED += 1
        print(f'  [PASS] {name}')
    else:
        _FAILED += 1
        print(f'  [FAIL] {name}')


_KEYS = (repo.KEY_STRATEGY_CONFIG, repo.KEY_COIN_SELECTION)
_STRATEGY_FILE = os.path.join(_HERE, 'task', 'config', 'config_trend_range.json')
_COIN_FILE = os.path.join(_HERE, 'config.json')


def snapshot():
    with session_scope() as s:
        rows = s.execute(select(KVStore).where(KVStore.key.in_(_KEYS))).scalars().all()
        return [(r.key, r.value) for r in rows]


def restore(snap):
    with session_scope() as s:
        s.execute(delete(KVStore).where(KVStore.key.in_(_KEYS)))
        for k, v in snap:
            s.add(KVStore(key=k, value=v))
        s.flush()


_SNAP = snapshot()
atexit.register(restore, _SNAP)

try:
    # ================= A 组：repo 存取语义 =================
    print('A 组：config_store_repo 存取语义')
    with session_scope() as s:
        for k in _KEYS:
            repo.delete_json_config(s, k)

    sample = {
        'name': '测试配置',
        'nested': {'a': [3, 1, 2], 'b': {'c': 0.123456789}},
        'list_order': ['x', 'y', 'z'],
        'flag': True,
        'empty': {},
    }
    with session_scope() as s:
        repo.save_json_config(s, repo.KEY_STRATEGY_CONFIG, sample)
    with session_scope() as s:
        back = repo.load_json_config(s, repo.KEY_STRATEGY_CONFIG)
    ok('A1 整份 round-trip 深度相等', back == sample)
    ok('A2 中文与嵌套结构保真', back['name'] == '测试配置' and back['nested']['b']['c'] == 0.123456789)
    ok('A3 列表顺序保持', back['list_order'] == ['x', 'y', 'z'])

    with session_scope() as s:
        ok('A4 不存在的键返回 None', repo.load_json_config(s, 'no_such_key_xyz') is None)

    # 损坏的 JSON 文本 → None（模拟脏数据容错）
    with session_scope() as s:
        s.add(KVStore(key='corrupt_probe', value='{bad json'))
    with session_scope() as s:
        ok('A5 损坏 JSON 返回 None', repo.load_json_config(s, 'corrupt_probe') is None)
    with session_scope() as s:
        s.execute(delete(KVStore).where(KVStore.key == 'corrupt_probe'))

    # value 为合法 JSON 但非 dict → None
    with session_scope() as s:
        s.add(KVStore(key='list_probe', value='[1, 2]'))
    with session_scope() as s:
        ok('A6 非 dict JSON 返回 None', repo.load_json_config(s, 'list_probe') is None)
    with session_scope() as s:
        s.execute(delete(KVStore).where(KVStore.key == 'list_probe'))

    with session_scope() as s:
        repo.save_json_config(s, repo.KEY_STRATEGY_CONFIG, {'v': 2})
    with session_scope() as s:
        ok('A7 覆盖写生效', repo.load_json_config(s, repo.KEY_STRATEGY_CONFIG) == {'v': 2})

    with session_scope() as s:
        repo.delete_json_config(s, repo.KEY_STRATEGY_CONFIG)
    with session_scope() as s:
        ok('A8 删除后读取为 None', repo.load_json_config(s, repo.KEY_STRATEGY_CONFIG) is None)
        repo.delete_json_config(s, repo.KEY_STRATEGY_CONFIG)  # 重复删除静默
    ok('A9 重复删除不抛异常', True)

    # ================= B 组：real_strategy_adapter 切库 =================
    print('B 组：real_strategy_adapter（币种自选）切库')
    from crypto import real_strategy_adapter as rsa  # noqa: E402

    with open(_COIN_FILE, 'r', encoding='utf-8') as f:
        file_cfg = json.load(f)

    # B1 DB 不可用 → 回退本地文件
    _saved_ss, _saved_repo = rsa._db_session_scope, rsa._config_store_repo
    rsa._db_session_scope, rsa._config_store_repo = None, None
    try:
        fallback_cfg = rsa.load_config()
        ok('B1 DB 不可用时回退文件且字段完整',
           fallback_cfg.get('all_coins') == file_cfg.get('all_coins')
           and 'starred_coins' in fallback_cfg and 'floating_coins' in fallback_cfg)
    finally:
        rsa._db_session_scope, rsa._config_store_repo = _saved_ss, _saved_repo

    # B2 DB 优先：写入标记后读取应命中 DB（而非文件）
    marker = dict(file_cfg)
    marker['_smoke_marker'] = 'from_db'
    with session_scope() as s:
        repo.save_json_config(s, repo.KEY_COIN_SELECTION, marker)
    ok('B2 DB 有数据时优先读 DB', rsa.load_config().get('_smoke_marker') == 'from_db')

    # B3 DB 键缺失 → 回退文件
    with session_scope() as s:
        repo.delete_json_config(s, repo.KEY_COIN_SELECTION)
    ok('B3 DB 无数据时回退文件', '_smoke_marker' not in rsa.load_config())

    # ================= C 组：trend_range_trader._load_config 切库 =================
    print('C 组：trend_range_trader._load_config（交易配置热加载）切库')
    from crypto.task.trend_range_trader import TrendRangeTrader  # noqa: E402

    fake = types.SimpleNamespace(config_path=_STRATEGY_FILE)
    with open(_STRATEGY_FILE, 'r', encoding='utf-8') as f:
        file_strategy = json.load(f)

    # C1 DB 优先：标记配置命中 DB
    marker_cfg = {'_smoke_marker': 'strategy_db', 'global_settings': {}}
    with session_scope() as s:
        repo.save_json_config(s, repo.KEY_STRATEGY_CONFIG, marker_cfg)
    got = TrendRangeTrader._load_config(fake)
    ok('C1 DB 有数据时优先读 DB', got.get('_smoke_marker') == 'strategy_db')

    # C2 DB 键缺失 → 回退本地文件（与文件深度相等）
    with session_scope() as s:
        repo.delete_json_config(s, repo.KEY_STRATEGY_CONFIG)
    got = TrendRangeTrader._load_config(fake)
    ok('C2 DB 无数据时回退文件且深度相等', got == file_strategy)

    # C3 DB 与文件均不可用 → 返回 {}（不抛异常，交易循环继续）
    fake2 = types.SimpleNamespace(config_path=os.path.join(_HERE, 'no_such_config.json'))
    ok('C3 DB 与文件均缺失时返回空 dict', TrendRangeTrader._load_config(fake2) == {})

    # ================= D 组：迁移结果完整性 =================
    print('D 组：迁移结果完整性（DB 数据 == 源文件）')
    restore(_SNAP)  # 恢复真实迁移数据后再校验
    with session_scope() as s:
        ok('D1 strategy_config 与源文件深度相等',
           repo.load_json_config(s, repo.KEY_STRATEGY_CONFIG) == file_strategy)
        ok('D2 coin_selection 与源文件深度相等',
           repo.load_json_config(s, repo.KEY_COIN_SELECTION) == file_cfg)
    ok('D3 _comment_* 注释键完整保留',
       all(k.startswith('_comment_') for k in file_strategy if k.startswith('_'))
       and sum(1 for k in file_strategy if k.startswith('_comment_')) >= 10)
finally:
    restore(_SNAP)

print('=' * 50)
print(f'结果: {_PASSED} 通过 / {_FAILED} 失败')
sys.exit(1 if _FAILED else 0)
