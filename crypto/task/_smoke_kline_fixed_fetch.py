# -*- coding: utf-8 -*-
"""
冒烟测试：OKX 固定币种 K 线定时增量抓取（kline_fixed_fetch）
============================================================
离线运行：假交易所（多 symbol 版 FakeOkx）+ kv 配置桩（sys.modules 注入，
不连数据库），不联网、不触碰真实 crypto/data 目录。

覆盖场景：
  1. instId → ccxt symbol 转换与独立 CSV 路径推导
  2. 首拉（空 CSV 从 since_days 锚点起）+ 落盘严格连续
  3. 断点续传：无新数据重跑不重复写；时钟前进后只补新增根
  4. 单币失败隔离（非法 instId → partial_failed，其余币不受影响）
  5. max_candles_per_coin 限幅（按批截断）
  6. 软开关 disabled / 并发互斥 skipped_busy
  7. 配置坏值兜底（kv 桩给 99/0 → 越界回默认）
运行：python crypto/task/_smoke_kline_fixed_fetch.py
"""
import os
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'crypto', 'task'))

STEP = 60_000
NO_SLEEP = lambda *_: None  # noqa: E731


def ck(cond, label, detail=''):
    if not cond:
        raise AssertionError(f'{label} 失败: {detail}')
    print(f'  [ok] {label}')


class FakeOkx:
    """多 symbol 假交易所：bars = {symbol: {ts: (o,h,l,c,v)}}，接口对齐 ccxt。"""

    def __init__(self, bars, now_ms):
        self._bars = bars
        self.now_ms = now_ms
        self.id = 'okx'
        self.rateLimit = 0  # 不触发真实等待

    def parse_timeframe(self, tf):
        return 60

    def milliseconds(self):
        return self.now_ms

    def fetch_ohlcv(self, symbol, timeframe='1m', since=None, limit=None):
        ts_list = sorted(t for t in self._bars.get(symbol, {})
                         if since is None or t >= since)
        if limit:
            ts_list = ts_list[:limit]
        return [[t, *self._bars[symbol][t]] for t in ts_list]


def make_bars(n, start_ms):
    return {start_ms + i * STEP: (100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i)
            for i in range(n)}


