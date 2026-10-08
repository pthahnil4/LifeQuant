#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OKX Python SDK —— 方法名 ↔ REST 端点 反查脚本（证据重生成）

用法: python data/okx_sdk_probe.py
用途: 校验《OKX交易操作能力清单》里 "SDK 方法" 与 "REST 端点" 两列。
     SDK 用 consts.py 里的常量拼端点，所以要先建 常量名->端点 映射，
     再逐个方法体找它引用的常量。
"""
import os
import re
import sys

try:
    import okx
except ImportError:
    sys.exit("未安装 OKX Python SDK: pip install python-okx")

BASE = os.path.dirname(okx.__file__)

# 1) consts.py: NAME = '/api/v5/...'
consts = {}
const_src = open(os.path.join(BASE, 'consts.py'), encoding='utf-8').read()
for m in re.finditer(r"^([A-Z][A-Z0-9_]*)\s*=\s*['\"](/api/v5/[^'\"]+)['\"]", const_src, re.M):
    consts[m.group(1)] = m.group(2)

VERB = re.compile(r"\b(GET|POST)\b")

WANTED = {
    'Trade.py': ['place_order', 'place_multiple_orders', 'cancel_order', 'cancel_multiple_orders',
                 'amend_order', 'amend_multiple_orders', 'close_positions', 'get_order', 'get_order_list',
                 'get_orders_history', 'get_orders_history_archive', 'get_fills',
                 'place_algo_order', 'cancel_algo_order', 'amend_algo_order',
                 'order_algos_list', 'order_algos_history', 'get_algo_order_details'],
    'Account.py': ['set_leverage', 'get_leverage', 'set_position_mode', 'adjustment_margin',
                   'set_isolated_mode', 'get_positions', 'get_positions_history',
                   'get_max_order_size', 'get_max_avail_size', 'get_account_config',
                   'get_simulated_margin', 'borrow_repay', 'get_fee_rates'],
    'Grid.py': ['grid_order_algo', 'grid_amend_order_algo', 'grid_stop_order_algo',
                'grid_orders_algo_pending', 'grid_orders_algo_details', 'grid_sub_orders',
                'grid_positions', 'grid_adjust_margin_balance', 'grid_compute_margin_balance',
                'place_recurring_buy_order', 'amend_recurring_buy_order', 'stop_recurring_buy_order'],
}


def method_index(src):
    idx = {}
    lines = src.split('\n')
    for i, line in enumerate(lines):
        m = re.match(r'    def (\w+)\(', line)
        if m and m.group(1) not in idx:
            idx[m.group(1)] = i
    return idx, lines


missing_report = []
for fname, names in WANTED.items():
    path = os.path.join(BASE, fname)
    if not os.path.exists(path):
        print("!! 缺少模块文件 %s" % fname)
        continue
    src = open(path, encoding='utf-8').read()
    idx, lines = method_index(src)
    print('=' * 78)
    print(fname)
    print('=' * 78)
    for name in names:
        if name not in idx:
            print('  %-32s !! SDK 中不存在该方法（清单若引用需删除）' % name)
            missing_report.append('%s.%s' % (fname, name))
            continue
        start = idx[name]
        nxt = [v for v in idx.values() if v > start]
        end = min(nxt) if nxt else len(lines)
        body = '\n'.join(lines[start:end])
        used = [consts[c] for c in re.findall(r'\b([A-Z][A-Z0-9_]{3,})\b', body) if c in consts]
        verb = VERB.search(body.replace("import *", ""))
        ep = used[0] if used else '-'
        if len(used) > 1:
            ep += '   (同体内其他端点: %s)' % ', '.join(used[1:])
        print('  %-32s %-5s %s' % (name, verb.group(1) if verb else '?', ep))

if missing_report:
    print('\n!!! 以下方法在本机 SDK 版本中不存在，需回改清单: %s' % ', '.join(missing_report))
else:
    print('\n[OK] 清单中引用的全部 SDK 方法均已在 v%s 实测存在' %
          getattr(__import__('okx'), '__version__', 'unknown'))
