#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpsCenter 配置：数据源定位与阈值
==================================
本模块只负责"去哪里读、按什么口径判"，不含任何业务逻辑。
所有路径均以**只读**方式指向交易系统已落盘的产物；OpsCenter 绝不写入这些文件。

安全边界：本包不 import crypto 任何模块（尤其 crypto.app / task.scheduler，
它们导入即拉起后台线程、run_check 还会写心跳/发邮件）。健康与内存数据一律
读 memory_history.jsonl，磁盘用 stdlib 现算。
"""
import os

# 项目根 = opscenter/ 的上一级
_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)

# 交易系统日志目录（只读数据源）
LOG_DIR = os.environ.get('OPSCENTER_LOG_DIR') or os.path.join(PROJECT_ROOT, 'crypto', 'logs')

# 关键产物文件
MEMORY_HISTORY = os.path.join(LOG_DIR, 'memory_history.jsonl')   # 内存/系统心跳（每分钟采样）
BOOT_LEDGER = os.path.join(LOG_DIR, 'boot_ledger.jsonl')         # 启动归因台账
PROC_EXIT = os.path.join(LOG_DIR, 'proc_exit.json')              # 最近一次退出标记

# ---------------- 日志聚合（P2 · 只读文本日志） ----------------
# 每个源：显示名 + 文件路径。运维站只读不写，文件缺失时页面降级为空。
LOG_SOURCES = {
    'scheduler': {'name': '调度器', 'path': os.path.join(LOG_DIR, 'task_scheduler.log')},
    'trade':     {'name': '交易操作', 'path': os.path.join(LOG_DIR, 'trade_operations.log')},
}
LOG_DEFAULT_LIMIT = 200     # 单次返回条数（前端"载入更早"按此递增）
LOG_MAX_SCAN = 6000         # 每源最多回读的行数（tail 语义，防止超大日志拖垮）
LOG_MAX_BYTES = 6 * 1024 * 1024   # 每源回读字节上限（超限时只取文件尾部）

# ---------------- 运行态全景（P3 · 只读 HTTP 调用交易系统 Web API） ----------------
# 调度可视化 + 实盘只读监控的数据源是交易系统 Web 进程本身（APScheduler 任务态
# 只活在运行进程内存里、无持久 jobstore；持仓/委托是交易所实时数据），因此只能
# 走只读 HTTP，绝不 import crypto.app / task.scheduler。
#
# 地址：环境变量 OPSCENTER_TRADING_API 优先；缺省指向本机默认端口 7777
#      （与交易系统 app.py 的 CRYPTO_WEB_PORT 缺省一致；历史文档里的 6001 已过时）。
TRADING_API_BASE = (os.environ.get('OPSCENTER_TRADING_API') or 'http://127.0.0.1:7777').rstrip('/')

# 访问口令来源与交易系统 web_auth 完全同构（运维站自己独立解析，不 import 它）：
#   环境变量 CRYPTO_WEB_TOKEN 优先 → 其次数据目录 web_token.txt 首行；都没有=未配口令。
#   数据目录与 crypto/data_paths 同规则：CRYPTO_PLAN_DATA_DIR 优先 → 项目根 data/。
WEB_TOKEN_ENV = 'CRYPTO_WEB_TOKEN'
_DATA_DIR = (os.environ.get('CRYPTO_PLAN_DATA_DIR', '').strip()
             or os.path.join(PROJECT_ROOT, 'data'))
WEB_TOKEN_FILE = os.path.join(_DATA_DIR, 'web_token.txt')

# 只读端点表（均为 GET；对应交易系统 app.py / api_routes.py / alert_routes.py，返回 {code,message,data}）
API_ENDPOINTS = {
    'task_status': '/api/task/status',        # 调度器状态 + 任务列表（含实盘调度器）
    'account_info': '/api/account/info',      # 账户总权益/逐币种
    'positions': '/api/account/positions',    # 当前持仓
    'open_orders': '/api/trade/open_orders',  # 当前未成交委托
    'balance': '/api/funds/balance',          # 资金余额/资产估值
    # 告警中心（P5）：alert_routes 的三个只读 GET；绝不对接 POST 的存配置/手动检测（会发信）
    'alert_status': '/alert/api/status',      # 监控引擎状态 + 最近一轮检测摘要
    'alert_config': '/alert/api/config',      # 告警阈值规则（只读展示）
    'alert_history': '/alert/api/history',    # 告警历史（alert_log）
}
API_TIMEOUT_SEC = float(os.environ.get('OPSCENTER_API_TIMEOUT') or 3.5)  # 单次 GET 超时
SCHEDULE_TTL_SEC = 8       # 调度状态缓存（进程内存态，无需高频）
LIVE_TTL_SEC = 10          # 实盘只读缓存：稍长以克制对 OKX 的读频（经交易系统限频）
ALERT_TTL_SEC = 8          # 告警中心缓存
ALERT_HISTORY_LIMIT = 200  # 告警历史一次取多少条（与 alert_routes 默认一致）

# ---------------- 运维工具箱（P4 · 只读盘点 / 脱敏总览） ----------------
# 冒烟运行器 / 诊断脚本 / 配置总览，三样一律**只读盘点**：
#   - 交易系统的 _smoke_*.py 多含写库/发信/调真实 OKX（见 SMOKE_TESTS.md 分级），
#     运维站只列索引与落盘状态，绝不代跑；
#   - 唯一可一键运行的是**运维站自身**的自检冒烟（_smoke_opscenter_p*.py），它们
#     不 import crypto、只用 Flask test_client 打本地渲染，无副作用；
#   - 配置总览经 ast 字面量解析取账号名/环境并**脱敏**，绝不 import、绝不回显密钥明文。
SMOKE_INDEX_MD = os.path.join(PROJECT_ROOT, 'crypto', 'SMOKE_TESTS.md')   # 冒烟统一索引（只读）
API_CONFIG_PY = os.path.join(PROJECT_ROOT, 'crypto', 'api_config.py')     # OKX 配置（ast 解析脱敏）
CONFIG_JSON = os.path.join(PROJECT_ROOT, 'crypto', 'config.json')         # 币种等非金属配置（只读）
# 脚本盘点扫描目录（只在这些目录里找 _smoke_*.py / _diag_*.py，不做全仓递归）
SCRIPT_SCAN_DIRS = ['', 'crypto', os.path.join('crypto', 'task')]
TOOLS_TTL_SEC = 20                    # 清单/配置盘点缓存（脚本与配置很少变）
SMOKE_RUN_TIMEOUT_SEC = 45           # 自检冒烟一键运行的硬超时（秒）
# 允许一键运行的白名单（仅运维站自身冒烟；键→相对路径，前端只传键）
SELF_SMOKE_WHITELIST = {
    'p1': '_smoke_opscenter_p1.py',
    'p2': '_smoke_opscenter_p2.py',
    'p3': '_smoke_opscenter_p3.py',
    'p4': '_smoke_opscenter_p4.py',
}
# api_config 里的占位值（示例模板未填真实密钥时视为"未配置"）
OKX_PLACEHOLDER_VALUES = {'your_api_key_here', 'your_secret_key_here', 'your_passphrase_here', ''}


# ---------------- 判定阈值（展示口径，与交易系统实际告警阈值保持一致的观感） ----------------
HEARTBEAT_FRESH_SEC = 180      # 心跳距今 <= 此秒数视为交易进程存活
RSS_WARN_MB = 600              # 进程 RSS 关注线（与 memory_watchdog warn 对齐）
RSS_KILL_MB = 800              # 进程 RSS 危险线（与 memory_watchdog kill 对齐）
MEM_PCT_WARN = 85.0            # 系统内存使用率关注
MEM_PCT_DANGER = 90.0          # 系统内存使用率危险（OOM 风险线）
DISK_FREE_WARN_PCT = 15.0      # 磁盘剩余关注
DISK_FREE_DANGER_PCT = 5.0     # 磁盘剩余危险

# ---------------- 内存治理（P2）窗口与告警线 ----------------
MEM_RSS_WINDOW = 600           # RSS 趋势图采样点数（app 心跳，每2分钟一点）
MEM_SYS_WINDOW = 720           # 系统内存/磁盘趋势图采样点数（embedded+guard）
MEM_DUMP_GLOB = 'memdump_*.txt'  # 看门狗导出的 tracemalloc 快照（泄漏排查证据）

# 采集结果短时缓存（秒），避免每次渲染页面都重读大文件
CACHE_TTL_SEC = 5

# 演示模式：置 OPSCENTER_FAKE=1 时即使无真实数据也给出占位，便于纯前端联调
FAKE = str(os.environ.get('OPSCENTER_FAKE', '')).strip().lower() in ('1', 'true', 'yes', 'on')
