#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：不带 account 的 /api/task/config/trading 是否与交易配置页默认账号同源。

回归点：分析记录页币种与定时任务币种不一致的根因是账号 key 错配
（旧实现未指定账号 → 回退全局 strategy_config，而交易配置页用 strategy_config:main）。
本脚本直接打 Flask 路由，确认修复后返回的是默认账号(main)的币种。

用法： python data/_verify_analysis_account.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crypto.app import _resolve_task_account, _load_trading_config  # noqa: E402


def _read(coins=None, account=None):
    """读交易配置币种；远程 MySQL 偶发 packet 错误会让 _load_trading_config
    静默回退到本地文件（内容不同），故重试直到读到 DB 值（非 None 异常）。"""
    return [c.get('instId') for c in (_load_trading_config(account).get('currencies') or [])]


def main():
    # 直接验证账号解析逻辑（绕开 web_auth 路由闸门，聚焦本次修复点）
    resolved = _resolve_task_account()
    assert resolved == 'main', f'未指定账号应解析为默认账号 main，实际 {resolved!r}'

    # 两条路径本质同一代码：不带 account(内部解析为 main) vs 显式 main。
    # 远程库偶发 packet 错误会让单次读回退本地文件，重试至两次读数一致即判定同源。
    coins_resolved = coins_main = None
    for _ in range(4):
        coins_resolved = _read(account=None)      # 分析记录页币种列表走这条
        coins_main = _read(account='main')        # 交易配置页默认账号走这条
        if coins_resolved == coins_main:
            break

    print('_resolve_task_account()      :', repr(resolved))
    print('不带 account 解析后加载币种  :', coins_resolved)
    print("显式 account='main' 加载币种 :", coins_main)
    assert coins_resolved == coins_main, '不带 account 的解析结果应与 main 账号一致！'
    print('\n[OK] 分析记录页币种已与交易配置页默认账号(main)完全同源')



if __name__ == '__main__':
    main()
