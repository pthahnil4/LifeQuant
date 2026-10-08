# -*- coding: utf-8 -*-
"""远端 MySQL 连通性探针：区分"库挂了 / 网络不通 / 只是慢"

判据组合：
  A. TCP 能否建连（Test 3306 端口）
  B. 全新连接执行 SELECT 1 的耗时（>0 即说明握手链路是否可用）
  C. 已有长连接（走应用连接池，通过 7777 的 HTTP 接口间接验证）是否仍可服务
  D. ICMP 可达性
A 失败 + C 正常  → 服务端连接数/handshake 被打满或防火墙限制新建连接
A 失败 + C 失败  → 主机或网络整体不可达
"""
import json
import os
import socket
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, 'data', 'db_url.txt'), encoding='utf-8') as f:
    URL = f.read().strip()
with open(os.path.join(ROOT, 'data', 'web_token.txt'), encoding='utf-8') as f:
    TOKEN = f.read().strip()

# pymysql://user:pwd@host:port/db?charset=...
body = URL.split('://', 1)[1]
auth, rest = body.split('@', 1)
hostport, db = rest.split('/', 1)
HOST, PORT = hostport.split(':')[0], int(hostport.split(':')[1])
USER, PWD = auth.split(':', 1)
print(f'目标 {USER}@{HOST}:{PORT}/{db}')

print('\n--- A. TCP 建连 ---')
for attempt in range(1, 4):
    t0 = time.time()
    s = socket.socket()
    s.settimeout(8)
    try:
        s.connect((HOST, PORT))
        print(f'  #{attempt} TCP OK  {time.time()-t0:.2f}s')
    except Exception as e:  # noqa: BLE001
        print(f'  #{attempt} TCP FAIL {time.time()-t0:.2f}s : {e}')
    finally:
        s.close()

print('\n--- B. 全新 MySQL 连接 + SELECT 1 + 全表计数 ---')
try:
    import pymysql
    t0 = time.time()
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PWD,
                           database=db.split('?')[0], charset='utf8mb4',
                           connect_timeout=10, read_timeout=60)
    print(f'  connect {time.time()-t0:.2f}s')
    with conn.cursor() as cur:
        t0 = time.time()
        cur.execute('SELECT 1')
        cur.fetchall()
        print(f'  SELECT 1 {time.time()-t0:.2f}s')
        t0 = time.time()
        cur.execute('SELECT COUNT(*) FROM plan_slots')
        print(f'  COUNT plan_slots={cur.fetchone()[0]} {time.time()-t0:.2f}s')
        t0 = time.time()
        cur.execute("SHOW GLOBAL STATUS LIKE 'Threads_connected'")
        for r in cur.fetchall():
            print(f'  {r[0]} = {r[1]}  ({time.time()-t0:.2f}s)')
        cur.execute('SHOW VARIABLES LIKE "max_connections"')
        for r in cur.fetchall():
            print(f'  {r[0]} = {r[1]}')
        cur.execute("SHOW GLOBAL STATUS LIKE 'Threads_running'")
        for r in cur.fetchall():
            print(f'  {r[0]} = {r[1]}')
    conn.close()
except Exception as e:  # noqa: BLE001
    print(f'  FAIL: {type(e).__name__}: {e}')

print('\n--- C. 走应用（已持有连接池）的读接口 ---')
for path in ('/plan/api/today-status', '/api/task/trading/runtime'):
    try:
        req = urllib.request.Request('http://127.0.0.1:7777' + path,
                                     headers={'Authorization': 'Bearer ' + TOKEN})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=120) as r:
            b = json.loads(r.read().decode('utf-8'))
        print(f'  {path} code={b.get("code")} {time.time()-t0:.2f}s')
    except Exception as e:  # noqa: BLE001
        print(f'  {path} FAIL: {e}')

print('\n--- D. ICMP ---')
out = subprocess.run(['ping', '-n', '3', HOST], capture_output=True, text=True, timeout=30).stdout
for line in out.splitlines():
    if any(k in line for k in ('TTL', '请求', 'timed out', '无法', 'Received', 'ms')):
        print('  ' + line.strip())
