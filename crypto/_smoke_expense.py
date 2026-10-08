"""支出模块隔离验收；必须配置 CRYPTO_TEST_DB_URL，仅建 expense_* 表。
运行：python -B -m crypto._smoke_expense
网页：python -B -m crypto._smoke_expense --serve（仅回环地址，无交易调度器）
测试目标须为空账本；不接受覆盖已有测试数据。SQLite 不代表 MySQL 锁验收。
"""
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from .test_isolation import require_isolated_test_db, TestDbNotConfigured

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
try:
    TEST_URL = require_isolated_test_db()
except TestDbNotConfigured as exc:
    print(exc)
    raise SystemExit(2)

from flask import Flask, request, jsonify
from sqlalchemy import create_engine, event, select, delete, func, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql
from . import expense_routes as routes, expense_service as svc, expense_repo as repo
from . import expense_budget_repo as budgets
from .database import Base

ENGINE = create_engine(TEST_URL)
if ENGINE.dialect.name == 'sqlite':
    @event.listens_for(ENGINE, 'connect')
    def foreign_keys(connection, record):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=15000')

TABLES = [t for t in Base.metadata.sorted_tables if t.name.startswith('expense_')]
Base.metadata.create_all(ENGINE, tables=TABLES)
FACTORY = sessionmaker(ENGINE, expire_on_commit=False)
with FACTORY() as session:
    if any(session.scalar(select(func.count()).select_from(t)) for t in TABLES):
        print('拒绝覆盖：隔离库 expense_* 表已有数据，请另选空测试库。')
        raise SystemExit(2)


@contextmanager
def scope():
    with FACTORY.begin() as session:
        yield session


routes.session_scope = scope
HERE = Path(__file__).resolve().parent
APP = Flask('expense_smoke', template_folder=str(HERE / 'templates'), static_folder=str(HERE / 'static'))
APP.config['TESTING'] = True
APP.register_blueprint(routes.expense_bp)


@APP.context_processor
def navigation_context():
    # 复用导航纯规则；隔离网页不加载导航写接口或查询业务配置表。
    from .navigation_service import build_default_config, resolve_active
    default = build_default_config()
    active, parent = resolve_active(default['items'], request.path, request.args.get('_ap'))
    return {'nav_default_config': default, 'nav_active_id': active,
            'nav_active_parent_id': parent, 'nav_request_path': request.path}


@APP.get('/api/navigation/config')
def navigation_preview():
    from .navigation_service import build_default_config
    return jsonify(success=True, config=build_default_config(), source='expense-test', editable=False, warnings=[])


HEADERS = {'Accept': 'application/json', 'X-Expense-Request': '1', 'Origin': 'http://localhost'}
NOW = date(2026, 9, 15)


