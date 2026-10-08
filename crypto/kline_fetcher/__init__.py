# -*- coding: utf-8 -*-
"""
kline_fetcher —— K 线获取与本地存储工具包
==========================================

模块划分：
    csv_store         CSV 读写 / 表头 / 时间戳加载 / 去重追加
    exchange_fetcher  交易所实例 / 批次上限 / 带重试的安全抓取
    gap_backfill      时间缺口检测与多交易所回填
    fetcher           流程编排（续传 + 增量抓取 + 回填）
    symbols           币种配置表（替代原先每币种一个重复包装脚本）
    cli               命令行入口

对外便捷接口：
    from kline_fetcher import fetcher, cli
    cli.main(["--coin", "btc", "--timeframe", "1m"])
"""

from . import csv_store, exchange_fetcher, gap_backfill, fetcher, symbols  # noqa: F401

__all__ = ["csv_store", "exchange_fetcher", "gap_backfill", "fetcher", "symbols", "cli"]
