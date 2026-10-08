"""支出预算与待付计划：准备计划不等于发生支出，所有调用均在显式写事务内。"""
import copy
from datetime import timedelta
from sqlalchemy import select, func
from . import expense_service as svc
from . import expense_repo as repo
from .models import (
    ExpensePeriod as Period, ExpenseBillTemplate as Template,
    ExpenseBillOccurrence as Bill, ExpenseRecord as Record,
    ExpenseCategory as Category, expense_utcnow,
)


def period_at(session, when):
    return session.scalar(select(Period).where(Period.start_date <= when, Period.end_date_exclusive > when))


def payments(session):
    rows = session.execute(select(Record.bill_occurrence_id, func.sum(Record.amount_minor)).where(
        Record.kind == 'expense', Record.deleted_at.is_(None), Record.bill_occurrence_id.is_not(None)
    ).group_by(Record.bill_occurrence_id))
    return {key: int(total) for key, total in rows}


def bill_dict(row, paid=0):
    data = repo.snapshot(row)
    data['details'] = svc.loads(row.snapshot_json)
    data['expected'] = svc.amount(row.expected_minor)
    data['paid'] = svc.amount(paid)
    data['remaining_minor'] = max(row.expected_minor - paid, 0) if row.status == 'open' else 0
    data['remaining'] = svc.amount(data['remaining_minor'])
    return data


def list_bills(session, end):
    paid = payments(session)
    return [bill_dict(bill, paid.get(bill.id, 0)) for bill in session.scalars(
        select(Bill).where(Bill.due_date < end).order_by(Bill.due_date, Bill.id))]


def bill_baseline(session, start, end):
    paid = payments(session)
    result = []
    for row in session.scalars(select(Bill).where(Bill.due_date < end, Bill.status != 'skipped')):
        minor = row.expected_minor if row.due_date >= start else (
            max(row.expected_minor - paid.get(row.id, 0), 0) if row.status == 'open' else 0)
        if minor:
            result.append({'id': row.id, 'name': svc.loads(row.snapshot_json)['name'],
                           'due_date': max(row.due_date, start).isoformat(), 'amount_minor': minor})
    return result


def fixed_basis(session, start, end):
    return sum(b['amount_minor'] for b in bill_baseline(session, start, end))


def generate_bills(session, end):
    """仅创建待付实例；唯一键与模块写锁保证重复准备不产生重复计划。"""
    for template in session.scalars(select(Template).where(Template.is_active.is_(True))):
        rule = svc.loads(template.repeat_rule_json)
        existing = set(session.scalars(select(Bill.due_date).where(Bill.template_id == template.id)))
        first = svc.day(rule['first_date'])
        for due in svc.bill_dates(first, rule['frequency'], end):
            if due < template.effective_from or due in existing:
                continue
            details = {'name': template.name, 'category_id': template.category_id,
                       'tag_ids': svc.loads(template.tag_ids_json), 'template_version': template.version}
            bill = Bill(id=repo.uid(), template_id=template.id, due_date=due,
                        expected_minor=template.amount_minor, snapshot_json=svc.dumps(details))
            session.add(bill)
            repo.audit(session, bill, 'plan')
    session.flush()


def period_dict(row):
    data = repo.snapshot(row)
    data['budget'] = svc.amount(row.budget_minor)
    data['rules'] = svc.loads(row.rule_snapshot_json)
    return data


def open_period(session, settings, when):
    existing = period_at(session, when)
    if existing:
        return existing
    stored = repo.preferences(settings)
    start, end, rule = svc.period_rule(when, stored['rules'])
    if session.scalar(select(Period.id).where(Period.start_date < end, Period.end_date_exclusive > start).limit(1)):
        raise svc.ExpenseError('周期边界与已有快照重叠', 409)
    generate_bills(session, end)
    budget = rule['budget_minor']
    baseline = bill_baseline(session, start, end)
    fixed = sum(b['amount_minor'] for b in baseline)
    category_budgets = check_category_budgets(session, [
        {'category_id': c['category_id'], 'budget': svc.amount(c['budget_minor'])}
        for c in rule.get('category_budgets', [])], budget)
    rules = {'warning': rule['warning'], 'critical': rule['critical'], 'pace_points': rule['pace_points'],
             'week_start': rule['week_start'], 'cycle_day': rule['cycle_day'], 'week_mode': 'auto',
             'fixed_minor': fixed, 'bill_baseline': baseline, 'category_budgets': category_budgets,
             'weeks': svc.weekly_allocation(start, end, budget, fixed, rule['week_start'])}
    row = Period(id=repo.uid(), start_date=start, end_date_exclusive=end,
                 budget_minor=budget, rule_snapshot_json=svc.dumps(rules))
    session.add(row)
    repo.audit(session, row, 'open')
    return row


