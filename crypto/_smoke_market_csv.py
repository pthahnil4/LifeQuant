#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试 — 批次7b：行情 CSV（crypto_coins / star_market）
=============================================================
覆盖：
  A 组  market_data_repo coin 侧语义（整表覆盖/rank 数值排序/中文键/归一化）
  B 组  market_data_repo star 侧语义（行序=插入序/覆盖重建/中文键）
  C 组  调用方切库读路径（batch_trend_updater/get_csv_coins/star_market）
  D 组  DB 不可用时文件兜底
  E 组  写路径 DB+文件双写一致性（文件先备份、finally 恢复）
  F 组  迁移结果完整性（DB 数据 == 源 CSV 文件）

沙箱约定：快照 crypto_coins / star_market 两表 + 备份两份 CSV 文件，
清场跑用例，finally/atexit 无条件恢复。
"""

import os
import sys
import csv
import atexit

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import delete as sa_delete

from crypto.database import session_scope
from crypto import market_data_repo as repo
from crypto.models import CryptoCoin, StarMarketRow

PASS = 0
FAIL = 0


def ok(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✅ {name}')
    else:
        FAIL += 1
        print(f'  ❌ {name}')


COIN_CSV = os.path.join(_HERE, 'crypto_coins.csv')
STAR_CSV = os.path.join(_HERE, 'star币种行情.csv')

COIN_COLS = [
    'rank', 'symbol', 'inst_id', 'name_cn',
    '1H_趋势', '1H_交易价格', '1H_交易时间', '1H_盈亏%', '1H_收盘价',
    'MACD_1H', 'DIF_1H', 'ADX_1H', 'ATR_1H', 'SAR_1H', 'SAR颜色_1H',
    '4H_趋势', '4H_交易价格', '4H_交易时间', '4H_盈亏%', '4H_收盘价',
    'MACD_4H', 'DIF_4H', 'ADX_4H', 'ATR_4H', 'SAR_4H', 'SAR颜色_4H',
    '1D_趋势', '1D_交易价格', '1D_交易时间', '1D_盈亏%', '1D_收盘价',
    'MACD_1D', 'DIF_1D', 'ADX_1D', 'ATR_1D', 'SAR_1D', 'SAR颜色_1D',
]
STAR_COLS = [
    '名称', '代码', '现价', '15分钟', '60分钟', '4小时', '日线',
    '上次交易时间(1H)', '方向(1H)', '上次交易价格(1H)', '策略盈亏(1H)',
    '持仓时间', '预测涨跌', '建议操作',
]


def _snap_table(model):
    with session_scope() as s:
        rows = s.execute(__import__('sqlalchemy').select(model)).scalars().all()
        return [r.to_dict() for r in rows]


def _restore_table(model, rows_dicts, from_row):
    with session_scope() as s:
        s.execute(sa_delete(model))
        for d in rows_dicts:
            s.add(from_row(d))


def _read_csv_file(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def _write_csv_file(path, rows, cols):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


# ---- 快照（表 + 文件）----
_SNAP_COIN = _snap_table(CryptoCoin)
_SNAP_STAR = _snap_table(StarMarketRow)
_BAK_COIN = open(COIN_CSV, 'rb').read()
_BAK_STAR = open(STAR_CSV, 'rb').read()


def restore_all():
    try:
        _restore_table(CryptoCoin, _SNAP_COIN, CryptoCoin.from_row)
        _restore_table(StarMarketRow, _SNAP_STAR, StarMarketRow.from_row)
    finally:
        open(COIN_CSV, 'wb').write(_BAK_COIN)
        open(STAR_CSV, 'wb').write(_BAK_STAR)


atexit.register(restore_all)


def clear_tables():
    with session_scope() as s:
        s.execute(sa_delete(CryptoCoin))
        s.execute(sa_delete(StarMarketRow))


def _mk_coin(rank, symbol):
    return {c: '' for c in COIN_COLS} | {
        'rank': rank, 'symbol': symbol, 'inst_id': f'{symbol}-USDT-SWAP',
        'name_cn': f'{symbol}币', '1H_趋势': '上涨', 'MACD_1H': '-1.5'}


def _mk_star(code, name):
    return {c: '' for c in STAR_COLS} | {'代码': code, '名称': name, '现价': '100.5'}


try:
    # ================= A 组：coin 侧 repo 语义 =================
    print('A 组：market_data_repo coin 侧')
    clear_tables()
    with session_scope() as s:
        # 乱序写入（rank 2,1,3）验证读取按 rank 数值升序
        repo.save_coin_rows(s, [_mk_coin('2', 'ETH'), _mk_coin('1', 'BTC'), _mk_coin('3', 'SOL')])
    with session_scope() as s:
        rows = repo.load_coin_rows(s)
    ok('A1 读取按 rank 数值升序', [r['symbol'] for r in rows] == ['BTC', 'ETH', 'SOL'])
    ok('A2 中文键齐全（37列）', list(rows[0].keys()) == COIN_COLS)
    ok('A3 单元格全字符串', all(isinstance(v, str) for r in rows for v in r.values()))
    ok('A4 值 round-trip 相等', rows[0]['1H_趋势'] == '上涨' and rows[0]['MACD_1H'] == '-1.5')
    with session_scope() as s:
        ok('A5 count 正确', repo.count_coin_rows(s) == 3)
        ids = repo.coin_inst_ids(s)
    ok('A6 coin_inst_ids 保序过滤', ids == ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP'])
    # 整表覆盖：再写 1 行，旧行消失
    with session_scope() as s:
        repo.save_coin_rows(s, [_mk_coin('9', 'DOGE')])
    with session_scope() as s:
        rows = repo.load_coin_rows(s)
    ok('A7 整表覆盖写语义', len(rows) == 1 and rows[0]['symbol'] == 'DOGE')
    # 缺失列归一化
    with session_scope() as s:
        repo.save_coin_rows(s, [{'symbol': 'X', 'inst_id': 'X-USDT-SWAP'}])
    with session_scope() as s:
        rows = repo.load_coin_rows(s)
    ok('A8 缺失列归一为空串', rows[0]['rank'] == '' and rows[0]['SAR_1D'] == ''
       and rows[0]['inst_id'] == 'X-USDT-SWAP')
    # 空表
    with session_scope() as s:
        repo.save_coin_rows(s, [])
    with session_scope() as s:
        ok('A9 空表读写', repo.load_coin_rows(s) == [] and repo.count_coin_rows(s) == 0)

    # ================= B 组：star 侧 repo 语义 =================
    print('B 组：market_data_repo star 侧')
    clear_tables()
    with session_scope() as s:
        repo.save_star_rows(s, [_mk_star('BTC', '比特币'), _mk_star('NEAR', '万物协议'),
                                _mk_star('INJ', '因哲媞')])
    with session_scope() as s:
        rows = repo.load_star_rows(s)
    ok('B1 行序=插入序（拖拽排序基础）', [r['代码'] for r in rows] == ['BTC', 'NEAR', 'INJ'])
    ok('B2 中文键齐全（14列）', list(rows[0].keys()) == STAR_COLS)
    # 覆盖重写重建行序（模拟拖拽排序）
    with session_scope() as s:
        repo.save_star_rows(s, [_mk_star('INJ', '因哲媞'), _mk_star('BTC', '比特币')])
    with session_scope() as s:
        rows = repo.load_star_rows(s)
    ok('B3 覆盖写重建行序', [r['代码'] for r in rows] == ['INJ', 'BTC'] and len(rows) == 2)
    with session_scope() as s:
        ok('B4 count 正确', repo.count_star_rows(s) == 2)

    # ================= C 组：调用方切库读路径 =================
    print('C 组：调用方切库（DB 优先读）')
    restore_all()  # 恢复真实迁移数据
    from crypto import batch_trend_updater as btu
    from crypto import real_strategy_adapter as rsa
    from crypto import star_market as sm
    with session_scope() as s:
        db_rows = repo.load_coin_rows(s)
    ok('C1 batch_trend_updater._read_csv 读DB', btu._read_csv() == db_rows)
    src_coin = sorted(_read_csv_file(COIN_CSV),
                      key=lambda r: int(r['rank']) if r.get('rank', '').isdigit() else 10 ** 9)
    ok('C2 get_csv_coins 与源文件 inst_id 序一致',
       rsa.get_csv_coins() == [r['inst_id'] for r in src_coin if r.get('inst_id', '').strip()])
    with session_scope() as s:
        db_star = repo.load_star_rows(s)
    ok('C3 star_market.read 读DB', sm.read_star_market_data() == db_star)
    ok('C4 star 读结果过滤空代码行', all(r.get('代码', '').strip() for r in sm.read_star_market_data()))

    # ================= D 组：DB 不可用时文件兜底 =================
    print('D 组：文件兜底')
    _ss_btu = btu._db_session_scope
    btu._db_session_scope = None  # 模拟 DB 不可用
    try:
        rows = btu._read_csv()
        ok('D1 _read_csv 回退CSV文件', len(rows) > 0 and 'rank' in rows[0])
    finally:
        btu._db_session_scope = _ss_btu
    _orig_load = repo.load_star_rows
    def _boom(session):
        raise RuntimeError('模拟DB故障')
    repo.load_star_rows = _boom
    try:
        rows = sm.read_star_market_data()
        ok('D2 star_market.read 回退CSV文件', len(rows) > 0 and rows[0].get('代码'))
    finally:
        repo.load_star_rows = _orig_load

    # ================= E 组：写路径 DB+文件双写 =================
    print('E 组：写路径双写一致性')
    # E1: batch_trend_updater._write_csv（改一行后双写）
    rows = btu._read_csv()
    rows[0] = dict(rows[0])
    rows[0]['1H_趋势'] = '横盘测试'
    btu._write_csv(rows)
    with session_scope() as s:
        db_rows = repo.load_coin_rows(s)
    file_rows = _read_csv_file(COIN_CSV)
    file_first = next(r for r in file_rows if r.get('rank') == db_rows[0].get('rank'))
    ok('E1 _write_csv 双写（DB=新值）', db_rows[0]['1H_趋势'] == '横盘测试')
    ok('E2 _write_csv 双写（文件=新值）', file_first.get('1H_趋势') == '横盘测试')
    # E3: star_market.write（返回值契约 + 双写）
    records = sm.read_star_market_data()
    records[0] = dict(records[0])
    records[0]['预测涨跌'] = '看涨测试'
    ret = sm.write_star_market_data(records)
    with session_scope() as s:
        db_star = repo.load_star_rows(s)
    file_star = _read_csv_file(STAR_CSV)
    ok('E3 star write 返回 True（文件写成功）', ret is True)
    ok('E4 star 双写（DB=新值）', db_star[0]['预测涨跌'] == '看涨测试')
    ok('E5 star 双写（文件=新值）', file_star[0].get('预测涨跌') == '看涨测试')
    # E6: update_record 走 DB（允许字段 预测涨跌/建议操作）
    res = sm.update_record(records[0]['代码'], {'建议操作': '观望测试'})
    with session_scope() as s:
        db_star = repo.load_star_rows(s)
    ok('E6 update_record 落DB', res.get('status') == 'success'
       and db_star[0]['建议操作'] == '观望测试')

    # ================= F 组：迁移结果完整性 =================
    print('F 组：迁移结果完整性（DB == 源CSV）')
    restore_all()  # 恢复真实迁移数据
    with session_scope() as s:
        db_rows = repo.load_coin_rows(s)
    diff = []
    for d, src in zip(db_rows, src_coin):
        for col in COIN_COLS:
            if str(d.get(col) or '') != str(src.get(col) or ''):
                diff.append(f"{src.get('symbol')}[{col}]")
    ok('F1 crypto_coins 逐行逐列 == 源文件', not diff and len(db_rows) == len(src_coin))
    with session_scope() as s:
        db_star = repo.load_star_rows(s)
    src_star = [r for r in _read_csv_file(STAR_CSV) if str(r.get('代码') or '').strip()]
    diff = []
    for d, src in zip(db_star, src_star):
        for col in STAR_COLS:
            if str(d.get(col) or '') != str(src.get(col) or ''):
                diff.append(f"{src.get('代码')}[{col}]")
    ok('F2 star_market 逐行逐列 == 源文件', not diff and len(db_star) == len(src_star))

finally:
    restore_all()

print()
print(f'结果: {PASS}/{PASS + FAIL} 通过' + ('  ✅ 全部通过' if FAIL == 0 else f'  ❌ {FAIL} 失败'))
sys.exit(0 if FAIL == 0 else 1)
