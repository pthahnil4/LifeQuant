#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批次6 冒烟测试：结构化成交流水（trade_journal 表）
====================================================
覆盖：
  A. record_fill 写入 + read_journal 读回（字段形态与 JSONL 版一致）
  B. 筛选语义：inst_id / bucket / ts 闭区间
  C. 排序：ts 升序，同 ts 按写入顺序
  D. 容错：read_journal 异常返回 []（不抛）；record_fill 绝不阻断
  E. classify_reason 归因分类（纯函数回归）
  F. 对比引擎消费面：trend_compare 导入的接口签名不变

运行前快照真实数据、清场跑用例、finally 无条件恢复。
用法: python crypto/_smoke_trade_journal.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import select, delete  # noqa: E402
from crypto.database import session_scope  # noqa: E402
from crypto.models import TradeJournal  # noqa: E402
from crypto import trade_journal_repo as repo  # noqa: E402

# ---- 沙箱：快照真实流水 → 清场 → finally 恢复 ----
def _row_dict(row):
    d = dict(row.__dict__)
    d.pop('_sa_instance_state', None)
    return d


print('📸 快照真实成交流水...')
with session_scope() as s:
    _SNAP = [_row_dict(r) for r in
             s.execute(select(TradeJournal)).scalars().all()]
    s.execute(delete(TradeJournal))
print(f'   快照 {len(_SNAP)} 条，已清场')

PASSED = []
FAILED = []


def ok(name, cond, extra=''):
    if cond:
        PASSED.append(name)
        print(f'  ✅ {name}')
    else:
        FAILED.append(name)
        print(f'  ❌ {name} {extra}')


def restore():
    try:
        with session_scope() as s:
            s.execute(delete(TradeJournal))
            for d in _SNAP:
                d2 = dict(d)
                d2.pop('id', None)   # 自增 id 由库重新分配
                s.add(TradeJournal(**d2))
        print(f'♻️ 已恢复真实流水快照（{len(_SNAP)} 条）')
    except Exception as e:
        print(f'❌ 快照恢复失败: {e}')