class ExpenseTest(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(svc, 'today', return_value=NOW)
        self.clock.start()
        self.client = APP.test_client()
        self.statements = []
        self.listener = lambda conn, cursor, statement, params, context, many: self.statements.append(statement)
        event.listen(ENGINE, 'before_cursor_execute', self.listener)
        self.call('POST', '/setup', {'budget': '1000.00', 'tracking_start_date': '2026-09-01'})
        self.boot = self.get('/bootstrap')
        self.cat = next(c['id'] for c in self.boot['categories'] if c['name'] == '午餐')
        self.tags = [t['id'] for t in self.boot['tags'][:2]]

    def tearDown(self):
        event.remove(ENGINE, 'before_cursor_execute', self.listener)
        self.clock.stop()
        with FACTORY.begin() as session:
            session.execute(delete(repo.RecordTag))
            session.execute(delete(repo.Record).where(repo.Record.original_record_id.is_not(None)))
            session.execute(delete(repo.Record))
            # 先清依赖分类的账单模板，再清一级分类。
            for model in (repo.Bill, repo.BillTemplate):
                session.execute(delete(model))
            session.execute(delete(repo.Category).where(repo.Category.parent_id.is_not(None)))
            for table in reversed(TABLES):
                if table.name not in ('expense_records', 'expense_record_tags', 'expense_bill_occurrences', 'expense_bill_templates'):
                    session.execute(delete(table))

    def call(self, method, path, body=None, status=200):
        response = self.client.open('/expense/api' + path, method=method, json=body, headers=HEADERS)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True)[:1000])
        value = response.get_json()
        return value['data'] if status < 400 else value['error']

    def get(self, path):
        return self.call('GET', path)

    def record(self, amount='10.00', **fields):
        return self.call('POST', '/records', {'amount': amount, 'business_date': NOW.isoformat(),
            'category_id': self.cat, 'client_request_id': repo.uid(), **fields})['record']

    def test_money_and_calendar(self):
        self.assertEqual(svc.money('0.10') + svc.money('0.20'), 30)
        for value in ('-1', 'NaN', 'Infinity', '1e2', '1.001', 1.2, True, '100000000'):
            with self.assertRaises(svc.ExpenseError):
                svc.money(value)
        self.assertEqual(svc.money('0', zero=True), 0)
        self.assertIsNone(svc.money(None, nullable=True))
        self.assertEqual(svc.ordinary_bounds(date(2024, 2, 29), 31), (date(2024, 2, 29), date(2024, 3, 31)))
        self.assertEqual(list(svc.bill_dates(date(2024, 2, 29), 'yearly', date(2027, 1, 1))),
                         [date(2024, 2, 29), date(2025, 2, 28), date(2026, 2, 28)])
        for year in (2023, 2024, 2026):
            for month in range(1, 13):
                for cycle in (1, 28, 29, 30, 31):
                    start, end = svc.ordinary_bounds(date(year, month, 15), cycle)
                    weeks = svc.weekly_allocation(start, end, 10001, 2000)
                    self.assertEqual(sum(w['budget_minor'] for w in weeks), 8001)
                    self.assertEqual(weeks[0]['start'], start.isoformat())
                    self.assertEqual(weeks[-1]['end'], end.isoformat())
        for value, level in ((799, 'normal'), (800, 'warning'), (900, 'critical'), (1000, 'exhausted'), (1001, 'over')):
            self.assertEqual(svc.budget_status(value, 1000)['level'], level)
        self.assertEqual(svc.budget_status(1, 0)['level'], 'over')
        self.assertIsNone(svc.budget_status(1, 0)['percent'])

    def test_record_idempotency_tags_and_versions(self):
        body = {'amount': '0.10', 'category_id': self.cat, 'tag_ids': self.tags, 'client_request_id': repo.uid()}
        first = self.call('POST', '/records', body)
        replay = self.call('POST', '/records', body)
        self.assertEqual(first['record']['id'], replay['record']['id'])
        self.assertTrue(replay['replayed'])
        self.call('POST', '/records', {**body, 'amount': '0.20'}, 409)
        self.record('0.20')
        self.assertEqual(self.get('/dashboard')['summary']['net'], '0.30')
        selected = self.get('/records?tags=' + ','.join(self.tags))
        self.assertEqual(selected['summary']['net'], '0.10')
        rec = first['record']
        self.call('PATCH', '/records/' + rec['id'], {'version': 1, 'amount': '0.15'})
        self.call('PATCH', '/records/' + rec['id'], {'version': 1, 'amount': '0.16'}, 409)
        self.assertEqual(set(self.get('/records/' + rec['id'])['tag_ids']), set(self.tags))

    def test_refunds_dates_and_restore(self):
        original = self.record('100.00', business_date='2026-08-31')
        refund = self.record('40.00', kind='refund', original_record_id=original['id'])
        self.assertEqual(self.get('/dashboard')['summary']['net'], '-40.00')
        self.call('POST', '/records', {'amount': '61.00', 'kind': 'refund', 'original_record_id': original['id'],
                                     'client_request_id': repo.uid()}, 400)
        self.call('DELETE', '/records/' + original['id'], {'version': 1}, 409)
        self.call('PATCH', '/records/' + original['id'], {'version': 1, 'amount': '39.00'}, 400)
        self.call('DELETE', '/records/' + refund['id'], {'version': 1})
        self.call('DELETE', '/records/' + original['id'], {'version': 1})
        self.call('POST', '/records/' + refund['id'] + '/restore', {'version': 2}, 409)
        self.call('POST', '/records/' + original['id'] + '/restore', {'version': 2})
        self.call('POST', '/records/' + refund['id'] + '/restore', {'version': 2})
        movement = self.record('800.00', kind='movement')
        self.assertEqual(self.get('/dashboard')['summary']['net'], '-40.00')
        self.assertEqual(movement['kind'], 'movement')

    def test_concurrent_refund_limit(self):
        original = self.record('100.00')
        def attempt(_):
            with APP.test_client() as client:
                return client.post('/expense/api/records', json={'amount': '60.00', 'kind': 'refund',
                    'original_record_id': original['id'], 'client_request_id': repo.uid(), 'confirm_duplicate': True},
                    headers=HEADERS).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = sorted(executor.map(attempt, range(2)))
        self.assertEqual(results, [200, 400])
        self.assertEqual(self.get('/records/' + original['id'])['refund_total'], '60.00')

    def test_budget_transition_and_history(self):
        current = self.get('/dashboard')['period']
        self.call('PATCH', '/settings', {'version': 1, 'cycle_day': 25, 'budget': '1200.00', 'transition_budget': '800.00'})
        transition = self.call('POST', '/periods/open', {'date': '2026-10-01'})
        regular = self.call('POST', '/periods/open', {'date': '2026-10-25'})
        self.assertEqual(transition['end_date_exclusive'], '2026-10-25')
        self.assertEqual(transition['budget'], '800.00')
        self.assertEqual(regular['budget'], '1200.00')
        self.assertEqual(self.get('/periods/' + current['id'])['budget'], '1000.00')
        old = self.call('POST', '/periods/open', {'date': '2025-01-01'})
        self.assertIsNone(old['budget'])
        self.call('PATCH', '/periods/' + current['id'] + '/budget', {'version': 1, 'budget': '0', 'reason': '测试零预算'})
        self.record('1.00')
        self.assertEqual(self.get('/dashboard')['budget_status']['level'], 'over')

    def test_bill_partial_payment_edit_and_void(self):
        self.call('POST', '/bill-templates', {'name': '房租测试', 'amount': '500.00', 'category_id': self.cat,
             'first_date': NOW.isoformat(), 'frequency': 'monthly', 'confirm_reallocate': True})
        bill = self.get('/bills')[0]
        self.assertEqual(self.get('/dashboard')['outstanding'], '500.00')
        payload = {'amount': '200.00', 'client_request_id': repo.uid()}
        paid = self.call('POST', '/bills/' + bill['id'] + '/pay', payload)['record']
        self.call('POST', '/bills/' + bill['id'] + '/pay', payload)
        self.assertEqual(self.get('/dashboard')['outstanding'], '300.00')
        self.assertEqual(sum(int(float(w['actual']) * 100) for w in self.get('/dashboard')['weeks']), 0)
        second = self.call('POST', '/bills/' + bill['id'] + '/pay',
                           {'amount': '100.00', 'client_request_id': repo.uid(), 'settle_bill': True})['record']
        self.assertEqual(self.get('/dashboard')['outstanding'], '0.00')
        self.record('50.00', kind='refund', original_record_id=paid['id'])
        self.assertEqual(self.get('/dashboard')['outstanding'], '0.00')
        self.call('PATCH', '/records/' + second['id'], {'version': 1, 'amount': '90.00'})
        self.assertEqual(self.get('/dashboard')['outstanding'], '210.00')
        self.call('DELETE', '/records/' + second['id'], {'version': 2})
        self.assertEqual(self.get('/dashboard')['outstanding'], '300.00')
        self.call('POST', '/periods/open', {'date': '2026-10-01'})
        self.call('POST', '/periods/open', {'date': '2026-10-01'})
        self.assertEqual(len(self.get('/bills?date=2026-10-01')), 2)

    def test_daily_check_dictionary_and_export(self):
        self.call('POST', '/daily-checks', {'business_date': NOW.isoformat()})
        self.assertEqual(self.get('/dashboard')['checked_days'], 1)
        rec = self.record('23.00', tag_ids=self.tags, note='=SUM(1,2)', merchant='<img src=x onerror=alert(1)>')
        self.assertEqual(self.get('/dashboard')['checked_days'], 0)
        self.call('DELETE', '/tags/' + self.tags[0], {'version': 1})
        self.call('POST', '/setup', {})
        self.assertFalse(next(t for t in self.get('/tags') if t['id'] == self.tags[0])['is_active'])
        self.call('DELETE', '/records/' + rec['id'], {'version': 1})
        self.call('POST', '/records/' + rec['id'] + '/restore', {'version': 2})
        self.assertEqual(len(self.get('/records/' + rec['id'])['tag_ids']), 2)
        result = self.client.get('/expense/api/export')
        self.assertIn("'=SUM", result.get_data(as_text=True))
        self.assertIn('no-store', result.headers['Cache-Control'])

    def test_read_only_preview_and_failure_atomicity(self):
        rec = self.record('10.00')
        period = self.get('/dashboard')['period']
        paths = ['/bootstrap', '/settings', '/dashboard', '/charts', '/records', '/records/' + rec['id'],
                 '/tags', '/categories', '/periods/' + period['id'], '/bill-templates', '/bills', '/export',
                 '/preview?amount=20.00&id=' + rec['id']]
        self.statements.clear()
        for path in paths:
            response = self.client.get('/expense/api' + path)
            self.assertEqual(response.status_code, 200, path)
        forbidden = [q for q in self.statements if q.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE', 'CREATE', 'ALTER', 'DROP', 'REPLACE'))]
        self.assertEqual(forbidden, [])
        count = self.get('/records')['total']
        with patch.object(repo, 'audit', side_effect=svc.ExpenseError('测试回滚')):
            self.call('POST', '/records', {'amount': '99.00', 'client_request_id': repo.uid()}, 400)
        self.assertEqual(self.get('/records')['total'], count)
        with patch.object(routes.queries, 'dashboard', side_effect=RuntimeError('测试统计失败')):
            result = self.call('POST', '/records', {'amount': '98.00', 'client_request_id': repo.uid()})
        self.assertTrue(result['statistics_pending'])
        self.assertEqual(self.get('/records')['total'], count + 1)

    def test_security_headers(self):
        self.assertEqual(self.client.post('/expense/api/records', json={}).status_code, 400)
        response = self.client.post('/expense/api/records', json={}, headers={**HEADERS, 'Origin': 'https://evil.invalid'})
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/expense/api/records', json=[], headers=HEADERS)
        self.assertEqual(response.status_code, 400)
        self.call('POST', '/records', {'amount': '1.00', 'business_date': '2026-09-16', 'client_request_id': repo.uid()}, 400)

    def test_category_manual_and_future_budgets(self):
        period = self.get('/dashboard')['period']
        root = next(c['parent_id'] for c in self.boot['categories'] if c['id'] == self.cat)
        entries = [{'category_id': root, 'budget': '600.00'}, {'category_id': self.cat, 'budget': '200.00'}]
        path = '/periods/' + period['id'] + '/budget'
        self.call('PATCH', path, {'version': 1, 'category_budgets': [{'category_id': self.cat, 'budget': '1'}], 'reason': '拒绝无父预算'}, 400)
        weeks = [{'budget': '50.00'} for w in period['rules']['weeks']]
        self.call('PATCH', path, {'version': 1, 'category_budgets': entries, 'week_mode': 'manual', 'weeks': weeks, 'reason': '手动分配'})
        self.record('20.00')
        self.assertEqual(self.get('/dashboard')['category_budgets'][0]['actual'], '20.00')
        self.assertEqual(self.get('/dashboard')['unallocated_week'], svc.amount(100000 - len(weeks) * 5000))
        self.call('PATCH', '/settings', {'version': 1, 'category_budgets': entries})
        future = self.call('POST', '/periods/open', {'date': '2026-10-01'})
        self.assertEqual(future['rules']['category_budgets'][0]['budget_minor'], 60000)
        self.call('PATCH', '/settings', {'version': 2, 'budget': '500'}, 400)

    def test_bill_link_skip_reopen_and_snapshot(self):
        original = self.record('80.00')
        refund = self.record('10.00', kind='refund', original_record_id=original['id'])
        t = self.call('POST', '/bill-templates', {'name': '计划', 'amount': '100.00', 'first_date': '2026-09-20',
            'category_id': self.cat, 'frequency': 'once', 'confirm_reallocate': True})
        bill = self.get('/bills')[0]
        self.call('POST', '/bills/' + bill['id'] + '/link-record', {'version': bill['version'], 'record_id': original['id'], 'record_version': original['version']})
        self.assertEqual(self.get('/records/' + refund['id'])['bill_occurrence_id'], bill['id'])
        self.assertEqual(self.get('/dashboard')['outstanding'], '20.00')
        self.assertTrue(all(w['actual'] == '0.00' for w in self.get('/dashboard')['weeks']))
        old_period = self.get('/dashboard')['period']
        self.call('POST', '/bills/' + bill['id'] + '/skip', {'version': self.get('/bills')[0]['version']}, 409)
        self.call('POST', '/bills/' + bill['id'] + '/skip', {'version': self.get('/bills')[0]['version'], 'confirm_reallocate': True})
        self.assertEqual(self.get('/dashboard')['outstanding'], '0.00')
        self.call('POST', '/bills/' + bill['id'] + '/reopen', {'version': self.get('/bills')[0]['version'], 'confirm_reallocate': True})
        self.assertEqual(self.get('/dashboard')['outstanding'], '20.00')
        self.assertEqual(old_period['rules']['bill_baseline'][0]['amount_minor'], 10000)
        self.call('DELETE', '/bill-templates/' + t['id'], {'version': 1})
        self.assertEqual(len(self.get('/bills')), 1)

    def test_preview_exact_and_inheritance(self):
        original = self.record('10.00')
        preview = self.get('/preview?amount=20.00&id=' + original['id'])
        self.assertEqual(preview['periods'][0]['before'], '10.00')
        self.assertEqual(preview['periods'][0]['after'], '20.00')
        self.assertEqual(preview['periods'][0]['remaining'], '980.00')
        self.assertEqual(self.get('/records')['summary']['net'], '10.00')
        self.call('POST', '/records', {'kind': 'refund', 'amount': '10.00', 'unlinked_refund_reason': '历史退款', 'client_request_id': repo.uid()}, 400)
        self.record('10.00', kind='refund', unlinked_refund_reason='历史退款')
        self.get('/charts?tags=' + ','.join(self.tags))
        self.call('POST', '/records', {'amount': '1', 'category_id': {'bad': 'id'}, 'client_request_id': repo.uid()}, 400)
        self.call('PATCH', '/settings', {'version': 1, 'timezone': '/etc/passwd'}, 400)

    def test_page_auth_and_schema_contract(self):
        from . import web_auth
        gated = Flask('expense_auth_smoke', template_folder=str(HERE / 'templates'), static_folder=str(HERE / 'static'))
        gated.register_blueprint(routes.expense_bp)
        web_auth.init_app(gated)
        client = gated.test_client()
        with patch.object(web_auth, 'configured_token', return_value=repo.uid()):
            for path in ('/expense', '/expense/api/bootstrap', '/expense/api/export'):
                self.assertEqual(client.get(path, headers={'Accept': 'application/json'}).status_code, 401)
            self.assertEqual(client.post('/expense/api/records', json={}, headers=HEADERS).status_code, 401)
        with patch.object(web_auth, 'configured_token', return_value=''):
            self.assertEqual(client.get('/expense', environ_base={'REMOTE_ADDR': '203.0.113.10'}).status_code, 403)
        for name in APP.jinja_env.list_templates():
            APP.jinja_env.get_template(name)
        response = self.client.get('/expense')
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('支出管理', page)
        self.assertIn('/expense', page)
        ids = re.findall(r'\bid="([^"]+)"', page)
        self.assertEqual(len(ids), len(set(ids)))
        source = (HERE / 'static/js/expense.js').read_text(encoding='utf-8')
        requested = re.findall(r"\$\('([^']+)'\)", source)
        self.assertTrue(set(requested).issubset(set(ids)), set(requested) - set(ids))
        schema = (HERE.parent / 'db_schema.sql').read_text(encoding='utf-8')
        for table in TABLES:
            block = re.search(r'CREATE TABLE IF NOT EXISTS `' + table.name + r'` \((.*?)\) ENGINE=', schema, re.S).group(1)
            sql_columns = re.findall(r'^\s+`([^`]+)` ', block, re.M)
            self.assertEqual(set(sql_columns), set(table.columns.keys()))
            ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
            self.assertIn('InnoDB', ddl)
            self.assertIn('utf8mb4_bin', ddl)

    def test_uninitialized_get_and_forecast_completeness(self):
        with scope() as session:
            row = session.get(repo.Settings, 1)
            initialized = row.initialized_at
            row.initialized_at = None
        self.statements.clear()
        self.assertFalse(self.get('/bootstrap')['settings']['initialized'])
        self.get('/dashboard?date=2025-01-01')
        self.assertFalse(any(q.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE')) for q in self.statements))
        with scope() as session:
            session.get(repo.Settings, 1).initialized_at = initialized
        self.record('150.00')
        self.assertIsNone(self.get('/dashboard')['forecast'])
        for when in svc.date_range(date(2026, 9, 1), NOW + timedelta(days=1)):
            self.call('POST', '/daily-checks', {'business_date': when.isoformat()})
        self.assertEqual(self.get('/dashboard')['forecast'], '300.00')
        self.record('1.00')
        self.assertIsNone(self.get('/dashboard')['forecast'])
        self.call('POST', '/records', {'amount': '1.00', 'business_date': '2026-09-15', 'category_id': self.cat, 'client_request_id': repo.uid()}, 409)
        self.record('1.00', confirm_duplicate=True)

    def test_pending_transition_survives_default_edit(self):
        self.call('PATCH', '/settings', {'version': 1, 'cycle_day': 25, 'budget': '1200', 'transition_budget': '800'})
        self.call('PATCH', '/settings', {'version': 2, 'budget': '1300'})
        transition = self.call('POST', '/periods/open', {'date': '2026-10-01'})
        self.assertEqual(transition['budget'], '800.00')
        self.assertEqual(transition['end_date_exclusive'], '2026-10-25')
        regular = self.call('POST', '/periods/open', {'date': '2026-10-25'})
        self.assertEqual(regular['budget'], '1300.00')

    def test_bill_future_adjustment_and_historical_basis(self):
        self.call('POST', '/bill-templates', {'name': '历史欠付', 'amount': '100', 'first_date': '2026-08-20',
            'category_id': self.cat, 'frequency': 'once', 'confirm_reallocate': True})
        bill = self.get('/bills')[0]
        historical = self.call('POST', '/periods/open', {'date': '2026-08-01'})
        future = self.call('POST', '/periods/open', {'date': '2026-10-01'})
        self.assertEqual(future['rules']['fixed_minor'], 10000)
        self.call('POST', '/bills/' + bill['id'] + '/skip', {'version': bill['version'], 'confirm_reallocate': True})
        self.assertEqual(self.get('/periods/' + future['id'])['rules']['fixed_minor'], 0)
        changed = self.call('PATCH', '/periods/' + historical['id'] + '/budget',
                            {'version': 1, 'budget': '200', 'reason': '仅更改历史总预算'})
        self.assertEqual(changed['rules']['fixed_minor'], 10000)
        self.assertEqual(changed['rules']['bill_baseline'], historical['rules']['bill_baseline'])

    def test_full_form_payload_and_tag_audit(self):
        payload = {'amount': '12.34', 'business_date': NOW.isoformat(), 'kind': 'expense',
                   'category_id': self.cat, 'merchant': '', 'payment_method': '', 'note': '',
                   'bill_occurrence_id': '', 'original_record_id': '', 'unlinked_refund_reason': '',
                   'settle_bill': False, 'tag_ids': self.tags, 'client_request_id': repo.uid()}
        saved = self.call('POST', '/records', payload)['record']
        self.call('PATCH', '/records/' + saved['id'], {**payload, 'amount': '20.00', 'tag_ids': self.tags[:1], 'version': 1})
        detail = self.get('/records/' + saved['id'])
        log = next(log for log in detail['history'] if log['action'] == 'update')
        self.assertEqual(set(svc.loads(log['before_json'])['tag_ids']), set(self.tags))
        self.assertEqual(svc.loads(log['after_json'])['tag_ids'], self.tags[:1])
        refund = {**payload, 'amount': '5', 'kind': 'refund', 'original_record_id': saved['id'], 'client_request_id': repo.uid()}
        self.call('POST', '/records', refund)
        self.assertEqual(self.get('/dashboard')['summary']['net'], '15.00')
        self.assertEqual(self.get('/dashboard')['categories'][0]['net_minor'], 1500)

    def test_nested_invalid_category_budget(self):
        period = self.get('/dashboard')['period']
        self.call('PATCH', '/periods/' + period['id'] + '/budget', {'version': 1, 'reason': '无效参数',
                  'category_budgets': [{'category_id': {}, 'budget': '1'}]}, 400)
        self.assertEqual(self.get('/periods/' + period['id'])['version'], 1)

    def test_javascript_unit_contract(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('未安装 Node，未验证前端单元契约')
        source = (HERE / 'static/js/expense.js').read_text(encoding='utf-8')
        script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const nodes = new Map();
let listener, sent;
const context = vm.createContext({
    document: {getElementById: id => {
        if (!nodes.has(id)) nodes.set(id, {addEventListener: (_, fn) => {listener=fn;}});
        return nodes.get(id);
    }},
    URLSearchParams,
    fetch: async (url, options) => {
        sent={url, options}; return {ok:true, json:async()=>({success:true, data:{saved:true}})};
    }
});
let source=fs.readFileSync(0, 'utf8');
source=source.slice(0, source.indexOf('    const actions='));
vm.runInContext(source+'globalThis.test={minor,intMoney,esc,categoryOptions,state,on,request};})();',context);
const t=context.test;
assert.equal(t.minor(t.intMoney('0.10')+t.intMoney('0.20')), '0.30');
assert.equal(t.minor(-30n), '-0.30');
for (const bad of ['-1','NaN','Infinity','1e2','1.001']) assert.throws(()=>t.intMoney(bad));
assert.equal(t.esc('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
t.state.categories=[{id:'a',name:'普通',is_active:true},{id:'b',name:'最近',is_active:true}];
t.state.recent_categories=['b'];
const options=t.categoryOptions();
assert.ok(options.indexOf('value="b"')<options.indexOf('value="a"'));
t.state.categories.push({id:'p',name:'停用父类',is_active:false},{id:'c',parent_id:'p',name:'子类',is_active:true});
assert.ok(!t.categoryOptions().includes('value="c"'));
assert.ok(t.categoryOptions('c').includes('value="c"'));
let prevented=false;
t.on('form','submit',async e=>{e.preventDefault();});
listener({preventDefault:()=>{prevented=true;}});
assert.ok(prevented, '阻止表单默认提交必须同步执行');
(async()=>{
    await t.request('/records','POST',{amount:'0.10'});
    assert.equal(sent.options.headers.Accept,'application/json');
    assert.equal(sent.options.headers['X-Expense-Request'],'1');
    assert.equal(sent.options.credentials,'same-origin');
    assert.equal(sent.options.cache,'no-store');
    assert.equal(JSON.parse(sent.options.body).amount,'0.10');
    context.fetch=async()=>({ok:false,status:409,json:async()=>({success:false,error:{message:'版本冲突',details:{current_version:2}}})});
    await assert.rejects(t.request('/records'), e=>e.status===409&&e.details.current_version===2);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([node, '-e', script], input=source, text=True, encoding='utf-8',
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pagination_performance_and_mysql_compile(self):
        with scope() as session:
            session.execute(repo.Record.__table__.insert(), [{'id': repo.uid(), 'kind': 'expense', 'amount_minor': i + 1,
                'business_date': NOW, 'category_id': self.cat, 'client_request_id': repo.uid(), 'request_payload_hash': '0' * 64}
                for i in range(10000)])
        samples = []
        for _ in range(20):
            started = time.perf_counter()
            self.statements.clear()
            listing = self.get('/records?page_size=50&page=2')
            self.get('/dashboard')
            samples.append((time.perf_counter() - started) * 1000)
        self.assertEqual(listing['total'], 10000)
        self.assertEqual(len(listing['records']), 50)
        self.assertLess(len(self.statements), 50)
        print(f'\n隔离 {ENGINE.dialect.name}：1 万笔，列表+总览 P95={sorted(samples)[18]:.1f}ms，SQL={len(self.statements)}')
        for table in TABLES:
            self.assertIn('CREATE TABLE', str(CreateTable(table).compile(dialect=mysql.dialect())))
        self.assertTrue({t.name for t in TABLES}.issubset(set(inspect(ENGINE).get_table_names())))
        Base.metadata.create_all(ENGINE, tables=TABLES)
        from . import database
        self.assertTrue({t.name for t in TABLES}.issubset(set(database._NEW_TABLE_NAMES)))
        with patch.object(Base.metadata, 'create_all') as create:
            database._ensure_extra_tables(ENGINE)
            self.assertTrue({t.name for t in TABLES}.issubset({t.name for t in create.call_args.kwargs['tables']}))
            self.assertTrue(create.call_args.kwargs['checkfirst'])


if __name__ == '__main__':
    if '--serve' in sys.argv:
        from .web_auth import init_app
        init_app(APP)
        APP.config['TESTING'] = False
        APP.run(host='127.0.0.1', port=5091, debug=False, use_reloader=False)
    else:
        unittest.main(verbosity=2)
