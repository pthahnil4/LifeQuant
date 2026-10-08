# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 3 自动搬运（2026-10-08）。
# 原名: stocks/__init__.py  →  现位置: crypto/stocks/__init__.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底与根级 config 依赖已移除。
"""
股票交易模块
============
提供 A 股行情数据获取、技术指标分析和策略信号生成功能。

核心模块：
  - stock_akshare_api.py: 统一的 akshare 股票数据接口封装
  - stock_adapter.py: 股票策略数据适配器（含技术指标计算）

策略代码统一存放于 strategies/ 目录:
  - crypto/market_strategy/pro3_strategy.py:  Pro3 策略核心
  - crypto/market_strategy/indicators.py:     公共技术指标函数
"""

from crypto.stocks.stock_akshare_api import AkshareStockAPI, AkshareAPIError
from crypto.market_strategy.pro3_strategy import StockPro3Strategy, run_stock_pro3_strategy
from crypto.stocks.stock_adapter import (
    get_stock_categories,
    get_all_stocks,
    get_stock_code_name_map,
    get_selected_stocks,
    set_selected_stocks,
    calculate_stock_single,
    calculate_stock_strategy_data,
    calculate_stock_multi_period,
    get_stock_strategy_detail,
    # Pro3 策略适配函数
    calculate_stock_pro3_single,
    calculate_stock_pro3_multi_period,
    get_stock_pro3_strategy_detail,
)