try:
    # 被测模块（接口面）
    from crypto.task.utils.trade_journal import (  # noqa: E402
        record_fill, read_journal, classify_reason)

    print('\n--- A. 写入 + 读回 ---')
    ok('A0 空库读取返回空列表', read_journal() == [])
    record_fill('SMK-USDT-SWAP', 'trend', 'long', 'open',
                1.23456789, 0.2, reason='signal', ord_id='ORD-1', run_id='R1')
    recs = read_journal()
    ok('A1 写入后可读回 1 条', len(recs) == 1, str(recs))
    r = recs[0] if recs else {}
    ok('A2 字段形态与 JSONL 版一致',
       set(r.keys()) == {'ts', 'run_id', 'inst_id', 'bucket', 'direction',
                         'action', 'price', 'amount', 'ord_id', 'reason'}, str(r))
    ok('A3 字段值正确',
       r.get('inst_id') == 'SMK-USDT-SWAP' and r.get('bucket') == 'trend'
       and r.get('direction') == 'long' and r.get('action') == 'open'
       and abs(r.get('price', 0) - 1.23456789) < 1e-12
       and abs(r.get('amount', 0) - 0.2) < 1e-12
       and r.get('ord_id') == 'ORD-1' and r.get('run_id') == 'R1'
       and r.get('reason') == 'signal', str(r))
    ok('A4 ts 为 19 位本地时间字符串',
       len(r.get('ts', '')) == 19 and r.get('ts', '')[4] == '-', str(r.get('ts')))
    # 缺省参数归一化
    record_fill('SMK-USDT-SWAP', 'trend', 'long', 'close', None, None)
    r2 = read_journal(inst_id='SMK-USDT-SWAP', bucket='trend')[-1]
    ok('A5 缺省归一化 price=0/amount=0/reason=signal/ord_id空',
       r2.get('price') == 0.0 and r2.get('amount') == 0.0
       and r2.get('reason') == 'signal' and r2.get('ord_id') == ''
       and r2.get('run_id') == '', str(r2))

    print('\n--- B. 筛选语义 ---')
    record_fill('OTH-USDT-SWAP', 'range', 'short', 'open',
                5.0, 1.0, reason='signal', ord_id='ORD-9')
    record_fill('SMK-USDT-SWAP', 'account', 'long', 'close',
                2.0, 0.2, reason='人工强平')
    ok('B1 inst_id 精确筛选',
       all(x['inst_id'] == 'SMK-USDT-SWAP' for x in
           read_journal(inst_id='SMK-USDT-SWAP'))
       and len(read_journal(inst_id='SMK-USDT-SWAP')) == 3)
    ok('B2 bucket 精确筛选',
       [x['bucket'] for x in read_journal(bucket='range')] == ['range'])
    ok('B3 inst_id+bucket 组合筛选',
       len(read_journal(inst_id='SMK-USDT-SWAP', bucket='account')) == 1)
    all_ts = [x['ts'] for x in read_journal()]
    ok('B4 ts 闭区间下界',
       all(x['ts'] >= all_ts[0] for x in read_journal(start=all_ts[0]))
       and len(read_journal(start='9999-12-31 23:59:59')) == 0)
    ok('B5 ts 闭区间上界',
       len(read_journal(end='2000-01-01 00:00:00')) == 0
       and len(read_journal(end='9999-12-31 23:59:59')) == 4)

    print('\n--- C. 排序 ---')
    recs_all = read_journal()
    ok('C1 ts 升序', all(recs_all[i]['ts'] <= recs_all[i + 1]['ts']
                         for i in range(len(recs_all) - 1)))
    # 同 ts 写入顺序保持（追加两条同 inst 记录，先写的排前）
    n_before = len(recs_all)
    record_fill('SMK-USDT-SWAP', 'trend', 'short', 'open', 3.0, 0.1, ord_id='S1')
    record_fill('SMK-USDT-SWAP', 'trend', 'short', 'close', 3.1, 0.1, ord_id='S2')
    tail = read_journal()[-2:]
    ok('C2 同 ts 按写入顺序',
       [x.get('ord_id') for x in tail] == ['S1', 'S2']
       and len(read_journal()) == n_before + 2, str(tail))

    print('\n--- D. 容错 ---')
    ok('D1 read_journal 无异常返回 list', isinstance(read_journal(), list))
    try:
        record_fill(None, None, None, None, 'x', 'y')  # 脏参数不抛
        ok('D2 record_fill 脏参数静默吞掉', True)
    except Exception as e:
        ok('D2 record_fill 脏参数静默吞掉', False, str(e))

    print('\n--- E. classify_reason 归因 ---')
    cases = [
        ('signal', 'signal'), ('', 'signal'), (None, 'signal'),
        ('触发止损', 'sl'), ('分批止盈第1档', 'tp'), ('时间兜底平仓', 'tp'),
        ('长周期反转强平', 'reverse'), ('睡眠窗口平仓', 'reverse'),
        ('人工强平', 'manual'), ('手动平仓', 'manual'),
        ('反向持仓风控', 'guard'), ('风控强平', 'guard'),
        ('未知原因', 'other'),
    ]
    for reason, expect in cases:
        ok(f"E 归因 {reason!r}→{expect}", classify_reason(reason) == expect,
           f'实际={classify_reason(reason)}')

    print('\n--- F. 对比引擎消费面 ---')
    from crypto.task.trend_compare import read_journal as cmp_read  # noqa: E402
    ok('F1 trend_compare 导入 read_journal 同源', cmp_read is read_journal)
    import inspect
    sig = inspect.signature(read_journal)
    ok('F2 read_journal 签名不变',
       list(sig.parameters) == ['inst_id', 'bucket', 'start', 'end'])
    sig2 = inspect.signature(record_fill)
    ok('F3 record_fill 签名不变',
       list(sig2.parameters) == ['inst_id', 'bucket', 'direction', 'action',
                                 'price', 'amount', 'reason', 'ord_id', 'run_id'])

finally:
    restore()

print('\n' + '=' * 60)
print(f'结果: {len(PASSED)} 通过 / {len(FAILED)} 失败')
if FAILED:
    print('失败项:', FAILED)
    sys.exit(1)
print('🎉 全部通过')
