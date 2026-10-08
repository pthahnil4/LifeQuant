#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务计划读路径体积/耗时基准（纯离线，SQLite 内存库）
========================================================
为什么要它：MySQL 隔离库还没落地（应用账号只有 `crypto`.* 权限，无
CREATE DATABASE），但"少读列"的收益不依赖 MySQL —— 少读的是同一大表里的
TEXT 列，在任何引擎上都是实打实的字节量与反序列化量。用同一份合成数据量：

  whole_tree   整树读（优化前 /plan/api/list 的读法，plan_repo.load_plans_data）
  summary      窄投影整树（列表 / 今日统计新读法，plan_repo.load_summary_tree）
  card_view    单卡局部读（card-detail 新读法，plan_repo.load_card_view）
  write_reuse  打卡一次：同 session 读整树 → 存整树（行对象复用后）
  write_legacy 打卡一次：同上，但中途作废登记表（等价于优化前的两次全树读）
  write_view   打卡一次：局部读写（本计划投影 + 目标卡完整，只写被改的那几行）

指标口径：
  - SELECT 条数：before_cursor_execute 捕获的 SELECT 语句数
  - 回传字节：把同一批 SELECT 原样再执行一次，逐行逐列 len(str(v)) 求和
              （近似网络载荷，只用于比较相对量级）
  - slot 对象数：读法返回的 dict 树里 slots 元素总数（Python 侧工作量）
  - 耗时：同进程内交替重复 N 次取中位数，抵消 GC/缓存抖动

运行：
    python -B data/db_read_volume_probe.py
    python -B data/db_read_volume_probe.py --plans 3 --cards 20 --out doc/DB读路径基准.md
