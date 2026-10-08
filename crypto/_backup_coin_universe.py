#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读备份：把生产 MySQL 的 crypto_coins 全币种宇宙导出为带时间戳的 CSV 还原点。

用途：在执行「移除选中/提升为固定」这类会改写 crypto_coins 宇宙（DB表+CSV）
的破坏性操作前，先留一个可还原的快照。本脚本只 SELECT，不写库、不改文件源。

运行： python crypto/_backup_coin_universe.py
产物： crypto/_universe_backup/crypto_coins_db_<时间戳>.csv
"""
import os
import csv
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_HERE, '_universe_backup')


def main():
    try:
        from crypto.database import session_scope
        from crypto import market_data_repo as repo
    except ImportError:
        # 独立运行（仅 crypto 目录在 sys.path）时的兜底导入
        import sys
        sys.path.insert(0, os.path.dirname(_HERE))
        from crypto.database import session_scope
        from crypto import market_data_repo as repo

    with session_scope() as s:
        rows = repo.load_coin_rows(s)

    if not rows:
        print('[备份] DB crypto_coins 为空，未生成文件（可能 DB 不可用或表无数据）')
        return

    os.makedirs(_OUT_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(_OUT_DIR, f'crypto_coins_db_{ts}.csv')
    fieldnames = list(rows[0].keys())
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    inst_ids = [r.get('inst_id', '') for r in rows]
    print(f'[备份] 已导出 {len(rows)} 行 × {len(fieldnames)} 列 → {out_path}')
    print(f'[备份] inst_id 列表: {", ".join(i for i in inst_ids if i)}')


if __name__ == '__main__':
    main()
