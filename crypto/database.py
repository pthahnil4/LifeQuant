#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MySQL 数据库连接层（全局共享）
================================
连接串解析优先级：
  1. 环境变量 CRYPTO_DB_URL（最高优先，宝塔面板可配置）
     格式: mysql+pymysql://用户:密码@主机:端口/库名?charset=utf8mb4
  2. 数据目录下的 db_url.txt 文件（单行连接串，便于宝塔免环境变量部署）
     数据目录：环境变量 CRYPTO_PLAN_DATA_DIR → 缺省项目内 data/ 目录

设计约定：
- 连接池开启 pool_pre_ping，防止 MySQL wait_timeout 造成的僵死连接
- 建表权威来源为项目根目录 db_schema.sql；应用运行时 init_db() 仅做
  连通性探活 + 种子补全 + 存量索引自动补齐（不再逐表 create_all），
  完整建表仅迁移脚本 force_create 模式执行
"""

import os
import logging
import datetime
import threading
import time

from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import sessionmaker, declarative_base

from .db_performance import TimedQueuePool, instrument_engine, measure_commit

logger = logging.getLogger(__name__)

Base = declarative_base()

SCHEMA_VERSION = '1.0.0'

# 与 data_paths.resolve_data_dir 相同的数据目录解析（避免循环导入，独立实现）
def _resolve_data_dir() -> str:
    env_dir = os.environ.get('CRYPTO_PLAN_DATA_DIR', '').strip()
    if env_dir:
        return env_dir
    # 缺省项目内 data/ 目录（crypto/ 的上级即项目根）
    _crypto_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(_crypto_dir), 'data')


def resolve_db_url() -> str:
    """解析数据库连接串；未配置时返回空字符串（调用方负责报错提示）"""
    url = os.environ.get('CRYPTO_DB_URL', '').strip()
    if url:
        return url
    data_dir = _resolve_data_dir()
    if data_dir:
        url_file = os.path.join(data_dir, 'db_url.txt')
        if os.path.isfile(url_file):
            try:
                with open(url_file, 'r', encoding='utf-8') as f:
                    url = f.readline().strip()
            except OSError as e:
                logger.warning(f"[DB] 读取 db_url.txt 失败: {e}")
    return url


_engine = None
_SessionFactory = None
_engine_lock = threading.Lock()


# ---------------------------------------------------------------------
# DB 连接健康度（供"持久化降级"旁路告警读取，见 trend_range_trader P2-c）
# 只在 session_scope 出入口被动更新，零额外开销；跨线程共享故加锁。
# consecutive_failures 统计"连续"连接类失败次数，任一次成功 commit 即清零，
# 因此偶发抖动不会累积、只有持续故障（DB 真挂了）才会爬升。
# ---------------------------------------------------------------------
_db_health_lock = threading.Lock()
_DB_HEALTH = {
    'consecutive_failures': 0,   # 连续连接类失败次数（健康度主指标）
    'total_failures': 0,         # 进程累计连接类失败次数
    'last_ok_ts': 0.0,           # 最近一次成功 commit 的时间戳
    'last_error': '',            # 最近一次连接类失败摘要
}


def _is_conn_error(exc: BaseException) -> bool:
    """是否"连接/可用性"类数据库故障（区别于约束冲突等业务数据错误）。

    业务错误说明 DB 其实可达（只是这条写失败），不应记为链路降级；只有连接层
    异常（OperationalError/InterfaceError/DisconnectionError/TimeoutError）才
    意味着持久化真的用不了。延迟导入 sqlalchemy.exc，导入失败时保守判 False。
    """
    try:
        from sqlalchemy.exc import (
            OperationalError, InterfaceError, DisconnectionError, TimeoutError as SATimeout)
        conn_types = (OperationalError, InterfaceError, DisconnectionError, SATimeout)
    except Exception:
        return False
    if isinstance(exc, conn_types):
        return True
    # DBAPI 层原始异常（pymysql 未包装的连接错误）按类名兜底识别
    name = type(exc).__name__.lower()
    return any(k in name for k in ('operationalerror', 'interfaceerror',
                                   'disconnection', 'connectionerror', 'timeout'))


def _db_note_success():
    with _db_health_lock:
        _DB_HEALTH['consecutive_failures'] = 0
        _DB_HEALTH['last_ok_ts'] = time.time()


def _db_note_failure(exc: BaseException):
    with _db_health_lock:
        _DB_HEALTH['consecutive_failures'] += 1
        _DB_HEALTH['total_failures'] += 1
        _DB_HEALTH['last_error'] = (str(exc).splitlines() or [''])[0][:200]


def db_health() -> dict:
    """返回 DB 连接健康度快照（只读副本）：连续失败数 / 累计失败数 / 最近成功时间 / 最近错误。"""
    with _db_health_lock:
        return dict(_DB_HEALTH)


def _connect_args(url: str) -> dict:
    """按方言给出建连参数。

    connect_timeout 是 MySQL 驱动专有入参：非 MySQL 目标（本地 SQLite 隔离冒烟）
    一并传过去会直接 TypeError 建不出引擎，所以只对 MySQL/MariaDB 下发。
    MySQL 侧含义不变：DB 故障时 5 秒内快速失败，不阻塞启动与请求。
    """
    try:
        if make_url(url).get_dialect().name not in ('mysql', 'mariadb'):
            return {}
    except Exception:
        return {}
    return {'connect_timeout': 5}


def _make_engine():
    url = resolve_db_url()
    if not url:
        raise RuntimeError(
            '数据库未配置：请设置环境变量 CRYPTO_DB_URL，'
            '或在外置数据目录创建 db_url.txt（单行连接串，'
            '格式 mysql+pymysql://用户:密码@主机:端口/库名?charset=utf8mb4）')
    engine = create_engine(
        url,
        poolclass=TimedQueuePool,
        pool_pre_ping=True,     # 取连接前先探活，规避 MySQL 超时断开
        pool_recycle=1800,      # 连接最长存活 30 分钟后强制换新
        pool_size=10,           # 常驻连接扩容，减少 overflow 连接的反复销毁/重建握手
        max_overflow=5,         # overflow 仅作突发缓冲（其连接用完即销毁，代价高）
        pool_timeout=30,
        pool_use_lifo=True,     # 后进先出复用热连接；checkout 仍会执行 pre_ping
        connect_args=_connect_args(url),
        future=True)
    instrument_engine(engine)
    return engine


def get_engine():
    """获取全局引擎；初始化期间不发布尚未就绪的会话工厂。"""
    global _engine, _SessionFactory
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                engine = _make_engine()
                _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
                _engine = engine
    return _engine


def get_session_factory():
    get_engine()
    return _SessionFactory


def get_session():
    """获取会话（调用方负责 commit/rollback/close，推荐用 session_scope）"""
    return get_session_factory()()


class session_scope:
    """会话上下文管理器：正常退出自动 commit，异常自动 rollback"""

    def __init__(self):
        self.session = None

    def __enter__(self):
        try:
            self.session = get_session()
        except Exception as e:
            # 连会话都建不出来：几乎必是连接/引擎层故障，记一次 DB 失败后原样抛出
            _db_note_failure(e)
            raise
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                try:
                    with measure_commit():
                        self.session.commit()
                except Exception as e:
                    # commit 失败：区分"连接类故障"（计入 DB 健康度）与"业务/数据错误"
                    # （如约束冲突，说明 DB 其实可达，只是这次写失败，不算链路降级）
                    if _is_conn_error(e):
                        _db_note_failure(e)
                    raise
                _db_note_success()
            else:
                self.session.rollback()
        finally:
            self.session.close()
        return False


_init_lock = threading.Lock()
_init_done = False

# 存量表增量索引补丁（新建库由 create_all 含 models.Index 定义自动建出；
# 存量库的缺失索引在首次 init_db 时自动补齐，每进程仅检查一次）
_REQUIRED_INDEXES = (
    ('trade_journal', 'idx_tj_inst_ts', '(`inst_id`, `ts`)'),
    ('alert_log', 'idx_alert_inst_time', '(`inst_id`, `created_at`)'),
    # 批次11 分析纪律：按小时槽聚合分析记录（闸门/巡检最高频查询）
    ('task_analysis_records', 'idx_tar_slot', '(`hour_slot`)'),
)

# 存量表增量列补丁（后续迭代新增的列，存量库首次 init_db 时自动补齐，
# 新建库由 db_schema.sql / create_all 直接含列；每进程仅检查一次）
# - crypto_coins.m15_*：批量多周期趋势分析新增 15 分钟周期（与 models.CryptoCoin 对齐）
# - crypto_coins.*_er：批量多周期趋势分析新增 Kaufman 效率系数 ER 列
# - 批次11 分析纪律：task_analysis_records 增 hour_slot/source；
#   plan_slots 增打卡与分析记录的关联列
_REQUIRED_COLUMNS = (
    ('crypto_coins', 'm15_trend', "VARCHAR(8) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_price', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_time', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_profit', "VARCHAR(16) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_close', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_macd', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_dif', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_adx', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_atr', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_sar', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_sar_color', "VARCHAR(8) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'h1_er', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'h4_er', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'd1_er', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('crypto_coins', 'm15_er', "VARCHAR(24) NOT NULL DEFAULT ''"),
    ('task_analysis_records', 'hour_slot', "VARCHAR(13) NOT NULL DEFAULT ''"),
    ('task_analysis_records', 'source', "VARCHAR(16) NOT NULL DEFAULT 'live'"),
        # 批次16：分析记录先触碰判定 + 结构价计划（存量库自动补列，新库由 schema 直含）
        ('task_analysis_records', 'touch_near', "VARCHAR(8) NULL"),
        ('task_analysis_records', 'touch_near_ts', "VARCHAR(19) NULL"),
        ('task_analysis_records', 'touch_far', "VARCHAR(8) NULL"),
        ('task_analysis_records', 'touch_far_ts', "VARCHAR(19) NULL"),
        ('task_analysis_records', 'target_price', "DOUBLE NULL"),
        ('task_analysis_records', 'invalid_price', "DOUBLE NULL"),
        ('task_analysis_records', 'plan_result', "VARCHAR(8) NULL"),
        ('task_analysis_records', 'plan_r', "DOUBLE NULL"),
        ('task_analysis_records', 'plan_touch_ts', "VARCHAR(19) NULL"),
    ('plan_slots', 'analysis_ids', "VARCHAR(255) NOT NULL DEFAULT ''"),
    ('plan_slots', 'analysis_hour', "VARCHAR(13) NOT NULL DEFAULT ''"),
    ('plan_slots', 'bypass_analysis', "TINYINT(1) NOT NULL DEFAULT 0"),
    # 任务管理 v2（任务树）：plan_cards.tasks 整树 JSON；plan_slots.task_links
    # 打卡关联 [{task_id,state}]。TEXT 可空（NULL/'' 视同未迁移/待关联）
    ('plan_cards', 'tasks', "TEXT NULL"),
    ('plan_slots', 'task_links', "TEXT NULL"),
    # 热量模块明细结构扩展：食物支持「数量 × 单位热量 = 总热量」，
    # 存量行 quantity 缺省 1（原 calories 字段即该行总摄入，语义兼容）
    ('calorie_meal_items', 'quantity', "FLOAT NOT NULL DEFAULT 1"),
    ('calorie_meal_items', 'unit', "VARCHAR(32) NOT NULL DEFAULT ''"),
    # 双仓位出场增强：tp_runtime_state 新增 extra 列存放 combo 状态 JSON（P0/ATR0/R0/protect
    # /partial_done 等）。NULL 与老行为兼容（仅 ladder_done 老字段可用），新模式写入时自动填充。
    ('tp_runtime_state', 'extra', "TEXT NULL"),
    # 批次15 每日评级总控台：打卡记录关联当日日记条目 id（可空，提交时定格回填）。
    # 存量库首次启动自动补列；新建库由 db_schema.sql CREATE 语句直接含该列。
    ('daily_checkins', 'diary_entry_id', "VARCHAR(32) NOT NULL DEFAULT ''"),
    # 支出模块明细新增「标题/摘要」列：每笔记录显式展示用途背景（如订阅费/差旅报销）。
    # 存量库首次启动自动补列，空串与老行为兼容（前端回退显示占位符）。
    ('expense_records', 'title', "VARCHAR(120) NOT NULL DEFAULT ''"),
)

# 新增表自动补建（init_db 默认模式不再逐表 create_all，这里只对
# 后续迭代新增的表做 checkfirst 建表，存量库升级时无需手工跑 SQL）
_NEW_TABLE_NAMES = (
    'analysis_reminder_log',   # 批次11 分析纪律小时槽台账
    # 批次12 盘感模拟模块（RAG + LLM Wiki，见 doc/RAG_LLM_Wiki模拟盘感落地方案.md）
    'instinct_corpus',
    'instinct_wiki_rules',
    'instinct_predictions',
    'instinct_embeddings',
    # 支出模块独立表；只在启动迁移补建，不在页面 GET 中初始化。
    'expense_settings', 'expense_categories', 'expense_tags', 'expense_periods',
    'expense_bill_templates', 'expense_bill_occurrences', 'expense_records',
    'expense_record_tags', 'expense_daily_checks', 'expense_change_logs',
    # 日记本模块（diary_*）：设置/条目/目标倒计时，同样只在启动迁移补建。
    'diary_settings', 'diary_entries', 'diary_goals',
    # 美食打卡点模块（calorie_food_spot_*）：探店记录 + 照片索引，只在启动迁移补建。
    'calorie_food_spots', 'calorie_food_spot_photos',
    # 批次15 每日多维打卡：打卡记录 + 交易指标按日缓存，只在启动迁移补建。
    'daily_checkins', 'trading_daily_snapshots',
)

# 存量表废弃列退役补丁（架构调整后不再使用的列：先把存量数据迁移到
# 新归宿，再 DROP COLUMN；每进程仅执行一次，列不存在时直接跳过）
# - calorie_records.diary：热量记录内嵌随笔已废弃，随笔统一归入独立的
#   随笔模块（journal_notes），以 linked_from='calorie:YYYY-MM-DD' 回指热量记录日期
_LEGACY_COLUMNS = (
    ('calorie_records', 'diary'),
)


def _ensure_extra_indexes(engine):
    """检查增量索引是否存在，缺失则自动补建（information_schema 查询）。

    逐个索引独立提交：单个索引补建失败（如依赖列本次未补上）不得
    连带影响其余索引。MySQL DDL 本身就是隐式提交，包在同一事务里并无收益。
    """
    from sqlalchemy import text
    for table, name, cols in _REQUIRED_INDEXES:
        try:
            with engine.begin() as conn:
                exists = conn.execute(text(
                    'SELECT COUNT(*) FROM information_schema.statistics '
                    'WHERE table_schema = DATABASE() AND table_name = :t '
                    'AND index_name = :i'), {'t': table, 'i': name}).scalar()
                if not exists:
                    conn.execute(text(
                        f'ALTER TABLE `{table}` ADD INDEX `{name}` {cols}'))
                    logger.info('[DB] 已补建存量表索引: %s.%s', table, name)
        except Exception as e:
            logger.warning('[DB] 补建索引失败 %s.%s（不影响启动）: %s', table, name, e)


def _ensure_extra_columns(engine):
    """检查增量列是否存在，缺失则自动补建（information_schema 查询）。

    逐个列独立提交，与 _ensure_extra_indexes 同理。
    """
    from sqlalchemy import text
    for table, col, ddl in _REQUIRED_COLUMNS:
        try:
            with engine.begin() as conn:
                exists = conn.execute(text(
                    'SELECT COUNT(*) FROM information_schema.columns '
                    'WHERE table_schema = DATABASE() AND table_name = :t '
                    'AND column_name = :c'), {'t': table, 'c': col}).scalar()
                if not exists:
                    conn.execute(text(
                        f'ALTER TABLE `{table}` ADD COLUMN `{col}` {ddl}'))
                    logger.info('[DB] 已补建存量表列: %s.%s', table, col)
        except Exception as e:
            logger.warning('[DB] 补建列失败 %s.%s（不影响启动）: %s', table, col, e)


def _ensure_extra_tables(engine):
    """补建后续迭代新增的表（checkfirst，已存在则零 DDL）"""
    from . import models  # noqa: F401  确保模型已注册到 Base.metadata
    tables = [Base.metadata.tables[name] for name in _NEW_TABLE_NAMES
              if name in Base.metadata.tables]
    if tables:
        Base.metadata.create_all(engine, tables=tables, checkfirst=True)


def _backfill_analysis_hour_slot(engine):
    """存量分析记录回填 hour_slot（取 ts 前 13 位 'YYYY-MM-DD HH'）。

    幂等：仅更新 hour_slot 为空的行，全部回填后后续启动为零影响 UPDATE。
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        result = conn.execute(text(
            "UPDATE task_analysis_records SET hour_slot = LEFT(ts, 13) "
            "WHERE (hour_slot IS NULL OR hour_slot = '') AND ts <> ''"))
        if result.rowcount:
            logger.info('[DB] 已回填存量分析记录 hour_slot: %s 行', result.rowcount)


