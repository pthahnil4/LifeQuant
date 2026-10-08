# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/__init__.py  →  现位置: crypto/market_strategy/__init__.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
"""
策略模块（strategies）
====================

集中管理所有交易策略，消除期货版/股票版重复代码。

模块结构:
  - indicators.py:           公共技术指标函数（ADX, ATR, SAR, MACD, EMA 等）
  - pro3_strategy.py:        Pro3 策略核心（MACD平滑+ADX自适应，纯 pandas 实现）
  - pro3_dualtimeframe.py:   Pro3 双周期回测策略（backtrader 实现）
  - pro3_singletimeframe.py: Pro3 单周期回测策略（backtrader 实现）
  - futures_boll_limit_dualtimeframe.py: 布林带限价双周期策略（backtrader 实现）

使用示例::

    from crypto.market_strategy.pro3_strategy import Pro3Strategy, run_pro3_strategy

    # 期货（多空双向）
    result = run_pro3_strategy(df, long_only=False)

    # 股票（仅做多）
    result = run_pro3_strategy(df, long_only=True, use_sar=False)

向后兼容别名:
  - FuturesPro3Strategy = Pro3Strategy(long_only=False, use_sar=True)
  - StockPro3Strategy   = Pro3Strategy(use_sar=False)
"""

# 公共指标函数
from crypto.market_strategy.indicators import (
    calculate_adx,
    calc_sar,
    calc_atr,
    get_adaptive_smooth_weight,
    calculate_ema,
    calculate_macd,
)

# Pro3 策略核心（纯 pandas 实现）
from crypto.market_strategy.pro3_strategy import (
    Pro3Strategy,
    FuturesPro3Strategy,
    StockPro3Strategy,
    run_pro3_strategy,
    run_stock_pro3_strategy,
    get_strategy_full_data,
    get_dual_timeframe_strategy_full_data,
)

__all__ = [
    # 指标函数
    'calculate_adx',
    'calc_sar',
    'calc_atr',
    'get_adaptive_smooth_weight',
    'calculate_ema',
    'calculate_macd',
    # Pro3 策略
    'Pro3Strategy',
    'FuturesPro3Strategy',
    'StockPro3Strategy',
    'run_pro3_strategy',
    'run_stock_pro3_strategy',
    'get_strategy_full_data',
    'get_dual_timeframe_strategy_full_data',
]

# BOLL 限价双周期策略（backtrader 实现）
from crypto.market_strategy.futures_boll_limit_dualtimeframe import (
    run_backtest as run_boll_backtest,
    get_latest_data as get_boll_latest_data,
)

__all__ += [
    'run_boll_backtest',
    'get_boll_latest_data',
]
