# -*- coding: utf-8 -*-
"""RSS 根因回归：真实 Backtrader、合成行情、禁止联网和业务写入。

python crypto/_smoke_memory_growth.py
python crypto/_smoke_memory_growth.py --compare-ref HEAD --soak 1000 --output data/memory_regression_results.json
--compare-ref 从指定 Git 版本提取四个原始计算函数作结果对照，不加载历史应用。
"""
import argparse
import ast
from collections import OrderedDict
from contextlib import ExitStack, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import gc
import io
import itertools
import json
from pathlib import Path
import socket
import subprocess
import sys
import types
import unittest
from unittest.mock import mock_open, patch
import weakref

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent), str(ROOT), str(ROOT / 'strategy')]


def load_modules():
    # 不读取实际账号配置；策略计算只需要 flag，测试不构造交易所客户端。
    cfg = types.ModuleType('api_config')
    cfg.get_api_config = lambda: {'flag': '1'}
    cfg.validate_config = lambda _: (True, '')
    previous = sys.modules.get('api_config')
    sys.modules['api_config'] = cfg
    try:
        import pro3_singletimeframe as single
        import pro3_dualtimeframe as dual
    finally:
        # 仅还原配置桩，不能整体回滚 sys.modules，否则会重复加载 NumPy C 扩展。
        if previous is None:
            sys.modules.pop('api_config', None)
        else:
            sys.modules['api_config'] = previous
    from crypto import memory_watchdog as mw
    return single, dual, mw


