"""支出只读投影：数据库分页与聚合，不拉取全部历史明细，不隐式写库。"""
from datetime import timedelta
from sqlalchemy import select, func, case, and_, or_
from . import expense_service as svc
from . import expense_repo as repo
from . import expense_budget_repo as budgets
from .models import (ExpenseRecord as Record, ExpenseRecordTag as RecordTag,
                     ExpenseCategory as Category, ExpenseTag as Tag, ExpenseDailyCheck as DailyCheck,
                     ExpenseChangeLog as ChangeLog)


def range_for(session, settings, args):
    now = svc.today(settings.timezone if settings else 'Asia/Shanghai')
    if args.get('start') or args.get('end'):
        start, end = svc.day(args.get('start', '')), svc.day(args.get('end', '')) + timedelta(days=1)
        if end <= start or (end - start).days > 3660:
            raise svc.ExpenseError('查询范围应为 1～3660 天')
        period = budgets.period_at(session, start)
        if not period or period.start_date != start or period.end_date_exclusive != end:
            period = None
    else:
        chosen = svc.day(args.get('date', now.isoformat()))
        period = budgets.period_at(session, chosen)
        if period:
            start, end = period.start_date, period.end_date_exclusive
        else:
            start, end, _ = svc.period_rule(chosen, repo.preferences(settings)['rules'])
    return start, end, period, now


def filters(session, start, end, args):
    clauses = [Record.business_date >= start, Record.business_date < end]
    trash = args.get('trash') == '1'
    clauses.append(Record.deleted_at.is_not(None) if trash else Record.deleted_at.is_(None))
    kind = args.get('kind')
    if kind:
        if kind not in ('expense', 'refund', 'movement'):
            raise svc.ExpenseError('记录类型无效')
        clauses.append(Record.kind == kind)
    category_id = args.get('category_id')
    if category_id:
        repo.require(session, Category, category_id)
        clauses.append(or_(Record.category_id == category_id,
                           Record.category_id.in_(select(Category.id).where(Category.parent_id == category_id))))
    keyword = args.get('keyword', '').strip()
    if keyword:
        svc.text(keyword, 128)
        clauses.append(or_(Record.title.contains(keyword, autoescape=True),
                           Record.note.contains(keyword, autoescape=True),
                           Record.merchant.contains(keyword, autoescape=True),
                           Record.payment_method.contains(keyword, autoescape=True)))
    if args.get('min_amount'):
        clauses.append(Record.amount_minor >= svc.money(args['min_amount'], zero=True))
    if args.get('max_amount'):
        clauses.append(Record.amount_minor <= svc.money(args['max_amount'], zero=True))
    fixed = args.get('fixed')
    if fixed in ('0', '1'):
        clauses.append(Record.bill_occurrence_id.is_not(None) if fixed == '1' else Record.bill_occurrence_id.is_(None))
    tags = list(dict.fromkeys(t for t in args.get('tags', '').split(',') if t))
    if len(tags) > 10:
        raise svc.ExpenseError('最多筛选 10 个标签')
    if tags:
        exists = lambda ids: select(RecordTag.record_id).where(
            RecordTag.record_id == Record.id, RecordTag.tag_id.in_(ids)).correlate(Record).exists()
        if args.get('tag_mode', 'all') == 'any':
            clauses.append(exists(tags))
        else:
            clauses.extend(exists([tag]) for tag in tags)
    return clauses


def sums(clauses):
    return select(
        func.coalesce(func.sum(case((Record.kind == 'expense', Record.amount_minor), else_=0)), 0),
        func.coalesce(func.sum(case((Record.kind == 'refund', Record.amount_minor), else_=0)), 0),
        func.coalesce(func.sum(case((Record.kind == 'movement', Record.amount_minor), else_=0)), 0),
        func.count(Record.id)).where(*clauses)


def totals(session, clauses):
    gross, returned, movement, count = session.execute(sums(clauses)).one()
    g, r, m = int(gross), int(returned), int(movement)
    return {'gross': svc.amount(g), 'refunds': svc.amount(r), 'net': svc.amount(g - r),
            'movement': svc.amount(m), 'count': count, 'gross_minor': g, 'refunds_minor': r, 'net_minor': g - r}


def records(session, settings, args):
    start, end, _, _ = range_for(session, settings, args)
    clauses = filters(session, start, end, args)
    page = svc.integer(args.get('page', '1'), 1, 1_000_000)
    limit = svc.integer(args.get('page_size', '50'), 1, 100)
    rows = session.scalars(select(Record).where(*clauses).order_by(
        Record.business_date.desc(), Record.created_at.desc(), Record.id.desc()).offset((page - 1) * limit).limit(limit)).all()
    tags = {}
    if rows:
        for key, tag in session.execute(select(RecordTag.record_id, RecordTag.tag_id).where(
                RecordTag.record_id.in_([r.id for r in rows]))):
            tags.setdefault(key, []).append(tag)
    summary = totals(session, clauses)
    page_g = sum(r.amount_minor for r in rows if r.kind == 'expense')
    page_r = sum(r.amount_minor for r in rows if r.kind == 'refund')
    return {'records': [repo.record_dict(session, row, tags.get(row.id, [])) for row in rows],
            'page': page, 'page_size': limit, 'total': summary['count'], 'summary': summary,
            'page_net': svc.amount(page_g - page_r)}


