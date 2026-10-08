#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
写库冒烟的测试隔离守卫
==========================================
背景：项目里的清表还原型冒烟（如 _smoke_plan / _smoke_task_tree）会
snapshot → TRUNCATE → 跑用例 → restore 业务表。一旦 restore 中途被强杀，
或者用例抛异常绕过 finally，真实任务数据就没了；而"先备份再清库"这件事
本身在远程 MySQL 上就是高风险动作。本地和宝塔连的常常是同一个库，所以
这类冒烟绝不能默默回退到业务库上跑。

本模块提供唯一入口 `require_isolated_test_db()`：
- 测试库连接串只认环境变量 CRYPTO_TEST_DB_URL，或 CRYPTO_TEST_DB_SCHEMA
  （后者复用业务账号/主机、只换库名，避免密码进命令行与日志）；
  绝不把 db_url.txt / CRYPTO_DB_URL 的原始目标当作测试库；
- 库名必须带测试标记（test / smoke / ci / sandbox），防止手滑填成业务库名；
- 与业务库同库名一律拒绝，即使主机不同也拒绝（宝塔与本地可能同名）；
- 未配置或不合规 → 抛 TestDbNotConfigured，调用方直接退出，
  绝不"降级用业务库继续跑"；
- 通过后把 CRYPTO_DB_URL 指向测试库，并把 CRYPTO_PLAN_DATA_DIR 换到临时目录，
  使配置双写、计划备份等文件副作用也落在隔离区。

配套 `ensure_test_schema()`：隔离库刚建好（或本地用 SQLite 文件）时表还是空的，
冒烟第一步快照就会失败；该函数按缺补建表结构，只建表不动数据。

典型用法（冒烟脚本开头）：

    from crypto.test_isolation import require_isolated_test_db, TestDbNotConfigured
    try:
        require_isolated_test_db()
    except TestDbNotConfigured as e:
        print(f'✗ {e}')
        sys.exit(1)
    from crypto.database import session_scope   # 必须在守卫之后再导入使用

注意：SQLAlchemy 引擎是进程内单例，若在守卫之前就已经建过引擎，
`require_isolated_test_db()` 会主动 dispose 并重建，避免连到旧目标。
"""

import os
import re
import logging

logger = logging.getLogger(__name__)

TEST_DB_ENV = 'CRYPTO_TEST_DB_URL'
TEST_SCHEMA_ENV = 'CRYPTO_TEST_DB_SCHEMA'
DATA_DIR_ENV = 'CRYPTO_PLAN_DATA_DIR'

# 库名必须命中其中之一，肉眼即可判断"这不是业务库"
_TEST_SCHEMA_PATTERN = re.compile(r'(?:^|[_\-.])(?:test|tests|smoke|ci|sandbox)(?:$|[_\-.])', re.I)


class TestDbNotConfigured(RuntimeError):
    """未配置或不满足隔离条件的测试库；调用方必须终止写库冒烟"""


def _schema_of(url: str) -> str:
    """从连接串里取库名：mysql+pymysql://u:p@h:3306/dbname?charset=..."""
    match = re.search(r'://[^/?#]*/([^?#;]*)', url or '')
    if not match:
        return ''
    return match.group(1).strip()


def _strip_credentials(url: str) -> str:
    """去掉账号密码，只留「驱动+主机+库名」，用于同库判定与日志展示"""
    return re.sub(r'://[^/@]*@', '://', url or '')


def resolve_test_db_url() -> str:
    """取测试库连接串：优先 CRYPTO_TEST_DB_URL，其次用业务连接串换库名。

    只给库名的写法（CRYPTO_TEST_DB_SCHEMA=crypto_test）复用业务库的账号与主机，
    好处是密码永远不用出现在命令行、脚本或终端记录里；库名本身仍要过
    `validate_test_db_url` 的测试标记与同库校验，所以并不降低安全性。
    两者都没设置时返回空串 —— 绝不回退到 db_url.txt 指向的业务库。
    """
    url = os.environ.get(TEST_DB_ENV, '').strip()
    if url:
        return url
    schema = os.environ.get(TEST_SCHEMA_ENV, '').strip()
    if not schema:
        return ''
    from . import database
    return _swap_schema(database.resolve_db_url(), schema)


def _swap_schema(url: str, schema: str) -> str:
    """把连接串里的库名换成 schema（保留账号/主机/参数）"""
    if not url or not schema:
        return ''
    try:
        from sqlalchemy.engine import make_url
        return str(make_url(url).set(database=schema))
    except Exception:
        return re.sub(r'(://[^/?#]*/)[^?#;]*', lambda m: m.group(1) + schema,
                      url, count=1)