def make_df(seed=7, n=300):
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 100 + 12 * np.sin(t / 12 + seed) + np.cumsum(rng.normal(0, .2, n))
    opening = close * 1.001
    df = pd.DataFrame({'open': opening, 'close': close,
                       'high': np.maximum(opening, close) * 1.02,
                       'low': np.minimum(opening, close) * .98,
                       'volume': np.full(n, 1000.)},
                      index=pd.date_range('2026-01-01', periods=n, freq='h'))
    df.index.name = 'datetime'
    single.calculate_adx(df)
    single._init_df_columns(df)
    df['LONG_DIRECTION'] = np.where((t // 45) % 2, 'fall', 'rise')
    return df


def raw_candles(n=300):
    return [[str(1767225600000 + i * 60000), str(100 + i / 100),
             str(102 + i / 100), str(98 + i / 100), str(101 + i / 100), '1']
            for i in reversed(range(n))]


def configure(module, algo='diff', entry='close', exit_='open', fast=True):
    module.apply_signal_algo(algo)
    module.apply_price_types(entry, exit_)
    module.FAST_MODE = fast
    module.PRINT_MARKET = module.PRINT_TRADE_OPS = module.PRINT_TRADE_RECORDS = 0
    module.PRINT_ORIGINAL_OUTPUT = 0


def run_path(path, seed=7):
    df = make_df(seed)
    ref = weakref.ref(df)
    if path == 0:
        single._run_strategy(df, long_direction='rise', is_dual_period=bool(seed % 2))
    elif path == 1:
        single._calculate_long_direction_series(df)
    elif path == 2:
        dual._fill_long_direction_series(df)
    else:
        dual._run_dual_strategy(df, long_direction='fall')
    return ref


class MemoryRegression(unittest.TestCase):
    def setUp(self):
        configure(single)
        configure(dual)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (('_kline_cache', OrderedDict()), ('_kline_cache_sizes', {}),
                            ('_kline_cache_bytes', 0), ('_batch_kline_cache', {})):
            self.stack.enter_context(patch.object(single, name, value))
        self.stack.enter_context(patch.object(single.time, 'time', return_value=10000.))
        self.request = self.stack.enter_context(patch.object(
            single, '_request_candles', side_effect=RuntimeError('离线网络故障')))

    def test_strategy_registry_and_frames_released(self):
        import backtrader as bt
        for path in range(4):
            run_path(path)
        gc.collect()
        registered = set(vars(bt.metabase))
        refs = [run_path(path, 8 + i) for i in range(4) for path in range(4)]
        gc.collect()
        self.assertEqual(set(vars(bt.metabase)), registered)
        self.assertFalse(any(ref() is not None for ref in refs))

    def test_instance_state_does_not_cross_runs(self):
        first = make_df(7)
        saved = first.copy(deep=True)
        s1 = single._run_strategy(first, long_direction='rise', is_dual_period=True)
        after = first.copy(deep=True)
        s2 = single._run_strategy(make_df(19), long_direction='fall', is_dual_period=False)
        self.assertIsNot(s1.trades, s2.trades)
        self.assertEqual(s1.long_direction, 'rise')
        self.assertEqual(s2.long_direction, 'fall')
        self.assertTrue(s1.p.is_dual_period)
        self.assertFalse(s2.p.is_dual_period)
        self.assertFalse(first.equals(saved))
        self.assertTrue(first.equals(after))

    def test_cache_entry_limit_and_batch_cleanup(self):
        with patch.object(single, '_KLINE_CACHE_MAX_ENTRIES', 4):
            for i in range(10):
                single.store_batch_kline(str(i), '1H', raw_candles())
            self.assertEqual(len(single._kline_cache), 4)
            self.assertEqual(len(single._batch_kline_cache), 10)
            single.clear_batch_cache()
            self.assertEqual(len(single._batch_kline_cache), 0)
            self.assertEqual(len(single._kline_cache), 4)
            self.assertEqual(set(single._kline_cache), set(single._kline_cache_sizes))

    def test_cache_byte_budget_and_overwrite(self):
        single.store_batch_kline('A', '1H', raw_candles())
        size = single._kline_cache_bytes
        with patch.object(single, '_KLINE_CACHE_MAX_BYTES', size * 2):
            for _ in range(3):
                for key in ('A', 'B', 'C'):
                    single.store_batch_kline(key, '1H', raw_candles())
            self.assertLessEqual(single._kline_cache_bytes, size * 2)
            self.assertEqual(single._kline_cache_bytes, sum(single._kline_cache_sizes.values()))
        with patch.object(single, '_KLINE_CACHE_MAX_BYTES', 1):
            single.store_batch_kline('X', '1H', raw_candles())
            self.assertEqual(single._kline_cache_bytes, 0)
            self.assertFalse(single._kline_cache)

    def test_fresh_cache_lru_and_frame_mutation_isolation(self):
        with patch.object(single, '_KLINE_CACHE_MAX_ENTRIES', 2):
            for key in ('A', 'B'):
                single.store_batch_kline(key, '1H', raw_candles())
            single.clear_batch_cache()
            df = single._fetch_kline_data('A', '1H')
            df.iloc[-1, df.columns.get_loc('close')] = -1
            self.assertGreater(single._fetch_kline_data('A', '1H').iloc[-1]['close'], 0)
            single.store_batch_kline('C', '1H', raw_candles())
            self.assertIn(('A', '1H'), single._kline_cache)
            self.assertNotIn(('B', '1H'), single._kline_cache)
            self.request.assert_not_called()

    def test_stale_display_only_and_retention(self):
        single.store_batch_kline('A', '1H', raw_candles())
        single.clear_batch_cache()
        with patch.object(single.time, 'time', return_value=10200.):
            with self.assertRaisesRegex(RuntimeError, '离线'):
                single._fetch_kline_data('A', '1H')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(len(single._fetch_kline_data('A', '1H', allow_stale=True)), 300)
            self.assertEqual(single._kline_cache[('A', '1H')][0], 10000.)
        with patch.object(single.time, 'time', return_value=11800.):
            with self.assertRaises(RuntimeError):
                single._fetch_kline_data('A', '1H', allow_stale=True)
            self.assertFalse(single._kline_cache)
            self.assertEqual(single._kline_cache_bytes, 0)

    def test_expired_batch_removed_and_idle_sweep(self):
        single.store_batch_kline('A', '1H', raw_candles())
        with patch.object(single.time, 'time', return_value=10900.):
            self.assertIsNone(single.get_batch_kline('A', '1H'))
            self.assertFalse(single._batch_kline_cache)
        with patch.object(single.time, 'time', return_value=12000.), patch.object(mw, '_get_libc', return_value=None):
            mw.release_free_memory()
        self.assertFalse(single._kline_cache)
        self.assertEqual(single._kline_cache_bytes, 0)

    def test_network_success_populates_bounded_cache(self):
        self.request.side_effect = None
        self.request.return_value = raw_candles()
        df = single._fetch_kline_data('A', '1H')
        self.assertEqual(len(df), 300)
        self.assertIn(('A', '1H'), single._kline_cache)
        self.assertGreater(single._kline_cache_bytes, 0)

    def test_concurrent_cache_writes_stay_bounded(self):
        def worker(i):
            key = str(i % 20)
            single.store_batch_kline(key, '1H', raw_candles(20))
            single.get_batch_kline(key, '1H')
            single.prune_kline_caches()
        with patch.object(single, '_KLINE_CACHE_MAX_ENTRIES', 8):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(worker, range(100)))
            self.assertLessEqual(len(single._kline_cache), 8)
            self.assertEqual(set(single._kline_cache), set(single._kline_cache_sizes))
            self.assertEqual(single._kline_cache_bytes, sum(single._kline_cache_sizes.values()))

    def test_batch_hit_survives_regular_cache_eviction(self):
        with patch.object(single, '_KLINE_CACHE_MAX_ENTRIES', 1):
            single.store_batch_kline('A', '1H', raw_candles())
            single.store_batch_kline('B', '1H', raw_candles())
            self.assertNotIn(('A', '1H'), single._kline_cache)
            self.assertEqual(len(single._fetch_kline_data('A', '1H')), 300)
            self.request.assert_not_called()

    def test_detail_reuses_modules_and_restores_settings(self):
        import backtrader as bt
        import strategy_util as util
        registered = set(vars(bt.metabase))
        for module, strategy, fetch in ((single, 'single', '_fetch_kline_data'),
                                         (dual, 'dual', '_fetch_kline_data')):
            original = (module.FAST_MODE, module.ENTRY_PRICE_TYPE, module.SHORT_SIGNAL_ALGO)
            with patch.object(module, fetch, side_effect=lambda *a, **kw: make_df()):
                for _ in range(3):
                    result = util.get_strategy_detail('TEST', strategy=strategy,
                                                      entry_price='open', signal_algo='hybrid')
                    self.assertIsInstance(result, dict)
                    self.assertIs(sys.modules[module.__name__], module)
                    self.assertEqual((module.FAST_MODE, module.ENTRY_PRICE_TYPE,
                                      module.SHORT_SIGNAL_ALGO), original)
        gc.collect()
        self.assertEqual(set(vars(bt.metabase)), registered)
        self.assertIs(dual._single, single)

    def test_detail_failure_restores_flags_and_releases_frame(self):
        import strategy_util as util
        import backtrader as bt
        registered = set(vars(bt.metabase))
        refs = []
        def broken_fetch(*a, **kw):
            df = make_df(n=5)  # 不足 MACD 暖机长度，真实 Cerebro 计算会报错。
            refs.append(weakref.ref(df))
            return df
        for module, strategy in ((single, 'single'), (dual, 'dual')):
            original = (module.FAST_MODE, module.ENTRY_PRICE_TYPE, module.SHORT_SIGNAL_ALGO)
            with patch.object(module, '_fetch_kline_data', side_effect=broken_fetch):
                with self.assertRaises(IndexError):
                    util.get_strategy_detail('TEST', strategy=strategy, entry_price='open', signal_algo='hybrid')
            self.assertEqual((module.FAST_MODE, module.ENTRY_PRICE_TYPE,
                              module.SHORT_SIGNAL_ALGO), original)
        gc.collect()
        self.assertFalse(any(ref() is not None for ref in refs))
        self.assertEqual(set(vars(bt.metabase)), registered)

    def test_latest_history_after_filter(self):
        lines = [json.dumps({'i': i, 'source': 'app' if i % 4 == 0 else 'embedded'})
                 for i in range(20)]
        data = '\n'.join(lines) + '\n坏记录\n[]\nnull\n'
        with patch('builtins.open', mock_open(read_data=data)):
            self.assertEqual([r['i'] for r in mw.read_history(2, 'app')], [12, 16])
            self.assertEqual([r['i'] for r in mw.read_history(2)], [18, 19])
            self.assertEqual(mw.read_history(0), [])

    def test_warning_with_trace_configuration(self):
        class StopLoop(BaseException):
            pass
        for enabled in (False, True):
            with ExitStack() as stack:
                stack.enter_context(patch.object(mw, 'release_free_memory'))
                stack.enter_context(patch.object(mw, 'get_process_rss_mb', return_value=650.))
                stack.enter_context(patch.object(mw, 'append_history_record'))
                stack.enter_context(patch.object(mw, '_consume_dump_trigger', return_value=False))
                stack.enter_context(patch.object(mw, '_tracemalloc_enabled', return_value=enabled))
                stack.enter_context(patch.object(mw, '_last_dump_ts', 0.))
                stack.enter_context(patch.dict(mw._state, warn_mb=600, kill_mb=800))
                stack.enter_context(patch.object(mw.time, 'sleep', side_effect=StopLoop))
                dump = stack.enter_context(patch.object(mw, 'dump_tracemalloc_to_file', return_value=None))
                warn = stack.enter_context(patch.object(mw, '_try_warn_email'))
                with self.assertRaises(StopLoop):
                    mw._monitor_loop()
                warn.assert_called_once()
                self.assertEqual(dump.call_count, int(enabled))

    def test_trace_status_is_actual_not_configuration(self):
        fake = types.SimpleNamespace(is_tracing=lambda: False)
        with patch.dict(sys.modules, {'tracemalloc': fake}), patch.object(mw, '_tracemalloc_enabled', return_value=True):
            status = mw.get_memory_status()
            self.assertTrue(status['tracemalloc_configured'])
            self.assertFalse(status['tracemalloc_enabled'])
            self.assertEqual(status['tracemalloc_traceback_limit'], 0)


def baseline_module(module, ref, functions):
    filename = 'crypto/strategy/' + module.__name__ + '.py'
    source = subprocess.check_output(['git', 'show', ref + ':' + filename],
                                     cwd=ROOT.parent).decode('utf-8')
    tree = ast.parse(source)
    functions = (*functions, 'apply_price_types', 'apply_signal_algo')
    tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                 and (getattr(node, 'name', '') in functions or isinstance(node, ast.ClassDef))]
    old = types.ModuleType('_memory_baseline_' + module.__name__)
    old.__dict__.update(vars(module))
    old.__name__ = '_memory_baseline_' + module.__name__
    sys.modules[old.__name__] = old
    exec(compile(tree, filename, 'exec'), old.__dict__)
    return old


