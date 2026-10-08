"""指标导航（/indicator-nav）隔离冒烟验收。
运行：python -B -m crypto._smoke_indicator_nav

验收目标（对齐「改判定逻辑必须拿真实引擎跑 A/B」的教训）：
1. A/B 对齐：indicator_nav_service 的独立回测引擎与 copy 7 的 backtrader 引擎
   （get_latest_data）在同一份合成 1m 数据上逐笔对齐（开平仓时间/价格/盈亏/
   最大盈亏/剔除标记完全一致），覆盖 keltner / macd_adx / supertrend / trix_ma；
2. 注册表：copy 7 新增 6 指标全部注册，且 COPY4_ONLY 同名键不再被误拒；
3. HTTP 层：页面 / 三个 GET 接口 / 参数错误 400（数据层打桩，零网络零 CSV）；
4. 导航接线：DEFAULT_ITEMS 种子含入口 + 无 JS 渲染的 nav.html 里真实出现链接
   （种子静态断言不能替代渲染核验）；
5. 长周期视图：一次回测同帧返回 chart_long（长周期 K 线+趋势多空方向段），
   方向段区间与 analyze_trend_segments 统计口径一致，非法 view 参数 400。

纪律红线：全程零 DB 写入（CRYPTO_NO_BACKGROUND=1，接口本身只读）、零 OKX 触达
（build_frames / _get_csv_1m / _get_okx_frame 全部打桩为进程内合成数据）。
"""
import importlib.util
import os
import sys
import unittest

os.environ['CRYPTO_NO_BACKGROUND'] = '1'

import numpy as np
import pandas as pd

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from crypto import indicator_nav_service as svc

_STRATEGY_FILE = os.path.join(svc._STRATEGY_DIR,
                              'trend_strategy_boll_limit_scheduler_like copy 7.py')
_NEW_INDICATORS = ['supertrend', 'donchian', 'ema_cross', 'ma_slope', 'linreg', 'trix_ma']
_AB_INDICATORS = ['keltner', 'macd_adx', 'supertrend', 'trix_ma']


def _load_copy7():
    spec = importlib.util.spec_from_file_location('_v7_smoke_copy7', _STRATEGY_FILE)
    mod = importlib.util.module_from_spec(spec)
    # backtrader 的 metabase 会反查 sys.modules[cls.__module__]，必须先登记再 exec
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _synthetic_1m(days=30):
    """确定性合成 1m OHLC（趋势+双正弦波动），保证 BOLL 触边开平仓足够多。"""
    idx = pd.date_range('2026-01-01', periods=days * 1440, freq='1min')
    t = np.arange(len(idx), dtype=float)
    close = 100.0 + 0.0008 * t + 5.0 * np.sin(t / 900.0) + 2.5 * np.sin(t / 97.0)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = 0.05 + 0.03 * np.abs(np.sin(t / 500.0))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close},
                        index=idx)


COPY7 = _load_copy7()
DF_1M = _synthetic_1m()


def _copy7_engine_records(indicator, short_bar='15m', long_bar='4H',
                          leverage=10.0, entry_mult=0.0, exit_mult=0.0):
    """跑 copy 7 原版 get_latest_data（backtrader 真引擎），截获其内部
    strat.trade_records 作为 A/B 基准。

    截获方式：临时把 backtrader.Cerebro 换成子类，run() 后从 runstrats 取回
    真实策略实例 —— 对比的是 copy 7 自己的 DualTimeframeBollLimitStrategy，
    不是冒烟里重写的仿制品（否则 A/B 同源，证明不了任何事）。"""
    import backtrader as bt
    COPY7.GLOBAL_DF_1M = DF_1M
    COPY7.PRINT_MARKET = 0
    COPY7.PRINT_TRADE_OPS = 0
    COPY7.PRINT_TRADE_RECORDS = 0
    COPY7.PRINT_ORIGINAL_OUTPUT = 0

    captured = {}
    real_cerebro = bt.Cerebro

    class _SpyCerebro(real_cerebro):
        def run(self, *a, **k):
            out = super().run(*a, **k)
            try:
                captured['strat'] = self.runstrats[0][0]
            except Exception:
                pass
            return out

    bt.Cerebro = _SpyCerebro
    try:
        COPY7.get_latest_data(
            'NEAR-USDT-SWAP', short_bar, long_bar, order_size=1.0,
            trend_indicator=indicator, leverage_times=leverage,
            entry_atr_mult=entry_mult, exit_atr_mult=exit_mult)
    finally:
        bt.Cerebro = real_cerebro
    strat = captured.get('strat')
    if strat is None:
        raise AssertionError('未能截获 copy 7 策略实例（Cerebro 间谍失效）')
    return list(strat.trade_records)


