# -*- coding: utf-8 -*-
"""执行 data/merge_learn_card_verify.sql 并打印结果（只读核对，不写库）

用法（项目根目录）：
  python data/merge_learn_card_verify.py                 # 跑全部 12 条核对
  python data/merge_learn_card_verify.py --expect 87 0   # 跑并断言 目标卡/来源卡 filled

设计：所有语句在同一条连接上顺序执行；遇到错误语句即时打印并继续跑后面的
（远端 MySQL 偶发抖动时便于一次看全貌）。SQL 不使用 @会话变量——库表是
utf8mb4_general_ci、用户变量带连接层 collation，混用会报 Illegal mix of collations。
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError, ValueError):
        pass

SQL_FILE = os.path.join(ROOT, 'data', 'merge_learn_card_verify.sql')


def split_statements(text):
    """按分号切句；保留注释行（MySQL 自身支持 -- 行注释），丢弃纯注释片段"""
    out = []
    for raw in text.split(';'):
        stmt = raw.strip()
        if not stmt:
            continue
        body = '\n'.join(ln for ln in stmt.splitlines() if not ln.strip().startswith('--')).strip()
        if body:
            out.append(stmt)
    return out


def fmt(rows, cols, width=30):
    if not rows:
        return '    （空结果集）'
    def cell(v):
        if v is None:
            return 'NULL'
        s = str(v).replace('\n', ' ')
        return s if len(s) <= width else s[:width - 1] + '…'
    table = [[cell(r[c]) for c in cols] for r in rows]
    heads = [c if len(c) <= width else c[:width - 1] + '…' for c in cols]
    w = [max(len(h), *(len(row[i]) for row in table)) for i, h in enumerate(heads)]
    lines = ['    ' + ' | '.join(h.ljust(w[i]) for i, h in enumerate(heads)),
             '    ' + '-+-'.join('-' * x for x in w)]
    lines += ['    ' + ' | '.join(row[i].ljust(w[i]) for i in range(len(heads))) for row in table]
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description='只读核对学习任务卡合并结果')
    ap.add_argument('--expect', nargs=2, type=int, metavar=('DST', 'SRC'),
                    help='断言 目标卡/来源卡 的 filled_count')
    ap.add_argument('--dst-id', default='learn_545dc97c', help='目标卡 ID（断言用）')
    ap.add_argument('--src-id', default='learn_252c445e', help='来源卡 ID（断言用）')
    ap.add_argument('--file', default=SQL_FILE)
    args = ap.parse_args()

    from crypto.database import get_engine

    with open(args.file, encoding='utf-8') as f:
        stmts = split_statements(f.read())

    fails, result_rows = [], []
    with get_engine().connect() as conn:
        for i, stmt in enumerate(stmts, 1):
            # 取该语句开头注释里的序号标题，便于对照 SQL 文件
            head = next((ln.strip().lstrip('-').strip()
                         for ln in stmt.splitlines() if re.match(r'^--\s*[①-⑫]', ln.strip())),
                        re.sub(r'\s+', ' ', stmt[:60]))
            print(f'\n=== [{i}/{len(stmts)}] {head}')
            try:
                # 走 driver 原始通道：SQL 里的 JSON 路径、@会话变量都不该被 ORM 解析
                res = conn.exec_driver_sql(stmt)
                try:
                    cols = list(res.keys())
                except Exception:
                    cols = None
                if cols:
                    rows = [dict(zip(cols, r)) for r in res.fetchall()]
                    print(fmt(rows, cols))
                    if args.expect and 'filled_count' in cols:
                        for r in rows:
                            result_rows.append((str(r.get('card_id')), int(r.get('filled_count') or 0)))
                else:
                    print(f'    （OK，影响 {res.rowcount} 行）')
            except Exception as e:
                msg = (str(e).splitlines() or [''])[0][:200]
                print(f'    ❌ 执行失败：{msg}')
                fails.append(f'[{i}] {msg}')

    if args.expect:
        dst_exp, src_exp = args.expect
        got = dict(result_rows)
        print('\n=== 断言 ===')
        for label, cid, exp in (('目标卡', args.dst_id, dst_exp), ('来源卡', args.src_id, src_exp)):
            actual = got.get(cid)
            ok = actual == exp
            print(f'  {"✅" if ok else "❌"} {label} {cid} filled_count={actual}（期望 {exp}）')
            if not ok:
                fails.append(f'{label} filled_count={actual}，期望 {exp}')

    print('\n' + ('❌ 核对存在问题：\n   ' + '\n   '.join(fails) if fails else '✅ 全部核对语句执行通过'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
