# -*- coding: utf-8 -*-
"""
抓取流程编排模块
================

把 CSV 存储、交易所抓取、缺口回填三个模块串成完整的「获取 K 线并落盘」流程：

    run(config)
      ├── 初始化 CSV 表头
      ├── 建立交易所列表（首个为主交易所）
      ├── 确定起始时间（自动断点续传 or 从 --since 开始）
      ├── fetch_incremental()  增量抓取直到当前时间
      └── backfill_gaps()      连续性检测与缺口回填（可关闭）

设计要点：
  - 所有外部依赖（exchanges、sleep 函数）均可注入，便于离线单元测试；
  - 默认数据目录 DATA_DIR 指向 strategyAI/data，与各回测策略的硬编码路径保持一致。
"""

import datetime
import os
import time
from typing import List, Optional

from .csv_store import (
    append_rows_dedup,
    load_existing_timestamps,
    write_header_if_needed,
)
from .exchange_fetcher import get_exchange, limit_for_exchange, safe_fetch_ohlcv
from .gap_backfill import backfill_gaps

# 包所在目录：.../strategyAI/kline_fetcher
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
# 包内自带数据目录（打包带走时优先使用，使整个文件夹自包含、可移植）
_INTERNAL_DATA_DIR = os.path.join(_PKG_DIR, "data")
# 回退数据目录：.../strategyAI/data （原仓库布局，回测策略硬编码引用此路径）
_PARENT_DATA_DIR = os.path.normpath(os.path.join(_PKG_DIR, "..", "data"))
# 默认数据目录：优先包内自带 data/，不存在时回退到上级 strategyAI/data
DATA_DIR = _INTERNAL_DATA_DIR if os.path.isdir(_INTERNAL_DATA_DIR) else _PARENT_DATA_DIR


def default_save_path(coin: str, timeframe: str) -> str:
    """
    生成默认存储路径：{DATA_DIR}/{coin}_ohlcv_robust_{tf}_since_2022.csv

    DATA_DIR 优先为包内自带 data/（便于整体打包带走），不存在时回退到
    上级 strategyAI/data。命名与原 ccxt_btc_robust_1m.py 系列保持一致，
    确保回测策略能按既有硬编码路径找到文件。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    fname = f"{coin.lower()}_ohlcv_robust_{timeframe}_since_2022.csv"
    return os.path.join(DATA_DIR, fname)


def _utc_iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def open_exchanges(exchange_ids: List[str]):
    """
    按给定顺序建立交易所实例，跳过初始化失败者。

    返回 (exchanges, valid_ids)，两者一一对应；valid_ids[0] 为主交易所。
    """
    exchanges, valid_ids = [], []
    for ex_id in exchange_ids:
        try:
            exchanges.append(get_exchange(ex_id))
            valid_ids.append(ex_id)
        except Exception as e:
            print(f"Skip exchange {ex_id} due to error: {e}")
    if not exchanges:
        raise RuntimeError("No valid exchanges available")
    return exchanges, valid_ids


def resolve_start_ms(
    save_path: str, step_ms: int, since_ms: int, resume: bool, primary
) -> int:
    """
    确定起始抓取时间戳（毫秒）。

    规则：只要 CSV 已有数据，就自动从最后一根的下一周期续传（无论是否显式
    传 --resume）；否则从 since_ms 开始。返回 start_ms。
    """
    _, ordered_ts = load_existing_timestamps(save_path)
    if ordered_ts:
        last_ts = ordered_ts[-1]
        start_ms = last_ts + step_ms
        print(f"检测到已有数据 {len(ordered_ts)} 条，最后时间: {_utc_iso(last_ts)}")
        print(f"从断点续传: {_utc_iso(start_ms)}")
        return start_ms
    print(f"从头开始获取: {_utc_iso(since_ms)}")
    return int(since_ms)


def fetch_incremental(
    primary,
    primary_id: str,
    symbol: str,
    timeframe: str,
    step_ms: int,
    save_path: str,
    start_ms: int,
    seen_ts: set,
    max_candles: Optional[int] = None,
    sleep_fn=time.sleep,
    max_consecutive_failures: int = 5,
) -> int:
    """
    从 start_ms 起按批次增量抓取，去重写入 CSV，直到追上当前时间。

    返回本次实际追加的 K 线总数。连续失败达到阈值时停止（不无限占用网络）。
    """
    now_ms = primary.milliseconds()
    limit = limit_for_exchange(primary_id, timeframe)
    total_appended = 0
    consecutive_failures = 0

    while start_ms < now_ms - step_ms:
        batch = safe_fetch_ohlcv(primary, symbol, timeframe, start_ms, limit)
        if not batch:
            consecutive_failures += 1
            print(f"获取失败 ({consecutive_failures}/{max_consecutive_failures})，跳过当前批次")
            if consecutive_failures >= max_consecutive_failures:
                print(f"连续失败{max_consecutive_failures}次，停止获取")
                break
            # 跳过当前时间段，继续下一个
            start_ms += limit * step_ms
            sleep_fn(5)
            continue

        consecutive_failures = 0  # 重置失败计数
        appended = append_rows_dedup(save_path, primary_id, batch, seen_ts)
        total_appended += appended
        print(
            f"Fetched {len(batch)} (appended {appended}), "
            f"last dt={_utc_iso(batch[-1][0])}, total={total_appended}"
        )
        start_ms = batch[-1][0] + step_ms
        sleep_fn(max(getattr(primary, "rateLimit", 100) / 1000.0, 0.5))
        if max_candles and total_appended >= max_candles:
            break

    return total_appended


def run(config, sleep_fn=time.sleep) -> str:
    """
    执行完整抓取流程。

    config 需包含字段：
        exchanges(list[str]) symbol timeframe since_ms save_path
        max_candles no_backfill
    返回最终保存路径。
    """
    save_path = config.save_path
    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    write_header_if_needed(save_path)

    exchanges, valid_ids = (
        (config.exchanges_objs, config.exchanges)
        if getattr(config, "exchanges_objs", None)
        else open_exchanges(config.exchanges)
    )
    primary, primary_id = exchanges[0], valid_ids[0]
    step_ms = primary.parse_timeframe(config.timeframe) * 1000

    seen_ts, _ = load_existing_timestamps(save_path)
    start_ms = resolve_start_ms(save_path, step_ms, config.since_ms, True, primary)

    total = fetch_incremental(
        primary, primary_id, config.symbol, config.timeframe, step_ms,
        save_path, start_ms, seen_ts, config.max_candles, sleep_fn=sleep_fn,
    )
    print(f"Initial fetch appended={total}. Running continuity check...")
    if not config.no_backfill:
        backfill_gaps(exchanges, valid_ids, config.symbol, config.timeframe, step_ms, save_path, seen_ts)

    _, ordered_final = load_existing_timestamps(save_path)
    print(f"Final total candles={len(ordered_final)} saved to {save_path}")
    return save_path
