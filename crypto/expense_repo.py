"""支出数据访问与写事务。写入口先锁本模块设置行，GET 不初始化、不写库。"""
import copy
import hashlib
import re
import unicodedata
import uuid
from datetime import date, datetime

from sqlalchemy import select, update, delete, func
from .models import (
    ExpenseSettings as Settings, ExpenseCategory as Category, ExpenseTag as Tag,
    ExpenseRecord as Record, ExpenseRecordTag as RecordTag, ExpenseDailyCheck as DailyCheck,
    ExpenseChangeLog as ChangeLog, ExpensePeriod as Period,
    ExpenseBillTemplate as BillTemplate, ExpenseBillOccurrence as Bill, expense_utcnow,
)
from . import expense_service as svc


def uid():
    return uuid.uuid4().hex


def snapshot(row):
    if row is None:
        return {}
    result = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        if isinstance(value, datetime):
            value = value.isoformat(timespec='seconds') + 'Z'
        elif isinstance(value, date):
            value = value.isoformat()
        result[col.name] = value
    return result


def audit(session, row, action, before=None, reason=''):
    session.flush()
    before, after = dict(before or {}), snapshot(row)
    if isinstance(row, Record):
        after['tag_ids'] = record_tags(session, row.id)
        if before:
            before.setdefault('tag_ids', after['tag_ids'])
    session.add(ChangeLog(id=uid(), entity_type=row.__tablename__,
                          entity_id=str(getattr(row, 'id', None) or row.business_date),
                          action=action, before_json=svc.dumps(before),
                          after_json=svc.dumps(after), reason=svc.text(reason, 255)))


def bump(row):
    row.version += 1
    row.updated_at = expense_utcnow()


def version(row, body):
    expected = svc.integer(body.get('version', ''), 1, 2_147_483_647)
    if row.version != expected:
        raise svc.ExpenseError('内容已被其他页签修改，请刷新后重试', 409,
                               {'current_version': row.version})


def require(session, model, key):
    key = svc.text(key, 32, True)
    row = session.get(model, key)
    if row is None:
        raise svc.ExpenseError('记录不存在', 404)
    return row


def get_settings(session):
    return session.get(Settings, 1)


def lock_settings(session, setup=False):
    """UPDATE 首先取得锁，避免 SQLite 读升级与 MySQL 空库间隙锁竞争。"""
    if setup:
        dialect = session.get_bind().dialect.name
        values = dict(id=1, currency='CNY', timezone='Asia/Shanghai',
                      tracking_start_date=svc.today(), settings_json='{}')
        if dialect == 'mysql':
            from sqlalchemy.dialects.mysql import insert
            stmt = insert(Settings).values(**values).on_duplicate_key_update(id=1)
        elif dialect == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert
            stmt = insert(Settings).values(**values).on_conflict_do_nothing()
        else:
            raise svc.ExpenseError('支出模块仅支持 MySQL 和隔离 SQLite', 503)
        session.execute(stmt)
    result = session.execute(update(Settings).where(Settings.id == 1).values(version=Settings.version))
    if not result.rowcount:
        raise svc.ExpenseError('请先完成支出管理初始化', 409)
    row = session.execute(select(Settings).where(Settings.id == 1)
                          .execution_options(populate_existing=True)).scalar_one()
    if not setup and row.initialized_at is None:
        raise svc.ExpenseError('请先完成初始化', 409)
    return row


def preferences(row):
    return svc.loads(row.settings_json) if row and row.initialized_at else {'rules': [], 'preferences': copy.deepcopy(svc.DEFAULTS)}


def settings_dict(row):
    config = preferences(row)
    return {'initialized': bool(row and row.initialized_at), 'version': row.version if row else 0,
            'currency': 'CNY', 'timezone': row.timezone if row else 'Asia/Shanghai',
            'today': svc.today(row.timezone if row else 'Asia/Shanghai').isoformat(),
            'tracking_start_date': row.tracking_start_date.isoformat() if row else None,
            **config}


