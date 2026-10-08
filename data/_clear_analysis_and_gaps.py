# -*- coding: utf-8 -*-
"""清空分析记录与断档台账（重新开始）——dry-run 默认，--apply 才写库
================================================================
用户裁定：把「实盘分析记录」和「分析纪律断档台账」全部清空，从零重新开始。

清理范围（MySQL）：
  1. task_analysis_records   —— 实盘分析记录全部行（交易复盘弹列表同源）；
  2. analysis_reminder_log   —— 分析纪律小时槽台账全部行（断档热力/合规率/
                                streak/邮件防重发 notified 的唯一数据源）；
  3. plan_slots 的分析关联三列（analysis_ids / analysis_hour / bypass_analysis）
     置空——记录已不存在，留悬空 id 会让纪律看板"有分析支撑"归因失真。

不清理（明确保留）：
  - plan_slots 打卡正文/时间/余额等内容列（打卡记录属于任务卡本体）；
  - 纪律配置 kv_store（规则、时段、strict 模式等，重新开始沿用现配置）；
  - plan_cards / plan_plans（任务卡状态，上一轮已单独处理）。

安全措施：写库前把两张表全部行 + 将被置空的 slots 行备份为 JSON。

用法：
  python data/_clear_analysis_and_gaps.py           # dry-run 只统计展示
  python data/_clear_analysis_and_gaps.py --apply   # 真正写库
"""
import io
import json
import os
import sys
import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import create_engine, text

APPLY = '--apply' in sys.argv
url = open(os.path.join(ROOT, 'data', 'db_url.txt')).read().strip()
engine = create_engine(url)


def dump_rows(conn, sql):
    return [dict(r) for r in conn.execute(text(sql)).mappings().all()]


with engine.connect() as conn:
    ana = dump_rows(conn, "SELECT * FROM task_analysis_records ORDER BY id")
    logs = dump_rows(conn, "SELECT * FROM analysis_reminder_log ORDER BY hour_slot")
    linked_slots = dump_rows(conn,
        "SELECT card_id, slot_index, filled, filled_at, analysis_ids, analysis_hour, "
        "bypass_analysis FROM plan_slots "
        "WHERE analysis_ids <> '' OR analysis_hour <> '' OR bypass_analysis = 1 "
        "ORDER BY card_id, slot_index")

    n_missing = sum(1 for r in logs if r['status'] == 'missing')
    n_later = sum(1 for r in logs if r['status'] == 'satisfied_later')
    print('== 当前数据量 ==')
    print(f'  task_analysis_records：{len(ana)} 条'
          + (f'（最早 {ana[0]["ts"]} ~ 最新 {ana[-1]["ts"]}）' if ana else ''))
    print(f'  analysis_reminder_log：{len(logs)} 条'
          f'（missing {n_missing} / satisfied_later {n_later} / 其余 {len(logs) - n_missing - n_later}）')
    print(f'  plan_slots 带分析关联的行：{len(linked_slots)} 条')

    if not ana and not logs and not linked_slots:
        print('✅ 三处均已清空，无需处理（幂等退出）')
        sys.exit(0)

    # 备份
    backup_dir = os.path.join(ROOT, 'data', 'plan_backups')
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(backup_dir, f'clear_analysis_gaps_{ts}.json')
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump({'task_analysis_records': ana,
                   'analysis_reminder_log': logs,
                   'plan_slots_linked': linked_slots},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f'== 备份 ==\n  已写入 {backup_path}')

    print('== 将执行的变更 ==')
    print(f'  1. DELETE task_analysis_records 全部 {len(ana)} 行')
    print(f'  2. DELETE analysis_reminder_log 全部 {len(logs)} 行')
    print(f'  3. UPDATE plan_slots 置空分析三列（仅关联行，共 {len(linked_slots)} 行）')

    if not APPLY:
        print('\n（dry-run 未写库；确认无误后加 --apply 执行）')
        sys.exit(0)

    with engine.begin() as wconn:
        if ana:
            wconn.execute(text("DELETE FROM task_analysis_records"))
        if logs:
            wconn.execute(text("DELETE FROM analysis_reminder_log"))
        if linked_slots:
            wconn.execute(text(
                "UPDATE plan_slots SET analysis_ids='', analysis_hour='', "
                "bypass_analysis=0 WHERE analysis_ids <> '' OR analysis_hour <> '' "
                "OR bypass_analysis = 1"))

    # 回读验证
    with engine.connect() as conn:
        c1 = conn.execute(text("SELECT COUNT(*) FROM task_analysis_records")).scalar()
        c2 = conn.execute(text("SELECT COUNT(*) FROM analysis_reminder_log")).scalar()
        c3 = conn.execute(text(
            "SELECT COUNT(*) FROM plan_slots WHERE analysis_ids <> '' OR "
            "analysis_hour <> '' OR bypass_analysis = 1")).scalar()
    ok = (c1 == 0 and c2 == 0 and c3 == 0)
    print('== 回读验证 ==')
    print(f'  task_analysis_records={c1} analysis_reminder_log={c2} 带关联slots={c3}')
    print('✅ 已全部清空，可以从零开始' if ok else '❌ 回读校验未通过，请对照备份人工检查')
    sys.exit(0 if ok else 3)