def compare_baseline(ref):
    import pandas as pd
    old_s = baseline_module(single, ref, ('_run_strategy', '_calculate_long_direction_series'))
    old_d = baseline_module(dual, ref, ('_run_dual_strategy', '_fill_long_direction_series'))
    attrs = ('trades', 'trade_times', 'trade_dirs', 'profits', 'max_wins', 'max_losses',
             'fix_money', 'contract_money', 'mix_money', 'modify_flag', 'long_direction',
             'current_position', 'filtered_signals', 'total_signals', 'zero_entries',
             'false_signals', 'true_signals', 'total_crossings', 'pending_order',
             'entry_orders_submitted', 'entry_orders_filled', 'entry_orders_expired',
             'entry_orders_cancelled', 'total_exits', 'total_signal_flips')
    count = 0
    for mod, old, runner, entries, modes in (
            (single, old_s, '_run_strategy', ('open', 'close', 'zero'), (False, True)),
            (dual, old_d, '_run_dual_strategy', ('open', 'close'), (None,))):
        for seed, fast, algo, entry, exit_, mode in itertools.product(
                (7, 19), (False, True), ('diff', 'hybrid'), entries, ('open', 'close'), modes):
            for m in (mod, old):
                configure(m, algo, entry, exit_, fast)
                # 覆盖 stop 的统计分支，包括单双周期实例模式取值。
                m.PRINT_ORIGINAL_OUTPUT = 1
            a, b = make_df(seed), make_df(seed)
            kwargs = {'long_direction': 'rise' if seed == 7 else 'fall'}
            if mode is not None:
                kwargs['is_dual_period'] = mode
            with redirect_stdout(io.StringIO()):
                sa, sb = getattr(mod, runner)(a, **kwargs), getattr(old, runner)(b, **kwargs)
            pd.testing.assert_frame_equal(a, b, check_exact=True)
            for attr in attrs:
                assert getattr(sa, attr, None) == getattr(sb, attr, None), (runner, attr, seed, fast, algo, entry, exit_, mode)
            count += 1
    for mod, old, runner in ((single, old_s, '_calculate_long_direction_series'),
                             (dual, old_d, '_fill_long_direction_series')):
        for seed in (7, 19):
            a, b = make_df(seed), make_df(seed)
            getattr(mod, runner)(a)
            getattr(old, runner)(b)
            pd.testing.assert_frame_equal(a, b, check_exact=True)
            count += 1
    print('策略 A/B 逐列及交易状态完全一致：', count, '组', flush=True)
    return count


