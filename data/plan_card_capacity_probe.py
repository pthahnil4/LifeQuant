# -*- coding: utf-8 -*-
"""学习任务卡「信息容量」体检（只读）：量化 TEXT 列余量与打卡文本分布

用途：评估「脸盆里塞大象」到底装了多少、还能装多少——
  plan_cards.notes / tasks / review / goal 都是 MySQL TEXT（上限 65535 字节），
  打卡正文 plan_slots.content 也是 TEXT。本脚本按卡统计已用字节、剩余余量、
  单条最长文本，作为前端布局改造（是否支持长文本、是否需要分页/折叠）的依据。

用法（项目根目录）：
  python data/plan_card_capacity_probe.py                  # 全部计划卡
  python data/plan_card_capacity_probe.py --plan plan_learn_1000
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError, ValueError):
        pass

TEXT_MAX = 65535   # MySQL TEXT 列上限（字节）


def main():
    ap = argparse.ArgumentParser(description='任务卡文本容量体检（只读）')
    ap.add_argument('--plan', default='', help='只看某个计划（缺省全部）')
    args = ap.parse_args()

    from crypto.database import get_engine

    where = "WHERE c.plan_id = %s" if args.plan else ""
    sql_card = f"""
        SELECT c.plan_id, c.round, c.id, c.title,
               LENGTH(c.notes)  AS notes_bytes,  CHAR_LENGTH(c.notes)  AS notes_chars,
               LENGTH(c.tasks)  AS tasks_bytes,  CHAR_LENGTH(c.tasks)  AS tasks_chars,
               LENGTH(c.review) AS review_bytes, CHAR_LENGTH(c.review) AS review_chars,
               LENGTH(c.goal)   AS goal_bytes
        FROM plan_cards c {where}
        ORDER BY c.plan_id, c.round
    """
    sql_slot = f"""
        SELECT s.card_id,
               COUNT(*)                              AS filled,
               MAX(LENGTH(s.content))                AS max_content_bytes,
               AVG(LENGTH(s.content))                AS avg_content_bytes,
               MAX(CHAR_LENGTH(s.content))           AS max_content_chars,
               MAX(LENGTH(IFNULL(s.task_links, ''))) AS max_links_bytes
        FROM plan_slots s JOIN plan_cards c ON c.id = s.card_id
        WHERE s.filled {('AND c.plan_id = %s' if args.plan else '')}
        GROUP BY s.card_id
    """
    with get_engine().connect() as conn:
        cards = conn.exec_driver_sql(sql_card, (args.plan,) if args.plan else ()).fetchall()
        slots = {r[0]: r for r in conn.exec_driver_sql(
            sql_slot, (args.plan,) if args.plan else ()).fetchall()}

    print(f"{'卡':<34}{'小记':>9}{'任务树':>9}{'复盘':>8}{'目标':>8}"
          f"{'打卡文本(最长字/平均B)':>24}{'最紧列余量':>12}")
    print('-' * 104)
    worst = []
    for r in cards:
        plan_id, rnd, cid, title = r[0], r[1], r[2], r[3]
        nb, tb, rb, gb = r[4] or 0, r[6] or 0, r[8] or 0, r[10] or 0
        s = slots.get(cid)
        av = int((s[3] or 0) if s else 0)
        mxch = (s[4] or 0) if s else 0
        # 单卡最吃紧的那一列：TEXT 上限 65535 字节
        used = max(nb, tb, rb, gb)
        left = TEXT_MAX - used
        colname = ['notes', 'tasks', 'review', 'goal'][[nb, tb, rb, gb].index(used)]
        worst.append((left, cid, title, colname))
        print(f'{("[" + plan_id + "] r" + str(rnd) + " " + str(title)[:12]):<34}'
              f'{nb / 1024:>7.1f}KB{tb / 1024:>7.1f}KB{rb / 1024:>6.1f}KB{gb / 1024:>6.1f}KB'
              f'{f"{mxch}字 / {av}B":>24}{left / 1024:>10.1f}KB')
    worst.sort()
    print('\n最吃紧的 5 个字段（距离 TEXT 65535 字节上限）：')
    for left, cid, title, col in worst[:5]:
        print(f'  {col:<7} {cid} {str(title)[:20]:<22} 剩余 {left / 1024:.1f}KB '
              f'（约还能写 {int(left / 3)} 个汉字）')


if __name__ == '__main__':
    main()
