#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略计算全局串行锁（pro3 引擎独占）
====================================

为什么需要这把锁
----------------
pro3 引擎（``pro3_singletimeframe`` / ``pro3_dualtimeframe``）把运行参数放在
**模块级全局变量**里：``FAST_MODE`` / ``PRINT_*`` / ``CURRENT_INSTID`` /
``CURRENT_SHORT_BAR`` / ``CURRENT_LONG_BAR`` / ``ENTRY_PRICE_TYPE`` …
调用方普遍是「进函数改写全局 → 调引擎 → finally 恢复」的写法。

两个线程同时跑引擎时，后改写者赢，先恢复者又把后者的参数打回自己的值，最坏
后果不是报错而是**算出错误信号**：``CoreStrategy.stop()`` 仅在
``FAST_MODE is False`` 时才执行「期末强制平仓」分支，于是一个旁观线程把
``FAST_MODE`` 从 True 恢复成 False，就可能让正在运行的实盘策略在错误的时点
强平。这不是理论风险——``analyze()`` 同时被实盘下单（trend_range_trader）、
分析记录快照（app 路由）、行情看板（batch_trend_updater，且它自己开 3 线程）
和提醒邮件（本功能新增）调用。

所以：凡是「改写引擎全局 + 调引擎」的整段代码，必须在同一把锁下串行。

锁为什么放在本模块，而不是引擎模块里
------------------------------------
历史上 ``strategy_util`` 曾在详情请求中删除并重新导入引擎模块，产生多份实例。
现已改为复用模块并在 finally 恢复展示参数，以避免重复注册 Backtrader 类及
拆散缓存。锁仍必须挂在独立、稳定的模块上：包内与裸模块导入身份可能并存，
一把锁需要服务所有引擎实例，不能因取消详情重载而放宽串行约束。

为什么用 RLock 而不是 Lock
--------------------------
``analyze()`` 会被已经持锁的调用链再次进入（批量快照逐币调用、理论重放、
详情页取数）。可重入让同线程嵌套不自锁。代价是嵌套看起来"没问题"，因此
不要把长耗时操作（整轮批量）也塞进同一把锁里持着不放——见 ``pro3_exclusive``
的 timeout 约定。

为什么锁对象挂在 builtins 上，而不是只用模块变量
------------------------------------------------
本模块可能以两种身份被加载：包内 ``crypto.task.strategy_gate``（Flask 走这条）
与裸模块 ``strategy_gate``（``crypto/task`` 目录本身在 sys.path 上，独立脚本
走这条）。Python 视作两个不同的模块对象，各自执行一遍 → **两把互不可见的
锁**，而这把锁存在的全部理由就是"全进程只有一把"。挂到 builtins 上按固定
属性名取放，可保证无论哪种身份拿到的都是同一个 RLock。

已铺锁的入口（都在同一把锁下）
------------------------------
- ``strategy_adapter.DualPeriodStrategyAdapter.analyze()`` —— 实盘下单、
  分析记录快照、提醒邮件行情三条路都走它
- ``real_strategy_adapter.calculate_single_coin_data`` / ``calculate_dual_period_data``
  —— 前端逐币加载、趋势看板批量刷新
- ``strategy_util._get_single_period_detail`` / ``_get_dual_period_detail``
  —— 策略详情页（这两个会把 ``FAST_MODE`` 设成 **False**，对实盘最危险）
- ``trend_compare.TrendCompareService._replay`` —— 理论重放

有意未覆盖的入口
----------------
- ``real_strategy_adapter.calculate_strategy_data``：整轮多币扫描，一次持锁
  可达几十秒，会把实盘推后同样时长；且该入口已被逐币异步加载取代。
  它只把 ``FAST_MODE`` 置 True（与 analyze 同值），不会翻转实盘依赖的分支。
- ``crypto_analysis_batch.CryptoAnalysisBatch.calculate_dual_period``：
  文件头注明已被 ``DualPeriodStrategyAdapter`` 取代的历史实现。
- ``backtest/signal_engine.py``：离线回测脚本，不与实盘同进程。
- ``strategy_util._get_boll_detail``：只从 pro3 借只读取数辅助函数，
  不写 ``FAST_MODE`` / ``PRINT_*`` 全局。