def setup(session, row, body):
    if row.initialized_at:
        return settings_dict(row)
    if body.get('currency', 'CNY') != 'CNY':
        raise svc.ExpenseError('首版只支持 CNY 本位币')
    row.timezone = svc.tz(body.get('timezone', 'Asia/Shanghai')).key
    now = svc.today(row.timezone)
    row.tracking_start_date = svc.day(body.get('tracking_start_date', now.isoformat()))
    if row.tracking_start_date > now:
        raise svc.ExpenseError('记账起始日不能在未来')
    config = svc.validate_preferences(body)
    start, _ = svc.ordinary_bounds(now, config['cycle_day'])
    row.settings_json = svc.dumps({'preferences': config, 'rules': [dict(config, effective_from=start.isoformat())]})
    row.initialized_at = expense_utcnow()
    session.add(Category(id=svc.UNCATEGORIZED, name='未分类', normalized_name='未分类', is_system=True, sort_order=-1))
    for index, (name, children) in enumerate(svc.SEED_CATEGORIES.items()):
        parent = Category(id=uid(), name=name, normalized_name=name, color=svc.COLORS[index % len(svc.COLORS)], sort_order=index)
        session.add(parent)
        session.flush()
        for j, label in enumerate(children.split() + ['其他']):
            session.add(Category(id=uid(), parent_id=parent.id, parent_key=parent.id,
                                 name=label, normalized_name=label.casefold(), color=parent.color, sort_order=j))
    for i, label in enumerate(svc.SEED_TAGS):
        session.add(Tag(id=uid(), name=label, normalized_name=label, sort_order=i,
                        is_favorite=i < 4, color=svc.COLORS[i % len(svc.COLORS)]))
    session.flush()
    audit(session, row, 'setup')
    return settings_dict(row)


def update_settings(session, row, body):
    from .expense_budget_repo import open_period, check_category_budgets
    version(row, body)
    before = snapshot(row)
    stored = preferences(row)
    current = stored['preferences']
    config = svc.validate_preferences(body, current)
    zone = svc.tz(body.get('timezone', row.timezone)).key
    now = svc.today(zone)
    if 'tracking_start_date' in body:
        start_tracking = svc.day(body['tracking_start_date'])
        if start_tracking > now:
            raise svc.ExpenseError('记账起始日不能在未来')
        row.tracking_start_date = start_tracking
    if 'quick_entries' in body:
        entries = body['quick_entries']
        if not isinstance(entries, list) or len(entries) > 30:
            raise svc.ExpenseError('常用模板最多 30 项')
        clean = []
        for item in entries:
            if not isinstance(item, dict):
                raise svc.ExpenseError('模板格式错误')
            category = choose_category(session, item.get('category_id'))
            tags = choose_tags(session, item.get('tag_ids', []))
            clean.append({'name': svc.text(item.get('name', ''), 32, True),
                          'amount': svc.amount(svc.money(item.get('amount'), nullable=True)),
                          'category_id': category.id, 'tag_ids': tags,
                          'note': svc.text(item.get('note', ''), 1000)})
        config['quick_entries'] = clean
    entries = body.get('category_budgets', [
        {'category_id': c['category_id'], 'budget': svc.amount(c['budget_minor'])}
        for c in config.get('category_budgets', [])])
    config['category_budgets'] = check_category_budgets(session, entries, config['budget_minor'])
    rule_fields = ('cycle_day', 'week_start', 'budget_minor', 'warning', 'critical', 'pace_points', 'category_budgets')
    if any(config.get(k) != current.get(k) for k in rule_fields):
        period = open_period(session, row, now)
        effective = period.end_date_exclusive
        if session.scalar(select(Period.id).where(Period.start_date >= effective).limit(1)):
            raise svc.ExpenseError('未来周期已有快照；请直接调整该周期预算，不能覆盖已准备的边界', 409)
        rule = dict(config, effective_from=effective.isoformat())
        natural_start, _ = svc.ordinary_bounds(effective, config['cycle_day'])
        if natural_start != effective:
            pending = next((r for r in stored['rules'] if r['effective_from'] == effective.isoformat()), {})
            if 'transition_budget' in body:
                transition = svc.money(body['transition_budget'], nullable=True, zero=True)
            elif config['cycle_day'] == current['cycle_day'] and 'transition_budget_minor' in pending:
                transition = pending['transition_budget_minor']
            else:
                raise svc.ExpenseError('更改周期需要明确过渡周期预算（可以为空）')
            rule['transition_budget_minor'] = transition
            check_category_budgets(session, entries, transition)
        stored['rules'] = [r for r in stored['rules'] if r['effective_from'] < effective.isoformat()] + [rule]
    stored['preferences'] = config
    row.timezone = zone
    row.settings_json = svc.dumps(stored)
    bump(row)
    audit(session, row, 'settings', before, body.get('reason', '更新设置'))
    return settings_dict(row)


def dictionary(session):
    return {'categories': [snapshot(r) for r in session.scalars(select(Category).order_by(Category.sort_order, Category.name))],
            'tags': [snapshot(r) for r in session.scalars(select(Tag).order_by(Tag.is_favorite.desc(), Tag.sort_order, Tag.name))]}


def normalized_name(value):
    return unicodedata.normalize('NFKC', value).casefold()