def _service_raw(indicator, short_bar='15m', long_bar='4H',
                 leverage=10.0, entry_mult=0.0, exit_mult=0.0):
    """走服务层内部管道拿到**全精度**逐笔 records（不经 JSON 舍入），
    用于与 backtrader 引擎逐笔 A/B。数据层打桩为同一份合成数据。"""
    v7 = svc._load_v7_module()
    df_short = v7._resample_ohlc(DF_1M, short_bar)
    df_long = v7._resample_ohlc(DF_1M, long_bar)
    df, _dirs = svc._attach_long_direction(v7, df_short, df_long, indicator)
    records, _zones = svc._run_backtest(df, leverage_times=leverage,
                                        entry_atr_mult=entry_mult,
                                        exit_atr_mult=exit_mult)
    return records


def _service_records(indicator, short_bar='15m', long_bar='4H',
                     leverage=10.0, entry_mult=0.0, exit_mult=0.0):
    """跑服务层完整 run_backtest（数据层打桩），返回可直接 jsonify 的 payload。"""
    v7 = svc._load_v7_module()

    def _fake_build_frames(symbol, sb, lb):
        df_short = v7._resample_ohlc(DF_1M, sb)
        df_long = v7._resample_ohlc(DF_1M, lb)
        return df_short, df_long, 'csv', []

    orig = svc.build_frames
    svc.build_frames = _fake_build_frames
    try:
        result = svc.run_backtest(symbol='near', short_bar=short_bar, long_bar=long_bar,
                                  indicator=indicator, leverage_times=leverage,
                                  entry_atr_mult=entry_mult, exit_atr_mult=exit_mult)
    finally:
        svc.build_frames = orig
    return result


class TestIndicatorRegistry(unittest.TestCase):
    def test_01_new_indicators_registered(self):
        keys = list(COPY7.TREND_INDICATORS.keys())
        for ind in _NEW_INDICATORS:
            self.assertIn(ind, keys, f'{ind} 未注册进 TREND_INDICATORS')

    def test_02_copy4_names_now_resolvable(self):
        """注册表优先：supertrend 等 COPY4_ONLY 同名键应能正常计算而非报错。"""
        df = COPY7._resample_ohlc(DF_1M, '4H')
        for ind in ('supertrend', 'donchian', 'ema_cross', 'ma_slope', 'linreg'):
            dirs = COPY7.compute_direction_series(df, ind)
            self.assertEqual(len(dirs), len(df))
            self.assertTrue(np.all(np.isfinite(dirs)))
            self.assertTrue(set(np.unique(dirs)).issubset({-1.0, 0.0, 1.0}))
        # 仍属 copy 4 专属的键要继续报错（口径未放宽）
        with self.assertRaises(ValueError):
            COPY7.compute_direction_series(df, 'macd_hist')

    def test_03_all_indicators_compute(self):
        df = COPY7._resample_ohlc(DF_1M, '4H')
        for ind in COPY7.TREND_INDICATORS.keys():
            dirs = COPY7.compute_direction_series(df, ind)
            self.assertEqual(len(dirs), len(df), f'{ind} 长度不齐')
            self.assertTrue(np.all(np.isfinite(dirs)), f'{ind} 含 NaN/inf')


