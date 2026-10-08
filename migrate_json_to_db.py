#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON → MySQL 数据迁移脚本（分批执行）
=======================================
用法（在项目根目录 cryptoTrade 下执行）：
    python migrate_json_to_db.py journal           # 迁移随笔模块
    python migrate_json_to_db.py calorie           # 迁移热量模块
    python migrate_json_to_db.py plans             # 迁移任务计划模块
    python migrate_json_to_db.py balance           # 迁移账户余额历史
    python migrate_json_to_db.py trader_state      # 迁移交易运行时状态（5个状态JSON）
    python migrate_json_to_db.py trade_journal     # 迁移结构化成交流水（JSONL）
    python migrate_json_to_db.py configs           # 迁移策略配置/币种自选（整份存入kv_store）
    python migrate_json_to_db.py coins             # 迁移行情CSV（crypto_coins/star_market）
    python migrate_json_to_db.py cache             # 迁移缓存类（合约规格/市场扫描，整份存入kv_store）
    python migrate_json_to_db.py <batch> --clean   # 先清空目标表再迁移（重跑用）

每批次流程：读取 JSON → 事务内导入 → round-trip 校验（从 DB 重组结构逐字段 diff）
校验失败自动回滚，绝不留下半迁移状态。

批次计划：
    journal      ✅
    calorie      ✅
    plans        ✅
    balance      ✅
    trader_state ✅
    trade_journal ✅
    configs      ✅
    coins        ✅
    cache        ✅（全部 8 批迁移完成）