def choose_category(session, key, previous=None):
    row = require(session, Category, key or svc.UNCATEGORIZED)
    if not row.is_active and row.id != previous:
        raise svc.ExpenseError('分类已停用')
    if row.parent_id:
        parent = require(session, Category, row.parent_id)
        if not parent.is_active and row.id != previous:
            raise svc.ExpenseError('上级分类已停用')
    children = session.scalars(select(Category).where(Category.parent_id == row.id)).all()
    if children:
        fallback = next((r for r in children if r.name == '其他' and r.is_active), None)
        if fallback is None:
            raise svc.ExpenseError('请选择一个二级分类')
        row = fallback
    return row


def choose_tags(session, keys, previous=()):
    if not isinstance(keys, list) or len(keys) > 10 or not all(isinstance(k, str) for k in keys):
        raise svc.ExpenseError('每笔最多选择 10 个有效标签')
    keys = list(dict.fromkeys(keys))
    for key in keys:
        tag = require(session, Tag, key)
        if not tag.is_active and key not in previous:
            raise svc.ExpenseError('标签已停用')
    return keys


def edit_dictionary(session, model, body, key=None, remove=False):
    row = require(session, model, key) if key else model(id=uid())
    before = snapshot(row) if key else {}
    if key:
        version(row, body)
    if model is Category and key == svc.UNCATEGORIZED:
        raise svc.ExpenseError('未分类为系统兜底项，不能修改')
    if remove:
        # 被配置引用也保留，避免未来周期的分类预算或常用模板悬空。
        referenced = session.scalar(select(Settings.id).where(Settings.settings_json.contains(key)).limit(1))
        referenced = referenced or session.scalar(select(Period.id).where(Period.rule_snapshot_json.contains(key)).limit(1))
        referenced = referenced or session.scalar(select(Bill.id).where(Bill.snapshot_json.contains(key)).limit(1))
        if model is Category:
            referenced = referenced or session.scalar(select(Record.id).where(Record.category_id == key).limit(1))
            referenced = referenced or session.scalar(select(Category.id).where(Category.parent_id == key).limit(1))
            referenced = referenced or session.scalar(select(BillTemplate.id).where(BillTemplate.category_id == key).limit(1))
        else:
            referenced = referenced or session.scalar(select(RecordTag.record_id).where(RecordTag.tag_id == key).limit(1))
            referenced = referenced or session.scalar(select(BillTemplate.id).where(BillTemplate.tag_ids_json.contains(key)).limit(1))
        if referenced:
            row.is_active = False
            bump(row)
            audit(session, row, 'archive', before)
            return {'id': key, 'archived': True, 'message': '已停用，保留历史引用'}
        audit(session, row, 'delete', before)
        session.delete(row)
        return {'id': key, 'deleted': True}
    name = svc.text(body.get('name', row.name or ''), 32, True)
    normalized = svc.text(normalized_name(name), 64, True)
    clauses = [model.normalized_name == normalized, model.id != row.id]
    if model is Category:
        parent_id = body.get('parent_id', row.parent_id) or None
        if key and parent_id != row.parent_id:
            raise svc.ExpenseError('分类层级创建后固定；请新增分类后显式调整记录')
        if parent_id:
            parent = require(session, Category, parent_id)
            if parent.parent_id or parent.is_system or not parent.is_active:
                raise svc.ExpenseError('仅可在有效一级分类下创建子类')
            if (session.scalar(select(Record.id).where(Record.category_id == parent_id).limit(1))
                    or session.scalar(select(BillTemplate.id).where(BillTemplate.category_id == parent_id).limit(1))):
                raise svc.ExpenseError('该分类已有直接记录或账单引用，不能改为父类')
        row.parent_id, row.parent_key = parent_id, parent_id or ''
        clauses.append(Category.parent_key == row.parent_key)
    if session.scalar(select(model.id).where(*clauses).limit(1)):
        raise svc.ExpenseError('同名分类或标签已存在（包括已停用项）', 409)
    row.name, row.normalized_name = name, normalized
    color = body.get('color', row.color or '#4f7b70')
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise svc.ExpenseError('颜色须为六位十六进制色值')
    row.color = color
    row.sort_order = svc.integer(body.get('sort_order', row.sort_order or 0), 0, 10000)
    for field in ('is_active', 'is_favorite'):
        if field in body and hasattr(row, field):
            if not isinstance(body[field], bool):
                raise svc.ExpenseError('状态须为布尔值')
            setattr(row, field, body[field])
    session.add(row)
    if key:
        bump(row)
    audit(session, row, 'update' if key else 'create', before)
    return snapshot(row)