class TestBacktestABAlignment(unittest.TestCase):
    """服务层独立引擎 vs copy 7 backtrader 引擎逐笔对齐（核心验收）。"""

    def _assert_records_equal(self, ind, recs7, recs_svc):
        self.assertEqual(len(recs7), len(recs_svc),
                         f'{ind}: 笔数不一致 copy7={len(recs7)} svc={len(recs_svc)}')
        self.assertGreater(len(recs_svc), 5, f'{ind}: 交易样本过少，A/B 无意义')
        for k, (a, b) in enumerate(zip(recs7, recs_svc)):
            self.assertEqual(a['dir'], b['dir'], f'{ind}#{k} 方向')
            self.assertEqual(pd.Timestamp(a['entry_time']),
                             pd.Timestamp(b['entry_time']), f'{ind}#{k} 开仓时间')
            self.assertEqual(pd.Timestamp(a['exit_time']),
                             pd.Timestamp(b['exit_time']), f'{ind}#{k} 平仓时间')
            self.assertAlmostEqual(a['open_price'], b['open_price'], places=6,
                                   msg=f'{ind}#{k} 开仓价')
            self.assertAlmostEqual(a['close_price'], b['close_price'], places=6,
                                   msg=f'{ind}#{k} 平仓价')
            self.assertAlmostEqual(a['profit'], b['profit'], places=6,
                                   msg=f'{ind}#{k} 盈亏%')
            self.assertAlmostEqual(a['max_win'], b['max_win'], places=6,
                                   msg=f'{ind}#{k} 最大盈利')
            self.assertAlmostEqual(a['max_loss'], b['max_loss'], places=6,
                                   msg=f'{ind}#{k} 最大亏损')
            self.assertEqual(a['excluded'], b['excluded'], f'{ind}#{k} 剔除标记')

    def test_10_ab_default_params(self):
        for ind in _AB_INDICATORS:
            with self.subTest(indicator=ind):
                recs7 = _copy7_engine_records(ind)
                recs_svc = _service_raw(ind)
                self._assert_records_equal(ind, recs7, recs_svc)

    def test_11_ab_with_atr_mult_and_leverage(self):
        """带 ATR 外扩与非默认杠杆也要逐笔一致（挂单价路径不同）。"""
        for ind in ('keltner', 'trix_ma'):
            with self.subTest(indicator=ind):
                recs7 = _copy7_engine_records(ind, leverage=5.0,
                                              entry_mult=0.3, exit_mult=0.2)
                recs_svc = _service_raw(ind, leverage=5.0,
                                        entry_mult=0.3, exit_mult=0.2)
                self._assert_records_equal(ind, recs7, recs_svc)

    def test_12_ab_other_bars(self):
        """1H 短周期 × 1D 长周期档位对齐（周期切换刷新路径）。"""
        recs7 = _copy7_engine_records('keltner', short_bar='1H', long_bar='1D')
        recs_svc = _service_raw('keltner', short_bar='1H', long_bar='1D')
        self._assert_records_equal('keltner@1H/1D', recs7, recs_svc)

    def test_13_money_curve_matches_copy7_formula(self):
        """资金曲线公式抽查：用全精度 raw records 验证定投/真实定投终值。"""
        raw = _service_raw('keltner')
        result = _service_records('keltner')
        included = [r for r in raw if not r['excluded']]
        expect_fix = 100.0 + sum(100.0 * r['profit'] * 0.01 for r in included)
        self.assertAlmostEqual(result['stats']['fix_money'], expect_fix, places=4)
        fix_real = 100.0 + sum(100.0 * r['profit'] * 0.01 for r in raw)
        self.assertAlmostEqual(result['stats']['fix_real_money'], fix_real, places=4)
        # 笔数一致（raw 与 payload 同源）
        self.assertEqual(len(raw), len(result['trades']))

    def test_14_segments_and_stats_shape(self):
        result = _service_records('keltner')
        seg = result['segments']
        self.assertEqual(seg['indicator'], 'keltner')
        for key in ('num_segments', 'reversals', 'avg_dur_hours',
                    'median_dur_hours', 'coverage', 'reversals_per_day'):
            self.assertIn(key, seg)
        for key in ('win_rate', 'final_mean_pl_ratio', 'contract_money', 'mix_money',
                    'fix_drawdown', 'contract_drawdown', 'daily_profit',
                    'weekly_profit', 'avg_hold_hours'):
            self.assertIn(key, result['stats'])
        chart = result['chart']
        self.assertEqual(len(chart['times']), len(chart['candles']))
        self.assertEqual(len(chart['long_dir']), len(chart['times']))

    def test_15_chart_long_view_payload(self):
        """长周期视图载荷：形状自检 + 方向段区间与 analyze_trend_segments 同口径。"""
        for ind in ('keltner', 'trix_ma'):
            with self.subTest(indicator=ind):
                result = _service_records(ind)
                cl = result['chart_long']
                # 各序列与 K 线根数对齐
                for key in ('times', 'candles', 'boll_mid', 'boll_top', 'boll_bot', 'long_dir'):
                    self.assertEqual(len(cl[key]), len(cl['times']), f'{key} 长度未对齐')
                # 区间索引合法且落在窗口内，同一区间内方向一致
                n = len(cl['times'])
                for s, e, d in cl['zones']:
                    self.assertLessEqual(0, s <= e < n)
                    seg = cl['long_dir'][s:e + 1]
                    self.assertTrue(all((x >= 0.5) if d == 'rise' else (x <= -0.5) for x in seg))
                # 区间拓扑：升序不重叠，相邻区间方向交替（段边界与方向翻转一一对应）
                zs = sorted(cl['zones'])
                for a, b in zip(zs, zs[1:]):
                    self.assertLess(a[1], b[0])
                    self.assertNotEqual(a[2], b[2])
                # 与趋势段统计对齐：全量方向序列上的同向连续段数 = num_segments
                v7 = svc._load_v7_module()
                df_long = v7._resample_ohlc(DF_1M, '4H')
                seg = v7.analyze_trend_segments(df_long, ind, '4H')
                dirs = cl['long_dir']
                # 注：chart_long 只裁最近 chart_bars 根，合成数据下 4H 全量不足裁剪阈值，
                # 窗口即全量，因此可直接与全量段数对比
                self.assertEqual(len(zs), seg['num_segments'])
                self.assertEqual(len(cl['times']), int(len(df_long)))

    def test_16_long_view_stats_are_long_period(self):
        """长周期视图的回测统计/资金曲线/逐笔均为长周期口径，不沿用短周期。

        验证点：长周期 bar 稀疏，交易笔数少于短周期、平均持仓时长明显更长；
        三块长周期数据内部自洽；与短周期不相等（确认发生了切换）。"""
        r = _service_records('keltner', short_bar='15m', long_bar='4H')
        st, stl = r['stats'], r['stats_long']
        trl, mcl = r['trades_long'], r['money_curves_long']
        # 内部自洽：逐笔总数 = 计入笔数 + 剔除笔数；资金曲线点数 = 逐笔数
        self.assertEqual(len(trl), stl['total_trades'] + stl['excluded_trades'])
        self.assertEqual(len(mcl['fix']), len(trl))
        self.assertEqual(len(mcl['contract']), len(trl))
        self.assertLessEqual(stl['total_trades'], st['total_trades'])
        # 长周期逐笔已平仓（合成数据下 4H 应产生交易）且与短周期不同口径
        self.assertGreater(len(trl), 0)
        self.assertNotEqual(st['total_trades'], stl['total_trades'])
        # 平均持仓时长：长周期 bar 粒度粗，应明显长于短周期
        self.assertGreater(stl['avg_hold_hours'], st['avg_hold_hours'])
        # 终值一致性：若有交易，定投终值 = 资金曲线末点
        if trl:
            self.assertAlmostEqual(stl['fix_money'], mcl['fix'][-1], places=2)
        # chart_long 已带长周期买卖点（与逐笔同源）
        self.assertIsInstance(r['chart_long']['markers'], list)
        self.assertGreater(len(r['chart_long']['markers']), 0)


