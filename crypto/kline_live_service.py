#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监控台币种 1 分钟 K 线实时拉取（kline_live_service）
==================================================
币种清单跟着监控台走（固定池按市值降序 + 浮动池），每个币种两条独立数据流，
持续增量落盘 CSV，并把「拉取进度 + 最新一根 K 线」以内存状态暴露给 /kline-live 页面：

    {coin}:swap  BTC/USDT:USDT   （OKX 线性永续，与该币实盘 XXX-USDT-SWAP 同口径）
    {coin}:spot  BTC/USDT        （OKX 现货，用于对照基差）

「开拉前先在 ccxt 的 okx 市场表里查这个交易对」：spot 与 swap 是两条不同的 market
（id 分别是 BTC-USDT / BTC-USDT-SWAP，type 分别是 spot / swap），查不到、类型不符或
已下架的都标成「ccxt 无此数据源」并跳过，不报错、不建文件、不进抓取队列（有些小币
只有永续没有现货，这是常态而不是异常）。

底层全部复用 crypto/kline_fetcher 工具链（ccxt → okx 公共行情，无需密钥）：
    csv_store.write_header_if_needed / tail_last_timestamp / append_rows
    csv_store.csv_stats / merge_ordered_csv / scan_continuity
    exchange_fetcher.get_exchange / limit_for_exchange

与 task/kline_fixed_fetch.py 的分工：那个任务是「监控台全币种 · 每 10 分钟 · 每币
只跑永续」的批量增量，落 *_since_2022.csv 供回测策略硬编码引用；本模块是「每币永续
＋现货 · 每 1 分钟」的可视化通道，CSV 落在独立子目录 live/ 并按币种分文件
（{coin}_ohlcv_live_1m_{swap|spot}.csv），两者物理隔离（同一文件被两个写入者按不同
 symbol 追加会造成现货/永续价格串列）。

进度口径（重要，避免页面骗人）：
    - 「目标」不是固定天数，而是该 symbol 在 OKX 上的**数据地板 → 现在**。
      地板用二分探测得到（实测 BTC/USDT 现货 = 2018-01-11 11:12 UTC，
      BTC/USDT:USDT 永续 = 2019-12-16 06:09 UTC，再早返回空数组），探测结果
      落快照，一次探测终身使用。backfill_mode='days' 才退回「最近 N 天」口径。
    - 「已收盘 K 线」：游标只推进到 now - step，因此落盘的最后一根必然是已收盘
      整根，页面标注「最新已收盘 1m」；正在走的那一分钟用 fetch_ticker 的最新价
      单独展示，两者不混为一谈。
    - 台账行数直接从 CSV 数出来（按 size+mtime 记忆化，只追加时用平均行长估算，
      不对几百 MB 文件反复整读）；回填分段用计数器累加，不整读。
    - 每轮运行历史（成功/失败/新增根数/耗时）保存在内存 + data/ 下的轻量快照
      文件，仅作展示用途（非业务数据，不进 MySQL，不给实盘链路增加写压力）。

百万行级回填的两条硬约束（为什么不是简单改个天数）：
    1. 不能用 append_rows_dedup 的 seen 集合（450 万时间戳 ≈ 几百 MB），也不能用
       sort_and_dedup_csv（全文件读成 dict ≈ 数 GB）——回填靠「游标只向前」保证不
       重复，早于主文件首根的那一段先写独立分段文件，完成后流式归并回主文件。
    2. 不能每轮 load_existing_timestamps + detect_gaps 全量扫（几百 MB 文件上每秒
       都做不到）——回填是连续推进，天然无洞，因此不需要每轮缺口扫描。

配置（kv_store key='kline_live_config'，DB 不可用时退回环境变量/默认值；
interval_seconds 改动需重注册任务才反映到调度周期，register 时读一次）：
    {
      "enabled": true,               # 模块软开关（关=不注册任务）
      "timeframe": "1m",
      "interval_seconds": 60,        # 调度周期，钳位 20~600
      "backfill_mode": "earliest",   # earliest=从交易所地板起全量 | days=只近 N 天
      "since_days": 7,               # 仅 days 模式生效（历史兼容项）
      "round_budget_sec": 40,        # 单轮回填时间预算（两流共享，防一轮跨过一个调度周期）
      "max_candles_per_stream": 0,   # 单流单轮根数上限（0=只受时间预算约束）
      "request_interval_ms": 150,    # 回填批间隔（实测 150ms 稳定无 429，地板以下返回空）
      "data_dir": "",                # 空=kline_fetcher/data/live
      "streams": ["swap", "spot"]    # 启用的数据流
    }
环境变量兜底：CRYPTO_KLINE_LIVE_INTERVAL_SEC / CRYPTO_KLINE_LIVE_SINCE_DAYS /
CRYPTO_KLINE_LIVE_MODE（earliest|days）。
"""

import contextlib
import datetime
import io
import json
import os
import re
import sys
import threading
import time

# 双模式导入：Flask 包内（crypto.*）/ 独立脚本（crypto 目录在 sys.path）
try:
    from .kline_fetcher.csv_store import (
        append_rows,
        csv_stats,
        merge_ordered_csv,
        read_tail_rows as _kf_read_tail,
        scan_continuity,
        tail_last_timestamp,
        write_header_if_needed,
    )
    from .kline_fetcher.exchange_fetcher import (
        get_exchange,
        limit_for_exchange,
    )
    from .kline_fetcher.fetcher import DATA_DIR as _KF_DATA_DIR
except ImportError:  # pragma: no cover
    _CRYPTO_DIR = os.path.dirname(os.path.abspath(__file__))
    if _CRYPTO_DIR not in sys.path:
        sys.path.insert(0, _CRYPTO_DIR)
    from kline_fetcher.csv_store import (
        append_rows,
        csv_stats,
        merge_ordered_csv,
        read_tail_rows as _kf_read_tail,
        scan_continuity,
        tail_last_timestamp,
        write_header_if_needed,
    )
    from kline_fetcher.exchange_fetcher import (
        get_exchange,
        limit_for_exchange,
    )
    from kline_fetcher.fetcher import DATA_DIR as _KF_DATA_DIR

try:
    from .task.utils.logger import get_task_logger, generate_run_id
except ImportError:  # pragma: no cover
    _TASK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'task')
    if _TASK_DIR not in sys.path:
        sys.path.insert(0, _TASK_DIR)
    from utils.logger import get_task_logger, generate_run_id

task_log = get_task_logger()

JOB_ID = 'kline_live_btc'      # 沿用旧 job_id：调度器/任务开关/已注册条目的键，改名会丢开关状态
KV_CONFIG_KEY = 'kline_live_config'
KV_STATE_KEY = 'kline_live_state'
EXCHANGE_ID = 'okx'
SNAPSHOT_FILE = 'kline_live_snapshot.json'

# 东八区展示时区（中国大陆无夏令时，固定偏移即可，避免 Windows 缺 tzdata）
_TZ_LOCAL = datetime.timezone(datetime.timedelta(hours=8), 'CST')

DEFAULT_CONFIG = {
    'enabled': True,
    'timeframe': '1m',
    'interval_seconds': 60,
    'backfill_mode': 'earliest',     # earliest=从 OKX 数据地板起全量拉 | days=只最近 N 天
    'since_days': 7,                 # 仅 days 模式生效（保留兼容）
    'round_budget_sec': 40,          # 单轮回填时间预算（两流共享）
    'max_candles_per_stream': 0,     # 单流单轮根数上限，0=只受时间预算约束
    'request_interval_ms': 150,      # 回填批间隔，防 429
    'data_dir': '',
    'streams': ['swap', 'spot'],     # 数据流类型（每个启用币种各开这两条）
    'coins': [],                     # 币种白名单（BTC / BTC-USDT-SWAP 均可）；空=监控台全部币种
    'schedule_backfill': False,      # 定时轮是否顺带推历史回填（百万根会挤死同轮其他币的增量）
}

BACKFILL_MODES = ('earliest', 'days')

# 正在被连续跑批（run_backfill）驱动的流程：定时轮遇到它们时照样推游标，
# 其余流在 schedule_backfill=false 下只做尾段增量（新币靠起头窗口拿近期数据）
_backfill_active = set()

# 地板探测参数（实测：二分约 23 次请求 / 6 秒）
_FLOOR_LO_MS = 1_420_036_800_000       # 2015-01-01，比任何币都早的安全下界
_FLOOR_PROBE_WINDOW = 20               # 二分用 20 分钟窗口，容忍个别缺分钟
_FLOOR_MAX_AGE_MS = 86_400_000 * 30    # 探测结果保鲜期，过期后重探（交易所可能补数据）
_EMPTY_STREAK_STOP = 20                # 连续 20 批（6000 根）取不到 → 判定为真断档，本轮停

# 数据流类型模板：实际每条流 = 类型 × 币种，symbol/label/filename 按币种生成
STREAM_TYPES = {
    'swap': {'stream_type': 'swap', 'want_type': 'swap', 'suffix': 'swap',
             'type_label': '永续合约', 'sub_tpl': 'OKX {base}-USDT-SWAP 同口径 · 量纲为合约张数'},
    'spot': {'stream_type': 'spot', 'want_type': 'spot', 'suffix': 'spot',
             'type_label': '现货', 'sub_tpl': 'OKX {base}-USDT 现货 · 量纲为币本位'},
}

# 动态注册表：stream key（如 btc:swap）→ 流定义 dict。每次按「币种清单 + 启用类型」
# 指纹重建；保留 dict 形态是为了让页面/CLI/接口沿用原来的取法（label/symbol/sub）。
STREAM_DEFS = {}
_registry_fp = None

# ccxt 市场表缓存（load_markets 约 2 秒且每轮都调没必要；下架/新上币靠 TTL 过期后重查）
_MARKETS_TTL_SEC = 6 * 3600
_markets_cache = {'at': 0.0, 'markets': {}, 'error': None}

# 旧版只有 BTC 两条流，key 就是裸的 swap/spot；快照里遗留的旧 key 迁到 btc:xxx，
# 免得升级后游标/地板/统计对不上号（对不上就会拿旧游标去重请己填过的一段）
_LEGACY_KEY_MAP = {'swap': 'btc:swap', 'spot': 'btc:spot'}

NO_SOURCE_MARK = 'ccxt 无此数据源'

# stream key 形如 btc:swap；币种 base 只允许字母数字（快照/接口传进来的 key 先过这关）
_BASE_RE = re.compile(r'^[a-z0-9]{2,20}$')

# ---------------------------------------------------------------- 运行期状态
_state_lock = threading.RLock()
_run_lock = threading.Lock()
# 跑批等锁上限：定时轮每轮约占住 40s，非阻塞抢锁经常正好撞在它写台账的时候
# （页面点一下「全量回填」就报「稍后重试」）；跑批是长任务，值得排队等一轮交还
_RUN_LOCK_WAIT_SEC = 90
_events = []                       # 最近若干条运行流水（供页面时间线）
_EVENT_LIMIT = 60
_streams_state = {}                # key → 状态 dict
_rounds = 0
_last_round_at = None
_running = False
_started_at = time.time()
_next_run_at = None                # 'YYYY-MM-DD HH:MM:SS'（注册/每轮刷新）
_interval_seconds = DEFAULT_CONFIG['interval_seconds']
_exchange_local = threading.local()


# =============================================================================
# 小工具
# =============================================================================

def _num(value, default, lo=None, hi=None):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default
    if hi is not None and n > hi:
        return default
    return n


def _utc_iso(ms):
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime(
        '%Y-%m-%d %H:%M:%S')


def _local_iso(ms):
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, _TZ_LOCAL).strftime(
        '%Y-%m-%d %H:%M:%S')


def _now_text():
    return datetime.datetime.now(_TZ_LOCAL).strftime('%Y-%m-%d %H:%M:%S')


def _now_text_from_ts(ts) -> str:
    """time.time() 秒值 → 东八区文本（市表缓存时间给页面看用）。"""
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(float(ts), _TZ_LOCAL).strftime('%Y-%m-%d %H:%M:%S')


def live_data_dir() -> str:
    """CSV 落盘目录：env CRYPTO_KLINE_LIVE_DATA_DIR > kv data_dir > kline_fetcher/data/live。

    env 是给「独立跑批与实盘进程物理隔离」用的：同一份 live/ CSV 只允许一个写入者，
    用 --backfill 另起进程跑历史时把它指到单独目录，避免和 7777 上正在写的同名文件互踩。
    """
    env_dir = str(os.environ.get('CRYPTO_KLINE_LIVE_DATA_DIR', '')).strip()
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir
    cfg = load_config()
    if cfg['data_dir']:
        os.makedirs(cfg['data_dir'], exist_ok=True)
        return cfg['data_dir']
    d = os.path.join(_KF_DATA_DIR, 'live')
    os.makedirs(d, exist_ok=True)
    return d


def csv_path(stream: dict, data_dir: str = None) -> str:
    return os.path.join(data_dir or live_data_dir(), stream['filename'])


# =============================================================================
# 币种清单与流注册表（监控台币种 × 数据流类型）
# =============================================================================

DEFAULT_COINS = ('BTC-USDT-SWAP',)     # 监控台完全面不可读时的保底清单


def _base_of(inst_id) -> str:
    """inst_id / ccxt symbol / 裸币种名 → 大写 base。

    BTC-USDT-SWAP / BTC-USDT / BTC/USDT:USDT / btc 统一得 BTC（前段就是 base，
    后面都是计价币/合约类，不能拿它当币种名）。
    """
    s = str(inst_id or '').strip().upper()
    if not s:
        return ''
    return s.split('/')[0].split('-')[0].split(':')[0].strip()


def _coin_list_from_file() -> list:
    """直接读 crypto/config.json 的币种清单（DB/服务不可用时的兜底）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        task_log.warning(f'[K线实时] 读本地 config.json 币种清单失败: {e}')
        return []
    if not isinstance(data, dict):
        return []
    fixed = data.get('all_coins') or []
    floating = data.get('floating_coins') or []
    if not isinstance(fixed, list):
        fixed = []
    if not isinstance(floating, list):
        floating = []
    return [str(c or '') for c in list(fixed) + list(floating)]


