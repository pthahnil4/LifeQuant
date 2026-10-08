# -*- coding: utf-8 -*-
"""
命令行入口
==========

通用 K 线抓取命令行接口。业务逻辑全部委托给 fetcher.run，本模块只负责：
  - 解析参数
  - 组装运行配置（含币种表解析、默认存储路径推导、起始时间换算）
  - 调用 fetcher.run

用法示例：
    python -m kline_fetcher.cli --coin btc --timeframe 1m
    python -m kline_fetcher.cli --symbol ETH/USDT --timeframe 1h --exchanges okx,bybit
    python -m kline_fetcher.cli --coin sol --since 1700000000000 --max-candles 5000
"""

import argparse
import datetime
from types import SimpleNamespace

from . import fetcher
from .symbols import resolve_symbol

# 默认起始时间：2022-01-01T00:00:00Z（毫秒）
DEFAULT_SINCE_MS = int(
    datetime.datetime(2022, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kline_fetcher",
        description="获取交易所 K 线并稳健存储到本地 CSV（续传/去重/缺口回填）",
    )
    parser.add_argument(
        "--exchanges", default="okx,bybit,bitget",
        help="逗号分隔的交易所 id，按顺序用于抓取/回填缺口，首个为主交易所",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--coin", default=None,
        help="币种简称（btc/eth/sol/xrp/ada/doge/dot/link/ltc/near），自动映射 symbol 与默认文件名",
    )
    group.add_argument(
        "--symbol", default="BTC/USDT",
        help="交易对，如 BTC/USDT（未使用 --coin 时生效）",
    )
    parser.add_argument("--timeframe", default="1m", help="K 线周期，如 1m/5m/1h/1d，默认 1m")
    parser.add_argument(
        "--since", type=int, default=DEFAULT_SINCE_MS,
        help="起始时间戳（毫秒），默认 2022-01-01 UTC；CSV 已有数据时自动续传会覆盖此值",
    )
    parser.add_argument(
        "--save", default=None,
        help="输出 CSV 路径；缺省时按币种/周期推导为 strategyAI/data/{coin}_ohlcv_robust_{tf}_since_2022.csv",
    )
    parser.add_argument(
        "--no-backfill", action="store_true",
        help="跳过连续性检测与自动缺口回填",
    )
    parser.add_argument(
        "--max-candles", type=int, default=None,
        help="最多抓取多少根 K 线后停止（用于首轮爬取或限流保护）",
    )
    return parser


def make_config(args: argparse.Namespace) -> SimpleNamespace:
    """把命令行参数转换为 fetcher.run 所需的配置对象。"""
    if args.coin:
        symbol = resolve_symbol(args.coin)
        coin_prefix = args.coin.strip().lower()
    else:
        symbol = args.symbol
        # 从 "BTC/USDT" 取 "btc" 作为文件名前缀
        coin_prefix = symbol.split("/")[0].lower()

    save_path = args.save or fetcher.default_save_path(coin_prefix, args.timeframe)

    return SimpleNamespace(
        exchanges=[x.strip() for x in args.exchanges.split(",") if x.strip()],
        exchanges_objs=None,          # CLI 场景由 fetcher 自行建立实例
        symbol=symbol,
        timeframe=args.timeframe,
        since_ms=int(args.since),
        save_path=save_path,
        max_candles=args.max_candles,
        no_backfill=args.no_backfill,
    )


def main(argv=None) -> str:
    args = build_parser().parse_args(argv)
    config = make_config(args)
    return fetcher.run(config)


if __name__ == "__main__":
    main()