def run_smoke():
    import kline_fixed_fetch as m
    from kline_fetcher.csv_store import load_existing_timestamps
    from kline_fetcher.gap_backfill import detect_gaps

    tmp = tempfile.mkdtemp(prefix='kline_fetch_smoke_')
    now = 1_760_000_000_000 - (1_760_000_000_000 % STEP)

    def cfg_for(**over):
        cfg = dict(m.DEFAULT_CONFIG)
        cfg.update({'data_dir': tmp, 'since_days': 30,
                    'max_workers': 3, 'max_candles_per_coin': 0})
        cfg.update(over)
        return cfg

    print('== 1) 交易对转换与独立 CSV 路径 ==')
    ck(m.inst_to_ccxt_symbol('BTC-USDT-SWAP') == 'BTC/USDT:USDT', '永续→ccxt线性合约')
    ck(m.inst_to_ccxt_symbol('near-usdt-swap') == 'NEAR/USDT:USDT', '大小写归一')
    ck(m.inst_to_ccxt_symbol('ETH-USDT') == 'ETH/USDT', '现货直转')
    try:
        m.inst_to_ccxt_symbol('WEIRD')
        raise AssertionError('非法 instId 应抛 ValueError')
    except ValueError:
        ck(True, '非法 instId 抛 ValueError')
    p = m.build_save_path('NEAR-USDT-SWAP', '1m', data_dir=tmp)
    ck(p.endswith(os.path.join(tmp, 'near_ohlcv_robust_1m_since_2022.csv')),
       '每币独立命名路径')

    print('== 2) 首拉：两币 250/120 根，市值序提交，落盘严格连续 ==')
    symA, symB = 'NEAR/USDT:USDT', 'BTC/USDT:USDT'
    bars = {symA: make_bars(250, now - 250 * STEP),
            symB: make_bars(120, now - 120 * STEP)}
    fake = FakeOkx(bars, now)
    r = m.run_kline_fixed_fetch_job(
        cfg=cfg_for(), coins=['NEAR-USDT-SWAP', 'BTC-USDT-SWAP'],
        exchange_factory=lambda: fake, sleep_fn=NO_SLEEP)
    ck(r == 'success', f'整轮 success（实际 {r}）')
    for sym, inst, n in ((symA, 'NEAR-USDT-SWAP', 250), (symB, 'BTC-USDT-SWAP', 120)):
        path = m.build_save_path(inst, '1m', data_dir=tmp)
        _, ordered = load_existing_timestamps(path)
        ck(len(ordered) == n, f'{inst} 落盘 {n} 根', f'实际 {len(ordered)}')
        ck(detect_gaps(ordered, STEP) == [], f'{inst} 时间戳连续无缺口')

    print('== 3) 断点续传：无新增写 0 根；时钟前进只补新增 ==')
    r = m.run_kline_fixed_fetch_job(
        cfg=cfg_for(), coins=['NEAR-USDT-SWAP'],
        exchange_factory=lambda: fake, sleep_fn=NO_SLEEP)
    ck(r == 'success', '重跑不报错')
    _, ordered = load_existing_timestamps(m.build_save_path('NEAR-USDT-SWAP', '1m', tmp))
    ck(len(ordered) == 250, '重跑未重复写入（仍 250 根）', f'实际 {len(ordered)}')
    # 交易所"现在"前进 12 分钟，并新增 12 根
    new_start = now
    fake.now_ms = now + 12 * STEP
    bars[symA].update(make_bars(12, new_start))
    r = m.run_kline_fixed_fetch_job(
        cfg=cfg_for(), coins=['NEAR-USDT-SWAP'],
        exchange_factory=lambda: fake, sleep_fn=NO_SLEEP)
    _, ordered = load_existing_timestamps(m.build_save_path('NEAR-USDT-SWAP', '1m', tmp))
    ck(r == 'success' and len(ordered) == 262, '增量补齐 12 根（250→262）',
       f'r={r} 实际 {len(ordered)}')
    ck(detect_gaps(ordered, STEP) == [], '续传后仍连续')

    print('== 4) 单币失败隔离 ==')
    r = m.run_kline_fixed_fetch_job(
        cfg=cfg_for(), coins=['WEIRD', 'BTC-USDT-SWAP'],
        exchange_factory=lambda: fake, sleep_fn=NO_SLEEP)
    ck(r == 'partial_failed:1', f'坏币失败但好币照常（实际 {r}）')

    print('== 5) max_candles_per_coin 批级限幅 ==')
    tmp2 = tempfile.mkdtemp(prefix='kline_fetch_smoke2_')
    symC = 'SOL/USDT:USDT'
    many = FakeOkx({symC: make_bars(900, now - 900 * STEP)}, now)
    r = m.run_kline_fixed_fetch_job(
        cfg=cfg_for(data_dir=tmp2, max_candles_per_coin=100),
        coins=['SOL-USDT-SWAP'], exchange_factory=lambda: many, sleep_fn=NO_SLEEP)
    _, ordered = load_existing_timestamps(m.build_save_path('SOL-USDT-SWAP', '1m', tmp2))
    # 限幅按批生效（首 batchSize=300 ≥ 100 即停），不会单轮无上限拖时间
    ck(r == 'success' and 0 < len(ordered) <= 300,
       f'单轮限幅生效（落盘 {len(ordered)} 根 ≤ 300）')

    print('== 6) 软开关 disabled / 互斥 skipped_busy ==')
    r = m.run_kline_fixed_fetch_job(
        cfg=cfg_for(enabled=False), coins=['BTC-USDT-SWAP'],
        exchange_factory=lambda: fake, sleep_fn=NO_SLEEP)
    ck(r == 'disabled', 'enabled=false 直接退出')
    m._run_lock.acquire()
    try:
        r = m.run_kline_fixed_fetch_job(
            cfg=cfg_for(), coins=['BTC-USDT-SWAP'],
            exchange_factory=lambda: fake, sleep_fn=NO_SLEEP)
        ck(r == 'skipped_busy', '上一轮未结束时不并发重跑')
    finally:
        m._run_lock.release()

    print('== 7) 配置坏值兜底（kv 桩，不连 DB）==')
    stub = types.ModuleType('config_store_repo')
    stub.load_json_config_cached = lambda key: {
        'max_workers': 99, 'interval_minutes': 0, 'timeframe': '1m',
        'since_days': -5, 'enabled': True}
    stub.invalidate_config_cache = lambda key: None
    sys.modules['config_store_repo'] = stub
    for var in ('CRYPTO_KLINE_FETCH_WORKERS', 'CRYPTO_KLINE_FETCH_INTERVAL_MIN'):
        os.environ.pop(var, None)
    cfg = m.load_config()
    ck(cfg['max_workers'] == 3, f'workers=99 越界回默认 3（实际 {cfg["max_workers"]}）')
    ck(cfg['interval_minutes'] == 10, f'interval=0 越界回默认 10（实际 {cfg["interval_minutes"]}）')
    ck(cfg['since_days'] == 30, 'since_days=-5 越界回默认 30')
    stub.load_json_config_cached = lambda key: {'max_workers': 4, 'interval_minutes': 30}
    cfg = m.load_config()
    ck(cfg['max_workers'] == 4 and cfg['interval_minutes'] == 30, '合法 kv 值透传')

    print('=' * 50)
    print('KLINE_FIXED_FETCH SMOKE ALL PASS')


if __name__ == '__main__':
    run_smoke()
