# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 2 自动搬运（2026-10-08）。
# 原名: futures/__init__.py  →  现位置: crypto/futures/__init__.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底已移除。
"""
期货交易模块
============

主要模块:
- akshare_api: 统一的 akshare 期货数据接口工具类（推荐使用）
- futures_adapter: 策略数据适配器（连接数据层与策略层）

策略代码统一存放于 strategies/ 目录:
  - strategies/pro3_strategy.py:  Pro3 策略核心
  - strategies/indicators.py:     公共技术指标函数

使用方式::

    from crypto.futures.akshare_api import AkshareFuturesAPI
    from crypto.market_strategy.pro3_strategy import FuturesPro3Strategy, run_pro3_strategy
"""

from crypto.futures.akshare_api import AkshareFuturesAPI
from crypto.market_strategy.pro3_strategy import FuturesPro3Strategy, run_pro3_strategy
