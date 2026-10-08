#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比「定时任务交易配置币种」与「实盘分析记录币种」差异。

只读诊断脚本：
  1. 列出 kv_store 中所有 strategy_config* 键（全局 + 各账号）及其币种；
  2. 列出 task_analysis_records 中出现过的币种及记录数；
  3. 打印两者的集合差异（分析记录里有、但当前交易配置已不含的历史币种）。

用法： python data/_dump_coin_lists.py
"""
import os
import sys
import json

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import text  # noqa: E402
from crypto.database import session_scope  # noqa: E402


def dump_trading_configs():
    """返回 {key: [instId, ...]}，含全局与所有账号专属 strategy_config。"""
    out = {}
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT `key`, value FROM kv_store WHERE `key` LIKE 'strategy_config%'"
        )).all()
    for key, value in rows:
        try:
            cfg = json.loads(value or '{}')
            coins = [c.get('instId') for c in (cfg.get('currencies') or [])
                     if c.get('instId')]
        except Exception as e:  # 非法 JSON 时不炸，原样报告
            coins = [f'<解析失败: {e}>']
        out[key] = coins
    return out


def dump_analysis_coins():
    """返回 [(inst_id, count, first_ts, last_ts), ...]。"""
    with session_scope() as s:
        return s.execute(text(
            "SELECT inst_id, COUNT(*), MIN(ts), MAX(ts) "
            "FROM task_analysis_records GROUP BY inst_id ORDER BY inst_id"
        )).all()


def main():
    configs = dump_trading_configs()
    print('==== 定时任务交易配置（kv_store, DB 权威值）====')
    if not configs:
        print('  (kv_store 无任何 strategy_config 键)')
    # 汇总所有配置里出现过的币种（跨全局+各账号），作为"当前配置币种"并集
    all_cfg_coins = set()
    for key, coins in sorted(configs.items()):
        print(f'  [{key}] {len(coins)} 个: {coins}')
        all_cfg_coins.update(c for c in coins if not c.startswith('<'))

    analysis = dump_analysis_coins()
    print('\n==== 实盘分析记录币种（task_analysis_records）====')
    ana_coins = set()
    for inst, cnt, first, last in analysis:
        ana_coins.add(inst)
        print(f'  {inst}: {cnt} 条  ({first} ~ {last})')
    if not analysis:
        print('  (暂无分析记录)')

    print('\n==== 差异对比 ====')
    only_in_analysis = sorted(ana_coins - all_cfg_coins)
    only_in_config = sorted(all_cfg_coins - ana_coins)
    print(f'  仅在分析记录出现（交易配置已不含的历史币种）: {only_in_analysis or "无"}')
    print(f'  仅在交易配置出现（还没做过分析记录）        : {only_in_config or "无"}')


if __name__ == '__main__':
    main()