"""

import argparse
import json
import os
import statistics
import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy import create_engine, event                 # noqa: E402
from sqlalchemy.orm import sessionmaker                     # noqa: E402

from crypto import plan_repo                                # noqa: E402
from crypto.db_performance import TimedQueuePool            # noqa: E402
from crypto.models import KVStore, PlanPlan, PlanCard, PlanSlot  # noqa: E402

# 一格打卡正文/行情分析的真实量级：两三百字
LONG_TEXT = ('今天的行情结构是高位震荡，量能萎缩，'
             '复盘要点：突破后的回踩确认与止损位设置都需要写清楚。' * 4)

TASKS_MIGRATED = json.dumps(
    [{'id': 'task_a', 'title': '第一章', 'estimated_minutes': 60,
      'status': 'done', 'children': []}], ensure_ascii=False)


def build_engine(plans=3, cards=20, slots=100):
    """造一份与线上同构的合成库：cards×slots 全量填充长文本"""
    engine = create_engine('sqlite://', poolclass=TimedQueuePool)
    for table in (PlanPlan.__table__, PlanCard.__table__,
                  PlanSlot.__table__, KVStore.__table__):
        table.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        s.merge(KVStore(key='task_plans_initialized', value='true'))
        for p in range(plans):
            pid = f'plan_{p}'
            s.add(PlanPlan(id=pid, sort_order=p, type='learn' if p % 2 else 'trade',
                           name=f'计划{p}', total_hours=cards, round_count=cards,
                           per_round_hours=1,
                           daily_rule='{"mode":"longtermism"}'))
            for c in range(cards):
                cid = f'{pid}_card_{c}'
                ctype = 'trade' if c % 2 else 'learn'
                s.add(PlanCard(id=cid, plan_id=pid, sort_order=c, type=ctype,
                               round=c, title=f'第{c}轮', status='in_progress',
                               reward=2000, base_reward=2000, hourly_rate=20,
                               milestones='[]', todos='[]',
                               tasks='[]' if c else TASKS_MIGRATED,
                               notes=f'[{{"content": "{LONG_TEXT}"}}]',
                               review=LONG_TEXT, goal=LONG_TEXT))
                for idx in range(slots):
                    filled = idx % 2 == 0
                    s.add(PlanSlot(
                        card_id=cid, slot_index=idx, filled=filled,
                        filled_at=f'2026-{(idx % 9) + 1:02d}-{(idx % 28) + 1:02d} '
                                  f'10:00:00' if filled else '',
                        has_record=filled,
                        content=LONG_TEXT if (ctype == 'learn' and filled) else '',
                        duration_minutes=60 if filled else 0,
                        prediction='up' if (ctype == 'trade' and filled) else '',
                        actual='up' if (ctype == 'trade' and filled) else '',
                        hit=True if (ctype == 'trade' and filled) else None,
                        market_analysis=LONG_TEXT if (ctype == 'trade' and filled) else '',
                        action_advice=LONG_TEXT if (ctype == 'trade' and filled) else '',
                        task_links='[]'))
        s.commit()
    return engine, factory


def count_slot_dicts(node):
    """递归数出返回树里的 slot dict 个数（Python 侧构造量的直接指标）"""
    if isinstance(node, dict):
        total = len(node.get('slots') or []) if 'slots' in node else 0
        for v in node.values():
            total += count_slot_dicts(v)
        return total
    if isinstance(node, (list, tuple)):
        return sum(count_slot_dicts(v) for v in node)
    return 0


class Probe:
    """一次读法的 SELECT 条数 / 回传字节 / 对象数 / 耗时采集器"""

    def __init__(self, engine, factory):
        self.engine = engine
        self.factory = factory
        self.captured = []
        self._recording = False

        @event.listens_for(engine, 'before_cursor_execute')
        def _record(conn, cursor, statement, params, context, executemany):
            if self._recording and statement.lstrip().upper().startswith('SELECT'):
                self.captured.append((statement, params))

    def _once(self, fn, capture=False):
        self.captured = []
        self._recording = capture
        with self.factory() as session:
            started = time.perf_counter()
            result = fn(session)
            elapsed = (time.perf_counter() - started) * 1000.0
        self._recording = False
        return result, elapsed

    def _payload(self):
        """把刚捕获的 SELECT 原样再执行一遍，逐列累加 UTF-8 字节量"""
        rows = nbytes = 0
        with self.engine.connect() as conn:
            for statement, params in self.captured:
                fetched = conn.exec_driver_sql(statement, params).fetchall()
                rows += len(fetched)
                nbytes += sum(len(str(value).encode('utf-8'))
                              for row in fetched for value in row)
        return rows, nbytes

    def run(self, label, fn, repeats=5):
        timings = []
        for _ in range(repeats):
            _, elapsed = self._once(fn)
            timings.append(elapsed)
        result, elapsed = self._once(fn, capture=True)
        rows, chars = self._payload()
        return {
            'label': label,
            'sql_count': len(self.captured),
            'rows': rows,
            'kbytes': round(chars / 1024.0, 1),
            'slot_objs': count_slot_dicts(result),
            'ms_p50': round(statistics.median(timings), 2),
        }


def scenario_write(legacy: bool):
    """打卡一次的完整读+存：legacy=True 时作废登记表，还原优化前的两次全树读"""

    def _run(session):
        data = plan_repo.load_plans_data(session)
        if legacy:
            session.info.pop(plan_repo._ROWS_CACHE_KEY, None)
        plan_repo.save_plans_data(session, data)
        return data
    return _run


def scenario_local_write(plan_id, card_id, slot_index=1):
    """打卡一次的局部读写（阶段 2 新读法）：只读本计划投影 + 目标卡，
    只写被改的那一个格子、目标卡自身列与被牵连的兄弟卡标量列。

    反复调用会在"补上/撤掉同一个格子"之间摆动：写面恒定为一行，
    计时与字节量都反映稳态，不会被首次写入的额外列变化放大。
    """

    def _run(session):
        view = plan_repo.load_plan_write_view(session, plan_id, card_id)
        card = view['card']
        slot = card['slots'][slot_index]
        if slot.get('filled'):
            slot['filled'] = False
            slot['filled_at'] = ''
            slot['record'] = None
        else:
            slot['filled'] = True
            slot['filled_at'] = '2026-09-17 10:11:12'
            slot['record'] = {'content': '基准补录', 'duration_minutes': 30,
                              'task_links': []}
        plan_repo.save_card_slots(session, card_id, card['slots'],
                                  view['rows']['slots'])
        plan_repo.save_card(session, view['rows']['card'], card)
        plan_repo.save_sibling_changes(session, plan_id, view['plan']['cards'],
                                       view['rows']['siblings'])
        return view
    return _run


def main():
    parser = argparse.ArgumentParser(description='任务计划读路径体积/耗时基准')
    parser.add_argument('--plans', type=int, default=3, help='计划数（默认 3）')
    parser.add_argument('--cards', type=int, default=20, help='每计划卡数（默认 20）')
    parser.add_argument('--slots', type=int, default=100, help='每卡格子数（默认 100）')
    parser.add_argument('--repeats', type=int, default=5, help='计时重复次数')
    parser.add_argument('--out', default='', help='把报告写到该路径（Markdown）')
    args = parser.parse_args()

    engine, factory = build_engine(args.plans, args.cards, args.slots)
    first_plan = f'plan_0'
    first_card = f'{first_plan}_card_0'
    probe = Probe(engine, factory)

    cases = [
        ('whole_tree', '整树读（优化前列表）',
         lambda s: plan_repo.load_plans_data(s)),
        ('summary', '窄投影整树（列表/今日统计）',
         lambda s: plan_repo.load_summary_tree(s)),
        ('card_view', '单卡局部读（card-detail）',
         lambda s: plan_repo.load_card_view(s, first_plan, first_card)),
        ('write_reuse', '打卡读+存（复用行对象）', scenario_write(False)),
        ('write_legacy', '打卡读+存（优化前重复全树读）', scenario_write(True)),
        ('write_view', '打卡局部读写（本计划投影 + 目标卡）',
         scenario_local_write(first_plan, first_card)),
    ]
    report = []
    for key, label, fn in cases:
        row = probe.run(label, fn, repeats=args.repeats)
        row['key'] = key
        report.append(row)

    total_slots = args.plans * args.cards * args.slots
    lines = [
        '# 任务计划读路径体积/耗时基准',
        '',
        f'- 合成规模：{args.plans} 计划 × {args.cards} 卡 × {args.slots} 格'
        f' = {total_slots:,} 个格子行（长文本列已按线上量级填充）',
        '- 引擎：SQLite 内存库 + 与线上同一套 repo 代码；字节量按逐列 '
        '`len(str(v).encode("utf-8"))` 求和，只用于比较相对量级',
        '- 耗时取同进程交替重复多次的中位数（冷连接已排除）',
        '',
        '| 场景 | SELECT 条数 | 回传行数 | 回传量(KB) | 构造 slot 对象 | 耗时中位数(ms) |',
        '| --- | --- | --- | --- | --- | --- |',
    ]
    for row in report:
        lines.append(f"| {row['label']} | {row['sql_count']} | {row['rows']:,} "
                     f"| {row['kbytes']:,} | {row['slot_objs']:,} | {row['ms_p50']} |")

    def _ratio(a_key, b_key, field):
        a = next(r for r in report if r['key'] == a_key)[field]
        b = next(r for r in report if r['key'] == b_key)[field]
        return (b / a) if a else float('inf')

    lines += [
        '',
        '## 读法对比结论（同一份数据、同一台机器）',
        '',
        f"- 列表读法换窄投影后回传量降为整树的 "
        f"{_ratio('whole_tree', 'summary', 'kbytes') * 100:.1f}%"
        f"（{next(r for r in report if r['key'] == 'whole_tree')['kbytes']:,} KB → "
        f"{next(r for r in report if r['key'] == 'summary')['kbytes']:,} KB）",
        f"- 单卡局部读回传量降为整树的 "
        f"{_ratio('whole_tree', 'card_view', 'kbytes') * 100:.1f}%",
        f"- 打卡写路径复用行对象后，SELECT 条数从 "
        f"{next(r for r in report if r['key'] == 'write_legacy')['sql_count']}"
        f" 降到 "
        f"{next(r for r in report if r['key'] == 'write_reuse')['sql_count']}"
        f"，回传量降为 "
        f"{_ratio('write_legacy', 'write_reuse', 'kbytes') * 100:.1f}%",
        f"- 打卡改局部读写后，SELECT 条数进一步从 "
        f"{next(r for r in report if r['key'] == 'write_legacy')['sql_count']}"
        f" 降到 "
        f"{next(r for r in report if r['key'] == 'write_view')['sql_count']}，"
        f"回传量降为优化前的 "
        f"{_ratio('write_legacy', 'write_view', 'kbytes') * 100:.1f}%"
        f"（{next(r for r in report if r['key'] == 'write_legacy')['kbytes']:,} KB → "
        f"{next(r for r in report if r['key'] == 'write_view')['kbytes']:,} KB）"
        f"，构造 slot 对象从 "
        f"{next(r for r in report if r['key'] == 'write_legacy')['slot_objs']:,} 降到 "
        f"{next(r for r in report if r['key'] == 'write_view')['slot_objs']:,}",
        '',
        '> 说明：SQLite 只能证明「少读了多少」，不能证明 MySQL 的行锁等待、',
        '> 索引选择与网络 RTT 表现；那部分必须等隔离测试库（见 ',
        '> doc/隔离测试库授权.sql）批下来后按同口径再跑一遍。',
        '',
    ]
    text = '\n'.join(lines)
    print(text)
    if args.out:
        out = args.out if os.path.isabs(args.out) else os.path.join(_ROOT, args.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'[OK] 报告已写入 {out}')
    engine.dispose()
    return 0


if __name__ == '__main__':
    sys.exit(main())
