#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
建/重建隔离测试库（MySQL 验收专用，绝不碰业务库）
====================================================
用途：本地与宝塔共用一个线上 MySQL，写库冒烟（清表还原型）与并发/性能验收
都需要一个独立 schema。本脚本只做三件事：

  1. 用 CRYPTO_TEST_DB_SCHEMA（或 CRYPTO_TEST_DB_URL）解析出测试库连接串，
     并交给 crypto/test_isolation.py 的同一套校验：库名必须带
     test/smoke/ci/sandbox 标记、必须与业务库不同名，否则直接拒绝执行；
  2. 建库（字符集/排序规则镜像业务库，避免用错校对规则得出不能代表线上的结论）
     + 按 crypto/models.py 建全表（等价于 db_schema.sql 的权威结构）；
  3. 报告结果（只打印 host/库名，不打印任何凭证）。

用法（PowerShell）：
    $env:CRYPTO_TEST_DB_SCHEMA = 'crypto_test'
    python -B data/make_test_schema.py              # 建库建表（幂等）
    python -B data/make_test_schema.py --rebuild    # 删库重建（仅限带测试标记的库名）
    python -B data/make_test_schema.py --check      # 只连一次做探活与差异报告

--rebuild 为什么允许：能进这里的库名已经过测试标记 + 非业务库双重校验，
删掉的只可能是测试库自己；业务库即便被误填也过不了校验（直接退出 2）。
"""

import os
import sys
import argparse

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sqlalchemy.engine import make_url                      # noqa: E402
from crypto import test_isolation as iso                    # noqa: E402
from crypto.test_isolation import TestDbNotConfigured       # noqa: E402

# 管理员一次性授权脚本（建库权不在应用账号手里时给出该路径）
_SQL_GRANT = os.path.join(_ROOT, 'doc', '隔离测试库授权.sql')


def _resolve_urls():
    """返回 (测试库连接串, 业务库连接串)；校验不通过抛 TestDbNotConfigured"""
    test_url = iso.resolve_test_db_url()
    from crypto.database import resolve_db_url
    business_url = resolve_db_url()
    iso.validate_test_db_url(test_url, business_url=business_url)
    return test_url, business_url


def _server_params(url: str):
    parsed = make_url(url)
    return {
        'host': parsed.host,
        'port': int(parsed.port or 3306),
        'user': parsed.username,
        'password': parsed.password or '',
        'charset': (parsed.query or {}).get('charset', 'utf8mb4'),
        'schema': iso._schema_of(url),
    }


def _connect(params, database=None, connect_timeout=15):
    import pymysql
    return pymysql.connect(host=params['host'], port=params['port'],
                           user=params['user'], password=params['password'],
                           db=database, charset=params['charset'],
                           autocommit=True, connect_timeout=connect_timeout)


class _NoCreatePrivilege(RuntimeError):
    """账号没有建库权（实测应用账号只有 crypto.*）；需要管理员执行一次授权"""


def _open_server_conn(params):
    """优先建立"不带默认库"的服务器级连接（建库/删库必须要）。

    应用账号常见的权限形态是 GRANT ALL ON `crypto`.*，此时不带库名连接会被
    直接拒（1045/1044）。这种情况不代表测试库不可用：如果库已由管理员建好并
    授权，带库名连接就能通。所以这里按「服务器级 → 库级」两级尝试，并明确
    返回是否具备建库能力，供上层决定 --rebuild 是否可执行。
    返回 (conn, can_create_database)。
    """
    try:
        return _connect(params), True
    except Exception as server_err:
        try:
            return _connect(params, database=params['schema']), False
        except Exception as db_err:
            raise _NoCreatePrivilege(
                f'服务器级连接与测试库连接均失败：server={_brief(server_err)}; '
                f'db={_brief(db_err)}') from server_err


def _brief(exc) -> str:
    return (str(exc).splitlines() or [exc.__class__.__name__])[0][:180]


def _business_collation(conn, business_schema: str):
    """读业务库的字符集/排序规则（只读 information_schema），镜像给测试库。

    排序规则不一致会让 LIKE/等值比较的行为偏离线上（例如大小写与重音折叠），
    在测试库上得出的结论就不能外推到业务库。读不到就退回 utf8mb4 通用值。
    """
    fallback = ('utf8mb4', 'utf8mb4_general_ci')
    if not business_schema:
        return fallback
    try:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME '
                'FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s',
                (business_schema,))
            row = cur.fetchone()
        if row and row[0] and row[1]:
            return tuple(row)
    except Exception as e:
        print(f'  · 读取业务库排序规则失败，使用默认值：{e}')
    return fallback


def ensure_database(test_url, business_url, rebuild=False):
    params = _server_params(test_url)
    business_schema = iso._schema_of(business_url or '')
    conn, can_create = _open_server_conn(params)
    try:
        charset, collation = _business_collation(conn, business_schema)
        with conn.cursor() as cur:
            if not can_create:
                # 库级连接能通 = 库已存在且已授权（建库由管理员完成）
                if rebuild:
                    raise _NoCreatePrivilege(
                        f'当前账号无服务器级权限，无法执行 --rebuild（需要 DROP DATABASE）；'
                        f'请改用表级重建或直接复用现有 `{params["schema"]}`')
                print(f'  · 目标库 `{params["schema"]}` 已存在且账号有库级权限'
                      f'（建库由管理员完成，本账号无 CREATE DATABASE 权限）')
                return
            if rebuild:
                cur.execute(f'DROP DATABASE IF EXISTS `{params["schema"]}`')
                print(f'  · 已删除旧测试库 `{params["schema"]}`（--rebuild）')
            cur.execute(
                f'CREATE DATABASE IF NOT EXISTS `{params["schema"]}` '
                f'CHARACTER SET {charset} COLLATE {collation}')
            cur.execute(
                'SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s',
                (params['schema'],))
            assert cur.fetchone(), '建库后仍查不到该库，权限或名称异常'
        print(f'  · 目标库 `{params["schema"]}` 就绪（{charset}/{collation}，'
              f'业务库 `{business_schema}` 未参与写入）')
    finally:
        conn.close()


def create_tables(test_url):
    """在测试库上建全表 + 走一遍应用侧幂等初始化（含增量列/种子）"""
    from crypto import database
    # 硬门禁：引擎实际连的库必须就是测试库。建表是写操作，绝不能因为漏设
    # CRYPTO_DB_URL 而落到业务库上（create_all 会对业务库逐表 SHOW CREATE TABLE）。
    expect = iso._schema_of(test_url)
    actual = iso._schema_of(database.resolve_db_url())
    if actual != expect:
        raise RuntimeError(
            f'引擎指向库 {actual!r}，与测试库 {expect!r} 不一致，拒绝建表')
    database.init_db(force_create=True)
    with database.get_engine().connect() as conn:
        rows = conn.exec_driver_sql('SHOW TABLES').fetchall()
    names = sorted(r[0] for r in rows)
    print(f'  · 表数量 {len(names)}：{", ".join(names)}')
    return names


def summarize(test_url):
    params = _server_params(test_url)
    print(f'[OK] 隔离测试库可用：{params["host"]}:{params["port"]}/{params["schema"]}'
          f'（口令未回显）')


def main():
    parser = argparse.ArgumentParser(description='建/重建 crypto 隔离测试库')
    parser.add_argument('--rebuild', action='store_true', help='先删除测试库再重建')
    parser.add_argument('--check', action='store_true', help='只校验配置与连通性，不建表')
    args = parser.parse_args()

    try:
        test_url, business_url = _resolve_urls()
    except TestDbNotConfigured as e:
        print(f'✗ {e}')
        return 2

    print(f'[TestSchema] 隔离校验通过：{iso._strip_credentials(test_url)}')
    try:
        ensure_database(test_url, business_url, rebuild=args.rebuild)
    except _NoCreatePrivilege as e:
        print(f'✗ {e}')
        print('  应用账号通常只有业务库权限（SHOW GRANTS 可见仅 `crypto`.*），'
              '建库需管理员执行一次：')
        print(f'  1) 用 root 执行 SQL 文件：{_SQL_GRANT}')
        print('  2) 或在宝塔面板 → 数据库 → 添加数据库，库名填测试库同名，'
              '并把该库授权给应用账号')
        print('  完成后重跑本脚本即可（脚本会自动降级为库级连接，不再要求建库权）')
        return 2
    # 建表/探活都必须走测试库：交给守卫统一改写 CRYPTO_DB_URL 并重建引擎，
    # 这样 crypto.database 后续任何连接都不可能碰到业务库。
    iso.require_isolated_test_db()
    if args.check:
        summarize(test_url)
        return 0
    create_tables(test_url)
    summarize(test_url)
    return 0


if __name__ == '__main__':
    sys.exit(main())