def _column_exists(conn, table, col):
    """information_schema 检查列是否存在"""
    from sqlalchemy import text
    return bool(conn.execute(text(
        'SELECT COUNT(*) FROM information_schema.columns '
        'WHERE table_schema = DATABASE() AND table_name = :t '
        'AND column_name = :c'), {'t': table, 'c': col}).scalar())


def _migrate_calorie_diary_to_journal(conn):
    """把 calorie_records.diary 存量随笔迁移为独立随笔条目（幂等）

    - 每条非空 diary 转为 journal_notes 一条随笔，打『减肥随笔』标签，
      linked_from='calorie:{记录日期}' 回指热量记录，支持双向跳转；
    - 幂等去重：同 linked_from + 同正文已存在则跳过（重复执行不重复插入）。
    注：原始 SQL 插入需显式带上时间列（表列无数据库默认值）。
    """
    import uuid
    import datetime
    from sqlalchemy import text

    now_ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    rows = conn.execute(text(
        'SELECT `date`, `diary`, `created_at` FROM calorie_records '
        'WHERE diary IS NOT NULL AND TRIM(diary) <> \'\'')).all()
    if not rows:
        return 0

    # 确保『减肥随笔』标签存在（色值取色板中未被占用的第一个）
    tag_name, tag_color = '减肥随笔', '#16a085'
    if conn.execute(text(
            'SELECT COUNT(*) FROM journal_tags WHERE `name` = :n'),
            {'n': tag_name}).scalar() == 0:
        used = {r[0] for r in conn.execute(
            text('SELECT color FROM journal_tags')).all()}
        for c in ('#16a085', '#d81b60', '#5c6bc0', '#f39c12', '#00897b', '#8d6e63'):
            if c not in used:
                tag_color = c
                break
        conn.execute(text(
            'INSERT INTO journal_tags (`name`, `color`, `created_at`) '
            'VALUES (:n, :c, :ts)'),
            {'n': tag_name, 'c': tag_color, 'ts': now_ts})

    migrated = 0
    for rec_date, diary, created_at in rows:
        content = (diary or '').strip()
        if not content:
            continue
        linked_from = f'calorie:{rec_date}'
        dup = conn.execute(text(
            'SELECT COUNT(*) FROM journal_notes '
            'WHERE linked_from = :lf AND content = :c'),
            {'lf': linked_from, 'c': content}).scalar()
        if dup:
            continue
        ts = created_at or f'{rec_date} 20:00:00'
        note_id = f'note_{uuid.uuid4().hex[:8]}'
        conn.execute(text(
            'INSERT INTO journal_notes '
            '(id, `type`, content, pinned, distilled, linked_from, created_at, updated_at) '
            'VALUES (:id, \'note\', :c, 0, 0, :lf, :ts, :ts)'),
            {'id': note_id, 'c': content, 'lf': linked_from, 'ts': ts})
        conn.execute(text(
            'INSERT INTO note_tags (note_id, tag_name) VALUES (:id, :t)'),
            {'id': note_id, 't': tag_name})
        migrated += 1
    if migrated:
        logger.info('[DB] 已将 %d 条热量记录随笔迁移至随笔模块（标签：%s）',
                    migrated, tag_name)
    return migrated


