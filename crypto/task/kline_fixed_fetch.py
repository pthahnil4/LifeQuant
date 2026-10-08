#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 固定币种 K 线定时增量抓取（kline_fixed_fetch）
==================================================
按监控台「固定币种列表」（市值降序，大币优先）定时增量拉取 K 线 CSV，
底层全部复用 crypto/kline_fetcher 工具链（fetcher / csv_store /
exchange_fetcher / gap_backfill），交易所统一走 CCXT 封装的 okx。

与手动 CLI（python -m kline_fetcher.cli）的分工：
    CLI 负责首轮深回填与全量缺口体检；本任务只负责「上次断点之后」的
    日常增量 —— 因此续传锚点用 csv_store.tail_last_timestamp 轻量定位
    文件末尾（不全量解析 CSV），去重由 fetcher.fetch_incremental 的
    seen_ts + 严格前移的 start_ms 双保险保证，不重复写历史数据。

并发度评估（默认 3，可配 1~5）：
    - OKX 公共 K 线接口限制约 20 请求 / 2 秒 / IP；
    - ccxt 实例 enableRateLimit 强制单实例请求间隔 ≥100ms，叠加网络
      RTT（0.3~0.5s），单线程稳态 ≈2~3 请求/秒；
    - 3 并行稳态 ≈6~8 请求/秒（12~16 请求/2s），贴着限额但不越界；
      5 并行理论可达 20+ 请求/2s，会频繁触发 429（虽有 safe_fetch_ohlcv
      指数退避兜底，但整轮耗时反而变长）；
    - ccxt 同步版的节流器非线程安全，故每个工作线程持有独立 exchange
      实例（threading.local 缓存，load_markets 每轮每线程只做一次）。
    增量轮每币通常只需 1~2 个请求（补齐 interval_minutes 内的新 K 线），
    真正的高频压力出现在首轮回填，已由 max_candles_per_coin 限幅。

任务总开关走统一 task_switches（job_id='kline_fixed_fetch'），另有
kv 配置内 enabled 字段作为模块级软开关（关闭时不注册任务）。

配置（kv_store key='kline_fetch_config'，改动后需调
update_kline_fetch_config 或重启才反映到调度周期）：
    {
      "enabled": true,               # 模块软开关
      "timeframe": "1m",             # 基础周期（大周期由它聚合，暂不落盘）
      "interval_minutes": 10,        # 调度周期（重启/重注册生效）
      "max_workers": 3,              # 并行抓取币种数，钳位 1~5
      "since_days": 30,              # CSV 为空时的首拉回看天数
      "max_candles_per_coin": 20000, # 单币单轮最多新增根数（0=不限）
      "backfill_gaps": false,        # 缺口回填（需全量解析 CSV，默认关）
      "data_dir": ""                 # 空=工具链默认 crypto/data
    }
环境变量兜底（kv 读不到时生效）：
    CRYPTO_KLINE_FETCH_WORKERS / CRYPTO_KLINE_FETCH_INTERVAL_MIN。

首轮大量回填建议直接用 CLI（多交易所互备更快）：
    python -m kline_fetcher.cli --symbol BTC/USDT:USDT --timeframe 1m