def detail(session, key):
    row = repo.require(session, Record, key)
    data = repo.record_dict(session, row)
    data['refund_total'] = svc.amount(repo.refund_sum(session, key))
    data['refund_records'] = [repo.record_dict(session, refund) for refund in session.scalars(
        select(Record).where(Record.original_record_id == key).order_by(Record.business_date))]
    data['history'] = [repo.snapshot(log) for log in session.scalars(select(ChangeLog).where(
        ChangeLog.entity_type == 'expense_records', ChangeLog.entity_id == key)
        .order_by(ChangeLog.created_at.desc(), ChangeLog.id.desc()).limit(100))]
    return data


def analytics(session, settings, args):
    start, end, period, now = range_for(session, settings, args)
    clauses = filters(session, start, end, args)
    summary = totals(session, clauses)
    daily_rows = session.execute(select(
        Record.business_date,
        func.sum(case((Record.kind == 'expense', Record.amount_minor), else_=0)),
        func.sum(case((Record.kind == 'refund', Record.amount_minor), else_=0)),
        func.sum(case((and_(Record.kind == 'expense', Record.bill_occurrence_id.is_(None)), Record.amount_minor), else_=0)),
        func.sum(case((and_(Record.kind == 'refund', Record.bill_occurrence_id.is_(None)), Record.amount_minor), else_=0)),
        func.count(Record.id),
    ).where(*clauses).group_by(Record.business_date)).all()
    grouped = {row[0]: row[1:] for row in daily_rows}
    checked = set(session.scalars(select(DailyCheck.business_date).where(
        DailyCheck.business_date >= start, DailyCheck.business_date < end, DailyCheck.status == 'confirmed')))
    daily, cumulative, history = [], 0, []
    for current in svc.date_range(start, end):
        gross, returned, variable_g, variable_r, count = map(int, grouped.get(current, (0, 0, 0, 0, 0)))
        cumulative += gross - returned
        history.append(gross)
        daily.append({'date': current.isoformat(), 'gross_minor': gross, 'refunds_minor': returned,
                      'net_minor': gross - returned, 'variable_gross_minor': variable_g,
                      'variable_net_minor': variable_g - variable_r, 'count': count,
                      'cumulative_minor': cumulative if current <= now else None,
                      'average_minor': sum(history[-7:]) // len(history[-7:]),
                      'confirmed': current in checked, 'future': current > now})
    categories = {r.id: r for r in session.scalars(select(Category))}
    category_rows = session.execute(select(Record.category_id,
        func.sum(case((Record.kind == 'expense', Record.amount_minor), else_=0)),
        func.sum(case((Record.kind == 'refund', Record.amount_minor), else_=0)), func.count(Record.id)
    ).where(*clauses, Record.kind != 'movement').group_by(Record.category_id))
    leaves, roots = [], {}
    for key, g, r, count in category_rows:
        cat = categories[key]
        leaf = {'id': key, 'name': cat.name, 'parent_id': cat.parent_id, 'color': cat.color,
                'gross_minor': int(g), 'net_minor': int(g - r), 'count': count}
        leaves.append(leaf)
        root = categories.get(cat.parent_id, cat)
        value = roots.setdefault(root.id, {'id': root.id, 'name': root.name, 'color': root.color,
                                          'gross_minor': 0, 'net_minor': 0, 'count': 0})
        for field in ('gross_minor', 'net_minor', 'count'):
            value[field] += leaf[field]
    tags = []
    tagged = session.execute(select(Tag.id, Tag.name, Tag.color,
        func.sum(case((Record.kind == 'expense', Record.amount_minor), else_=0)),
        func.sum(case((Record.kind == 'refund', Record.amount_minor), else_=0)), func.count(Record.id)
    ).select_from(Record).join(RecordTag, RecordTag.record_id == Record.id).join(Tag, Tag.id == RecordTag.tag_id)
       .where(*clauses, Record.kind != 'movement').group_by(Tag.id, Tag.name, Tag.color))
    for key, name, color, g, r, count in tagged:
        tags.append({'id': key, 'name': name, 'color': color, 'gross_minor': int(g), 'net_minor': int(g - r), 'count': count})
    largest = session.scalar(select(Record).where(*clauses, Record.kind == 'expense')
                             .order_by(Record.amount_minor.desc()).limit(1))
    elapsed = max(0, (min(now + timedelta(days=1), end) - start).days)
    return {'start': start.isoformat(), 'end': (end - timedelta(days=1)).isoformat(),
            'summary': summary, 'daily': daily, 'categories': sorted(roots.values(), key=lambda r: -r['gross_minor']),
            'subcategories': leaves, 'tags': sorted(tags, key=lambda r: -r['gross_minor']),
            'checked_days': len(checked), 'elapsed_days': elapsed,
            'unclassified_count': int(session.scalar(select(func.count()).select_from(Record).where(
                *clauses, Record.category_id == svc.UNCATEGORIZED)) or 0),
            'largest': repo.record_dict(session, largest) if largest else None,
            'small_expenses': totals(session, clauses + [Record.kind == 'expense', Record.amount_minor <= 5000])}