def _clean_coin_list(raw) -> list:
    """币种白名单清洗：数组与 'BTC,ETH' 写法都收，逐项归一成大写 base 并保序去重。"""
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [b for b in dict.fromkeys(_base_of(x) for x in raw) if b]


def _clean_stream_types(raw) -> list:
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [t for t in dict.fromkeys(str(x).strip().lower() for x in raw)
            if t in STREAM_TYPES]


def watch_coins() -> list:
    """监控台币种 base 全集（不看 kv coins 白名单）：固定池按市值降序 + 浮动池在后。

    读不到监控台配置（DB 挂了、独立入口没起全套服务）就退回直接读 config.json，
    再退回只剩 BTC：清单为空不该让整条抓取链报错停摆。页面「币种范围」需要拿这个
    全集当候选，否则一旦收窄到几个币就再也看不上其他币了。
    """
    inst_ids = []
    try:
        try:
            from .real_strategy_adapter import get_all_coins
        except ImportError:  # pragma: no cover
            from real_strategy_adapter import get_all_coins
        inst_ids = [str(c or '') for c in (get_all_coins() or [])]
    except Exception as e:
        task_log.warning(f'[K线实时] 读监控台币种列表失败，退回本地 config.json: {e}')
    bases = [b for b in dict.fromkeys(_base_of(x) for x in inst_ids) if b]
    if not bases:
        bases = [b for b in dict.fromkeys(_base_of(x) for x in _coin_list_from_file()) if b]
    return bases or list(DEFAULT_COINS)


# 未设白名单时，默认只拉监控台固定币种里市值最大的前 N 个（watch_coins 已按市值降序）。
# 想拉更多/更少就在页面「币种范围」勾选（写进 coins 白名单即覆盖本默认）。
DEFAULT_MONITOR_TOP_COINS = 12


def monitor_coins(cfg: dict = None) -> list:
    """本轮要拉取的全部币种 base：kv 配置 coins 非空时以它为准（收窄范围）。

    写 BTC 或 BTC-USDT-SWAP 都认（归一成大写 base）；空清单 = 跟随监控台固定币种按
    市值降序的前 DEFAULT_MONITOR_TOP_COINS 个（watch_coins 本身就是「固定池按市值降序
    + 浮动池在后」，取前 12 恰好就是市值最大的 12 个固定币，不含小市值浮动币）。
    """
    cfg = cfg or load_config()
    picked = _clean_coin_list(cfg.get('coins'))
    if picked:
        return picked
    return watch_coins()[:DEFAULT_MONITOR_TOP_COINS]


def _stream_def(base: str, type_key: str, timeframe: str) -> dict:
    """一条流 = 币种 × 类型；文件名按币种区分，symbol 与 market id 成对生成。"""
    tpl = STREAM_TYPES[type_key]
    swap = type_key == 'swap'
    return {
        'key': f'{base.lower()}:{type_key}',
        'coin': base,
        'inst_id': f'{base}-USDT-SWAP' if swap else f'{base}-USDT',
        'stream_type': type_key,
        'symbol': f'{base}/USDT:USDT' if swap else f'{base}/USDT',
        'market_id': f'{base}-USDT-SWAP' if swap else f'{base}-USDT',
        'want_type': tpl['want_type'],
        'label': f'{base} {tpl["type_label"]}',
        'sub': tpl['sub_tpl'].format(base=base),
        'filename': f'{base.lower()}_ohlcv_live_{timeframe}_{tpl["suffix"]}.csv',
        # available=None 表示「还没查过 ccxt 市表」，不能当成「无数据源」而默默丢弃
        'available': None,
        'market_note': None,
        'market_checked_at': None,
    }


def ensure_registry(cfg: dict = None) -> dict:
    """按「币种清单 × 启用类型 × 周期」重建 STREAM_DEFS。

    指纹没变就不重建（重建会新建 dict，已得到的 ccxt 校验结论靠 prev 带过来）；
    监控台上加/减币或页面改了启用类型，下一轮自动反映到流清单上。
    """
    global _registry_fp, STREAM_DEFS
    cfg = cfg or load_config()
    bases = monitor_coins(cfg)
    tf = cfg['timeframe']
    types = [t for t in STREAM_TYPES if t in cfg['streams']] or list(STREAM_TYPES)
    fp = ('|'.join(bases), '|'.join(types), tf)
    if _registry_fp == fp and STREAM_DEFS:
        return STREAM_DEFS
    prev = STREAM_DEFS
    defs = {}
    for base in bases:
        for t in types:
            sd = _stream_def(base, t, tf)
            old = prev.get(sd['key'])
            if old:      # 市表校验结论不随重建丢失
                sd['available'] = old.get('available')
                sd['market_note'] = old.get('market_note')
                sd['market_checked_at'] = old.get('market_checked_at')
            defs[sd['key']] = sd
    STREAM_DEFS = defs
    _registry_fp = fp
    return defs


# =============================================================================
# ccxt 市场校验：这个币在 okx 上到底有没有（现货/永续是两个不同 market）
# =============================================================================

def _markets_map(exchange, force: bool = False) -> dict:
    """ccxt okx 全市场表（symbol → market），带 TTL 缓存。"""
    now = time.time()
    if (not force and _markets_cache['markets']
            and (now - float(_markets_cache['at'] or 0)) < _MARKETS_TTL_SEC):
        return _markets_cache['markets']
    markets = getattr(exchange, 'markets', None)
    if not markets:
        markets = exchange.load_markets()
    markets = markets or {}
    _markets_cache.update({'at': now, 'markets': markets, 'error': None})
    return markets


def _lookup_market(markets: dict, stream: dict):
    """按 symbol 命中，命不中再按 market id 直查。

    先 symbol 后 id：现货/永续在 ccxt 里 symbol 不同但 base 部相同，拿 base 去模糊
    匹会错把永续当现货；id 兑底是为了 ccxt 个别结算币拼法差异时不误报「无数据源」。
    """
    mkt = markets.get(stream['symbol'])
    if isinstance(mkt, dict):
        return mkt
    want_id = str(stream['market_id']).upper()
    for m in markets.values():
        if isinstance(m, dict) and str(m.get('id') or '').upper() == want_id:
            return m
    return None


def check_market(stream: dict, markets: dict):
    """(available, note)：查不到/类型不符/已下架都归为「ccxt 无此数据源」。"""
    if not markets:
        return None, 'ccxt 市场表未加载'
    mkt = _lookup_market(markets, stream)
    if mkt is None:
        return False, f'{NO_SOURCE_MARK}（okx 查不到 {stream["market_id"]}）'
    mtype = str(mkt.get('type') or '').lower()
    if mtype != stream['want_type']:
        return False, (f'{NO_SOURCE_MARK}（{stream["market_id"]} 实际市场类型是 '
                       f'{mtype or "未知"}，不是 {stream["want_type"]}）')
    if mkt.get('active') is False:
        return False, f'{NO_SOURCE_MARK}（{stream["market_id"]} 已下架）'
    return True, None


def resolve_markets(exchange, force: bool = False, cfg: dict = None) -> dict:
    """开拉前校验：把每条流的存在性写进注册表。返回计数汇总（不抛异常）。

    市表加载失败时保留上一次结论（宁可拿着旧结论跑，也不把全部币误标成无数据源
    而默默停掉整条链）。
    """
    defs = ensure_registry(cfg)
    out = {'checked': 0, 'available': 0, 'missing': 0, 'unknown': 0, 'error': None,
           'reused': bool(_markets_cache['markets']
                          and not force
                          and (time.time() - float(_markets_cache['at'] or 0))
                          < _MARKETS_TTL_SEC)}
    try:
        markets = _markets_map(exchange, force=force)
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {str(e)[:160]}'
        task_log.warning(f'[K线实时] ccxt 市场表加载失败，本轮沿用上次校验结论: {out["error"]}')
        return out
    if not markets:
        # 拿到空市表也不能把全部币改成「未校验」：旧结论总比清空后页面上一片「无数据源」强
        out['error'] = 'ccxt 市场表为空，无法校验'
        task_log.warning('[K线实时] ccxt 市场表为空，本轮沿用上次校验结论')
        return out
    at = _now_text()
    for sd in defs.values():
        ok, note = check_market(sd, markets)
        sd['available'] = ok
        sd['market_note'] = note
        sd['market_checked_at'] = at
        out['checked'] += 1
        if ok is True:
            out['available'] += 1
        elif ok is False:
            out['missing'] += 1
        else:
            out['unknown'] += 1
    missing = [sd for sd in defs.values() if sd.get('available') is False]
    if missing:
        task_log.info(f'[K线实时] ccxt 校验：可用 {out["available"]} 条、'
                      f'无数据源 {out["missing"]} 条（已跳过：'
                      + '、'.join(f'{s["inst_id"]}' for s in missing[:12])
                      + ('…' if len(missing) > 12 else '') + '）')
    return out


