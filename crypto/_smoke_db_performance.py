"""数据库优化离线回归：只使用内存 SQLite，不加载真 app、不连外部服务。
运行：python -B -m unittest crypto._smoke_db_performance -v
SQLite 仅验证功能与 SQL 结构，不能替代 MySQL 锁及精度验收。
"""
import ast
import contextlib
import datetime
import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.dialects import mysql as _mysql
from sqlalchemy.orm import sessionmaker

from . import calorie_repo as repo, calorie_routes as routes, database
from . import market_data_repo, config_store_repo as configs, discipline_repo as discipline
from . import trading_runtime_repo as runtime, balance_repo, plan_repo, plan_routes
from .models import (CalorieConfig, CalorieRecord, CalorieMealItem, CalorieFood,
                     CryptoCoin, KVStore, TaskAnalysisRecord, AnalysisReminderLog,
                     PlanPlan, PlanCard, PlanSlot, BalanceHistory)
from .db_performance import TimedQueuePool, instrument_engine, measure, init_app


TABLES = [CalorieConfig.__table__, CalorieRecord.__table__, CalorieMealItem.__table__,
          CalorieFood.__table__, CryptoCoin.__table__, KVStore.__table__,
          TaskAnalysisRecord.__table__, AnalysisReminderLog.__table__,
          PlanPlan.__table__, PlanCard.__table__, PlanSlot.__table__, BalanceHistory.__table__]


def seed_calories(session, count=100):
    session.add(CalorieConfig(id=1, **repo.DEFAULT_CONFIG))
    start = datetime.date(2020, 1, 1)
    for i in range(count):
        day = (start + datetime.timedelta(days=i)).isoformat()
        session.add(CalorieRecord(id=day, date=day, morning_weight=120,
                                 calorie_deficit=i + 0.125, cumulative_deficit=0,
                                 breakfast_calories=100, breakfast_food='已有食物'))
    session.flush()
    for i in range(count):
        day = (start + datetime.timedelta(days=i)).isoformat()
        session.add(CalorieMealItem(record_id=day, meal='breakfast', position=0,
                                   name='已有食物', calories=100, quantity=0.5, unit='份'))
    session.add(CalorieFood(id='food_001', name='已有食物', calories=100))
    session.flush()


def legacy_records(session):
    rows = session.execute(select(CalorieRecord).order_by(CalorieRecord.date)).scalars().all()
    return [repo._record_to_dict(session, row) for row in rows]