def record_tags(session, key):
    return list(session.scalars(select(RecordTag.tag_id).where(RecordTag.record_id == key)))


def record_dict(session, row, tags=None):
    data = snapshot(row)
    data.pop('request_payload_hash', None)
    data['amount'] = svc.amount(row.amount_minor)
    data['tag_ids'] = record_tags(session, row.id) if tags is None else tags
    return data


def invalidate_checks(session, dates):
    session.execute(update(DailyCheck).where(DailyCheck.business_date.in_(set(dates))).values(
        status='pending', confirmed_at=None, version=DailyCheck.version + 1, updated_at=expense_utcnow()))


def refund_sum(session, original_id, excluded=None):
    return int(session.scalar(select(func.coalesce(func.sum(Record.amount_minor), 0)).where(
        Record.original_record_id == original_id, Record.deleted_at.is_(None), Record.id != (excluded or ''))) or 0)


def save_record(session, settings, body, key=None):
    from .expense_budget_repo import open_period, refresh_bill_status
    now = svc.today(settings.timezone)
    row = require(session, Record, key) if key else None
    if row and row.deleted_at:
        raise svc.ExpenseError('请先恢复已作废记录', 409)
    before = snapshot(row)
    old_tags = record_tags(session, key) if key else []
    if key:
        before['tag_ids'] = old_tags
        version(row, body)
        data = {**record_dict(session, row, old_tags), **body}
    else:
        data = body
        request_id = svc.text(data.get('client_request_id', ''), 64, True)
        canonical = {k: v for k, v in body.items() if k not in ('confirm_duplicate', 'version')}
        fingerprint = hashlib.sha256(svc.dumps(canonical).encode()).hexdigest()
        existing = session.scalar(select(Record).where(Record.client_request_id == request_id))
        if existing:
            if existing.request_payload_hash != fingerprint:
                raise svc.ExpenseError('同一保存标识不能提交不同内容', 409)
            return {'record': record_dict(session, existing), 'replayed': True, 'affected_dates': []}
    kind = data.get('kind', 'expense')
    if kind not in ('expense', 'refund', 'movement'):
        raise svc.ExpenseError('记录类型错误')
    when = svc.day(data.get('business_date', now.isoformat()))
    if when > now:
        raise svc.ExpenseError('未来支出请使用账单计划')
    minor = svc.money(data.get('amount'))
    original_id = svc.text(data.get('original_record_id') or '', 32) or None
    bill_id = svc.text(data.get('bill_occurrence_id') or '', 32) or None
    category_id = svc.text(data.get('category_id') or svc.UNCATEGORIZED, 32, True)
    reason = svc.text(data.get('unlinked_refund_reason', ''), 255)
    if kind == 'refund':
        if original_id:
            original = session.scalar(select(Record).where(Record.id == original_id).with_for_update())
            if original is None:
                raise svc.ExpenseError('原支出不存在', 404)
            if original.kind != 'expense' or original.deleted_at or original.id == key:
                raise svc.ExpenseError('回款必须关联未作废的原支出')
            if when < original.business_date or refund_sum(session, original_id, key) + minor > original.amount_minor:
                raise svc.ExpenseError('回款日期早于支出或累计回款超过原支出')
            category_id, bill_id = original.category_id, original.bill_occurrence_id
        elif not reason or category_id == svc.UNCATEGORIZED:
            raise svc.ExpenseError('未关联原支出时必须填写历史回款说明及具体分类')
    elif original_id:
        raise svc.ExpenseError('只有回款可以关联原支出')
    if kind == 'movement' and bill_id:
        raise svc.ExpenseError('资金划转不能作为账单支付')
    if row and refund_sum(session, row.id):
        if kind != 'expense' or minor < refund_sum(session, row.id):
            raise svc.ExpenseError('修改后金额不能小于关联回款，且不能更改记录类型')
        earliest = session.scalar(select(func.min(Record.business_date)).where(
            Record.original_record_id == row.id, Record.deleted_at.is_(None)))
        if earliest and when > earliest:
            raise svc.ExpenseError('支出日期不能晚于已关联的回款')
    inherited = category_id if kind == 'refund' and original_id else (row.category_id if row else None)
    category = choose_category(session, category_id, inherited)
    tags = choose_tags(session, data.get('tag_ids', []), old_tags)
    if bill_id:
        bill = require(session, Bill, bill_id)
        if kind == 'expense' and bill.status != 'open' and (not row or row.bill_occurrence_id != bill_id):
            raise svc.ExpenseError('账单已关闭；如需继续支付，请先重新开放', 409)
    if not row and not data.get('confirm_duplicate'):
        duplicate = session.scalar(select(Record.id).where(Record.deleted_at.is_(None),
            Record.business_date == when, Record.amount_minor == minor, Record.category_id == category.id,
            Record.kind == kind).limit(1))
        if duplicate:
            raise svc.ExpenseError('同日已有同分类同金额记录，请确认不是重复记账', 409, {'duplicate_id': duplicate})
    affected = {when}
    previous_bill = row.bill_occurrence_id if row else None
    if row:
        affected.add(row.business_date)
        bump(row)
    else:
        row = Record(id=uid(), client_request_id=request_id, request_payload_hash=fingerprint)
        session.add(row)
    row.kind, row.amount_minor, row.business_date = kind, minor, when
    row.category_id, row.original_record_id, row.bill_occurrence_id = category.id, original_id, bill_id
    row.unlinked_refund_reason = reason
    for field, limit in (('title', 120), ('merchant', 128), ('note', 1000), ('payment_method', 64)):
        setattr(row, field, svc.text(data.get(field, ''), limit))
    session.flush()
    session.execute(delete(RecordTag).where(RecordTag.record_id == row.id))
    session.add_all(RecordTag(record_id=row.id, tag_id=tag) for tag in tags)
    if key and (before['category_id'] != row.category_id or before['bill_occurrence_id'] != bill_id):
        for refund in session.scalars(select(Record).where(Record.original_record_id == row.id)):
            old = snapshot(refund)
            refund.category_id, refund.bill_occurrence_id = row.category_id, bill_id
            bump(refund)
            affected.add(refund.business_date)
            audit(session, refund, 'inherit', old)
    session.flush()
    if previous_bill and before.get('kind') == 'expense' and (previous_bill != bill_id or kind != 'expense'):
        refresh_bill_status(session, previous_bill, reopen=True)
    if bill_id and kind == 'expense':
        reduced = bool(key and before['kind'] == 'expense' and previous_bill == bill_id and minor < before['amount_minor'])
        refresh_bill_status(session, bill_id, reopen=reduced, settle=body.get('settle_bill') is True)
    affected_periods = {}
    for affected_day in sorted(affected):
        period = open_period(session, settings, affected_day)
        affected_periods[period.id] = {'id': period.id, 'start': period.start_date.isoformat(),
                                      'end_exclusive': period.end_date_exclusive.isoformat()}
    invalidate_checks(session, affected)
    audit(session, row, 'update' if key else 'create', before)
    return {'record': record_dict(session, row, tags), 'replayed': False,
            'affected_dates': [d.isoformat() for d in sorted(affected)],
            'affected_periods': list(affected_periods.values())}


