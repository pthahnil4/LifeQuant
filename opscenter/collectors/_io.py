#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集层通用 IO 与判级工具（纯 stdlib，无副作用）。"""
import json
import os
import time
from datetime import datetime

# 时间戳格式（交易系统落盘统一用这个）
_TS_FMT = '%Y-%m-%d %H:%M:%S'

# 简单的 TTL 缓存：{key: (expiry, value)}
_cache = {}


def cached(key, ttl):
    """装饰器：按 ttl 秒缓存无参函数的结果，避免高频重读大文件。"""
    def deco(fn):
        def wrapper():
            now = time.time()
            hit = _cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
            val = fn()
            _cache[key] = (now + ttl, val)
            return val
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def parse_ts(text):
    """解析 '%Y-%m-%d %H:%M:%S'；失败返回 None。"""
    try:
        return datetime.strptime(str(text).strip(), _TS_FMT)
    except (ValueError, TypeError, AttributeError):
        return None


def lag_seconds(ts_text, now=None):
    """某时间戳距今的秒数；解析失败返回 None。"""
    dt = parse_ts(ts_text)
    if dt is None:
        return None
    now = now or datetime.now()
    return max(0, int((now - dt).total_seconds()))


def read_jsonl(path, max_lines=None, max_bytes=4 * 1024 * 1024):
    """安全读取 JSONL，返回 [dict]（按文件顺序，旧→新）。

    - 文件不存在 / 读失败 → 返回 []（绝不抛，页面要能继续渲染）
    - 单行 JSON 解析失败 → 跳过该行（防止个别脏行拖垮整页）
    - max_lines：只取尾部若干行；配合 max_bytes 只回读文件末尾，避免整档载入
    """
    if not path or not os.path.isfile(path):
        return []
    try:
        size = os.path.getsize(path)
        if max_lines and size > max_bytes:
            with open(path, 'rb') as f:
                f.seek(max(0, size - max_bytes))
                if size > max_bytes:
                    f.readline()          # 丢弃可能被截断的首行
                blob = f.read().decode('utf-8', errors='replace')
        else:
            with open(path, encoding='utf-8', errors='replace') as f:
                blob = f.read()
    except OSError:
        return []

    out = []
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except ValueError:
            continue
    if max_lines and len(out) > max_lines:
        out = out[-max_lines:]
    return out


def read_text_lines(path, max_lines=6000, max_bytes=6 * 1024 * 1024):
    """安全读取文本日志的**尾部**若干行，返回 [str]（旧→新）。

    - 文件不存在 / 读失败 → 返回 []（页面降级为空，绝不抛）
    - 超 max_bytes 时只回读文件末尾，并丢弃可能被截断的首行
    - 统一按 utf-8 解码，errors='replace' 容忍个别坏字节（实盘日志常见）
    """
    if not path or not os.path.isfile(path):
        return []
    try:
        size = os.path.getsize(path)
        if size > max_bytes:
            with open(path, 'rb') as f:
                f.seek(size - max_bytes)
                f.readline()          # 丢弃可能被截断的首行
                blob = f.read().decode('utf-8', errors='replace')
        else:
            with open(path, encoding='utf-8', errors='replace') as f:
                blob = f.read()
    except OSError:
        return []
    lines = blob.splitlines()
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


def level_for(value, warn, danger, higher_worse=True):
    """按阈值返回 'success'/'warning'/'danger'。

    higher_worse=True：值越大越糟（如内存使用率）；False：值越大越好（如磁盘剩余）。
    value 为 None 时返回 'neutral'（无法判定）。
    """
    if value is None:
        return 'neutral'
    if higher_worse:
        if value >= danger:
            return 'danger'
        if value >= warn:
            return 'warning'
        return 'success'
    else:
        if value <= danger:
            return 'danger'
        if value <= warn:
            return 'warning'
        return 'success'
