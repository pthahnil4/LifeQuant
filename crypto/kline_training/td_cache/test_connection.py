# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/database/test_connection.py  →  现位置: crypto/kline_training/td_cache/test_connection.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""TDengine 数据库连接测试"""
from crypto.kline_training.td_cache import get_connection

print("正在连接 TDengine...")

conn = get_connection()
cursor = conn.cursor()

# 1. 查看所有数据库
cursor.execute("SHOW DATABASES")
dbs = cursor.fetchall()
print(f"\n✅ 连接成功！现有数据库: {[db[0] for db in dbs]}")

# 2. 查看服务器版本
cursor.execute("SELECT server_version()")
ver = cursor.fetchone()
print(f"服务器版本: {ver[0]}")

# 3. 查看客户端版本（如有）
try:
    cursor.execute("SELECT client_version()")
    ver = cursor.fetchone()
    print(f"客户端版本: {ver[0]}")
except Exception:
    pass

conn.close()
print("\n连接测试完成，连接已关闭。")