注意：迁移已完成，原始 JSON/CSV 源文件已归档移除；
本脚本仅作历史留档，重跑需先恢复源文件（数据权威源为 MySQL）。
"""

import os
import sys
import json
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Windows 终端 GBK → UTF-8，避免中文输出报错
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from crypto.data_paths import resolve_data_dir  # noqa: E402
from crypto.database import init_db, session_scope, get_engine  # noqa: E402


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def _data_file(filename):
    """定位数据目录中的源 JSON 文件（数据目录解析见 data_paths.resolve_data_dir）"""
    data_dir = resolve_data_dir()
    path = os.path.join(data_dir, filename) if data_dir else os.path.join(_HERE, 'crypto', filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f'源数据文件不存在: {path}')
    return path


# =============================================================================
# 批次：随笔模块（journal_notes.json → journal_tags / journal_notes / note_tags）
# =============================================================================

def _journal_clean(session):
    from sqlalchemy import delete
    from crypto.models import NoteTag, JournalNote, JournalTag
    session.execute(delete(NoteTag))
    session.execute(delete(JournalNote))
    session.execute(delete(JournalTag))
    session.flush()


def migrate_journal():
    from crypto.models import JournalTag, JournalNote, NoteTag
    from crypto import journal_repo as repo

    src = _data_file('journal_notes.json')
    with open(src, 'r', encoding='utf-8') as f:
        data = json.load(f)
    src_tags = data.get('tags', [])
    src_notes = data.get('notes', [])
    print(f'[journal] 源文件: {src}')
    print(f'[journal] 待导入: 标签 {len(src_tags)} 个, 条目 {len(src_notes)} 条')

    clean = '--clean' in sys.argv
    with session_scope() as session:
        if clean:
            _journal_clean(session)
            print('[journal] 已清空目标表（--clean）')

        # ---- 标签：按原数组顺序写入（created_at 递增 1 秒保持顺序） ----
        base_ts = datetime.datetime(2026, 1, 1, 0, 0, 0)
        for i, t in enumerate(src_tags):
            name = (t.get('name') or '').strip()
            if not name:
                continue
            if session.get(JournalTag, name) is not None:
                # 已存在（可能是 init_db 预置的默认标签）→ 只校正颜色
                session.get(JournalTag, name).color = t.get('color', '#999')
                session.get(JournalTag, name).created_at = base_ts + datetime.timedelta(seconds=i)
            else:
                session.add(JournalTag(
                    name=name, color=t.get('color', '#999'),
                    created_at=base_ts + datetime.timedelta(seconds=i)))
        session.flush()

        # ---- 条目 ----
        for n in src_notes:
            note_id = n.get('id', '')
            if not note_id:
                print(f'[journal] ⚠️ 跳过无 id 条目: {str(n)[:60]}')
                continue
            if session.get(JournalNote, note_id) is not None:
                print(f'[journal] ⚠️ 条目已存在，跳过: {note_id}')
                continue
            rv = n.get('review') or {}
            is_review = n.get('type') == 'review'
            session.add(JournalNote(
                id=note_id,
                type=n.get('type', 'note'),
                content=n.get('content', ''),
                review_subject=rv.get('subject', '') if is_review else None,
                review_decision=rv.get('decision', '') if is_review else None,
                review_outcome=rv.get('outcome', '') if is_review else None,
                review_lesson=rv.get('lesson', '') if is_review else None,
                pinned=bool(n.get('pinned', False)),
                distilled=bool(n.get('distilled', False)),
                linked_from=n.get('linked_from') or '',
                created_at=_parse_ts(n.get('created_at')),
                updated_at=_parse_ts(n.get('updated_at'))
            ))
            session.flush()
            # 条目标签：缺失的标签自动补建（防御脏数据）
            for t in n.get('tags', []):
                if session.get(JournalTag, t) is None:
                    session.add(JournalTag(name=t, color='#999'))
                    session.flush()
                session.add(NoteTag(note_id=note_id, tag_name=t))

        # ---- round-trip 校验：DB 重组结构 vs 源 JSON 逐字段比对 ----
        db_data = repo.load_journal_data(session)
        errors = _verify_journal(src_tags, src_notes, db_data)
        if errors:
            for e in errors:
                print(f'[journal] ❌ 校验失败: {e}')
            raise RuntimeError(f'round-trip 校验发现 {len(errors)} 处差异，事务已回滚')

    print(f'[journal] ✅ 迁移完成且校验通过（标签 {len(src_tags)} / 条目 {len(src_notes)}）')


def _verify_journal(src_tags, src_notes, db_data):
    """逐字段比对；返回差异描述列表（空列表 = 完全一致）"""
    errors = []

    # 标签：名称序列 + 颜色（顺序以名称序列比对，颜色按名映射）
    src_names = [(t.get('name') or '').strip() for t in src_tags if (t.get('name') or '').strip()]
    db_names = [t['name'] for t in db_data['tags']]
    if src_names != db_names:
        errors.append(f'标签序列不一致: 源={src_names} 库={db_names}')
    src_colors = {t['name']: t.get('color', '#999') for t in
                  [{'name': (x.get('name') or '').strip(), 'color': x.get('color')} for x in src_tags]}
    for t in db_data['tags']:
        if src_colors.get(t['name'], '#999') != t['color']:
            errors.append(f"标签颜色不一致: {t['name']} 源={src_colors.get(t['name'])} 库={t['color']}")

    # 条目：按 id 映射逐一比对
    db_by_id = {n['id']: n for n in db_data['notes']}
    if len(db_by_id) != len(src_notes):
        errors.append(f'条目数量不一致: 源={len(src_notes)} 库={len(db_by_id)}')
    for n in src_notes:
        d = db_by_id.get(n.get('id'))
        if d is None:
            errors.append(f"条目缺失: {n.get('id')}")
            continue
        for key in ('type', 'content', 'pinned', 'distilled', 'linked_from',
                    'created_at', 'updated_at'):
            sv, dv = n.get(key), d.get(key)
            if key in ('pinned', 'distilled'):
                sv, dv = bool(sv), bool(dv)
            sv = sv if sv is not None else ('' if key in ('linked_from',) else sv)
            if sv != dv:
                errors.append(f"条目 {n.get('id')} 字段 {key} 不一致: 源={sv!r} 库={dv!r}")
        # 标签集合比对（顺序不敏感）
        if set(n.get('tags', [])) != set(d.get('tags', [])):
            errors.append(f"条目 {n.get('id')} 标签不一致: 源={n.get('tags')} 库={d.get('tags')}")
        # 复盘四格比对
        srv, drv = n.get('review'), d.get('review')
        if n.get('type') == 'review':
            srv = {k: (srv or {}).get(k, '') for k in ('subject', 'decision', 'outcome', 'lesson')}
            if srv != drv:
                errors.append(f"条目 {n.get('id')} 复盘四格不一致: 源={srv} 库={drv}")
        elif drv is not None:
            errors.append(f"条目 {n.get('id')} 非复盘但库中有 review: {drv}")
    return errors


# =============================================================================
# 批次：热量模块（calorie_food_db.json / calorie_records.json
#        → calorie_foods / calorie_records / calorie_meal_items / calorie_config）
# =============================================================================

_MEALS = ('breakfast', 'lunch', 'dinner')


def _calorie_clean(session):
    from sqlalchemy import delete
    from crypto.models import CalorieMealItem, CalorieRecord, CalorieFood, CalorieConfig
    session.execute(delete(CalorieMealItem))
    session.execute(delete(CalorieRecord))
    session.execute(delete(CalorieFood))
    session.execute(delete(CalorieConfig))
    session.flush()


def migrate_calorie():
    from crypto.models import CalorieFood, CalorieRecord, CalorieMealItem, CalorieConfig
    from crypto import calorie_repo as repo

    src_food = _data_file('calorie_food_db.json')
    src_rec = _data_file('calorie_records.json')
    with open(src_food, 'r', encoding='utf-8') as f:
        food_data = json.load(f)
    with open(src_rec, 'r', encoding='utf-8') as f:
        rec_data = json.load(f)
    src_foods = food_data.get('foods', [])
    src_records = rec_data.get('records', [])
    src_config = rec_data.get('config') or {}
    print(f'[calorie] 源文件: {src_food}')
    print(f'[calorie] 源文件: {src_rec}')
    print(f'[calorie] 待导入: 食物 {len(src_foods)} 个, 记录 {len(src_records)} 条, 配置 1 份')

    clean = '--clean' in sys.argv
    with session_scope() as session:
        if clean:
            _calorie_clean(session)
            print('[calorie] 已清空目标表（--clean）')

        # ---- 食物库：按原数组顺序写入（保留原 id / created_at）----
        for fd in src_foods:
            fid = fd.get('id', '')
            if not fid:
                print(f'[calorie] ⚠️ 跳过无 id 食物: {str(fd)[:60]}')
                continue
            if session.get(CalorieFood, fid) is not None:
                print(f'[calorie] ⚠️ 食物已存在，跳过: {fid}')
                continue
            session.add(CalorieFood(
                id=fid,
                name=fd.get('name', ''),
                unit=fd.get('unit', '100克'),
                calories=float(fd.get('calories', 0) or 0),
                category=fd.get('category', '其他'),
                created_at=_parse_ts(fd.get('created_at'))))
        session.flush()

        # ---- 每日记录 + 三餐明细 ----
        for r in src_records:
            rid = r.get('id', '')
            if not rid:
                print(f'[calorie] ⚠️ 跳过无 id 记录: {str(r)[:60]}')
                continue
            if session.get(CalorieRecord, rid) is not None:
                print(f'[calorie] ⚠️ 记录已存在，跳过: {rid}')
                continue
            session.add(CalorieRecord(
                id=rid,
                date=r.get('date', rid),
                morning_weight=r.get('morning_weight'),
                evening_weight=r.get('evening_weight'),
                bmr=float(r.get('bmr', 0) or 0),
                breakfast_food=r.get('breakfast_food', '') or '',
                breakfast_calories=float(r.get('breakfast_calories', 0) or 0),
                lunch_food=r.get('lunch_food', '') or '',
                lunch_calories=float(r.get('lunch_calories', 0) or 0),
                dinner_food=r.get('dinner_food', '') or '',
                dinner_calories=float(r.get('dinner_calories', 0) or 0),
                intake_deficit=float(r.get('intake_deficit', 0) or 0),
                daily_steps=int(r.get('daily_steps', 0) or 0),
                exercise_calories=float(r.get('exercise_calories', 0) or 0),
                calorie_deficit=float(r.get('calorie_deficit', 0) or 0),
                cumulative_deficit=float(r.get('cumulative_deficit', 0) or 0),
                created_at=_parse_ts(r.get('created_at')),
                updated_at=_parse_ts(r.get('updated_at'))))
            session.flush()
            for meal in _MEALS:
                for pos, item in enumerate(r.get(f'{meal}_foods') or []):
                    session.add(CalorieMealItem(
                        record_id=rid, meal=meal, position=pos,
                        name=str(item.get('name', '') or '')[:64],
                        calories=float(item.get('calories', 0) or 0)))
            session.flush()

        # ---- 配置：单行表 UPSERT ----
        cfg_row = session.get(CalorieConfig, 1)
        if cfg_row is None:
            cfg_row = CalorieConfig(id=1)
            session.add(cfg_row)
        for key in ('height', 'age', 'step_frequency', 'weight_factor', 'target_deficit'):
            if key in src_config:
                setattr(cfg_row, key, float(src_config[key]))
        session.flush()

        # ---- round-trip 校验：DB 重组结构 vs 源 JSON 逐字段比对 ----
        errors = []
        errors += _verify_calorie_foods(src_foods, repo.load_foods(session))
        errors += _verify_calorie_records(src_records, repo.load_records(session))
        errors += _verify_calorie_config(src_config, repo.load_config(session))
        if errors:
            for e in errors:
                print(f'[calorie] ❌ 校验失败: {e}')
            raise RuntimeError(f'round-trip 校验发现 {len(errors)} 处差异，事务已回滚')

    print(f'[calorie] ✅ 迁移完成且校验通过（食物 {len(src_foods)} / 记录 {len(src_records)} / 配置 1）')


def _num_eq(a, b, tol=0.01):
    """数值容差比对（None 视为 0）"""
    return abs(float(a or 0) - float(b or 0)) < tol


def _verify_calorie_foods(src_foods, db_foods):
    errors = []
    src_by_id = {f['id']: f for f in src_foods if f.get('id')}
    if len(db_foods) != len(src_by_id):
        errors.append(f'食物数量不一致: 源={len(src_by_id)} 库={len(db_foods)}')
    for idx, d in enumerate(db_foods):
        s = src_by_id.get(d['id'])
        if s is None:
            errors.append(f"食物多出: {d['id']}")
            continue
        # 顺序比对（库内以 id 升序 = 原数组顺序）
        if src_foods[idx].get('id') != d['id']:
            errors.append(f"食物顺序不一致 @ {idx}: 源={src_foods[idx].get('id')} 库={d['id']}")
        for key in ('name', 'unit', 'category'):
            if (s.get(key) or '') != (d.get(key) or ''):
                errors.append(f"食物 {d['id']} 字段 {key} 不一致: 源={s.get(key)!r} 库={d.get(key)!r}")
        if not _num_eq(s.get('calories'), d.get('calories')):
            errors.append(f"食物 {d['id']} calories 不一致: 源={s.get('calories')} 库={d.get('calories')}")
        if (s.get('created_at') or '') != (d.get('created_at') or ''):
            errors.append(f"食物 {d['id']} created_at 不一致: 源={s.get('created_at')!r} 库={d.get('created_at')!r}")
    return errors


def _verify_calorie_records(src_records, db_records):
    errors = []
    db_by_id = {r['id']: r for r in db_records}
    if len(db_by_id) != len(src_records):
        errors.append(f'记录数量不一致: 源={len(src_records)} 库={len(db_by_id)}')
    for s in src_records:
        d = db_by_id.get(s.get('id'))
        if d is None:
            errors.append(f"记录缺失: {s.get('id')}")
            continue
        rid = s.get('id')
        if (s.get('date') or rid) != d.get('date'):
            errors.append(f"记录 {rid} date 不一致: 源={s.get('date')!r} 库={d.get('date')!r}")
        for key in ('morning_weight', 'evening_weight'):
            if not _num_eq(s.get(key), d.get(key)):
                errors.append(f"记录 {rid} 字段 {key} 不一致: 源={s.get(key)} 库={d.get(key)}")
        for key in ('bmr', 'breakfast_calories', 'lunch_calories', 'dinner_calories',
                    'intake_deficit', 'exercise_calories', 'calorie_deficit', 'cumulative_deficit'):
            if not _num_eq(s.get(key), d.get(key)):
                errors.append(f"记录 {rid} 字段 {key} 不一致: 源={s.get(key)} 库={d.get(key)}")
        for key in ('breakfast_food', 'lunch_food', 'dinner_food', 'created_at', 'updated_at'):
            if (s.get(key) or '') != (d.get(key) or ''):
                errors.append(f"记录 {rid} 字段 {key} 不一致: 源={s.get(key)!r} 库={d.get(key)!r}")
        if int(s.get('daily_steps') or 0) != int(d.get('daily_steps') or 0):
            errors.append(f"记录 {rid} daily_steps 不一致: 源={s.get('daily_steps')} 库={d.get('daily_steps')}")
        # 三餐明细数组按顺序比对
        for meal in _MEALS:
            s_items = s.get(f'{meal}_foods') or []
            d_items = d.get(f'{meal}_foods') or []
            if len(s_items) != len(d_items):
                errors.append(f"记录 {rid} {meal} 明细数量不一致: 源={len(s_items)} 库={len(d_items)}")
                continue
            for i, (si, di) in enumerate(zip(s_items, d_items)):
                if (si.get('name') or '') != (di.get('name') or '') or not _num_eq(si.get('calories'), di.get('calories')):
                    errors.append(f"记录 {rid} {meal}[{i}] 不一致: 源={si} 库={di}")
    return errors


def _verify_calorie_config(src_config, db_config):
    errors = []
    for key in ('height', 'age', 'step_frequency', 'weight_factor', 'target_deficit'):
        sv = src_config.get(key)
        if sv is None:
            continue  # 源缺省时 repo 返回代码默认值，不算差异
        if not _num_eq(sv, db_config.get(key)):
            errors.append(f"配置 {key} 不一致: 源={sv} 库={db_config.get(key)}")
    return errors


# =============================================================================
# 批次：任务计划模块（task_plans.json → plan_plans / plan_cards / plan_slots）
# -----------------------------------------------------------------------------
# 整树写入（plan_repo.save_plans_data 内部差量同步，迁移时全量插入）；
# 2000 个格子逐格落库，校验时对 filled 格子逐字段比对。
# =============================================================================

def _plans_clean(session):
    from sqlalchemy import delete
    from crypto.models import PlanSlot, PlanCard, PlanPlan, KVStore
    session.execute(delete(PlanSlot))
    session.execute(delete(PlanCard))
    session.execute(delete(PlanPlan))
    session.execute(delete(KVStore).where(KVStore.key == 'task_plans_initialized'))
    session.flush()


def migrate_plans():
    from crypto import plan_repo as repo

    src = _data_file('task_plans.json')
    with open(src, 'r', encoding='utf-8') as f:
        data = json.load(f)
    src_plans = data.get('plans', [])
    n_cards = sum(len(p.get('cards', [])) for p in src_plans)
    n_slots = sum(len(c.get('slots', [])) for p in src_plans for c in p.get('cards', []))
    n_filled = sum(1 for p in src_plans for c in p.get('cards', [])
                   for s in c.get('slots', []) if s.get('filled'))
    print(f'[plans] 源文件: {src}')
    print(f'[plans] 待导入: 计划 {len(src_plans)} 个, 卡片 {n_cards} 张, '
          f'格子 {n_slots} 个（已打卡 {n_filled}）')

    clean = '--clean' in sys.argv
    with session_scope() as session:
        if clean:
            _plans_clean(session)
            print('[plans] 已清空目标表（--clean）')

        # ---- 整树写入（含 initialized 标记）----
        data['initialized'] = True
        repo.save_plans_data(session, data)

        # ---- round-trip 校验：DB 重组结构 vs 源 JSON 逐字段比对 ----
        db_data = repo.load_plans_data(session)
        errors = _verify_plans(src_plans, db_data.get('plans', []))
        if errors:
            for e in errors[:20]:
                print(f'[plans] ❌ 校验失败: {e}')
            if len(errors) > 20:
                print(f'[plans] ... 其余 {len(errors) - 20} 处差异省略')
            raise RuntimeError(f'round-trip 校验发现 {len(errors)} 处差异，事务已回滚')

    print(f'[plans] ✅ 迁移完成且校验通过（计划 {len(src_plans)} / 卡片 {n_cards} / '
          f'已打卡 {n_filled}）')


_TRADE_RECORD_KEYS = ('prediction', 'duration_minutes', 'actual',
                      'market_analysis', 'action_advice', 'account_balance')


def _verify_plans(src_plans, db_plans):
    """逐字段比对；返回差异描述列表（空列表 = 完全一致）"""
    errors = []
    if len(src_plans) != len(db_plans):
        errors.append(f'计划数量不一致: 源={len(src_plans)} 库={len(db_plans)}')
        return errors
    for sp, dp in zip(src_plans, db_plans):
        pid = sp.get('id')
        if pid != dp.get('id'):
            errors.append(f'计划顺序/ID不一致: 源={pid} 库={dp.get("id")}')
            continue
        for key in ('type', 'name', 'grand_goal', 'total_hours',
                    'round_count', 'per_round_hours', 'created_at'):
            if sp.get(key) != dp.get(key):
                errors.append(f'计划 {pid} 字段 {key} 不一致: 源={sp.get(key)!r} 库={dp.get(key)!r}')
        if (sp.get('daily_rule') or {}) != (dp.get('daily_rule') or {}):
            errors.append(f'计划 {pid} daily_rule 不一致')
        if sp.get('round_config') != dp.get('round_config'):
            errors.append(f'计划 {pid} round_config 不一致: '
                          f'源={sp.get("round_config")} 库={dp.get("round_config")}')
        errors += _verify_plan_cards(pid, sp.get('cards', []), dp.get('cards', []))
    return errors


def _verify_plan_cards(pid, src_cards, db_cards):
    errors = []
    if len(src_cards) != len(db_cards):
        errors.append(f'计划 {pid} 卡片数量不一致: 源={len(src_cards)} 库={len(db_cards)}')
        return errors
    for sc, dc in zip(src_cards, db_cards):
        cid = sc.get('id')
        if cid != dc.get('id'):
            errors.append(f'计划 {pid} 卡片顺序/ID不一致: 源={cid} 库={dc.get("id")}')
            continue
        for key in ('type', 'round', 'title', 'goal', 'reward', 'status',
                    'start_time', 'end_time', 'review', 'created_at', 'updated_at'):
            if sc.get(key) != dc.get(key):
                errors.append(f'卡片 {cid} 字段 {key} 不一致: 源={sc.get(key)!r} 库={dc.get(key)!r}')
        for key in ('base_reward', 'hourly_rate', 'settlement'):
            if sc.get(key) != dc.get(key):
                errors.append(f'卡片 {cid} 字段 {key} 不一致: 源={sc.get(key)!r} 库={dc.get(key)!r}')
        if (sc.get('milestones') or []) != (dc.get('milestones') or []):
            errors.append(f'卡片 {cid} milestones 不一致')
        if (sc.get('notes') or []) != (dc.get('notes') or []):
            errors.append(f'卡片 {cid} notes 不一致')
        errors += _verify_card_slots(cid, sc, sc.get('slots', []), dc.get('slots', []))
    return errors


def _verify_card_slots(cid, card, src_slots, db_slots):
    errors = []
    if len(src_slots) != len(db_slots):
        errors.append(f'卡片 {cid} 格子数量不一致: 源={len(src_slots)} 库={len(db_slots)}')
        return errors
    for ss, ds in zip(src_slots, db_slots):
        idx = ss.get('slot_index')
        if idx != ds.get('slot_index'):
            errors.append(f'卡片 {cid} 格子顺序不一致 @ {idx}')
            continue
        if bool(ss.get('filled')) != bool(ds.get('filled')):
            errors.append(f'卡片 {cid} 格子[{idx}] filled 不一致')
            continue
        if not ss.get('filled'):
            continue
        if (ss.get('filled_at') or '') != (ds.get('filled_at') or ''):
            errors.append(f'卡片 {cid} 格子[{idx}] filled_at 不一致: '
                          f'源={ss.get("filled_at")!r} 库={ds.get("filled_at")!r}')
        sr, dr = ss.get('record'), ds.get('record')
        if sr is None and dr is None:
            continue
        if sr is None or dr is None:
            errors.append(f'卡片 {cid} 格子[{idx}] record 空值不一致: 源={sr!r} 库={dr!r}')
            continue
        if card.get('type') == 'trade':
            for key in _TRADE_RECORD_KEYS:
                if key == 'duration_minutes':
                    if int(sr.get(key) or 0) != int(dr.get(key) or 0):
                        errors.append(f'卡片 {cid} 格子[{idx}] record.{key} 不一致')
                elif str(sr.get(key) or '') != str(dr.get(key) or ''):
                    errors.append(f'卡片 {cid} 格子[{idx}] record.{key} 不一致')
            if bool(sr.get('hit')) != bool(dr.get('hit')):
                # 源数据无 hit 键时视为 False
                if 'hit' in sr or 'hit' in dr:
                    errors.append(f'卡片 {cid} 格子[{idx}] record.hit 不一致: '
                                  f'源={sr.get("hit")} 库={dr.get("hit")}')
        else:
            if str(sr.get('content') or '') != str(dr.get('content') or ''):
                errors.append(f'卡片 {cid} 格子[{idx}] record.content 不一致')
            if int(sr.get('duration_minutes') or 0) != int(dr.get('duration_minutes') or 0):
                errors.append(f'卡片 {cid} 格子[{idx}] record.duration_minutes 不一致')
    return errors


# =============================================================================
# 批次：账户余额历史（account_balance_history.json → balance_history）
# =============================================================================

def _balance_clean(session):
    from sqlalchemy import delete
    from crypto.models import BalanceHistory
    session.execute(delete(BalanceHistory))
    session.flush()


def migrate_balance():
    from crypto import balance_repo as repo

    src = _data_file('account_balance_history.json')
    with open(src, 'r', encoding='utf-8') as f:
        data = json.load(f)
    accounts = data.get('accounts') if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        accounts = {}
    n_points = sum(len(v) for v in accounts.values() if isinstance(v, list))
    print(f'[balance] 源文件: {src}')
    print(f'[balance] 待导入: 账号 {len(accounts)} 个, 快照点 {n_points} 个')

    clean = '--clean' in sys.argv
    with session_scope() as session:
        if clean:
            _balance_clean(session)
            print('[balance] 已清空目标表（--clean）')

        # ---- 逐账号写入（同 ts 去重，保留 source 语义）----
        for account_key, points in accounts.items():
            if not isinstance(points, list):
                continue
            seen = {}
            for p in points:
                if not isinstance(p, dict) or not p.get('ts'):
                    continue
                try:
                    ts = int(p['ts'])
                    bal = round(float(p.get('balance') or 0), 4)
                except (TypeError, ValueError):
                    continue
                seen[ts] = {
                    'ts': ts, 'balance': bal,
                    'source': 'backfill' if p.get('source') == 'backfill' else 'snapshot'
                }
            for ts in sorted(seen):
                pt = seen[ts]
                repo.upsert_point(session, account_key, pt['ts'], pt['balance'], pt['source'])

        # ---- round-trip 校验：DB 重组结构 vs 源 JSON 逐字段比对 ----
        db_data = repo.load_all(session)
        errors = _verify_balance(accounts, db_data.get('accounts', {}))
        if errors:
            for e in errors[:20]:
                print(f'[balance] ❌ 校验失败: {e}')
            if len(errors) > 20:
                print(f'[balance] ... 其余 {len(errors) - 20} 处差异省略')
            raise RuntimeError(f'round-trip 校验发现 {len(errors)} 处差异，事务已回滚')

    print(f'[balance] ✅ 迁移完成且校验通过（账号 {len(accounts)} / 快照点 {n_points}）')


def _verify_balance(src_accounts, db_accounts):
    """逐点比对 ts/balance/source；返回差异描述列表（空列表 = 完全一致）"""
    errors = []
    src_keys = [k for k, v in src_accounts.items() if isinstance(v, list) and v]
    if set(src_keys) != set(db_accounts.keys()):
        errors.append(f'账号集合不一致: 源={sorted(src_keys)} 库={sorted(db_accounts.keys())}')
        return errors
    for key in src_keys:
        src_pts = {}
        for p in src_accounts[key]:
            if not isinstance(p, dict) or not p.get('ts'):
                continue
            try:
                src_pts[int(p['ts'])] = (
                    round(float(p.get('balance') or 0), 4),
                    'backfill' if p.get('source') == 'backfill' else 'snapshot')
            except (TypeError, ValueError):
                continue
        db_pts = {
            p['ts']: (p['balance'], 'backfill' if p.get('source') == 'backfill' else 'snapshot')
            for p in db_accounts.get(key, [])}
        if len(src_pts) != len(db_pts):
            errors.append(f'{key} 点数不一致: 源={len(src_pts)} 库={len(db_pts)}')
            continue
        for ts, (bal, source) in src_pts.items():
            if ts not in db_pts:
                errors.append(f'{key} 缺少点 ts={ts}')
            elif db_pts[ts][0] != bal or db_pts[ts][1] != source:
                errors.append(f'{key} 点 ts={ts} 不一致: 源=({bal},{source}) 库={db_pts[ts]}')
    return errors


# =============================================================================
# 批次：交易运行时状态（5 个调度器状态 JSON → 8 张表）
#   scheduler_state.json      → trader_directions
#   reverse_guard_state.json  → reverse_guard
#   manual_pause_state.json   → manual_pause（文件可能不存在，按空处理）
#   tp_runtime_state.json     → tp_runtime_state
#   position_order_state.json → pos_book/pos_slot/pos_algo/pos_lev
# =============================================================================

def _trader_state_clean(session):
    from sqlalchemy import delete
    from crypto.models import (TraderDirection, ReverseGuard, ManualPause,
                               TpRuntimeState, PosBook, PosSlot, PosAlgo, PosLev)
    for m in (PosSlot, PosAlgo, PosBook, PosLev,
              TraderDirection, ReverseGuard, ManualPause, TpRuntimeState):
        session.execute(delete(m))
    session.flush()


def _load_state_json(filename):
    """读取状态 JSON；文件不存在按空 dict 处理（manual_pause_state.json 仅运行时生成）"""
    path = None
    try:
        path = _data_file(filename)
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return path, data
    except FileNotFoundError:
        path = path or filename
    except (OSError, ValueError) as e:
        print(f'[trader_state] ⚠️ 读取 {filename} 失败按空处理: {e}')
    return path, {}


def migrate_trader_state():
    from crypto import trader_state_repo as repo

    _, directions = _load_state_json('scheduler_state.json')
    _, guard = _load_state_json('reverse_guard_state.json')
    _, pause = _load_state_json('manual_pause_state.json')
    _, tp_state = _load_state_json('tp_runtime_state.json')
    _, pos_state = _load_state_json('position_order_state.json')
    print(f'[trader_state] 待导入: 方向记录 {len(directions)} 币种 / 风控计时 {len(guard)} / '
          f'强平冷却 {len(pause)} / 止盈状态 {len(tp_state)} / 双仓位账本 {len(pos_state)} 币种')

    clean = '--clean' in sys.argv
    with session_scope() as session:
        if clean:
            _trader_state_clean(session)
            print('[trader_state] 已清空目标表（--clean）')

        repo.save_directions(session, directions)
        repo.save_reverse_guard(session, guard)
        repo.save_manual_pause(session, pause)
        for key, st in tp_state.items():
            repo.upsert_tp_state(session, key, st)
        repo.save_position_state(session, pos_state)

        # ---- round-trip 校验：DB 重组结构 vs 源 JSON 逐字段比对 ----
        errors = []
        errors += _verify_state_map('scheduler_state', directions,
                                    repo.load_directions(session))
        errors += _verify_state_map('reverse_guard', guard,
                                    repo.load_reverse_guard(session))
        errors += _verify_state_map('manual_pause', pause,
                                    repo.load_manual_pause(session))
        errors += _verify_state_map('tp_runtime_state', tp_state,
                                    repo.load_tp_state(session))
        errors += _verify_position(pos_state, repo.load_position_state(session))
        if errors:
            for e in errors[:20]:
                print(f'[trader_state] ❌ 校验失败: {e}')
            if len(errors) > 20:
                print(f'[trader_state] ... 其余 {len(errors) - 20} 处差异省略')
            raise RuntimeError(f'round-trip 校验发现 {len(errors)} 处差异，事务已回滚')

    print(f'[trader_state] ✅ 迁移完成且校验通过（方向 {len(directions)} / 风控 {len(guard)} / '
          f'冷却 {len(pause)} / 止盈 {len(tp_state)} / 账本 {len(pos_state)} 币种）')


def _verify_state_map(name, src, db):
    """简单 {key: value} 状态表逐键比对；返回差异描述列表（空 = 完全一致）"""
    if src == db:
        return []
    errors = []
    for k in set(src) | set(db):
        if src.get(k) != db.get(k):
            errors.append(f'{name}[{k}] 不一致: 源={src.get(k)} 库={db.get(k)}')
    return errors


def _verify_position(src, db):
    """双仓位账本嵌套结构逐币种逐子结构比对"""
    errors = []
    if set(src.keys()) != set(db.keys()):
        errors.append(f'position 币种集合不一致: 源={sorted(src.keys())} 库={sorted(db.keys())}')
    for inst in src:
        s, d = src.get(inst) or {}, db.get(inst) or {}
        if s == d:
            continue
        for key in set(list(s.keys()) + list(d.keys())):
            if s.get(key) != d.get(key):
                errors.append(f'position[{inst}].{key} 不一致: '
                              f'源={s.get(key)} 库={d.get(key)}')
    return errors


# =============================================================================
# 批次：结构化成交流水（logs/trade_journal.jsonl → trade_journal）
# -----------------------------------------------------------------------------
# append-only 流水按原行序逐条插入（自增 id 保留写入顺序）；
# 脏行（JSON 解析失败/非 dict）与 JSONL 版读取语义一致直接跳过。
# =============================================================================

def _trade_journal_clean(session):
    from sqlalchemy import delete
    from crypto.models import TradeJournal
    session.execute(delete(TradeJournal))
    session.flush()


def migrate_trade_journal():
    from sqlalchemy import select
    from crypto import trade_journal_repo as repo
    from crypto.models import TradeJournal

    src = os.path.join(_HERE, 'crypto', 'logs', 'trade_journal.jsonl')
    if not os.path.isfile(src):
        raise FileNotFoundError(f'源数据文件不存在: {src}')
    src_recs = []
    n_dirty = 0
    with open(src, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                n_dirty += 1
                continue
            if not isinstance(rec, dict):
                n_dirty += 1
                continue
            src_recs.append(rec)
    print(f'[trade_journal] 源文件: {src}')
    print(f'[trade_journal] 待导入: {len(src_recs)} 条'
          + (f'（跳过脏行 {n_dirty}）' if n_dirty else ''))

    clean = '--clean' in sys.argv
    with session_scope() as session:
        if clean:
            _trade_journal_clean(session)
            print('[trade_journal] 已清空目标表（--clean）')

        # ---- 按原行序逐条插入（归一化规则与 record_fill 一致）----
        for rec in src_recs:
            repo.append_fill(session, {
                'ts': rec.get('ts', ''),
                'run_id': rec.get('run_id', ''),
                'inst_id': rec.get('inst_id', ''),
                'bucket': rec.get('bucket', ''),
                'direction': rec.get('direction', ''),
                'action': rec.get('action', ''),
                'price': round(float(rec.get('price') or 0), 8),
                'amount': round(float(rec.get('amount') or 0), 4),
                'ord_id': rec.get('ord_id', ''),
                'reason': rec.get('reason') or 'signal',
            })
        session.flush()

        # ---- round-trip 校验：按 id 序逐行与源记录逐字段比对 ----
        db_recs = [r.to_dict() for r in session.execute(
            select(TradeJournal).order_by(TradeJournal.id)).scalars().all()]
        errors = _verify_trade_journal(src_recs, db_recs)
        if errors:
            for e in errors[:20]:
                print(f'[trade_journal] ❌ 校验失败: {e}')
            if len(errors) > 20:
                print(f'[trade_journal] ... 其余 {len(errors) - 20} 处差异省略')
            raise RuntimeError(f'round-trip 校验发现 {len(errors)} 处差异，事务已回滚')

    print(f'[trade_journal] ✅ 迁移完成且校验通过（{len(src_recs)} 条）')


_TJ_KEYS = ('ts', 'run_id', 'inst_id', 'bucket', 'direction', 'action',
            'price', 'amount', 'ord_id', 'reason')


def _verify_trade_journal(src_recs, db_recs):
    """逐行逐字段比对（归一化后）；返回差异描述列表（空 = 完全一致）"""
    errors = []
    if len(src_recs) != len(db_recs):
        errors.append(f'记录数不一致: 源={len(src_recs)} 库={len(db_recs)}')
        return errors
    for i, (s, d) in enumerate(zip(src_recs, db_recs)):
        for key in _TJ_KEYS:
            sv = s.get(key)
            dv = d.get(key)
            if key in ('price', 'amount'):
                sv, dv = round(float(sv or 0), 8), round(float(dv or 0), 8)
            else:
                sv = str(sv if sv is not None else '') or ('signal' if key == 'reason' else '')
            if sv != dv:
                errors.append(f'第{i + 1}行 {key} 不一致: 源={s.get(key)!r} 库={d.get(key)!r}')
    return errors


# =============================================================================
# 批次7a：策略配置 / 币种自选（整份存入 kv_store，保留全部键含 _comment_*）
# =============================================================================

# (kv_store 键, 源文件相对 cryptoTrade 根的路径)
_CONFIG_SOURCES = (
    ('strategy_config', os.path.join('crypto', 'task', 'config', 'config_trend_range.json')),
    ('coin_selection', os.path.join('crypto', 'config.json')),
)


def migrate_configs():
    from crypto import config_store_repo as repo

    clean = '--clean' in sys.argv
    with session_scope() as session:
        if clean:
            for key, _rel in _CONFIG_SOURCES:
                repo.delete_json_config(session, key)
            session.flush()
            print('[configs] 已删除目标 kv_store 键（--clean）')

        for key, rel in _CONFIG_SOURCES:
            src = os.path.join(_HERE, rel)
            if not os.path.isfile(src):
                raise FileNotFoundError(f'源配置文件不存在: {src}')
            with open(src, 'r', encoding='utf-8') as f:
                data = json.load(f)
            repo.save_json_config(session, key, data)
            print(f'[configs] {key} ← {rel}（顶层键 {len(data)} 个）')
        session.flush()

        # ---- round-trip 校验：从 DB 读回与源文件深度相等 ----
        errors = []
        for key, rel in _CONFIG_SOURCES:
            src = os.path.join(_HERE, rel)
            with open(src, 'r', encoding='utf-8') as f:
                src_data = json.load(f)
            db_data = repo.load_json_config(session, key)
            if db_data is None:
                errors.append(f'{key}: DB 读取为空')
            elif db_data != src_data:
                # 定位首个差异键，便于排查
                diff_keys = [k for k in set(src_data) | set(db_data)
                             if src_data.get(k) != db_data.get(k)]
                errors.append(f'{key}: 内容不一致（差异键: {sorted(diff_keys)[:5]}）')
        if errors:
            for e in errors:
                print(f'[configs] ❌ 校验失败: {e}')
            raise RuntimeError(f'round-trip 校验发现 {len(errors)} 处差异，事务已回滚')

    print(f'[configs] ✅ 迁移完成且校验通过（{len(_CONFIG_SOURCES)} 份配置）')


# =============================================================================
# 批次7b：行情 CSV（crypto_coins.csv / star币种行情.csv → 两表，整表覆盖）
# =============================================================================

_COIN_CSV = os.path.join(_HERE, 'crypto', 'crypto_coins.csv')
_STAR_CSV = os.path.join(_HERE, 'crypto', 'star币种行情.csv')

# 与 models.CryptoCoin._FIELD_MAP / StarMarketRow._FIELD_MAP 的 CSV 列名一致
_COIN_COLS = [
    'rank', 'symbol', 'inst_id', 'name_cn',
    '1H_趋势', '1H_交易价格', '1H_交易时间', '1H_盈亏%', '1H_收盘价',
    'MACD_1H', 'DIF_1H', 'ADX_1H', 'ATR_1H', 'SAR_1H', 'SAR颜色_1H',
    '4H_趋势', '4H_交易价格', '4H_交易时间', '4H_盈亏%', '4H_收盘价',
    'MACD_4H', 'DIF_4H', 'ADX_4H', 'ATR_4H', 'SAR_4H', 'SAR颜色_4H',
    '1D_趋势', '1D_交易价格', '1D_交易时间', '1D_盈亏%', '1D_收盘价',
    'MACD_1D', 'DIF_1D', 'ADX_1D', 'ATR_1D', 'SAR_1D', 'SAR颜色_1D',
]
_STAR_COLS = [
    '名称', '代码', '现价', '15分钟', '60分钟', '4小时', '日线',
    '上次交易时间(1H)', '方向(1H)', '上次交易价格(1H)', '策略盈亏(1H)',
    '持仓时间', '预测涨跌', '建议操作',
]


def _read_csv_file(path: str) -> list:
    import csv
    if not os.path.isfile(path):
        raise FileNotFoundError(f'源CSV文件不存在: {path}')
    with open(path, 'r', encoding='utf-8-sig') as f:
        return [row for row in csv.DictReader(f)]


def _rank_key(row: dict):
    """rank 排序键（与 market_data_repo.load_coin_rows 一致）"""
    r = str(row.get('rank') or '')
    return int(r) if r.isdigit() else 10 ** 9


def _diff_rows(db_rows: list, src_rows: list, cols: list, label: str, errors: list):
    """逐行逐列比对（单元格均归一为字符串，缺失列归一为空串）"""
    if len(db_rows) != len(src_rows):
        errors.append(f'{label}: 行数不一致 DB={len(db_rows)} src={len(src_rows)}')
        return
    for i, (d, s) in enumerate(zip(db_rows, src_rows)):
        for col in cols:
            dv = str(d.get(col) or '')
            sv = str(s.get(col) or '')
            if dv != sv:
                errors.append(f'{label} 第{i + 1}行[{col}]: DB={dv!r} src={sv!r}')
                if len(errors) > 10:
                    return


def migrate_coins():
    from crypto import market_data_repo as repo

    coin_rows = _read_csv_file(_COIN_CSV)
    # star 表与 read_star_market_data 一致：过滤代码为空的行
    star_rows = [r for r in _read_csv_file(_STAR_CSV) if str(r.get('代码') or '').strip()]

    with session_scope() as session:
        if '--clean' in sys.argv:
            repo.save_coin_rows(session, [])
            repo.save_star_rows(session, [])
            session.flush()
            print('[coins] 已清空目标两表（--clean）')

        repo.save_coin_rows(session, coin_rows)
        print(f'[coins] crypto_coins ← crypto_coins.csv（{len(coin_rows)} 行 × {len(_COIN_COLS)} 列）')
        repo.save_star_rows(session, star_rows)
        print(f'[coins] star_market ← star币种行情.csv（{len(star_rows)} 行 × {len(_STAR_COLS)} 列）')
        session.flush()

        # ---- round-trip 校验：从 DB 读回与源 CSV 逐行逐列相等 ----
        errors = []
        db_coin = repo.load_coin_rows(session)
        src_coin_sorted = sorted(coin_rows, key=_rank_key)
        _diff_rows(db_coin, src_coin_sorted, _COIN_COLS, 'crypto_coins', errors)

        db_star = repo.load_star_rows(session)
        _diff_rows(db_star, star_rows, _STAR_COLS, 'star_market', errors)

        if errors:
            for e in errors[:10]:
                print(f'[coins] ❌ 校验失败: {e}')
            raise RuntimeError(f'round-trip 校验发现 {len(errors)} 处差异，事务已回滚')

    print(f'[coins] ✅ 迁移完成且校验通过（crypto_coins {len(coin_rows)} 行 / star_market {len(star_rows)} 行）')


# =============================================================================
# 批次8：缓存类（instrument_spec_cache / market_scan_cache 整份存入 kv_store）
# =============================================================================

# (kv_store 键, 外置数据目录中的源文件名)
_CACHE_SOURCES = (
    ('instrument_spec_cache', 'instrument_spec_cache.json'),
    ('market_scan_cache', 'market_scan_cache.json'),
)


def migrate_cache():
    from crypto import config_store_repo as repo

    clean = '--clean' in sys.argv
    with session_scope() as session:
        if clean:
            for key, _fname in _CACHE_SOURCES:
                repo.delete_json_config(session, key)
            session.flush()
            print('[cache] 已删除目标 kv_store 键（--clean）')

        for key, fname in _CACHE_SOURCES:
            src = _data_file(fname)
            with open(src, 'r', encoding='utf-8') as f:
                data = json.load(f)
            repo.save_json_config(session, key, data)
            print(f'[cache] {key} ← {fname}（顶层键 {len(data)} 个）')
        session.flush()

        # ---- round-trip 校验：从 DB 读回与源文件深度相等 ----
        errors = []
        for key, fname in _CACHE_SOURCES:
            src = _data_file(fname)
            with open(src, 'r', encoding='utf-8') as f:
                src_data = json.load(f)
            db_data = repo.load_json_config(session, key)
            if db_data is None:
                errors.append(f'{key}: DB 读取为空')
            elif db_data != src_data:
                diff_keys = [k for k in set(src_data) | set(db_data)
                             if src_data.get(k) != db_data.get(k)]
                errors.append(f'{key}: 内容不一致（差异键: {sorted(diff_keys)[:5]}）')
        if errors:
            for e in errors:
                print(f'[cache] ❌ 校验失败: {e}')
            raise RuntimeError(f'round-trip 校验发现 {len(errors)} 处差异，事务已回滚')

    print(f'[cache] ✅ 迁移完成且校验通过（{len(_CACHE_SOURCES)} 份缓存）')


# =============================================================================
# 入口
# =============================================================================

_BATCHES = {
    'journal': migrate_journal,
    'calorie': migrate_calorie,
    'plans': migrate_plans,
    'balance': migrate_balance,
    'trader_state': migrate_trader_state,
    'trade_journal': migrate_trade_journal,
    'configs': migrate_configs,
    'coins': migrate_coins,
    'cache': migrate_cache,
}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in _BATCHES:
        print(f"用法: python migrate_json_to_db.py <{'/'.join(_BATCHES)}> [--clean]")
        sys.exit(2)

    print('=' * 60)
    print(f"JSON → MySQL 迁移批次: {sys.argv[1]}")
    print('=' * 60)
    # 迁移/全新部署场景需完整建表（应用运行时 init_db 默认仅探活不建表）
    init_db(force_create=True)
    try:
        _BATCHES[sys.argv[1]]()
    except Exception as e:
        print(f'❌ 迁移失败（数据未变更）: {e}')
        sys.exit(1)