class TestHttpLayer(unittest.TestCase):
    """HTTP 接线：真实 app 导入（后台任务全关），数据层打桩零网络零 CSV。"""

    @classmethod
    def setUpClass(cls):
        from crypto.app import app
        cls.app = app
        cls.client = app.test_client()
        cls.v7 = svc._load_v7_module()
        # 本机开发环境可能配了 web_auth 口令（web_token.txt），带上 Bearer 过闸
        cls.auth_headers = {}
        try:
            from crypto import web_auth
            tok = web_auth.configured_token()
            if tok:
                cls.auth_headers['Authorization'] = 'Bearer ' + tok
        except Exception:
            pass

        def _fake_build_frames(symbol, sb, lb):
            df_short = cls.v7._resample_ohlc(DF_1M, sb)
            df_long = cls.v7._resample_ohlc(DF_1M, lb)
            return df_short, df_long, 'csv', []

        cls._patches = [
            (svc, 'build_frames', _fake_build_frames),
            (svc, '_get_csv_1m', lambda v7, symbol: DF_1M),
        ]
        cls._saved = [(obj, name, getattr(obj, name)) for obj, name, _ in cls._patches]
        for obj, name, fn in cls._patches:
            setattr(obj, name, fn)

    @classmethod
    def tearDownClass(cls):
        for obj, name, orig in cls._saved:
            setattr(obj, name, orig)

    def _get(self, url, **kw):
        headers = dict(self.auth_headers)
        headers.update(kw.pop('headers', {}) or {})
        return self.client.get(url, headers=headers, **kw)

    def test_20_page_renders(self):
        resp = self._get('/indicator-nav')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('indicator_nav.js', html)
        self.assertIn('指标导航', html)

    def test_21_meta_lists_registry(self):
        resp = self._get('/indicator-nav/api/meta')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        for ind in _NEW_INDICATORS + ['keltner', 'trix', 'macd_adx']:
            self.assertIn(ind, data['indicators'])
        self.assertIn('15m', data['short_bar_options'])
        self.assertIn('4H', data['long_bar_options'])

    def test_22_backtest_ok(self):
        resp = self._get('/indicator-nav/api/backtest',
                               query_string={'symbol': 'near', 'short_bar': '15m',
                                             'long_bar': '4H', 'indicator': 'keltner'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'], data.get('error'))
        self.assertEqual(data['source'], 'csv')
        self.assertGreater(len(data['chart']['times']), 100)
        self.assertIn('win_rate', data['stats'])
        self.assertIsInstance(data['trades'], list)
        # 默认视图 short，但长周期载荷同样同帧返回（前端切换免请求）
        self.assertEqual(data['params']['view'], 'short')
        self.assertGreater(len(data['chart_long']['times']), 30)
        # 长周期回测统计/资金曲线/逐笔与短周期分开回传（视图切换时前端取这套）
        for key in ('stats_long', 'money_curves_long', 'trades_long'):
            self.assertIn(key, data)
        self.assertIn('total_trades', data['stats_long'])

    def test_22b_backtest_view_long_echo(self):
        resp = self._get('/indicator-nav/api/backtest',
                               query_string={'indicator': 'keltner', 'view': 'long'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['params']['view'], 'long')

    def test_23_backtest_bad_indicator_400(self):
        resp = self._get('/indicator-nav/api/backtest',
                               query_string={'indicator': 'no_such_ind'})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['success'])

    def test_23b_backtest_bad_view_400(self):
        resp = self._get('/indicator-nav/api/backtest',
                               query_string={'view': 'weekly'})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['success'])

    def test_24_backtest_bad_symbol_400(self):
        resp = self._get('/indicator-nav/api/backtest',
                               query_string={'symbol': '../etc'})
        self.assertEqual(resp.status_code, 400)

    def test_25_compare_ok(self):
        resp = self._get('/indicator-nav/api/compare',
                               query_string={'symbol': 'near', 'long_bar': '4H'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'], data.get('error'))
        names = {r['indicator'] for r in data['rows']}
        self.assertTrue(set(_NEW_INDICATORS).issubset(names))
        # 排序口径：日均反转升序
        rpd = [r.get('reversals_per_day', 1e9) for r in data['rows'] if 'error' not in r]
        self.assertEqual(rpd, sorted(rpd))


class TestNavWiring(unittest.TestCase):
    def test_30_default_items_contains_entry(self):
        from crypto import navigation_service as ns
        item = next((it for it in ns.DEFAULT_ITEMS
                     if it.get('builtin_key') == 'indicator-nav'), None)
        self.assertIsNotNone(item, 'DEFAULT_ITEMS 缺指标导航入口')
        self.assertEqual(item['parent_id'], 'group-market')
        self.assertEqual(item['url'], '/indicator-nav')
        self.assertIn(item['icon']['value'], ns._ICON_BUILTIN_KEYS)
        self.assertEqual(ns.ACTIVE_PAGE_ALIASES.get('indicator-nav'), '/indicator-nav')

    def test_31_seed_config_validates(self):
        from crypto import navigation_service as ns
        canonical, warnings = ns.validate_and_normalize(ns.build_default_config())
        ids = {it['id'] for it in canonical['items']}
        self.assertIn('link-indicator-nav', ids)

    def test_32_nav_html_renders_entry(self):
        """无 JS 渲染兑底也须出现入口链接（渲染产物核验，非种子字符串断言）。"""
        from crypto.app import app
        client = app.test_client()
        headers = {}
        try:
            from crypto import web_auth
            tok = web_auth.configured_token()
            if tok:
                headers['Authorization'] = 'Bearer ' + tok
        except Exception:
            pass
        resp = client.get('/indicator-nav', headers=headers)
        html = resp.get_data(as_text=True)
        self.assertIn('href="/indicator-nav"', html)
        self.assertIn('指标导航', html)


class TestOkxWindowAnchor(unittest.TestCase):
    """回归守卫：OKX 回退取数窗口必须结束于当前时间（细周期不被请求数截断在旧段）。"""

    def test_40_window_ends_at_now(self):
        now_ms = 1_700_000_000_000
        for bar_min in (5, 15, 30, 60, 240, 1440):
            want, since_ms = svc._okx_anchor(now_ms, bar_min)
            step_ms = bar_min * 60 * 1000
            self.assertLessEqual(want, svc._OKX_MAX_REQUESTS * 100,
                                 f'{bar_min}m want 超请求数上限')
            self.assertEqual(since_ms + want * step_ms, now_ms,
                             f'{bar_min}m 窗口未结束于当前时间（细周期数据不新鲜回归）')
            self.assertGreater(want, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
