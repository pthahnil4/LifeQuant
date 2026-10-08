"""日记本模块隔离验收；必须配置 CRYPTO_TEST_DB_URL，仅建 diary_* 表。
运行：python -B -m crypto._smoke_diary
测试目标须为空账本；不接受覆盖已有测试数据。SQLite 不代表 MySQL 锁验收。
"""
import os
import sys
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from .test_isolation import require_isolated_test_db, TestDbNotConfigured

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
try:
    TEST_URL = require_isolated_test_db()
except TestDbNotConfigured as exc:
    print(exc)
    raise SystemExit(2)

from flask import Flask, request, jsonify
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import sessionmaker
from . import diary_routes as routes
from . import diary_repo as repo
from .database import Base

ENGINE = create_engine(TEST_URL)
if ENGINE.dialect.name == 'sqlite':
    @event.listens_for(ENGINE, 'connect')
    def foreign_keys(connection, record):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=15000')

TABLES = [t for t in Base.metadata.sorted_tables if t.name.startswith('diary_')]
assert len(TABLES) == 3, f'diary_* 表未接进 models（实际 {len(TABLES)} 张）'
Base.metadata.create_all(ENGINE, tables=TABLES)
FACTORY = sessionmaker(ENGINE, expire_on_commit=False)
with FACTORY() as session:
    if any(session.scalar(select(func.count()).select_from(t)) for t in TABLES):
        print('拒绝覆盖：隔离库 diary_* 表已有数据，请另选空测试库。')
        raise SystemExit(2)


@contextmanager
def scope():
    with FACTORY.begin() as session:
        yield session


routes.session_scope = scope
HERE = Path(__file__).resolve().parent
APP = Flask('diary_smoke', template_folder=str(HERE / 'templates'), static_folder=str(HERE / 'static'))
APP.config['TESTING'] = True
APP.register_blueprint(routes.diary_bp)


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
    return jsonify(success=True, config=build_default_config(), source='diary-test', editable=False, warnings=[])


API = '/diary/api'


