"""每日评级总控台（批次15）隔离验收；必须配置 CRYPTO_TEST_DB_URL，仅建打卡与依赖表。
运行：python -B -m crypto._smoke_checkin
测试目标须为空账本；不接受覆盖已有数据。SQLite 不代表 MySQL DDL/锁验收。

纪律红线（本脚本必须证明的两件事）：
- GET /checkin/api/today 在缺缓存/过期缓存时都绝不触达 OKX（打桩出口计数为 0）；
- 历史补录（含提交）全程纯本地 balance_history 差分，同样零 OKX 调用、
  且不写 trading_daily_snapshots 按日缓存。

曾经踩过的坑：checkin_routes 会延迟导入 crypto.app（账号解析）与 crypto.api_routes
（快照追加），冒烟里若让真导入发生，等于拉起整个生产 app —— 本脚本用
sys.modules 桩 + 模块属性替换把两条路都堵死，测的就是"桩没被绕开"这件事。
"""
import json
import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .test_isolation import require_isolated_test_db, TestDbNotConfigured

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
try:
    TEST_URL = require_isolated_test_db()
except TestDbNotConfigured as exc:
    print(exc)
    raise SystemExit(2)

from flask import Flask, request
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import sessionmaker
from . import checkin_routes as routes
from . import checkin_repo as repo
from . import config_store_repo
from .database import Base, _NEW_TABLE_NAMES

# ── 外部依赖桩：必须在任何路由调用之前装好 ──
_fake_api_config = types.ModuleType('api_config')
_fake_api_config.get_api_config = lambda account=None: {'account': 'smoke_acct'}
sys.modules['api_config'] = _fake_api_config

_fake_api_routes = types.ModuleType('crypto.api_routes')
_fake_api_routes._append_balance_snapshot = lambda *a, **k: None
sys.modules['crypto.api_routes'] = _fake_api_routes

OKX_CALLS = []          # 记录 (method,) 调用序列，红线断言依据
OKX_BALANCE = [{'totalEq': '0'}]
OKX_BILLS = []


def _fake_okx(account, method, **kwargs):
    OKX_CALLS.append(method)
    if method == 'get_account_balance':
        return list(OKX_BALANCE), '', None
    if method == 'get_account_bills':
        return list(OKX_BILLS), '', None
    raise AssertionError(f'冒烟不应调用其他 OKX 方法: {method}')


routes._okx_client_and_call = _fake_okx
routes._resolve_account = lambda: None   # 不导入 crypto.app
config_store_repo.load_json_config_cached = lambda key, ttl=None: None  # 走默认阈值

ENGINE = create_engine(TEST_URL)
if ENGINE.dialect.name == 'sqlite':
    @event.listens_for(ENGINE, 'connect')
    def foreign_keys(connection, record):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=15000')

_TABLE_WANTED = {'daily_checkins', 'trading_daily_snapshots',
                 'calorie_records', 'plan_plans', 'plan_cards', 'plan_slots',
                 'balance_history', 'diary_entries'}
TABLES = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_WANTED]
assert {t.name for t in TABLES} == _TABLE_WANTED, '打卡依赖表未全部接进 models'
Base.metadata.create_all(ENGINE, tables=TABLES)
FACTORY = sessionmaker(ENGINE, expire_on_commit=False)
with FACTORY() as _session:
    if any(_session.scalar(select(func.count()).select_from(t)) for t in TABLES):
        print('拒绝覆盖：隔离库相关表已有数据，请另选空测试库。')
        raise SystemExit(2)


@contextmanager
def scope():
    with FACTORY.begin() as session:
        yield session


routes.session_scope = scope

HERE = Path(__file__).resolve().parent
APP = Flask('checkin_smoke', template_folder=str(HERE / 'templates'),
            static_folder=str(HERE / 'static'))
APP.config['TESTING'] = True
APP.register_blueprint(routes.checkin_bp)


