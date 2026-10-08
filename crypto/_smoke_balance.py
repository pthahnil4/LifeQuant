#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账户余额历史模块冒烟测试（MySQL 版，迁移批次4）

测试对象：balance_repo 数据访问层 + api_routes 快照/回溯辅助函数
（完整 API 依赖 OKX 实时接口，不在冒烟范围）

测试隔离策略（保护真实数据）：
  1. 运行前快照 balance_history 全表
  2. 清空后执行全部用例
  3. finally 中无条件恢复快照（无论用例成败）

前置条件：环境变量 CRYPTO_TEST_DB_URL 指向专用隔离测试库。
未配置或不合规时退出码 2 —— 本用例会清空 balance_history，绝不允许在业务库上跑。
"""

import os
import sys
import time

# Windows 终端默认 GBK，强制 UTF-8 输出避免中文/emoji 乱码报错
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crypto.test_isolation import (  # noqa: E402
    require_isolated_test_db, ensure_test_schema, TestDbNotConfigured)

try:
    _TEST_DB_URL = require_isolated_test_db()
except TestDbNotConfigured as e:
    print(f'❌ {e}')
    sys.exit(2)

from sqlalchemy import select, delete  # noqa: E402
from crypto import api_routes as ar  # noqa: E402
from crypto import balance_repo as repo  # noqa: E402
from crypto.database import session_scope  # noqa: E402
from crypto.models import BalanceHistory  # noqa: E402

print(f"[Smoke] 目标数据库（隔离测试库）: {_TEST_DB_URL.split('@')[-1]}")
# 表结构补齐：全新隔离库（或本地 SQLite 文件）首次跑时表还不存在；只建表不动数据
ensure_test_schema()

_ACC = 'smoke_test_acc'
_NOW_MS = int(time.time() * 1000)

_passed = 0
_failed = 0


def check(desc, ok):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✅ {desc}")
    else:
        _failed += 1
        print(f"  ❌ {desc}")


# =============================================================================
# 真实数据快照 + 清场（测试不污染生产数据）
# =============================================================================

def _row_dict(row):
    d = dict(row.__dict__)
    d.pop('_sa_instance_state', None)
    return d


def _snapshot():
    with session_scope() as s:
        return [_row_dict(r) for r in s.execute(
            select(BalanceHistory).order_by(
                BalanceHistory.account_key, BalanceHistory.ts)).scalars()]


def _clean():
    with session_scope() as s:
        s.execute(delete(BalanceHistory))


def _restore(rows):
    with session_scope() as s:
        s.execute(delete(BalanceHistory))
        for r in rows:
            s.add(BalanceHistory(**r))
    with session_scope() as s:
        back = [_row_dict(r) for r in s.execute(
            select(BalanceHistory).order_by(
                BalanceHistory.account_key, BalanceHistory.ts)).scalars()]
    return back == rows


def _points():
    """读取测试账号当前点列表"""
    with session_scope() as s:
        return repo.load_account_points(s, _ACC)


# =============================================================================
# 用例
# =============================================================================

def run_cases():
    # ---- 1. 回溯初始化（空库首次查询）----
    print('\n=== 1. 回溯初始化（历史点不足自动补齐）===')
    added = ar._backfill_balance_history(_ACC, '冒烟账号', _NOW_MS, 100.0, days=7)
    check('回溯生成 7 天历史点', added == 7)
    pts = _points()
    check('库中点数=7', len(pts) == 7)
    check('全部为 backfill 来源', all(p.get('source') == 'backfill' for p in pts))
    check('按 ts 升序', pts == sorted(pts, key=lambda x: x['ts']))
    check('衰减公式正确（最旧点=0.95^7）',
          pts[0]['balance'] == round(100.0 * 0.95 ** 7, 4))
    check('最新回溯点=昨天', pts[-1]['ts'] == _NOW_MS - 86400000)

    # ---- 2. 回溯幂等（历史点已达阈值不再补）----
    print('\n=== 2. 回溯阈值控制 ===')
    added2 = ar._backfill_balance_history(_ACC, '冒烟账号', _NOW_MS, 200.0, days=7)
    check('已达5点阈值，回溯返回 0', added2 == 0)
    check('点数未变', len(_points()) == 7)

    # ---- 3. 追加快照（同 ts 去重/覆盖）----
    print('\n=== 3. 追加快照 ===')
    ar._append_balance_snapshot(_ACC, _NOW_MS, 123.4567)
    pts = _points()
    check('追加快照后点数=8', len(pts) == 8)
    check('快照点不带 source 键（对齐 JSON 形态）',
          'source' not in pts[-1])
    check('快照点 ts/balance 正确',
          pts[-1]['ts'] == _NOW_MS and pts[-1]['balance'] == 123.4567)

    ar._append_balance_snapshot(_ACC, _NOW_MS, 111.1111)
    pts = _points()
    check('同 ts 重查不新增点', len(pts) == 8)
    check('同 ts 余额被覆盖', pts[-1]['balance'] == 111.1111)

    # ---- 4. 超上限裁剪 ----
    print('\n=== 4. 超上限自动裁剪（临时调小上限）===')
    old_max = ar._BAL_HISTORY_MAX_POINTS
    ar._BAL_HISTORY_MAX_POINTS = 5
    try:
        ar._append_balance_snapshot(_ACC, _NOW_MS + 1000, 99.0)
        pts = _points()
        check('超限后裁剪至 5 点', len(pts) == 5)
        check('裁剪掉的是最旧的点', pts[0]['ts'] >= _NOW_MS - 3 * 86400000)
        check('最新点仍在', pts[-1]['ts'] == _NOW_MS + 1000)
    finally:
        ar._BAL_HISTORY_MAX_POINTS = old_max

    # ---- 5. 多账号隔离 + 全量读取 ----
    print('\n=== 5. 多账号隔离 ===')
    ar._append_balance_snapshot('smoke_other_acc', _NOW_MS, 66.66)
    with session_scope() as s:
        all_data = repo.load_all(s)
    accs = all_data.get('accounts', {})
    check('load_all 含两个测试账号',
          _ACC in accs and 'smoke_other_acc' in accs)
    check('账号间数据隔离',
          len(accs['smoke_other_acc']) == 1
          and accs['smoke_other_acc'][0]['balance'] == 66.66)
    check('count_points 单账号计数',
          _count(_ACC) == 5 and _count('smoke_other_acc') == 1)

    # ---- 6. repo 基础操作边界 ----
    print('\n=== 6. repo 边界操作 ===')
    with session_scope() as s:
        n = repo.trim_oldest(s, 'smoke_other_acc', 100)
    check('未超限 trim 返回 0', n == 0)
    with session_scope() as s:
        repo.upsert_point(s, _ACC, _NOW_MS, 77.77, source='snapshot')
    pts = _points()
    check('upsert 覆盖生效', any(
        p['ts'] == _NOW_MS and p['balance'] == 77.77 for p in pts))


def _count(acc):
    with session_scope() as s:
        return repo.count_points(s, acc)


# =============================================================================
# 主流程
# =============================================================================

if __name__ == '__main__':
    snapshot = _snapshot()
    real_accounts = {r['account_key'] for r in snapshot}
    print(f"[Smoke] 已快照真实数据: {len(snapshot)} 个快照点"
          f"（账号: {sorted(real_accounts) or '无'}），测试后自动恢复")
    if _ACC in real_accounts or 'smoke_other_acc' in real_accounts:
        print('❌ 测试账号与真实数据冲突，中止')
        sys.exit(3)

    try:
        _clean()
        run_cases()
    finally:
        print('\n=== 恢复真实数据快照 ===')
        ok = _restore(snapshot)
        check(f'恢复校验: 快照点 {len(snapshot)}/{len(snapshot)}', ok)

    print(f"\n通过 {_passed} / 失败 {_failed}")
    if _failed:
        sys.exit(1)
    print('ALL SMOKE TESTS PASSED (balance module on MySQL)')
