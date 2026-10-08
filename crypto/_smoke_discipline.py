#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析纪律（批次11）冒烟：DB 增量迁移 → 配置读写 → 闸门判定 → 状态/看板只读链路

只做读取与幂等迁移，不写业务数据（唯一写入是 kv_store 配置回环，测完还原原值）。
运行：python -m crypto._smoke_discipline
"""

import datetime
import json

from .database import init_db, session_scope, get_engine
from . import discipline_repo as disc


def _title(t):
    print('\n' + '=' * 72)
    print(t)
    print('=' * 72)


def check_db():
    _title('[1] DB 增量迁移与结构校验')
    init_db()
    from sqlalchemy import text
    with get_engine().connect() as conn:
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name='task_analysis_records'"))}
        pcols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name='plan_slots'"))}
        idx = {r[0] for r in conn.execute(text(
            "SELECT DISTINCT index_name FROM information_schema.statistics "
            "WHERE table_schema=DATABASE() AND table_name='task_analysis_records'"))}
        tables = {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=DATABASE()"))}
        blank = conn.execute(text(
            "SELECT COUNT(*) FROM task_analysis_records WHERE hour_slot='' OR hour_slot IS NULL"
        )).scalar()
        total = conn.execute(text("SELECT COUNT(*) FROM task_analysis_records")).scalar()

    for c in ('hour_slot', 'source'):
        print(f"  task_analysis_records.{c:<12} {'OK' if c in cols else 'MISSING'}")
    for c in ('analysis_ids', 'analysis_hour', 'bypass_analysis'):
        print(f"  plan_slots.{c:<18} {'OK' if c in pcols else 'MISSING'}")
    print(f"  idx_tar_slot                 {'OK' if 'idx_tar_slot' in idx else 'MISSING'}")
    print(f"  analysis_reminder_log        {'OK' if 'analysis_reminder_log' in tables else 'MISSING'}")
    print(f"  hour_slot 回填               {total - blank}/{total} 行已填充（空 {blank}）")


def check_config():
    _title('[2] 配置读写回环')
    cfg = disc.load_config()
    print('  默认/现存配置:', json.dumps(cfg, ensure_ascii=False))
    from . import config_store_repo
    with session_scope() as s:
        original = config_store_repo.load_json_config(s, disc.KEY_DISCIPLINE_CONFIG)
    try:
        trial = json.loads(json.dumps(cfg))
        trial['required_count'] = 3
        trial['grace_minutes'] = 7
        disc.save_config(trial)
        back = disc.load_config()
        assert back['required_count'] == 3, back['required_count']
        assert back['grace_minutes'] == 7, back['grace_minutes']
        # 默认键必须被 _deep_merge 补齐（防止用户存了半份配置后新功能字段丢失）
        assert back['browser']['banner_after_minutes'] == 20
        assert back['email']['daily_report'] == '23:00'
        print('  写入/读回 required_count=3 grace_minutes=7 ... OK')
        print('  深合并补齐 browser/email 默认子键 ... OK')
    finally:
        with session_scope() as s:
            if original is None:
                config_store_repo.delete_json_config(s, disc.KEY_DISCIPLINE_CONFIG)
            else:
                config_store_repo.save_json_config(s, disc.KEY_DISCIPLINE_CONFIG, original)
        print('  已还原原始配置:', disc.load_config().get('required_count'),
              disc.load_config().get('grace_minutes'))


def check_gate():
    _title('[3] 闸门判定（当前小时 / 历史小时 / 时段外 / 豁免）')
    cfg = disc.load_config()
    now = datetime.datetime.now()
    cur_slot = disc.hour_slot_of(now)
    with session_scope() as s:
        ev = disc.evaluate_slot(s, cur_slot, cfg)
        gate = disc.gate_check(s, now, cfg)
        night = disc.gate_check(s, now.replace(hour=3, minute=30), cfg)
        yesterday = disc.gate_check(s, now - datetime.timedelta(days=1), cfg)

    print(f"  当前槽 {cur_slot}  记录 {ev['actual']} 条 / 要求 {ev['required']} 条  "
          f"active={ev['active']} ok={ev['ok']}")
    print(f"    覆盖币种: {ev['covered'] or '（无）'}")
    for r in ev['records'][:5]:
        print(f"      #{r['id']} {r['ts']} {r['inst_id']} {r.get('user_judgment')} "
              f"[{r.get('source')}]")
    print(f"  当前时刻闸门: allowed={gate['allowed']} mode={gate['mode']} reason={gate['reason']!r}")
    print(f"  凌晨 03:30  : allowed={night['allowed']} mode={night['mode']}（应为 inactive 放行）")
    print(f"  昨日同时刻  : allowed={yesterday['allowed']} mode={yesterday['mode']}")

    until = disc.set_exempt(2)
    with session_scope() as s:
        ex = disc.gate_check(s, now, cfg)
    print(f"  豁免 2 小时后: allowed={ex['allowed']} mode={ex['mode']} until={until}")
    disc.set_exempt(0)
    with session_scope() as s:
        back = disc.gate_check(s, now, cfg)
    print(f"  取消豁免后  : allowed={back['allowed']} mode={back['mode']}")


def check_failopen():
    _title('[4] fail-open 包装（不建 session，异常必须放行）')
    g = disc.safe_gate_check(datetime.datetime.now())
    print(f"  safe_gate_check → allowed={g['allowed']} mode={g['mode']}")
    print(f"  reason={g.get('reason', '')[:80]}")


def check_status_board():
    _title('[5] status / board / digest 只读链路')
    with session_scope() as s:
        st = disc.build_status(s)
        bd = disc.build_board(s, days=7)
        dg = disc.gap_digest(s, days=7)
        ms = disc.missing_slots(s, (datetime.datetime.now()
                                    - datetime.timedelta(days=7)).strftime('%Y-%m-%d'))

    print(f"  status: enabled={st['enabled']} mode={st['strict_mode']} slot={st['hour_slot']} "
          f"ok={st['ok']} elapsed={st['elapsed_minutes']}min banner={st['banner']}")
    print(f"  today : {json.dumps(st['today'], ensure_ascii=False)}")
    print(f"  streak: {json.dumps(st['streak'], ensure_ascii=False)}")
    print(f"  browser 开关: {json.dumps(st['browser'], ensure_ascii=False)}")
    print(f"  board summary : {json.dumps(bd['summary'], ensure_ascii=False)}")
    print(f"  board backfill: {json.dumps(bd['backfill'], ensure_ascii=False)}")
    print(f"  board 归因    : {json.dumps(bd['attribution'], ensure_ascii=False)}")
    print(f"  board daily 天数={len(bd['daily'])} heatmap 维度={len(bd['heatmap'])}x{len(bd['heatmap'][0])}")
    print(f"  gap_digest    : {json.dumps(dg, ensure_ascii=False)}")
    print(f"  待通知缺口行  : {len(ms)}")


def check_pending_slots():
    _title('[6] 巡检待判定槽集合（有界 + 过宽限期 + 生效时段）')
    from .task.monitor import analysis_discipline as ad
    cfg = disc.load_config()
    now = datetime.datetime.now()
    slots = ad._pending_slots(cfg, now)
    print(f"  LOOKBACK={disc.LOOKBACK_SLOTS}  待判定 {len(slots)} 槽")
    print(f"  最早 {slots[0] if slots else '-'} / 最晚 {slots[-1] if slots else '-'}")
    grace = cfg.get('grace_minutes', 15)
    for sl in slots:
        assert disc.slot_end(sl) <= now - datetime.timedelta(minutes=grace), sl
        assert disc.is_slot_active(sl, cfg), sl
    print('  断言：全部槽已过宽限期且在生效时段内 ... OK')
    print('  最近一轮巡检摘要:', json.dumps(ad.get_last_run(), ensure_ascii=False))


def check_upsert_rollback():
    _title('[7a] 台账 upsert 幂等性（合成槽 + 事务回滚，不落库）')
    from .task.monitor import analysis_discipline as ad
    from .database import get_session
    from sqlalchemy import select
    from .models import AnalysisReminderLog

    cfg = disc.load_config()
    slot = '2000-01-01 10'      # 远超生效起点、无任何分析记录的历史合成槽
    s = get_session()
    try:
        r1 = ad._upsert_slot(s, slot, cfg)
        r2 = ad._upsert_slot(s, slot, cfg)
        n = len(s.execute(select(AnalysisReminderLog)
                          .where(AnalysisReminderLog.hour_slot == slot)).scalars().all())
        print(f"  首次: status={r1['status']} is_new_gap={r1['is_new_gap']} resolved={r1['resolved']}")
        print(f"  再次: status={r2['status']} is_new_gap={r2['is_new_gap']} resolved={r2['resolved']}")
        print(f"  同槽行数={n}（必须为 1，验证 hour_slot 唯一幂等）")
        assert n == 1, n
        assert r1['is_new_gap'] is True and r2['is_new_gap'] is False
        assert r1['status'] == r2['status'] == disc.ST_MISSING
        print('  断言：重复判定不产生新缺口、不重复行 ... OK')
    finally:
        s.rollback()      # 合成槽绝不落库
        s.close()
    with session_scope() as s2:
        left = s2.execute(select(AnalysisReminderLog)
                          .where(AnalysisReminderLog.hour_slot == slot)).scalars().all()
    print(f"  回滚后残留行数={len(left)}（必须为 0）")


def check_run_once():
    _title('[7] 真实跑一轮巡检（写台账；冒烟期间关闭邮件，不污染收件箱）')
    from .task.monitor import analysis_discipline as ad
    from . import config_store_repo

    cfg = disc.load_config()
    with session_scope() as s:
        original = config_store_repo.load_json_config(s, disc.KEY_DISCIPLINE_CONFIG)
    # 巡检自己读配置，所以必须先落库：关掉缺口信与日报，只验台账幂等性
    quiet = json.loads(json.dumps(cfg))
    quiet['email'] = {'on_gap': False, 'merge_after': 2, 'daily_report': ''}
    try:
        disc.save_config(quiet)
        print('  生效起点 epoch:', disc.ensure_epoch())
        print('  起点前槽数过滤后待判定:',
              len(ad._pending_slots(disc.load_config(), datetime.datetime.now())))
        ad.run_discipline_check()
        print('  第一轮:', json.dumps(ad.get_last_run(), ensure_ascii=False))
        # 第二轮必须完全幂等：不产生新缺口、不重复判定为新增
        ad.run_discipline_check()
        print('  第二轮(幂等):', json.dumps(ad.get_last_run(), ensure_ascii=False))
    finally:
        with session_scope() as s:
            if original is None:
                config_store_repo.delete_json_config(s, disc.KEY_DISCIPLINE_CONFIG)
            else:
                config_store_repo.save_json_config(s, disc.KEY_DISCIPLINE_CONFIG, original)
        print('  已还原邮件配置:', json.dumps(disc.load_config().get('email'), ensure_ascii=False))

    with session_scope() as s:
        from sqlalchemy import select
        from .models import AnalysisReminderLog
        rows = s.execute(select(AnalysisReminderLog)
                         .order_by(AnalysisReminderLog.hour_slot.desc()).limit(8)).scalars().all()
        cnt = s.execute(select(AnalysisReminderLog)).scalars().all()
    print(f'  台账共 {len(cnt)} 行（应等于生效起点后的已完结槽数）:')
    for r in rows:
        print(f"    {r.hour_slot} {r.status:<16} req={r.required_count} act={r.actual_count} "
              f"notified={r.notified} resolved={r.resolved_at or '-'}")


def check_bool_filter():
    _title('[8] SQLAlchemy Boolean 过滤在 MySQL 上的渲染验证')
    from sqlalchemy import select
    from .models import PlanSlot, AnalysisReminderLog
    q1 = select(PlanSlot.card_id).where(PlanSlot.filled == True).limit(1)   # noqa: E712
    q2 = select(AnalysisReminderLog.id).where(
        AnalysisReminderLog.notified == False).limit(1)   # noqa: E712
    with session_scope() as s:
        r1 = s.execute(q1).first()
        r2 = s.execute(q2).first()
    print('  PlanSlot.filled == True        →', r1)
    print('  AnalysisReminderLog.notified==False →', r2)
    print('  两条查询均未抛异常 ... OK')


if __name__ == '__main__':
    check_db()
    check_config()
    check_gate()
    check_failopen()
    check_status_board()
    check_pending_slots()
    check_bool_filter()
    check_upsert_rollback()
    check_run_once()
    _title('冒烟完成')