def preview(session, settings, args):
    """只读金额影响预览；正式保存仍在事务内重新校验全部关联。"""
    when = svc.day(args.get('business_date', svc.today(settings.timezone if settings else 'Asia/Shanghai').isoformat()))
    now = svc.today(settings.timezone if settings else 'Asia/Shanghai')
    if when > now:
        raise svc.ExpenseError('未来支出请使用账单计划')
    minor = svc.money(args.get('amount'))
    kind = args.get('kind', 'expense')
    if kind not in ('expense', 'refund', 'movement'):
        raise svc.ExpenseError('记录类型错误')
    old = repo.require(session, Record, args['id']) if args.get('id') else None
    if old and old.deleted_at:
        raise svc.ExpenseError('请先恢复记录', 409)
    bill_id = args.get('bill_occurrence_id') or None
    if kind == 'refund' and args.get('original_record_id'):
        original = repo.require(session, Record, args['original_record_id'])
        bill_id = original.bill_occurrence_id
    signed = minor if kind == 'expense' else -minor if kind == 'refund' else 0
    old_signed = (old.amount_minor if old.kind == 'expense' else -old.amount_minor if old.kind == 'refund' else 0) if old else 0
    paid = budgets.payments(session)
    dates = {when} | ({old.business_date} if old else set())
    results, seen = [], set()
    for chosen in sorted(dates):
        start, end, period, _ = range_for(session, settings, {'date': chosen.isoformat()})
        if start in seen:
            continue
        seen.add(start)
        before = totals(session, filters(session, start, end, {}))['net_minor']
        after = before + (signed if start <= when < end else 0) - (old_signed if old and start <= old.business_date < end else 0)
        budget = period.budget_minor if period else None
        current = start <= now < end
        reserve = sum(b['remaining_minor'] for b in budgets.list_bills(session, end)) if current else None
        if current:
            for key in {bill_id, old.bill_occurrence_id if old else None} - {None}:
                bill = repo.require(session, budgets.Bill, key)
                if bill.due_date >= end:
                    continue
                previous_paid = paid.get(key, 0)
                changed_paid = previous_paid - (old.amount_minor if old and old.kind == 'expense' and old.bill_occurrence_id == key else 0)
                changed_paid += minor if kind == 'expense' and bill_id == key else 0
                prior = max(bill.expected_minor - previous_paid, 0) if bill.status == 'open' else 0
                reopened = bool(old and old.kind == 'expense' and old.bill_occurrence_id == key and changed_paid < previous_paid)
                closing = args.get('settle_bill') == '1' and kind == 'expense' and bill_id == key
                remaining = max(bill.expected_minor - changed_paid, 0) if (bill.status == 'open' or reopened) and not closing else 0
                reserve += remaining - prior
        results.append({'start': start.isoformat(), 'end': (end - timedelta(days=1)).isoformat(),
                        'before': svc.amount(before), 'after': svc.amount(after),
                        'remaining': svc.amount(budget - after) if budget is not None else None,
                        'available': svc.amount(budget - after - reserve) if budget is not None and current else None})
    affected = [r.isoformat() for r in session.scalars(select(Record.business_date).where(
        Record.original_record_id == old.id))] if old else []
    return {'periods': results, 'linked_refund_dates': sorted(set(affected)), 'read_only': True}