"""

import contextlib
import datetime
import io
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# 双模式导入：Flask 包内（crypto.task.*）/ 独立脚本（task 目录在 sys.path）
try:
    from ..utils.logger import get_task_logger, generate_run_id
except ImportError:  # pragma: no cover
    _TASK_DIR = os.path.dirname(os.path.abspath(__file__))
    if _TASK_DIR not in sys.path:
        sys.path.insert(0, _TASK_DIR)
    from utils.logger import get_task_logger, generate_run_id

# kline_fetcher 工具链（位于 crypto/ 下）
try:
    from ..kline_fetcher import fetcher as _kf_fetcher
    from ..kline_fetcher.csv_store import (
        load_existing_timestamps,
        tail_last_timestamp,
        write_header_if_needed,
    )
    from ..kline_fetcher.exchange_fetcher import get_exchange
    from ..kline_fetcher.gap_backfill import backfill_gaps
except ImportError:  # pragma: no cover
    _CRYPTO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _CRYPTO_DIR not in sys.path:
        sys.path.insert(0, _CRYPTO_DIR)
    from kline_fetcher import fetcher as _kf_fetcher
    from kline_fetcher.csv_store import (
        load_existing_timestamps,
        tail_last_timestamp,
        write_header_if_needed,
    )
    from kline_fetcher.exchange_fetcher import get_exchange
    from kline_fetcher.gap_backfill import backfill_gaps

task_log = get_task_logger()

JOB_ID = 'kline_fixed_fetch'
KV_CONFIG_KEY = 'kline_fetch_config'
EXCHANGE_ID = 'okx'

DEFAULT_CONFIG = {
    'enabled': True,
    'timeframe': '1m',
    'interval_minutes': 10,
    'max_workers': 3,
    'since_days': 30,
    'max_candles_per_coin': 20000,
    'backfill_gaps': False,
    'data_dir': '',
}

# 单轮任务互斥：APScheduler 的 max_instances=1 只约束「同一任务不重叠」，
# 脚本/手工调用本模块跑同一轮时不受调度器管辖，这层锁补上这个缺口。
_run_lock = threading.Lock()

# 每线程一个 ccxt 实例（同步版节流器非线程安全，见模块注释）
_tl = threading.local()


def _utc_iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec='seconds')


# =============================================================================
# 配置读取（kv 优先，环境变量兜底，默认值保底；任何坏值都退回默认不炸任务）
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


def load_config() -> dict:
    """合并出本次运行的有效配置：defaults < env < kv_store。绝不抛异常。"""
    cfg = dict(DEFAULT_CONFIG)
    env_workers = str(os.environ.get('CRYPTO_KLINE_FETCH_WORKERS', '')).strip()
    if env_workers:
        cfg['max_workers'] = _num(env_workers, cfg['max_workers'], 1, 5)
    env_interval = str(os.environ.get('CRYPTO_KLINE_FETCH_INTERVAL_MIN', '')).strip()
    if env_interval:
        cfg['interval_minutes'] = _num(env_interval, cfg['interval_minutes'], 1, 240)
    try:
        try:
            from .. import config_store_repo as cs
        except ImportError:  # pragma: no cover
            import config_store_repo as cs
        stored = cs.load_json_config_cached(KV_CONFIG_KEY)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
    except Exception as e:
        task_log.warning(f'[K线增量] 读取 kv 配置失败，按环境/默认值执行: {e}')
    cfg['enabled'] = bool(cfg.get('enabled', True))
    cfg['timeframe'] = str(cfg.get('timeframe') or '1m').strip() or '1m'
    cfg['interval_minutes'] = int(_num(cfg.get('interval_minutes'), 10, 1, 240))
    cfg['max_workers'] = int(_num(cfg.get('max_workers'), 3, 1, 5))
    cfg['since_days'] = int(_num(cfg.get('since_days'), 30, 1, 3650))
    cfg['max_candles_per_coin'] = int(_num(cfg.get('max_candles_per_coin'), 20000, 0, 10_000_000))
    cfg['backfill_gaps'] = bool(cfg.get('backfill_gaps', False))
    cfg['data_dir'] = str(cfg.get('data_dir') or '').strip()
    return cfg


def update_kline_fetch_config(patch: dict) -> dict:
    """合并写入 kv 配置并重注册任务（周期/开关变更即时生效）；返回最新整份配置。"""
    try:
        from ..database import session_scope
        from .. import config_store_repo as cs
    except ImportError:  # pragma: no cover
        _CRYPTO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _CRYPTO_DIR not in sys.path:
            sys.path.insert(0, _CRYPTO_DIR)
        from database import session_scope
        import config_store_repo as cs
    clean = {k: v for k, v in (patch or {}).items() if k in DEFAULT_CONFIG}
    with session_scope() as s:
        current = cs.load_json_config(s, KV_CONFIG_KEY) or {}
        current.update(clean)
        cs.save_json_config(s, KV_CONFIG_KEY, current)
    try:
        cs.invalidate_config_cache(KV_CONFIG_KEY)
    except Exception:
        pass
    register_kline_fetch_job()
    return current


# =============================================================================
# 币种列表 / 交易对转换 / 存储路径（纯函数，离线冒烟覆盖）
# =============================================================================

def get_target_coins() -> list:
    """监控台固定币种列表（已按 CoinGecko 市值降序），去空白、保序去重。

    优先级口径：固定币种列表本身就是监控主列表（高于浮动/自由市场列表），
    get_fixed_coins 内部已用 config['market_ranks'] 排名升序 = 市值降序，
    无排名的币稳定排在最后，保证大市值高流动性交易对先被提交抓取。
    """
    try:
        from ..real_strategy_adapter import get_fixed_coins
    except ImportError:  # pragma: no cover
        from real_strategy_adapter import get_fixed_coins
    coins = [str(c or '').strip().upper() for c in get_fixed_coins()]
    return [c for c in dict.fromkeys(coins) if c]


def inst_to_ccxt_symbol(inst_id: str) -> str:
    """OKX instId → ccxt 统一交易对。

    BTC-USDT-SWAP → BTC/USDT:USDT（第三段 SWAP 是合约类型，第二段 USDT
    才是结算币；ccxt okx 线性永续口径，与 indicator_nav_service 的
    f'{base}/USDT:USDT' 保持一致）；
    BTC-USDT      → BTC/USDT（现货）。其余格式抛 ValueError。
    """
    parts = str(inst_id).strip().upper().split('-')
    if len(parts) == 3 and parts[2] == 'SWAP':
        base, quote = parts[0], parts[1]
        if not base or not quote:
            raise ValueError(f"无法解析交易对: {inst_id}")
        return f"{base}/{quote}:{quote}"
    if len(parts) == 2 and all(parts):
        return f"{parts[0]}/{parts[1]}"
    raise ValueError(f"无法解析交易对: {inst_id}")


def build_save_path(inst_id: str, timeframe: str, data_dir: str = '') -> str:
    """单币独立 CSV 路径：{目录}/{base小写}_ohlcv_robust_{tf}_since_2022.csv。

    目录缺省沿用工具链 fetcher.DATA_DIR（crypto/data，可被包内 data/ 覆盖），
    命名与回测策略硬编码引用保持一致，便于隔离管理与按币独立回填。
    """
    base = str(inst_id).upper().split('-')[0].lower()
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, f"{base}_ohlcv_robust_{timeframe}_since_2022.csv")
    return _kf_fetcher.default_save_path(base, timeframe)


def _thread_exchange():
    """返回本线程的 ccxt okx 实例（首次调用时建立并缓存）。"""
    ex = getattr(_tl, 'exchange', None)
    if ex is None:
        ex = get_exchange(EXCHANGE_ID)
        _tl.exchange = ex
    return ex


def resolve_resume_start_ms(exchange, save_path: str, step_ms: int, since_days: int) -> int:
    """断点续传起点：CSV 有数据 → 末根下一周期；空文件 → now - since_days。"""
    last_ts = tail_last_timestamp(save_path)
    if last_ts:
        return last_ts + step_ms
    now_ms = exchange.milliseconds()
    return now_ms - int(since_days) * 86_400_000


# =============================================================================
# 单币增量抓取
# =============================================================================

def fetch_one_coin(inst_id: str, cfg: dict = None, exchange=None,
                   sleep_fn=time.sleep) -> dict:
    """抓取单个币种「断点之后」的增量 K 线并去重追加 CSV。

    返回 {inst_id, symbol, save_path, start_ms, appended, elapsed_sec, backfilled}；
    网络/解析异常原样上抛，由调用方按币记录成功/失败，不影响其他币种。
    """
    cfg = cfg or load_config()
    exchange = exchange if exchange is not None else _thread_exchange()
    tf = cfg['timeframe']
    symbol = inst_to_ccxt_symbol(inst_id)
    save_path = build_save_path(inst_id, tf, cfg['data_dir'])
    step_ms = exchange.parse_timeframe(tf) * 1000
    write_header_if_needed(save_path)

    start_ms = resolve_resume_start_ms(exchange, save_path, step_ms, cfg['since_days'])
    # 增量窗口严格在断点之后，正常不会撞历史行；seen_ts 供
    # append_rows_dedup 批内去重 + 回填模式跨批去重。
    seen_ts = set()
    max_candles = cfg['max_candles_per_coin'] or None
    backfilled = False
    t0 = time.time()
    # fetch_incremental / backfill_gaps 沿用工具链的 print 输出，
    # 捕获进缓冲区：只在失败时随错误日志带出，正常轮次不刷任务日志。
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        appended = _kf_fetcher.fetch_incremental(
            exchange, EXCHANGE_ID, symbol, tf, step_ms,
            save_path, start_ms, seen_ts, max_candles, sleep_fn=sleep_fn,
        )
        seen, ordered = (load_existing_timestamps(save_path)
                         if cfg['backfill_gaps'] else (None, None))
        if cfg['backfill_gaps'] and len(ordered) <= 3_000_000:
            # 全量时间戳集合同一次解析取回，供 backfill_gaps 跨批去重
            backfill_gaps([exchange], [EXCHANGE_ID], symbol, tf,
                          step_ms, save_path, seen)
            backfilled = True
        elif cfg['backfill_gaps']:
            task_log.warning(
                f'[K线增量] {inst_id} CSV 行数超 300 万，跳过全量缺口检测（请用 CLI 体检）')
    return {
        'inst_id': inst_id,
        'symbol': symbol,
        'save_path': save_path,
        'start_ms': start_ms,
        'appended': int(appended),
        'elapsed_sec': round(time.time() - t0, 1),
        'backfilled': backfilled,
        'output': buf.getvalue(),
    }


def _fetch_coin_logged(inst_id, cfg, exchange_factory, run_id, sleep_fn):
    """工作线程入口：抓取 + 结构化单币日志。返回 (是否成功, 新增根数)。"""
    t0 = time.time()
    try:
        r = fetch_one_coin(inst_id, cfg=cfg, exchange=exchange_factory(),
                           sleep_fn=sleep_fn)
        task_log.info(
            f"[KlineFetch][{run_id}] 币种={inst_id} 起始={_utc_iso(r['start_ms'])} "
            f"新增={r['appended']}根 耗时={r['elapsed_sec']}s 成功=True "
            f"文件={os.path.basename(r['save_path'])}"
            + (" 缺口回填=已执行" if r['backfilled'] else ""))
        return True, r['appended']
    except Exception as e:
        task_log.error(
            f"[KlineFetch][{run_id}] 币种={inst_id} 成功=False 错误={e!r} "
            f"耗时={round(time.time() - t0, 1)}s")
        return False, 0


# =============================================================================
# 任务体（interval 入口）
# =============================================================================

def run_kline_fixed_fetch_job(cfg: dict = None, coins: list = None,
                              exchange_factory=None, sleep_fn=time.sleep) -> str:
    """一轮固定币种 K 线增量抓取；返回处置字符串（供冒烟断言/日志）。

      success / partial_failed:N / skipped_busy / disabled / empty_coins /
      failed:coins / failed:exchange
    单币失败不阻断整轮（断点续传保证下一轮自动从各自断点补上）。
    """
    cfg = cfg or load_config()
    if not cfg['enabled']:
        return 'disabled'
    if not _run_lock.acquire(blocking=False):
        task_log.warning('[KlineFetch] 上一轮抓取尚未结束（回填或网络拖慢），本轮跳过')
        return 'skipped_busy'
    run_id = generate_run_id()
    t0 = time.time()
    try:
        # 1) 目标币种：监控台固定列表，市值降序 = 提交线程池的优先级顺序
        if coins is None:
            try:
                coins = get_target_coins()
            except Exception as e:
                task_log.error(f"[KlineFetch][{run_id}] 读取固定币种列表失败: {e!r}")
                return 'failed:coins'
        coins = [str(c).strip().upper() for c in coins if str(c or '').strip()]
        if not coins:
            task_log.warning(f'[KlineFetch][{run_id}] 固定币种列表为空，本轮无事可做')
            return 'empty_coins'

        # 2) 连通性探测：主线程建一次实例，网络不通就早退，
        #    避免 100 个币各自重试退避把一轮拖成小时级。
        factory = exchange_factory or _thread_exchange
        if exchange_factory is None:
            try:
                factory()
            except Exception as e:
                task_log.error(f'[KlineFetch][{run_id}] OKX(ccxt) 连接失败，本轮放弃: {e!r}')
                return 'failed:exchange'

        task_log.info(
            f'[KlineFetch][{run_id}] 开始: 币种={len(coins)}个 周期={cfg["timeframe"]} '
            f'并行={cfg["max_workers"]} 单币上限={cfg["max_candles_per_coin"] or "不限"}根')

        # 3) 并行抓取（提交顺序即市值优先级，线程池按序取任务）
        ok_cnt = fail_cnt = appended_total = 0
        with ThreadPoolExecutor(max_workers=cfg['max_workers'],
                                thread_name_prefix='KlineFetch') as pool:
            futures = [pool.submit(_fetch_coin_logged, inst, cfg, factory,
                                   run_id, sleep_fn) for inst in coins]
            for fut in futures:
                try:
                    ok, appended = fut.result()
                except Exception as e:  # 工作线程入口已兜底，这里只防未来改动漏网
                    fail_cnt += 1
                    task_log.error(f'[KlineFetch][{run_id}] 未知工作线程异常: {e!r}')
                    continue
                ok_cnt += 1 if ok else 0
                fail_cnt += 0 if ok else 1
                appended_total += appended

        elapsed = round(time.time() - t0, 1)
        summary = (f'[KlineFetch][{run_id}] 本轮结束: 成功={ok_cnt}/{len(coins)} '
                   f'失败={fail_cnt} 新增合计={appended_total}根 耗时={elapsed}s')
        if fail_cnt:
            task_log.warning(summary + '（失败币种将在下轮按断点自动补齐）')
            return f'partial_failed:{fail_cnt}'
        task_log.info(summary)
        return 'success'
    finally:
        _run_lock.release()


# =============================================================================
# 调度注册（scheduler.register_default_jobs 共用）
# =============================================================================

def register_kline_fetch_job():
    """注册固定币种 K 线增量抓取任务；返回 (job_id | None, 说明)。

    页面/文件级 task_switches 开关由 register_job 统一拦截；
    kv 配置 enabled=false 时这里额外做一次模块级软开关。
    """
    try:
        from .scheduler import task_scheduler
    except Exception as e:  # pragma: no cover
        task_log.warning(f'[K线增量] 调度器不可用，任务未注册: {e}')
        return None, '调度器不可用'

    cfg = load_config()
    if not cfg['enabled']:
        task_scheduler.remove_job(JOB_ID)
        task_log.info('[K线增量] enabled=false，任务已关闭')
        return None, '已关闭（kline_fetch_config.enabled=false）'

    minutes = int(cfg['interval_minutes'])
    desc = f'每{minutes}分钟 · {cfg["timeframe"]} · 并行{cfg["max_workers"]}'
    job_id = task_scheduler.register_job(
        run_kline_fixed_fetch_job, trigger='interval', minutes=minutes,
        job_id=JOB_ID,
        job_name=f'OKX固定币种K线增量抓取（{desc}）')
    return job_id, desc