class DatabasePerformanceTests(unittest.TestCase):
    def setUp(self):
        configs.invalidate_config_cache()
        self.engine = create_engine('sqlite://', poolclass=TimedQueuePool)
        instrument_engine(self.engine)
        for table in TABLES:
            table.create(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.register_blueprint(routes.calorie_bp)
        init_app(self.app)

        @contextlib.contextmanager
        def scope():
            with self.factory.begin() as session:
                yield session
        self.scope = scope
        self.route_patch = patch.object(routes, 'session_scope', scope)
        self.route_patch.start()
        self.client = self.app.test_client()
        self.statements = []
        self.cache_probe_key = 'coin_selection'

        @event.listens_for(self.engine, 'before_cursor_execute')
        def record(conn, cursor, statement, params, context, many):
            self.statements.append(statement)

    def tearDown(self):
        self.route_patch.stop()
        self.engine.dispose()

    def seed(self, count=100):
        with self.scope() as s:
            seed_calories(s, count)

    def test_batch_records_equivalent_and_constant_queries(self):
        self.seed()
        with self.scope() as s, measure() as old:
            expected = legacy_records(s)
        with self.scope() as s, measure() as new:
            actual = repo.load_records(s)
        self.assertEqual(actual, expected)
        self.assertEqual(old.sql_count, 101)
        self.assertEqual(new.sql_count, 2)
        self.assertEqual(actual[0]['breakfast_foods'][0]['total_calories'], 50)
        with measure() as request:
            response = self.client.get('/calorie/api/records').get_json()
        self.assertTrue(response['success'])
        self.assertEqual(request.sql_count, 3)
        self.assertEqual(response['records'][0]['date'], actual[-1]['date'])

    def test_batch_empty_and_chunk_boundary(self):
        with self.scope() as s, measure() as metrics:
            self.assertEqual(repo.load_records(s), [])
        self.assertEqual(metrics.sql_count, 1)
        self.seed(501)
        with self.scope() as s, measure() as metrics:
            self.assertEqual(len(repo.load_records(s, False)), 501)
        self.assertEqual(metrics.sql_count, 3)

    def test_metrics_projection_and_sparse_updates(self):
        self.seed(2)
        with self.scope() as s:
            before = repo.load_record_metrics(s)
            self.assertNotIn('breakfast_food', before[0])
            after = routes._recalc_cumulative([dict(r) for r in before])
            updates = repo.changed_metrics(before, after)
            repo.bulk_update_records(s, updates)
            self.assertEqual(repo.changed_metrics(after, repo.load_record_metrics(s)), [])
            repo.bulk_update_records(s, [{'id': before[0]['id'], 'bmr': 333},
                                         {'id': before[1]['id'], 'calorie_deficit': -10}])
            result = repo.load_record_metrics(s)
            self.assertEqual(result[0]['calorie_deficit'], 0.125)
            self.assertEqual(result[1]['bmr'], 0)
            self.assertEqual(result[0]['bmr'], 333)

    def assert_cumulative(self):
        with self.scope() as s:
            actual = repo.load_record_metrics(s)
        expected = routes._recalc_cumulative([dict(r) for r in actual])
        self.assertEqual(actual, expected)
        return actual

    def test_http_create_update_delete_config(self):
        self.seed(10)
        for day in ('2020-01-05', '2019-12-31', '2020-02-01'):
            payload = dict(date=day, morning_weight=120, daily_steps=1234,
                           breakfast_foods=[dict(name='已有食物', calories=100, quantity=1.5),
                                            dict(name='新食物', calories=30, quantity=2)])
            self.statements.clear()
            response = self.client.post('/calorie/api/record', json=payload)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data['success'])
            self.assertEqual(data['record']['breakfast_calories'], 210)
            actual = self.assert_cumulative()
            self.assertEqual(data['dashboard'], routes.CalorieCalculator().calc_dashboard(actual))
            self.assertEqual(data['record']['cumulative_deficit'],
                             next(r['cumulative_deficit'] for r in actual if r['id'] == day))
        with self.scope() as s:
            created = repo.get_record(s, '2020-01-05')['created_at']
        again = self.client.post('/calorie/api/record', json={'date': '2020-01-05'})
        self.assertEqual(again.get_json()['record']['created_at'], created)
        self.assert_cumulative()
        result = self.client.delete('/calorie/api/record/2020-01-05')
        self.assertEqual(result.status_code, 200)
        self.assert_cumulative()
        self.assertEqual(self.client.delete('/calorie/api/record/missing').status_code, 404)
        result = self.client.put('/calorie/api/config', json={'height': 180, 'target_deficit': 5000})
        self.assertEqual(result.status_code, 200)
        actual = self.assert_cumulative()
        calculator = routes.CalorieCalculator(result.get_json()['config'])
        for record in actual:
            self.assertEqual(record['bmr'], calculator.calc_bmr(record['morning_weight']))
        self.assertEqual(result.get_json()['dashboard'], calculator.calc_dashboard(actual))

    def test_delete_and_config_do_not_read_meal_items(self):
        self.seed(3)
        for method, path in ((self.client.delete, '/calorie/api/record/2020-01-01'),
                             (self.client.put, '/calorie/api/config')):
            self.statements.clear()
            response = method(path, json={'height': 175})
            self.assertEqual(response.status_code, 200)
            reads = [q for q in self.statements if q.lstrip().upper().startswith('SELECT')]
            self.assertFalse(any('calorie_meal_items' in q for q in reads))
            self.assertFalse(any('breakfast_food,' in q for q in reads))

    def test_config_noop_has_no_record_update(self):
        self.seed(3)
        self.client.put('/calorie/api/config', json={'height': 180})
        self.statements.clear()
        self.client.put('/calorie/api/config', json={'height': 180})
        self.assertFalse(any(q.upper().startswith('UPDATE CALORIE_RECORDS') for q in self.statements))

    def test_rollback_restores_parent_children_and_config(self):
        self.seed(2)
        with self.scope() as s:
            before = repo.load_records(s)
        with patch.object(repo, 'add_missing_foods', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                self.client.post('/calorie/api/record', json={'date': '2020-01-01'})
        with self.scope() as s:
            self.assertEqual(repo.load_records(s), before)

    def test_food_seed_batch_and_crud(self):
        with measure() as metrics:
            response = self.client.get('/calorie/api/foods')
        self.assertEqual(response.status_code, 200)
        self.assertLess(metrics.sql_count, 10)
        with self.scope() as s:
            added = repo.add_missing_foods(s, ['a', 'a', '', 'b'])
            self.assertEqual([f['name'] for f in added], ['a', 'b'])
            with measure() as known:
                self.assertEqual(repo.add_missing_foods(s, ['a', 'b']), [])
            self.assertEqual(known.sql_count, 1)
        data = self.client.post('/calorie/api/food', json={'name': '测试食物'}).get_json()
        fid = data['food']['id']
        self.assertEqual(self.client.post('/calorie/api/food', json={'name': '测试食物'}).status_code, 409)
        self.assertEqual(self.client.put('/calorie/api/food/' + fid,
                                        json={'calories': 99}).get_json()['food']['calories'], 99)
        self.assertTrue(self.client.delete('/calorie/api/food/' + fid).get_json()['success'])

    def test_market_ids_projection_sort(self):
        with self.scope() as s:
            for rank, inst in [('10', ' B '), ('2', 'A'), ('x', 'C'), ('1', ''), ('2', 'D')]:
                s.add(CryptoCoin(rank_no=rank, inst_id=inst))
        with self.scope() as s:
            expected = [r['inst_id'].strip() for r in market_data_repo.load_coin_rows(s)
                        if r['inst_id'].strip()]
            self.statements.clear()
            self.assertEqual(market_data_repo.coin_inst_ids(s), expected)
        self.assertNotIn('h1_', self.statements[0])

    def test_metrics_nested_error_and_commit(self):
        with measure() as outer:
            with self.engine.connect() as conn:
                conn.execute(text('SELECT 1'))
            with measure() as inner:
                with self.engine.connect() as conn:
                    with self.assertRaises(Exception):
                        conn.execute(text('SELECT nonexistent_column'))
            with patch.object(database, 'get_session', self.factory):
                with database.session_scope() as s:
                    s.execute(text('SELECT 2'))
        self.assertEqual(outer.sql_count, 2)
        self.assertEqual(inner.sql_errors, 1)
        self.assertEqual(outer.acquire_count, 2)
        self.assertGreater(outer.connection_ms, 0)
        self.assertGreater(outer.commit_ms, 0)
        self.assertGreater(outer.request_ms, 0)

    def test_request_metrics_no_sensitive_log(self):
        with patch.dict('os.environ', {'CRYPTO_DB_PERF': '1'}):
            with self.assertLogs('crypto.db_performance', level='INFO') as captured:
                self.client.get('/calorie/api/config?secret=private')
        payload = json.loads(captured.output[-1].split('[DBPerf] ')[1])
        self.assertEqual(payload['route'], '/calorie/api/config')
        self.assertEqual(payload['sql_count'], 1)
        self.assertNotIn('private', str(payload))
        with measure() as metrics:
            with self.engine.connect() as conn:
                conn.execute(text('SELECT 1'))
        self.assertEqual(metrics.sql_count, 1)


    def _cached_read_sqls(self):
        """统计一次 load_json_config_cached 直读库的 SELECT 条数（须串行调用，
        SQLite 内存库在同一时刻只允许一个连接，否则新连接是空库）。"""
        self.statements.clear()
        with patch.object(database, 'session_scope', self.scope):
            configs.load_json_config_cached(self.cache_probe_key)
        return len([q for q in self.statements if q.lstrip().upper().startswith('SELECT')])

    def test_cache_invalidates_only_after_commit(self):
        """写事务未提交前不得失效缓存；提交后失效，回滚保持旧值。"""
        key = 'coin_selection'
        with patch.object(database, 'session_scope', self.scope):
            with self.factory() as s:
                configs.save_json_config(s, key, {'v': 1})
                s.commit()
            self.assertEqual(configs.load_json_config_cached(key), {'v': 1})

            with self.factory() as s:
                configs.save_json_config(s, key, {'v': 2})
                # 新值尚未提交，缓存仍是旧值（提前失效会缓存到读不到的脏数据）
                self.assertEqual(configs.load_json_config_cached(key), {'v': 1})
                s.commit()
            self.assertEqual(configs.load_json_config_cached(key), {'v': 2})

            with self.factory() as s:
                configs.save_json_config(s, key, {'v': 3})
                s.rollback()
            self.assertEqual(configs.load_json_config_cached(key), {'v': 2})

    def test_cache_savepoint_defers_and_discards_invalidation(self):
        """savepoint 提交：失效推迟到外层事务；savepoint 回滚：失效作废。"""
        key = 'coin_selection'
        with patch.object(database, 'session_scope', self.scope):
            with self.factory() as s:
                configs.save_json_config(s, key, {'v': 1})
                s.commit()
            self.assertEqual(configs.load_json_config_cached(key), {'v': 1})

            with self.factory() as s:
                generation = configs._cache_generation
                sp = s.begin_nested()
                configs.save_json_config(s, key, {'v': 9})
                sp.commit()
                self.assertEqual(configs._cache_generation, generation)   # 推迟
                self.assertEqual(configs.load_json_config_cached(key), {'v': 1})
                s.commit()
                self.assertEqual(configs._cache_generation, generation + 1)  # 外层提交才失效
            self.assertEqual(configs.load_json_config_cached(key), {'v': 9})

            with self.factory() as s:
                generation = configs._cache_generation
                sp2 = s.begin_nested()
                configs.save_json_config(s, key, {'v': 10})
                sp2.rollback()
                s.commit()
                self.assertEqual(configs._cache_generation, generation)   # 回滚作废
            self.assertEqual(configs.load_json_config_cached(key), {'v': 9})

    def test_critical_keys_bypass_ttl_cache(self):
        """关键键（运行状态/账号策略/纪律配置）绕过 TTL 缓存直读库。"""
        self.assertTrue(configs._critical_key(configs.KEY_TRADING_RUNTIME))
        self.assertTrue(configs._critical_key(configs.KEY_STRATEGY_CONFIG))
        self.assertTrue(configs._critical_key(configs.strategy_config_key('main')))
        self.assertTrue(configs._critical_key(discipline.KEY_DISCIPLINE_CONFIG))
        self.assertTrue(configs._critical_key(discipline.KEY_DISCIPLINE_STATE))
        self.assertFalse(configs._critical_key('coin_selection'))

        self.cache_probe_key = configs.KEY_TRADING_RUNTIME
        # 关键键每次都回源：外部绕过 repo 直改库后立即可见，不受 TTL 影响
        self.assertEqual(self._cached_read_sqls(), 1)
        with self.factory() as s:
            s.merge(KVStore(key=configs.KEY_TRADING_RUNTIME,
                            value=json.dumps({'desired_running': True})))
            s.commit()
        self.assertEqual(self._cached_read_sqls(), 1)
        with patch.object(database, 'session_scope', self.scope):
            self.assertEqual(configs.load_json_config_cached(
                configs.KEY_TRADING_RUNTIME)['desired_running'], True)
        # 非关键键第二次命中缓存，零 SQL
        self.cache_probe_key = 'coin_selection'
        with patch.object(database, 'session_scope', self.scope):
            configs.invalidate_config_cache('coin_selection')
            self.assertEqual(self._cached_read_sqls(), 1)
            self.assertEqual(self._cached_read_sqls(), 0)

    def test_generation_barrier_blocks_stale_cache_fill(self):
        """读取期间发生失效时，不得把晚到的旧快照回填进缓存。"""
        key = 'market_scan_cache'
        real_load = configs.load_json_config

        def load_then_invalidate(session, k):
            data = real_load(session, k)
            configs.invalidate_config_cache(key)
            return data

        configs.load_json_config = load_then_invalidate
        try:
            with patch.object(database, 'session_scope', self.scope):
                self.assertIsNone(configs.load_json_config_cached(key))
        finally:
            configs.load_json_config = real_load
        self.assertNotIn(key, configs._kv_cache)

    def test_discipline_status_reads_each_source_once(self):
        """build_status 请求内复用配置/台账/记录，当前槽不重复判定。"""
        now = datetime.datetime(2026, 9, 17, 10, 30)
        slot = discipline.hour_slot_of(now)
        day = now.strftime('%Y-%m-%d')
        with self.scope() as s:
            configs.save_json_config(s, discipline.KEY_DISCIPLINE_CONFIG,
                                     {'required_count': 1, 'grace_minutes': 15})
            configs.save_json_config(s, discipline.KEY_DISCIPLINE_STATE, {})
            configs.save_json_config(s, configs.KEY_TRADING_RUNTIME, {'account': 'main'})
            configs.save_json_config(s, configs.strategy_config_key('main'),
                                     {'currencies': [{'instId': 'BTC-USDT'}]})
            s.add(TaskAnalysisRecord(id=1, ts=now.strftime('%Y-%m-%d %H:%M:%S'),
                                     inst_id='BTC-USDT', hour_slot=slot, source='live'))
            s.add(AnalysisReminderLog(id=1, hour_slot=f'{slot[:11]}09', stat_date=day,
                                      required_count=1, actual_count=1,
                                      status=discipline.ST_SATISFIED))

        def selects(table):
            return len([q for q in self.statements
                        if table in q and q.lstrip().upper().startswith('SELECT')])

        with self.scope() as s:
            self.statements.clear()
            payload = discipline.build_status(s, now=now)
            reads = {table: selects(table) for table in ('kv_store', 'task_analysis_records')}

        with patch.object(database, 'session_scope', self.scope):
            cfg = discipline.load_config()
        slots = [x for x in discipline.day_slots(day, cfg) if x <= slot]
        self.assertEqual(reads['kv_store'], 4)          # 配置/状态/运行状态/策略配置
        self.assertEqual(reads['task_analysis_records'], 1)  # 当前槽只判定一次
        self.assertEqual(payload['hour_slot'], slot)
        self.assertEqual(payload['actual'], 1)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['today']['slots'], len(slots))
        self.assertEqual(payload['today']['ok'], 2)        # 09 台账合格 + 10 当前槽合格
        self.assertEqual(payload['today']['pending'], 1)   # 08 台账未覆盖，不计入分母
        self.assertEqual(payload['today']['missing'], 0)
        self.assertEqual(payload['tracked'], ['BTC-USDT'])
        # 对照：拆散调用会把当前槽的分析记录读两遍
        with self.scope() as s:
            self.statements.clear()
            naive_cfg = discipline.load_config(s)
            discipline.evaluate_slot(s, slot, naive_cfg)
            discipline.today_summary(s, day, naive_cfg, now)
            self.assertEqual(selects('task_analysis_records'), 2)

    def test_trading_runtime_atomic_merge(self):
        """启停与自动恢复开关在同一事务锁内合并，互不覆盖。"""
        with patch.object(runtime, 'session_scope', self.scope):
            with self.factory() as s:
                configs.save_json_config(s, configs.KEY_TRADING_RUNTIME, {
                    'desired_running': False, 'account': 'old', 'auto_resume': False})
                s.commit()

            self.assertTrue(runtime.set_desired_running(True, 'main', 'manual_start'))
            latest = runtime.load_runtime()
            self.assertTrue(latest['desired_running'])
            self.assertEqual(latest['account'], 'main')
            self.assertFalse(latest['auto_resume'])

            toggled = runtime.set_auto_resume(True)
            self.assertTrue(toggled['auto_resume'])
            self.assertTrue(toggled['desired_running'])
            self.assertEqual(toggled['account'], 'main')

            # 停止路径不带账号：账号与开关都保留，只改运行期望
            runtime.set_desired_running(False)
            stopped = runtime.load_runtime()
            self.assertFalse(stopped['desired_running'])
            self.assertEqual(stopped['account'], 'main')
            self.assertTrue(stopped['auto_resume'])

            # 记账失败只降级为日志，绝不向启停主流程抛异常
            with patch.object(configs, 'patch_json_config',
                              side_effect=RuntimeError('db down')):
                self.assertFalse(runtime.set_desired_running(True, 'main'))
            self.assertFalse(runtime.load_runtime()['desired_running'])

    def test_balance_points_time_range_and_projection(self):
        """余额点按 [start,end) 半开区间取，且只读三列窄投影。"""
        with self.scope() as s:
            for idx in range(5):
                s.add(BalanceHistory(account_key='main', ts=idx * 1000,
                                     balance=idx + 0.5,
                                     source='backfill' if idx == 2 else 'snapshot'))
        with self.scope() as s, measure() as metrics:
            points = balance_repo.load_account_points(s, 'main', 1000, 3000)
        self.assertEqual(points, [{'ts': 1000, 'balance': 1.5},
                                  {'ts': 2000, 'balance': 2.5, 'source': 'backfill'}])
        self.assertEqual(metrics.sql_count, 1)
        self.assertIn('SELECT balance_history.ts', self.statements[-1])
        self.assertNotIn('balance_history.balance, balance_history.source, balance_history.account_key',
                         self.statements[-1])
        with self.scope() as s:
            self.assertEqual([p['ts'] for p in balance_repo.load_account_points(s, 'main')],
                             [0, 1000, 2000, 3000, 4000])
            self.assertEqual(balance_repo.load_account_points(s, 'main', end_ms=1000),
                             [{'ts': 0, 'balance': 0.5}])
            self.assertEqual(balance_repo.load_account_points(s, 'main', start_ms=4000),
                             [{'ts': 4000, 'balance': 4.5}])