def _drop_legacy_columns(engine):
    """废弃列退役：先迁移存量数据到新归宿，再删除列（幂等，列不存在即跳过）"""
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, col in _LEGACY_COLUMNS:
            if not _column_exists(conn, table, col):
                continue
            if table == 'calorie_records' and col == 'diary':
                _migrate_calorie_diary_to_journal(conn)
            conn.execute(text(f'ALTER TABLE `{table}` DROP COLUMN `{col}`'))
            logger.info('[DB] 已退役存量表废弃列: %s.%s', table, col)


def init_db(force_create=False):
    """幂等初始化（进程内只真正执行一次，重复调用直接返回）。

    - 默认模式：建表由 db_schema.sql 负责，这里仅做一次廉价连通性探活
      （SELECT 1）+ 缺失时补 meta 版本与随笔种子标签。避免 create_all
      逐表 SHOW CREATE TABLE 探测在远端 MySQL 上产生数十次串行往返。
    - force_create=True：仅迁移脚本/全新部署时使用，执行完整 create_all。
    """
    global _init_done
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return
        from . import models  # noqa: F401  确保全部模型注册到 Base.metadata

        engine = get_engine()
        if force_create:
            Base.metadata.create_all(engine)
        else:
            from sqlalchemy import text
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
            # 存量库增量表/列/索引自动补齐 + 废弃列退役迁移（每进程一次，通常全部命中直接返回）
            # 顺序不可调换：索引可能建在增量列上（idx_tar_slot → hour_slot），
            # 必须先补列再补索引；每步独立兜底，任一步失败不得阻断后续步骤。
            for step in (_ensure_extra_tables, _ensure_extra_columns, _ensure_extra_indexes,
                         _backfill_analysis_hour_slot, _drop_legacy_columns):
                try:
                    step(engine)
                except Exception as e:
                    logger.warning(f'[DB] {step.__name__} 执行失败（不影响启动）: {e}')

        from sqlalchemy import select
        from .models import Meta, JournalTag

        with session_scope() as session:
            version = session.get(Meta, 'schema_version')
            if version is None:
                session.add(Meta(key='schema_version', value=SCHEMA_VERSION))
                session.flush()
            else:
                version.value = SCHEMA_VERSION
            init_ts = session.get(Meta, 'initialized_at')
            if init_ts is None:
                session.add(Meta(key='initialized_at',
                                 value=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

            # 随笔模块种子：标签表为空时预置 6 个默认标签（与 JSON 版 _default_data 一致）
            if session.execute(select(JournalTag.name).limit(1)).first() is None:
                from .journal_repo import DEFAULT_TAGS
                session.add_all([JournalTag(name=t['name'], color=t['color'])
                                 for t in DEFAULT_TAGS])
        _init_done = True
        logger.info('[DB] 初始化完成（schema %s）', SCHEMA_VERSION)


def warmup_async(attempts=4, first_wait=10.0):
    """后台预热：应用启动时在守护线程里完成 init_db，失败则退避重试。

    为什么要重试，而不是"等首个请求再试"：库在公网（远端 MySQL），启动瞬间一次
    几十秒的链路抖动就会让进程长时间带着未初始化状态跑 —— 期间"每进程一次"的
    增量列/索引补齐、种子补全全都没做，第一个撞上它的请求要么报错要么走慢路径。
    重试只发生在后台线程的 sleep 里，绝不阻塞 app.run()。

    失败默认只打一行不打栈：预热失败绝大多数是网络抖动，几十行 pymysql 调用栈
    会把同屏的真故障淹没（2026-09-11 终端里那一坨 2013/10060 就是这种噪声）。
    需要看栈时设 CRYPTO_DB_WARM_TRACE=1。
    """
    def _warm():
        wait = float(first_wait)
        for i in range(1, int(attempts) + 1):
            try:
                init_db()
                if i > 1:
                    logger.info(f'[DB] 后台预热第 {i} 次尝试成功')
                return
            except Exception as e:
                brief = str(e).splitlines()[0][:200] if str(e) else e.__class__.__name__
                if i < attempts:
                    logger.warning(f'[DB] 后台预热第 {i}/{attempts} 次失败：{brief}'
                                   f'，{wait:g}s 后重试')
                    time.sleep(wait)
                    wait = min(wait * 2, 120.0)
                else:
                    logger.error(
                        f'[DB] 后台预热 {attempts} 次均未成功：{brief}'
                        f'（首个 DB 请求时仍会重试）',
                        exc_info=bool(os.environ.get('CRYPTO_DB_WARM_TRACE')))

    threading.Thread(target=_warm, daemon=True, name='db-warmup').start()
