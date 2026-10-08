#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热量模块数据访问层（MySQL 版）
================================
替代原 calorie_routes.py 中的 _load_records/_save_records/_load_food_db/_save_food_db。
业务语义与 JSON 版逐条对齐：
- 食物名不区分大小写查重（依赖 utf8mb4_general_ci 的 CI 排序规则）
- 新食物 id 生成规则 food_{现有最大序号+1:03d}
- 记录按日期排序重算 cumulative_deficit
- 配置单行表，无行时返回代码内默认值
"""

import datetime
import logging

from sqlalchemy import select, delete, func, update, case

from .models import CalorieFood, CalorieRecord, CalorieMealItem, CalorieConfig, \
    FoodSpot, FoodSpotPhoto

logger = logging.getLogger(__name__)

# 与 JSON 版 _DEFAULT_CONFIG 一致的默认配置
DEFAULT_CONFIG = {
    'height': 169,
    'age': 29,
    'step_frequency': 0.7,
    'weight_factor': 55,
    'target_deficit': 100000
}

_MEALS = ('breakfast', 'lunch', 'dinner')


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


# =============================================================================
# 配置（单行表）
# =============================================================================

def load_config(session):
    """读取配置 dict；无行时返回默认配置（对齐 JSON 版缺 config 时的兜底）"""
    row = session.get(CalorieConfig, 1)
    if row is None:
        return dict(DEFAULT_CONFIG)
    return row.to_dict()


def lock_config(session):
    """写事务先锁配置行；累计值及食物编号共享这一串行化入口。

    空库用幂等插入避免两个首次写请求竞争；不在普通 GET 建锁行。
    """
    dialect = session.get_bind().dialect.name
    if dialect == 'mysql':
        from sqlalchemy.dialects.mysql import insert
        create = insert(CalorieConfig).values(id=1, **DEFAULT_CONFIG)
        create = create.on_duplicate_key_update(id=1)
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
        create = insert(CalorieConfig).values(id=1, **DEFAULT_CONFIG).on_conflict_do_nothing()
    else:
        raise RuntimeError('热量写入锁仅支持 MySQL 和离线 SQLite 测试')
    # 先插入/原值更新取排他锁，避免空库 SELECT FOR UPDATE 间隙锁升级死锁。
    session.execute(create)
    stmt = select(CalorieConfig).where(CalorieConfig.id == 1).with_for_update()
    return session.execute(stmt.execution_options(populate_existing=True)).scalar_one().to_dict()


def save_config(session, config):
    """整体写入配置（UPSERT 单行）"""
    row = session.get(CalorieConfig, 1)
    if row is None:
        row = CalorieConfig(id=1)
        session.add(row)
    for key in ('height', 'age', 'step_frequency', 'weight_factor', 'target_deficit'):
        if key in config:
            setattr(row, key, float(config[key]))
    session.flush()


# =============================================================================
# 每日记录
# =============================================================================

def _record_to_dict(session, row, items=None):
    """记录行序列化；列表调用显式提供明细，避免逐条回源。"""
    if items is None:
        items = session.execute(
            select(CalorieMealItem)
            .where(CalorieMealItem.record_id == row.id)
            .order_by(CalorieMealItem.meal, CalorieMealItem.position)
        ).scalars().all()
    meals = {m: [] for m in _MEALS}
    for it in items:
        if it.meal in meals:
            # 明细结构扩展：calories 为单位热量，总摄入 = calories × quantity；
            # 存量行 quantity 列默认 1，与原绝对热量语义兼容
            qty = round(float(it.quantity or 1.0), 3)
            meals[it.meal].append({
                'name': it.name,
                'calories': it.calories,
                'quantity': qty,
                'unit': it.unit or '',
                'total_calories': round(float(it.calories or 0) * qty, 2),
            })
    return {
        'id': row.id,
        'date': row.date,
        'morning_weight': row.morning_weight,
        'evening_weight': row.evening_weight,
        'bmr': row.bmr,
        'breakfast_foods': meals['breakfast'],
        'breakfast_food': row.breakfast_food,
        'breakfast_calories': row.breakfast_calories,
        'lunch_foods': meals['lunch'],
        'lunch_food': row.lunch_food,
        'lunch_calories': row.lunch_calories,
        'dinner_foods': meals['dinner'],
        'dinner_food': row.dinner_food,
        'dinner_calories': row.dinner_calories,
        'intake_deficit': row.intake_deficit,
        'daily_steps': row.daily_steps,
        'exercise_calories': row.exercise_calories,
        'calorie_deficit': row.calorie_deficit,
        'cumulative_deficit': row.cumulative_deficit,
        'created_at': row.created_at.strftime('%Y-%m-%d %H:%M:%S') if row.created_at else '',
        'updated_at': row.updated_at.strftime('%Y-%m-%d %H:%M:%S') if row.updated_at else ''
    }


def load_records(session, order_by_date_asc=True):
    """全部记录（默认按日期升序，对齐 JSON 版 _recalc_cumulative 的排序语义）"""
    order = CalorieRecord.date.asc() if order_by_date_asc else CalorieRecord.date.desc()
    rows = session.execute(select(CalorieRecord).order_by(order)).scalars().all()
    grouped = {}
    for start in range(0, len(rows), 500):
        ids = [r.id for r in rows[start:start + 500]]
        items = session.execute(select(CalorieMealItem).where(
            CalorieMealItem.record_id.in_(ids)
        ).order_by(CalorieMealItem.record_id, CalorieMealItem.meal,
                   CalorieMealItem.position)).scalars().all()
        for item in items:
            grouped.setdefault(item.record_id, []).append(item)
    return [_record_to_dict(session, r, grouped.get(r.id, [])) for r in rows]


def load_record_metrics(session):
    """累计值、配置重算及看板专用：不读取正文或三餐明细。"""
    cols = ('id', 'date', 'morning_weight', 'bmr', 'breakfast_calories',
            'lunch_calories', 'dinner_calories', 'intake_deficit', 'daily_steps',
            'exercise_calories', 'calorie_deficit', 'cumulative_deficit')
    rows = session.execute(select(*(getattr(CalorieRecord, c) for c in cols))
                           .order_by(CalorieRecord.date)).mappings().all()
    return [dict(r) for r in rows]


def changed_metrics(before, after):
    """仅返回实际变化的数值列；累计计算仍由原计算引擎完成。"""
    old = {r['id']: r for r in before}
    updates = []
    fields = ('bmr', 'intake_deficit', 'exercise_calories',
              'calorie_deficit', 'cumulative_deficit')
    for row in after:
        diff = {k: row[k] for k in fields if k in row and old[row['id']].get(k) != row[k]}
        if diff:
            updates.append(dict(diff, id=row['id']))
    return updates


def get_record(session, record_id):
    row = session.get(CalorieRecord, record_id)
    return _record_to_dict(session, row) if row else None


def upsert_record(session, record):
    """新增或整行更新一条记录（含三餐明细重建）；保留原 created_at"""
    row = session.get(CalorieRecord, record['id'])
    created_at = row.created_at if row else _parse_ts(record.get('created_at'))
    if row is None:
        row = CalorieRecord(id=record['id'])
        session.add(row)

    row.date = record.get('date', record['id'])
    row.morning_weight = record.get('morning_weight')
    row.evening_weight = record.get('evening_weight')
    row.bmr = record.get('bmr', 0) or 0
    row.breakfast_food = record.get('breakfast_food', '') or ''
    row.breakfast_calories = record.get('breakfast_calories', 0) or 0
    row.lunch_food = record.get('lunch_food', '') or ''
    row.lunch_calories = record.get('lunch_calories', 0) or 0
    row.dinner_food = record.get('dinner_food', '') or ''
    row.dinner_calories = record.get('dinner_calories', 0) or 0
    row.intake_deficit = record.get('intake_deficit', 0) or 0
    row.daily_steps = int(record.get('daily_steps', 0) or 0)
    row.exercise_calories = record.get('exercise_calories', 0) or 0
    row.calorie_deficit = record.get('calorie_deficit', 0) or 0
    row.cumulative_deficit = record.get('cumulative_deficit', 0) or 0
    row.created_at = created_at or datetime.datetime.now()
    row.updated_at = _parse_ts(record.get('updated_at')) or datetime.datetime.now()
    session.flush()

    # 重建三餐明细（calories=单位热量，quantity 缺省 1，unit 存单位快照）
    session.execute(delete(CalorieMealItem).where(CalorieMealItem.record_id == row.id))
    for meal in _MEALS:
        for pos, item in enumerate(record.get(f'{meal}_foods') or []):
            if not isinstance(item, dict):
                continue
            try:
                qty = float(item.get('quantity', 1) or 1)
            except (ValueError, TypeError):
                qty = 1.0
            if qty <= 0:
                qty = 1.0
            session.add(CalorieMealItem(
                record_id=row.id, meal=meal, position=pos,
                name=str(item.get('name', '') or '')[:64],
                calories=float(item.get('calories', 0) or 0),
                quantity=round(qty, 3),
                unit=str(item.get('unit', '') or '')[:32]))
    session.flush()


def delete_record(session, record_id):
    """删除记录（明细由外键级联清理）；返回是否存在并删除"""
    session.execute(delete(CalorieMealItem).where(CalorieMealItem.record_id == record_id))
    result = session.execute(delete(CalorieRecord).where(CalorieRecord.id == record_id))
    return result.rowcount > 0


def update_record_fields(session, record_id, **fields):
    """批量更新记录的指定列（用于配置变更后重算 bmr/缺口/累计值）"""
    row = session.get(CalorieRecord, record_id)
    if row is None:
        return None
    for k, v in fields.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.flush()
    return row


def bulk_update_records(session, updates):
    """批量更新多条记录，合并为一条 UPDATE...CASE SQL 一次发出。

    updates: [{'id': 记录id, 列名: 新值, ...}, ...]
    用于保存/删除/配置变更后全量重算回写：避免逐条 UPDATE 在远端 MySQL
    上产生 N 次串行网络往返（N 条记录×跨地域 RTT 是热量页保存卡顿的主因）。
    """
    if not updates:
        return
    for start in range(0, len(updates), 500):
        batch = updates[start:start + 500]
        ids = [u['id'] for u in batch]
        cols = {k for u in batch for k in u if k != 'id'}
        values = {}
        for col in sorted(cols):
            if col not in CalorieRecord.__table__.columns:
                continue
            column = getattr(CalorieRecord, col)
            # 稀疏差量中未提供此列的行必须保持原值，不能由 CASE 写成 NULL。
            values[col] = case(
                *[(CalorieRecord.id == u['id'], u[col]) for u in batch if col in u],
                else_=column)
        if values:
            session.execute(update(CalorieRecord).where(CalorieRecord.id.in_(ids))
                            .values(**values).execution_options(synchronize_session=False))
    session.flush()
    session.expire_all()


# =============================================================================
# 食物热量库
# =============================================================================

def load_foods(session, for_update=False):
    """全部食物；首次初始化的二次确认使用当前读。"""
    stmt = select(CalorieFood).order_by(CalorieFood.id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    rows = session.execute(stmt).scalars().all()
    return [f.to_dict() for f in rows]


def get_food(session, food_id):
    row = session.get(CalorieFood, food_id)
    return row.to_dict() if row else None


def find_food_by_name(session, name):
    """按名称查找（不区分大小写，依赖 utf8mb4_general_ci）"""
    row = session.execute(
        select(CalorieFood).where(CalorieFood.name == (name or '').strip())
    ).scalars().first()
    return row.to_dict() if row else None


def add_missing_foods(session, names, defaults=None):
    """批量检查名称，比较规则仍由数据库决定；调用方须先 lock_config。

    defaults: 可选 {食物名: {'calories': 单位热量, 'unit': 单位快照, 'category': 分类}}；
    库中不存在而入库时采用，避免写入 0 热量食物污染后续搜索建议的热量回填，
    缺省时仍按 0/'100克'/'其他' 兜底。
    """
    from sqlalchemy import literal, union_all
    names = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    existing = set()
    for start in range(0, len(names), 100):
        checks = [select(literal(name).label('name')).where(
            select(CalorieFood.id).where(CalorieFood.name == name).exists()
        ) for name in names[start:start + 100]]
        existing.update(session.execute(union_all(*checks)).scalars())
    added = []
    next_num = None
    for name in names:
        if name in existing:
            continue
        # 同一批新名字也可能按数据库排序规则相等（不只是 Python lower）。
        if added and find_food_by_name(session, name):
            continue
        if next_num is None:
            next_num = int(next_food_id(session).split('_')[-1])
        d = (defaults or {}).get(name) or {}
        try:
            cal = float(d.get('calories', 0) or 0)
        except (ValueError, TypeError):
            cal = 0.0
        food = dict(id=f'food_{next_num:03d}', name=name,
                    unit=str(d.get('unit') or '').strip() or '100克',
                    calories=max(cal, 0.0),
                    category=str(d.get('category') or '').strip() or '其他',
                    created_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        add_food(session, food)
        next_num += 1
        added.append(food)
    return added


def find_food_by_name_excluding(session, name, exclude_id):
    """按名称查找但排除指定 id（更新时查重用）"""
    row = session.execute(
        select(CalorieFood).where(
            CalorieFood.name == (name or '').strip(),
            CalorieFood.id != exclude_id)
    ).scalars().first()
    return row.to_dict() if row else None


def next_food_id(session):
    """新食物 id：food_{现有最大序号+1:03d}（对齐 JSON 版生成规则，删除后不复用缺口前序号）"""
    max_num = 0
    ids = session.execute(select(CalorieFood.id)).scalars().all()
    for fid in ids:
        try:
            max_num = max(max_num, int(str(fid).split('_')[-1]))
        except (ValueError, IndexError):
            continue
    return f'food_{max_num + 1:03d}'


def add_food(session, food, flush=True):
    """新增食物（food 为含 id/name/unit/calories/category/created_at 的 dict）"""
    session.add(CalorieFood(
        id=food['id'],
        name=food['name'],
        unit=food.get('unit', '100克'),
        calories=float(food.get('calories', 0) or 0),
        category=food.get('category', '其他'),
        created_at=_parse_ts(food.get('created_at'))))
    if flush:
        session.flush()


def update_food_fields(session, food_id, **fields):
    """更新食物指定字段；返回更新后 dict 或 None"""
    row = session.get(CalorieFood, food_id)
    if row is None:
        return None
    for k, v in fields.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.flush()
    return row.to_dict()


def delete_food(session, food_id):
    row = session.get(CalorieFood, food_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


# =============================================================================
# 美食打卡点（calorie_food_spots / calorie_food_spot_photos）
# =============================================================================

def next_spot_id():
    """新打卡点 id：fspot_{uuid8}（与 journal/diary 模块的短 uuid 主键风格一致）"""
    import uuid
    return f'fspot_{uuid.uuid4().hex[:8]}'


def next_photo_id():
    """新照片 id：fsp_{uuid16}，同时作为磁盘文件名的主体"""
    import uuid
    return f'fsp_{uuid.uuid4().hex[:16]}'


def list_spots(session):
    """全部打卡点（按创建时间倒序），photos 内嵌返回（position 升序）"""
    rows = session.execute(
        select(FoodSpot).order_by(FoodSpot.created_at.desc(), FoodSpot.id)).scalars().all()
    grouped = {}
    for start in range(0, len(rows), 500):
        ids = [r.id for r in rows[start:start + 500]]
        photos = session.execute(
            select(FoodSpotPhoto).where(FoodSpotPhoto.spot_id.in_(ids))
            .order_by(FoodSpotPhoto.spot_id, FoodSpotPhoto.position)
        ).scalars().all()
        for p in photos:
            grouped.setdefault(p.spot_id, []).append(p)
    result = []
    for r in rows:
        d = r.base_dict()
        d['photos'] = [p.to_dict() for p in grouped.get(r.id, [])]
        result.append(d)
    return result


def get_spot(session, spot_id):
    row = session.get(FoodSpot, spot_id)
    if row is None:
        return None
    d = row.base_dict()
    photos = session.execute(
        select(FoodSpotPhoto).where(FoodSpotPhoto.spot_id == spot_id)
        .order_by(FoodSpotPhoto.position)).scalars().all()
    d['photos'] = [p.to_dict() for p in photos]
    return d


def upsert_spot(session, spot_id, fields):
    """新增或更新打卡点主表字段；返回更新后的 dict（不含照片）。spot 不存在返回 None"""
    row = session.get(FoodSpot, spot_id)
    if row is None:
        row = FoodSpot(id=spot_id)
        session.add(row)
    for key in ('shop_name', 'address', 'category', 'review'):
        if key in fields:
            setattr(row, key, fields[key])
    session.flush()
    return row.base_dict()


def add_spot_photo(session, photo):
    """登记一条照片索引（photo 含 id/spot_id/position/filename/original_name/file_size）"""
    row = FoodSpotPhoto(
        id=photo['id'], spot_id=photo['spot_id'],
        position=int(photo.get('position', 0) or 0),
        filename=photo['filename'],
        original_name=str(photo.get('original_name', '') or '')[:255],
        file_size=int(photo.get('file_size', 0) or 0))
    session.add(row)
    session.flush()
    return row.to_dict()


def get_spot_photo_filename(session, photo_id):
    """只取照片磁盘文件名（内部回显用，不进对外 payload）；不存在返回 None"""
    return session.execute(
        select(FoodSpotPhoto.filename).where(FoodSpotPhoto.id == photo_id)
    ).scalars().first()


def delete_spot_photo(session, photo_id):
    """删除单条照片索引；返回 (是否删除, 磁盘文件名)。文件清理由调用方在事务提交后执行"""
    row = session.get(FoodSpotPhoto, photo_id)
    if row is None:
        return False, ''
    filename = row.filename
    session.delete(row)
    session.flush()
    return True, filename


def delete_spot(session, spot_id):
    """删除打卡点；返回其全部照片磁盘文件名，供事务提交后清理文件。不存在返回 None"""
    row = session.get(FoodSpot, spot_id)
    if row is None:
        return None
    filenames = session.execute(
        select(FoodSpotPhoto.filename).where(FoodSpotPhoto.spot_id == spot_id)
    ).scalars().all()
    session.execute(delete(FoodSpotPhoto).where(FoodSpotPhoto.spot_id == spot_id))
    session.delete(row)
    session.flush()
    return list(filenames)