def validate_test_db_url(test_url: str, business_url: str = None) -> str:
    """校验测试库连接串，返回该连接串；不合规抛 TestDbNotConfigured。

    business_url 缺省时读取当前业务配置（CRYPTO_DB_URL → db_url.txt）。
    """
    test_url = (test_url or '').strip()
    if not test_url:
        raise TestDbNotConfigured(
            f'未设置 {TEST_DB_ENV}（或 {TEST_SCHEMA_ENV}）：写库冒烟必须指向专用测试库，'
            f'禁止回退业务库。示例：{TEST_SCHEMA_ENV}=crypto_test'
            '（复用业务账号/主机，只换库名，密码不进命令行）')

    schema = _schema_of(test_url)
    if not schema:
        raise TestDbNotConfigured(
            f'{TEST_DB_ENV} 连接串里解析不到库名：{_strip_credentials(test_url)}')
    if not _TEST_SCHEMA_PATTERN.search(schema):
        raise TestDbNotConfigured(
            f'测试库库名 {schema!r} 缺少测试标记（test/smoke/ci/sandbox，'
            f'可用分隔符开头或结尾），拒绝在疑似业务库上执行清表冒烟')

    if business_url is None:
        from . import database
        business_url = database.resolve_db_url()
    if business_url and _schema_of(business_url) == schema:
        raise TestDbNotConfigured(
            f'测试库与业务库同名（{schema}），清表冒烟会直接毁坏真实数据，已拒绝')
    if business_url and _strip_credentials(business_url) == _strip_credentials(test_url):
        raise TestDbNotConfigured(
            f'测试库连接串与业务库完全一致（{_strip_credentials(test_url)}），已拒绝')
    return test_url


def _reset_engine():
    """引擎是进程内单例：切换 CRYPTO_DB_URL 后必须丢弃旧引擎与初始化标记"""
    from . import database
    engine = getattr(database, '_engine', None)
    if engine is not None:
        try:
            engine.dispose()
        except Exception as e:      # 旧目标不可达也不能挡住隔离
            logger.warning(f'[TestIsolation] dispose 旧引擎失败（忽略）: {e}')
    lock = getattr(database, '_engine_lock', None) or _nullctx()
    with lock:
        database._engine = None
        database._SessionFactory = None
    database._init_done = False


class _nullctx:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _isolate_data_dir() -> str:
    """把文件型副作用（配置双写、计划备份）挪到临时目录，返回该目录"""
    import tempfile
    current = os.environ.get(DATA_DIR_ENV, '').strip()
    if current and _TEST_SCHEMA_PATTERN.search(os.path.basename(current.rstrip('\\/'))):
        return current
    tmp = tempfile.mkdtemp(prefix='crypto_trade_test_')
    os.environ[DATA_DIR_ENV] = tmp
    return tmp


def _register_sqlite_type_shims():
    """让 MySQL 专有列类型在 SQLite 上也能建表（只影响 SQLite 编译分支）。

    models 里 instinct_embeddings.vec_json 用了 MEDIUMTEXT；SQLite 编译器不认识
    这个类型，create_all 会直接 CompileError，导致本地文件库当隔离冒烟目标时
    第一步就崩。SQLite 的 TEXT 本身无长度上限，映射成 TEXT 不丢语义。
    """
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.dialects.mysql import MEDIUMTEXT

    @compiles(MEDIUMTEXT, 'sqlite')
    def _mediumtext_sqlite(type_, compiler, **kw):  # noqa: ARG001
        return 'TEXT'
    return _mediumtext_sqlite


def ensure_test_schema() -> list:
    """测试库缺表时补建完整表结构（幂等），返回本次新建的表名。

    为什么需要：清表还原型冒烟假设目标库里表已经存在，第一步快照就 SELECT。
    刚由管理员建好的空隔离库、或本地拿 SQLite 文件当隔离库时表是空的，
    冒烟会直接死在 "no such table"。这里只做 create_all（已存在的表自动跳过），
    不会动任何数据；连接串已经过 require_isolated_test_db() 校验并接成
    CRYPTO_DB_URL，所以建表目标必然还是那个隔离库，不可能是业务库。

    必须先调用 require_isolated_test_db()，否则引擎还没指向测试库。
    """
    from sqlalchemy import inspect
    from . import database
    engine = database.get_engine()
    if engine.dialect.name == 'sqlite':
        _register_sqlite_type_shims()
    existing = set(inspect(engine).get_table_names())
    pending = [t.name for t in database.Base.metadata.sorted_tables
               if t.name not in existing]
    if pending:
        database.Base.metadata.create_all(engine)
        logger.info(f'[TestIsolation] 测试库补建 {len(pending)} 张缺失表: '
                    f'{", ".join(sorted(pending))}')
    return pending


def require_isolated_test_db(apply: bool = True) -> str:
    """写库冒烟的统一前置：校验通过返回测试库连接串，否则抛 TestDbNotConfigured。

    apply=True（默认）时同时改写 CRYPTO_DB_URL / CRYPTO_PLAN_DATA_DIR 并重建引擎。
    """
    test_url = resolve_test_db_url()
    validate_test_db_url(test_url)
    if not apply:
        return test_url
    os.environ['CRYPTO_DB_URL'] = test_url
    data_dir = _isolate_data_dir()
    _reset_engine()
    logger.info(f'[TestIsolation] 写库冒烟已隔离：db={_strip_credentials(test_url)} '
                f'data_dir={data_dir}')
    return test_url
