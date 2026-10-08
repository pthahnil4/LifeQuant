"""批量趋势离线回归：python -m crypto._smoke_batch_trend

仅 AST 加载被测定义，禁止导入生产 app 或连接真实策略/数据库；读写、行情均打桩。
--serve 提供 127.0.0.1:5092 浏览器验收夹具，复用真实路由、前端代码及日志面板。
"""
import ast
import csv
import datetime
import logging
import os
import tempfile
import sys
import threading
import time
import traceback
import types
import unittest
import uuid
from collections import deque
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List
from unittest.mock import Mock, patch

from flask import Flask, Response, jsonify, request, stream_with_context

ROOT = Path(__file__).resolve().parent


def load_batch():
    module = types.ModuleType('crypto.batch_trend_updater')
    ns = module.__dict__
    ns.update(threading=threading, datetime=datetime, uuid=uuid, deque=deque,
              Dict=Dict, List=List, sys=sys, ThreadPoolExecutor=ThreadPoolExecutor,
              as_completed=as_completed, csv=csv, os=os, tempfile=tempfile,
              _db_session_scope=None, _market_repo=None)
    ns.update(_batch_state_lock=threading.RLock(), _batch_run_lock=threading.Lock(),
              _BATCH_BARS=('15m', '1H', '4H', '1D'), _BATCH_WORKERS=2,
              _SAVE_EVERY=20, _BATCH_LOG_LIMIT=5000, _batch_log_seq=0,
              _batch_log_lines=deque(maxlen=5000), logger=logging.getLogger('batch-smoke'))
    ns['_batch_log_lock'] = ns['_batch_state_lock']
    names = {'BatchAlreadyRunning', '_ProgressState', 'BatchTrendAnalyzer', 'get_progress',
             '_batch_log', 'clear_batch_logs', 'get_batch_logs_since', 'get_batch_snapshot',
             '_extract_trend_info', '_warm_kline', '_write_csv'}
    tree = ast.parse((ROOT / 'batch_trend_updater.py').read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / 'batch_trend_updater.py'), 'exec'), ns)
    ns['_progress'] = ns['_ProgressState']()
    ns['_read_csv'] = Mock(return_value=[{'inst_id': 'BTC-USDT-SWAP', 'symbol': 'BTC'}])
    ns['_real_write_csv'] = ns['_write_csv']
    ns['_real_warm_kline'] = ns['_warm_kline']
    ns['_write_csv'] = Mock()
    ns['_warm_kline'] = Mock(return_value=True)
    ns['_compute_sar_from_df'] = Mock(return_value={'sar': ''})
    ns['_compute_er_from_df'] = Mock(return_value='')
    return module


