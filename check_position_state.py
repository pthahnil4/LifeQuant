#!/usr/bin/env python3
# -*- coding: utf-8 -*-
2

import pymysql
import sys


def get_conn():
    with open('data/db_url.txt', 'r') as f:
        url = f.read().strip()
    parts = url.replace('mysql+pymysql://', '').split('@')
    up = parts[0].split(':')
    hd = parts[1].split('/')
    hp = hd[0].split(':')
    return pymysql.connect(
        host=hp[0], port=int(hp[1]), user=up[0], password=up[1],
        database=hd[1].split('?')[0], charset='utf8mb4')


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else ['NEAR-USDT-SWAP']
    conn = get_conn()
    c = conn.cursor()

    print("=" * 90)
    print("pos_book 完整状态（含 last_desired）")
    print("=" * 90)
    for t in targets:
        c.execute(
            """SELECT inst_id, bucket, held_long, held_short, avg_px_long,
                      avg_px_short, prev_open_confirmed, prev_close_confirmed,
                      last_desired
               FROM pos_book WHERE inst_id=%s ORDER BY bucket""", (t,))
        rows = c.fetchall()
        print(f"\n{t}:")
        if not rows:
            print("  (无记录)")
            continue
        for r in rows:
            (inst, bk, hl, hs, al, ash, poc, pcc, ld) = r
            name = "趋势跟踪" if bk == "trend" else "区间波动"
            print(f"  [{name:4s}] long={hl:.4f}(@{al:.6g})  "
                  f"short={hs:.4f}(@{ash:.6g})  "
                  f"last_desired={ld}  "
                  f"prev_open={poc} prev_close={pcc}")

    # 顺便查所有单边持仓的币种
    print("\n" + "=" * 90)
    print("只有单边篮子持仓的币种（可能吸收或漏开）")
    print("=" * 90)
    c.execute("""
        SELECT inst_id,
               MAX(CASE WHEN bucket='trend' THEN held_long ELSE 0 END) AS tl,
               MAX(CASE WHEN bucket='trend' THEN held_short ELSE 0 END) AS ts,
               MAX(CASE WHEN bucket='range' THEN held_long ELSE 0 END) AS rl,
               MAX(CASE WHEN bucket='range' THEN held_short ELSE 0 END) AS rs,
               MAX(CASE WHEN bucket='trend' THEN last_desired END) AS tld,
               MAX(CASE WHEN bucket='range' THEN last_desired END) AS rld
        FROM pos_book
        GROUP BY inst_id
        HAVING (tl>0.01 OR ts>0.01 OR rl>0.01 OR rs>0.01)
        ORDER BY inst_id
    """)
    for r in c.fetchall():
        inst, tl, ts, rl, rs, tld, rld = r
        trend_side = "long" if tl > 0.01 else ("short" if ts > 0.01 else "-")
        range_side = "long" if rl > 0.01 else ("short" if rs > 0.01 else "-")
        mark = ""
        if trend_side != range_side:
            mark = "  <-- 单边"
        print(f"  {inst:24s} trend={trend_side}({tl:.2f}/{ts:.2f}) ld={tld}  "
              f"range={range_side}({rl:.2f}/{rs:.2f}) ld={rld}{mark}")

    c.close()
    conn.close()


if __name__ == '__main__':
    main()
