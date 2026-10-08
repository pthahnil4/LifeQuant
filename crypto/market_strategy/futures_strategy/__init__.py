# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: strategies/futuresStrategy/__init__.py  →  现位置: crypto/market_strategy/futures_strategy/__init__.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
# -*- coding: utf-8 -*-
"""
futures_strategy 包
==================
期货版三策略（以 strategies/cryptoStrategy 为母本，核心逻辑完全一致，
仅数据获取层由 OKX API 替换为 akshare 期货数据源）：
- pro3_singletimeframe: 单周期 Pro3（含公共基础设施：数据获取/ADX/详情页构建）
- pro3_dualtimeframe:   真·双周期 Pro3（长周期 justice_flag 状态机 + 开仓限价单）
- BOLL 限价双周期策略沿用 strategies/futures_boll_limit_dualtimeframe.py
"""
