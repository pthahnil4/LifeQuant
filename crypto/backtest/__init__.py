#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
双周期多点位分批限价交易策略 — 回测框架
=======================================

独立于正在运行的定时任务（crypto/task/），仅"只读复用"策略信号引擎
（crypto/strategy/pro3_dualtimeframe.py），并在回测目录内自包含地复刻
分批限价挂单生命周期（batch_order_manager.py 的逻辑），用于历史回测。

模块：
- signal_engine : 历史K线加载 + 引擎逐bar注解(MODIFY_FLAG/LONG_DIRECTION/ADX) + CSV缓存
- batch_sim     : 逐bar分批限价挂单模拟引擎（点位隔离持仓 + 手续费 + 指标）
- run_backtest  : 3组双周期(5m-1H/15m-4H/1H-1D) × 静态/动态比例 对比 + 报告
"""
