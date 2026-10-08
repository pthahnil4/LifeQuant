# -*- coding: utf-8 -*-
"""
冒烟测试：监控台币种 1 分钟 K 线实时拉取（kline_live_service · 全量回填引擎）
====================================================================
离线运行：假交易所（自带 ccxt 市表）+ kv 配置桩（sys.modules 注入，不连数据库、
不连 OKX），全程写临时目录，不碰真实 data/ 与 kline_fetcher/data/live。

假交易所严格照抄 OKX 语义：fetch_ohlcv(symbol, tf, since, limit) 返回
[since, since + limit×step) 区间内存在的整根（升序），地板以下/真空洞返回空数组
——「地板以下返回空」是全量回填与二分探测的地基，这条语义错了整份测试自欺欺人。

覆盖场景：
  1. 配置默认值与坏值兜底（回填口径/单轮预算/批间隔/回看天数上限）
  2. 数据地板二分探测（分钟级精确 + 请求次数上界 + 地板以下返回空）
  3. earliest 首拉：从地板起，游标只向前，单流上限限幅
  4. 分轮续补 + 追平后零次历史请求 + 尾段只补新增且必是已收盘整根
  5. 台账已有近段：早于首根那段先写分段文件，追平后流式归并回主文件
  6. 交易所侧真空洞：缺口精确记账（不欠账）+ 连续空批停机
  7. 网络异常：游标原地不动、不记缺口、下一轮原位重试
  8. days 口径（不起探测请求）与口径切换重置
  9. 进度口径：分母 = 地板→现在，扣掉交易所侧缺口，ETA 用实测速率外推
 10. run_backfill 连续跑批：--rows 语义 / 与调度轮互斥 / 一路追平 / 人工停止
 11. 百万行级台账工具：csv_stats 精确、rows_meta 追加估算、单批 300 根
 12. 回填游标落快照：重启后不重探、不重复请求、接着向前
 13. CSV 只读工具容错（尾部半行、有序归并、小文件排序去重）
 14. 接口契约（backfill start/stop、audit、meta/coins 新字段）+ 导航接线 + 页面渲染
 15. 多币种：注册表按币种 × 类型、ccxt 市表校验（无数据源跳过不报错）、按币种进度
运行：python -m crypto._smoke_kline_live
"""
import datetime
import json
import os
import sys
import tempfile
import time
import types

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

STEP = 60_000
DAY = 86_400_000
NO_SLEEP = lambda *_: None  # noqa: E731
T0 = 1_760_000_000_000 - (1_760_000_000_000 % STEP)     # 假交易所墙钟（对齐整分钟）
FLOOR = T0 - 3 * DAY                                     # 假数据地板
FULL_N = 3 * DAY // STEP                                 # 地板→now-1根


def ck(cond, label, detail=''):
    if not cond:
        raise AssertionError(f'{label} 失败: {detail}')
    print(f'  [ok] {label}')


def _fmt(ms):
    if not ms:
        return '--'
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime('%Y-%m-%d %H:%M')


def mk_market(base, mtype, active=True, real_type=None):
    """照 ccxt okx 形态造一条 market：现货/永续的 symbol 与 market id 都不是同一个。"""
    swap = mtype == 'swap'
    sym = f'{base}/USDT:USDT' if swap else f'{base}/USDT'
    return {sym: {'id': f'{base}-USDT-SWAP' if swap else f'{base}-USDT',
                  'symbol': sym, 'base': base, 'quote': 'USDT',
                  'type': real_type or mtype, 'active': active}}


# 币种清单被桩死成只剩 BTC（见 run_smoke 里的 WATCH），默认市表就得有 BTC 双流
BTC_MARKETS = {**mk_market('BTC', 'swap'), **mk_market('BTC', 'spot')}


class FakeOkx:
    """假交易所：bars = {symbol: {ts: (o,h,l,c,v)}}，接口对齐 ccxt 并统计请求次数。

    markets 就是 ccxt 的市场表（校验链只读它，不发请求）；markets 为空时才走
    load_markets()（真实 ccxt 实例在 load 之前 markets 就是空 dict），loaded 用来演
    「load 完拿到哪份表」；markets_error 非空则模拟市表加载失败 —— 「市表拿不到」
    与「这个币真没市场」是两件事，前者不能把整张清单误标成无数据源。
    """

    def __init__(self, bars, now_ms=T0, markets=None, loaded=None, markets_error=None):
        self._bars = bars
        self.now_ms = now_ms
        self.id = 'okx'
        self.rateLimit = 0
        self.calls = 0
        self.fail_next = 0          # >0 时抛网络错误（模拟 ccxt NetworkError）
        self.markets = BTC_MARKETS if markets is None else markets
        self._loaded = self.markets if loaded is None else loaded
        self.markets_error = markets_error
        self.load_calls = 0

    def load_markets(self):
        self.load_calls += 1
        if self.markets_error:
            raise RuntimeError(self.markets_error)
        self.markets = self._loaded
        return self._loaded

    def parse_timeframe(self, tf):
        return 60

    def milliseconds(self):
        return self.now_ms

    def fetch_ohlcv(self, symbol, timeframe='1m', since=None, limit=None):
        self.calls += 1
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError('fake network error')
        data = self._bars.get(symbol, {})
        if since is None:
            since = min(data) if data else 0
        span = (limit or 100) * STEP          # OKX：窗口是 [since, since+limit×step)
        out = sorted(t for t in data if since <= t < since + span)
        return [[t, *data[t]] for t in out]

    def fetch_ticker(self, symbol):
        return {'last': 100.5, 'timestamp': self.now_ms}


def base_bars():
    """地板 → now-1 根的完整 1m 数据（每个用例拿自己的副本，避免相互污染）。"""
    return {FLOOR + i * STEP: (100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i)
            for i in range(FULL_N)}


