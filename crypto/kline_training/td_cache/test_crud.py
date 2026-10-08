# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/database/test_crud.py  →  现位置: crypto/kline_training/td_cache/test_crud.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
TDengine 数据库 CRUD 完整流程测试

测试步骤（严格按照 查→增→查→改→查→删→查 顺序）：
  1. 查询现有数据（查）
  2. 建库建表并插入测试数据（增）
  3. 查询验证新增数据（查）
  4. 修改测试数据 —— 使用 INSERT upsert（改）
  5. 查询验证修改结果（查）
  6. 删除测试数据 —— 时间戳需转 UTC（删）
  7. 查询验证删除结果（查）
  8. 清理测试环境（删库）

TDengine REST API 注意事项：
  - UPDATE 语句不被 REST API 支持，使用 INSERT（相同 ts 覆盖）代替
  - DELETE WHERE ts 必须使用 UTC 格式（taosrest 返回的是 UTC+8 naive datetime）
  - USE database 不会跨请求保持，需要全限定表名或带 database 参数的连接
"""

import sys
from datetime import datetime, timezone, timedelta

from crypto.kline_training.td_cache import get_connection, TDENGINE_REST_URL, USER, PASSWORD
from taosrest import connect as taos_connect

# ──────────────────────────────────────────────────────
#  测试配置
# ──────────────────────────────────────────────────────
TEST_DB   = "test_crud_db"
STABLE    = "market_data"
CHILD_BIN = "btc_binance"
CHILD_OKX = "eth_okx"

TZ8       = timezone(timedelta(hours=8))
TZ_UTC    = timezone.utc

# ──────────────────────────────────────────────────────
#  测试报告收集
# ──────────────────────────────────────────────────────
results: list[tuple[str, bool]] = []


def log(msg: str):
    print(msg)


def ok(step: str):
    results.append((step, True))
    log(f"  ✅ {step}")


def fail(step: str, err: Exception):
    results.append((step, False))
    log(f"  ❌ {step}: {err}")


def to_utc_str(dt: datetime) -> str:
    """
    将 taosrest 返回的 naive datetime（实际是 UTC+8 时间）
    转为 TDengine DELETE 需要的 UTC ISO 字符串。
    """
    aware = dt.replace(tzinfo=TZ8)
    utc_dt = aware.astimezone(TZ_UTC)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _get_db_conn():
    """获取已绑定数据库的连接"""
    return taos_connect(url=TDENGINE_REST_URL, user=USER, password=PASSWORD, database=TEST_DB)


# ──────────────────────────────────────────────────────
#  1. 查询现有数据（查）
# ──────────────────────────────────────────────────────
def test_read_initial(cur):
    log("\n" + "─" * 55)
    log("📋 Step 1: 查询现有数据（查）")

    cur.execute("SHOW DATABASES")
    dbs = [r[0] for r in cur.fetchall()]
    log(f"  现有数据库: {dbs}")

    if TEST_DB in dbs:
        log(f"  ⚠️  {TEST_DB} 已存在，先清理旧数据")
        cur.execute(f"DROP DATABASE {TEST_DB}")

    cur.execute("SHOW DATABASES")
    dbs = [r[0] for r in cur.fetchall()]
    ok(f"查询完成，当前数据库: {dbs}")


# ──────────────────────────────────────────────────────
#  2. 新增数据（增）
# ──────────────────────────────────────────────────────
def test_create_and_insert(cur):
    log("\n" + "─" * 55)
    log("➕ Step 2: 新增数据（增）")

    # 建库
    cur.execute(f"CREATE DATABASE {TEST_DB} PRECISION 'ms' KEEP 365 DURATION 10")
    ok(f"创建数据库: {TEST_DB}")

    # 建超级表
    cur.execute(f"""
        CREATE STABLE {TEST_DB}.{STABLE} (
            ts     TIMESTAMP,
            price  DOUBLE,
            volume BIGINT,
            symbol NCHAR(20)
        ) TAGS (
            source NCHAR(20)
        )
    """)
    ok(f"创建超级表: {STABLE}")

    # 插入 BTC 数据（BINANCE 子表）
    cur.execute(f"""
        INSERT INTO {TEST_DB}.{CHILD_BIN}
            USING {TEST_DB}.{STABLE} TAGS('BINANCE')
        VALUES
            (NOW,     60000.50, 100, 'BTC'),
            (NOW+1s,  60100.00, 200, 'BTC'),
            (NOW+2s,  59900.75, 150, 'BTC')
    """)
    ok("插入 BTC/BINANCE 数据: 3 条")

    # 插入 ETH 数据（OKX 子表）
    cur.execute(f"""
        INSERT INTO {TEST_DB}.{CHILD_OKX}
            USING {TEST_DB}.{STABLE} TAGS('OKX')
        VALUES
            (NOW,     3000.25, 500, 'ETH'),
            (NOW+1s,  3010.00, 600, 'ETH'),
            (NOW+2s,  2990.50, 450, 'ETH')
    """)
    ok("插入 ETH/OKX 数据: 3 条")


# ──────────────────────────────────────────────────────
#  3. 查询验证新增数据（查）
# ──────────────────────────────────────────────────────
def test_verify_insert(cur):
    log("\n" + "─" * 55)
    log("📋 Step 3: 查询验证新增数据（查）")

    # 全表查询
    cur.execute(f"SELECT * FROM {TEST_DB}.{STABLE} ORDER BY ts")
    rows = cur.fetchall()
    log(f"  全表数据共 {len(rows)} 条:")
    for row in rows:
        log(f"    {row}")

    if len(rows) != 6:
        raise AssertionError(f"期望 6 条，实际 {len(rows)} 条")

    # 子表计数
    cur.execute(f"SELECT count(*) FROM {TEST_DB}.{CHILD_BIN}")
    cnt_bin = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {TEST_DB}.{CHILD_OKX}")
    cnt_okx = cur.fetchone()[0]

    if cnt_bin != 3 or cnt_okx != 3:
        raise AssertionError(f"BTC:{cnt_bin} ETH:{cnt_okx}，期望各 3 条")

    # 按标签过滤
    cur.execute(f"SELECT * FROM {TEST_DB}.{STABLE} WHERE source='BINANCE'")
    log(f"  BINANCE 标签过滤: {len(cur.fetchall())} 条")
    cur.execute(f"SELECT * FROM {TEST_DB}.{STABLE} WHERE source='OKX'")
    log(f"  OKX 标签过滤: {len(cur.fetchall())} 条")

    ok(f"验证通过: BTC={cnt_bin}条  ETH={cnt_okx}条  共{len(rows)}条")


# ──────────────────────────────────────────────────────
#  4. 修改数据（改）—— 使用 INSERT upsert
# ──────────────────────────────────────────────────────
def test_update(db_cur):
    log("\n" + "─" * 55)
    log("✏️  Step 4: 修改数据（改）— INSERT upsert")

    # 查出 BTC 第一条
    db_cur.execute(f"SELECT ts, price, volume, symbol FROM {CHILD_BIN} ORDER BY ts LIMIT 1")
    first = db_cur.fetchone()
    old_ts, old_price, old_vol, old_sym = first
    log(f"  修改前: ts={old_ts}  price={old_price}  volume={old_vol}")

    # taosrest 返回 UTC+8 naive datetime，必须转回 UTC 才能匹配原始 ts
    ts_str = to_utc_str(old_ts)
    new_price = 65000.00
    new_vol   = 999

    # TDengine REST API 不支持 UPDATE，使用 INSERT upsert（同 ts 覆盖）
    db_cur.execute(f"""
        INSERT INTO {CHILD_BIN}
        VALUES ('{ts_str}', {new_price}, {new_vol}, '{old_sym}')
    """)
    ok(f"INSERT upsert: price {old_price} → {new_price}, volume {old_vol} → {new_vol}")

    # 修改 OKX 第一条
    db_cur.execute(f"SELECT ts, price, symbol FROM {CHILD_OKX} ORDER BY ts LIMIT 1")
    okx_first = db_cur.fetchone()
    okx_ts, okx_price, okx_sym = okx_first
    okx_ts_str = to_utc_str(okx_ts)

    db_cur.execute(f"""
        INSERT INTO {CHILD_OKX}
        VALUES ('{okx_ts_str}', 3500.00, 888, '{okx_sym}')
    """)
    ok(f"INSERT upsert OKX: price {okx_price} → 3500.00")


# ──────────────────────────────────────────────────────
#  5. 查询验证修改结果（查）
# ──────────────────────────────────────────────────────
def test_verify_update(db_cur):
    log("\n" + "─" * 55)
    log("📋 Step 5: 查询验证修改结果（查）")

    db_cur.execute(f"SELECT ts, price, volume, symbol FROM {CHILD_BIN} ORDER BY ts")
    rows = db_cur.fetchall()
    log(f"  {CHILD_BIN} 当前数据:")
    for row in rows:
        log(f"    {row}")

    first = rows[0]
    if float(first[1]) != 65000.00 or int(first[2]) != 999:
        raise AssertionError(f"BTC 更新失败: price={first[1]}, volume={first[2]}")

    db_cur.execute(f"SELECT ts, price, volume, symbol FROM {CHILD_OKX} ORDER BY ts")
    okx_rows = db_cur.fetchall()
    log(f"  {CHILD_OKX} 当前数据:")
    for row in okx_rows:
        log(f"    {row}")

    okx_first = okx_rows[0]
    if float(okx_first[1]) != 3500.00 or int(okx_first[2]) != 888:
        raise AssertionError(f"OKX 更新失败: price={okx_first[1]}, volume={okx_first[2]}")

    ok("验证通过: BTC price=65000/vol=999  |  ETH price=3500/vol=888")


# ──────────────────────────────────────────────────────
#  6. 删除测试数据（删）—— 时间戳必须转 UTC
# ──────────────────────────────────────────────────────
def test_delete(db_cur):
    log("\n" + "─" * 55)
    log("🗑️  Step 6: 删除测试数据（删）")

    # 查出 BTC 第一条的时间戳
    db_cur.execute(f"SELECT ts FROM {CHILD_BIN} ORDER BY ts LIMIT 1")
    del_ts = db_cur.fetchone()[0]
    del_ts_utc = to_utc_str(del_ts)
    log(f"  待删除: {CHILD_BIN} ts={del_ts} → UTC: {del_ts_utc}")

    db_cur.execute(f"DELETE FROM {CHILD_BIN} WHERE ts='{del_ts_utc}'")
    ok(f"删除 {CHILD_BIN} 中 1 条记录")

    # 删除 OKX 全部记录（逐条删除）
    db_cur.execute(f"SELECT ts FROM {CHILD_OKX} ORDER BY ts")
    okx_timestamps = [to_utc_str(row[0]) for row in db_cur.fetchall()]
    for ts_utc in okx_timestamps:
        db_cur.execute(f"DELETE FROM {CHILD_OKX} WHERE ts='{ts_utc}'")
    ok(f"删除 {CHILD_OKX} 全部 {len(okx_timestamps)} 条记录")


# ──────────────────────────────────────────────────────
#  7. 查询验证删除结果（查）
# ──────────────────────────────────────────────────────
def test_verify_delete(db_cur):
    log("\n" + "─" * 55)
    log("📋 Step 7: 查询验证删除结果（查）")

    db_cur.execute(f"SELECT count(*) FROM {CHILD_BIN}")
    cnt_bin = db_cur.fetchone()[0]
    db_cur.execute(f"SELECT count(*) FROM {CHILD_OKX}")
    cnt_okx = db_cur.fetchone()[0]

    log(f"  {CHILD_BIN} 剩余: {cnt_bin} 条")
    log(f"  {CHILD_OKX} 剩余: {cnt_okx} 条")

    if cnt_bin != 2:
        raise AssertionError(f"BTC 期望剩余 2 条，实际 {cnt_bin} 条")
    if cnt_okx != 0:
        raise AssertionError(f"OKX 期望剩余 0 条，实际 {cnt_okx} 条")

    ok(f"验证通过: BTC={cnt_bin}条  ETH={cnt_okx}条（符合预期）")


# ──────────────────────────────────────────────────────
#  8. 清理测试环境
# ──────────────────────────────────────────────────────
def test_cleanup(cur):
    log("\n" + "─" * 55)
    log("🧹 Step 8: 清理测试环境")

    cur.execute(f"DROP DATABASE {TEST_DB}")

    cur.execute("SHOW DATABASES")
    dbs = [r[0] for r in cur.fetchall()]
    if TEST_DB in dbs:
        raise AssertionError(f"{TEST_DB} 删除后仍然存在")

    ok(f"数据库 {TEST_DB} 已清理，剩余: {dbs}")


# ──────────────────────────────────────────────────────
#  主流程
# ──────────────────────────────────────────────────────
def main():
    log("=" * 55)
    log("   TDengine 数据库 CRUD 完整流程测试")
    log("=" * 55)

    # 全局连接（用于 SHOW/DROP/CREATE DATABASE）
    global_conn = get_connection()
    global_cur  = global_conn.cursor()

    # ── Step 1: 查 ──
    try:
        test_read_initial(global_cur)
    except Exception as e:
        fail("Step 1 查询现有数据", e)

    # ── Step 2: 增 ──
    try:
        test_create_and_insert(global_cur)
    except Exception as e:
        fail("Step 2 新增数据", e)

    # ── Step 3: 查 ──
    try:
        test_verify_insert(global_cur)
    except Exception as e:
        fail("Step 3 查询验证新增", e)

    # 切换到带 database 参数的连接（后续操作需要）
    db_conn = _get_db_conn()
    db_cur  = db_conn.cursor()

    # ── Step 4: 改 ──
    try:
        test_update(db_cur)
    except Exception as e:
        fail("Step 4 修改数据", e)

    # ── Step 5: 查 ──
    try:
        test_verify_update(db_cur)
    except Exception as e:
        fail("Step 5 查询验证修改", e)

    # ── Step 6: 删 ──
    try:
        test_delete(db_cur)
    except Exception as e:
        fail("Step 6 删除数据", e)

    # ── Step 7: 查 ──
    try:
        test_verify_delete(db_cur)
    except Exception as e:
        fail("Step 7 查询验证删除", e)

    db_conn.close()

    # ── Step 8: 清理 ──
    try:
        test_cleanup(global_cur)
    except Exception as e:
        fail("Step 8 清理环境", e)

    global_conn.close()

    # ── 测试报告 ──
    log("\n" + "═" * 55)
    log("   📊 测试报告")
    log("═" * 55)
    total  = len(results)
    passed = sum(1 for _, p in results if p)
    failed = total - passed
    for name, p in results:
        log(f"  {'✅' if p else '❌'} {name}")
    log(f"\n  总计: {total} 项  |  通过: {passed}  |  失败: {failed}")
    if failed == 0:
        log("\n  🎉 所有测试全部通过！")
    log("═" * 55)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

