#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
写库冒烟隔离守卫回归（纯离线，不连任何数据库）
================================================
被测：crypto/test_isolation.py

为什么单独测它：清表还原型冒烟（_smoke_plan / _smoke_task_tree）会把
plan_* 三表清空后重建，一旦守卫被绕过、或有人把测试库名误填成业务库，
代价是真实任务数据被抹掉（本项目 db_url.txt 指向的就是线上远端 MySQL）。
所以这里把「必须拒绝」的每一种情形都钉死，并用子进程真跑一遍这些冒烟的
入口，确认未配置测试库时它们是退出而不是开跑。

运行：python -B -m unittest crypto._smoke_test_isolation -v
"""
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

from . import test_isolation as iso      # noqa: E402
from .database import resolve_db_url     # noqa: E402


class TestIsolationGuardTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop(iso.TEST_DB_ENV, None)
        os.environ.pop(iso.TEST_SCHEMA_ENV, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_missing_env_is_rejected_without_fallback(self):
        """未设置测试库环境变量：即使业务库可连通也必须拒绝（不回退）"""
        self.assertTrue(resolve_db_url(), '前置：本机应已配置业务库连接')
        business_before = os.environ.get('CRYPTO_DB_URL')
        with self.assertRaises(iso.TestDbNotConfigured) as ctx:
            iso.require_isolated_test_db()
        self.assertIn(iso.TEST_DB_ENV, str(ctx.exception))
        self.assertIn(iso.TEST_SCHEMA_ENV, str(ctx.exception))
        # 业务库配置原样未动（守卫只在通过时才改写环境）
        self.assertEqual(os.environ.get('CRYPTO_DB_URL'), business_before)

    def test_schema_env_reuses_credentials_but_swaps_database(self):
        """只给库名时复用业务账号/主机换库名；填成业务库名本身照样被拒"""
        business = resolve_db_url()
        if not business:
            self.skipTest('未配置业务库连接，无法校验换库名语义')
        self.assertEqual(iso._swap_schema(business, 'crypto_test'),
                         iso._swap_schema(iso._swap_schema(business, 'anything'), 'crypto_test'))
        self.assertEqual(iso._schema_of(iso._swap_schema(business, 'crypto_test')), 'crypto_test')
        os.environ[iso.TEST_SCHEMA_ENV] = 'crypto_test'
        self.assertEqual(iso._schema_of(iso.resolve_test_db_url()), 'crypto_test')
        # 用业务账号连的另一个库，仍然必须带测试标记：填回业务库名要拒绝
        os.environ[iso.TEST_SCHEMA_ENV] = iso._schema_of(business)
        with self.assertRaises(iso.TestDbNotConfigured):
            iso.require_isolated_test_db()

    def test_schema_name_must_carry_test_marker(self):
        business = resolve_db_url() or 'mysql+pymysql://u:p@127.0.0.1:3306/crypto_trade?charset=utf8mb4'
        self.assertNotEqual(iso._schema_of(business), 'crypto_trade_test')
        with self.assertRaises(iso.TestDbNotConfigured):
            iso.validate_test_db_url(business, business_url=business)
        with self.assertRaises(iso.TestDbNotConfigured):
            iso.validate_test_db_url(
                'mysql+pymysql://u:p@127.0.0.1:3306/crypto_trade_test2?charset=utf8mb4',
                business_url=business)
        # 与业务库同库名：换主机也拒绝
        with self.assertRaises(iso.TestDbNotConfigured):
            iso.validate_test_db_url(
                'mysql+pymysql://u:p@10.0.0.9:3306/' + iso._schema_of(business),
                business_url=business)
        self.assertEqual(iso._schema_of(''), '')

    def test_valid_test_db_applies_env_and_data_dir(self):
        business = resolve_db_url() or 'mysql+pymysql://u:p@127.0.0.1:3306/crypto_trade?charset=utf8mb4'
        schema = iso._schema_of(business) + '_smoke'
        test_url = f'mysql+pymysql://u:p@127.0.0.1:3306/{schema}?charset=utf8mb4'
        os.environ[iso.TEST_DB_ENV] = test_url
        os.environ.pop('CRYPTO_DB_URL', None)
        original_dir = os.environ.get(iso.DATA_DIR_ENV, '')
        self.assertEqual(iso.require_isolated_test_db(), test_url)
        self.assertEqual(os.environ['CRYPTO_DB_URL'], test_url)
        # 凭证不出现在校验失败信息里
        with self.assertRaises(iso.TestDbNotConfigured) as ctx:
            iso.validate_test_db_url(test_url, business_url=test_url)
        self.assertNotIn('u:p@', str(ctx.exception))
        new_dir = os.environ.get(iso.DATA_DIR_ENV, '')
        self.assertTrue(new_dir and os.path.isdir(new_dir))
        if original_dir:
            os.environ[iso.DATA_DIR_ENV] = original_dir

    def test_clearing_smokes_refuse_to_run_without_test_db(self):
        """所有清表冒烟：未配置测试库时退出码 2，且绝不连库"""
        os.environ.pop(iso.TEST_DB_ENV, None)
        scripts = ('_smoke_plan.py', '_smoke_task_tree.py', '_smoke_calorie.py',
                   '_smoke_balance.py', '_smoke_journal.py')
        for script in scripts:
            path = os.path.join(_HERE, script)
            env = dict(os.environ)
            env.pop(iso.TEST_DB_ENV, None)
            proc = subprocess.run([sys.executable, '-B', path], cwd=_ROOT, env=env,
                                  capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=180)
            self.assertEqual(proc.returncode, 2, f'{script} 应拒绝运行: {proc.stdout[-400:]}')
            self.assertIn(iso.TEST_DB_ENV, proc.stdout + proc.stderr)


class _FakeCursor:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self._log.append((sql, args))

    def fetchone(self):
        # 同时满足「业务库字符集/排序规则」两列与「库名存在」一列的读取
        return ('utf8mb4', 'utf8mb4_general_ci')


class _FakeConn:
    def __init__(self, log):
        self._log = log
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._log)

    def close(self):
        self.closed = True


class _ConnectError(Exception):
    """替代 pymysql 连接异常（离线桩，不引入真数据库依赖）"""


class MakeTestSchemaPrivilegeTests(unittest.TestCase):
    """建库脚本在无建库权时必须降级：只读探测，绝不尝试 CREATE/DROP DATABASE"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = os.path.join(_ROOT, 'data', 'make_test_schema.py')
        spec = importlib.util.spec_from_file_location('make_test_schema_under_test', path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def setUp(self):
        self.log = []
        self.conns = []
        self._orig_connect = self.mod._connect

    def tearDown(self):
        self.mod._connect = self._orig_connect

    def _stub(self, fail_server=False, fail_db=False):
        log, conns = self.log, self.conns

        def _connect(params, database=None, connect_timeout=15):
            if database is None and fail_server:
                raise _ConnectError(
                    "(1045, \"Access denied for user 'hunter'@'x' (using password: YES)\")")
            if database is not None and fail_db:
                raise _ConnectError('(1045, "Access denied")')
            conn = _FakeConn(log)
            conns.append(conn)
            return conn
        self.mod._connect = _connect

    _DDL = ('CREATE DATABASE', 'DROP DATABASE')

    def test_db_level_fallback_performs_no_ddl(self):
        """服务器级被拒 + 库级可通：确认库已存在即可，不下发任何 DDL"""
        self._stub(fail_server=True)
        self.mod.ensure_database('mysql+pymysql://u:p@h:3306/crypto_test',
                                 'mysql+pymysql://u:p@h:3306/crypto')
        self.assertFalse([s for s, _ in self.log if s.startswith(self._DDL)],
                         f'无建库权时不得执行 DDL：{self.log}')
        self.assertTrue(all('information_schema' in s for s, _ in self.log), self.log)
        self.assertTrue(all(c.closed for c in self.conns))

    def test_rebuild_without_server_privilege_is_refused(self):
        """--rebuild 需要 DROP DATABASE：无服务器级权限时必须拒绝而不是静默跳过"""
        self._stub(fail_server=True)
        with self.assertRaises(self.mod._NoCreatePrivilege):
            self.mod.ensure_database('mysql+pymysql://u:p@h:3306/crypto_test',
                                     'mysql+pymysql://u:p@h:3306/crypto', rebuild=True)
        self.assertFalse([s for s, _ in self.log if s.startswith(self._DDL)], self.log)

    def test_server_privilege_creates_with_mirrored_collation(self):
        """有建库权：CREATE DATABASE 带上从业务库镜像来的字符集/排序规则"""
        self._stub()
        self.mod.ensure_database('mysql+pymysql://u:p@h:3306/crypto_test',
                                 'mysql+pymysql://u:p@h:3306/crypto')
        ddl = [s for s, _ in self.log if s.startswith(self._DDL)]
        self.assertEqual(len(ddl), 1, self.log)
        self.assertIn('CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci', ddl[0])
        self.assertIn('`crypto_test`', ddl[0])
        # 业务库名只能作为 information_schema 只读查询的参数出现
        for sql, args in self.log:
            if 'information_schema' not in sql:
                self.assertNotIn('`crypto`', sql, sql)

    def test_both_levels_failed_raises_privilege_error(self):
        self._stub(fail_server=True, fail_db=True)
        with self.assertRaises(self.mod._NoCreatePrivilege) as ctx:
            self.mod.ensure_database('mysql+pymysql://u:p@h:3306/crypto_test', '')
        self.assertNotIn('u:p@', str(ctx.exception))

    def test_create_tables_refuses_engine_mismatch(self):
        """建表前引擎必须已指向测试库，否则拒绝（防止漏设环境变量打到业务库）"""
        from crypto import database
        saved_env = os.environ.get('CRYPTO_DB_URL')
        saved_engine, saved_factory = database._engine, database._SessionFactory
        try:
            os.environ['CRYPTO_DB_URL'] = 'mysql+pymysql://u:p@h:3306/crypto'
            database._engine, database._SessionFactory = None, None
            with self.assertRaises(RuntimeError) as ctx:
                self.mod.create_tables('mysql+pymysql://u:p@h:3306/crypto_test')
            self.assertIn('拒绝建表', str(ctx.exception))
        finally:
            database._engine, database._SessionFactory = saved_engine, saved_factory
            if saved_env is None:
                os.environ.pop('CRYPTO_DB_URL', None)
            else:
                os.environ['CRYPTO_DB_URL'] = saved_env


if __name__ == '__main__':
    unittest.main()
