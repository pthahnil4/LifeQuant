#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
固定币种池刷新：替换为当前市值 Top50 热门币种（命令行入口）
==============================================================================
核心逻辑已抽到 crypto/market_cap_updater.py（与 /task 交易配置页的
「🔄 更新市值列表」按钮同源），本脚本只做 CLI 包装，口径说明见该模块文件头。

安全：只读行情接口 + 本地配置/DB 写入，不下单、不动交易状态。
运行：
    python data/update_fixed_coins_top50.py            # 正式更新（config+DB+宇宙）
    python data/update_fixed_coins_top50.py --dry-run  # 只算不改
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, 'crypto')):
    if p not in sys.path:
        sys.path.insert(0, p)

import market_cap_updater as mcu   # noqa: E402


def main():
    dry_run = '--dry-run' in sys.argv

    print('== 1/3 拉取市值榜与 OKX 合约列表 ==')
    rows = mcu.fetch_marketcap_top()
    okx_insts = mcu.fetch_okx_swap_insts()
    print(f'  市值榜 {len(rows)} 个 | OKX USDT 线性永续 {len(okx_insts)} 个')

    new_fixed, ranks = mcu.build_top_fixed_coins(rows, okx_insts)
    print(f'== 2/3 目标固定池 {len(new_fixed)} 个 ==')
    print('  ' + '、'.join(c.replace('-USDT-SWAP', '') for c in new_fixed))

    if dry_run:
        print('== 3/3 [dry-run] 未写入 ==')
        return

    print('== 3/3 应用更新 ==')
    result = mcu.refresh_fixed_coins()
    label = lambda cs: [c.replace('-USDT-SWAP', '') for c in cs] or ['无']
    print(f'  移出({len(result["removed"])}): {label(result["removed"])}')
    print(f'  新增({len(result["added"])}): {label(result["added"])}')
    print(f'  宇宙新增 {len(result["universe_added"])} 个 | DB 主存: '
          f'{"写入成功" if result["db_ok"] else "!! 写入失败，仅本地文件生效，DB 仍是旧值 !!"}')


if __name__ == '__main__':
    main()