def check_category_budgets(session, entries, budget):
    if not isinstance(entries, list) or len(entries) > 200:
        raise svc.ExpenseError('分类预算格式错误或数量过多')
    result, mapping = [], {}
    categories = {r.id: r for r in session.scalars(select(Category))}
    for item in entries:
        if not isinstance(item, dict):
            raise svc.ExpenseError('分类预算格式错误')
        key = svc.text(item.get('category_id'), 32, True)
        category = categories.get(key)
        if not category or key in mapping:
            raise svc.ExpenseError('分类预算存在无效或重复分类')
        minor = svc.money(item.get('budget'), zero=True)
        mapping[key] = minor
        result.append({'category_id': key, 'name': category.name, 'budget_minor': minor})
    roots, children = 0, {}
    for key, minor in mapping.items():
        parent = categories[key].parent_id
        if parent:
            if parent not in mapping:
                raise svc.ExpenseError('设置二级预算前请先设置对应一级预算')
            children[parent] = children.get(parent, 0) + minor
        else:
            roots += minor
    if budget is None and result:
        raise svc.ExpenseError('请先设置月总预算')
    if roots > (budget or 0) or any(value > mapping[key] for key, value in children.items()):
        raise svc.ExpenseError('分类预算不能超过总预算，子类合计不能超过父类预算')
    return result


def update_budget(session, row, body):
    repo.version(row, body)
    before = repo.snapshot(row)
    reason = svc.text(body.get('reason', ''), 255, True)
    rules = svc.loads(row.rule_snapshot_json)
    budget = svc.money(body.get('budget', svc.amount(row.budget_minor)), nullable=True, zero=True)
    prefs = svc.validate_preferences(body, {**svc.DEFAULTS, **rules})
    for key in ('warning', 'critical', 'pace_points'):
        rules[key] = prefs[key]
    # 总预算调整沿用该周期已确认的计划基准，不能按今天状态重写历史。
    fixed = rules['fixed_minor']
    mode = body.get('week_mode', rules['week_mode'])
    if mode not in ('auto', 'manual'):
        raise svc.ExpenseError('周预算模式不正确')
    generated = svc.weekly_allocation(row.start_date, row.end_date_exclusive, budget, fixed, rules['week_start'])
    if mode == 'manual':
        if budget is None:
            raise svc.ExpenseError('手动周预算需要先设置月预算')
        values = body.get('weeks', [{'budget': svc.amount(w['budget_minor'])} for w in rules['weeks']])
        if not isinstance(values, list) or len(values) != len(generated):
            raise svc.ExpenseError('周预算数量须与本周期周段一致')
        for target, value in zip(generated, values):
            if not isinstance(value, dict):
                raise svc.ExpenseError('周预算格式错误')
            target['budget_minor'] = svc.money(value.get('budget'), zero=True)
        if sum(w['budget_minor'] for w in generated) > max(budget - fixed, 0):
            raise svc.ExpenseError('周预算合计超过扣除计划后的预算池，请重新分配')
    rules['weeks'], rules['week_mode'] = generated, mode
    entries = body.get('category_budgets', [{'category_id': c['category_id'], 'budget': svc.amount(c['budget_minor'])}
                                           for c in rules['category_budgets']])
    rules['category_budgets'] = check_category_budgets(session, entries, budget)
    row.budget_minor = budget
    row.rule_snapshot_json = svc.dumps(rules)
    repo.bump(row)
    repo.audit(session, row, 'budget', before, reason)
    return period_dict(row)


def reallocate(session, row, confirmed=False):
    fixed = fixed_basis(session, row.start_date, row.end_date_exclusive)
    rules = svc.loads(row.rule_snapshot_json)
    if fixed == rules['fixed_minor']:
        return
    if not confirmed:
        raise svc.ExpenseError('账单计划变化将影响本周期周预算，请确认重新计算', 409,
                               {'reallocate': True, 'fixed': svc.amount(fixed)})
    before = repo.snapshot(row)
    if rules['week_mode'] == 'manual':
        pool = max((row.budget_minor or 0) - fixed, 0)
        if sum(w['budget_minor'] or 0 for w in rules['weeks']) > pool:
            raise svc.ExpenseError('现有手动周预算超过新预算池，请先调低周预算或切换自动分配', 409)
    else:
        rules['weeks'] = svc.weekly_allocation(row.start_date, row.end_date_exclusive,
                                              row.budget_minor, fixed, rules['week_start'])
    rules['fixed_minor'] = fixed
    rules['bill_baseline'] = bill_baseline(session, row.start_date, row.end_date_exclusive)
    row.rule_snapshot_json = svc.dumps(rules)
    repo.bump(row)
    repo.audit(session, row, 'bill_reallocate', before, '用户确认账单计划引起的预算变化')


def template_dict(row):
    data = repo.snapshot(row)
    data['amount'] = svc.amount(row.amount_minor)
    data['tag_ids'] = svc.loads(row.tag_ids_json)
    data['rule'] = svc.loads(row.repeat_rule_json)
    return data