def delete_or_restore(session, row, body, restore=False):
    from .expense_budget_repo import refresh_bill_status
    version(row, body)
    if restore == (row.deleted_at is None):
        return {'record': record_dict(session, row)}
    if not restore and refund_sum(session, row.id):
        raise svc.ExpenseError('请先处理关联回款，再作废原支出', 409)
    if restore and row.original_record_id:
        original = require(session, Record, row.original_record_id)
        if original.deleted_at or original.kind != 'expense' or row.business_date < original.business_date:
            raise svc.ExpenseError('原支出状态或日期已改变，不能恢复该回款', 409)
        if refund_sum(session, original.id) + row.amount_minor > original.amount_minor:
            raise svc.ExpenseError('恢复后累计回款超过原支出', 409)
        row.category_id, row.bill_occurrence_id = original.category_id, original.bill_occurrence_id
    before = snapshot(row)
    row.deleted_at = None if restore else expense_utcnow()
    bump(row)
    session.flush()
    if row.bill_occurrence_id and row.kind == 'expense':
        refresh_bill_status(session, row.bill_occurrence_id, reopen=True)
    invalidate_checks(session, [row.business_date])
    audit(session, row, 'restore' if restore else 'void', before)
    return {'record': record_dict(session, row), 'affected_dates': [row.business_date.isoformat()]}


def daily_check(session, settings, body):
    when = svc.day(body.get('business_date', ''))
    if when > svc.today(settings.timezone):
        raise svc.ExpenseError('不能核对未来日期')
    confirmed = body.get('confirmed', True)
    if not isinstance(confirmed, bool):
        raise svc.ExpenseError('核对状态格式错误')
    row = session.get(DailyCheck, when)
    before = snapshot(row)
    if row is None:
        row = DailyCheck(business_date=when)
        session.add(row)
    else:
        bump(row)
    row.status = 'confirmed' if confirmed else 'pending'
    row.confirmed_at = expense_utcnow() if confirmed else None
    audit(session, row, 'check', before)
    return snapshot(row)
