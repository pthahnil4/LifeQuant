"""
futureStockTrade 迁移基线脚本（Phase 0 · 一次性执行）
=====================================================
职责（只做基线固化，不改任何业务代码）：
  1. 对被并入方 SQLite（data/training.db）执行 WAL checkpoint，
     把 -wal 中未落盘的数据合并回主库，避免复制时丢最近写入。
  2. 生成迁移前 zip 快照，排除 __pycache__/日志/截图等产物，
     保证任何时候都能回到合并前的原始状态。
  3. 打印源目录清单与体积统计，作为验收证据。

用法：python -X utf8 migrate_00_baseline.py
"""
import os
import sys
import zipfile
import sqlite3
import hashlib
from datetime import datetime

SRC_ROOT = r"D:\python\futureStockTrade"
DB_PATH = os.path.join(SRC_ROOT, "data", "training.db")
STAMP = datetime.now().strftime("%Y%m%d")
ZIP_PATH = os.path.join(os.path.dirname(SRC_ROOT), f"futureStockTrade_premerge_{STAMP}.zip")

# 不纳入快照的目录名/扩展名（纯产物，无迁移价值）
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}
EXCLUDE_EXT = {".log", ".png", ".jpeg", ".pyc"}
# 排除的根目录散落产物文件名前缀
EXCLUDE_PREFIX = ("screenshot_", "server_out", "server_err")


def checkpoint_wal(db_path: str) -> str:
    """把 WAL 数据合并回主库；返回执行前后的文件体积说明。"""
    if not os.path.exists(db_path):
        return f"[跳过] 数据库不存在: {db_path}"
    wal = db_path + "-wal"
    before_wal = os.path.getsize(wal) if os.path.exists(wal) else 0
    before_db = os.path.getsize(db_path)
    conn = sqlite3.connect(db_path)
    try:
        # TRUNCATE：合并回主库并把 wal 文件截断为 0，复制单一 .db 即完整
        rows = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.execute("PRAGMA journal_mode=WAL")  # 保持原模式，不改变运行行为
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    finally:
        conn.close()
    after_wal = os.path.getsize(wal) if os.path.exists(wal) else 0
    after_db = os.path.getsize(db_path)
    return (f"[OK] wal_checkpoint(TRUNCATE) 返回={rows} 完整性校验={integrity}\n"
            f"     .db   {before_db:,} -> {after_db:,} 字节\n"
            f"     .db-wal {before_wal:,} -> {after_wal:,} 字节\n"
            f"     表清单({len(tables)}): {', '.join(tables)}")


def build_snapshot() -> str:
    """生成 zip 快照，返回统计说明。"""
    n_files, n_bytes = 0, 0
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, dirnames, filenames in os.walk(SRC_ROOT):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for name in filenames:
                if os.path.splitext(name)[1].lower() in EXCLUDE_EXT:
                    continue
                if name.startswith(EXCLUDE_PREFIX):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, os.path.dirname(SRC_ROOT))
                zf.write(full, os.path.join("futureStockTrade", rel))
                n_files += 1
                n_bytes += os.path.getsize(full)
    digest = hashlib.md5(open(ZIP_PATH, "rb").read()).hexdigest()
    return (f"[OK] 快照已生成: {ZIP_PATH}\n"
            f"     纳入 {n_files} 个文件，原始体积 {n_bytes:,} 字节，"
            f"压缩包 {os.path.getsize(ZIP_PATH):,} 字节\n"
            f"     MD5={digest}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 70)
    print("Step 1 · SQLite WAL 落盘")
    print(checkpoint_wal(DB_PATH))
    print("=" * 70)
    print("Step 2 · 迁移前 zip 快照")
    print(build_snapshot())
    print("=" * 70)
    print("基线固化完成。后续所有迁移操作均可回退到本状态。")