class DiaryTest(unittest.TestCase):
    def setUp(self):
        self.client = APP.test_client()
        routes._unlock_fails.clear()

    def tearDown(self):
        with FACTORY.begin() as session:
            for table in reversed(TABLES):
                session.execute(table.delete())

    # ------------------------------------------------------------- 工具
    def call(self, method, path, body=None, headers=None):
        response = self.client.open(API + path, method=method, json=body, headers=headers or {})
        value = response.get_json()
        self.assertIsNotNone(value, f'{path} 未返回 JSON: {response.status_code}')
        return response.status_code, value

    def ok(self, method, path, body=None):
        status, value = self.call(method, path, body)
        self.assertEqual(status, 200, value)
        self.assertEqual(value['code'], 200, value)
        return value['data']

    def boot(self, query=''):
        status, value = self.call('GET', '/bootstrap' + query)
        self.assertEqual(value['code'], 200, value)
        return value['data']

    def set_password(self, pwd='s3cret-pass'):
        status, value = self.call('POST', '/password-set', {'password': pwd})
        self.assertEqual(value['code'], 200, value)
        return value

    def unlock(self, pwd='s3cret-pass'):
        return self.call('POST', '/unlock', {'password': pwd})

    def cookie_of(self, response):
        for header in response.headers.getlist('Set-Cookie'):
            if header.startswith(repo.UNLOCK_COOKIE + '='):
                return header.split(';', 1)[0]
        return None

    def drop_cookie(self):
        """test_client 会自动持久化 cookie；模拟陌生访客时手动摘掉解锁凭据"""
        self.client.delete_cookie(repo.UNLOCK_COOKIE)

    # ------------------------------------------------------------- 纯函数
    def test_password_hash_roundtrip(self):
        stored = repo.hash_password('correct horse battery staple')
        self.assertTrue(repo.verify_password('correct horse battery staple', stored))
        self.assertFalse(repo.verify_password('wrong password', stored))
        self.assertFalse(repo.verify_password('anything', ''))
        self.assertFalse(repo.verify_password('', stored))
        # 未知前缀（人为改库）必须拒绝，绝不放行
        self.assertFalse(repo.verify_password('x', 'md5$deadbeef'))
        with self.assertRaises(repo.DiaryPasswordError):
            repo.hash_password('123')          # 过短
        with self.assertRaises(repo.DiaryPasswordError):
            repo.hash_password('x' * 200)      # 过长

    def test_unlock_cookie_signature(self):
        h = 'argon2id$fake-hash-for-signature-test'
        now = int(time.time())
        cookie = repo.unlock_cookie_value(h, repo.new_unlock_expiry(now))
        self.assertTrue(repo.unlock_cookie_ok(cookie, h, now))
        self.assertFalse(repo.unlock_cookie_ok(cookie, h, now + repo.UNLOCK_TTL_SECONDS + 1))  # 过期
        self.assertFalse(repo.unlock_cookie_ok(cookie, 'argon2id$other-hash', now))            # 改密即失效
        self.assertFalse(repo.unlock_cookie_ok(cookie + 'x', h, now))                           # 篡改
        self.assertFalse(repo.unlock_cookie_ok('', h, now))
        self.assertFalse(repo.unlock_cookie_ok('garbage', h, now))

    def test_life_and_goal_math(self):
        import datetime
        # 出生当天 = 人生第 1 天；次日 00:30 = 第 2 天 0 点过半小时
        age = repo.life_day_age('2000-01-01', datetime.datetime(2000, 1, 1, 23, 59))
        self.assertEqual((age['days'], age['hours']), (1, 23))
        age2 = repo.life_day_age('2000-01-01', datetime.datetime(2000, 1, 2, 0, 30))
        self.assertEqual((age2['days'], age2['hours']), (2, 0))
        self.assertIsNone(repo.life_day_age('2000-01-01', datetime.datetime(1999, 12, 31)))
        self.assertIsNone(repo.life_day_age('not-a-date'))
        cd = repo.goal_countdown('2026-10-05', datetime.datetime(2026, 10, 1, 10, 0))
        self.assertEqual((cd['days_left'], cd['hours_left'], cd['overdue']), (4, 13, False))
        cd2 = repo.goal_countdown('2026-10-05', datetime.datetime(2026, 10, 6, 0, 0))
        self.assertTrue(cd2['overdue'])

    # ------------------------------------------------------------- 页面与导航
    def test_page_and_nav_entry(self):
        response = self.client.get('/diary')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('日记本', html)
        self.assertIn('diary.js', html)
        # 导航默认目录里有「日记本」，且挂在个人管理分组下、URL 正确
        from .navigation_service import build_default_config
        items = build_default_config()['items']
        diary = next(n for n in items if n['id'] == 'link-diary')
        self.assertEqual(diary['url'], '/diary')
        self.assertEqual(diary['parent_id'], 'group-personal')

    # ------------------------------------------------------------- 按天记录
    def test_entries_day_dimension_and_validation(self):
        d1 = self.ok('POST', '/add', {'content': '第一条', 'date': '2026-09-25'})
        d2 = self.ok('POST', '/add', {'content': '第二条同一天', 'date': '2026-09-25', 'mood': '😄'})
        self.ok('POST', '/add', {'content': '另一天', 'date': '2026-09-26'})
        status, value = self.call('GET', '/day?date=2026-09-25')
        self.assertEqual(value['code'], 200)
        contents = [e['content'] for e in value['data']['entries']]
        self.assertEqual(contents, ['第一条', '第二条同一天'])   # 书写顺序正排
        self.assertEqual(value['data']['entries'][1]['mood'], '😄')
        # 缺省日期 = 今天；空正文/坏日期拒绝
        status, value = self.call('POST', '/add', {'content': '  '})
        self.assertEqual(value['code'], 400)
        status, value = self.call('POST', '/add', {'content': 'x', 'date': '2026-13-99'})
        self.assertEqual(value['code'], 400)
        # 更新与删除
        self.ok('POST', '/update', {'id': d1['id'], 'fields': {'content': '改过的', 'private': True}})
        status, value = self.call('GET', '/day?date=2026-09-25')
        self.assertTrue(value['data']['entries'][0]['private'])
        self.ok('POST', '/delete', {'id': d2['id']})
        status, value = self.call('POST', '/delete', {'id': 'dentry_missing'})
        self.assertEqual(value['code'], 404)

    def test_calendar_and_pagination(self):
        for i in range(3):
            self.ok('POST', '/add', {'content': f'九月{i}', 'date': '2026-09-25'})
        self.ok('POST', '/add', {'content': '十月', 'date': '2026-10-01', 'private': True})
        status, value = self.call('GET', '/calendar?month=2026-09')
        days = {d['date']: d for d in value['data']['days']}
        self.assertEqual(days['2026-09-25']['total'], 3)
        self.assertEqual(days['2026-09-25']['private'], 0)
        status, value = self.call('GET', '/calendar?month=2026-10')
        self.assertEqual(value['data']['days'][0]['private'], 1)
        # 分页：limit=1 逐页翻，不重不漏
        page1 = self.boot('?limit=1')
        self.assertTrue(page1['has_more'])
        page2 = self.boot('?limit=1&before=' + page1['entries'][-1]['id'])
        ids = {e['id'] for e in page1['entries']} | {e['id'] for e in page2['entries']}
        self.assertEqual(len(ids), 2)

    # ------------------------------------------------------------- 隐私与密码
    def test_privacy_modes_and_unlock_flow(self):
        self.ok('POST', '/add', {'content': '公开的话', 'date': '2026-09-26'})
        self.ok('POST', '/add', {'content': '秘密的话', 'date': '2026-09-26', 'private': True})
        # 无密码：一切可见（unlocked）
        data = self.boot()
        self.assertEqual(data['locked_mode'], 'unlocked')
        self.assertTrue(data['unlocked'])
        # 未设密码不许开隐私模式
        status, value = self.call('POST', '/settings', {'visibility': 'private'})
        self.assertEqual(value['code'], 400)
        # 设密码 → public 模式只锁单条隐私
        resp = self.client.post(API + '/password-set', json={'password': 'topsecret1'})
        self.assertEqual(resp.get_json()['code'], 200)
        cookie = self.cookie_of(resp)
        self.assertIsNotNone(cookie)   # 设密码即顺带发放解锁凭据
        data = self.boot()
        self.assertTrue(data['unlocked'])
        # 无 cookie：partial 模式，隐私条正文被服务端脱敏
        self.drop_cookie()
        data = self.boot()
        self.assertEqual(data['locked_mode'], 'partial')
        self.assertFalse(data['unlocked'])
        priv = [e for e in data['entries'] if e['private']][0]
        self.assertTrue(priv['locked'])
        self.assertEqual(priv['content'], '')
        pub = [e for e in data['entries'] if not e['private']][0]
        self.assertEqual(pub['content'], '公开的话')
        # 错误密码被拒；正确密码发 cookie
        resp = self.client.post(API + '/unlock', json={'password': 'wrong'})
        self.assertEqual(resp.get_json()['code'], 401)
        resp = self.client.post(API + '/unlock', json={'password': 'topsecret1'})
        self.assertEqual(resp.get_json()['code'], 200)
        cookie = self.cookie_of(resp)
        headers = {'Cookie': cookie}
        status, value = self.call('GET', '/bootstrap', headers=headers)
        self.assertTrue(value['data']['unlocked'])
        priv = [e for e in value['data']['entries'] if e['private']][0]
        self.assertEqual(priv['content'], '秘密的话')
        self.drop_cookie()   # 后面按场景手动带 Cookie，避免 jar 里残留干扰
        # 切全书隐私：无 cookie 时全部脱敏 + 目标/生日不透出 + 写接口拒绝
        self.ok('POST', '/settings', {'visibility': 'private'})
        self.drop_cookie()
        status, value = self.call('GET', '/bootstrap')
        self.assertEqual(value['data']['locked_mode'], 'locked')
        self.assertTrue(all(e.get('locked') for e in value['data']['entries']))
        self.assertEqual(value['data']['goals'], [])
        self.assertEqual(value['data']['settings']['birth_date'], '')
        status, value = self.call('POST', '/goal-add', {'title': '偷塞', 'due_date': '2026-12-31'})
        self.assertEqual(value['code'], 401)
        # 改密码后旧 cookie 立即失效
        resp = self.client.post(API + '/password-set',
                                json={'password': 'another-pass', 'old_password': 'topsecret1'})
        self.assertEqual(resp.get_json()['code'], 200)
        self.drop_cookie()   # 顺带发放的新凭据也清掉，只留改密前的旧 cookie 场景
        status, value = self.call('GET', '/bootstrap', headers=headers)
        self.assertFalse(value['data']['unlocked'])
        # 换密码要验旧密码；清除密码退回 public 并作废 cookie
        status, value = self.call('POST', '/password-set', {'password': 'nope'})
        self.assertEqual(value['code'], 400)
        status, value = self.call('POST', '/password-remove', {'password': 'wrong'})
        self.assertEqual(value['code'], 401)
        resp = self.client.post(API + '/password-remove', json={'password': 'another-pass'})
        self.assertEqual(resp.get_json()['code'], 200)
        data = self.boot()
        self.assertEqual(data['locked_mode'], 'unlocked')
        self.assertEqual(data['settings']['visibility'], 'public')

    def test_unlock_throttle(self):
        self.set_password('throttle-pass')
        # 清掉设密码前可能记录的失败，再从干净状态刷失败
        routes._unlock_fails.clear()
        for _ in range(routes._UNLOCK_MAX_FAILS):
            self.client.post(API + '/unlock', json={'password': 'bad'})
        resp = self.client.post(API + '/unlock', json={'password': 'bad'})
        self.assertEqual(resp.get_json()['code'], 429)
        # 限流只锁错误尝试路径，正确密码同样被暂时拒绝（防碰运气）
        resp = self.client.post(API + '/unlock', json={'password': 'throttle-pass'})
        self.assertEqual(resp.get_json()['code'], 429)
        routes._unlock_fails.clear()
        resp = self.client.post(API + '/unlock', json={'password': 'throttle-pass'})
        self.assertEqual(resp.get_json()['code'], 200)

    # ------------------------------------------------------------- 目标与设置
    def test_goals_lifecycle(self):
        goal = self.ok('POST', '/goal-add', {'title': '掌握Python技能', 'due_date': '2026-10-05'})
        self.ok('POST', '/goal-add', {'title': '过期目标', 'due_date': '2020-01-01'})
        status, value = self.call('POST', '/goal-add', {'title': '', 'due_date': '2026-10-05'})
        self.assertEqual(value['code'], 400)
        status, value = self.call('POST', '/goal-add', {'title': '坏日期', 'due_date': 'Oct-5'})
        self.assertEqual(value['code'], 400)
        goals = self.boot()['goals']
        self.assertEqual(len(goals), 2)
        self.ok('POST', '/goal-update', {'id': goal['id'], 'fields': {'done': True}})
        goals = self.boot()['goals']
        self.assertTrue([g for g in goals if g['id'] == goal['id']][0]['done'])
        status, value = self.call('POST', '/goal-update', {'id': 'dgoal_x', 'fields': {'done': True}})
        self.assertEqual(value['code'], 404)
        self.ok('POST', '/goal-delete', {'id': goal['id']})

    def test_settings_birth_date(self):
        self.ok('POST', '/settings', {'birth_date': '1990-05-20'})
        data = self.boot()
        self.assertEqual(data['settings']['birth_date'], '1990-05-20')
        self.assertIsNotNone(data['life_age'])
        self.assertGreater(data['life_age']['days'], 10000)
        status, value = self.call('POST', '/settings', {'birth_date': '2099-01-01'})
        self.assertEqual(value['code'], 400)
        status, value = self.call('POST', '/settings', {'birth_date': '90-05-20'})
        self.assertEqual(value['code'], 400)
        self.ok('POST', '/settings', {'birth_date': ''})
        self.assertIsNone(self.boot()['life_age'])

    # ------------------------------------------------------------- 导出导入
    def test_export_import_roundtrip(self):
        self.ok('POST', '/add', {'content': '要导出的日记', 'date': '2026-09-01'})
        self.ok('POST', '/add', {'content': '隐私日记', 'date': '2026-09-02', 'private': True})
        self.ok('POST', '/goal-add', {'title': '导出的目标', 'due_date': '2026-12-31'})
        self.ok('POST', '/settings', {'birth_date': '1995-02-03'})
        resp = self.client.get(API + '/export')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attachment', resp.headers.get('Content-Disposition', ''))
        import json as _json
        payload = _json.loads(resp.get_data(as_text=True))
        self.assertEqual(payload['app'], 'cryptoTrade-diary')
        self.assertEqual(len(payload['entries']), 2)
        self.assertNotIn('password_hash', _json.dumps(payload))   # 导出不含哈希/明文
        # 导入回同库：同 id 幂等跳过
        stats = self.ok('POST', '/import', {'payload': payload})
        self.assertEqual(stats['entries_added'], 0)
        self.assertEqual(stats['entries_skipped'], 2)
        # 抹掉 id 再导：作为新条目入库
        for e in payload['entries']:
            e['id'] = ''
        stats = self.ok('POST', '/import', {'payload': payload})
        self.assertEqual(stats['entries_added'], 2)
        self.assertEqual(self.boot()['stats']['total_entries'], 4)
        # 脏数据条目只会跳过，不会崩
        dirty = {'app': 'cryptoTrade-diary', 'entries': [
            {'date': 'bad', 'content': 'x'}, {'date': '2026-09-03', 'content': '  '},
            {'date': '2026-09-03', 'content': 'ok', 'linked_from': 'evil:/etc/passwd'}]}
        stats = self.ok('POST', '/import', {'payload': dirty})
        self.assertEqual(stats['entries_added'], 1)
        status, value = self.call('POST', '/import', {'payload': {'app': 'other'}})
        self.assertEqual(value['code'], 400)
        # 隐私模式未解锁禁止导入
        self.set_password('lock-import')
        self.ok('POST', '/settings', {'visibility': 'private'})
        self.drop_cookie()
        status, value = self.call('POST', '/import', {'payload': payload})
        self.assertEqual(value['code'], 401)

    # ------------------------------------------------------------- 接线自检
    def test_wiring_contracts(self):
        # database._NEW_TABLE_NAMES 必须包含三张 diary 表（存量库自动补建入口）
        from .database import _NEW_TABLE_NAMES
        for name in ('diary_settings', 'diary_entries', 'diary_goals'):
            self.assertIn(name, _NEW_TABLE_NAMES)
        # db_schema.sql 手工同步了等价 DDL（两条独立口径）
        schema = (HERE.parent / 'db_schema.sql').read_text(encoding='utf-8')
        for name in ('diary_settings', 'diary_entries', 'diary_goals'):
            self.assertIn(f'CREATE TABLE IF NOT EXISTS `{name}`', schema)
        # 导航禁止把链接指向 /diary/api 动作端点
        from .navigation_service import validate_url, NavValidationError
        with self.assertRaises(NavValidationError):
            validate_url('/diary/api/unlock')


if __name__ == '__main__':
    unittest.main(verbosity=2)
