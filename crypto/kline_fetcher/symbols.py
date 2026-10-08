# -*- coding: utf-8 -*-
"""
币种配置表
==========

原先每个币种一个 ccxt_{coin}_robust_1m.py 的 runpy 包装脚本，内容完全相同、
仅 symbol 与输出文件名不同。这里统一为一张配置表，由 CLI 通过 --coin 引用，
彻底消除重复代码。

新增币种只需在 COINS 中添加一行即可。
"""

from typing import Dict

# coin_key -> (ccxt symbol, 默认存储文件名的币种前缀)
COINS: Dict[str, tuple] = {
    "btc":  ("BTC/USDT",  "btc"),
    "eth":  ("ETH/USDT",  "eth"),
    "sol":  ("SOL/USDT",  "sol"),
    "xrp":  ("XRP/USDT",  "xrp"),
    "ada":  ("ADA/USDT",  "ada"),
    "doge": ("DOGE/USDT", "doge"),
    "dot":  ("DOT/USDT",  "dot"),
    "link": ("LINK/USDT", "link"),
    "ltc":  ("LTC/USDT",  "ltc"),
    "near": ("NEAR/USDT", "near"),
}


def resolve_symbol(coin: str) -> str:
    """把币种简称解析为 ccxt symbol；未知则原样返回（允许直接传 BTC/USDT）。"""
    key = coin.strip().lower()
    if key in COINS:
        return COINS[key][0]
    return coin
