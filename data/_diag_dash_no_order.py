# -*- coding: utf-8 -*-
"""只读诊断：对比 6 币种的挂单槽位/持仓/人工暂停/规格缓存，定位 DASH 为何没挂单。"""
import sys, os, json, time, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from sqlalchemy import text
from crypto.database import session_scope
from crypto import config_store_repo as csr
from crypto import trader_state_repo as state_repo

COINS = ['POL-USDT-SWAP', 'DASH-USDT-SWAP', 'BICO-USDT-SWAP',
         'TRUMP-USDT-SWAP', 'PENGU-USDT-SWAP', 'NEAR-USDT-SWAP']

now = time.time()
with session_scope() as s:
    print(f'当前时间戳={now:.0f}  本地时间={datetime.datetime.now()}')

    # 挂单槽位 + 持仓：全量
    print('\n──── 挂单槽位 pos_slot ────')
    rows = s.execute(text(
        "SELECT inst_id,bucket,slot,state,ord_id,price,amount,placed_ts,acc_filled,dir "
        "FROM pos_slot ORDER BY inst_id,bucket,slot")).fetchall()
    for r in rows:
        age = (now - float(r[7])) / 60 if r[7] else 0
        print(f'  {r[0]:16} {r[1]:6} slot={r[2]} state={r[3]:10} '
              f'price={r[5]} amt={r[6]} dir={r[9]} 已挂{age:.1f}分钟 ord={r[4]}')

    print('\n──── 持仓账本 pos_book ────')
    rows = s.execute(text(
        "SELECT inst_id,bucket,held_long,held_short,avg_px_long,avg_px_short,last_desired "
        "FROM pos_book ORDER BY inst_id,bucket")).fetchall()
    for r in rows:
        if r[2] or r[3]:
            print(f'  {r[0]:16} {r[1]:6} 多={r[2]} 空={r[3]} 均价多={r[4]} 均价空={r[5]} desired={r[6]}')

    print('\n──── 人工暂停 manual_pause ────')
    mp = state_repo.load_manual_pause(s)
    for inst, ts in mp.items():
        remain = (ts - now) / 60
        print(f'  {inst}: 恢复于{datetime.datetime.fromtimestamp(ts)} 剩余{remain:.1f}分钟 '
              f'{"← 仍在暂停!" if ts > now else "(已过期)"}')
    if not mp:
        print('  (无)')

    # 规格缓存
    print('\n──── 合约规格缓存 instrument_spec_cache ────')
    spec = csr.load_json_config(s, csr.KEY_INSTRUMENT_SPEC_CACHE) or {}
    for c in COINS:
        print(f'  {c:16}: {spec.get(c, "★ 缺失")}')
