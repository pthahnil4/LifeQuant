#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
双日志系统 — 定时任务执行日志 + 实盘交易操作日志
====================================================

- task_scheduler.log  : 定时任务执行流（每轮开始/结束、分析结果、决策判断、异常）
- trade_operations.log: 实盘交易操作流（开仓/平仓/订单详情、API响应、成功/失败）

每条日志生成时会携带 run_id（如 R20260708-143000）用于交叉定位，
但格式化输出阶段会自动剥离形如 [R20260708-143000] 的前缀，
保证日志可读性（run_id 仍保留在 trade_journal 等结构化记录中）。
"""

import logging
import os
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from datetime import datetime

# 数据文件路径统一管理（宝塔部署：数据与代码分离）
# 日志目录：crypto/logs/
_LOG_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_DIR = os.path.join(_LOG_BASE, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# 单个日志文件上限 10MB，最多保留 10 份历史（.1 ~ .10），防止 7x24 运行无限膨胀
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 10

# 日志格式（带毫秒方便定位）
class _RunIdStripFormatter(logging.Formatter):
    """在消息生成阶段剥离 RunID 时间戳前缀（如 [R20260819-164322]），
    其余格式保持不变。调用方仍按 `f"[{run_id}] ..."` 拼接，无需改动业务代码。"""

    _RUN_ID_RE = re.compile(r'^\[R\d{8}-\d{6}\]\s*')

    def format(self, record):
        if isinstance(record.msg, str) and record.msg.startswith('[R'):
            record.msg = self._RUN_ID_RE.sub('', record.msg)
        return super().format(record)


_LOG_FMT = _RunIdStripFormatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# 模块级单例
_task_logger = None
_trade_logger = None
_lock = threading.Lock()


def _make_file_logger(name, filename) -> logging.Logger:
    """创建同时写文件 + 控制台的 logger（单例）"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # 避免重复添加 handler（热重载时）
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return logger

    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, filename), maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT, encoding='utf-8'
    )
    fh.setFormatter(_LOG_FMT)
    logger.addHandler(fh)

    # 控制台输出强制 UTF-8，避免 Windows 控制台(GBK)实时盯盘时中文乱码
    stream = sys.stdout
    try:
        stream.reconfigure(encoding='utf-8')  # Python 3.7+
    except Exception:
        pass
    sh = logging.StreamHandler(stream)
    sh.setFormatter(_LOG_FMT)
    logger.addHandler(sh)

    return logger


def get_task_logger() -> logging.Logger:
    """获取定时任务执行日志 logger"""
    global _task_logger
    if _task_logger is None:
        with _lock:
            if _task_logger is None:
                _task_logger = _make_file_logger('task_scheduler', 'task_scheduler.log')
    return _task_logger


def get_trade_logger() -> logging.Logger:
    """获取实盘交易操作日志 logger"""
    global _trade_logger
    if _trade_logger is None:
        with _lock:
            if _trade_logger is None:
                _trade_logger = _make_file_logger('trade_operations', 'trade_operations.log')
    return _trade_logger


def generate_run_id() -> str:
    """生成唯一调度轮次 ID，格式 R20260708-143000"""
    return f"R{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def get_log_dir() -> str:
    """返回日志目录路径"""
    return LOG_DIR


# =================================================================
# 通用格式化助手 —— task/trade 双日志共用，保证两个日志文件输出风格一致
# =================================================================

_DIR_CN = {'long': '多', 'short': '空'}


def fmt_dir(direction) -> str:
    """交易方向转中文：long→多 short→空 其他→--"""
    return _DIR_CN.get(str(direction or '').lower(), '--')


def fmt_pct(value) -> str:
    """百分比格式化（带正负号）：1.234→+1.23%；非法值返回 --"""
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return '--'


def fmt_qty(value) -> str:
    """张数格式化：去掉无意义的小数尾零（30.0→30，0.5→0.5）"""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return '0'
