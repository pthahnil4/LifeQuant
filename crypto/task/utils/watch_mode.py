# -*- coding: utf-8 -*-
"""观察者模式的只读执行边界；上下文按线程/异步任务隔离。"""
import builtins
from contextlib import contextmanager
from contextvars import ContextVar

WATCH_MODE_NOTICE = '当前处于观察者模式，不执行任何实际交易操作'

# 包导入与独立脚本导入必须共享同一个上下文，避免两种模块身份拆散保护。
_KEY = '_crypto_watch_mode_context'
if not hasattr(builtins, _KEY):
    setattr(builtins, _KEY, ContextVar(_KEY, default=False))
_WATCH_MODE = getattr(builtins, _KEY)


def watch_mode_active():
    return _WATCH_MODE.get()


@contextmanager
def watch_mode(enabled=True):
    """嵌套调用不能解除外层只读约束；退出（含异常）恢复原上下文。"""
    token = _WATCH_MODE.set(watch_mode_active() or bool(enabled))
    try:
        yield
    finally:
        _WATCH_MODE.reset(token)
