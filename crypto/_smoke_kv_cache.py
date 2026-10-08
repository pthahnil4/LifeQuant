#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试 — 批次8：缓存类整份存入 kv_store（instrument_spec_cache / market_scan_cache）
========================================================================================
覆盖：
  A 组  config_store_repo 缓存键存取语义（round-trip / 缺省 / 覆盖 / 删除）
  B 组  instrument_spec.InstrumentSpecCache 切库（DB 优先读 + 文件兜底 + 双写）
  C 组  market_scanner._load/_save_scan_cache 切库（DB 优先 + 文件兜底 + 双写）
  D 组  迁移结果完整性（DB 数据与源文件深度相等）

沙箱约定：先快照 kv_store 中两个缓存键，清场跑用例，finally/atexit 无条件恢复；
磁盘缓存文件（外置目录）先备份，用例后恢复。
"""

import os
import sys
import json
import shutil
import atexit

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import delete as _sa_delete

from crypto.database import session_scope
from crypto.models import KVStore
from crypto import config_store_repo as repo

_PASS = 0
_FAIL = 0


def ok(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f'  ✅ {name}')
    else:
        _FAIL += 1
        print(f'  ❌ {name}')


KEY_SPEC = repo.KEY_INSTRUMENT_SPEC_CACHE
KEY_SCAN = repo.KEY_MARKET_SCAN_CACHE
_CACHE_KEYS = (KEY_SPEC, KEY_SCAN)

# ---------------- 沙箱：快照 kv_store 两个缓存键 ----------------

def _snapshot():
    snap = {}
    with session_scope() as s:
        for key in _CACHE_KEYS:
            kv = s.get(KVStore, key)
            snap[key] = kv.value if kv is not None else None
    return snap


def _restore(snap):
    with session_scope() as s:
        for key, value in snap.items():
            if value is None:
                s.execute(_sa_delete(KVStore).where(KVStore.key == key))
            else:
                kv = s.get(KVStore, key)
                if kv is None:
                    s.add(KVStore(key=key, value=value))
                else:
                    kv.value = value


def _clear_keys():
    with session_scope() as s:
        s.execute(_sa_delete(KVStore).where(KVStore.key.in_(_CACHE_KEYS)))


_SNAP = _snapshot()
atexit.register(_restore, _SNAP)
print(f'[Smoke] 已快照 kv_store 缓存键: '
      + ', '.join(f'{k}={"有" if _SNAP[k] is not None else "无"}' for k in _CACHE_KEYS))

# ---------------- 源文件定位（与迁移脚本同口径） ----------------

from crypto.data_paths import resolve_data_dir

_DATA_DIR = resolve_data_dir()
SPEC_FILE = os.path.join(_DATA_DIR, 'instrument_spec_cache.json')
SCAN_FILE = os.path.join(_DATA_DIR, 'market_scan_cache.json')

# 备份两份磁盘缓存文件（B/C 组双写用例会覆盖它们）
_SPEC_BAK = SPEC_FILE + '.smoke.bak'
_SCAN_BAK = SCAN_FILE + '.smoke.bak'
for _src, _bak in ((SPEC_FILE, _SPEC_BAK), (SCAN_FILE, _SCAN_BAK)):
    if os.path.isfile(_src):
        shutil.copy2(_src, _bak)


_FILES_RESTORED = False


def _restore_files():
    global _FILES_RESTORED
    if _FILES_RESTORED:
        return
    _FILES_RESTORED = True
    for _src, _bak in ((SPEC_FILE, _SPEC_BAK), (SCAN_FILE, _SCAN_BAK)):
        if os.path.isfile(_bak):
            shutil.move(_bak, _src)


atexit.register(_restore_files)


def main():
    import crypto.task.utils.instrument_spec as ins_spec
    import crypto.market_scanner as mscan

    # ================= A 组：缓存键存取语义 =================
    print('A 组：kv_store 缓存键存取语义')
    _clear_keys()
    with session_scope() as s:
        ok('A1 不存在键读取返回 None', repo.load_json_config(s, KEY_SPEC) is None)
        repo.save_json_config(s, KEY_SPEC, {'BTC-USDT-SWAP': {'ct_val': 0.01}})
    with session_scope() as s:
        d = repo.load_json_config(s, KEY_SPEC)
        ok('A2 round-trip 相等',
           d == {'BTC-USDT-SWAP': {'ct_val': 0.01}})
        repo.save_json_config(s, KEY_SPEC, {'ETH-USDT-SWAP': {'ct_val': 0.1}})
    with session_scope() as s:
        d = repo.load_json_config(s, KEY_SPEC)
        ok('A3 整份覆盖（旧键消失）',
           d == {'ETH-USDT-SWAP': {'ct_val': 0.1}})
        repo.delete_json_config(s, KEY_SPEC)
        s.expire_all()  # 避免 identity map 残留刚删除的对象
        ok('A4 删除后读取返回 None', repo.load_json_config(s, KEY_SPEC) is None)
    # 损坏 value 容错
    with session_scope() as s:
        s.add(KVStore(key=KEY_SPEC, value='{bad json'))
    with session_scope() as s:
        ok('A5 损坏 JSON 返回 None', repo.load_json_config(s, KEY_SPEC) is None)
    _clear_keys()

    # ================= B 组：instrument_spec 切库 =================
    print('B 组：InstrumentSpecCache 切库（DB 优先 + 文件兜底 + 双写）')
    _clear_keys()
    # DB 写入一条规格 → 构造缓存应优先从 DB 加载
    db_spec = {'TEST-USDT-SWAP': {'ct_val': 1.0, 'ct_val_ccy': 'TEST', 'lot_sz': 1.0,
                                  'min_sz': 1.0, 'tick_sz': 0.0001, 'settle_ccy': 'USDT',
                                  'max_lever': 50.0, 'fetched_ts': 1787000000.0}}
    with session_scope() as s:
        repo.save_json_config(s, KEY_SPEC, db_spec)
    cache = ins_spec.InstrumentSpecCache(cache_file=SPEC_FILE)
    ok('B1 DB 优先加载（含 TEST 键）', cache._mem.get('TEST-USDT-SWAP', {}).get('ct_val') == 1.0)

    # DB 键删除 → 回退磁盘文件（备份文件中是真实迁移数据）
    _clear_keys()
    cache2 = ins_spec.InstrumentSpecCache(cache_file=SPEC_FILE)
    ok('B2 DB 无数据回退磁盘文件', 'TEST-USDT-SWAP' not in cache2._mem
       and len(cache2._mem) > 0)

    # 模拟 DB 不可用（模块级变量置 None）→ 仍从文件加载
    _orig_ss, _orig_repo = ins_spec._db_session_scope, ins_spec._config_store_repo
    ins_spec._db_session_scope = None
    ins_spec._config_store_repo = None
    try:
        cache3 = ins_spec.InstrumentSpecCache(cache_file=SPEC_FILE)
        ok('B3 DB 不可用回退磁盘文件', len(cache3._mem) > 0)
    finally:
        ins_spec._db_session_scope, ins_spec._config_store_repo = _orig_ss, _orig_repo

    # 双写：手动触发 _save_disk → DB 与文件都更新
    cache2._mem['DUAL-USDT-SWAP'] = dict(db_spec['TEST-USDT-SWAP'], ct_val_ccy='DUAL')
    cache2._save_disk()
    with session_scope() as s:
        d = repo.load_json_config(s, KEY_SPEC)
    ok('B4 双写：DB 已更新', d is not None and 'DUAL-USDT-SWAP' in d)
    with open(SPEC_FILE, 'r', encoding='utf-8') as f:
        fd = json.load(f)
    ok('B5 双写：磁盘文件同步更新', 'DUAL-USDT-SWAP' in fd)

    # ================= C 组：market_scanner 切库 =================
    print('C 组：market_scanner 扫描缓存切库（DB 优先 + 文件兜底 + 双写）')
    _clear_keys()
    scan_fake = {'rankings': {'gainers': []}, 'generated_at': 'SMOKE'}
    with session_scope() as s:
        repo.save_json_config(s, KEY_SCAN, scan_fake)
    mscan._scan_result = None
    mscan._load_scan_cache()
    ok('C1 DB 优先加载', isinstance(mscan._scan_result, dict)
       and mscan._scan_result.get('generated_at') == 'SMOKE')

    # DB 键删除 → 回退磁盘文件
    _clear_keys()
    mscan._scan_result = None
    mscan._load_scan_cache()
    ok('C2 DB 无数据回退磁盘文件',
       isinstance(mscan._scan_result, dict)
       and mscan._scan_result.get('generated_at') != 'SMOKE'
       and 'rankings' in mscan._scan_result)

    # 双写：set_scan_result → 内存 + DB + 文件
    mscan.set_scan_result({'rankings': {}, 'generated_at': 'SMOKE2'})
    with session_scope() as s:
        d = repo.load_json_config(s, KEY_SCAN)
    ok('C3 双写：DB 已更新', d is not None and d.get('generated_at') == 'SMOKE2')
    with open(SCAN_FILE, 'r', encoding='utf-8') as f:
        fd = json.load(f)
    ok('C4 双写：磁盘文件同步更新', fd.get('generated_at') == 'SMOKE2')

    # ================= D 组：迁移结果完整性 =================
    print('D 组：迁移结果完整性（DB 数据 == 源文件）')
    _restore(_SNAP)  # 恢复真实迁移数据后再校验
    _restore_files()  # 磁盘文件也恢复为迁移时刻的原始内容
    for key, src in ((KEY_SPEC, SPEC_FILE), (KEY_SCAN, SCAN_FILE)):
        with open(src, 'r', encoding='utf-8') as f:
            src_data = json.load(f)
        with session_scope() as s:
            db_data = repo.load_json_config(s, key)
        ok(f'D1 {key} 与源文件深度相等', db_data == src_data)

    # ================= 收尾 =================
    print()
    print(f'通过 {_PASS} / 失败 {_FAIL}')
    if _FAIL == 0:
        print('ALL SMOKE TESTS PASSED (kv_cache on MySQL)')
    else:
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    finally:
        _restore(_SNAP)
        _restore_files()