@APP.context_processor
def navigation_context():
    # 复用导航纯规则；隔离网页不加载导航写接口或查询业务配置表。
    from .navigation_service import build_default_config, resolve_active
    default = build_default_config()
    active, parent = resolve_active(default['items'], request.path, request.args.get('_ap'))
    return {'nav_default_config': default, 'nav_active_id': active,
            'nav_active_parent_id': parent, 'nav_request_path': request.path}


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _day(offset=0):
    return (datetime.now() + timedelta(days=offset)).strftime('%Y-%m-%d')


class CheckinSmoke(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        global OKX_BALANCE, OKX_BILLS
        with FACTORY.begin() as session:
            for table in reversed(TABLES):
                session.execute(table.delete())
        OKX_CALLS.clear()
        OKX_BALANCE = [{'totalEq': '0'}]
        OKX_BILLS = []
        self.client = APP.test_client()

    # ------------------------------------------------------------------ 工具
    def get(self, path):
        response = self.client.get(path)
        value = response.get_json()
        self.assertIsNotNone(value, f'{path} 未返回 JSON: {response.status_code}')
        return response.status_code, value

    def post(self, path, body=None):
        response = self.client.post(path, json=body or {})
        value = response.get_json()
        self.assertIsNotNone(value, f'{path} 未返回 JSON: {response.status_code}')
        return response.status_code, value

    def seed_calorie(self, date_str, deficit):
        from .models import CalorieRecord
        with FACTORY.begin() as session:
            session.add(CalorieRecord(id=date_str, date=date_str,
                                      calorie_deficit=deficit))

    def seed_learn_slots(self, date_str, slots, card_type='learn', card_id=None):
        """slots: [(duration_minutes, task_links 或 None)]，格子时间固定为当日 09:00

        注意：PlanPlan/PlanCard/PlanSlot 之间只有裸 FK 没有 relationship()，
        同一 flush 里 SQLAlchemy 不保证插入顺序 —— 必须分段 flush。
        """
        from .models import PlanPlan, PlanCard, PlanSlot
        plan_id = f'plan_{card_type}_{date_str}'
        cid = card_id or f'card_{card_type}_{date_str}'
        with FACTORY.begin() as session:
            if session.get(PlanPlan, plan_id) is None:
                session.add(PlanPlan(id=plan_id, name='冒烟计划', type=card_type))
                session.flush()
            if session.get(PlanCard, cid) is None:
                session.add(PlanCard(id=cid, plan_id=plan_id, type=card_type, title='冒烟卡'))
                session.flush()
            for idx, (minutes, links) in enumerate(slots):
                session.add(PlanSlot(
                    card_id=cid, slot_index=idx, filled=True, has_record=True,
                    filled_at=f'{date_str} 09:00:00', duration_minutes=minutes,
                    task_links=json.dumps(links) if links is not None else None))

    def seed_balance(self, dt, balance, account_key='smoke_acct'):
        from .models import BalanceHistory
        with FACTORY.begin() as session:
            session.add(BalanceHistory(account_key=account_key, ts=_ms(dt),
                                       balance=balance, source='smoke'))

    def seed_diary(self, date_str, content='心得', linked_from='', entry_id=None):
        from .models import DiaryEntry
        eid = entry_id or f'dentry_{date_str}_{content}'
        with FACTORY.begin() as session:
            session.add(DiaryEntry(id=eid, date=date_str, content=content,
                                   linked_from=linked_from))
        return eid

    # ------------------------------------------------- 1. 页面与模板/静态契约
    def test_01_page_and_static_contract(self):
        response = self.client.get('/checkin')   # 页面是 HTML，不是 JSON
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="checkin-page"', html)
        self.assertIn('每日评级总控台', html)

    def test_01b_templates_compile_and_bar_wired(self):
        for name in ('checkin.html', 'index.html', 'nav.html'):
            APP.jinja_env.get_template(name)   # 编译失败即抛
        index_html = (HERE / 'templates' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="checkin-bar"', index_html)
        self.assertIn('js/checkin.js', index_html)
        page_html = (HERE / 'templates' / 'checkin.html').read_text(encoding='utf-8')
        js = (HERE / 'static' / 'js' / 'checkin.js').read_text(encoding='utf-8')
        for marker in ('checkin-page', 'checkin-bar'):
            self.assertIn(marker, page_html if marker == 'checkin-page' else index_html)
            self.assertIn(marker, js)
        # JS 引用的关键元素 id 必须在页面存在
        for el_id in ('ck-date', 'ck-notice', 'ck-card-calorie', 'ck-card-trade',
                      'ck-card-learn', 'ck-trade-detail', 'ck-refresh-trade',
                      'ck-trade-actions', 'ck-learn-value', 'ck-learn-target',
                      'ck-comment', 'ck-submit', 'ck-all-badge', 'ck-hist-body',
                      'ck-goto-today', 'ck-toast',
                      'ck-diary-tip', 'ck-diary-jump', 'ck-diary-entry', 'ck-rating'):
            self.assertIn(f'id="{el_id}"', page_html, f'页面缺少元素 {el_id}')
        # 日记双向关联的前后端契约：页面按钮 + JS 深链构造 + 日记本页回链
        self.assertIn('diaryUrl', js)
        self.assertIn('renderDiary', js)
        self.assertIn("link=" , js.replace("'", '"'))   # JS 拼 ?link= 深链
        self.assertIn('每日评级总控台', index_html)
        diary_js = (HERE / 'static' / 'js' / 'diary.js').read_text(encoding='utf-8')
        self.assertIn('checkin:', diary_js)             # linkedHtml 识别 checkin 回指
        self.assertIn('/checkin?date=', diary_js)       # 日记本「查看今日评级」深链

    # ------------------------------------------- 2. 红线：GET today 绝不触达 OKX
    def test_02_today_without_cache_never_calls_okx(self):
        status, value = self.get('/checkin/api/today')
        self.assertEqual((status, value['code']), (200, 200))
        metrics = value['data']['metrics']
        self.assertEqual(metrics['trade']['status'], 'no_data')
        self.assertEqual(metrics['trade']['src'], 'none')
        self.assertIsNone(metrics['trade']['pct'])
        self.assertEqual(metrics['calorie']['status'], 'no_data')
        self.assertEqual(metrics['learn']['hours'], 0)
        self.assertEqual(OKX_CALLS, [], 'GET today 不得调用任何 OKX 接口')
        # GET 零写入
        with FACTORY() as session:
            for t in TABLES:
                if t.name in ('daily_checkins', 'trading_daily_snapshots'):
                    self.assertEqual(session.scalar(select(func.count()).select_from(t)), 0,
                                     f'GET 写了 {t.name}')

    def test_02b_stale_cache_served_without_okx(self):
        old = (datetime.now() - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
        with FACTORY.begin() as session:
            repo.upsert_snapshot(session, 'smoke_acct', _day(), {
                'base_balance': 1000.0, 'base_date': _day(-1), 'live_balance': 1050.0,
                'net_transfer': None, 'pct': 5.0, 'status': 'ok', 'fetched_at': old})
        status, value = self.get('/checkin/api/today')
        trade = value['data']['metrics']['trade']
        self.assertEqual(trade['pct'], 5.0)
        self.assertEqual(trade['src'], 'cache_stale')   # 过期也只用缓存，不实拉
        self.assertEqual(OKX_CALLS, [], '过期缓存仍不得触达 OKX')

    # ------------------------------------------------- 3. 三项指标采集口径
    def test_03_calorie_and_learn_metrics(self):
        self.seed_calorie(_day(), 900)
        self.seed_calorie(_day(-1), 100)   # 干扰项：不该被读到
        # 学习：600 分钟=10h；done 任务去重后 2 个；doing 与野链接不算
        self.seed_learn_slots(_day(), [
            (300, [{'task_id': 't1', 'state': 'done'}, {'task_id': 't1', 'state': 'done'}]),
            (300, [{'task_id': 't2', 'state': 'done'}, {'task_id': 't3', 'state': 'doing'}]),
        ])
        self.seed_learn_slots(_day(-1), [(600, [{'task_id': 't9', 'state': 'done'}])])  # 跨日干扰
        self.seed_learn_slots(_day(), [(120, [{'task_id': 't8', 'state': 'done'}])],
                              card_type='trade', card_id='trade_only_card')  # 交易卡默认不计
        status, value = self.get('/checkin/api/today')
        metrics = value['data']['metrics']
        self.assertEqual(metrics['calorie']['value'], 900)
        self.assertTrue(metrics['calorie']['passed'])
        self.assertEqual(metrics['learn']['hours'], 10.0)
        self.assertEqual(metrics['learn']['task_done_count'], 2)
        self.assertTrue(metrics['learn']['passed'])
        self.assertFalse(metrics['all_passed'])   # 交易还没数据

    # ----------------------------------------- 4. refresh-trade 实拉与按日缓存
    def test_04_refresh_trade_and_cache(self):
        self.seed_balance(datetime.now() - timedelta(days=1, hours=1), 1000.0)
        global OKX_BALANCE, OKX_BILLS
        OKX_BALANCE = [{'totalEq': '1080'}]
        # 当日净入金 +50（type=1 非盈亏类应被剔除；type=2 交易盈亏保留在差额里）
        OKX_BILLS = [
            {'ts': str(_ms(datetime.now())), 'type': '1', 'balChg': '50'},
            {'ts': str(_ms(datetime.now())), 'type': '2', 'balChg': '30'},
        ]
        status, value = self.post('/checkin/api/refresh-trade', {'date': _day()})
        self.assertEqual((status, value['code']), (200, 200))
        data = value['data']
        self.assertEqual(data['base'], 1000.0)
        self.assertEqual(data['live'], 1080.0)
        self.assertEqual(data['transfer'], 50.0)
        self.assertEqual(data['pct'], 3.0)   # (1080-1000-50)/1000
        self.assertEqual(data['src'], 'live')
        self.assertEqual(OKX_CALLS, ['get_account_balance', 'get_account_bills'])

        # 再 GET：只回缓存，不再打 OKX
        OKX_CALLS.clear()
        status, value = self.get('/checkin/api/today')
        trade = value['data']['metrics']['trade']
        self.assertEqual(trade['pct'], 3.0)
        self.assertIn(trade['src'], ('cache', 'cache_stale'))
        self.assertEqual(OKX_CALLS, [], '刷新后 GET 必须吃缓存')

        # 未过期的非强制刷新直接回缓存（消息可区分）
        status, value = self.post('/checkin/api/refresh-trade', {'date': _day()})
        self.assertEqual(value['message'], '缓存命中')
        self.assertEqual(OKX_CALLS, [])

        # 历史日期拒绝刷新
        status, value = self.post('/checkin/api/refresh-trade', {'date': _day(-1)})
        self.assertEqual((status, value['code']), (200, 400))

    # --------------------------------------------------- 5. 提交打卡（服务端重算）
    def test_05_submit_checkin(self):
        self.seed_calorie(_day(), 800)
        self.seed_learn_slots(_day(), [
            (600, [{'task_id': 'a', 'state': 'done'}]),
            (30, [{'task_id': 'b', 'state': 'done'}]),
        ])
        self.seed_balance(datetime.now() - timedelta(days=1, hours=1), 1000.0)
        global OKX_BALANCE
        OKX_BALANCE = [{'totalEq': '1080'}]   # 无入金账单 → pct 8.0 恰好达标
        status, value = self.post('/checkin/api/checkin',
                                  {'rating': ' s ', 'comment': '  全部达标  '})
        self.assertEqual((status, value['code']), (200, 200))
        saved = value['data']
        self.assertTrue(saved['all_passed'])
        self.assertEqual(saved['rating'], 'S')
        self.assertEqual(saved['comment'], '全部达标')
        self.assertEqual(saved['trade']['pct'], 8.0)
        self.assertTrue(saved['trade']['passed'])
        self.assertEqual(saved['trade']['src'], 'live')
        self.assertEqual(saved['calorie']['value'], 800)
        self.assertEqual(saved['trade']['account_key'], 'smoke_acct')

        first_created = saved['created_at']
        # 覆盖提交：created_at 保留、非法评级归空
        status, value = self.post('/checkin/api/checkin', {'rating': 'X', 'comment': '改'})
        saved2 = value['data']
        self.assertEqual(saved2['created_at'], first_created)
        self.assertEqual(saved2['rating'], '')
        self.assertEqual(saved2['comment'], '改')
        self.assertFalse(saved2['all_passed'] and not saved2['learn']['passed'])

        # 参数校验
        status, value = self.post('/checkin/api/checkin', {'date': _day(1)})
        self.assertEqual(value['code'], 400)          # 未来日期
        status, value = self.post('/checkin/api/checkin', {'date': 'bad-date'})
        self.assertEqual(value['code'], 400)          # 非法格式
        status, value = self.post('/checkin/api/checkin', {'comment': '字' * 2250})
        self.assertEqual(value['code'], 400)          # 超缓冲上限拒收
        # 2000~2200 之间容忍并截断落库
        status, value = self.post('/checkin/api/checkin', {'comment': '字' * 2100})
        self.assertEqual(value['code'], 200)
        self.assertEqual(len(value['data']['comment']), repo.MAX_COMMENT_CHARS)

        # 提交后 GET 能看到已存记录（最后一次是截断的长评价）
        status, value = self.get('/checkin/api/today')
        saved_now = value['data']['saved']
        self.assertIsNotNone(saved_now)
        self.assertEqual(len(saved_now['comment']), repo.MAX_COMMENT_CHARS)

    # --------------------------------- 6. 历史补录：纯本地差分、零 OKX、不写缓存
    def test_06_history_backfill_local_only(self):
        d2 = _day(-2)
        day_start = datetime.strptime(d2, '%Y-%m-%d')
        self.seed_balance(day_start - timedelta(hours=4), 900.0)     # 基准（前一日最后点）
        self.seed_balance(day_start + timedelta(minutes=5), 1000.0)  # 当日首点
        self.seed_balance(day_start + timedelta(hours=20), 1080.0)   # 当日末点=live
        self.seed_calorie(d2, 1000)
        self.seed_learn_slots(d2, [(700, [{'task_id': 'x', 'state': 'done'},
                                          {'task_id': 'y', 'state': 'done'}])])
        status, value = self.post('/checkin/api/checkin', {'date': d2, 'rating': 'A'})
        self.assertEqual(value['code'], 200)
        saved = value['data']
        self.assertEqual(saved['trade']['base'], 900.0)
        self.assertEqual(saved['trade']['pct'], 20.0)   # (1080-900)/900，无账单修正
        self.assertIsNone(saved['trade']['transfer'])
        self.assertTrue(saved['trade']['passed'])
        self.assertEqual(OKX_CALLS, [], '历史补录绝不触达 OKX')
        with FACTORY() as session:
            snap = repo.load_snapshot(session, 'smoke_acct', d2)
            self.assertIsNone(snap, '历史补录不得写按日缓存')

        # 历史列表
        status, value = self.get(f'/checkin/api/history?start={_day(-29)}&end={_day()}')
        items = value['data']['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['date'], d2)
        self.assertEqual(items[0]['rating'], 'A')
        # 参数校验
        status, value = self.get('/checkin/api/history?start=bad')
        self.assertEqual(value['code'], 400)
        status, value = self.get(f'/checkin/api/history?start={_day(-90)}&end={_day()}')
        self.assertEqual(value['code'], 400)   # 超 62 天

    # ------------------------------------------------------- 7. repo 层单元测试
    def test_07_repo_units(self):
        self.assertEqual(repo.normalize_rating(' s '), 'S')
        self.assertEqual(repo.normalize_rating('x'), None)
        self.assertEqual(repo.normalize_rating(None), None)
        self.assertEqual(repo.normalize_comment('  ab  '), 'ab')
        self.assertEqual(len(repo.normalize_comment('字' * 3000)), repo.MAX_COMMENT_CHARS)

        with FACTORY.begin() as session:
            with self.assertRaises(ValueError):
                repo.upsert_checkin(session, {'date': 'not-a-date'})
            row = repo.upsert_checkin(session, {'date': _day(), 'comment': 'v1'})
            created = row.created_at
            row2 = repo.upsert_checkin(session, {'date': _day(), 'comment': 'v2'})
            self.assertEqual(row2.created_at, created)   # 覆盖保留创建时间
            self.assertEqual(row2.comment, 'v2')
            self.assertEqual(repo.load_checkin(session, _day()).comment, 'v2')
            self.assertIsNone(repo.load_checkin(session, _day(-3)))

            now = datetime.now()
            fresh = types.SimpleNamespace(fetched_at=now.strftime('%Y-%m-%d %H:%M:%S'))
            stale = types.SimpleNamespace(
                fetched_at=(now - timedelta(seconds=400)).strftime('%Y-%m-%d %H:%M:%S'))
            self.assertTrue(repo.snapshot_is_fresh(fresh, 300))
            self.assertFalse(repo.snapshot_is_fresh(stale, 300))
            self.assertFalse(repo.snapshot_is_fresh(None, 300))
            self.assertFalse(repo.snapshot_is_fresh(
                types.SimpleNamespace(fetched_at='坏时间戳'), 300))

            # 裁剪：超过保留期的旧行被清掉
            ancient = (now - timedelta(days=repo._SNAPSHOT_RETAIN_DAYS + 5)).strftime('%Y-%m-%d')
            repo.upsert_snapshot(session, 'smoke_acct', ancient, {'pct': 1.0, 'status': 'ok',
                                                                  'fetched_at': '2020-01-01 00:00:00'})
            repo.upsert_snapshot(session, 'smoke_acct', _day(), {'pct': 2.0, 'status': 'ok',
                                                                 'fetched_at': '2020-01-02 00:00:00'})
            self.assertIsNone(repo.load_snapshot(session, 'smoke_acct', ancient),
                              '超期缓存应被裁剪')
            self.assertIsNotNone(repo.load_snapshot(session, 'smoke_acct', _day()))

    # ------------------------------------------------------- 8. 三处接线自检
    def test_08_wiring_contracts(self):
        self.assertIn('daily_checkins', _NEW_TABLE_NAMES)
        self.assertIn('trading_daily_snapshots', _NEW_TABLE_NAMES)
        schema_sql = (HERE.parent / 'db_schema.sql').read_text(encoding='utf-8')
        self.assertIn('CREATE TABLE IF NOT EXISTS `daily_checkins`', schema_sql)
        self.assertIn('CREATE TABLE IF NOT EXISTS `trading_daily_snapshots`', schema_sql)
        from .navigation_service import DEFAULT_ITEMS, ACTIVE_PAGE_ALIASES
        entry = next(i for i in DEFAULT_ITEMS if i['id'] == 'link-checkin')
        self.assertEqual(entry['url'], '/checkin')
        self.assertEqual(entry['builtin_key'], 'checkin')
        self.assertEqual(ACTIVE_PAGE_ALIASES.get('checkin'), '/checkin')
        # 页面激活高亮：/checkin 解析到 link-checkin
        from .navigation_service import resolve_active
        active, _parent = resolve_active(DEFAULT_ITEMS, '/checkin', None)
        self.assertEqual(active, 'link-checkin')

    # ------------------------------- 9. 日记双向关联：状态读取 + 提交定格回填
    def test_09_diary_association_and_freeze(self):
        # 无日记：api/today 透出 diary.has_entry=False
        status, value = self.get('/checkin/api/today')
        self.assertEqual((status, value['code']), (200, 200))
        d0 = value['data']['diary']
        self.assertFalse(d0['has_entry'])
        self.assertEqual(d0['entry_count'], 0)
        self.assertEqual(d0['entry_id'], '')
        # 无日记时提交：diary_entry_id 定格为空串
        status, value = self.post('/checkin/api/checkin', {'rating': 'B'})
        self.assertEqual(value['code'], 200)
        self.assertEqual(value['data']['diary_entry_id'], '')

        # 写一条回指本模块的日记 → api/today 命中该 entry_id
        linked = f'checkin:{_day()}'
        eid = self.seed_diary(_day(), content='复盘', linked_from=linked, entry_id='dA')
        OKX_CALLS.clear()   # 清掉前面提交动作产生的实拉记录
        status, value = self.get('/checkin/api/today')
        d1 = value['data']['diary']
        self.assertTrue(d1['has_entry'])
        self.assertEqual(d1['entry_id'], eid)
        self.assertEqual(OKX_CALLS, [], '读日记状态（GET today）绝不触达 OKX')

        # 再加一条非回指日记：计数+1，但 entry_id 仍优先取回指那条
        self.seed_diary(_day(), content='随手', linked_from='', entry_id='dB')
        status, value = self.get('/checkin/api/today')
        d2 = value['data']['diary']
        self.assertEqual(d2['entry_count'], 2)
        self.assertEqual(d2['entry_id'], 'dA')

        # 再次提交：diary_entry_id 定格为回指条目
        status, value = self.post('/checkin/api/checkin', {'rating': 'B'})
        self.assertEqual(value['data']['diary_entry_id'], 'dA')

    # ------------------- 10. 改名 + 23:30 收尾提醒任务接线 + 加列三处一致
    def test_10_rename_and_reminder_wiring(self):
        # 名称统一：导航种子项已改名
        from .navigation_service import DEFAULT_ITEMS
        entry = next(i for i in DEFAULT_ITEMS if i['id'] == 'link-checkin')
        self.assertEqual(entry['name'], '每日评级总控台')

        # 收尾提醒模块：job_id、纯函数判定、配置键
        from .task.monitor import checkin_diary_reminder as cdr
        self.assertEqual(cdr.JOB_ID, 'checkin_diary_reminder')
        self.assertEqual(cdr.KEY_CONFIG, 'checkin_diary_reminder_config')
        self.assertEqual((cdr.RUN_HOUR, cdr.RUN_MINUTE), (23, 30))
        self.assertEqual(cdr._evaluate(None, True), {'skip': 'not_checked_in'})
        self.assertEqual(cdr._evaluate(types.SimpleNamespace(rating='A'), True),
                         {'skip': 'complete'})
        v = cdr._evaluate(types.SimpleNamespace(rating=''), False)
        self.assertTrue(v['need'] and v['missing_rating'] and v['missing_diary'])
        self.assertTrue(callable(cdr.register_checkin_diary_reminder_job))

        # 调度接线：SWITCHABLE_JOB_LABELS 有该任务 + register_default_jobs 注册它
        sched_src = (HERE / 'task' / 'scheduler.py').read_text(encoding='utf-8')
        self.assertIn("'checkin_diary_reminder':", sched_src)
        self.assertIn('register_checkin_diary_reminder_job', sched_src)

        # diary_entry_id 三处一致：模型列 / 增量加列清单 / DDL
        from .models import DailyCheckin
        self.assertTrue(hasattr(DailyCheckin, 'diary_entry_id'))
        db_src = (HERE / 'database.py').read_text(encoding='utf-8')
        self.assertIn("('daily_checkins', 'diary_entry_id'", db_src)
        schema_sql = (HERE.parent / 'db_schema.sql').read_text(encoding='utf-8')
        self.assertIn('`diary_entry_id`', schema_sql)
        # 到_dict 带出该字段
        with FACTORY.begin() as session:
            row = repo.upsert_checkin(session, {'date': _day(), 'diary_entry_id': 'dX'})
            self.assertEqual(row.to_dict()['diary_entry_id'], 'dX')


if __name__ == '__main__':
    unittest.main(verbosity=2)
