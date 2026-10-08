"""可选请求性能观测：只保存计数和时长，不保存 SQL、参数或业务数据。"""
import contextlib
import contextvars
import json
import logging
import os
import time
from dataclasses import dataclass, asdict

from sqlalchemy import event
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)
_current = contextvars.ContextVar('db_performance', default=None)


@dataclass
class Metrics:
    sql_count: int = 0
    sql_errors: int = 0
    sql_ms: float = 0.0
    acquire_count: int = 0
    acquire_ms: float = 0.0
    connection_ms: float = 0.0
    commit_ms: float = 0.0
    request_ms: float = 0.0

    def snapshot(self):
        return {k: round(v, 3) if isinstance(v, float) else v
                for k, v in asdict(self).items()}


@contextlib.contextmanager
def measure():
    """CLI/测试显式采样；支持嵌套，退出后恢复外层上下文。"""
    metrics = Metrics()
    token = _current.set(metrics)
    started = time.perf_counter()
    try:
        yield metrics
    finally:
        metrics.request_ms = (time.perf_counter() - started) * 1000
        _current.reset(token)


@contextlib.contextmanager
def measure_commit():
    metrics = _current.get()
    if metrics is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        metrics.commit_ms += (time.perf_counter() - started) * 1000


class TimedQueuePool(QueuePool):
    """获取时长包括排队、建连及 pre_ping，不能当作纯锁等待时间。"""
    def connect(self):
        metrics = _current.get()
        if metrics is None:
            return super().connect()
        started = time.perf_counter()
        try:
            return super().connect()
        finally:
            metrics.acquire_count += 1
            metrics.acquire_ms += (time.perf_counter() - started) * 1000


def instrument_engine(engine):
    """每个引擎只安装一次；没有采样上下文时不累计数据。"""
    if getattr(engine, '_crypto_performance_installed', False):
        return
    engine._crypto_performance_installed = True

    @event.listens_for(engine, 'before_cursor_execute')
    def before(conn, cursor, statement, parameters, context, executemany):
        metrics = _current.get()
        if metrics is not None:
            metrics.sql_count += 1
            context._crypto_perf = (metrics, time.perf_counter())

    def finish(context, failed=False):
        sample = getattr(context, '_crypto_perf', None)
        if sample is not None:
            metrics, started = sample
            metrics.sql_ms += (time.perf_counter() - started) * 1000
            metrics.sql_errors += int(failed)
            context._crypto_perf = None

    @event.listens_for(engine, 'after_cursor_execute')
    def after(conn, cursor, statement, parameters, context, executemany):
        finish(context)

    @event.listens_for(engine, 'handle_error')
    def error(context):
        finish(context.execution_context, True)

    @event.listens_for(engine, 'checkout')
    def checkout(dbapi_connection, record, proxy):
        metrics = _current.get()
        if metrics is not None:
            record.info['_crypto_perf'] = (metrics, time.perf_counter())

    @event.listens_for(engine, 'checkin')
    def checkin(dbapi_connection, record):
        sample = record.info.pop('_crypto_perf', None)
        if sample is not None:
            metrics, started = sample
            metrics.connection_ms += (time.perf_counter() - started) * 1000


def init_app(app):
    """CRYPTO_DB_PERF=1 开启；使用路由模板，日志不包含 URL 参数。"""
    if app.extensions.get('crypto_db_performance'):
        return
    from flask import g, request
    app.extensions['crypto_db_performance'] = True

    @app.before_request
    def start_request():
        if os.getenv('CRYPTO_DB_PERF', '').lower() not in ('1', 'true', 'yes', 'on'):
            return
        scope = measure()
        metrics = scope.__enter__()
        g._crypto_perf = (scope, metrics)

    @app.after_request
    def finish_request(response):
        g._crypto_perf_status = response.status_code
        return response

    @app.teardown_request
    def release_request(error):
        sample = g.pop('_crypto_perf', None)
        if sample is None:
            return
        scope, metrics = sample
        scope.__exit__(None, None, None)
        payload = metrics.snapshot()
        payload.update(route=request.url_rule.rule if request.url_rule else '<unmatched>',
                       method=request.method,
                       status=g.pop('_crypto_perf_status', 500 if error else 0))
        logger.info('[DBPerf] %s', json.dumps(payload, ensure_ascii=False))