def load_app(module):
    app = Flask('batch-offline', static_folder=None)
    ns = dict(app=app, jsonify=jsonify, request=request, Response=Response,
              stream_with_context=stream_with_context, traceback=traceback, __package__='crypto')
    names = {'batch_update', '_batch_stream_cursor', 'batch_progress', 'batch_log_stream'}
    tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / 'app.py'), 'exec'), ns)
    return app


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.b = load_batch()
        self.pro3 = types.SimpleNamespace(clear_batch_cache=Mock(), _fetch_kline_data=Mock())
        self.adapter = types.SimpleNamespace(calculate_single_coin_data=Mock(return_value={}))
        self.patcher = patch.dict(sys.modules, {'crypto.batch_trend_updater': self.b,
                                               'real_strategy_adapter': self.adapter,
                                               'pro3_singletimeframe': self.pro3})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.app = load_app(self.b)
        self.client = self.app.test_client()

    def test_full_python_sources_compile_without_cache(self):
        # 部分 Windows 环境不能创建 __pycache__ 临时文件；内存编译不依赖缓存路径。
        for name in ['batch_trend_updater.py', 'app.py', '_smoke_batch_trend.py']:
            path = ROOT / name
            compile(path.read_text(encoding='utf-8'), str(path), 'exec')

    def test_progress_phases_and_terminal(self):
        p = self.b._progress
        p.start(0)
        run_id = p.to_dict()['run_id']
        p.configure(10, 2)
        values = []
        for current in [1, 2, 8, 8.25, 8.5, 9, 10]:
            p.update(current)
            values.append(p.to_dict()['progress_pct'])
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 99.9)
        p.update(3)
        self.assertEqual(p.current, 10)
        p.finish()
        self.assertEqual(p.to_dict()['progress_pct'], 100)
        self.assertEqual(p.run_id, run_id)
        self.assertIn('/2', p.message)
        elapsed = p.to_dict()['elapsed_seconds']
        self.assertEqual(p.to_dict()['elapsed_seconds'], elapsed)

    def test_atomic_start_and_conflict_before_worker_runs(self):
        queued = []
        class DelayedThread:
            def __init__(self, target, **kwargs):
                self.target = target
            def start(self):
                queued.append(self.target)
        with patch.object(threading, 'Thread', DelayedThread):
            one = self.client.post('/api/batch/update')
            two = self.client.post('/api/batch/update')
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json['data']['status'], 'running')
        self.assertEqual(two.status_code, 409)
        self.assertEqual(len(queued), 1)
        queued[0]()
        self.assertFalse(self.b._batch_run_lock.locked())
        self.assertEqual(self.b.get_progress()['status'], 'completed')

    def test_parallel_requests_only_one_worker(self):
        entered, release = threading.Event(), threading.Event()
        def pipeline(analyzer, callback=None):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('测试屏障超时')
            return {'total': 1, 'success': 1, 'error': 0}
        with patch.object(self.b.BatchTrendAnalyzer, '_run_pipeline', pipeline):
            first = self.b.BatchTrendAnalyzer().run_in_background()
            try:
                self.assertTrue(entered.wait(2))
                with ThreadPoolExecutor(max_workers=8) as pool:
                    def post(_):
                        with self.app.test_client() as client:
                            return client.post('/api/batch/update').status_code
                    self.assertEqual(list(pool.map(post, range(8))), [409] * 8)
            finally:
                release.set()
                first.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(self.b._batch_run_lock.locked())

    def test_thread_start_failure_releases_lock(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('启动失败')):
            with patch.object(traceback, 'print_exc'):
                response = self.client.post('/api/batch/update')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.b.get_progress()['status'], 'error')
        self.assertFalse(self.b._batch_run_lock.locked())
        self.b.BatchTrendAnalyzer().run()
        self.assertEqual(self.b.get_progress()['status'], 'completed')

    def test_read_warmup_compute_save_failure_cleanup(self):
        for target in ['_read_csv', 'ThreadPoolExecutor', '_write_csv']:
            with self.subTest(target=target):
                with patch.object(self.b, target, side_effect=RuntimeError(target)):
                    with self.assertRaises(RuntimeError):
                        self.b.BatchTrendAnalyzer().run()
                self.assertEqual(self.b.get_progress()['status'], 'error')
                self.assertIn(target, self.b.get_progress()['message'])
                self.assertFalse(self.b._batch_run_lock.locked())
        with patch.object(self.b.BatchTrendAnalyzer, '_process_single_symbol', side_effect=RuntimeError('计算失败')):
            with self.assertRaises(RuntimeError):
                self.b.BatchTrendAnalyzer().run()
        self.assertEqual(self.pro3.clear_batch_cache.call_count, 4)

    def test_empty_and_invalid_coin_lists(self):
        for rows in [[], [{'symbol': '坏记录'}]]:
            self.b._read_csv.return_value = rows
            with self.assertRaises(ValueError):
                self.b.BatchTrendAnalyzer().run()
            self.assertEqual(self.b.get_progress()['status'], 'error')
            self.assertFalse(self.b._batch_run_lock.locked())
        self.b._write_csv.assert_not_called()

    def test_pipeline_per_period_progress_and_all_failed(self):
        states = []
        old = self.b._progress.update
        def record(*args, **kwargs):
            old(*args, **kwargs)
            states.append(self.b.get_progress())
        with patch.object(self.b._progress, 'update', side_effect=record):
            result = self.b.BatchTrendAnalyzer().run()
        self.assertEqual(result, {'total': 1, 'success': 1, 'error': 0})
        self.assertTrue(any(s['current'] == 4.25 for s in states))
        self.assertTrue(any(s['phase'] == 'saving' and s['progress_pct'] < 100 for s in states))
        self.adapter.calculate_single_coin_data.return_value = None
        result = self.b.BatchTrendAnalyzer().run()
        self.assertEqual(result['error'], 1)
        self.assertEqual(self.b.get_progress()['status'], 'error')

    def test_cache_cleanup_failure_is_terminal_error(self):
        self.pro3.clear_batch_cache.side_effect = RuntimeError('清理失败')
        self.b.BatchTrendAnalyzer().run()
        self.assertEqual(self.b.get_progress()['status'], 'error')
        self.assertFalse(self.b._batch_run_lock.locked())

    def test_primary_save_failure_not_reported_as_success(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent / 'data', prefix='batch_smoke_') as tmp:
            self.b.CSV_PATH = str(Path(tmp) / 'coins.csv')
            self.b._db_session_scope = lambda: nullcontext(None)
            self.b._market_repo = types.SimpleNamespace(save_coin_rows=Mock(side_effect=RuntimeError('DB 不可用')))
            self.b._write_csv = self.b._real_write_csv
            with self.assertRaisesRegex(RuntimeError, 'CSV 备份已保存'):
                self.b.BatchTrendAnalyzer().run()
            self.assertTrue(Path(self.b.CSV_PATH).exists())
            self.assertEqual(self.b.get_progress()['status'], 'error')
            self.assertFalse(self.b._batch_run_lock.locked())

    def test_csv_atomic_write_preserves_previous_file_on_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent / 'data', prefix='batch_smoke_') as tmp:
            output = Path(tmp) / 'coins.csv'
            self.b.CSV_PATH = str(output)
            output.write_text('原始数据', encoding='utf-8')
            with patch.object(os, 'replace', side_effect=PermissionError('模拟替换失败')):
                with self.assertRaises(PermissionError):
                    self.b._real_write_csv([{'symbol': 'BTC'}])
            self.assertEqual(output.read_text(encoding='utf-8'), '原始数据')
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)
            self.b._real_write_csv([{'symbol': 'BTC'}, {'symbol': 'ETH', 'new_col': '值'}])
            with output.open(encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[1]['new_col'], '值')

    def test_real_coin_list_offline_pipeline(self):
        # 只读取真实 CSV 币种清单；所有行情计算及保存仍为内存桩。
        with (ROOT / 'crypto_coins.csv').open(encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(rows)
        self.b._read_csv.return_value = rows
        result = self.b.BatchTrendAnalyzer().run()
        self.assertEqual(result['success'], len(rows))
        self.assertEqual(self.b.get_progress()['coin_total'], len(rows))
        self.assertEqual(self.b._warm_kline.call_count, len(rows) * 4)
        self.assertEqual(self.adapter.calculate_single_coin_data.call_count, len(rows) * 4)
        self.assertEqual(self.b.get_progress()['progress_pct'], 100)

    def test_repeated_warmup_page_stops_instead_of_looping(self):
        self.pro3._KLINE_TARGET_COUNT = 300
        self.pro3._request_candles = Mock(return_value=[['100']])
        self.pro3.store_batch_kline = Mock()
        self.b._rl = lambda group, func, *args, **kw: func(*args)
        with self.assertRaisesRegex(RuntimeError, '游标未前进'):
            self.b._real_warm_kline('BTC-USDT-SWAP', '1D')
        self.assertEqual(self.pro3._request_candles.call_count, 2)
        self.pro3.store_batch_kline.assert_not_called()

    def test_logs_bounded_reset_reconnect_and_complete_lines(self):
        self.b._progress.start(1)
        old_id = self.b._progress.run_id
        for i in range(5010):
            self.b._batch_log(str(i))
        snap = self.b.get_batch_snapshot(0, old_id)
        self.assertEqual(len(snap['logs']), 5000)
        self.assertEqual(snap['dropped_logs'], 10)
        self.assertEqual(self.b.get_batch_snapshot(snap['cursor'], old_id)['logs'], [])
        self.b._progress.start(1)
        self.b.clear_batch_logs()
        self.b._batch_log('长日志' * 3000)
        snap = self.b.get_batch_snapshot(5010, old_id)
        self.assertTrue(snap['reset'])
        self.assertEqual(len(snap['logs'][0]['msg']), 9000)
        self.assertEqual(snap['cursor'], 1)

    def test_sse_snapshot_includes_final_log_and_resume_id(self):
        self.b._progress.start(1)
        self.b._batch_log('首行')
        self.b._batch_log('尾行')
        self.b._progress.finish()
        run_id = self.b._progress.run_id
        response = self.client.get('/api/batch/log/stream', headers={'Last-Event-ID': run_id + ':1'})
        text = response.get_data(as_text=True)
        self.assertNotIn('首行', text)
        self.assertIn('尾行', text)
        self.assertLess(text.index('尾行'), text.index('event: done'))
        self.assertIn('id: ' + run_id + ':2', text)
        self.assertEqual(response.headers['X-Accel-Buffering'], 'no')
        self.assertIn('text/event-stream', response.content_type)

    def test_sse_running_progress_first_not_done(self):
        self.b._progress.start(10)
        self.b._progress.update(2)
        response = self.client.get('/api/batch/log/stream', buffered=False)
        iterator = iter(response.response)
        first = next(iterator) + next(iterator)
        response.close()
        self.assertIn(b'event: progress', first)
        self.assertIn(b'20.0', first)
        self.assertNotIn(b'event: done', first)

    def test_http_fallback_and_bad_cursors(self):
        self.b._progress.start(5)
        self.b._batch_log('轮询日志')
        response = self.client.get('/api/batch/progress?with_logs=1')
        self.assertEqual(response.json['data']['logs'][0]['msg'], '轮询日志')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        for path in ['/api/batch/progress', '/api/batch/log/stream']:
            for cursor in ['-1', 'bad', str(2**64)]:
                self.assertEqual(self.client.get(path + '?cursor=' + cursor).status_code, 400)
            self.assertEqual(self.client.get(path, headers={'Last-Event-ID': 'bad'}).status_code, 400)


def serve_fixture():
    b = load_batch()
    sys.modules['crypto.batch_trend_updater'] = b
    sys.modules['pro3_singletimeframe'] = types.SimpleNamespace(clear_batch_cache=lambda: None)
    def pipeline(self, callback=None):
        self.progress.configure(300, 60)
        for i in range(300):
            b._batch_log('隔离模拟日志 %d：' % i + ('长行内容 ' * 15))
            self.progress.update(i + 1, message='隔离模拟分析 %d/300' % (i + 1), phase='compute')
            time.sleep(0.25)
        return {'total': 60, 'success': 60, 'error': 0}
    b.BatchTrendAnalyzer._run_pipeline = pipeline
    app = load_app(b)
    switches = {'sse': False, 'offline': False}
    @app.before_request
    def failures():
        if request.path.startswith('/api/batch/'):
            if switches['offline'] or (switches['sse'] and request.path.endswith('/stream')):
                return jsonify(code=503, message='隔离模拟断线'), 503
    @app.post('/__fixture/<kind>')
    def toggle(kind):
        if kind in switches:
            switches[kind] = not switches[kind]
        return jsonify(switches)
    @app.get('/')
    def page():
        template = (ROOT / 'templates/index.html').read_text(encoding='utf-8')
        panel = template.split('<!-- 进度条区域 -->', 1)[1].split('<!-- 统计条 -->', 1)[0]
        return '''<!doctype html><meta charset="utf-8"><title>批量分析隔离验收</title>
        <style>body{font:16px Arial;margin:30px}.progress-bar-outer{background:#eee;height:24px}
        .progress-bar-inner{height:24px;color:white}button{padding:8px;margin:4px}</style>
        <h2>批量分析隔离验收（无业务数据读写）</h2>
        <button id="batch-start-btn">批量更新趋势数据</button>
        <button onclick="fetch('/__fixture/sse',{method:'POST'})">切换 SSE 故障</button>
        <button onclick="fetch('/__fixture/offline',{method:'POST'})">切换全网故障</button>
        <div id="fixture-reloads"></div>''' + panel + '''
        <script src="/controller.js"></script>'''
    @app.get('/controller.js')
    def script():
        source = (ROOT / 'static/js/main.js').read_text(encoding='utf-8')
        code = source.split('    // ---- SSE 实时推送与持续轮询兜底', 1)[1]
        code = '    // ---- SSE 实时推送与持续轮询兜底' + code.split('    // ---- 初始加载表格数据 ----', 1)[0]
        setup = '''const batchStartBtn=document.getElementById('batch-start-btn');
        const batchProgress=document.getElementById('batch-progress');
        const progressBar=document.getElementById('progress-bar');
        const progressMsg=document.getElementById('progress-msg');
        let isRunning=false; let currentView='full'; let reloads=0;
        function loadTableData(){document.getElementById('fixture-reloads').textContent='结果刷新次数：'+(++reloads);}
        const MDialog={confirm: o=>{if(window.confirm('确定启动隔离模拟任务？'))o.onOk();else o.onCancel();}};
        '''
        return Response(setup + code + "\nsetBatchBusy(true, '状态确认中...'); scheduleBatchSync(0);", mimetype='text/javascript')
    app.run(host='127.0.0.1', port=5092, threaded=True, debug=False, use_reloader=False)


if __name__ == '__main__':
    if '--serve' in sys.argv:
        serve_fixture()
    else:
        logging.disable(logging.CRITICAL)
        unittest.main(verbosity=2)
