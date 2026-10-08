# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/database/import_csv.py  →  现位置: crypto/kline_training/td_cache/import_csv.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
TDengine CSV 批量导入工具

将 NEAR/USDT 1分钟 K线 CSV 数据高效导入 TDengine。
使用原生 REST API (urllib) 以获得最佳性能，绕过 taosrest 游标开销。

CSV 结构：
  timestamp, datetime, open, high, low, close, volume, exchange

用法：
  python database/import_csv.py           # 执行导入
  python database/import_csv.py --verify  # 仅验证（不导入）
"""

import os
import sys
import csv
import time
import base64
import urllib.request
import json
from datetime import datetime, timezone
from pathlib import Path

# 凭据统一来自 crypto.kline_training.settings（只读环境变量，无代码内默认值）
from crypto.kline_training import settings as config

TDENGINE_REST_URL = config.TDENGINE_REST_URL
USER = config.TDENGINE_USER
PASSWORD = config.TDENGINE_PASSWORD

# ──────────────────────────────────────────────────────
#  配置
# ──────────────────────────────────────────────────────
# CSV 路径必须显式给出（原来写死了另一个项目的绝对路径 d:\python\OkxTrade\...，
# 合并后不再假设外部项目存在）：环境变量 CRYPTO_TD_NEAR_CSV 或 FST_NEAR_CSV。
_CSV_SOURCE = (os.environ.get('CRYPTO_TD_NEAR_CSV')
               or os.environ.get('FST_NEAR_CSV') or '')
CSV_FILE = Path(_CSV_SOURCE)
DATABASE = config.TDENGINE_DATABASE
STABLE = config.TDENGINE_STABLE
CHILD = config.TDENGINE_CHILD          # NEAR/USDT on OKX
BATCH_SIZE = 2000                # 每批行数
MAX_RETRIES = 3
RETRY_DELAY = 2                  # 秒

REST_URL   = f"{TDENGINE_REST_URL}/rest/sql"
AUTH_HDR   = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

# 验证范围
MIN_TS     = 1640995200000       # 2022-01-01 00:00:00 UTC
MAX_TS     = 1900000000000       # ~2030-03-24
MIN_PRICE  = 0.001
MAX_PRICE  = 10000.0


# ──────────────────────────────────────────────────────
#  REST API 工具
# ──────────────────────────────────────────────────────
def exec_sql(sql: str, db: str = None) -> dict:
    """通过原生 HTTP REST API 执行 SQL"""
    url = f"{REST_URL}/{db}" if db else REST_URL
    req = urllib.request.Request(
        url,
        data=sql.encode("utf-8"),
        headers={"Authorization": AUTH_HDR}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode())
        if data.get("code") != 0:
            raise RuntimeError(f"SQL Error [{data['code']}]: {data.get('desc', '')}")
        return data
    except urllib.error.URLError as e:
        raise ConnectionError(f"网络错误: {e}")


def get_rows(result: dict) -> list:
    """从 REST API 响应中提取数据行（TDengine 用 'data' 字段存放行数据）"""
    return result.get("data", [])


def exec_sql_with_retry(sql: str, db: str = None, retries: int = MAX_RETRIES) -> dict:
    """带重试的 SQL 执行"""
    for attempt in range(retries):
        try:
            return exec_sql(sql, db)
        except (ConnectionError, TimeoutError) as e:
            if attempt < retries - 1:
                print(f"  ⚠️  重试 {attempt+1}/{retries}: {e}")
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                raise


# ──────────────────────────────────────────────────────
#  数据验证
# ──────────────────────────────────────────────────────
def validate_row(row: list, line_no: int) -> tuple[bool, str]:
    """验证单行数据，返回 (是否有效, 错误信息)"""
    if len(row) < 8:
        return False, f"列数不足: {len(row)}"

    # 时间戳
    try:
        ts = int(row[0])
    except ValueError:
        return False, f"时间戳非整数: {row[0]}"
    if ts < MIN_TS or ts > MAX_TS:
        return False, f"时间戳越界: {ts}"

    # OHLCV
    try:
        o, h, l, c = float(row[2]), float(row[3]), float(row[4]), float(row[5])
        v = float(row[6])
    except ValueError:
        return False, f"数值字段解析失败: {row[2:7]}"

    if any(p <= 0 or p > MAX_PRICE for p in (o, h, l, c)):
        return False, f"价格异常: O={o} H={h} L={l} C={c}"
    if h < l or h < o or h < c:
        return False, f"High 不是最高: H={h} O={o} L={l} C={c}"
    if l > o or l > c:
        return False, f"Low 不是最低: L={l} O={o} C={c}"
    if v < 0:
        return False, f"成交量为负: {v}"

    return True, ""


# ──────────────────────────────────────────────────────
#  CSV 流式读取 + 批量构建
# ──────────────────────────────────────────────────────
def stream_batches(csv_path: Path, batch_size: int):
    """
    流式读取 CSV，每次 yield (batch_index, values_list, stats)。
    values_list 中每个元素是格式化的 VALUES 字符串。
    不将整个文件加载到内存。
    """
    stats = {
        "total": 0,
        "valid": 0,
        "invalid": 0,
        "first_ts": None,
        "last_ts": None,
        "first_dt": None,
        "last_dt": None,
        "sample_errors": [],
    }

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # skip header

        batch = []
        for line_no, row in enumerate(reader, start=2):
            stats["total"] += 1

            ok, err = validate_row(row, line_no)
            if not ok:
                stats["invalid"] += 1
                if len(stats["sample_errors"]) < 10:
                    stats["sample_errors"].append(f"行{line_no}: {err}")
                continue

            stats["valid"] += 1
            ts = int(row[0])
            o, h, l, c, v = row[2], row[3], row[4], row[5], row[6]

            # 直接用 Unix 毫秒时间戳（BIGINT），TDengine PRECISION 'ms' 自动识别
            batch.append(f"({ts},{o},{h},{l},{c},{v})")

            # 记录首尾时间戳
            if stats["first_ts"] is None:
                stats["first_ts"] = ts
                stats["first_dt"] = row[1]
            stats["last_ts"] = ts
            stats["last_dt"] = row[1]

            if len(batch) >= batch_size:
                yield batch, stats
                batch = []

        if batch:
            yield batch, stats


# ──────────────────────────────────────────────────────
#  主导入流程
# ──────────────────────────────────────────────────────
def run_import():
    # 迁移后 CSV 路径不再有默认值：先校验再取文件大小，避免空路径直接抛
    # FileNotFoundError 掩盖「未配置」这个真实原因（原实现先 .stat() 后 exists()）。
    if not str(CSV_FILE) or not CSV_FILE.exists():
        print(f"❌ 未找到 CSV 文件: {CSV_FILE or '(未设置)'}")
        print("   请先设置环境变量 CRYPTO_TD_NEAR_CSV（或 FST_NEAR_CSV）指向 "
              "NEAR/USDT 1 分钟 K 线 CSV")
        sys.exit(1)

    print("=" * 60)
    print("  TDengine CSV 批量导入")
    print(f"  文件: {CSV_FILE.name}")
    print(f"  大小: {CSV_FILE.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"  目标: {DATABASE}.{STABLE} → {CHILD}")
    print(f"  批量: {BATCH_SIZE} 行/批")
    print("=" * 60)

    # ── 1. 准备数据库 ──
    print("\n📦 准备数据库...")

    # 检查数据库是否已存在
    result = exec_sql("SHOW DATABASES")
    existing_dbs = [row[0] for row in get_rows(result)]

    if DATABASE in existing_dbs:
        print(f"  ⚠️  数据库 {DATABASE} 已存在")
        # 检查表是否存在
        try:
            r = exec_sql(f"SELECT count(*) FROM {DATABASE}.{STABLE}")
            existing_rows = get_rows(r)[0][0] if get_rows(r) else 0
            print(f"  ⚠️  表 {STABLE} 已有 {existing_rows} 条数据")
            print(f"  ℹ️  使用 INSERT upsert 模式，相同时间戳会自动覆盖")
        except RuntimeError:
            print(f"  ℹ️  表 {STABLE} 不存在，将创建")
    else:
        exec_sql(f"CREATE DATABASE {DATABASE} PRECISION 'ms' KEEP 3650 DURATION 30")
        print(f"  ✅ 创建数据库: {DATABASE}")

    # 创建超级表
    exec_sql(f"""
        CREATE STABLE IF NOT EXISTS {DATABASE}.{STABLE} (
            ts     TIMESTAMP,
            open   DOUBLE,
            high   DOUBLE,
            low    DOUBLE,
            close  DOUBLE,
            volume DOUBLE
        ) TAGS (
            exchange NCHAR(20),
            symbol   NCHAR(20)
        )
    """, DATABASE)
    print(f"  ✅ 超级表: {STABLE}")

    # ── 2. 流式导入 ──
    # 预估总行数（用于进度显示）
    with open(CSV_FILE, 'r', encoding='utf-8') as f:
        file_line_count = sum(1 for _ in f) - 1  # 减去表头
    print(f"\n📤 开始导入（每 {BATCH_SIZE} 行一批，共 {file_line_count:,} 行）...")
    print("─" * 60)

    start_time = time.time()
    batch_idx = 0
    total_inserted = 0
    total_valid = 0
    total_invalid = 0
    insert_errors = 0
    sample_errors = []
    first_ts = None
    last_ts = None
    first_dt = None
    last_dt = None

    for batch, stats in stream_batches(CSV_FILE, BATCH_SIZE):
        batch_idx += 1
        total_valid = stats["valid"]
        total_invalid = stats["invalid"]
        first_ts = stats["first_ts"]
        last_ts = stats["last_ts"]
        first_dt = stats["first_dt"]
        last_dt = stats["last_dt"]
        sample_errors = stats["sample_errors"]

        values = ",".join(batch)
        sql = (
            f"INSERT INTO {CHILD} USING {STABLE} TAGS('okx','NEAR/USDT') "
            f"VALUES {values}"
        )

        try:
            exec_sql_with_retry(sql, DATABASE)
            total_inserted += len(batch)
        except Exception as e:
            insert_errors += 1
            if insert_errors <= 5:
                print(f"  ❌ 批次 #{batch_idx} 失败: {e}")

        # 进度显示
        if batch_idx % 50 == 0 or batch_idx <= 3:
            elapsed = time.time() - start_time
            speed = total_inserted / elapsed if elapsed > 0 else 0
            pct = total_inserted / file_line_count * 100 if file_line_count > 0 else 0
            remaining = (file_line_count - total_inserted) / speed if speed > 0 else 0

            print(
                f"  [{batch_idx:>5}批] "
                f"已插入: {total_inserted:>9,} / {stats['total']:>9,}  "
                f"({pct:5.1f}%)  "
                f"速度: {speed:>8,.0f} 行/秒  "
                f"剩余: ~{remaining/60:.1f}分钟"
            )

    total_time = time.time() - start_time
    avg_speed = total_inserted / total_time if total_time > 0 else 0

    # ── 3. 导入报告 ──
    print("\n" + "═" * 60)
    print("  📊 导入报告")
    print("═" * 60)
    print(f"  CSV 总行数:      {stats['total']:>12,}")
    print(f"  有效行数:        {total_valid:>12,}")
    print(f"  无效行数:        {total_invalid:>12,}")
    print(f"  成功插入:        {total_inserted:>12,}")
    print(f"  批次失败:        {insert_errors:>12,}")
    print(f"  总耗时:          {total_time:>12.1f} 秒")
    print(f"  平均速度:        {avg_speed:>12,.0f} 行/秒")

    if first_ts and last_ts:
        ts_range_days = (last_ts - first_ts) / 1000 / 86400
        print(f"\n  时间范围:")
        print(f"    起始: {first_dt}  (ts={first_ts})")
        print(f"    结束: {last_dt}  (ts={last_ts})")
        print(f"    跨度: {ts_range_days:.1f} 天")

    if sample_errors:
        print(f"\n  ⚠️  前 {len(sample_errors)} 条验证错误:")
        for err in sample_errors:
            print(f"    {err}")

    print("═" * 60)

    # ── 4. 导入后校验 ──
    print("\n🔍 数据校验...")
    verify_data(total_inserted, first_ts, last_ts)

    return total_inserted, total_time


# ──────────────────────────────────────────────────────
#  数据校验
# ──────────────────────────────────────────────────────
def verify_data(expected_inserted: int = None, first_ts: int = None, last_ts: int = None):
    """导入后数据完整性校验"""
    print("─" * 60)

    # 行数
    r = exec_sql(f"SELECT count(*) FROM {DATABASE}.{STABLE}")
    rows = get_rows(r)
    actual_count = rows[0][0]
    status = "✅" if (expected_inserted is None or actual_count == expected_inserted) else "⚠️"
    print(f"  {status} 总行数: {actual_count:,}")
    if expected_inserted and actual_count != expected_inserted:
        print(f"     期望: {expected_inserted:,}  实际: {actual_count:,}")

    # 时间范围
    r = exec_sql(f"SELECT FIRST(ts), LAST(ts) FROM {DATABASE}.{STABLE}")
    row = get_rows(r)[0]
    print(f"  ℹ️  数据库最早: {row[0]}")
    print(f"  ℹ️  数据库最晚: {row[1]}")

    # 关键字段统计
    r = exec_sql(f"SELECT AVG(open), AVG(close), AVG(volume) FROM {DATABASE}.{STABLE}")
    row = get_rows(r)[0]
    print(f"  ℹ️  平均 Open:  {float(row[0]):.4f}")
    print(f"  ℹ️  平均 Close: {float(row[1]):.4f}")
    print(f"  ℹ️  平均 Volume:{float(row[2]):.2f}")

    # 价格范围
    r = exec_sql(f"SELECT MIN(low), MAX(high) FROM {DATABASE}.{STABLE}")
    row = get_rows(r)[0]
    print(f"  ℹ️  最低价: {float(row[0]):.4f}  最高价: {float(row[1]):.4f}")

    # 时间连续性检查（抽查前 100 条间隔是否都是 60 秒）
    r = exec_sql(
        f"SELECT ts FROM {DATABASE}.{CHILD} ORDER BY ts LIMIT 101"
    )
    data_rows = get_rows(r)
    if data_rows and len(data_rows) >= 2:
        def parse_ts(val):
            """解析 REST API 返回的时间戳（可能是 ISO 字符串或 Unix ms）"""
            if isinstance(val, (int, float)):
                return int(val)
            # ISO 字符串: '2022-01-01T00:00:00.000Z'
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
    
        timestamps = [parse_ts(row[0]) for row in data_rows]
        intervals = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
        bad = [iv for iv in intervals if iv != 60000]
        if bad:
            print(f"  \u26a0\ufe0f  前 100 条中发现 {len(bad)} 个非 60 秒间隔（可能有数据缺口）")
        else:
            print(f"  \u2705 前 100 条时间间隔均正常（60秒）")

    print("─" * 60)
    print("  ✅ 数据校验完成")


# ──────────────────────────────────────────────────────
#  入口
# ──────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--verify":
        print("🔍 仅执行数据校验（不导入）\n")
        verify_data()
    else:
        run_import()