def soak(rounds):
    import backtrader as bt
    configure(single)
    configure(dual)
    for _ in range(3):
        for path in range(4):
            run_path(path)
    gc.collect()
    registry = set(vars(bt.metabase))
    samples = []
    for i in range(rounds + 1):
        if i:
            refs = [run_path(path, 7 + i % 2) for path in range(4)]
        if i % max(1, rounds // 10) == 0 or i == rounds:
            gc.collect()
            assert set(vars(bt.metabase)) == registry, '派生类仍在累积'
            if i:
                assert all(r() is None for r in refs), '行情实例未释放'
            sample = {'rounds_per_path': i, 'rss_mb': round(mw.get_process_rss_mb(), 2),
                      'registered_delta': len(set(vars(bt.metabase)) - registry),
                      'gc_objects': len(gc.get_objects())}
            samples.append(sample)
            print('内存长跑', json.dumps(sample), flush=True)
    return samples


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare-ref')
    parser.add_argument('--soak', type=int, default=0)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('禁止测试联网')):
        single, dual, mw = load_modules()
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(MemoryRegression))
        report = {'tests': result.testsRun, 'passed': result.wasSuccessful()}
        if not result.wasSuccessful():
            sys.exit(1)
        if args.compare_ref:
            report['comparison_cases'] = compare_baseline(args.compare_ref)
        if args.soak:
            report['soak'] = soak(args.soak)
        if args.output:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print('结果保存：', args.output)