def dashboard(session, settings, args):
    # 总览忽略分类/标签筛选，始终展示完整周期；筛选图表走独立 charts 接口。
    span = {k: args[k] for k in ('date', 'start', 'end') if args.get(k)}
    start, end, period, now = range_for(session, settings, span)
    data = analytics(session, settings, span)
    current = start <= now < end
    budget = period.budget_minor if period else None
    rules = svc.loads(period.rule_snapshot_json) if period else {**svc.DEFAULTS, 'weeks': [], 'category_budgets': [], 'fixed_minor': 0}
    net = data['summary']['net_minor']
    bills = budgets.list_bills(session, end)
    shown_bills = [b for b in bills if b['due_date'] >= start.isoformat() or b['status'] == 'open']
    outstanding = sum(b['remaining_minor'] for b in bills) if current else None
    remaining = None if budget is None else budget - net
    available = None if budget is None or outstanding is None else budget - net - outstanding
    comparison_rows = session.execute(select(Record.business_date,
        func.sum(case((Record.kind == 'expense', Record.amount_minor),
                      (Record.kind == 'refund', -Record.amount_minor), else_=0)), func.count(Record.id)
    ).where(Record.business_date >= start - timedelta(days=7), Record.business_date < end,
            Record.deleted_at.is_(None), Record.bill_occurrence_id.is_(None), Record.kind != 'movement')
      .group_by(Record.business_date))
    comparison_days = {when.isoformat(): (int(net), count) for when, net, count in comparison_rows}
    weeks = []
    for week in rules['weeks']:
        actual = sum(d['variable_net_minor'] for d in data['daily'] if week['start'] <= d['date'] < week['end'])
        prior_days = [d - timedelta(days=7) for d in svc.date_range(
            svc.day(week['start']), min(svc.day(week['end']), now + timedelta(days=1)))]
        prior_net = sum(comparison_days.get(d.isoformat(), (0, 0))[0] for d in prior_days)
        prior_count = sum(comparison_days.get(d.isoformat(), (0, 0))[1] for d in prior_days)
        weeks.append({**week, 'actual': svc.amount(actual), 'budget': svc.amount(week['budget_minor']),
                      'previous': svc.amount(prior_net) if prior_count and prior_net else None,
                      'comparison_days': len(prior_days),
                      'status': svc.budget_status(actual, week['budget_minor'], rules['warning'], rules['critical']),
                      'current': week['start'] <= now.isoformat() < week['end']})
    category_stats = {r['id']: r['net_minor'] for r in data['subcategories']}
    category_stats.update({r['id']: r['net_minor'] for r in data['categories']})
    category_budgets = [{**item, 'budget': svc.amount(item['budget_minor']),
                         'actual': svc.amount(category_stats.get(item['category_id'], 0)),
                         'status': svc.budget_status(category_stats.get(item['category_id'], 0), item['budget_minor'], rules['warning'], rules['critical'])}
                        for item in rules['category_budgets']]
    elapsed, length = data['elapsed_days'], (end - start).days
    complete = bool(elapsed and data['checked_days'] == elapsed and settings and settings.tracking_start_date <= start)
    variable_gross = sum(d['variable_gross_minor'] for d in data['daily'])
    variable_net = sum(d['variable_net_minor'] for d in data['daily'])
    pool = None if budget is None else max(budget - rules['fixed_minor'], 0)
    pace_fast = False
    if current and pool is not None and rules['pace_points']:
        pace_fast = variable_net > 0 if pool == 0 else variable_net * 100 * length >= pool * (elapsed * 100 + rules['pace_points'] * length)
    forecast = (net + outstanding + variable_gross * (length - elapsed) // elapsed
                if current and elapsed >= 7 and complete else None)
    data.update({'period': budgets.period_dict(period) if period else None,
                 'needs_open': period is None and not bool(span.get('start')),
                 'current': current, 'today': now.isoformat(), 'budget': svc.amount(budget),
                 'remaining': svc.amount(remaining), 'outstanding': svc.amount(outstanding), 'available': svc.amount(available),
                 'daily_allowance': svc.amount(max(available, 0) // (end - now).days) if available is not None else None,
                 'budget_status': svc.budget_status(net, budget, rules['warning'], rules['critical']),
                 'weeks': weeks, 'category_budgets': category_budgets, 'bills': shown_bills,
                 'pace_fast': pace_fast, 'forecast': svc.amount(forecast), 'complete': complete,
                 'time_percent': round(elapsed * 100 / length, 2),
                 'fixed_over_budget': budget is not None and rules['fixed_minor'] > budget,
                 'unallocated_week': svc.amount(None if pool is None else pool - sum(w['budget_minor'] or 0 for w in weeks))})
    # 相同已过天数对比；无基期或零基期不构造无限增长率。
    prev_day = start - timedelta(days=1)
    prev_period = budgets.period_at(session, prev_day)
    ps, pe, _ = svc.period_rule(prev_day, repo.preferences(settings)['rules'])
    if prev_period:
        ps, pe = prev_period.start_date, prev_period.end_date_exclusive
    pe = min(pe, ps + timedelta(days=elapsed))
    previous = totals(session, filters(session, ps, pe, {}))
    data['comparison'] = {'previous': previous['net'], 'previous_count': previous['count'],
                          'days': max((pe - ps).days, 0),
                          'current_daily': svc.amount(net // elapsed) if elapsed else None,
                          'previous_daily': svc.amount(previous['net_minor'] // (pe - ps).days) if pe > ps else None}
    return data