def get_stream(key: str, cfg: dict = None) -> dict:
    """按 key 取流定义（未知 key 返回 None，让接口层能报 400 而不是 500）。

    旧的裸 key（swap / spot）仍然认识，迁到 btc:swap / btc:spot：页面、CLI、接口
    和快照都可能带着旧写法，不能因为改了 key 就集体报「未知数据流」。
    """
    k = str(key or '').strip().lower()
    return ensure_registry(cfg).get(_LEGACY_KEY_MAP.get(k, k))


def all_streams(cfg=None) -> list:
    """清单里的全部流（含 ccxt 无数据源的）：页面据此把币标灰而不是默默消失。"""
    return [dict(v) for v in ensure_registry(cfg).values()]


def unavailable_streams(cfg=None) -> list:
    return [s for s in all_streams(cfg) if s.get('available') is False]



# =============================================================================
# 配置读取（defaults < env < kv_store；任何坏值都退回默认，绝不炸任务）
# =============================================================================

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    env_interval = str(os.environ.get('CRYPTO_KLINE_LIVE_INTERVAL_SEC', '')).strip()
    if env_interval:
        cfg['interval_seconds'] = _num(env_interval, cfg['interval_seconds'], 20, 600)
    env_since = str(os.environ.get('CRYPTO_KLINE_LIVE_SINCE_DAYS', '')).strip()
    if env_since:
        cfg['since_days'] = _num(env_since, cfg['since_days'], 1, 36500)
    env_mode = str(os.environ.get('CRYPTO_KLINE_LIVE_MODE', '')).strip().lower()
    if env_mode in BACKFILL_MODES:
        cfg['backfill_mode'] = env_mode
    try:
        try:
            from . import config_store_repo as cs
        except ImportError:  # pragma: no cover
            import config_store_repo as cs
        stored = cs.load_json_config_cached(KV_CONFIG_KEY)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
    except Exception as e:
        task_log.warning(f'[K线实时] 读取 kv 配置失败，按环境/默认值执行: {e}')
    cfg['enabled'] = bool(cfg.get('enabled', True))
    cfg['timeframe'] = str(cfg.get('timeframe') or '1m').strip() or '1m'
    cfg['interval_seconds'] = int(_num(cfg.get('interval_seconds'), 60, 20, 600))
    mode = str(cfg.get('backfill_mode') or '').strip().lower()
    cfg['backfill_mode'] = mode if mode in BACKFILL_MODES else DEFAULT_CONFIG['backfill_mode']
    cfg['since_days'] = int(_num(cfg.get('since_days'), DEFAULT_CONFIG['since_days'], 1, 36500))
    cfg['round_budget_sec'] = int(_num(cfg.get('round_budget_sec'),
                                       DEFAULT_CONFIG['round_budget_sec'], 1, 3000))
    cfg['max_candles_per_stream'] = int(
        _num(cfg.get('max_candles_per_stream'), DEFAULT_CONFIG['max_candles_per_stream'],
             0, 20_000_000))
    cfg['request_interval_ms'] = int(_num(cfg.get('request_interval_ms'),
                                          DEFAULT_CONFIG['request_interval_ms'], 0, 5000))
    cfg['data_dir'] = str(cfg.get('data_dir') or '').strip()
    cfg['streams'] = _clean_stream_types(cfg.get('streams')) or list(DEFAULT_CONFIG['streams'])
    cfg['coins'] = _clean_coin_list(cfg.get('coins'))
    return cfg


def active_streams(cfg=None) -> list:
    """本轮真正要抓的流：类型启用 且 未被 ccxt 校验判定为无数据源。

    available 为 None（还没查过市表）的先当可用对待 —— 任务轮开头会先校验再抓，
    真正被过滤掉的是确实没有那个市场的币。
    """
    return [s for s in all_streams(cfg) if s.get('available') is not False]


def update_live_config(patch: dict) -> dict:
    """合并写入 kv 配置并重注册任务（周期/软开关即时生效）。"""
    try:
        from .database import session_scope
        from . import config_store_repo as cs
    except ImportError:  # pragma: no cover
        _CRYPTO_DIR = os.path.dirname(os.path.abspath(__file__))
        if _CRYPTO_DIR not in sys.path:
            sys.path.insert(0, _CRYPTO_DIR)
        from database import session_scope
        import config_store_repo as cs
    clean = {k: v for k, v in (patch or {}).items() if k in DEFAULT_CONFIG}
    if 'coins' in clean:
        clean['coins'] = _clean_coin_list(clean['coins'])
    if 'streams' in clean:
        clean['streams'] = _clean_stream_types(clean['streams'])
    with session_scope() as s:
        current = cs.load_json_config(s, KV_CONFIG_KEY) or {}
        current.update(clean)
        cs.save_json_config(s, KV_CONFIG_KEY, current)
    try:
        cs.invalidate_config_cache(KV_CONFIG_KEY)
    except Exception:
        pass
    register_kline_live_job()
    return load_config()


# =============================================================================
# CSV 台账读取（行数 / 首根，带 size+mtime 记忆化 + 只追加估算）
# =============================================================================

_ledger_cache = {}    # path → {sig, rows, first_ts, avg, est_sig, est_rows}

# 小于这个体积直接整读数行数（约 20 万根，几十毫秒级），大文件才走锚点估算
_LEDGER_EXACT_BYTES = 16 * 1024 * 1024


def _file_stat_sig(path):
    try:
        st = os.path.getsize(path), int(os.path.getmtime(path))
        return st
    except OSError:
        return None


def invalidate_ledger_cache(path: str = None):
    """归并/重写后必须失效，否则「只追加估算」会在行数跳变上算错。"""
    if path is None:
        _ledger_cache.clear()
    else:
        _ledger_cache.pop(path, None)


def rows_meta(path: str, allow_estimate: bool = True):
    """(台账行数, 首根ts)。

    全量历史文件会到几百 MB，不能每轮整读：整读一次得到「精确锚点 + 平均行长」，
    之后文件变大（追加）就用字节差估算，误差约 1%；估算结果只另存，绝不覆写锚点
    （覆写过一次，下一轮就拿着上一次的估算当基线，误差会逐轮复利）；文件变小（归并
    去重）或体积不到 16MB 时一律重新整读。跑批写入方还会用 note_ledger_append() 把
    锚点校准成精确值（追加了多少行写入方自己最清楚，不需要估）。
    """
    sig = _file_stat_sig(path)
    if sig is None:
        return 0, None
    c = _ledger_cache.get(path)
    if c:
        if c['sig'] == sig:
            return c['rows'], c['first_ts']
        if c.get('est_sig') == sig:
            return c['est_rows'], c['first_ts']
    if (not allow_estimate or sig[0] <= _LEDGER_EXACT_BYTES
            or not c or c['first_ts'] is None or c['avg'] <= 0
            or sig[0] < c['sig'][0]):
        rows, first_ts, avg = csv_stats(path)
        _ledger_cache[path] = {'sig': sig, 'rows': rows, 'first_ts': first_ts, 'avg': avg}
        return rows, first_ts
    est = int(c['rows']) + int((sig[0] - c['sig'][0]) / c['avg'])
    _ledger_cache[path] = dict(c, est_sig=sig, est_rows=est)
    return est, c['first_ts']


def note_ledger_append(path: str, appended: int):
    """写入方事后校准锚点：本轮真实追加行数，免掉大文件的字节差估算。"""
    if not appended or appended <= 0:
        return
    sig = _file_stat_sig(path)
    c = _ledger_cache.get(path)
    if sig is None:
        return
    if not c or c['first_ts'] is None or sig[0] <= c['sig'][0]:
        # 锚点缺失，或体积对不上（另有写入者/刚归并过）：作废，下次整读一次
        invalidate_ledger_cache(path)
        return
    grew = sig[0] - c['sig'][0]
    _ledger_cache[path] = {'sig': sig, 'rows': c['rows'] + int(appended),
                           'first_ts': c['first_ts'], 'avg': grew / float(appended)}


# =============================================================================
# 交易所实例（每线程一个：ccxt 同步版节流器非线程安全）
# =============================================================================

def _thread_exchange():
    ex = getattr(_exchange_local, 'exchange', None)
    if ex is None:
        ex = get_exchange(EXCHANGE_ID)
        _exchange_local.exchange = ex
    return ex


# =============================================================================
# 数据地板探测：该 symbol 在 OKX 上最早能取到哪一分钟
# =============================================================================

class FloorProbeError(RuntimeError):
    """地板探测中途抛错（区别于「地板以下返回空数组」）。"""


_probe_lock = threading.Lock()


def _fetch_window(exchange, symbol: str, timeframe: str, at_ms: int, limit: int) -> list:
    """取 [at_ms, at_ms + limit×step) 区间的整根，时间升序；无数据返回 []。

    直连 exchange.fetch_ohlcv 而不走 safe_fetch_ohlcv：后者把网络错误也返回成空
    列表，而回填场景下「空 = 交易所确实没这段数据」，两者混淆会凭空记一大批缺口。
    ccxt 的 okx.fetchOHLCV 在 since 超过 1440 根时会自己切 history-candles 端点。
    """
    rows = exchange.fetch_ohlcv(symbol, timeframe, since=at_ms, limit=limit) or []
    return [r for r in rows if r and r[0] is not None and int(r[0]) >= at_ms]