def save_template(session, settings, body, key=None, remove=False):
    current = open_period(session, settings, svc.today(settings.timezone))
    row = repo.require(session, Template, key) if key else Template(id=repo.uid())
    before = repo.snapshot(row) if key else {}
    if key:
        repo.version(row, body)
        generate_bills(session, current.end_date_exclusive)
    if remove:
        row.is_active = False
        repo.bump(row)
        repo.audit(session, row, 'disable', before, '停止生成新计划，已生成待付计划不变')
        return template_dict(row)
    first = svc.day(body.get('first_date', ''))
    if key and first < current.end_date_exclusive:
        raise svc.ExpenseError('模板修改从下一周期生效，首次到期日须不早于当前周期结束；本次金额请编辑待付实例')
    category = repo.choose_category(session, body.get('category_id'), row.category_id if key else None)
    tags = repo.choose_tags(session, body.get('tag_ids', []), svc.loads(row.tag_ids_json) if key else [])
    frequency = body.get('frequency', 'monthly')
    if frequency not in ('once', 'monthly', 'yearly'):
        raise svc.ExpenseError('账单重复规则不正确')
    row.name = svc.text(body.get('name', ''), 128, True)
    row.amount_minor = svc.money(body.get('amount'))
    row.category_id, row.tag_ids_json = category.id, svc.dumps(tags)
    row.repeat_rule_json = svc.dumps({'frequency': frequency, 'first_date': first.isoformat()})
    row.effective_from = first
    row.is_active = True
    session.add(row)
    if key:
        repo.bump(row)
    repo.audit(session, row, 'update' if key else 'create', before)
    prepared = session.scalars(select(Period).where(Period.start_date >= current.start_date).order_by(Period.start_date)).all()
    generate_bills(session, max(p.end_date_exclusive for p in prepared))
    for period in prepared:
        reallocate(session, period, body.get('confirm_reallocate') is True)
    return template_dict(row)


def refresh_bill_status(session, key, reopen=False, settle=False):
    row = repo.require(session, Bill, key)
    paid = int(session.scalar(select(func.coalesce(func.sum(Record.amount_minor), 0)).where(
        Record.bill_occurrence_id == key, Record.kind == 'expense', Record.deleted_at.is_(None))) or 0)
    before = repo.snapshot(row)
    if reopen:
        row.status, row.closed_at = 'open', None
    if settle or (paid >= row.expected_minor and row.status == 'open'):
        row.status, row.closed_at = 'settled', expense_utcnow()
    if before['status'] != row.status or before['closed_at'] != (row.closed_at.isoformat(timespec='seconds') + 'Z' if row.closed_at else None):
        repo.bump(row)
        repo.audit(session, row, 'payment_status', before)
    return row


def bill_action(session, settings, key, action, body):
    row = repo.require(session, Bill, key)
    if action == 'pay':
        # 重试须先经过记录幂等判断，不能被“上次已付清”挡住。
        details = svc.loads(row.snapshot_json)
        payload = {**body, 'kind': 'expense', 'bill_occurrence_id': key,
                   'category_id': body.get('category_id', details['category_id']),
                   'tag_ids': body.get('tag_ids', details.get('tag_ids', [])),
                   'note': body.get('note', details['name'])}
        return repo.save_record(session, settings, payload)
    repo.version(row, body)
    before = repo.snapshot(row)
    if action == 'link-record':
        record = repo.require(session, Record, body.get('record_id'))
        if record.kind != 'expense' or record.deleted_at:
            raise svc.ExpenseError('只能关联有效支出')
        return repo.save_record(session, settings, {'version': body.get('record_version'),
                                                     'bill_occurrence_id': key}, record.id)
    if action in ('close', 'skip', 'reopen'):
        row.status = {'close': 'settled', 'skip': 'skipped', 'reopen': 'open'}[action]
        row.closed_at = None if action == 'reopen' else expense_utcnow()
    elif action == 'edit':
        if row.status != 'open':
            raise svc.ExpenseError('仅待付实例可修改，请先重新开放')
        row.expected_minor = svc.money(body.get('amount'))
        details = svc.loads(row.snapshot_json)
        details['name'] = svc.text(body.get('name', details['name']), 128, True)
        row.snapshot_json = svc.dumps(details)
    else:
        raise svc.ExpenseError('账单操作不存在', 404)
    repo.bump(row)
    repo.audit(session, row, action, before, body.get('reason', '手动调整账单计划'))
    current = open_period(session, settings, svc.today(settings.timezone))
    prepared = session.scalars(select(Period).where(Period.start_date >= current.start_date).order_by(Period.start_date)).all()
    for period in prepared:
        reallocate(session, period, body.get('confirm_reallocate') is True)
    return bill_dict(row, payments(session).get(row.id, 0))