def run_smoke():
    # kv 配置桩常驻整份测试：任何 load_config() 都不碰 MySQL
    stub = types.ModuleType('crypto.config_store_repo')
    stub.load_json_config_cached = lambda key: {}
    stub.invalidate_config_cache = lambda key: None
    # 桩得守住真实模块的导入面（navigation_routes 拉 CorruptConfigError），
    # 不然冒烟挂在假模块上，报的是「桩不够真」而不是被测代码的问题
    stub.CorruptConfigError = type('CorruptConfigError', (Exception,), {})
    sys.modules['crypto.config_store_repo'] = stub
    import crypto
    crypto.config_store_repo = stub

    import crypto.kline_live_service as svc
    from crypto.kline_fetcher.csv_store import (
        append_rows, csv_stats, merge_ordered_csv, read_tail_rows, scan_continuity,
        sort_and_dedup_csv, tail_last_timestamp, write_header_if_needed)
    from crypto.kline_fetcher.exchange_fetcher import limit_for_exchange

    # 监控台币种全集被假函数顶成只剩 BTC：不拦住它，load_config 里 coins 为空时会去读真实
    # 监控台清单（五十几个币），每轮上百条流跑假数据，既慢又让后面的断言全部失真。
    # 第 15 组拿它当“监控台加了币”的注入点（改 WATCH 即改清单）。
    WATCH = ['BTC']
    svc.watch_coins = lambda: list(WATCH)

    def clear_markets_cache():
        svc._markets_cache.update({'at': 0.0, 'markets': {}, 'error': None})

    snap_dir = tempfile.mkdtemp(prefix='kl_snap_')
    svc._snapshot_path = lambda: os.path.join(snap_dir, 'kline_live_snapshot.json')
    now = T0
    clear_markets_cache()
    swap = svc.get_stream('btc:swap')
    ck(swap is not None and swap['key'] == 'btc:swap'
       and swap['symbol'] == 'BTC/USDT:USDT'
       and swap['filename'] == 'btc_ohlcv_live_1m_swap.csv',
       f'注册表按「币种×类型」建表，key=btc:swap（实际 {swap and swap["key"]}）')
    ck(svc.get_stream('swap')['key'] == 'btc:swap' and svc.get_stream('spot') is not None,
       '旧的裸 key（swap/spot）仍认识，迁到 btc:swap / btc:spot')

    def cfg_for(tmp, **over):
        cfg = dict(svc.DEFAULT_CONFIG)
        # schedule_backfill=True 保留原有那 13 组对「定时轮顺带推历史」的验证；
        # 默认口径（False）由第 15 组单独验：新币起头只取近段，历史交给跑批
        cfg.update({'data_dir': tmp, 'max_candles_per_stream': 0, 'round_budget_sec': 40,
                    'request_interval_ms': 0, 'interval_seconds': 60, 'streams': ['swap'],
                    'schedule_backfill': True})
        cfg.update(over)
        return cfg

    def reset_state(*keys):
        for k in (keys or ('btc:swap', 'btc:spot')):
            svc._backfill_state.pop(k, None)
            svc._backfill_stop.pop(k, None)
            svc._streams_state.pop(k, None)
            svc._events.clear()
        svc._ledger_cache.clear()

    def view(cfg, key='btc:swap'):
        return [s for s in svc.get_status(cfg)['streams'] if s['key'] == key][0]

    # ========================================================== 1) 配置兜底
    print('== 1) 配置默认值与坏值兜底（kv 桩，不连 DB）==')
    stub.load_json_config_cached = lambda key: {
        'interval_seconds': 5, 'since_days': 0, 'max_candles_per_stream': -1,
        'timeframe': '  ', 'streams': ['swap', 'nope', 'SPOT', 'swap'],
        'coins': ['btc-usdt-swap', 'ETH', 'eth', '', 'sol/USDT'],
        'backfill_mode': 'nonsense', 'round_budget_sec': 99999, 'request_interval_ms': -5}
    for var in ('CRYPTO_KLINE_LIVE_INTERVAL_SEC', 'CRYPTO_KLINE_LIVE_SINCE_DAYS',
               'CRYPTO_KLINE_LIVE_MODE'):
        os.environ.pop(var, None)
    cfg = svc.load_config()
    ck(cfg['backfill_mode'] == 'earliest',
       f'默认口径 = 全量（地板→现在），实际 {cfg["backfill_mode"]}')
    ck(cfg['since_days'] == 7, f'默认回看 7 天仅 days 模式生效（实际 {cfg["since_days"]}）')
    ck(cfg['interval_seconds'] == 60,
       f'interval=5 越界回默认 60（实际 {cfg["interval_seconds"]}）')
    ck(cfg['max_candles_per_stream'] == 0, '单流上限 0 = 只受时间预算约束')
    ck(cfg['round_budget_sec'] == 40 and cfg['request_interval_ms'] == 150,
       f'坏值兜底：预算 {cfg["round_budget_sec"]}s / 间隔 {cfg["request_interval_ms"]}ms')
    ck(cfg['timeframe'] == '1m', '空周期回 1m')
    ck(cfg['streams'] == ['swap', 'spot'], f'未知流剔除+归一+去重（实际 {cfg["streams"]}）')
    ck(cfg['coins'] == ['BTC', 'ETH', 'SOL'],
       f'币种白名单写法归一（inst_id/symbol/裸币名都行）+去重，空项丢弃（实际 {cfg["coins"]}）')
    ck(cfg['schedule_backfill'] is False,
       '定时轮默认不顺带推历史（否则一条流百万根就能挤死同轮其他币的增量）')
    ck(svc.monitor_coins(cfg) == ['BTC', 'ETH', 'SOL'], '白名单非空就收窄拉取范围')
    ck(svc.monitor_coins(dict(cfg, coins=[])) == WATCH,
       '白名单清空 = 取监控台固定币种按市值降序前 12（桩里 WATCH 不超 12 个，切前 12 仍是 WATCH）')
    stub.load_json_config_cached = lambda key: {'backfill_mode': 'days',
                                                'since_days': 30, 'interval_seconds': 90}
    cfg = svc.load_config()
    ck(cfg['backfill_mode'] == 'days' and cfg['since_days'] == 30
       and cfg['interval_seconds'] == 90, '合法 kv 值透传（days 保留为兼容口径）')
    os.environ['CRYPTO_KLINE_LIVE_MODE'] = 'earliest'
    ck(svc.load_config()['backfill_mode'] == 'days',
       '优先级 defaults < env < kv：页面上改的口径不会被 env 默默盖掉')
    os.environ.pop('CRYPTO_KLINE_LIVE_MODE')
    ck(svc.load_config()['backfill_mode'] == 'days', 'env 缺失时仍用 kv 口径')
    stub.load_json_config_cached = lambda key: {}

    # ================================================== 2) 地板二分探测
    print('== 2) 数据地板二分探测（有多少可取 = 地板说了算）==')
    fake = FakeOkx({swap['symbol']: base_bars()})
    req_before = fake.calls
    earliest, reqs = svc.probe_earliest_ms(fake, swap['symbol'], '1m', STEP,
                                          sleep_fn=NO_SLEEP)
    ck(earliest == FLOOR,
       f'探测地板精确到分钟（期望 {_fmt(FLOOR)} 实际 {_fmt(earliest)}）')
    ck(reqs <= 40, f'二分只用 {reqs} 次请求锁定地板（2015→今天 约 30 次量级）')
    ck(fake.calls - req_before == reqs, '探测请求计数与返回值一致')
    ck(fake.fetch_ohlcv(swap['symbol'], '1m', since=FLOOR - 10 * DAY, limit=300) == [],
       '地板以下窗口返回空数组（回填据此判定「交易所确实没有」）')

    # ================================ 3) earliest 首拉：空 CSV 直写主文件
    print('== 3) earliest 首拉：游标从地板起，单流上限限幅 ==')
    tmp = tempfile.mkdtemp(prefix='kl_live_smoke_')
    path = svc.csv_path(swap, tmp)
    cfg3 = cfg_for(tmp, max_candles_per_stream=1500)
    reset_state()
    fake.calls = 0
    r = svc.run_kline_live_job(cfg=cfg3, exchange=fake, sleep_fn=NO_SLEEP)
    st = view(cfg3)
    ck(r == 'success', f'整轮 success（实际 {r}）')
    ck(st['earliest_ms'] == FLOOR and st['floor_source'] == 'probe',
       '地板探测结果落入状态（一次探测长期复用）')
    ck(st['last_backfilled'] == 1500 and st['rows_main'] == 1500,
       f'单流上限 1500 根限幅生效（实际 {st["last_backfilled"]} 根）')
    ck(tail_last_timestamp(path) == FLOOR + 1499 * STEP,
       '游标只向前：落盘最后一根即游标前一根')
    ck(st['head_cursor'] == FLOOR + 1500 * STEP and not st['backfill_done'],
       f'一轮补不完（游标 {_fmt(st["head_cursor"])} / 终点 {_fmt(st["backfill_stop_ms"])}）')
    ck(st['backfill_target_rows'] == FULL_N, f'分母 = 地板→现在 {FULL_N} 根')
    ck(0 < st['backfill_pct'] < 100 and st['backfill_remaining_rows'] == FULL_N - 1500,
       f'未追平 → {st["backfill_pct"]}% 剩余 {st["backfill_remaining_rows"]} 根')

    # ==================================== 4) 分轮续补 + 追平后不重复请历史
    print('== 4) 分轮续补：游标接着向前，追平后零次历史请求 ==')
    rounds = 1
    while not st['backfill_done'] and rounds < 20:
        svc.run_kline_live_job(cfg=cfg3, exchange=fake, sleep_fn=NO_SLEEP)
        rounds += 1
        st = view(cfg3)
    ck(st['backfill_done'] and rounds == 3, f'共 {rounds} 轮追平地板→现在')
    ck(st['rows_main'] == FULL_N and st['backfill_pct'] == 100.0,
       f'全量到位 {st["rows_main"]} 根，进度 {st["backfill_pct"]}%')
    audit = scan_continuity(path, STEP)
    ck(audit['gaps'] == 0 and audit['dup_rows'] == 0 and audit['rows'] == FULL_N,
       f'台账连续、无重复、无缺行（{audit}）')
    fake.calls = 0
    svc.run_kline_live_job(cfg=cfg3, exchange=fake, sleep_fn=NO_SLEEP)
    ck(fake.calls == 0, f'铺满且不推进时钟时零次请求（实际 {fake.calls} 次）')
    # 时钟前进 10 分钟 → 尾段只补 10 根，最后一根必是已收盘整根
    ext = base_bars()
    ext.update({now + i * STEP: (1, 1, 1, 1, 1) for i in range(10)})
    fake._bars[swap['symbol']] = ext
    fake.now_ms = now + 10 * STEP
    fake.calls = 0
    svc.run_kline_live_job(cfg=cfg3, exchange=fake, sleep_fn=NO_SLEEP)
    st = view(cfg3)
    ck(st['last_tail_appended'] == 10 and st['latest_ts'] == now + 9 * STEP,
       f'尾段增量补 10 根且不含正在走的那一分钟（末根 {_fmt(st["latest_ts"])}）')
    ck(fake.calls == 1, f'单批 300 根够用（实际 {fake.calls} 次请求）')

    # ======================= 5) 已有近段：早于首根那段走分段文件 + 归并
    print('== 5) 已有近段台账：早于首根的回填补进分段文件，追平后归并 ==')
    tmp2 = tempfile.mkdtemp(prefix='kl_live_smoke2_')
    path2 = svc.csv_path(swap, tmp2)
    bars = base_bars()
    recent = [[t, *bars[t]] for t in sorted(bars) if t >= now - 200 * STEP]
    write_header_if_needed(path2)      # 真实台账一律带表头，预置数据也得按这个口径造
    ck(append_rows(path2, 'okx', recent) == 200, '预置「最近 200 根」台账')
    reset_state()
    fake2 = FakeOkx({swap['symbol']: bars})
    cfg5 = cfg_for(tmp2)
    r = svc.run_kline_live_job(cfg=cfg5, exchange=fake2, sleep_fn=NO_SLEEP)
    st = view(cfg5)
    ck(r == 'success' and st['backfill_done'], f'一轮即追平（{r}）')
    ck(st['last_backfilled'] == FULL_N - 200,
       f'只补早于首根的那段（{st["last_backfilled"]} 根，不碰已有 200 根）')
    ck('已归并回主文件' in (st['backfill_note'] or ''), f'归并说明：{st["backfill_note"]}')
    ck(not os.path.exists(svc.backfill_file_of(path2)), '分段文件归并后删除')
    a2 = scan_continuity(path2, STEP)
    ck(a2['rows'] == FULL_N and a2['first_ts'] == FLOOR and a2['last_ts'] == now - STEP
       and a2['gaps'] == 0 and a2['dup_rows'] == 0, f'归并后台账时间单调且不重复（{a2}）')
    ck(st['first_ts'] == FLOOR, '状态首根来自 CSV（不是内存计数器）')
    ck(st['rows_main'] == FULL_N and st['rows_total'] == FULL_N, '台账行数与 CSV 一致')

    # ============================= 6) 交易所侧真空洞：缺口记账 + 停机判据
    print('== 6) 交易所侧真空洞：缺口计入「已处理」，进度仍到 100% ==')
    tmp3 = tempfile.mkdtemp(prefix='kl_live_smoke3_')
    path3 = svc.csv_path(swap, tmp3)
    holey = base_bars()
    for i in list(range(100, 140)) + list(range(200, 2200)):
        holey.pop(FLOOR + i * STEP)                # 40 根小洞 + 2000 根大洞
    fake3 = FakeOkx({swap['symbol']: holey})
    reset_state()
    cfg6 = cfg_for(tmp3)
    r = svc.run_kline_live_job(cfg=cfg6, exchange=fake3, sleep_fn=NO_SLEEP)
    st = view(cfg6)
    ck(r == 'success' and st['backfill_done'], '一轮追平（含跨洞跳过）')
    ck(st['backfill_gap_rows'] == 2040,
       f'两个洞都精确记为交易所侧缺口（实际 {st["backfill_gap_rows"]} 根）')
    ck(st['rows_main'] == len(holey) and scan_continuity(path3, STEP)['gaps'] == 2,
       f'只落有数据的 {st["rows_main"]} 根，台账内 2 处断点')
    ck(st['backfill_need_rows'] == FULL_N - 2040 and st['backfill_pct'] == 100.0,
       f'缺口不欠账：应有 {st["backfill_need_rows"]} 根 → {st["backfill_pct"]}%')
    # 地板以上几乎没有数据 → 连续空批到上界停机，不做无谓轮询。
    # 这里只把游标推过 902 分钟（3 天窗口共 4320 分钟，14 批空窗就能跨过止点被判追平），
    # 所以临时把上界调小验证判据本身；真实回填里要 4 天以上的大洞才会撞上这条护栏
    tiny = {FLOOR: (1, 1, 1, 1, 1), FLOOR + STEP: (1, 1, 1, 1, 1)}
    fake4 = FakeOkx({swap['symbol']: tiny})
    tmp4 = tempfile.mkdtemp(prefix='kl_live_smoke4_')
    reset_state()
    cfg6b = cfg_for(tmp4)
    streak_real = svc._EMPTY_STREAK_STOP
    svc._EMPTY_STREAK_STOP = 3
    try:
        svc.run_kline_live_job(cfg=cfg6b, exchange=fake4, sleep_fn=NO_SLEEP)
    finally:
        svc._EMPTY_STREAK_STOP = streak_real
    st = view(cfg6b)
    ck(st['last_halt'] == 'empty_streak',
       f'连续空批到上界（临时 3 批，线上 {streak_real} 批）即停机（halt={st["last_halt"]}）')
    ck(st['rows_main'] == 2 and not st['backfill_done'], '停机时只落了 2 根，游标未追平')
    ck(st['backfill_gap_rows'] >= 900,
       f'空窗那几分钟全记成交易所侧缺口（实际 {st["backfill_gap_rows"]} 根）')

    # =============================== 7) 网络异常：游标不动、不记缺口
    print('== 7) 网络异常：游标原地不动、不计缺口，下一轮原位重试 ==')
    tmp5b = tempfile.mkdtemp(prefix='kl_live_smoke5b_')
    path5b = svc.csv_path(swap, tmp5b)
    cfg7 = cfg_for(tmp5b, max_candles_per_stream=300)
    fake5 = FakeOkx({swap['symbol']: base_bars()})
    reset_state()
    svc.run_kline_live_job(cfg=cfg7, exchange=fake5, sleep_fn=NO_SLEEP)
    cur_before = svc._backfill_state['btc:swap']['head_cursor']
    size_before = os.path.getsize(path5b)
    gap_before = svc._backfill_state['btc:swap']['gap_rows']
    fake5.fail_next = 1
    svc.run_kline_live_job(cfg=cfg7, exchange=fake5, sleep_fn=NO_SLEEP)
    st = view(cfg7)
    ck(st['head_cursor'] == cur_before and os.path.getsize(path5b) == size_before,
       f'异常轮游标与台账都没动（仍在 {_fmt(cur_before)}）')
    ck(svc._backfill_state['btc:swap']['gap_rows'] == gap_before, '网络异常没被当成缺口')
    ck(st['last_halt'] == 'error' and st['last_error'],
       f'异常透出到状态而非「补完了」：{st["last_error"]}')
    svc.run_kline_live_job(cfg=cfg7, exchange=fake5, sleep_fn=NO_SLEEP)
    ck(svc._backfill_state['btc:swap']['head_cursor'] > cur_before, '下一轮原位重试并继续向前')

    # ====================== 8) days 口径（不起探测）与口径切换
    print('== 8) days 口径与口径切换 ==')
    tmp8 = tempfile.mkdtemp(prefix='kl_live_smoke8_')
    path8 = svc.csv_path(swap, tmp8)
    reset_state()
    svc._backfill_state['btc:swap'] = {'mode': 'earliest', 'earliest_ms': FLOOR,
                                       'probed_at_ms': now, 'head_cursor': FLOOR,
                                       'stop_ms': now, 'target_file': path8,
                                       'writes_main': True, 'rows': 0, 'gap_rows': 0,
                                       'batches': 0}
    cfg8 = cfg_for(tmp8, backfill_mode='days', since_days=1)
    day_bars = {now - DAY + i * STEP: (1, 1, 1, 1, 1) for i in range(DAY // STEP)}
    fake8 = FakeOkx({swap['symbol']: day_bars})
    svc.run_kline_live_job(cfg=cfg8, exchange=fake8, sleep_fn=NO_SLEEP)
    st = view(cfg8)
    ck(st['floor_source'] == 'days' and st['earliest_ms'] == now - DAY,
       f'days 口径起点 = now-1天（{_fmt(st["earliest_ms"])}）')
    ck(st['backfill_done'] and st['rows_main'] == DAY // STEP,
       f'一天窗口 {st["rows_main"]} 根铺满')
    ck(fake8.calls == DAY // STEP // 300 + 1,
       f'只请了 {fake8.calls} 批（1440/300 向上取整），零探测请求')
    ck(svc._backfill_state['btc:swap']['mode'] == 'days:1', '游标记录口径键（切换即重置）')

    # ===================================== 9) 进度口径与 ETA
    print('== 9) 进度口径：分母 = 地板→现在，扣缺口，ETA 用实测速率 ==')
    cfg9 = cfg_for(tmp)
    v9 = {'earliest_ms': now - 2 * DAY, 'now_ms': now, 'rows_total': 1440,
          'backfill_gap_rows': 0, 'backfill_rate': 1000.0, 'latest_ts': now - STEP}
    prog = svc._backfill_progress(dict(v9), cfg9)
    ck(prog['backfill_target_rows'] == 2880 and prog['backfill_need_rows'] == 2880,
       f'地板→现在 2 天 = 2880 根（实际 {prog["backfill_target_rows"]}）')
    ck(prog['backfill_pct'] == 50.0 and prog['backfill_remaining_rows'] == 1440,
       f'到位 1440 → 50%（实际 {prog["backfill_pct"]}%）')
    ck(prog['backfill_eta_sec'] == 1, f'ETA = 1440/1000 ≈ {prog["backfill_eta_sec"]}s')
    prog = svc._backfill_progress(dict(v9, backfill_gap_rows=480), cfg9)
    ck(prog['backfill_need_rows'] == 2400 and prog['backfill_pct'] == 60.0,
       f'交易所侧缺 480 根不欠账：应有 2400 → 60%（实际 {prog["backfill_pct"]}%）')
    prog = svc._backfill_progress({'rows_total': 10, 'now_ms': now}, cfg9)
    ck(prog['backfill_target_rows'] == 0 and prog['backfill_pct'] is None,
       '未探测到地板 → 目标未知（pct=null），不拿假分母骗页面')
    prog = svc._backfill_progress(dict(v9, backfill_done=True, rows_total=2880,
                                       latest_ts=now - 3 * STEP), cfg9)
    ck(prog['backfill_pct'] == 100.0 and prog['backfill_eta_sec'] == 0
       and prog['backfill_remaining_rows'] == 2,
       f'追平后固定 100%，差额转为实时滞后（{prog["backfill_remaining_rows"]} 根）')
    ck(svc._target_rows(cfg9, {'earliest_ms': now - DAY, 'now_ms': now}) == 1440,
       '1 天窗口目标 1440 根')

    # ============================= 10) run_backfill 连续跑批
    print('== 10) run_backfill 连续跑批：--rows 语义 / 互斥 / 追平 / 停止 ==')
    tmp10 = tempfile.mkdtemp(prefix='kl_live_smoke10_')
    path10 = svc.csv_path(swap, tmp10)
    cfg10 = cfg_for(tmp10, round_budget_sec=3000)
    fake10 = FakeOkx({swap['symbol']: base_bars()})
    reset_state()
    seen = []
    res = svc.run_backfill('btc:swap', until_rows=900, cfg=cfg10, exchange=fake10,
                           sleep_fn=NO_SLEEP,
                           on_round=lambda p: seen.append(p['appended']))
    ck(res['ok'] and res['appended'] == 900 and res['halt'] == 'until_rows',
       f'跑到 until_rows 就收工（新增 {res["appended"]} 根，halt={res["halt"]}）')
    ck(res['head_cursor'] == FLOOR + 900 * STEP, '游标停在最后一根之后一分钟（断点）')
    ck(seen == [900], f'进度回调逐段上报：{seen}')
    ck(res['rate'] and res['rate'] > 0, f'实测速率写入游标（{res["rate"]} 根/秒）')
    st10 = svc._backfill_state['btc:swap']
    rate_before, cur_before = st10['rate'], st10['head_cursor']
    starved = svc._consume_backfill(swap, cfg10, path10, st10,
                                    fake10, STEP, time.time() - 1, NO_SLEEP)
    ck(starved['appended'] == 0 and st10['rate'] == rate_before
       and st10['head_cursor'] == cur_before,
       '预算为 0 的段不落根也不污染实测速率（否则下一条流的 ETA 会被假速率拖成几十小时）')
    svc._run_lock.acquire()
    wait_real = svc._RUN_LOCK_WAIT_SEC
    svc._RUN_LOCK_WAIT_SEC = 0        # 争抢用例不能真的等 90s
    try:
        busy = svc.run_backfill('btc:swap', cfg=cfg10, exchange=fake10, sleep_fn=NO_SLEEP)
        ck(not busy['ok'] and 'CSV' in busy['error'],
           '与调度轮争抢同一份 CSV 时拒绝并发（单写入者）')
    finally:
        svc._run_lock.release()
        svc._RUN_LOCK_WAIT_SEC = wait_real
    ck(wait_real > 0 and svc.run_backfill(
        'btc:swap', cfg=cfg10, exchange=fake10, until_rows=300, sleep_fn=NO_SLEEP)['ok'],
        '台账空出来时跑批正常拿到写入权（排队而不是一口回绝）')
    try:
        svc.run_backfill('nope', cfg=cfg10)
        raise AssertionError('未知数据流应抛 ValueError')
    except ValueError:
        ck(True, '未知数据流抛 ValueError（接口层转 400）')
    res = svc.run_backfill('btc:swap', cfg=cfg10, exchange=fake10, sleep_fn=NO_SLEEP)
    ck(res['done'] and res['halt'] == 'caught_up',
       f'连续跑批一路追平（新增 {res["appended"]} 根，halt={res["halt"]}）')
    ck(scan_continuity(path10, STEP)['rows'] == FULL_N,
       f'跑批后台账完整（{scan_continuity(path10, STEP)["rows"]} 根）')
    ck(svc.stop_full_backfill('btc:swap')['stopping'] == ['btc:swap']
       and svc._backfill_stop.get('btc:swap'),
       'stop_full_backfill 置停止标志并归一成完整 key（游标保留）')
    before = svc._backfill_state['btc:swap']['head_cursor']
    ck(svc.run_backfill('btc:swap', cfg=cfg10, exchange=fake10,
                        sleep_fn=NO_SLEEP)['halt'] == 'stopped'
       and svc._backfill_state['btc:swap']['head_cursor'] == before,
       '停止态下跑批立即返回且游标不前移')

    # ==================== 11) 百万行级台账工具：精确 vs 估算
    print('== 11) 台账工具：csv_stats 精确 / rows_meta 估算 / 单批 300 根 ==')
    rows, first_ts, avg = csv_stats(path10)
    ck(rows == FULL_N and first_ts == FLOOR and avg > 0,
       f'csv_stats 分块扫出 {rows} 根，首根 {_fmt(first_ts)}，平均 {avg:.0f} 字节/行')
    ck(limit_for_exchange('okx', '1m') == 300, '单批 300 根（OKX 公共行情上限）')
    e_rows, e_first = svc.rows_meta(path10)
    ck(e_rows == rows and e_first == FLOOR, 'rows_meta 命中记忆化 = 精确值')
    with open(path10, 'a', encoding='utf-8') as f:
        f.write(f'{now},{now},1,1,1,1,1,okx\n')
    g_rows, _ = svc.rows_meta(path10)
    ck(abs(g_rows - (rows + 1)) <= 1, f'只追加时按字节差估算（估 {g_rows}，真 {rows + 1}）')

    # ========================= 12) 游标落快照：重启不丢、不重探
    print('== 12) 回填游标持久化：重启后接着向前，不再起探测 ==')
    tmp12 = tempfile.mkdtemp(prefix='kl_live_smoke12_')
    cfg12 = cfg_for(tmp12, max_candles_per_stream=600)
    fake12 = FakeOkx({swap['symbol']: base_bars()})
    reset_state()
    svc.run_kline_live_job(cfg=cfg12, exchange=fake12, sleep_fn=NO_SLEEP)
    st = view(cfg12)
    ck(not st['backfill_done'] and st['head_cursor'] == FLOOR + 600 * STEP,
       f'第一轮推进到 {_fmt(st["head_cursor"])}')
    bf_snapshot = dict(svc._backfill_state['btc:swap'])
    reset_state()
    ck(not svc._backfill_state, '模拟进程重启：内存游标清空')
    svc._load_snapshot()
    ck(svc._backfill_state.get('btc:swap', {}).get('head_cursor')
       == bf_snapshot['head_cursor']
       and svc._backfill_state['btc:swap']['earliest_ms'] == FLOOR,
       '快照恢复游标与地板（重启不丢断点）')
    fake12.calls = 0
    svc.run_kline_live_job(cfg=cfg12, exchange=fake12, sleep_fn=NO_SLEEP)
    st = view(cfg12)
    ck(st['head_cursor'] > bf_snapshot['head_cursor'] and not st['backfill_done'],
       f'重启后从断点继续（游标 {_fmt(st["head_cursor"])}）')
    ck(fake12.calls == 2, f'复用快照地板不再二分（{fake12.calls} 次请求 = 2 批数据）')
    for key in ('earliest_ms', 'head_cursor', 'stop_ms', 'rows', 'gap_rows', 'batches',
                'mode', 'done', 'rate', 'target_file', 'writes_main'):
        ck(key in bf_snapshot, f'游标字段 {key} 已落快照')
    with open(svc._snapshot_path(), encoding='utf-8') as f:
        snap_raw = json.load(f)
    ck('btc:swap' in snap_raw['backfill'] and 'swap' not in snap_raw['backfill'],
       f'快照按完整 stream key 落盘（实际 {sorted(snap_raw["backfill"])}）')
    ck(os.path.isfile(svc.backfill_file_of(path)) or svc._backfill_state['btc:swap']
       ['writes_main'], '冷启动直写主文件（无需分段）或分段文件已就位')
    # 中途被杀：快照里的游标比台账旧，续跑起点必须以文件为准，不能重复 append
    stt = svc._backfill_state['btc:swap']
    seg12 = stt['target_file']
    cur_ok = int(stt['head_cursor'])
    with open(seg12, 'a', encoding='utf-8') as f:
        for t in range(cur_ok, cur_ok + 200 * STEP, STEP):
            f.write(f'{t},{t},1,1,1,1,1,okx\n')
    stt['head_cursor'] = cur_ok - 100 * STEP      # 快照落后台账 300 根
    fake12.calls = 0
    svc.run_kline_live_job(cfg=cfg12, exchange=fake12, sleep_fn=NO_SLEEP)
    st = view(cfg12)
    ck(st['head_cursor'] == cur_ok + 200 * STEP + 600 * STEP,
       f'续跑对齐文件末根而非旧快照（游标 {_fmt(st["head_cursor"])}，'
       f'期望 {_fmt(cur_ok + 800 * STEP)}）')
    ck(tail_last_timestamp(seg12) == st['head_cursor'] - STEP
       and scan_continuity(seg12, STEP)['dup_rows'] == 0,
       '没有把已经到手的 300 根重复落盘')

    # ============================== 13) CSV 只读工具容错
    print('== 13) CSV 只读工具容错 ==')
    tail = read_tail_rows(path10, 5)
    ck(len(tail) == 5 and tail[-1]['timestamp'] == now and tail[0]['timestamp'] < now,
       'read_tail_rows 返回末段升序 5 行')
    ck(tail[1]['close'] is not None and tail[1]['exchange'] == 'okx', '尾部行字段解析完整')
    p_half = os.path.join(tempfile.mkdtemp(prefix='kl_live_smoke13_'), 'tail.csv')
    write_header_if_needed(p_half)
    with open(p_half, 'a', encoding='utf-8') as f:
        f.write('\n'.join(f'{now - i * STEP},x,{i},{i},{i},{i},{i},okx'
                          for i in range(9, -1, -1)))
        f.write('176000009999')              # 并发追加时被读到的半行
    bad = read_tail_rows(p_half, 3)
    ck(len(bad) == 3 and bad[-1]['timestamp'] == now, '尾部半行被丢弃且不抛异常')
    ck(bad[0]['timestamp'] == now - 2 * STEP, '按 limit 截断（升序）正确')
    p_a = os.path.join(tmp12, 'a.csv')
    p_b = os.path.join(tmp12, 'b.csv')
    write_header_if_needed(p_a)
    write_header_if_needed(p_b)
    append_rows(p_a, 'okx', [[t, 1, 1, 1, 1, 1] for t in range(now - 5 * STEP, now, STEP)])
    append_rows(p_b, 'okx', [[t, 2, 2, 2, 2, 2] for t in
                             range(now - 10 * STEP, now - 2 * STEP, STEP)])
    merged = merge_ordered_csv(p_a, p_b)
    ck(merged == 10 and scan_continuity(p_a, STEP)['gaps'] == 0,
       f'两段有序流归并成 10 根且连续（merge={merged}）')
    ck(not os.path.exists(p_b), '归并后分段文件删除')
    ck(sort_and_dedup_csv(p_a) == 10, '小文件排序去重仍可用（万行级工具）')
    try:
        svc.get_bars('unknown_stream', 5)
        raise AssertionError('未知数据流应抛 ValueError')
    except ValueError:
        ck(True, '未知数据流抛 ValueError（接口层转 400）')

    # ==================== 14) 接口契约 + 导航接线 + 页面渲染
    print('== 14) 接口契约 / 导航接线 / 页面渲染 ==')
    stub.load_json_config_cached = lambda key: {'data_dir': tmp10, 'streams': ['swap']}
    started = {}
    real_start_full = svc.start_full_backfill

    def fake_start(keys=None, budget=None, coins=None):
        started['keys'] = keys
        started['coins'] = coins
        return {'started': True, 'streams': keys, 'coins': ['BTC'], 'skipped': [],
                'message': 'ok'}

    svc.start_full_backfill = fake_start
    import run_kline_live
    client = run_kline_live.build_app().test_client()
    j = client.get('/kline-live/api/meta').get_json()
    ck(j['success'] and [m['value'] for m in j['backfill_modes']] == ['earliest', 'days'],
       'meta 暴露两种回填口径（全量 / 仅最近 N 天）')
    ck(j['config']['backfill_mode'] in svc.BACKFILL_MODES
       and j['limits']['since_days'][1] == 36500, 'meta 配置与上限随之更新')
    ck([t['value'] for t in j['stream_types']] == ['swap', 'spot']
       and j['coins'][0]['coin'] == 'BTC' and j['coins'][0]['streams'] == ['btc:swap']
       and j['streams'][0]['key'] == 'btc:swap' and 'coin' in j['streams'][0]
       and j['streams'][0]['enabled'] is True,
       'meta 把币种/类型/启用与否一并下发（前端选择器就靠这几列）')
    coins_j = client.get('/kline-live/api/coins').get_json()['data']
    ck([c['coin'] for c in coins_j['coins']] == ['BTC']
       and coins_j['streams'][0]['filename'].startswith('btc_')
       and coins_j['watch_coins'] == WATCH and coins_j['selected_types'] == ['swap'],
       '/api/coins 给币种目录（不触网：markets 没真去 load）')
    j = client.get('/kline-live/api/status').get_json()
    top = j['streams'][0]
    ck(all(k in j for k in ('backfill_mode', 'round_budget_sec',
                           'request_interval_ms', 'backfill_running')),
       'status 顶层回显回填口径、预算、批间隔与运行标志')
    ck(all(k in j for k in ('coins', 'coins_total', 'coins_available', 'coins_missing',
                           'coins_unchecked', 'streams_total', 'streams_skipped',
                           'watch_coins', 'schedule_backfill')),
       'status 带币种维度字段（页面按币种看进度就靠这些）')
    ck(all(k in top for k in ('earliest_ms', 'head_cursor', 'backfill_rate',
                             'backfill_eta_sec', 'backfill_need_rows', 'backfill_gap_rows',
                             'backfill_paused', 'backfill_temp', 'coin', 'stream_type',
                             'available', 'market_note')),
       '每条流带齐全量回填指标与币种/数据源结论')
    ck(client.post('/kline-live/api/backfill/start', json={'streams': 'swap'}).status_code
       == 400, 'streams 传字符串 → 400（不接受脏参数）')
    ck(client.post('/kline-live/api/backfill/start',
                   json={'coins': 5}).status_code == 400, 'coins 传数字 → 400')
    ck(client.post('/kline-live/api/backfill/start',
                   json={'budget_sec': 'abc'}).status_code == 400, 'budget_sec 非整数 → 400')
    ck(client.post('/kline-live/api/backfill/start',
                   json={'streams': ['btc:swap']}).status_code == 200
       and started.get('keys') == ['btc:swap'], '回填启动接口把完整 key 传给服务层')
    ck(client.post('/kline-live/api/backfill/start',
                   json={'coins': 'BTC,ETH'}).status_code == 200
       and started.get('coins') == 'BTC,ETH', '按币种启动回填（页面只回填当前看的币）')
    ck(client.post('/kline-live/api/backfill/stop',
                   json={'stream': 'nope'}).status_code == 400, '停未知流 → 400')
    ck(client.get('/kline-live/api/audit?stream=nope').status_code == 400, '审计未知流 → 400')
    ck(client.get('/kline-live/api/audit?stream=btc:spot').status_code == 400,
       '类型未启用的流也不认（streams 只选了 swap）')
    ck(client.get('/kline-live/api/bars?stream=zzz:swap').status_code == 400, '查未知流 → 400')
    au = client.get('/kline-live/api/audit?stream=btc:swap').get_json()['data']
    ck(au['stream'] == 'btc:swap' and au['coin'] == 'BTC' and au['stream_type'] == 'swap'
       and au['rows_exact'] >= FULL_N and au['main']['first_ts'] == FLOOR
       and au['dup_rows'] == 0 and au['target_rows'] > 0,
       f'审计给出精确台账 {au["rows_exact"]} 根 / 目标 {au["target_rows"]} 根')
    ck(client.get('/kline-live/api/audit?stream=swap').status_code == 200,
       '旧写法 ?stream=swap 仍能用（迁到 btc:swap）')
    svc.start_full_backfill = real_start_full
    # 拿回真函数后走真校验：排队前先查 key，列不存在的币不能进队列（也不发请求）
    ck(client.post('/kline-live/api/backfill/start',
                   json={'streams': ['doge:swap']}).status_code == 400,
       '非监控台币种的流 → 400（而不是默默少跑一个币）')
    reset_state()
    del sys.modules['crypto.config_store_repo']
    del crypto.config_store_repo

    from crypto import navigation_service as ns
    nav_item = next((it for it in ns.DEFAULT_ITEMS if it.get('builtin_key') == 'kline-live'),
                    None)
    ck(nav_item is not None and nav_item['parent_id'] == 'group-market'
       and nav_item['url'] == '/kline-live', 'DEFAULT_ITEMS 里「行情 → K线拉取」入口就位')
    ck(nav_item['icon']['value'] in ns._ICON_BUILTIN_KEYS, '图标键在写接口白名单内')
    ck(ns.ACTIVE_PAGE_ALIASES.get('kline-live') == '/kline-live', 'active_page 别名已登记')
    canonical, _warn = ns.validate_and_normalize(ns.build_default_config())
    ck('link-kline-live' in {it['id'] for it in canonical['items']}, '种子整份能过导航校验')
    ck(ns.resolve_active(ns.DEFAULT_ITEMS, '/kline-live', None)[0] == 'link-kline-live',
       '访问 /kline-live 时导航高亮到本项')
    html = client.get('/kline-live').get_data(as_text=True)
    ck('data-nav-id="link-kline-live"' in html and nav_item['name'] in html,
       f"渲染出的导航条含入口链接（{nav_item['name']}）")
    ck('id="kl-bf-start"' in html and 'id="kl-mode"' in html and 'id="kl-audit"' in html,
       '页面含全量回填 / 口径切换 / 台账体检控件')
    ck('id="kl-coin"' in html and 'id="kl-coin-table"' in html
       and 'id="kl-coin-pick"' in html, '页面含币种选择器与币种回填总览表')

    # ============ 15) 多币种：注册表 × ccxt 校验跳过 × 按币种进度 ============
    print('== 15) 多币种注册表 / ccxt 市表校验 / 按币种进度 ==')
    # 14) 结尾把 kv 桩卸了（导航那边要真模块），这组还得靠它兜住 load_config
    sys.modules['crypto.config_store_repo'] = stub
    crypto.config_store_repo = stub
    WATCH[:] = ['BTC', 'ETH', 'XRP', 'DOGE']
    tmp15 = tempfile.mkdtemp(prefix='kl_live_smoke15_')
    stub.load_json_config_cached = lambda key: {'data_dir': tmp15,
                                                'streams': ['swap', 'spot'],
                                                'round_budget_sec': 40,
                                                'request_interval_ms': 0}
    clear_markets_cache()
    # 默认口径：定时轮不推历史（新币靠起头窗拿近期），历史交给逐币跑批
    cfg15 = cfg_for(tmp15, streams=['swap', 'spot'], schedule_backfill=False,
                    max_candles_per_stream=600)
    keys15 = [f'{b.lower()}:{t}' for b in WATCH for t in ('swap', 'spot')]
    reset_state(*keys15)
    defs = svc.all_streams(cfg15)
    ck([d['key'] for d in defs] == keys15 and len(defs) == 8,
       f'币种清单 × 启用类型 = {len(defs)} 条流（实际 {[d["key"] for d in defs]}）')
    d15 = {d['key']: d for d in defs}
    ck(d15['eth:swap']['filename'] == 'eth_ohlcv_live_1m_swap.csv'
       and d15['eth:spot']['filename'] == 'eth_ohlcv_live_1m_spot.csv'
       and len({d['filename'] for d in defs}) == 8, 'CSV 文件名按币种区分（每币两个文件）')
    ck(d15['eth:swap']['symbol'] == 'ETH/USDT:USDT'
       and d15['eth:swap']['market_id'] == 'ETH-USDT-SWAP'
       and d15['eth:spot']['symbol'] == 'ETH/USDT'
       and d15['eth:spot']['market_id'] == 'ETH-USDT',
       '永续/现货的 symbol 与 market id 成对区分（spot 与 swap 是两个 market）')
    ck(all(d['available'] is None for d in defs if d['coin'] != 'BTC'),
       '未查过市表的新币 available=None（“没查过”不等于“无数据源”）')
    ck(svc.stream_keys_for('BTC', cfg=cfg15) == ['btc:swap', 'btc:spot']
       and svc.stream_keys_for('btc-usdt-swap', cfg=cfg15) == ['btc:swap', 'btc:spot']
       and svc.stream_keys_for('btc', types=['spot'], cfg=cfg15) == ['btc:spot']
       and svc.stream_keys_for(['ETH', 'xrp-usdt'], cfg=cfg15)
       == ['eth:swap', 'eth:spot', 'xrp:swap', 'xrp:spot']
       and svc.stream_keys_for('all', cfg=cfg15) == keys15,
       '币种/类型 → stream key 翻译（监控台 inst_id 写法当币种名，类型另用 types 选）')
    try:
        svc.stream_keys_for('ADA', cfg=cfg15)
        raise AssertionError('清单里没有的币应抛 ValueError')
    except ValueError:
        ck(True, '非监控台币种抛 ValueError（接口层转 400）')

    # ---- 开拉前的 ccxt 校验：现货/永续分开算，查不到就跳过
    MKT = {**mk_market('BTC', 'swap'), **mk_market('BTC', 'spot'),
           **mk_market('ETH', 'swap'),                     # ETH 只有永续，现货没给
           **mk_market('XRP', 'swap'),
           'XRP/USDT': {'id': 'XRP-USDT', 'symbol': 'XRP/USDT', 'type': 'swap',
                        'active': True},                   # 现货位上挂的是 swap 市场
           **mk_market('DOGE', 'spot', active=False)}       # 现货下架，永续压根没这个市场
    mk = svc.resolve_markets(FakeOkx({}, markets=MKT), cfg=cfg15)
    ck(mk['checked'] == 8 and mk['available'] == 4 and mk['missing'] == 4
       and mk['unknown'] == 0 and not mk['error'], f'开拉前逐条校验市表：{mk}')
    d15 = {d['key']: d for d in svc.all_streams(cfg15)}
    ck(d15['eth:spot']['available'] is False
       and str(d15['eth:spot']['market_note']).startswith(svc.NO_SOURCE_MARK)
       and 'ETH-USDT' in d15['eth:spot']['market_note'],
       f'现货查不到 → 无数据源：{d15["eth:spot"]["market_note"]}')
    ck('实际市场类型' in d15['xrp:spot']['market_note'],
       f'市场类型不符也算无数据源：{d15["xrp:spot"]["market_note"]}')
    ck('已下架' in d15['doge:spot']['market_note']
       and 'DOGE-USDT-SWAP' in d15['doge:swap']['market_note'], '下架/查不到分别写明原因')
    ck(d15['btc:swap']['available'] is True and d15['btc:swap']['market_note'] is None
       and d15['xrp:swap']['available'] is True, '能拉的流不背备注')

    # ---- 任务轮：无数据源的跳过不报错、不建文件；新币起头先落到最近一整窗
    bars15 = {swap['symbol']: base_bars(), d15['btc:spot']['symbol']: base_bars(),
              d15['eth:swap']['symbol']: base_bars(), d15['xrp:swap']['symbol']: base_bars()}
    fake15 = FakeOkx(bars15, markets=MKT)
    r = svc.run_kline_live_job(cfg=cfg15, exchange=fake15, sleep_fn=NO_SLEEP)
    ck(r == 'success', f'无数据源的币种被跳过而不是报错（本轮返回 {r}）')
    ck({s['key'] for s in svc.active_streams(cfg15)}
       == {'btc:swap', 'btc:spot', 'eth:swap', 'xrp:swap'}, '本轮队列只剩 okx 真有市场的流')
    for k in ('eth:spot', 'xrp:spot', 'doge:swap', 'doge:spot'):
        ck(not os.path.exists(svc.csv_path(d15[k], tmp15)),
           f'{k} 连台账文件都不建（跳过不是留个空文件）')
    for k in ('btc:swap', 'btc:spot', 'eth:swap', 'xrp:swap'):
        v = svc._streams_state[k]
        ck(v['last_tail_appended'] == limit_for_exchange('okx', '1m')
           and v['last_backfilled'] == 0,
           f'{k} 空台账起头：先取最近一整窗（新币没历史也得有近期数据）')
        ck(not svc._backfill_state.get(k, {}).get('earliest_ms'),
           f'{k} 定时轮不推历史（schedule_backfill=false，百万根交给跑批）')

    # ---- 按币种看进度（页面与 CLI 同一口径）
    st15 = svc.get_status(cfg15)
    c15 = {c['coin']: c for c in st15['coins']}
    counts = {k: st15[k] for k in ('coins_total', 'streams_total', 'streams_skipped',
                                   'coins_available', 'coins_missing', 'coins_unchecked')}
    ck(counts == {'coins_total': 4, 'streams_total': 8, 'streams_skipped': 4,
                  'coins_available': 3, 'coins_missing': 1, 'coins_unchecked': 0},
       f'币种/流两级计数：{counts}')
    ck(c15['BTC']['rows_total'] == 600 and c15['ETH']['rows_total'] == 300
       and c15['XRP']['rows_total'] == 300 and c15['DOGE']['rows_total'] == 0,
       f'按币种汇总台账（BTC {c15["BTC"]["rows_total"]} 根）')
    ck(c15['DOGE']['available'] is False
       and svc.NO_SOURCE_MARK in (c15['DOGE']['market_note'] or ''),
       '无数据源的币仍在清单里（页面标灰 + 写明原因，而不是默默消失）')
    ck(c15['ETH']['available'] is True, '只要有一条流能拉就算这个币可用')
    ck([s['key'] for s in st15['streams'] if s['available'] is False]
       == ['eth:spot', 'xrp:spot', 'doge:swap', 'doge:spot'],
       '被跳过的流逐条带原因留在状态里')
    cat15 = svc.coin_catalog(cfg15)
    ck(len(cat15['streams']) == 8 and cat15['watch_coins'] == WATCH
       and cat15['markets_checked'] and cat15['stream_types'][0]['suffix'] == 'swap'
       and all(s['filename'] for s in cat15['streams'])
       and {s['key'] for s in cat15['streams'] if s['enabled']} == set(keys15),
       '币种目录（/api/coins）字段齐，且没为填页面去调 load_markets')

    # ---- 无数据源的流不能拿百万根去“猜”，点名回填也先拒绝
    res = svc.run_backfill('doge:spot', cfg=cfg15, exchange=fake15)
    ck(not res['ok'] and res['halt'] == 'no_market' and res['appended'] == 0
       and res['coin'] == 'DOGE' and svc.NO_SOURCE_MARK in res['error'],
       f'点名回填无数据源的流：直接拒绝并写明原因（{res["error"]}）')
    ck(not os.path.exists(svc.csv_path(d15['doge:spot'], tmp15)), '拒绝后仍不建文件')
    res = svc.run_backfill('eth:swap', budget_sec=60, until_rows=900, cfg=cfg15,
                           exchange=fake15, sleep_fn=NO_SLEEP)
    ck(res['ok'] and res['appended'] == 900 and res['halt'] == 'until_rows'
       and res['coin'] == 'ETH' and res['stream_type'] == 'swap',
       f'按币种回填（eth:swap 新增 {res["appended"]} 根）')
    ck(res['markets']['checked'] == 8 and res['markets']['available'] == 4,
       f'跑批前也先过一遍市表：{res["markets"]}')
    ck('eth:swap' not in svc._backfill_active, '跑批结束从「正在驱动」集合里摘掉')
    eth15 = {c['coin']: c for c in svc.get_status(cfg15)['coins']}['ETH']
    ck(eth15['rows_total'] == 1200 and 0 < eth15['backfill_pct'] < 100
       and eth15['backfill_remaining_rows'] > 0,
       f'按币种看进度：ETH 台账 {eth15["rows_total"]} 根 / {eth15["backfill_pct"]}%')

    # ---- 启动/叫停都按币种（页面只回填当前看的币）
    worker_real = svc._backfill_worker
    captured = {}

    def spy_worker(keys, budget_sec):
        captured['keys'] = sorted(keys)
        captured['budget'] = budget_sec
        svc._backfill_thread = None

    svc._backfill_worker = spy_worker
    try:
        b1 = svc.start_full_backfill(coins='BTC,ETH', budget_sec=30)
        time.sleep(0.2)
        ck(b1['started']
           and captured['keys'] == ['btc:spot', 'btc:swap', 'eth:spot', 'eth:swap']
           and captured['budget'] == 30 and b1['coins'] == ['BTC', 'ETH']
           and not b1['skipped'], f'按币种启动回填：{captured}')
        captured.clear()
        b2 = svc.start_full_backfill()
        time.sleep(0.2)
        ck(captured['keys'] == ['btc:spot', 'btc:swap', 'eth:swap', 'xrp:swap']
           and len(b2['skipped']) == 4
           and all(str(s['reason']).startswith(svc.NO_SOURCE_MARK) for s in b2['skipped']),
           '缺省只排可用流，无数据源的写进 skipped（不报错也不跑）')
        captured.clear()
        b3 = svc.start_full_backfill(['eth:spot'])
        time.sleep(0.2)
        ck(b3['streams'] == ['eth:spot'] and len(b3['skipped']) == 1,
           '用户点名要回填无数据源的流：照排队但在回报里写明会被跳过')
        sp = svc.stop_full_backfill(coins='ETH')
        ck(sorted(sp['stopping']) == ['eth:spot', 'eth:swap']
           and svc._backfill_stop.get('eth:swap') and not svc._backfill_stop.get('btc:swap'),
           f'按币种叫停不影响其他币：{sp["stopping"]}')
    finally:
        svc._backfill_worker = worker_real
        for k in keys15:
            svc._backfill_stop.pop(k, None)

    # ---- 监控台加了新币：注册表自动多两条流，旧币的校验结论不能被重建冲掉
    WATCH.append('SOL')
    bars15.update({'SOL/USDT:USDT': base_bars(), 'SOL/USDT': base_bars()})
    clear_markets_cache()
    mk = svc.resolve_markets(FakeOkx({}, markets={**MKT, **mk_market('SOL', 'swap'),
                                                  **mk_market('SOL', 'spot')}), cfg=cfg15)
    ck(mk['checked'] == 10 and mk['available'] == 6
       and svc.get_stream('btc:swap', cfg15)['available'] is True
       and svc.get_stream('doge:swap', cfg15)['available'] is False,
       '清单变长会重建注册表，但已有币的 ccxt 结论跟着走（不重查也不清空）')
    r = svc.run_kline_live_job(cfg=cfg15, exchange=fake15, sleep_fn=NO_SLEEP)
    ck(r == 'success' and svc._streams_state['sol:swap']['last_tail_appended'] == 300,
       f'新币进列下一轮自动起头（本轮 {r}）')
    ck(len(svc.get_status(cfg15)['coins']) == 5, '币种总览跟着多一行')

    # ---- 市表拿不到≠无数据源：结论不能被动，未校验的新币也不能盲跑
    clear_markets_cache()
    ex_bad = FakeOkx({}, markets={}, markets_error='HTTP 502 bad gateway')
    mk = svc.resolve_markets(ex_bad, cfg=cfg15)
    ck(mk['checked'] == 0 and mk['error'] and ex_bad.load_calls == 1
       and all(d['available'] is not None for d in svc.all_streams(cfg15)),
       f'markets 为空才 load_markets；失败只记错误不抛异常且沿用旧结论：{mk["error"]}')
    mk = svc.resolve_markets(FakeOkx({}, markets={}), cfg=cfg15)
    ck(mk['error'] and mk['checked'] == 0
       and svc.get_stream('btc:swap', cfg15)['available'] is True,
       '拿到空市表也不把全部币刷成「未校验」')
    clear_markets_cache()
    ex_ttl = FakeOkx({}, markets={}, loaded=MKT)      # ccxt 实例 load 前 markets 就是空
    svc.resolve_markets(ex_ttl, cfg=cfg15)
    svc.resolve_markets(ex_ttl, cfg=cfg15)
    ck(ex_ttl.load_calls == 1, '市表走 TTL 缓存（6 小时内两轮只 load 一次）')
    WATCH.append('LTC')                                # 新币进列，available 还是 None
    clear_markets_cache()
    res = svc.run_backfill('ltc:swap', cfg=cfg15,
                           exchange=FakeOkx({}, markets={}, markets_error='http 502'))
    ck(not res['ok'] and res['halt'] == 'markets_unavailable' and res['appended'] == 0,
       f'市表不可用又确认不了交易对：百万根跑批拒绝盲跑（{res["error"]}）')

    # ---- 旧版快照（裸 swap/spot 键）升级后仍要能认：丢游标 = 回填白跑
    with open(svc._snapshot_path(), 'w', encoding='utf-8') as f:
        json.dump({'rounds': 7, 'backfill': {
            'swap': {'earliest_ms': FLOOR, 'mode': 'earliest',
                     'head_cursor': FLOOR + 50 * STEP, 'rows': 50},
            'garbage/x': {'earliest_ms': FLOOR}},
            'streams': {'spot': {'last_run_at': '2026-01-01 00:00:00'}}}, f)
    reset_state('btc:swap', 'btc:spot')
    svc._load_snapshot()
    ck(svc._backfill_state['btc:swap']['head_cursor'] == FLOOR + 50 * STEP
       and svc._streams_state['btc:spot']['last_run_at'] == '2026-01-01 00:00:00',
       '旧 key 快照迁到 btc:swap / btc:spot（升级不丢断点）')
    ck('garbage' not in str(svc._backfill_state), '非法 stream key 不带病启动')

    # 收尾：清单与市表缓存恢复成只有 BTC，监控台删币 = 下一轮不再拉
    WATCH[:] = ['BTC']
    clear_markets_cache()
    reset_state()
    ck([d['key'] for d in svc.all_streams(cfg15)] == ['btc:swap', 'btc:spot'],
       '币种减少时注册表同步收窄')

    print('=' * 52)
    print('KLINE_LIVE SMOKE ALL PASS')


if __name__ == '__main__':
    run_smoke()