def probe_earliest_ms(exchange, symbol: str, timeframe: str, step_ms: int,
                      sleep_fn=time.sleep):
    """二分找最早可取的整根时间戳，返回 (earliest_ms, 请求次数)。

    可行因：OKX 的取数窗口是 [since, since+limit×step)，地板以下返回空数组，
    所以「窗口内是否有数据」是个单调谓词。实测约 23 次请求 / 6 秒。
    """
    lo = _FLOOR_LO_MS
    hi = exchange.milliseconds() - 3 * 86_400_000
    requests = 0
    nap = max(getattr(exchange, 'rateLimit', 110) / 1000.0, 0.05)
    while hi - lo > step_ms:
        mid = lo + ((hi - lo) // 2 // step_ms) * step_ms
        try:
            got = _fetch_window(exchange, symbol, timeframe, mid, _FLOOR_PROBE_WINDOW)
        except Exception as exc:
            raise FloorProbeError(f'{type(exc).__name__}: {str(exc)[:160]}')
        requests += 1
        if got:
            hi = mid
        else:
            lo = mid + step_ms
        sleep_fn(nap)
    # 收敛后回看半小时，用真实返回的首根时间戳当地板（比二分边界更精确）
    try:
        got = _fetch_window(exchange, symbol, timeframe,
                            max(lo - 30 * step_ms, _FLOOR_LO_MS), 300)
    except Exception as exc:
        raise FloorProbeError(f'{type(exc).__name__}: {str(exc)[:160]}')
    requests += 1
    return int(got[0][0] if got else hi), requests


# =============================================================================
# 回填游标状态（落快照，重启/分轮接着跑）
# =============================================================================

_backfill_state = {}     # stream key → 游标与统计
_backfill_stop = {}      # stream key → 人工停止标志
_backfill_thread = None  # 页面「全量回填」的后台连续跑批线程（同一时刻只允许一个）
_BF_PERSIST = ('mode', 'earliest_ms', 'probed_at_ms', 'probe_requests', 'floor_source',
               'head_cursor', 'stop_ms', 'target_file', 'writes_main', 'rows', 'gap_rows',
               'batches', 'done', 'started_at', 'finished_at', 'rate', 'note')


def _bf_state(key: str) -> dict:
    return _backfill_state.setdefault(key, {})


def backfill_file_of(path: str) -> str:
    """「早于主文件首根」那段回填数据的分段文件（自身时间递增，完成后归并）。"""
    return path + '.backfill.csv'


def _align_down(ms, step_ms) -> int:
    return int(ms) - (int(ms) % int(step_ms))


def _align_up(ms, step_ms) -> int:
    """向上对齐到周期边界（窗口起点本身不是整分钟）。"""
    return int(ms) + (-int(ms) % int(step_ms))


def _inner_gap_rows(rows: list, step_ms: int) -> int:
    """同一批内部相邻整根之间的空洞（交易所那几分钟确实没有）。

    不能只盯「窗口首根与游标的差」：一个 300 根的窗口里前后都有数、中间缺 40
    分钟，那种洞会静悄悄地漏记，进度条就永远算不到 100%。代价是每批 O(300) 的
    比较，相对网络往返可忽略。
    """
    miss = 0
    prev = None
    for r in rows:
        ts = int(r[0])
        if prev is not None and ts - prev > step_ms:
            miss += int((ts - prev) // step_ms) - 1
        prev = ts
    return miss


def _advance(exchange, stream: dict, cfg: dict, path: str, cursor: int, stop_ms: int,
             step_ms: int, deadline, max_rows, sleep_fn=time.sleep):
    """游标只向前地连续取数落盘，直到追平 stop_ms / 预算耗尽 / 连续取不到。

    返回 {appended, cursor, gap_rows, batches, halt, error}。不建 seen 集合（游标
    单调 ⇒ 天然不重复）；只写 timestamp < stop_ms 的行，所以尾段不会落正在走的
    那一分钟。
    """
    tf = cfg['timeframe']
    symbol = stream['symbol']
    limit = limit_for_exchange(EXCHANGE_ID, tf)
    nap = max(cfg['request_interval_ms'], 0) / 1000.0
    appended = gap_rows = empty_streak = batches = 0
    halt, error = 'done', None
    while cursor < stop_ms:
        if deadline is not None and time.time() >= deadline:
            halt = 'budget'
            break
        if max_rows and appended >= max_rows:
            halt = 'max_rows'
            break
        try:
            batch = _fetch_window(exchange, symbol, tf, cursor, limit)
        except Exception as exc:
            # 网络异常：游标原地不动，下轮同一位置重试（不当作缺口，不记缺口）
            halt = 'error'
            error = f'{type(exc).__name__}: {str(exc)[:200]}'
            break
        batches += 1
        if not batch:
            # 交易所这一段确实没有数据（1m 历史里的真空洞）：计入缺口并跳过
            gap_rows += min(limit, max(1, int((stop_ms - cursor) // step_ms)))
            cursor += limit * step_ms
            empty_streak += 1
            if empty_streak >= _EMPTY_STREAK_STOP:
                halt = 'empty_streak'
                break
            continue
        empty_streak = 0
        if batch[0][0] >= stop_ms:
            cursor = stop_ms
            break
        if batch[0][0] > cursor:
            gap_rows += int((batch[0][0] - cursor) // step_ms)
        batch = [r for r in batch if r[0] < stop_ms]
        if not batch:
            cursor = stop_ms
            break
        gap_rows += _inner_gap_rows(batch, step_ms)
        appended += append_rows(path, EXCHANGE_ID, batch)
        cursor = int(batch[-1][0]) + step_ms
        if nap:
            sleep_fn(nap)
    return {'appended': appended, 'cursor': cursor, 'gap_rows': gap_rows,
            'batches': batches, 'halt': halt, 'error': error}


# =============================================================================
# 单流拓取
# =============================================================================

def _prepare_backfill(stream: dict, cfg: dict, path: str, st: dict, exchange,
                      step_ms: int, sleep_fn=time.sleep) -> bool:
    """初始化回填游标（确定地板 / 写入目标 / 终止点）。返回是否需要回填。"""
    mode = cfg['backfill_mode']
    mode_key = mode if mode == 'earliest' else f'days:{cfg["since_days"]}'
    if st.get('mode') != mode_key:
        # 口径切换： leftovers 归并回主文件，游标重置（不默默丢数据）
        temp = st.get('target_file')
        if temp and temp != path and os.path.exists(temp) and os.path.getsize(temp) > 0:
            try:
                merge_ordered_csv(path, temp, remove_extra=True)
                invalidate_ledger_cache(path)
            except OSError as exc:
                task_log.warning(f'[K线实时] 口径切换归并失败: {exc}')
        st.clear()
        st['mode'] = mode_key

    now_ms = exchange.milliseconds()
    if mode == 'days':
        st.setdefault('earliest_ms', _align_up(now_ms - cfg['since_days'] * 86_400_000,
                                              step_ms))
        st['floor_source'] = 'days'
    else:
        cached = st.get('earliest_ms')
        fresh = (now_ms - int(st.get('probed_at_ms') or 0)) < _FLOOR_MAX_AGE_MS
        if not (cached and fresh):
            with _probe_lock:
                if not (st.get('earliest_ms')
                        and (exchange.milliseconds() - int(st.get('probed_at_ms') or 0))
                        < _FLOOR_MAX_AGE_MS):
                    earliest, reqs = probe_earliest_ms(
                        exchange, stream['symbol'], cfg['timeframe'], step_ms,
                        sleep_fn=sleep_fn)
                    st['earliest_ms'] = earliest
                    st['probed_at_ms'] = exchange.milliseconds()
                    st['probe_requests'] = reqs
                    st['floor_source'] = 'probe'

    if st.get('done'):
        return False
    if st.get('head_cursor') is None:
        rows, first_ts = rows_meta(path)
        start = int(st['earliest_ms'])
        stop = int(first_ts) if rows else _align_down(now_ms, step_ms)
        if rows and start >= stop:
            st['done'] = True
            st['note'] = '地板不早于现有首根，无需回填'
            return False
        st['writes_main'] = (rows == 0)
        st['target_file'] = path if rows == 0 else backfill_file_of(path)
        st['stop_ms'] = stop
        st['head_cursor'] = _align_down(start, step_ms)
        st['rows'] = 0
        st['gap_rows'] = 0
        st['done'] = False
        st['started_at'] = _now_text()
        write_header_if_needed(st['target_file'])
        # 游标丢了但分段文件还在（快照被清、stream key 改过、上一轮写到一半被杀）：
        # 必须按文件末根续，不然会把已经到手的那一段从头再 append 一遍（百万根量级
        # 这一层不跑去重，不重复全靠游标单调，游标不可信时只能拿台账当唯一事实）
        last_ts = tail_last_timestamp(st['target_file'])
        if last_ts is not None and int(last_ts) + step_ms > int(st['head_cursor']):
            st['head_cursor'] = int(last_ts) + step_ms
    elif st.get('target_file'):
        # 断点续跑（重启/被杀后）：分段文件可能被手工清理，补个表头再写，避免 append 炸开
        write_header_if_needed(st['target_file'])
        # 续跑起点以台账实际落盘为准：快照只在每轮末写，中途被杀会让游标落后于文件，
        # 照着旧游标接着请就会把已经写过的那段重复 append（这一层为了内存不跑去重）
        last_ts = tail_last_timestamp(st['target_file'])
        if last_ts is not None and int(last_ts) + step_ms > int(st['head_cursor']):
            st['head_cursor'] = int(last_ts) + step_ms
    return True


def _consume_backfill(stream: dict, cfg: dict, path: str, st: dict, exchange,
                      step_ms: int, deadline, sleep_fn=time.sleep, max_rows=None):
    """推一段回填游标并回写游标状态；追平终点则归并分段文件收尾。

    fetch_stream（定时轮）与 run_backfill（连续跑批）共用这一块，保证两者对
    游标/行数/缺口/速率的记账口径完全一致（不然页面上的 ETA 会因入口不同而不同）。
    max_rows 传入时覆盖配置里的单轮上限（run_backfill 靠它实现 --rows 语义）。
    """
    if _backfill_stop.get(stream['key']):
        return None
    cur = int(st['head_cursor'])
    t0 = time.time()
    got = _advance(exchange, stream, cfg, st['target_file'], cur, int(st['stop_ms']),
                   step_ms, deadline,
                   cfg['max_candles_per_stream'] if max_rows is None else max_rows,
                   sleep_fn)
    elapsed = max(time.time() - t0, 1e-6)
    if got['appended']:
        # 写入方知道确切行数，拿来校准台账锚点，大文件就不用靠字节差估了
        note_ledger_append(st['target_file'], got['appended'])
    st['rows'] = int(st.get('rows', 0)) + got['appended']
    st['gap_rows'] = int(st.get('gap_rows', 0)) + got['gap_rows']
    st['batches'] = int(st.get('batches', 0)) + got['batches']
    st['head_cursor'] = got['cursor']
    inst_rate = got['appended'] / elapsed
    # 预算被上一轮占光时 _advance 会一根没取就返回（elapsed 接近 0）：那种零根段不能
    # 进速率 EWMA，否则每轮会把后面那条流的实测速率提到 0，ETA 变成几十小时假数
    if got['appended'] > 0:
        st['rate'] = round(inst_rate if not st.get('rate')
                           else st['rate'] * 0.7 + inst_rate * 0.3, 1)
    # 追平终止点 → 归并分段文件，回填收尾
    if got['cursor'] >= int(st['stop_ms']):
        if not st['writes_main']:
            merged = merge_ordered_csv(path, st['target_file'], remove_extra=True)
            invalidate_ledger_cache(path)
            st['note'] = f'已归并回主文件（台账 {merged} 根）'
            st['rows'] = 0
        st['done'] = True
        st['finished_at'] = _now_text()
    return got


def fetch_stream(stream: dict, cfg: dict, exchange, sleep_fn=time.sleep,
                 deadline=None) -> dict:
    """一条数据流一轮的处置：先推回填游标（可调），再补尾段增量。

    返回 {tail_appended, backfilled, bootstrapped, gap_rows, halt, error, earliest_ms,
    head_cursor, backfill_done, start_ms, latest_ts, elapsed_sec, probe_requests,
    output}；异常不外抛（本模块一轮内要串行跑上百条流，一条失败不该拖垮整轮）。
    """
    tf = cfg['timeframe']
    path = csv_path(stream, cfg['data_dir'] or None)
    step_ms = _step_ms(exchange, tf)
    st = _bf_state(stream['key'])
    t0 = time.time()
    res = {'tail_appended': 0, 'backfilled': 0, 'gap_rows': 0, 'halt': None,
           'earliest_ms': None, 'head_cursor': st.get('head_cursor'),
           'backfill_done': bool(st.get('done')), 'probe_requests': 0,
           'start_ms': None, 'latest_ts': None, 'elapsed_sec': 0.0,
           'bootstrapped': False, 'error': None, 'output': ''}
    try:
        write_header_if_needed(path)
        res['appended'] = 0
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # ---- 1) 历史回填：从地板（或 N 天窗口起点）向后连续推游标 ----
            # 定时轮默认不推历史：一条流百万根就能吃光整轮时间预算，同轮其他币的
            # 实时增量会被挤没；历史交给连续跑批（页面 ▶ 全量回填 / CLI --backfill）。
            allow_bf = (bool(cfg.get('schedule_backfill'))
                        or stream['key'] in _backfill_active)
            if allow_bf:
                need = _prepare_backfill(stream, cfg, path, st, exchange, step_ms,
                                         sleep_fn)
                res['earliest_ms'] = st.get('earliest_ms')
                if need:
                    got = _consume_backfill(stream, cfg, path, st, exchange, step_ms,
                                            deadline, sleep_fn)
                    if got:
                        res['backfilled'] = got['appended']
                        res['gap_rows'] = got['gap_rows']
                        res['halt'] = got['halt']
                        res['head_cursor'] = got['cursor']
                        if got['error']:
                            res['error'] = got['error']

            # ---- 2) 尾段增量：从主文件末根追到最后一个已收盘整根 ----
            # 冷启动（回填直接写主文件）且尚未追平时必须跳过：尾段会从游标位置接着写同一个
            # 文件，把回填下一轮要请的范围提前写进去 → 必然重复（append_rows 不建 seen）
            cold_backfill = (not st.get('done')) and bool(st.get('writes_main'))
            if not cold_backfill:
                # 止点用「当前分钟边界」而不是最后一根已收盘整根的时间戳：_advance 的
                # stop 是开区间（只写 ts < stop），写成 now-step 会把刚收盘的那一根
                # 永远差在门外（每轮少补一分钟，滞后一直赖着不走）
                tail_stop = _align_down(exchange.milliseconds(), step_ms)
                last_ts = tail_last_timestamp(path)
                if last_ts is None:
                    # 空台账起头：新入列的币种没有末根锚点，尾段无从「接着补」，
                    # 不处理就会让这个币永远空着——直接请最近一整窗（300 根 ≈ 5 小时）
                    # 先落地，下一轮就按末根走正常增量
                    tail_cur = tail_stop - limit_for_exchange(EXCHANGE_ID, tf) * step_ms
                    res['bootstrapped'] = True
                else:
                    tail_cur = last_ts + step_ms
                res['start_ms'] = tail_cur
                if tail_cur <= tail_stop:
                    # 尾段只给 5 秒小预算：不能让全量回填把实时增量挤没
                    got2 = _advance(exchange, stream, cfg, path, tail_cur, tail_stop,
                                    step_ms, time.time() + 5.0, 0, sleep_fn)
                    res['tail_appended'] = got2['appended']
                    note_ledger_append(path, got2['appended'])
                    res['gap_rows'] += got2['gap_rows']
                    if got2['gap_rows']:
                        # 尾段缺口也归入「交易所侧取不到」，否则进度条永远算不到 100%
                        st['gap_rows'] = int(st.get('gap_rows') or 0) + got2['gap_rows']
                    if got2['error']:
                        res['error'] = got2['error']
        result_latest = tail_last_timestamp(path)
        res['latest_ts'] = result_latest
        res['backfill_done'] = bool(st.get('done'))
        res['head_cursor'] = st.get('head_cursor')
        res['output'] = buf.getvalue()
    except FloorProbeError as exc:
        res['error'] = f'地板探测失败: {exc}'
    except Exception as exc:
        res['error'] = f'{type(exc).__name__}: {exc}'
    res['appended'] = int(res.get('appended', 0)) + res['backfilled'] + res['tail_appended']
    res['elapsed_sec'] = round(time.time() - t0, 1)
    return res


def _stream_baseline(stream: dict, cfg: dict) -> dict:
    """从 CSV 与回填游标现算进度指标（进程重启后即为初始状态，无需 DB）。"""
    key = stream['key']
    path = csv_path(stream, cfg['data_dir'] or None)
    rows, first_ts = rows_meta(path)
    bf = _backfill_state.get(key, {})
    temp = bf.get('target_file')
    pending = bool(temp and temp != path and not bf.get('done'))
    temp_rows = int(bf.get('rows') or 0) if pending else 0
    last_line = _kf_read_tail(path, 1)
    latest_ts = last_line[0]['timestamp'] if last_line else None
    if last_line:
        last_line[0]['time_local'] = _local_iso(latest_ts)
        last_line[0]['time_utc'] = _utc_iso(latest_ts)
    st = {
        'key': key,
        'coin': stream.get('coin') or _base_of(key.split(':')[0]),
        'stream_type': stream.get('stream_type'),
        'inst_id': stream.get('inst_id'),
        'market_id': stream.get('market_id'),
        'available': stream.get('available'),
        'market_note': stream.get('market_note'),
        'market_checked_at': stream.get('market_checked_at'),
        'label': stream['label'],
        'sub': stream['sub'],
        'symbol': stream['symbol'],
        'csv_path': path,
        'csv_bytes': os.path.getsize(path) if os.path.exists(path) else 0,
        'rows_total': rows + temp_rows,
        'rows_main': rows,
        'first_ts': first_ts,
        'first_time_local': _local_iso(first_ts),
        'latest_ts': latest_ts,
        'latest_bar': last_line[0] if last_line else None,
        # ---- 全量回填游标（「有多少取多少」的进度就看这几个字段）----
        'earliest_ms': bf.get('earliest_ms'),
        'earliest_time_local': _local_iso(bf.get('earliest_ms')),
        'floor_source': bf.get('floor_source'),
        'probe_requests': bf.get('probe_requests'),
        'head_cursor': bf.get('head_cursor'),
        'head_cursor_time_local': _local_iso(bf.get('head_cursor')),
        'backfill_stop_ms': bf.get('stop_ms'),
        'backfill_done': bool(bf.get('done')),
        'backfill_rows': int(bf.get('rows') or 0),
        'backfill_gap_rows': int(bf.get('gap_rows') or 0),
        'backfill_batches': int(bf.get('batches') or 0),
        'backfill_rate': bf.get('rate'),
        'backfill_started_at': bf.get('started_at'),
        'backfill_finished_at': bf.get('finished_at'),
        'backfill_note': bf.get('note'),
        'backfill_paused': bool(_backfill_stop.get(key)),
        'backfill_temp': temp if pending else None,
        'backfill_temp_bytes': (os.path.getsize(temp)
                                if pending and os.path.exists(temp) else 0),
    }
    st['lag_seconds'] = _lag_seconds(latest_ts, cfg['timeframe'])
    st['coverage_days'] = round((latest_ts - first_ts) / 86_400_000, 2) \
        if latest_ts and first_ts else 0
    return st


# 1m 预置（进服务页时可能尚未建立 ccxt 实例，回填进度需要步长）
_TF_STEP_CACHE = {'1m': 60_000}


def _step_ms(exchange, timeframe: str) -> int:
    """周期 → 毫秒步长（ccxt parse_timeframe 结果按周期缓存）。"""
    tf = str(timeframe or '1m').strip()
    if tf not in _TF_STEP_CACHE:
        _TF_STEP_CACHE[tf] = int(exchange.parse_timeframe(tf)) * 1000
    return _TF_STEP_CACHE[tf]


def _lag_seconds(latest_ts, timeframe):
    """最新已收盘整根距今的滞后秒数（含该根本身的一个周期）。"""
    if not latest_ts:
        return None
    step = _TF_STEP_CACHE.get(str(timeframe)) or 60_000
    return max(0, int((time.time() * 1000 - (latest_ts + step)) / 1000))


def _refresh_stream_view(key: str, cfg=None, now_ms=None):
    """把 CSV/游标指标合并进展示状态（保留本轮已知结果，不重算）。"""
    cfg = cfg or load_config()
    stream = ensure_registry(cfg).get(key)
    if not stream:
        return
    base = _stream_baseline(dict(stream, key=key), cfg)
    with _state_lock:
        prev = _streams_state.get(key, {})
        for k in ('last_run_at', 'last_appended', 'last_backfilled', 'last_tail_appended',
                  'last_gap_rows', 'last_halt', 'last_elapsed_sec', 'last_error',
                  'last_start_ms', 'skipped', 'live_price', 'live_price_at',
                  'now_ms', 'rounds_ok', 'rounds_fail'):
            if k in prev:
                base[k] = prev[k]
        # 分母锚点用交易所时钟（上一轮记下的），没进过交易所时才退回本机墙钟
        base['now_ms'] = int(now_ms or base.get('now_ms') or time.time() * 1000)
        _backfill_progress(base, cfg)     # 进度/ETA 随台账与游标一起刷新（纯算术，不重扫文件）
        _streams_state[key] = base


# =============================================================================
# 任务体（interval 入口）
# =============================================================================

def run_kline_live_job(cfg=None, exchange=None, sleep_fn=time.sleep) -> str:
    """一轮监控台币种 1m 实时拉取（启用的币种×类型串行挨个跑）。返回处置字符串：
      success / partial_failed:N / skipped_busy / disabled / empty_streams /
      failed:exchange / failed:markets

    不开线程池：一页纸的实验任务不值得引入并行复杂度，且串行天然避开 ccxt
    同步实例的非线程安全节流器。上一轮未结束时本轮直接跳过（不排队补跑）。

    轮初先用 ccxt 市场表校验每个交易对（带 TTL 缓存，不是每轮都 load_markets），
    查不到的标「ccxt 无此数据源」并从本轮队列里剔除。
    """
    global _running, _rounds, _last_round_at, _interval_seconds
    cfg = cfg or load_config()
    if not cfg['enabled']:
        return 'disabled'
    if not _run_lock.acquire(blocking=False):
        _push_event('warn', '上一轮尚未结束（首轮回填或网络退避），本轮跳过')
        with _state_lock:
            for st in _streams_state.values():
                st['skipped'] = st.get('skipped', 0) + 1
        return 'skipped_busy'

    _running = True
    run_id = generate_run_id()
    t0 = time.time()
    try:
        try:
            ex = exchange if exchange is not None else _thread_exchange()
        except Exception as exc:
            _push_event('error', f'OKX(ccxt) 连接失败，本轮放弃: {exc!r}')
            task_log.error(f'[KlineLive][{run_id}] 交易所初始化失败: {exc!r}')
            return 'failed:exchange'

        _interval_seconds = cfg['interval_seconds']
        # 1) 开拉前校验：这个币在 okx 上到底有没有（现货/永续分开算）
        mk = resolve_markets(ex, cfg=cfg)
        if mk['error'] and not mk['checked']:
            _push_event('warn', f'ccxt 市场表校验失败，本轮沿用旧结论：{mk["error"]}')
        streams = active_streams(cfg)
        skipped = unavailable_streams(cfg)
        if not streams:
            task_log.warning(f'[KlineLive][{run_id}] 本轮无可抓币种：清单 '
                             f'{len(all_streams(cfg))} 条流，无数据源 '
                             f'{len(skipped)} 条（类型未启用或 okx 确实没这些市场）')
            return 'empty_streams'
        deadline = time.time() + cfg['round_budget_sec']
        task_log.info(f'[KlineLive][{run_id}] 开始: 数据流={len(streams)}个 '
                      f'币种={len({s["coin"] for s in streams})}个 '
                      f'无数据源跳过={len(skipped)}个 周期={cfg["timeframe"]} '
                      f'口径={cfg["backfill_mode"]} '
                      f'定时轮补历史={"开" if cfg.get("schedule_backfill") else "关"} '
                      f'单轮预算={cfg["round_budget_sec"]}s '
                      f'单轮上限={cfg["max_candles_per_stream"] or "不限"}根')
        fail_cnt = 0
        for st in streams:
            r = fetch_stream(st, cfg, ex, sleep_fn=sleep_fn, deadline=deadline)
            with _state_lock:
                view = _streams_state.setdefault(st['key'], {})
                view.update({
                    'key': st['key'], 'label': st['label'], 'sub': st['sub'],
                    'symbol': st['symbol'], 'coin': st['coin'],
                    'stream_type': st['stream_type'], 'inst_id': st['inst_id'],
                    'available': st.get('available'),
                    'market_note': st.get('market_note'),
                    'last_run_at': _now_text(),
                    'last_appended': r['appended'],
                    'last_backfilled': r['backfilled'],
                    'last_tail_appended': r['tail_appended'],
                    'last_gap_rows': r['gap_rows'],
                    'last_halt': r['halt'],
                    'last_elapsed_sec': r['elapsed_sec'],
                    'last_error': r['error'],
                    'last_start_ms': r['start_ms'],
                })
                if r['error']:
                    view['rounds_fail'] = view.get('rounds_fail', 0) + 1
                else:
                    view['rounds_ok'] = view.get('rounds_ok', 0) + 1
            # CSV/游标指标重算（行数按 size+mtime 记忆化，文件未变不重扫）；分母锁定交易所时钟
            _refresh_stream_view(st['key'], cfg, ex.milliseconds())
            _attach_live_price(st, ex)
            remain = int((_streams_state.get(st['key'], {})
                          .get('backfill_remaining_rows') or 0))
            if r['error']:
                fail_cnt += 1
                task_log.error(
                    f"[KlineLive][{run_id}] 流={st['key']} 成功=False 错误={r['error']} "
                    f"耗时={r['elapsed_sec']}s")
                _push_event('error', f"{st['label']} 抓取失败：{r['error']}")
            else:
                task_log.info(
                    f"[KlineLive][{run_id}] 流={st['key']} 尾段={r['tail_appended']}根 "
                    f"回填={r['backfilled']}根 游标={_utc_iso(r['head_cursor'])} "
                    f"停顿={r['halt']} 末根={_utc_iso(r['latest_ts'])} "
                    f"耗时={r['elapsed_sec']}s 成功=True")
                msg = f"{st['label']} 新增 {r['appended']} 根"
                if r['backfilled'] or r['tail_appended']:
                    msg += f"（历史回填 {r['backfilled']} 根、实时增量 {r['tail_appended']} 根）"
                if not r['backfill_done']:
                    msg += f"，全量仍差 {remain} 根（游标 {_local_iso(r['head_cursor'])}）"
                if r['gap_rows']:
                    msg += f"，交易所侧缺 {r['gap_rows']} 根"
                msg += f" · 末根 {_local_iso(r['latest_ts'])}"
                _push_event('ok', msg)

        elapsed = round(time.time() - t0, 1)
        with _state_lock:
            _rounds += 1
            _last_round_at = _now_text()
        _persist_snapshot()
        refresh_next_run_time()
        if fail_cnt:
            task_log.warning(f'[KlineLive][{run_id}] 本轮结束 失败={fail_cnt} 耗时={elapsed}s'
                             '（断点续传保证下轮自动补齐）')
            return f'partial_failed:{fail_cnt}'
        task_log.info(f'[KlineLive][{run_id}] 本轮结束 成功 耗时={elapsed}s')
        return 'success'
    finally:
        _running = False
        _run_lock.release()


def _attach_live_price(stream: dict, exchange):
    """最新未收盘价（fetch_ticker，公共接口）。失败不影响主流程，置 None。"""
    try:
        t = exchange.fetch_ticker(stream['symbol'])
        price = t.get('last') or t.get('close')
        ts = t.get('timestamp')
    except Exception:
        price, ts = None, None
    with _state_lock:
        view = _streams_state.setdefault(stream['key'], {})
        view['live_price'] = price
        view['live_price_at'] = _local_iso(ts) if ts else _now_text()


def _push_event(level: str, text: str):
    with _state_lock:
        _events.append({'ts': _now_text(), 'level': level, 'msg': text})
        if len(_events) > _EVENT_LIMIT:
            del _events[:-_EVENT_LIMIT]


# =============================================================================
# 运行快照（纯展示用；CSV 才是数据台账，因此只落文件、不进 MySQL）
# =============================================================================

def _snapshot_path() -> str:
    try:
        from . import data_paths
        return data_paths.resolve_data_file(SNAPSHOT_FILE)
    except Exception:  # pragma: no cover
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), SNAPSHOT_FILE)


def _persist_snapshot():
    with _state_lock:
        payload = {
            'saved_at': _now_text(),
            'rounds': _rounds,
            'last_round_at': _last_round_at,
            'interval_seconds': _interval_seconds,
            'streams': {k: {'last_run_at': v.get('last_run_at'),
                            'last_appended': v.get('last_appended'),
                            'last_elapsed_sec': v.get('last_elapsed_sec'),
                            'last_error': v.get('last_error'),
                            'rounds_ok': v.get('rounds_ok', 0),
                            'rounds_fail': v.get('rounds_fail', 0)}
                        for k, v in _streams_state.items()},
            # 回填游标必须落盘：百万根不可能在一轮里跑完，重启丢游标等于从头再探测
            'backfill': {k: {f: v.get(f) for f in _BF_PERSIST if v.get(f) is not None}
                         for k, v in _backfill_state.items() if v},
            'events': list(_events)[-20:],
        }
    path = _snapshot_path()
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        task_log.warning(f'[K线实时] 运行快照写入失败（不影响抓取）: {e}')


def _migrate_snapshot_key(key: str):
    """快照里的旧 stream key → 新 key；不是合法 key 的返回 None（丢掉而不是带病启动）。

    这里不能拿 STREAM_DEFS 当过滤条件：_load_snapshot 在模块导入末尾执行，那时注册表
    还是空的（建表要读监控台币种清单，会拉起 DB 依赖），照旧写法会把上次跑出来的
    游标全丢掉，重启就等于百万根回填白跑。
    """
    k = str(key or '').strip().lower()
    k = _LEGACY_KEY_MAP.get(k, k)
    base, _, t = k.partition(':')
    if t not in STREAM_TYPES or not _BASE_RE.match(base):
        return None
    return k


def _load_snapshot():
    global _rounds, _last_round_at, _interval_seconds
    path = _snapshot_path()
    if not os.path.isfile(path):
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    _rounds = int(data.get('rounds') or 0)
    _last_round_at = data.get('last_round_at')
    _interval_seconds = int(data.get('interval_seconds') or _interval_seconds)
    for line in data.get('events') or []:
        if isinstance(line, dict) and line.get('msg'):
            _events.append({'ts': line.get('ts'), 'level': line.get('level', 'info'),
                            'msg': '[恢复] ' + str(line['msg'])})
    for raw_key, st in (data.get('streams') or {}).items():
        key = _migrate_snapshot_key(raw_key)
        if key and isinstance(st, dict):
            _streams_state.setdefault(key, {}).update(st)
    for raw_key, bf in (data.get('backfill') or {}).items():
        key = _migrate_snapshot_key(raw_key)
        if key and isinstance(bf, dict) and bf.get('earliest_ms'):
            _backfill_state.setdefault(key, {}).update(bf)


# =============================================================================
# 状态 / 数据查询（供接口层）
# =============================================================================

def _coin_rollup(streams: list) -> list:
    """按币种聚合每条流的状态（页面/CLI 的「按币种看进度」就看这个）。

    available 三态聚合：只要有一条流能拉就算可用；全部被 ccxt 判无数据源才算这个币
    没数据源；一条都还没校验过（全是 None）时保持 None，页面显示「未校验」而不是骗人。
    """
    groups = {}
    for s in streams:
        groups.setdefault(s.get('coin') or '?', []).append(s)
    out = []
    for coin, items in groups.items():
        flags = [s.get('available') for s in items]
        avail = any(f is True for f in flags)
        unchecked = all(f is None for f in flags)
        done = [s for s in items if s.get('backfill_done')]
        pcts = [s.get('backfill_pct') for s in items if s.get('backfill_pct') is not None]
        latest = [s.get('latest_ts') for s in items if s.get('latest_ts')]
        notes = [s.get('market_note') for s in items
                 if s.get('available') is False and s.get('market_note')]
        out.append({
            'coin': coin,
            'streams': [s['key'] for s in items],
            'types': [s.get('stream_type') for s in items],
            'available': (None if unchecked else avail),
            'stream_count': len(items),
            'ready_count': len(done),
            'rows_total': sum(int(s.get('rows_total') or 0) for s in items),
            'csv_bytes': sum(int(s.get('csv_bytes') or 0) for s in items),
            'backfill_pct': (min(pcts) if pcts else None),
            'backfill_remaining_rows': sum(int(s.get('backfill_remaining_rows') or 0)
                                            for s in items),
            'latest_ts': max(latest) if latest else None,
            'latest_time_local': _local_iso(max(latest)) if latest else None,
            'last_run_at': max([s.get('last_run_at') for s in items
                                 if s.get('last_run_at')] or [None]),
            'market_note': ('；'.join(dict.fromkeys(notes)) or None) if notes else None,
        })
    # 保持 streams 传入顺序（= 注册表顺序 = monitor_coins = 固定池按市值降序），
    # 不再按币名字母重排：groups 依 streams 首次出现建立，out 自然就是市值序，
    # 页面/CLI「按币种看进度」与下拉候选都用这个序（大币在前），别让字母序把 BTC/ETH 挤后面
    return out


def coin_catalog(cfg=None) -> dict:
    """币种目录（给页面勾选与 /api/coins）：不触网，只用注册表 + 已有的校验结论。

    没校验过时 available 统一为 None（页面显示「未校验」），因为这里不能为了填页面
    去调 load_markets（GET 路径零外部请求是硬约定）。
    """
    cfg = cfg or load_config()
    defs = all_streams(cfg)
    with _state_lock:
        detail = []
        for sd in defs:
            view = _streams_state.get(sd['key']) or {}
            detail.append({
                'key': sd['key'], 'coin': sd['coin'], 'stream_type': sd['stream_type'],
                'label': sd['label'], 'sub': sd['sub'], 'symbol': sd['symbol'],
                'inst_id': sd['inst_id'],
                'market_id': sd['market_id'], 'filename': sd['filename'],
                'available': sd.get('available'), 'market_note': sd.get('market_note'),
                'market_checked_at': sd.get('market_checked_at'),
                'enabled': sd['stream_type'] in cfg['streams'],
                'rows_total': int(view.get('rows_total') or 0),
                'backfill_pct': view.get('backfill_pct'),
                'backfill_done': bool(view.get('backfill_done')),
                'latest_ts': view.get('latest_ts'),
                'latest_time_local': _local_iso(view.get('latest_ts')),
                'last_run_at': view.get('last_run_at'),
            })
    return {
        'coins': _coin_rollup(detail),
        'streams': detail,
        'enabled_coins': list(cfg['coins']),
        'watch_coins': watch_coins(),
        'monitor_coins': [sd['coin'] for sd in defs
                          if sd['stream_type'] == cfg['streams'][0]],
        'stream_types': [{'value': t, 'label': tpl['type_label'], 'suffix': tpl['suffix']}
                         for t, tpl in STREAM_TYPES.items()],
        'selected_types': list(cfg['streams']),
        'markets_checked': bool(_markets_cache['markets']),
        'markets_checked_at': (_now_text_from_ts(_markets_cache['at'])
                               if _markets_cache['at'] else None),
        'schedule_backfill': bool(cfg.get('schedule_backfill')),
    }


def get_status(cfg=None) -> dict:
    """页面主状态载荷：调度信息 + 每个币种每条流的进度 + 最新数据 + 运行流水。"""
    cfg = cfg or load_config()
    # 注册表全集（含 ccxt 无数据源的）：这样页面上被跳过的币是「标灰 + 写明原因」，
    # 而不是默默消失，用户分不清「没启用」和「okx 根本没这个市场」
    defs = all_streams(cfg)
    # 每次状态读取都按 CSV 现算一遍：rows_meta 有 size+mtime 记忆化，尾部
    # 只读一个数据块，代价是常数级；但能保证定时轮之间页面不显示过期末根。
    for sd in defs:
        if sd.get('available') is not False:
            _refresh_stream_view(sd['key'], cfg)
    with _state_lock:
        streams = []
        for sd in defs:
            d = dict(_streams_state.get(sd['key']) or {})
            d.setdefault('key', sd['key'])
            d.setdefault('label', sd['label'])
            # 币种与校验结论每次都以注册表为准（快照里可能存着旧 key 时期的记录）
            d['coin'] = sd['coin']
            d['stream_type'] = sd['stream_type']
            d['inst_id'] = sd['inst_id']
            d['available'] = sd.get('available')
            d['market_note'] = sd.get('market_note')
            d['market_checked_at'] = sd.get('market_checked_at')
            d.setdefault('rows_total', 0)
            _backfill_progress(d, cfg)
            d['latest_bar'] = d.get('latest_bar') or None
            streams.append(d)
        coins = _coin_rollup(streams)
        missing = [s for s in streams if s.get('available') is False]
        return {
            'enabled': cfg['enabled'],
            'timeframe': cfg['timeframe'],
            'interval_seconds': cfg['interval_seconds'],
            'backfill_mode': cfg['backfill_mode'],
            'backfill_modes': list(BACKFILL_MODES),
            'since_days': cfg['since_days'],
            'round_budget_sec': cfg['round_budget_sec'],
            'request_interval_ms': cfg['request_interval_ms'],
            'max_candles_per_stream': cfg['max_candles_per_stream'],
            'schedule_backfill': bool(cfg.get('schedule_backfill')),
            'backfill_running': bool(_backfill_thread and _backfill_thread.is_alive()),
            'data_dir': cfg['data_dir'] or os.path.join(_KF_DATA_DIR, 'live'),
            'job_id': JOB_ID,
            'next_run_at': _next_run_at,
            'running': _running,
            'rounds': _rounds,
            'last_round_at': _last_round_at,
            'server_time': _now_text(),
            'uptime_sec': round(time.time() - _started_at),
            'exchange': EXCHANGE_ID,
            'coin_list': [c['coin'] for c in coins],
            'coins_enabled': list(cfg['coins']),
            'watch_coins': watch_coins(),
            'stream_types': [{'value': t, 'label': tpl['type_label']}
                             for t, tpl in STREAM_TYPES.items()],
            'coins': coins,
            'coins_total': len(coins),
            'coins_available': len([c for c in coins if c['available'] is True]),
            'coins_missing': len([c for c in coins if c['available'] is False]),
            'coins_unchecked': len([c for c in coins if c['available'] is None]),
            'streams_total': len(streams),
            'streams_skipped': len(missing),
            'markets_cache_at': _markets_cache['at'],
            'streams': streams,
            'events': list(reversed(_events)),
        }


def _target_rows(cfg: dict, view: dict, now_ms=None) -> int:
    """全量目标根数：该 symbol 的数据地板 → 现在（days 模式为窗口起点 → 现在）。

    地板未知（从未探测过）时返回 0：宁可在页面上显示「目标未知」，也不拿一个
    假分母（旧口径用 since_days 当天花板，真正的全量回填会永远显示“还差很多”）。
    """
    step_ms = _TF_STEP_CACHE.get(str(cfg['timeframe'])) or 60_000
    earliest = view.get('earliest_ms')
    if not earliest:
        return 0
    ref = now_ms or view.get('now_ms') or int(time.time() * 1000)
    end = _align_down(ref, step_ms)
    return max(0, int((end - int(earliest)) // step_ms))


def _backfill_progress(st: dict, cfg: dict) -> dict:
    """全量回填进度：分母 = 地板→现在，分子 = 台账已到位 + 交易所侧缺口。

    「有多少取多少」的诚实口径：交易所本身就没有的那几分钟也算「已处理」，
    否则进度条会永远卡在 99.x% 追不到头。ETA 用游标引擎实测速率（根/秒）外推；
    回填完成后进度固定 100%，差额转为「实时滞后多少根」。
    """
    step_ms = _TF_STEP_CACHE.get(str(cfg['timeframe'])) or 60_000
    target = _target_rows(cfg, st)
    covered = int(st.get('rows_total') or 0)
    gap = int(st.get('backfill_gap_rows') or 0)
    need = max(0, target - gap)
    remaining = max(0, need - covered)
    rate = float(st.get('backfill_rate') or 0)
    if st.get('backfill_done'):
        last = st.get('latest_ts')
        edge = _align_down(int(st.get('now_ms') or time.time() * 1000), step_ms) - step_ms
        remaining = max(0, int((edge - last) // step_ms)) if last else remaining
    st['backfill_target_rows'] = target
    st['backfill_need_rows'] = need
    st['backfill_covered_rows'] = covered
    st['backfill_pct'] = (round(min(100.0, covered / need * 100), 2) if need
                          else (100.0 if st.get('backfill_done') else None))
    st['backfill_remaining_rows'] = remaining
    st['backfill_eta_sec'] = (int(remaining / rate) if rate > 0 and remaining
                              and not st.get('backfill_done') else 0)
    return st


def get_bars(stream_key: str, limit: int = 60) -> dict:
    """读取某条流末段 N 根 K 线（倒序：最新在前）。"""
    cfg = load_config()
    stream = get_stream(stream_key, cfg)
    if not stream:
        raise ValueError(f'未知数据流: {stream_key}')
    path = csv_path(stream, cfg['data_dir'] or None)
    rows = _kf_read_tail(path, max(1, min(int(limit), 500)))
    for r in rows:
        r['time_local'] = _local_iso(r['timestamp'])
    rows.reverse()
    return {'stream': stream['key'], 'label': stream['label'], 'symbol': stream['symbol'],
            'coin': stream['coin'], 'stream_type': stream['stream_type'],
            'count': len(rows), 'bars': rows, 'csv_path': path}


def trigger_pull_async() -> dict:
    """页面「立即拉取」：后台线程跑一轮，不阻塞请求，也不与调度轮争抢（同一把锁）。"""
    t = threading.Thread(target=run_kline_live_job, name='KlineLiveManual', daemon=True)
    t.start()
    return {'started': True, 'at': _now_text()}


def _stream_def_or_die(stream_key: str, cfg: dict = None) -> dict:
    """按 key 取流定义；未知 key 抛 ValueError（接口层据此报 400）。"""
    stream = get_stream(stream_key, cfg)
    if not stream:
        raise ValueError(f'未知数据流: {stream_key}（不在监控台币种清单或类型未启用）')
    return stream


def stream_keys_for(coins=None, types=None, cfg=None) -> list:
    """把「币种 + 类型」翻译成 stream key 列表（页面按币种回填、CLI --coin 都用它）。

    coins 写成 BTC / btc-usdt-swap / 全部/all 都接受；无数据源的流也会包进来（由
    run_backfill 在请求前拒绝），这样用户能拿到「为什么这个币不能回填」的明确错误，
    而不是静默地少跑一个币。
    """
    cfg = cfg or load_config()
    defs = ensure_registry(cfg)
    if types is None:
        types = cfg['streams']
    tset = {t for t in (str(x).strip().lower() for x in types) if t in STREAM_TYPES}
    if coins is None or (isinstance(coins, str) and coins.strip().lower() in ('', 'all', '*')):
        cset = None
    else:
        raw = coins.split(',') if isinstance(coins, str) else list(coins or [])
        cset = {b for b in (_base_of(x) for x in raw) if b}
    keys = [sd['key'] for sd in defs.values()
            if sd['stream_type'] in tset and (cset is None or sd['coin'] in cset)]
    if not keys:
        raise ValueError('没有匹配的数据流（检查币种写法与启用类型）')
    return keys


# =============================================================================
# 全量回填（连续跑批）：调度轮每 60s 只能推进一个时间预算，百万根靠它要跑好几天
# =============================================================================

def run_backfill(stream_key: str, budget_sec=None, until_rows=None, cfg=None,
                 exchange=None, sleep_fn=time.sleep, on_round=None) -> dict:
    """连续推一条流的历史游标，直到：追平 / 预算耗尽 / 新增够 until_rows / 人工停止。

    与定时轮共用 _run_lock：同一份 live/ CSV 只允许一个写入者。回填期间调度轮会
    被「上一轮尚未结束」挡住，实时尾段因此滞后，停批后下一轮自动追平（不丢数据）。

    开拉前先用 ccxt 市场表校验该交易对；被判定「ccxt 无此数据源」的直接拒绝。
    """
    cfg = cfg or load_config()
    stream = _stream_def_or_die(stream_key, cfg)
    out = {'ok': False, 'stream': stream['key'], 'coin': stream['coin'],
           'stream_type': stream['stream_type'], 'appended': 0, 'gap_rows': 0,
           'batches': 0, 'rounds': 0, 'elapsed_sec': 0.0, 'done': False,
           'halt': None, 'error': None, 'head_cursor': None, 'earliest_ms': None,
           'rate': None}
    if not _run_lock.acquire(blocking=True, timeout=float(_RUN_LOCK_WAIT_SEC)):
        out['error'] = (f'{_RUN_LOCK_WAIT_SEC:g}s 内没拿到台账写入权'
                        '（定时轮或其他回填正在写同一份 CSV），请稍后重试')
        return out
    t0 = time.time()
    st = {}
    ex = exchange
    _backfill_active.add(stream['key'])
    try:
        ex = exchange if exchange is not None else _thread_exchange()
        mk = resolve_markets(ex, cfg=cfg)
        stream = _stream_def_or_die(stream_key, cfg)
        out['markets'] = {'checked': mk['checked'], 'available': mk['available'],
                          'missing': mk['missing'], 'reused': mk['reused'],
                          'error': mk['error']}
        if stream.get('available') is False:
            out['halt'] = 'no_market'
            out['error'] = stream.get('market_note') or NO_SOURCE_MARK
            return out
        if mk['error'] and stream.get('available') is None:
            # 市场表没拿到又不能确定这个交易对存在：百万根跑批不能“猜”，直接报回原因
            out['halt'] = 'markets_unavailable'
            out['error'] = (f'ccxt 市场表不可用，无法确认 {stream["market_id"]}：'
                           f'{mk["error"]}')
            return out
        step_ms = _step_ms(ex, cfg['timeframe'])
        path = csv_path(stream, cfg['data_dir'] or None)
        st = _bf_state(stream['key'])
        end_at = (t0 + budget_sec) if budget_sec else None
        while True:
            if _backfill_stop.get(stream['key']):
                out['halt'] = 'stopped'
                break
            if end_at and time.time() >= end_at:
                out['halt'] = 'budget'
                break
            if until_rows and out['appended'] >= until_rows:
                out['halt'] = 'until_rows'
                break
            if not _prepare_backfill(stream, cfg, path, st, ex, step_ms, sleep_fn):
                out['halt'] = out['halt'] or 'none_needed'
                break
            deadline = time.time() + max(float(cfg['round_budget_sec']), 5.0)
            if end_at:
                deadline = min(deadline, end_at)
            chunk_rows = None
            if until_rows:
                chunk_rows = max(1, int(until_rows) - out['appended'])
            got = _consume_backfill(stream, cfg, path, st, ex, step_ms, deadline,
                                    sleep_fn, max_rows=chunk_rows)
            if got is None:
                out['halt'] = 'stopped'
                break
            out['rounds'] += 1
            out['appended'] += got['appended']
            out['gap_rows'] += got['gap_rows']
            out['batches'] += got['batches']
            # 每段都把游标落一次快照：连续跑批要跑几小时，中途 Ctrl+C/被杀/断电
            # 都不该从上一轮的位置重来（不然会重复请己经到手的一段）
            _persist_snapshot()
            if on_round:
                on_round({'stream': stream['key'], 'coin': stream['coin'],
                          'appended': out['appended'],
                          'cursor_ms': st.get('head_cursor'), 'rate': st.get('rate'),
                          'elapsed_sec': round(time.time() - t0, 1),
                          'rows': int(st.get('rows') or 0),
                          'gap_rows': out['gap_rows']})
            if got['error']:
                out['error'] = got['error']
                out['halt'] = 'error'
                break
            if st.get('done'):
                out['halt'] = 'caught_up'
                break
        out['done'] = bool(st.get('done'))
        out['head_cursor'] = st.get('head_cursor')
        out['earliest_ms'] = st.get('earliest_ms')
        out['rate'] = st.get('rate')
        out['ok'] = out['error'] is None
    except FloorProbeError as exc:
        out['error'] = f'地板探测失败: {exc}'
        out['halt'] = 'probe_error'
    except Exception as exc:
        out['error'] = f'{type(exc).__name__}: {exc}'
        out['halt'] = 'error'
    finally:
        out['elapsed_sec'] = round(time.time() - t0, 1)
        _backfill_active.discard(stream['key'])
        _refresh_stream_view(stream['key'], cfg,
                             ex.milliseconds() if ex is not None else None)
        _persist_snapshot()
        _run_lock.release()
    return out


def _stream_label(key: str, cfg: dict = None) -> str:
    sd = get_stream(key, cfg)
    return sd['label'] if sd else str(key)


def _backfill_worker(keys, budget_sec):
    """后台串行跑多条流的全量回填（串行天然避开 ccxt 同步实例的非线程安全节流）。"""
    global _backfill_thread
    try:
        for k in keys:
            if _backfill_stop.get(k):
                continue
            cfg = load_config()
            label = _stream_label(k, cfg)
            _push_event('info', f'{label} 全量回填开始（预算 '
                               f'{budget_sec or "不限"}s）')
            r = run_backfill(k, budget_sec=budget_sec, cfg=cfg)
            text = (f'{label} 回填结束：本次新增 {r["appended"]:,} 根'
                    f'，耗时 {r["elapsed_sec"]:.0f}s，游标 {_local_iso(r["head_cursor"])}'
                    f'，原因 {r["halt"]}')
            if r['error']:
                text += f'，错误 {r["error"]}'
            _push_event('ok' if r['ok'] else 'error', text)
    finally:
        with _state_lock:
            _backfill_thread = None


def start_full_backfill(stream_keys=None, budget_sec=None, coins=None) -> dict:
    """页面/接口用：后台线程连续回填，不阻塞请求，进度仍由 /api/status 轮询反映。

    coins 传 'BTC,ETH' 或 'all' 可以只回填部分币种；不传 keys 也不传 coins 时，
    默认跑全部「可用」的流，被 ccxt 判无数据源的不入队（不报错，但回报里写清楚跳了几条）。
    """
    global _backfill_thread
    cfg = load_config()
    skipped = []
    if stream_keys:
        keys = []
        for k in stream_keys:
            sd = get_stream(k, cfg)
            if not sd:
                raise ValueError(f'未知数据流: {k}（不在监控台币种清单或类型未启用）')
            keys.append(sd['key'])
            if sd.get('available') is False:
                skipped.append({'stream': sd['key'], 'reason': sd.get('market_note')
                                or NO_SOURCE_MARK})
    elif coins:
        keys = stream_keys_for(coins, cfg=cfg)
    else:
        keys = [s['key'] for s in active_streams(cfg)]
        skipped = [{'stream': s['key'], 'reason': s.get('market_note') or NO_SOURCE_MARK}
                   for s in unavailable_streams(cfg)]
    with _state_lock:
        if _backfill_thread and _backfill_thread.is_alive():
            return {'started': False, 'reason': 'already_running',
                    'message': '全量回填已在运行中，先看进度，需要可先停止'}
        for k in keys:
            _backfill_stop.pop(k, None)
        _backfill_thread = threading.Thread(target=_backfill_worker, args=(keys, budget_sec),
                                            name='KlineLiveBackfill', daemon=True)
        _backfill_thread.start()
    return {'started': True, 'streams': list(keys),
            'coins': list(dict.fromkeys(get_stream(k, cfg)['coin'] for k in keys)),
            'budget_sec': budget_sec, 'skipped': skipped, 'at': _now_text()}


def stop_full_backfill(stream_key: str = None, coins=None) -> dict:
    """叫停回填（游标不丢，下次 start 或调度轮从原地接着跑）。"""
    cfg = load_config()
    if stream_key:
        sd = get_stream(stream_key, cfg)
        if not sd:
            raise ValueError(f'未知数据流: {stream_key}')
        keys = [sd['key']]
    elif coins:
        keys = stream_keys_for(coins, cfg=cfg)
    else:
        keys = list(ensure_registry(cfg))
    for k in keys:
        _backfill_stop[k] = True
    return {'stopping': keys, 'at': _now_text()}


def audit_stream(stream_key: str) -> dict:
    """精确体检：整份扫一遍台账（O(1) 内存，百万行约十几秒），返回真实行数与缺口。

    页面上的 rows_total 平时是「字节差/平均行长」估算值，进度足够，但不能当审计
    结论用；审计只在按需调用时跑，不每 5 秒自动执行。
    """
    cfg = load_config()
    stream = _stream_def_or_die(stream_key, cfg)
    stream_key = stream['key']
    path = csv_path(stream, cfg['data_dir'] or None)
    step_ms = _TF_STEP_CACHE.get(str(cfg['timeframe'])) or 60_000
    main = scan_continuity(path, step_ms)
    temp = _backfill_state.get(stream_key, {}).get('target_file')
    extra = (scan_continuity(temp, step_ms)
             if temp and temp != path and os.path.exists(temp) else None)
    bf = _backfill_state.get(stream_key, {})
    view = _streams_state.get(stream_key) or {}
    target = _target_rows(cfg, view) if view.get('earliest_ms') else 0
    missing = int(main.get('missing_rows') or 0) + int((extra or {}).get('missing_rows') or 0)
    # 分段文件里的行同样属于台账（只是还没归并回主文件），算覆盖率不能只看主文件
    rows_exact = int(main.get('rows') or 0) + int((extra or {}).get('rows') or 0)
    return {
        'stream': stream_key, 'label': stream['label'], 'coin': stream['coin'],
        'stream_type': stream['stream_type'], 'csv_path': path,
        'step_ms': step_ms, 'main': main, 'backfill_segment': extra,
        'rows_exact': rows_exact,
        'first_ts': main.get('first_ts') or (extra or {}).get('first_ts'),
        'last_ts': main.get('last_ts'),
        'dup_rows': int(main.get('dup_rows') or 0) + int((extra or {}).get('dup_rows') or 0),
        'ledger_missing_rows': missing,
        'exchange_gap_rows': int(bf.get('gap_rows') or 0),
        'target_rows': target,
        'coverage_pct': (round(rows_exact / target * 100, 3) if target else None),
        'audited_at': _now_text(),
    }


def refresh_next_run_time():
    """从调度器取该任务下次触发时刻（调度器不可用时保持原值）。"""
    global _next_run_at
    try:
        try:
            from .task.scheduler import task_scheduler
        except ImportError:  # pragma: no cover
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from task.scheduler import task_scheduler
        _next_run_at = task_scheduler.next_run_time(JOB_ID)
    except Exception:
        _next_run_at = None
    return _next_run_at


# =============================================================================
# 调度注册
# =============================================================================

def register_kline_live_job():
    """注册监控台币种 1m 实时拉取任务；返回 (job_id | None, 说明)。

    job_id 沿用旧值 kline_live_btc：调度器的任务开关状态存在 DB 里，改 id 会丢开关。
    """
    try:
        try:
            from .task.scheduler import task_scheduler
        except ImportError:  # pragma: no cover
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from task.scheduler import task_scheduler
    except Exception as e:  # pragma: no cover
        task_log.warning(f'[K线实时] 调度器不可用，任务未注册: {e}')
        return None, '调度器不可用'

    cfg = load_config()
    global _interval_seconds
    _interval_seconds = cfg['interval_seconds']
    if not cfg['enabled']:
        task_scheduler.remove_job(JOB_ID)
        task_log.info('[K线实时] enabled=false，任务已关闭')
        return None, '已关闭（kline_live_config.enabled=false）'

    coin_n = len(monitor_coins(cfg))
    type_labels = '、'.join(STREAM_TYPES[t]['type_label'] for t in cfg['streams'])
    desc = '每{}秒 · {} · {}个币种（{}，无数据源的自动跳过）'.format(
        cfg['interval_seconds'], cfg['timeframe'], coin_n, type_labels)
    job_id = task_scheduler.register_job(
        run_kline_live_job, trigger='interval', seconds=cfg['interval_seconds'],
        job_id=JOB_ID, job_name=f'监控台币种 1分钟K线实时拉取（{desc}）')
    refresh_next_run_time()
    return job_id, desc


# 进程内首次导入：把上次运行历史捞回来（CSV 指标现算，运行流水来自快照）
_load_snapshot()