失败取向
--------
拿不到锁时**由调用方决定降级方式**（装饰器的 ``on_busy``），本模块绝不静默
跳过：实盘宁愿本轮不出信号，也不能拿被污染的信号下单。
"""

import builtins
import functools
import logging
import threading
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ['PRO3_LOCK', 'StrategyBusy', 'pro3_exclusive', 'pro3_locked',
           'pro3_locked_now']

# 进程级唯一对象。命名故意直白：PRO3_LOCK 就是"策略引擎的独占使用权"。
# 跨身份共享的三个对象：锁（必须只有一把）、异常类（否则 A 身份抛的
# StrategyBusy，B 身份的 except 接不住）、logger（避免同一事件两个名字两处漂）。
def _shared(name: str, factory):
    attr = f'_crypto_pro3_gate_{name}'
    obj = getattr(builtins, attr, None)
    if obj is None:
        obj = factory()
        setattr(builtins, attr, obj)
    return obj


PRO3_LOCK = _shared('lock', threading.RLock)
logger = _shared('logger', lambda: logging.getLogger('crypto.task.strategy_gate'))


class StrategyBusy(RuntimeError):
    """在给定时间内没能拿到策略计算独占权"""


StrategyBusy = _shared('busy_cls', lambda: StrategyBusy)


@contextmanager
def pro3_exclusive(timeout: Optional[float] = None):
    """独占策略引擎计算段。

    Args:
        timeout: 最长等待秒数。``None`` 表示一直等到拿到（只用于确实不能失败的
            批处理入口）；给了数值而超时未拿到则抛 :class:`StrategyBusy`，
            由调用方降级（实盘跳过本轮 / 邮件标注"行情暂不可用"）。

    Raises:
        StrategyBusy: 超时未拿到锁。
    """
    if timeout is None:
        PRO3_LOCK.acquire()
    elif not PRO3_LOCK.acquire(timeout=timeout):
        raise StrategyBusy(f'策略计算通道被占用，等待 {timeout}s 仍未拿到锁')
    try:
        yield
    finally:
        PRO3_LOCK.release()


def pro3_locked_now() -> bool:
    """当前是否被占用（只用于诊断输出，不做判定依据）"""
    # 拿到马上放掉即表示"没人持锁"；同线程重入时计数 >0 仍算被占用
    if PRO3_LOCK.acquire(blocking=False):
        PRO3_LOCK.release()
        return False
    return True


def pro3_locked(default_timeout: Optional[float] = 120.0, on_busy: str = 'none'):
    """装饰器：整个函数体在策略计算独占权下执行。

    为什么用装饰器而不是函数内 ``with``：需要保护的代码段往往不是一个
    调用，而是"改全局 → 跑双周期引擎 → 提字段 → 再跑长周期引擎"整串，
    用 ``with`` 包就要重排几百行缩进（每一次重排都是一次新错误的机会）。

    Args:
        default_timeout: 默认最长等待秒数；``None`` = 一直等到拿到。
            调用方可用关键字参数 ``lock_timeout`` 逐次覆盖。
        on_busy: 等不到锁时的降级方式，必须按被装饰函数**自己的返回值契约**
            选，选错会把"繁忙"伪装成正常结果：
            ``'none'``  返回 None —— 适用于契约本来就是 Optional[Dict] 的
                        ``analyze()``，它的调用方已经把 None 当作"本轮不出
                        信号"处理。
            ``'raise'`` 抛 StrategyBusy —— 适用于返回值是定长元组/字典、
                        调用方靠 ``except`` 记录失败的函数（详情页、理论重放、
                        逐币取数）。这些地方返回 None 会让上层在解包或
                        ``.get`` 处炸出误导性的错误文案。
    """
    if on_busy not in ('none', 'raise'):
        raise ValueError(f'on_busy 只接受 none/raise，收到 {on_busy!r}')

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            timeout = kwargs.pop('lock_timeout', default_timeout)
            try:
                with pro3_exclusive(timeout=timeout):
                    return fn(*args, **kwargs)
            except StrategyBusy as e:
                logger.warning(f'[StrategyGate] {fn.__qualname__} 让位：{e}')
                if on_busy == 'raise':
                    raise
                return None
        return wrapper
    return deco