class _PlanTreeFixture(unittest.TestCase):
    """任务计划族共用夹具：内存 SQLite 建 plan 三表 + 最小 Flask 蓝图应用。

    只注册 plan_bp 并替换 session_scope，不加载真 app —— 因此不会触发启动
    副作用（预热迁移、调度、真实行情/交易调用）。子类各自实现 `_seed`。
    """

    def setUp(self):
        configs.invalidate_config_cache()
        self.engine = create_engine('sqlite://', poolclass=TimedQueuePool)
        instrument_engine(self.engine)
        for table in [PlanPlan.__table__, PlanCard.__table__, PlanSlot.__table__, KVStore.__table__]:
            table.create(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.register_blueprint(plan_routes.plan_bp)

        @contextlib.contextmanager
        def scope():
            with self.factory.begin() as session:
                yield session
        self.scope = scope
        self.patches = [patch.object(plan_routes, 'session_scope', scope),
                        patch.object(database, 'session_scope', scope)]
        for p in self.patches:
            p.start()
        self.client = self.app.test_client()
        self.statements = []

        @event.listens_for(self.engine, 'before_cursor_execute')
        def record(conn, cursor, statement, params, context, many):
            self.statements.append(statement)

        self._seed()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.engine.dispose()

    def _seed(self):
        raise NotImplementedError


class _PlanTreeSeed:
    """2 个计划、5 张卡（含未迁移旧卡），每卡 100 格并写入长文本

    刻意让「已迁移卡」同时保留 milestones/todos 旧列归档与 notes/review
    长文本：读路径若错把旧列当任务树读、写路径若把窄投影当整树保存，
    都会立刻在"与整树读法逐字段相等"的断言上暴露。
    """

    def _seed(self):
        long_text = '行情分析' * 60
        with self.scope() as s:
            s.merge(KVStore(key='task_plans_initialized', value='true'))
            s.add(PlanPlan(id='plan_a', sort_order=0, type='learn', name='计划A',
                           daily_rule=json.dumps({'mode': 'legacy', 'target_hours': 12})))
            s.add(PlanPlan(id='plan_b', sort_order=1, type='trade', name='计划B',
                           daily_rule=json.dumps({'mode': 'longtermism'})))
            spec = [('card_a1', 'plan_a', 0, 'learn', 'in_progress'),
                    ('card_a2', 'plan_a', 1, 'learn', 'pending'),
                    ('card_a3', 'plan_a', 2, 'trade', 'completed'),
                    ('card_a4', 'plan_a', 3, 'trade', 'pending'),
                    ('card_b1', 'plan_b', 0, 'trade', 'pending')]
            # card_a4 保持 tasks 为 NULL（旧数据）；其余卡显式写入任务树
            tasks_by_card = {
                'card_a1': [{'id': 'task_x1', 'title': '叶子A', 'estimated_minutes': 60,
                             'status': 'done', 'children': []},
                            {'id': 'task_x2', 'title': '叶子B', 'estimated_minutes': 30,
                             'status': 'done', 'children': []}],
                'card_a2': [{'id': 'task_y1', 'title': '半程', 'estimated_minutes': 45,
                             'status': 'done', 'children': []},
                            {'id': 'task_y2', 'title': '待补预估', 'estimated_minutes': 0,
                             'status': 'todo', 'children': []}],
                'card_a3': [],
                'card_b1': [{'id': 'task_z1', 'title': '唯一叶子', 'estimated_minutes': 20,
                             'status': 'todo', 'children': []}],
            }
            for cid, pid, order, ctype, status in spec:
                s.add(PlanCard(id=cid, plan_id=pid, sort_order=order, type=ctype,
                               round=order, title=cid, status=status,
                               # 旧列一律按仓储写回的口径序列化（ensure_ascii=False）：
                               # 否则"转义→非转义"的纯文本重编码会混进变更面里
                               milestones='[]' if cid != 'card_a4' else
                               json.dumps([{'content': '旧里程碑', 'done': True}],
                                          ensure_ascii=False),
                               # 已迁移卡也留着旧列归档：新读法不该再碰它
                               todos=json.dumps([{'content': '旧待办', 'done': False}],
                                                ensure_ascii=False),
                               tasks=json.dumps(tasks_by_card[cid], ensure_ascii=False)
                               if cid in tasks_by_card else None,
                               notes=json.dumps([{'content': long_text}],
                                                ensure_ascii=False),
                               review=long_text, goal=long_text))
            for cid, pid, order, ctype, status in spec:
                for idx in range(100):
                    filled = idx < (40 if ctype == 'learn' else 25)
                    marked = filled and ctype == 'trade'
                    # 日期跨 9 个月并留断档：连续在场 / 在场日历都会走到分支
                    # 正文只写在有记录的格子上：空格留残留文本是脏数据形态，
                    # 仓储写回时必然清掉它，会盖掉本批要验证的"变更面"信号
                    s.add(PlanSlot(card_id=cid, slot_index=idx, filled=filled,
                                   filled_at=(f'2026-{(idx % 9) + 1:02d}-'
                                              f'{(idx % 28) + 1:02d} 10:00:00')
                                   if filled else '',
                                   has_record=filled,
                                   content='学习内容' if filled and ctype == 'learn' else '',
                                   duration_minutes=60 if filled else 0,
                                   prediction='up' if marked else '',
                                   actual='up' if marked else '',
                                   hit=True if marked else None,
                                   market_analysis=long_text if marked else '',
                                   action_advice='建议' if marked else '',
                                   # 分析关联：回填打卡正文时必须原样保留（列值口径）
                                   analysis_ids='11,12' if marked else '',
                                   analysis_hour='2026-01-01 10' if marked else '',
                                   # 任务关联留空：幽灵 task_id 会让 update-slot 的
                                   # 严格校验先报 400，掩盖本批要验证的写面
                                   task_links='[]'))


class PlanCardViewTests(_PlanTreeSeed, _PlanTreeFixture):
    """任务计划读路径窄投影回归：单卡详情 / 列表 / 今日统计。

    判据统一为「与整树读法逐字段相等」+「不读未使用的重列」，
    前者保证功能与响应契约零变化，后者保证优化真的落到了 SQL 上。
    """

    def _legacy_detail(self, plan_id, card_id):
        with self.scope() as s:
            data = plan_routes._load_plans(s)
            plan = next(p for p in data['plans'] if p['id'] == plan_id)
            card = next(c for c in plan['cards'] if c['id'] == card_id)
            return plan_routes._build_card_detail(plan, card)

    def test_card_detail_matches_whole_tree_read(self):
        for card_id in ('card_a1', 'card_a2', 'card_a3', 'card_a4'):
            self.statements.clear()
            response = self.client.get(f'/plan/api/card-detail?plan_id=plan_a&card_id={card_id}')
            body = response.get_json()
            request_reads = list(self.statements)
            self.assertEqual(body['code'], 200)
            self.assertEqual(body['data'], self._legacy_detail('plan_a', card_id))
            slot_reads = [q for q in request_reads
                          if 'plan_slots' in q and q.lstrip().upper().startswith('SELECT')]
            self.assertEqual(len(slot_reads), 1)
            self.assertIn('card_id', slot_reads[0])
        # card_a4 是未迁移旧卡：任务树由 milestones + todos 现场推导，两条路径都要推出来
        with self.scope() as s:
            view = plan_repo.load_card_view(s, 'plan_a', 'card_a4')
            plan_routes._normalize_view_plan(view['plan'], view['card'])
            detail = plan_routes._build_card_detail(view['plan'], view['card'])
        self.assertEqual([t['title'] for t in detail['tasks']], ['旧里程碑', '旧待办'])
        # 兄弟卡只带锁定判定所需四列，杜绝误当整卡数据使用
        with self.scope() as s:
            view = plan_repo.load_card_view(s, 'plan_a', 'card_a1')
        self.assertEqual(set(view['plan']['cards'][1]), {'id', 'type', 'round', 'status'})
        self.assertIs(view['plan']['cards'][0], view['card'])

    def test_card_detail_does_not_load_other_cards_text(self):
        self.statements.clear()
        self.client.get('/plan/api/card-detail?plan_id=plan_a&card_id=card_a1')
        card_reads = [q for q in self.statements
                      if 'plan_cards' in q and q.lstrip().upper().startswith('SELECT')]
        summary = [q for q in card_reads if 'plan_cards.milestones' not in q]
        self.assertTrue(summary)
        self.assertFalse(any('plan_slots' in q and 'card_id' not in q for q in self.statements))
        self.assertLess(len(self.statements), 6)

    def test_card_detail_not_found_semantics(self):
        body = self.client.get('/plan/api/card-detail?plan_id=nope&card_id=card_a1').get_json()
        self.assertEqual(body['code'], 404)
        self.assertEqual(body['message'], '计划不存在')
        # 卡属于别的计划 → 与原实现一致报「任务卡不存在」
        body = self.client.get('/plan/api/card-detail?plan_id=plan_b&card_id=card_a1').get_json()
        self.assertEqual(body['code'], 404)
        self.assertEqual(body['message'], '任务卡不存在')
        body = self.client.get('/plan/api/card-detail?plan_id=plan_a').get_json()
        self.assertEqual(body['code'], 400)

    # ------------------------------------------------------------------
    # 列表 / 今日统计：窄投影整树必须与整树读法逐字段相等
    # ------------------------------------------------------------------
    def _legacy_built(self, kind):
        """用整树读法构造同一份响应，作为等价性基准"""
        with self.scope() as s:
            data = plan_routes._load_plans(s)
            plans = data['plans']
            if kind == 'list':
                return [plan_routes._build_plan_info(p) for p in plans]
            return [plan_routes._build_today_entry(p) for p in plans]

    def test_list_matches_whole_tree_read(self):
        self.statements.clear()
        body = self.client.get('/plan/api/list').get_json()
        request_reads = list(self.statements)
        self.assertEqual(body['code'], 200)
        self.assertEqual(body['data'], self._legacy_built('list'))
        selects = [q for q in request_reads if q.lstrip().upper().startswith('SELECT')]
        # kv + plans + cards + slots + 未迁移卡补读 = 5 条，与卡数量无关
        self.assertLessEqual(len(selects), 5, selects)

    def test_today_status_matches_whole_tree_read(self):
        self.statements.clear()
        body = self.client.get('/plan/api/today-status').get_json()
        request_reads = list(self.statements)
        self.assertEqual(body['code'], 200)
        self.assertEqual(body['data'], self._legacy_built('today'))
        slot_reads = [q for q in request_reads
                      if 'plan_slots' in q and q.lstrip().upper().startswith('SELECT')]
        self.assertEqual(len(slot_reads), 1)

    def test_summary_read_never_touches_long_text_columns(self):
        for endpoint in ('list', 'today-status'):
            self.statements.clear()
            self.client.get(f'/plan/api/{endpoint}')
            selects = [q for q in self.statements
                       if q.lstrip().upper().startswith('SELECT')]
            slot_q = [q for q in selects if 'plan_slots' in q]
            self.assertTrue(slot_q, endpoint)
            for q in slot_q:
                for heavy in ('plan_slots.content', 'plan_slots.market_analysis',
                              'plan_slots.action_advice', 'plan_slots.task_links',
                              'plan_slots.prediction', 'plan_slots.account_balance',
                              'plan_slots.analysis_ids'):
                    self.assertNotIn(heavy, q, f'{endpoint} 不应读取 {heavy}')
            card_q = [q for q in selects if 'plan_cards' in q]
            for q in card_q:
                for heavy in ('plan_cards.notes', 'plan_cards.review', 'plan_cards.goal'):
                    self.assertNotIn(heavy, q, f'{endpoint} 不应读取 {heavy}')
            # milestones/todos 只允许出现在「未迁移卡补读」那一条批量查询里
            legacy_q = [q for q in card_q if 'plan_cards.milestones' in q]
            self.assertEqual(len(legacy_q), 1, card_q)
            self.assertIn('IN', legacy_q[0].upper())

    def test_summary_tree_backfills_legacy_columns_only_for_unmigrated(self):
        """已迁移卡不补读旧列，未迁移卡才带 milestones/todos（推导任务树用）"""
        with self.scope() as s:
            data = plan_repo.load_summary_tree(s)
        cards = {c['id']: c for p in data['plans'] for c in p['cards']}
        self.assertNotIn('milestones', cards['card_a1'])
        self.assertNotIn('notes', cards['card_a1'])
        self.assertNotIn('goal', cards['card_a1'])
        self.assertEqual(cards['card_a1']['tasks'][0]['title'], '叶子A')
        # card_a4 是旧数据：由 milestones 现场推导，且 id 必须与整树读法一致
        self.assertIn('milestones', cards['card_a4'])
        plan_routes._migrate_tasks(data)
        derived = plan_routes._calc_task_progress(cards['card_a4'])
        with self.scope() as s:
            whole = plan_routes._load_plans(s)
        legacy_card = next(c for p in whole['plans'] for c in p['cards']
                           if c['id'] == 'card_a4')
        self.assertEqual(derived, plan_routes._calc_task_progress(legacy_card))
        self.assertEqual([t['id'] for t in cards['card_a4']['tasks']],
                         [t['id'] for t in legacy_card['tasks']])

    # ------------------------------------------------------------------
    # 写路径：同一 session 的"读整树 → 改 → 存整树"不得再读第二遍全树
    # ------------------------------------------------------------------
    def test_whole_tree_save_reuses_loaded_rows(self):
        with self.scope() as s:
            data = plan_routes._load_plans(s)
            target = data['plans'][0]['cards'][0]['slots'][7]
            target['filled'] = True
            target['filled_at'] = '2026-09-17 09:30'
            target['record'] = {'content': '补录', 'duration_minutes': 15,
                                'task_links': []}
            self.statements.clear()
            plan_repo.save_plans_data(s, data)
            writes = list(self.statements)
        selects = [q for q in writes if q.lstrip().upper().startswith('SELECT')]
        # 全表预取三条（plans/cards/slots 无 WHERE）必须全部省掉
        self.assertFalse([q for q in selects
                          if 'FROM plan_slots' in q and 'WHERE' not in q.upper()], selects)
        self.assertFalse([q for q in selects
                          if 'FROM plan_cards' in q and 'WHERE' not in q.upper()], selects)
        self.assertFalse([q for q in selects
                          if 'FROM plan_plans' in q and 'WHERE' not in q.upper()], selects)
        # 差量写入：只应看到被改的那一个格子的 UPDATE
        slot_updates = [q for q in writes if 'UPDATE plan_slots' in q]
        self.assertTrue(slot_updates, writes)
        with self.scope() as s:
            row = s.get(PlanSlot, ('card_a1', 7))
            self.assertTrue(row.filled)
            self.assertEqual(row.filled_at, '2026-09-17 09:30')
            self.assertEqual(row.content, '补录')
            self.assertEqual(row.duration_minutes, 15)
            # 其余格子不得被顺带改写
            self.assertEqual(s.get(PlanSlot, ('card_a1', 8)).filled, True)
            self.assertEqual(s.get(PlanSlot, ('card_a1', 99)).filled, False)

    def test_second_save_without_reload_falls_back_to_full_read(self):
        """登记表在写入后作废：同 session 再存一次必须重新读全表，且结果幂等"""
        with self.scope() as s:
            data = plan_routes._load_plans(s)
            plan_repo.save_plans_data(s, data)
            self.statements.clear()
            plan_repo.save_plans_data(s, data)
            second = list(self.statements)
        full_reads = [q for q in second if q.lstrip().upper().startswith('SELECT')
                      and 'WHERE' not in q.upper()
                      and any(t in q for t in ('plan_slots', 'plan_cards', 'plan_plans'))]
        self.assertEqual(len(full_reads), 3, second)
        # 第二次没有任何数据变化：不应产生 UPDATE/INSERT/DELETE
        self.assertFalse([q for q in second
                          if q.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE'))],
                         second)


class PlanLocalWriteTests(_PlanTreeSeed, _PlanTreeFixture):
    """打卡族局部读写回归：响应必须等于整树重读结果，写面必须收敛到被改的行。

    三条判据缺一不可：
    - refresh 载荷逐字段等于「写后重读整树」构造的结果 → 前端局部刷新语义不变，
      局部写没漏列、没写脏，也不靠内存里那半棵树自说自话；
    - 数据库变更行/列集合恰好等于预期 → 没有顺手改写兄弟卡长文本、没删多余格；
    - SQL 形状：不再出现无 WHERE 的全表扫描，重列只在目标卡那一条查询里出现。
    """

    FIXED_NOW = '2026-09-17 10:11:12'
    _MODELS = ((PlanPlan, lambda r: r.id), (PlanCard, lambda r: r.id),
               (PlanSlot, lambda r: (r.card_id, r.slot_index)))

    def _seed(self):
        super()._seed()
        # 交易卡 card_a3 设为进行中：局部写用例要覆盖交易打卡与结算解锁
        with self.scope() as s:
            s.get(PlanCard, 'card_a3').status = 'in_progress'

    # ------------------------------------------------------------------
    # 断言工具
    # ------------------------------------------------------------------
    def _snapshot(self):
        out = {}
        with self.scope() as s:
            for model, pk in self._MODELS:
                cols = [c.name for c in model.__table__.columns]
                out[model.__tablename__] = {
                    pk(r): {c: getattr(r, c) for c in cols}
                    for r in s.execute(select(model)).scalars().all()}
        return out

    def _diff(self, before, after):
        """{表名: {主键: [变化列]}} —— 只报真实差异，行增删单独标记"""
        changed = {}
        for table, rows in after.items():
            old_rows = before[table]
            for pk, vals in rows.items():
                old = old_rows.get(pk)
                if old is None:
                    changed.setdefault(table, {})[pk] = ['<insert>']
                    continue
                dirty = sorted(c for c, v in vals.items() if old.get(c) != v)
                if dirty:
                    changed.setdefault(table, {})[pk] = dirty
            for pk in old_rows:
                if pk not in rows:
                    changed.setdefault(table, {})[pk] = ['<delete>']
        return changed

    def _assert_changed(self, changed, expected):
        """逐项比对，失败信息直接指出"多改了哪一行 / 哪一列变了"，不印整页字典"""
        got = {t: {str(pk): sorted(v) for pk, v in rows.items()}
               for t, rows in changed.items()}
        want = {t: {str(pk): sorted(v) for pk, v in rows.items()}
                for t, rows in expected.items()}
        self.assertEqual(
            sorted((t, pk) for t, rows in got.items() for pk in rows),
            sorted((t, pk) for t, rows in want.items() for pk in rows),
            '变更行集合与预期不符')
        for table, rows in want.items():
            for pk, cols in rows.items():
                self.assertEqual(got[table][pk], cols, f'{table} {pk} 变更列不符')

    def _post(self, url, payload, extra=None):
        """固定时钟发起写请求：start_time/settled_at 一类时间列才可逐字段比对"""
        self.statements.clear()
        with patch.object(plan_routes, '_now_str', lambda: self.FIXED_NOW), \
                (extra or contextlib.nullcontext()):
            body = self.client.post(url, json=payload).get_json()
        self.request_sql = list(self.statements)
        return body

    def _refresh_from_whole_tree(self, plan_id, card_id):
        """写后重读整树构造局部刷新载荷 —— 打卡回传的等价性基准"""
        with self.scope() as s:
            data = plan_routes._load_plans(s)
            plan = next(p for p in data['plans'] if p['id'] == plan_id)
            card = next(c for c in plan['cards'] if c['id'] == card_id)
            return {
                'plan': plan_routes._build_plan_info(plan),
                'today': plan_routes._build_today_entry(plan),
                'card': plan_routes._build_card_detail(plan, card),
            }

    def _assert_refresh_matches_whole_tree(self, body, plan_id, card_id):
        self.assertEqual(body['code'], 200, body)
        self.assertEqual(body['data']['refresh'],
                         self._refresh_from_whole_tree(plan_id, card_id))

    def _assert_no_full_tree_scan(self):
        scans = [q for q in self.request_sql
                 if q.lstrip().upper().startswith('SELECT')
                 and 'WHERE' not in q.upper()
                 and any(t in q for t in ('plan_slots', 'plan_cards', 'plan_plans'))]
        self.assertEqual(scans, [], self.request_sql)

    def _assert_heavy_columns_scoped_to_target(self):
        """全字段格子读只允许一条，且必须按 card_id 过滤 —— 否则就是在读兄弟卡"""
        heavy = [q for q in self.request_sql
                 if 'plan_slots' in q and q.lstrip().upper().startswith('SELECT')
                 and 'plan_slots.market_analysis' in q]
        self.assertEqual(len(heavy), 1, self.request_sql)
        self.assertIn('WHERE plan_slots.card_id', ' '.join(heavy[0].split()))

    # ------------------------------------------------------------------
    # 打卡 / 改格子 / 撤销
    # ------------------------------------------------------------------
    def test_fill_learn_slot_writes_only_target_slot_and_card(self):
        before = self._snapshot()
        body = self._post('/plan/api/fill-slot', {
            'plan_id': 'plan_a', 'card_id': 'card_a1', 'slot_index': 40,
            'filled_at': '2026-09-17 09:30',
            'record': {'content': '补一段', 'duration_minutes': 45, 'task_links': []}})
        self._assert_refresh_matches_whole_tree(body, 'plan_a', 'card_a1')
        after = self._snapshot()
        self._assert_changed(self._diff(before, after), {
            'plan_slots': {('card_a1', 40): ['content', 'duration_minutes',
                                             'filled', 'filled_at', 'has_record']},
            'plan_cards': {'card_a1': ['updated_at']},
        })
        self._assert_no_full_tree_scan()
        self._assert_heavy_columns_scoped_to_target()
        # 兄弟行结构零变化：格子没被增删，未迁移旧卡也没被顺带"迁移"
        self.assertEqual(len(after['plan_slots']), len(before['plan_slots']))
        self.assertIsNone(after['plan_cards']['card_a4']['tasks'])

    def test_fill_trade_slot_keeps_siblings_untouched(self):
        """交易卡补录过闸门后只写本格 + 本卡；兄弟卡正文一个字节都不动"""
        gate = patch.object(plan_routes, '_discipline_gate',
                            lambda session, filled_at_str='', cfg=None: (True, {}, None))
        before = self._snapshot()
        body = self._post('/plan/api/fill-slot', {
            'plan_id': 'plan_a', 'card_id': 'card_a3', 'slot_index': 25,
            'record': {'prediction': 'up', 'duration_minutes': 60, 'actual': 'up',
                       'market_analysis': '新分析', 'action_advice': '持有',
                       'account_balance': '12345.6', 'task_links': []}}, extra=gate)
        self._assert_refresh_matches_whole_tree(body, 'plan_a', 'card_a3')
        after = self._snapshot()
        self._assert_changed(self._diff(before, after), {
            'plan_slots': {('card_a3', 25): ['account_balance', 'action_advice',
                                             'actual', 'duration_minutes', 'filled',
                                             'filled_at', 'has_record', 'hit',
                                             'market_analysis', 'prediction']},
            'plan_cards': {'card_a3': ['updated_at']},
        })
        # 打卡只补 record，不碰分析关联列（闸门载荷为空时保持空）
        self.assertEqual(after['plan_slots'][('card_a3', 25)]['analysis_ids'], '')
        self._assert_no_full_tree_scan()

    def test_fill_slot_rejects_locked_card_without_any_write(self):
        before = self._snapshot()
        body = self._post('/plan/api/fill-slot', {
            'plan_id': 'plan_a', 'card_id': 'card_a2', 'slot_index': 0,
            'record': {'content': 'x', 'duration_minutes': 10}})
        self.assertEqual(body['code'], 403, body)
        self._assert_changed(self._diff(before, self._snapshot()), {})
        # 状态校验不通过：一条 UPDATE 都不该发出去
        self.assertEqual([q for q in self.request_sql
                          if q.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE'))],
                         [], self.request_sql)

    def test_fill_slot_missing_card_is_404_without_writes(self):
        before = self._snapshot()
        for payload in ({'plan_id': 'plan_a', 'card_id': 'nope', 'slot_index': 0,
                         'record': {}},
                        {'plan_id': 'nope', 'card_id': 'card_a1', 'slot_index': 0,
                         'record': {}},
                        {'plan_id': 'plan_b', 'card_id': 'card_a1', 'slot_index': 0,
                         'record': {}}):
            body = self._post('/plan/api/fill-slot', payload)
            self.assertEqual(body['code'], 404, body)
            self.assertEqual(body['message'], '卡片不存在', body)
        self._assert_changed(self._diff(before, self._snapshot()), {})

    def test_backfill_batch_writes_only_new_slots(self):
        """批量补录：三条历史条目占用三个空格子，其余 97 格一条 UPDATE 都不发"""
        before = self._snapshot()
        body = self._post('/plan/api/backfill-batch', {
            'plan_id': 'plan_a', 'card_id': 'card_a1',
            'entries': [{'filled_at': '2026-09-16 09:00',
                         'record': {'content': 'B', 'duration_minutes': 45}},
                        {'filled_at': '2026-09-15 09:00',
                         'record': {'content': 'A', 'duration_minutes': 30}},
                        {'filled_at': '2026-09-17 08:00',
                         'record': {'content': 'C', 'duration_minutes': 60}}]})
        self._assert_refresh_matches_whole_tree(body, 'plan_a', 'card_a1')
        self.assertEqual(body['data']['filled'], 3, body['data'])
        self.assertEqual(body['data']['skipped'], 0, body['data'])
        self._assert_changed(self._diff(before, self._snapshot()), {
            'plan_slots': {('card_a1', i): ['content', 'duration_minutes', 'filled',
                                            'filled_at', 'has_record']
                           for i in (40, 41, 42)},
            'plan_cards': {'card_a1': ['updated_at']},
        })
        # 按时间升序占用空格子：09-15 落在最靠前的空位
        after = self._snapshot()['plan_slots']
        self.assertEqual([after[('card_a1', i)]['filled_at'] for i in (40, 41, 42)],
                         ['2026-09-15 09:00:00', '2026-09-16 09:00:00',
                          '2026-09-17 08:00:00'])
        # 同结构的 UPDATE 会被 SQLAlchemy 合并成一次 executemany：整批只发两条写
        writes = [q for q in self.request_sql if q.lstrip().upper().startswith('UPDATE')]
        self.assertEqual([q.split()[1] for q in writes],
                         ['plan_slots', 'plan_cards'], self.request_sql)
        self._assert_no_full_tree_scan()
        self._assert_heavy_columns_scoped_to_target()

    def test_update_slot_rewrites_single_slot_only(self):
        """回填实际涨跌：表单不带 task_links 时沿用库内旧值，不抹分析关联"""
        before = self._snapshot()
        body = self._post('/plan/api/update-slot', {
            'plan_id': 'plan_a', 'card_id': 'card_a3', 'slot_index': 24,
            'record': {'prediction': 'up', 'duration_minutes': 60, 'actual': 'down',
                       'market_analysis': '行情分析' * 60, 'action_advice': '建议',
                       'account_balance': ''}})
        self._assert_refresh_matches_whole_tree(body, 'plan_a', 'card_a3')
        self._assert_changed(self._diff(before, self._snapshot()), {
            'plan_slots': {('card_a3', 24): ['actual', 'hit']},
            'plan_cards': {'card_a3': ['updated_at']},
        })
        kept = self._snapshot()['plan_slots'][('card_a3', 24)]
        self.assertEqual(kept['analysis_ids'], '11,12')
        self.assertEqual(kept['analysis_hour'], '2026-01-01 10')
        self._assert_no_full_tree_scan()

    @contextlib.contextmanager
    def _history_gate(self, mode='strict', enabled=True):
        """真实闸门 + 内存分析表，只替换配置/豁免来源，不访问业务库。"""
        TaskAnalysisRecord.__table__.create(self.engine, checkfirst=True)
        with self.scope() as s:
            for i, ts in enumerate(('2026-09-10 09:00:00', '2026-09-11 09:00:00'), 1):
                s.merge(TaskAnalysisRecord(id=i, ts=ts, hour_slot=ts[:13],
                                          inst_id='BTC-USDT-SWAP', source='backfill'))
        cfg = dict(discipline.DEFAULT_DISCIPLINE_CONFIG, strict_mode=mode,
                   enabled=enabled, active_hours='00:00-24:00')
        with patch.object(discipline, 'load_config', return_value=cfg), \
                patch.object(discipline, 'load_state', return_value={}):
            yield

    def test_history_time_validation_is_atomic(self):
        before = self._snapshot()
        for cid, index in (('card_a1', 40), ('card_a3', 25)):
            for value in ('', None, {}, 'bad', '2026-02-30 09:00', '2026-09-10 25:00'):
                with self.subTest(card=cid, value=value):
                    fill = self._post('/plan/api/fill-slot', {
                        'plan_id': 'plan_a', 'card_id': cid, 'slot_index': index,
                        'fill_mode': 'backfill', 'filled_at': value, 'record': {}})
                    edit = self._post('/plan/api/update-slot', {
                        'plan_id': 'plan_a', 'card_id': cid, 'slot_index': 0,
                        'filled_at': value})
                    self.assertEqual(fill['code'], 400, fill)
                    self.assertEqual(edit['code'], 400, edit)
            missing = self._post('/plan/api/fill-slot', {
                'plan_id': 'plan_a', 'card_id': cid, 'slot_index': index,
                'fill_mode': 'backfill'})
            self.assertEqual(missing['code'], 400)
        self._assert_changed(self._diff(before, self._snapshot()), {})

    def test_history_single_and_today_keep_timestamp_and_gate(self):
        with self._history_gate():
            for cid, index in (('card_a1', 40), ('card_a3', 25)):
                payload = {'plan_id': 'plan_a', 'card_id': cid, 'slot_index': index,
                           'filled_at': '2026-09-10 09:35:27', 'fill_mode': 'backfill',
                           'record': {'content': '历史内容', 'duration_minutes': 35,
                                      'prediction': '涨', 'actual': '涨'}}
                result = self._post('/plan/api/fill-slot', payload)
                self._assert_refresh_matches_whole_tree(result, 'plan_a', cid)
                self.assertEqual(result['data']['slot']['filled_at'], payload['filled_at'])
                if cid == 'card_a3':
                    self.assertEqual(result['data']['slot']['record']['analysis_ids'], [1])
                    self.assertEqual(result['data']['slot']['record']['analysis_hour'], '2026-09-10 09')
                self.assertEqual(self._post('/plan/api/fill-slot', payload)['code'], 400)
                payload['slot_index'] += 1
                payload.pop('filled_at')
                payload.pop('fill_mode')
                # 今日请求仍过原闸门；配置关闭只用于固定本测试的当前时刻。
                with patch.object(discipline, 'load_config', return_value={'enabled': False}):
                    result = self._post('/plan/api/fill-slot', payload)
                self.assertEqual(result['code'], 200, result)
                self.assertEqual(result['data']['slot']['filled_at'], self.FIXED_NOW)
        with self.scope() as s:
            self.assertEqual(s.get(PlanSlot, ('card_a3', 25)).filled_at, '2026-09-10 09:35:27')

    def test_history_edit_learning_syncs_task_time_without_new_slot(self):
        with self.scope() as s:
            s.get(PlanCard, 'card_a1').tasks = json.dumps([
                {'id': 'done_leaf', 'title': '学习任务', 'estimated_minutes': 60,
                 'status': 'todo', 'children': []}])
        result = self._post('/plan/api/fill-slot', {
            'plan_id': 'plan_a', 'card_id': 'card_a1', 'slot_index': 40,
            'filled_at': '2026-09-10 09:35',
            'record': {'content': '必须保留', 'duration_minutes': 45,
                       'task_links': [{'task_id': 'done_leaf', 'state': 'done'}]}})
        self.assertEqual(result['code'], 200)
        before = self._snapshot()
        result = self._post('/plan/api/update-slot', {
            'plan_id': 'plan_a', 'card_id': 'card_a1', 'slot_index': 40,
            'filled_at': '2026-09-09 08:15:37'})
        self._assert_refresh_matches_whole_tree(result, 'plan_a', 'card_a1')
        record = result['data']['slot']['record']
        self.assertEqual((record['content'], record['duration_minutes']), ('必须保留', 45))
        card = result['data']['refresh']['card']
        self.assertEqual(card['tasks'][0]['completed_at'], '2026-09-09 08:15:37')
        self.assertEqual(card['tasks'][0]['completed_by_slot'], 40)
        self.assertEqual(card['status'], 'in_progress')
        self._assert_changed(self._diff(before, self._snapshot()), {
            'plan_slots': {('card_a1', 40): ['filled_at']},
            'plan_cards': {'card_a1': ['tasks']}})
        self._assert_no_full_tree_scan()

    def test_history_edit_trade_rechecks_target_and_preserves_record(self):
        payload = {'plan_id': 'plan_a', 'card_id': 'card_a3', 'slot_index': 24}
        before = self._snapshot()
        with self._history_gate():
            denied = self._post('/plan/api/update-slot', dict(payload, filled_at='2026-09-12 09:00'))
            self.assertEqual(denied['code'], 403, denied)
            self.assertTrue(denied['data']['need_analysis'])
            self.assertEqual(denied['data']['hour_slot'], '2026-09-12 09')
            self._assert_changed(self._diff(before, self._snapshot()), {})
            result = self._post('/plan/api/update-slot', dict(payload, filled_at='2026-09-10 09:35:27',
                                record={'analysis_ids': [999], 'analysis_hour': '伪造', 'bypass_analysis': True}))
        self._assert_refresh_matches_whole_tree(result, 'plan_a', 'card_a3')
        r = result['data']['slot']['record']
        self.assertEqual(r['analysis_ids'], [1])
        self.assertEqual(r['analysis_hour'], '2026-09-10 09')
        self.assertFalse(r['bypass_analysis'])
        self.assertEqual(r['market_analysis'], '行情分析' * 60)
        self.assertEqual((r['prediction'], r['actual'], r['hit']), ('up', 'up', True))
        self._assert_changed(self._diff(before, self._snapshot()), {
            'plan_slots': {('card_a3', 24): ['analysis_hour', 'analysis_ids', 'filled_at']},
            'plan_cards': {'card_a3': ['updated_at']}})
        for mode, enabled, target, bypass, hour in (
                ('soft', True, '2026-09-12 09:00', True, '2026-09-12 09'),
                ('strict', False, '2026-09-13 09:00', False, '')):
            with self._history_gate(mode, enabled):
                result = self._post('/plan/api/update-slot', dict(payload, filled_at=target))
            self.assertEqual(result['code'], 200, result)
            r = result['data']['slot']['record']
            self.assertEqual((r['analysis_ids'], r['analysis_hour'], r['bypass_analysis']), ([], hour, bypass))

    def test_history_unchanged_time_does_not_recheck_or_round_seconds(self):
        with self.scope() as s:
            s.get(PlanSlot, ('card_a3', 24)).filled_at = '2026-09-10 09:35:27'
        with patch.object(plan_routes, '_discipline_gate') as gate:
            for fields in ({}, {'filled_at': '2026-09-10 09:35:27'}):
                result = self._post('/plan/api/update-slot', dict(
                    plan_id='plan_a', card_id='card_a3', slot_index=24,
                    record={'actual': '跌'}, **fields))
                self.assertEqual(result['code'], 200)
                self.assertEqual(result['data']['slot']['filled_at'], '2026-09-10 09:35:27')
                self.assertEqual(result['data']['slot']['record']['analysis_ids'], [11, 12])
            gate.assert_not_called()

    def test_history_batch_trade_partial_gate_and_learning_doing(self):
        for cid in ('card_a1', 'card_a3'):
            with self.scope() as s:
                s.get(PlanCard, cid).tasks = json.dumps([
                    {'id': 'leaf', 'title': '推进任务', 'estimated_minutes': 60,
                     'status': 'todo', 'children': []}])
            with self._history_gate():
                result = self._post('/plan/api/backfill-batch', {
                    'plan_id': 'plan_a', 'card_id': cid,
                    'entries': [{'filled_at': f'2026-09-{d} 09:35',
                                 'record': {'content': d, 'duration_minutes': 45,
                                            'prediction': '涨', 'actual': '跌'}} for d in ('12', '11', '10')],
                    'task_links': [{'task_id': 'leaf', 'state': 'done'}]})
                self._assert_refresh_matches_whole_tree(result, 'plan_a', cid)
                self.assertEqual(result['data']['filled'], 2 if cid == 'card_a3' else 3)
                self.assertEqual(result['data']['blocked_slots'], ['2026-09-12 09'] if cid == 'card_a3' else [])
                card = result['data']['refresh']['card']
                self.assertEqual(card['tasks'][0]['status'], 'doing')
                self.assertNotIn('completed_by_slot', card['tasks'][0])
                new_slots = card['slots'][25:27] if cid == 'card_a3' else card['slots'][40:43]
                self.assertEqual(new_slots[0]['filled_at'], '2026-09-10 09:35:00')
                self.assertEqual(new_slots[1]['filled_at'], '2026-09-11 09:35:00')
                self.assertTrue(all(s['record']['task_links'] == [{'task_id': 'leaf', 'state': 'doing'}] for s in new_slots))
                if cid == 'card_a3':
                    before = self._snapshot()
                    blocked = self._post('/plan/api/backfill-batch', {
                        'plan_id': 'plan_a', 'card_id': cid,
                        'entries': [{'filled_at': '2026-09-12 09:35'}]})
                    self.assertEqual(blocked['code'], 403)
                    self._assert_changed(self._diff(before, self._snapshot()), {})

    def test_history_batch_rejects_invalid_batch_without_partial_write(self):
        before = self._snapshot()
        for cid in ('card_a1', 'card_a3'):
            for entries in ([], [{'filled_at': '2026-09-10 09:00'}, {'filled_at': 'bad'}],
                            [{'filled_at': '2026-09-10 09:00'}] * 101):
                result = self._post('/plan/api/backfill-batch', {
                    'plan_id': 'plan_a', 'card_id': cid, 'entries': entries})
                self.assertEqual(result['code'], 400)
            result = self._post('/plan/api/backfill-batch', {
                'plan_id': 'plan_a', 'card_id': cid,
                'entries': [{'filled_at': '2026-09-10 09:00'}],
                'task_links': [{'task_id': '不存在', 'state': 'doing'}]})
            self.assertEqual(result['code'], 400)
        self._assert_changed(self._diff(before, self._snapshot()), {})

    def test_history_finished_and_locked_cards_cannot_change_time(self):
        for cid, status in (('card_a1', 'completed'), ('card_a3', 'pending')):
            with self.scope() as s:
                s.get(PlanCard, cid).status = status
            before = self._snapshot()
            for endpoint, fields in (('update-slot', {'slot_index': 0, 'filled_at': '2026-09-10 09:00'}),
                                     ('fill-slot', {'slot_index': 99, 'filled_at': '2026-09-10 09:00'}),
                                     ('backfill-batch', {'entries': [{'filled_at': '2026-09-10 09:00'}]})):
                result = self._post('/plan/api/' + endpoint, dict(plan_id='plan_a', card_id=cid, **fields))
                self.assertEqual(result['code'], 403, result)
            self._assert_changed(self._diff(before, self._snapshot()), {})

    def test_history_batch_capacity_still_settles_learning_card(self):
        with self.scope() as s:
            for index in range(40, 99):
                row = s.get(PlanSlot, ('card_a1', index))
                row.filled = True
                row.filled_at = '2026-09-09 08:00:00'
        result = self._post('/plan/api/backfill-batch', {
            'plan_id': 'plan_a', 'card_id': 'card_a1',
            'entries': [{'filled_at': '2026-09-10 09:00'}, {'filled_at': '2026-09-11 09:00'}]})
        self._assert_refresh_matches_whole_tree(result, 'plan_a', 'card_a1')
        self.assertEqual((result['data']['filled'], result['data']['skipped']), (1, 1))
        self.assertEqual(result['data']['card_status'], 'completed')
        with self.scope() as s:
            self.assertEqual(s.get(PlanCard, 'card_a2').status, 'in_progress')

    def test_unfill_slot_keeps_row_and_other_slots(self):
        before = self._snapshot()
        body = self._post('/plan/api/unfill-slot', {
            'plan_id': 'plan_a', 'card_id': 'card_a1', 'slot_index': 5})
        self._assert_refresh_matches_whole_tree(body, 'plan_a', 'card_a1')
        after = self._snapshot()
        self._assert_changed(self._diff(before, after), {
            'plan_slots': {('card_a1', 5): ['content', 'duration_minutes', 'filled',
                                            'filled_at', 'has_record']},
            'plan_cards': {'card_a1': ['updated_at']},
        })
        # 撤销是清空值，不是删行：整树差量保存的"缺格即删"在这里绝不能出现
        self.assertEqual(len(after['plan_slots']), len(before['plan_slots']))
        self.assertIn(('card_a1', 5), after['plan_slots'])
        self._assert_no_full_tree_scan()

    def test_reset_card_clears_own_card_and_only_status_of_siblings(self):
        """重置清空本卡过程数据；串行链重算若牵动兄弟卡，也只改状态列"""
        before = self._snapshot()
        body = self._post('/plan/api/reset-card', {
            'plan_id': 'plan_a', 'card_id': 'card_a3'})
        self.assertEqual(body['code'], 200, body)
        after = self._snapshot()
        diff = self._diff(before, after)
        cleared = diff.get('plan_slots', {})
        self.assertEqual(len(cleared), 25, diff)
        self.assertTrue(all(set(cols) <= {'actual', 'filled', 'filled_at', 'has_record',
                                          'hit', 'market_analysis', 'prediction',
                                          'duration_minutes', 'action_advice',
                                          'analysis_ids', 'analysis_hour'}
                            for cols in cleared.values()), diff)
        self.assertEqual(sorted(diff), ['plan_cards', 'plan_slots'], diff)
        card_changes = diff['plan_cards']
        # 只有被重置这张卡变了：兄弟卡一行都不动（串行链无需重排时零 SQL）
        self.assertEqual(sorted(card_changes), ['card_a3'], card_changes)
        self.assertEqual(sorted(card_changes['card_a3']),
                         ['notes', 'review', 'status', 'todos', 'updated_at'])
        # 清空是写值，不是删行
        self.assertEqual(len(after['plan_slots']), len(before['plan_slots']))
        # 回传体与改造前逐字段一致：过程数据清空、配置列（标题/目标/奖励）保留
        self.assertEqual(body['data']['slots'], plan_routes._create_empty_slots(100))
        self.assertEqual(body['data']['status'], 'pending')
        self.assertEqual(body['data']['settlement'], None)
        self.assertEqual(body['data']['start_time'], '')
        for field in ('notes', 'milestones', 'todos', 'tasks'):
            self.assertEqual(body['data'][field], [], f'{field} 未清空')
        self.assertEqual(body['data']['review'], '')
        for field in ('id', 'type', 'round', 'title', 'goal', 'reward'):
            self.assertEqual(body['data'][field], before['plan_cards']['card_a3'][field])

    # ------------------------------------------------------------------
    # 小记 / 子目标 / 结算 / 任务树
    # ------------------------------------------------------------------
    def test_add_note_writes_one_column_no_slots(self):
        before = self._snapshot()
        body = self._post('/plan/api/add-note', {
            'plan_id': 'plan_a', 'card_id': 'card_a1', 'content': '一句小记'})
        self.assertEqual(body['code'], 200, body)
        self._assert_changed(self._diff(before, self._snapshot()), {
            'plan_cards': {'card_a1': ['notes', 'updated_at']},
        })
        slot_updates = [q for q in self.request_sql if 'UPDATE plan_slots' in q]
        self.assertEqual(slot_updates, [], self.request_sql)

    def test_toggle_milestone_on_legacy_card_derives_tasks(self):
        """旧卡（tasks 为 NULL）切子目标：与改造前一致，落库时把推导结果一并写入"""
        before = self._snapshot()
        self.assertIsNotNone(before['plan_cards']['card_a4']['milestones'])
        body = self._post('/plan/api/toggle-milestone', {
            'plan_id': 'plan_a', 'card_id': 'card_a4', 'milestone_index': 0})
        self.assertEqual(body['code'], 200, body)
        self._assert_changed(self._diff(before, self._snapshot()), {
            'plan_cards': {'card_a4': ['milestones', 'tasks', 'updated_at']},
        })

    def test_settle_round_unlocks_next_card_with_narrow_sibling_write(self):
        """结算学习卡顺带解锁下一张卡：兄弟卡只补写状态/起始/更新三列"""
        before = self._snapshot()
        body = self._post('/plan/api/settle-round', {
            'plan_id': 'plan_a', 'card_id': 'card_a1'})
        self.assertEqual(body['code'], 200, body)
        after = self._snapshot()
        self._assert_changed(self._diff(before, after), {
            'plan_cards': {
                'card_a1': ['settlement', 'status', 'updated_at'],
                'card_a2': ['start_time', 'status', 'updated_at'],
            },
        })
        # 解锁只动三列：下一张卡的正文原样保留
        self.assertEqual(after['plan_cards']['card_a2']['notes'],
                         before['plan_cards']['card_a2']['notes'])
        self.assertEqual(after['plan_cards']['card_a2']['review'],
                         before['plan_cards']['card_a2']['review'])
        self.assertEqual(body['data'], self._card_from_whole_tree('plan_a', 'card_a1'))
        self._assert_no_full_tree_scan()

    def test_task_add_matches_whole_tree_refresh(self):
        before = self._snapshot()
        body = self._post('/plan/api/task-add', {
            'plan_id': 'plan_a', 'card_id': 'card_a1', 'title': '新叶子',
            'estimated_minutes': 25})
        self._assert_refresh_matches_whole_tree(body, 'plan_a', 'card_a1')
        self._assert_changed(self._diff(before, self._snapshot()), {
            'plan_cards': {'card_a1': ['tasks', 'updated_at']},
        })
        self.assertEqual(body['data']['task_progress']['total_count'], 3, body['data'])

    def test_task_delete_matches_whole_tree_refresh(self):
        self._post('/plan/api/task-add', {'plan_id': 'plan_a', 'card_id': 'card_a1',
                                          'title': '待删', 'estimated_minutes': 10})
        with self.scope() as s:
            tasks = json.loads(s.get(PlanCard, 'card_a1').tasks)
        victim = next(t for t in tasks if t['title'] == '待删')
        before = self._snapshot()
        body = self._post('/plan/api/task-delete', {
            'plan_id': 'plan_a', 'card_id': 'card_a1', 'task_id': victim['id']})
        self._assert_refresh_matches_whole_tree(body, 'plan_a', 'card_a1')
        # updated_at 已被上一步 task-add 写成同一固定时钟，本次只有 tasks 变
        self._assert_changed(self._diff(before, self._snapshot()), {
            'plan_cards': {'card_a1': ['tasks']},
        })

    def _card_from_whole_tree(self, plan_id, card_id):
        with self.scope() as s:
            data = plan_routes._load_plans(s)
            plan = next(p for p in data['plans'] if p['id'] == plan_id)
            return next(c for c in plan['cards'] if c['id'] == card_id)


class _StubResult:
    def __init__(self, ids):
        self._ids = list(ids)

    def scalars(self):
        return self

    def all(self):
        return list(self._ids)


class _StubSession:
    """只提供 lock_plan_rows 用到的两个能力：报方言、收语句"""

    def __init__(self, dialect_name, ids=()):
        self._dialect_name = dialect_name
        self._ids = ids
        self.statements = []

    def get_bind(self):
        return types.SimpleNamespace(dialect=types.SimpleNamespace(name=self._dialect_name))

    def execute(self, stmt):
        self.statements.append(stmt)
        return _StubResult(self._ids)


def _mysql_sql(stmt):
    return str(stmt.compile(
        dialect=_mysql.dialect(), compile_kwargs={'literal_binds': True}))


class PlanWriteLockTests(unittest.TestCase):
    """写入口计划行锁的 SQL 结构与调用次序回归。

    行锁本身在 SQLite 上无法验证（方言不支持），因此这里只锁死两件事：
    MySQL 上语句形状正确（FOR UPDATE + 按 ID 排序，防死锁），非 MySQL 上
    一条 SQL 都不发（离线环境不炸）；以及"先加锁再读树"的次序不能颠倒。
    """

    def test_mysql_lock_compiles_to_for_update_ordered_by_id(self):
        s = _StubSession('mysql', ids=['plan_1', 'plan_2'])
        got = plan_repo.lock_plan_rows(s, ['plan_2', 'plan_1', 'plan_1', ''])
        self.assertEqual(got, ['plan_1', 'plan_2'])
        self.assertEqual(len(s.statements), 1, s.statements)
        sql = _mysql_sql(s.statements[0])
        self.assertIn('FOR UPDATE', sql)
        self.assertIn('FROM plan_plans', sql)
        self.assertIn('ORDER BY plan_plans.id', sql)
        # 加锁集合必须正好是去重后的入参，且按 ID 升序（两笔事务同序才不死锁）
        self.assertIn("IN ('plan_1', 'plan_2')", sql)
        self.assertLess(sql.index('plan_1'), sql.index('plan_2'))

    def test_empty_plan_ids_locks_whole_plan_table(self):
        """结构性改写（新建/删除计划会重排全部 sort_order）锁全表"""
        s = _StubSession('mysql')
        plan_repo.lock_plan_rows(s)
        self.assertEqual(len(s.statements), 1, s.statements)
        sql = _mysql_sql(s.statements[0])
        self.assertIn('FOR UPDATE', sql)
        self.assertNotIn('WHERE', sql.upper())
        self.assertNotIn('ORDER BY', sql.upper())

    def test_sqlite_lock_is_skipped_without_any_sql(self):
        s = _StubSession('sqlite')
        self.assertEqual(plan_repo.lock_plan_rows(s, ['plan_1']), [])
        self.assertEqual(s.statements, [])

    def test_lock_is_taken_before_reading_the_tree(self):
        """顺序错了就等于没锁：读快照后再加锁，仍会拿旧树覆盖新树"""
        calls = []
        with patch.object(plan_repo, 'lock_plan_rows',
                          side_effect=lambda session, plan_ids=None:
                          calls.append(('lock', tuple(plan_ids or [])))), \
             patch.object(plan_routes, '_load_plans',
                          side_effect=lambda session: calls.append(('read',)) or {'plans': []}):
            plan_routes._load_plans_for_write(_StubSession('mysql'), 'plan_9')
        self.assertEqual(calls, [('lock', ('plan_9',)), ('read',)])
        calls.clear()
        with patch.object(plan_repo, 'lock_plan_rows',
                          side_effect=lambda session, plan_ids=None:
                          calls.append(('lock', tuple(plan_ids or [])))), \
             patch.object(plan_routes, '_load_plans',
                          side_effect=lambda session: calls.append(('read',)) or {'plans': []}):
            plan_routes._load_plans_for_write(_StubSession('mysql'))
        self.assertEqual(calls, [('lock', ()), ('read',)])


class PlanWriteEntryStaticTests(unittest.TestCase):
    """路由级不变量的静态检查（不需要数据库，专门守住最容易回退的两条）"""

    WHOLE_TREE_SAVE_ROOTS = {'_save_plans', 'save_plans_data'}
    LOCAL_WRITE_ROOTS = {'_save_view_writes', 'save_card', 'save_card_slots',
                         'save_sibling_changes'}
    SAVE_ROOTS = WHOLE_TREE_SAVE_ROOTS | LOCAL_WRITE_ROOTS
    # 局部读入口：加计划行锁，但只读「本计划投影 + 目标卡完整数据」
    LOCAL_READ_ROOTS = {'_load_card_for_write'}
    # 整树读入口：打卡族改造后只应剩下结构性操作（增删计划/卡）
    WHOLE_TREE_READ_ROOTS = {'_load_plans_for_write'}
    # 自己从零构造整树并保存的函数（空库预置默认计划）：它写回的不是调用方
    # 读到的那棵树，与「窄投影误当整树保存」无关，因此不参与反向闭包。
    SELF_CONTAINED_WRITERS = {'_create_default_plans'}
    SOURCE = ast.parse(Path(plan_routes.__file__).read_text(encoding='utf-8'))

    @classmethod
    def _called_names(cls, node):
        out = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute):
                out.add(sub.attr)
            elif isinstance(sub, ast.Name):
                out.add(sub.id)
        return out

    @classmethod
    def _functions(cls):
        return {n.name: n for n in cls.SOURCE.body if isinstance(n, ast.FunctionDef)}

    @classmethod
    def _closure(cls, funcs, roots, exclude=()):
        """反向闭包：直接或经中间函数最终调到 roots 里任一调用的函数名"""
        reached = set(roots)
        changed = True
        while changed:
            changed = False
            for name, fn in funcs.items():
                if name in reached or name in exclude:
                    continue
                if cls._called_names(fn) & reached:
                    reached.add(name)
                    changed = True
        return reached

    @classmethod
    def _writers(cls, funcs):
        return cls._closure(funcs, cls.SAVE_ROOTS, cls.SELF_CONTAINED_WRITERS)

    @classmethod
    def _lockers(cls, funcs):
        """直接调读取入口的函数：整树锁与局部锁都算（并发保护语义相同）"""
        roots = cls.WHOLE_TREE_READ_ROOTS | cls.LOCAL_READ_ROOTS
        return {n for n, fn in funcs.items() if cls._called_names(fn) & roots}

    def _routes_by_method(self):
        result = {}
        for name, fn in self._functions().items():
            methods = set()
            for dec in fn.decorator_list:
                call = dec if isinstance(dec, ast.Call) else None
                if not call:
                    continue
                for kw in call.keywords:
                    if kw.arg != 'methods':
                        continue
                    if isinstance(kw.value, ast.List):
                        methods |= {e.value for e in kw.value.elts
                                    if isinstance(e, ast.Constant)}
            for method in methods:
                result.setdefault(method, set()).add(name)
        return result

    def test_every_write_lock_entry_actually_writes_back(self):
        """拿排他锁的入口必须真的写库：只读接口锁行 = 白白串行化"""
        funcs = self._functions()
        writers = self._writers(funcs)
        lockers = self._lockers(funcs)
        self.assertGreaterEqual(len(lockers), 17, sorted(lockers))
        self.assertEqual(sorted(lockers - writers), [])

    def test_write_lock_entries_are_post_only(self):
        funcs = self._functions()
        by_method = self._routes_by_method()
        lockers = self._lockers(funcs)
        self.assertEqual(sorted(lockers & (by_method.get('GET') or set())), [])
        self.assertTrue(lockers <= (by_method.get('POST') or set()),
                        sorted(lockers - (by_method.get('POST') or set())))

    def test_local_view_is_never_saved_as_whole_tree(self):
        """局部视图的 plan.cards 是窄投影，缺 goal/notes/review/milestones 列

        一旦喂给整树差量保存，会被当成"用户把这些清空了"：不报错、不崩，
        只是打卡正文和旧列静默消失。这是本阶段最危险的误用，用调用图锁死。
        """
        funcs = self._functions()
        names = set(funcs)
        whole_writers = self._closure(funcs, self.WHOLE_TREE_SAVE_ROOTS) & names
        view_users = self._closure(funcs, {'load_plan_write_view'}) & names
        local_writers = self._closure(funcs, self.LOCAL_WRITE_ROOTS) & names
        self.assertTrue(view_users, '局部读视图无人使用')
        self.assertTrue(local_writers, '局部写入口无人使用')
        # 读过局部视图的入口，一律不得走整树保存
        self.assertEqual(sorted(view_users & whole_writers), [])
        # 且必须有局部写回：只读不写 = 打卡丢了
        self.assertEqual(sorted(view_users - local_writers - self.LOCAL_READ_ROOTS), [])
        # 局部写入口自己也不许顺带整树保存
        for name in sorted(local_writers):
            calls = self._called_names(funcs[name])
            self.assertFalse(calls & self.WHOLE_TREE_SAVE_ROOTS, name)

    def test_whole_tree_write_is_limited_to_structural_entries(self):
        """整树读法只保留给确实需要全树的结构操作，防止新路由图省事走老路"""
        funcs = self._functions()
        whole = {n for n, fn in funcs.items()
                 if self._called_names(fn) & self.WHOLE_TREE_READ_ROOTS}
        self.assertEqual(
            sorted(whole),
            ['api_create_card', 'api_create_plan', 'api_delete_card',
             'api_delete_plan'])

    def test_narrow_projection_tree_is_never_saved(self):
        """窄投影整树缺长文本列，一旦喂给整树差量保存会被当成"用户删掉了"

        这是本阶段最危险的误用：不报错、不崩，只是把打卡正文静默清空。
        """
        funcs = self._functions()
        writers = self._writers(funcs)
        # 豁免项必须真的只写自建整树，否则豁免就成了漏检口
        for exempt in sorted(self.SELF_CONTAINED_WRITERS):
            calls = self._called_names(funcs[exempt])
            self.assertTrue(calls & self.SAVE_ROOTS, exempt)
            self.assertFalse(calls & {'load_summary_tree', 'load_plans_data',
                                      'load_card_view'}, exempt)
        narrow = {n for n, fn in funcs.items()
                  if '_load_summary_plans' in self._called_names(fn)
                  or 'load_summary_tree' in self._called_names(fn)}
        self.assertTrue(narrow, '窄投影读法未被任何入口使用')
        self.assertEqual(sorted(narrow & writers), [])
        # 反向：整树保存的调用点必须来自整树读法或局部视图读法
        self.assertIn('load_summary_tree',
                      self._called_names(funcs['_load_summary_plans']))


if __name__ == '__main__':
    unittest.main()
