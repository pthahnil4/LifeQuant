#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Pro3 双周期策略适配器 — 定时任务专用
=====================================

直接调用 crypto/strategy/pro3_dualtimeframe.py 的核心策略引擎，
确保实盘交易信号与回测引擎 100% 一致。

本文件不包含任何策略逻辑实现，仅做：
1. 路径管理与模块导入
2. 调用核心策略引擎 get_latest_data_dual()
3. 将结果转换为 trend_range_trader.py 所需的字典格式

作者：AI Assistant
创建时间：2026-07-08
"""

import sys
import os
import datetime
import logging
from typing import Dict, Optional, List, Tuple

try:  # 包内导入（Flask）/ 直接 script 导入（task 目录在 sys.path）
    from .strategy_gate import pro3_locked
except ImportError:  # pragma: no cover
    from strategy_gate import pro3_locked

logger = logging.getLogger(__name__)


class DualPeriodStrategyAdapter:
    """
    Pro3 双周期策略适配器（直接调用核心策略引擎）

    替代 CryptoAnalysisBatch.calculate_dual_period()，
    直接调用 pro3_dualtimeframe.get_latest_data_dual() 获取信号，
    确保与 backtrader 回测引擎的信号逻辑完全一致。

    信号来源：
    - 短周期：pro3_singletimeframe.CoreStrategy.next() 的 modify_flag
    - 长周期：pro3_singletimeframe.LongDirStrategy.next() 的 modify_flag
    - 双周期过滤：CoreStrategy._dual_period_trade_logic()

    方向映射：
    - modify_flag='rise' → direction='long'（做多）
    - modify_flag='fall' → direction='short'（做空）
    """

    def __init__(self):
        self._initialized = False
        self._single = None
        self._dual = None

    def _ensure_initialized(self):
        """延迟初始化：确保 strategy 模块正确加载"""
        if self._initialized:
            return

        # 路径设置：crypto/strategy/ 和 crypto/
        strategy_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'strategy'
        )
        crypto_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for d in (strategy_dir, crypto_dir):
            if d not in sys.path:
                sys.path.insert(0, d)

        import pro3_singletimeframe as _single
        import pro3_dualtimeframe as _dual

        self._single = _single
        self._dual = _dual
        self._initialized = True

    @pro3_locked(default_timeout=120.0)
    def analyze(self, inst_id: str, short_period: str, long_period: str,
                boll_period: int = 20, boll_dev: float = 2.0,
                signal_algo: str = None, entry_price_type: str = None,
                exit_price_type: str = None,
                lock_timeout: float = 120.0) -> Optional[Dict]:
        """
        执行双周期策略分析

        直接调用 pro3_dualtimeframe.get_latest_data_dual() 获取交易信号，
        信号逻辑与 backtrader 回测引擎完全一致。

        Args:
            inst_id: 合约ID (e.g. 'NEAR-USDT-SWAP')
            short_period: 短周期 (e.g. '1m', '5m', '15m')
            long_period: 长周期 (e.g. '15m', '1H', '4H')
            boll_period: 短周期布林带周期（默认 20）
            boll_dev: 短周期布林带标准差倍数（默认 2.0）
            signal_algo: 短周期信号算法 'diff'(平滑差,灵敏,默认) / 'hybrid'(状态机,稳定)
            entry_price_type: 开仓价格取值 'close'(默认) / 'open'
            exit_price_type: 平仓价格取值 'open'(默认) / 'close'
            lock_timeout: 等策略计算独占权的最长秒数。本参数由 @pro3_locked
                装饰器消费（不传则用默认 120s）。拿不到锁时 analyze 返回 None，
                调用方本来就是「None 则本轮不出信号」，不会拿着被污染的
                结果下单（并发原因见 strategy_gate 模块文档）。

        Returns:
            Dict: 包含 direction、long_direction 等字段的分析结果，
                  格式与 trend_range_trader.py 的 analyze_and_trade_real() 完全兼容。
                  失败或拿不到策略计算锁时返回 None。
        """
        try:
            self._ensure_initialized()

            # 本方法整体已在策略计算独占权下执行（@pro3_locked）：除了下面的
            # 双周期引擎，后面还要用长周期引擎，两段都吃同一批模块全局。

            # 保存并抑制策略模块的输出
            old_fast = self._single.FAST_MODE
            old_print_orig = self._single.PRINT_ORIGINAL_OUTPUT
            old_print_market = self._single.PRINT_MARKET
            old_print_ops = self._single.PRINT_TRADE_OPS
            old_print_records = self._single.PRINT_TRADE_RECORDS

            self._single.FAST_MODE = True
            self._single.PRINT_ORIGINAL_OUTPUT = 0
            self._single.PRINT_MARKET = 0
            self._single.PRINT_TRADE_OPS = 0
            self._single.PRINT_TRADE_RECORDS = 0

            try:
                latest_data, last_trade_data, atr_value, long_direction, df_short, df_long = \
                    self._dual.get_latest_data_dual(
                        inst_id, short_period, long_period,
                        entry_price_type=entry_price_type,
                        exit_price_type=exit_price_type,
                        signal_algo=signal_algo)
            finally:
                # 恢复全局变量
                self._single.FAST_MODE = old_fast
                self._single.PRINT_ORIGINAL_OUTPUT = old_print_orig
                self._single.PRINT_MARKET = old_print_market
                self._single.PRINT_TRADE_OPS = old_print_ops
                self._single.PRINT_TRADE_RECORDS = old_print_records

            # === 字段提取 ===
            price = float(latest_data.get('close', 0))
            modify_flag = latest_data.get('MODIFY_FLAG', None)

            # 方向映射：rise → long, fall → short
            direction = 'long' if modify_flag == 'rise' else 'short'

            # === 短周期前序方向提取（用于信号稳定性确认） ===
            short_prev_direction = None    # 上一个时段的短周期方向
            short_prev_prev_direction = None  # 上上个时段的短周期方向
            try:
                if df_short is not None and 'MODIFY_FLAG' in df_short.columns:
                    n = len(df_short)
                    if n >= 2:
                        prev_flag = df_short.iloc[-2].get('MODIFY_FLAG', None)
                        short_prev_direction = 'long' if prev_flag == 'rise' else ('short' if prev_flag == 'fall' else None)
                    if n >= 3:
                        prev_prev_flag = df_short.iloc[-3].get('MODIFY_FLAG', None)
                        short_prev_prev_direction = 'long' if prev_prev_flag == 'rise' else ('short' if prev_prev_flag == 'fall' else None)
            except Exception:
                pass  # 非关键信息，失败不影响主流程

            # 短周期交易信息（来自双周期策略引擎）
            short_trade_price = 0.0
            short_trade_time = None
            if last_trade_data is not None:
                short_trade_price = float(last_trade_data.get('TRADE_PRICE', 0) or 0)
                short_trade_time = last_trade_data.get('TRADE_TIME', None)
            if short_trade_price == 0.0:
                short_trade_price = price

            # ATR
            import math
            current_atr = float(atr_value) if atr_value and not math.isnan(atr_value) else 0.0
            atr_percentage = (current_atr / price * 100) if price > 0 else 0.0

            # 短周期当前持仓盈亏（根据当前方向和最新价格计算未实现盈亏）
            short_profit = 0.0
            if short_trade_price > 0:
                short_flag = latest_data.get('MODIFY_FLAG', None)
                if short_flag == 'rise':
                    short_profit = ((price / short_trade_price) - 1) * 100 * 10.0
                elif short_flag == 'fall':
                    short_profit = (1 - (price / short_trade_price)) * 100 * 10.0

            # 长周期方向标准化
            long_dir_str = None
            if long_direction is not None:
                s = str(long_direction)
                if s == 'rise':
                    long_dir_str = 'long'
                elif s == 'fall':
                    long_dir_str = 'short'
                else:
                    long_dir_str = s

            # === 长周期前序方向提取（使用上一时段方向避免震荡期频繁反转） ===
            long_prev_dir_str = None
            try:
                if df_long is not None and 'LONG_DIRECTION' in df_long.columns:
                    long_valid = df_long['LONG_DIRECTION'].dropna()
                    if len(long_valid) >= 2:
                        prev_long_raw = long_valid.iloc[-2]
                        s = str(prev_long_raw)
                        if s == 'rise':
                            long_prev_dir_str = 'long'
                        elif s == 'fall':
                            long_prev_dir_str = 'short'
                        else:
                            long_prev_dir_str = s
            except Exception:
                pass  # 非关键信息，失败不影响主流程

            # === 长周期交易信息（独立运行长周期策略引擎） ===
            # 注意：这段跑在上面那次引擎的 finally **之后**，PRINT_* 与 FAST_MODE
            # 已经恢复成进入时的值，于是一执行就往日志里灌 5 段“交易统计”
            #（每币 ~30 行）。这里只压掉打印；FAST_MODE 故意不动——它决定
            # stop() 是否走“期末强平”分支，改动会连带改变实盘依赖的
            # long_trade_price / long_profit，不属本次串行化改动的范围。
            long_trade_price = 0.0
            long_trade_time = None
            long_profit = 0.0
            _saved_print = {k: getattr(self._single, k, 0) for k in
                            ('PRINT_ORIGINAL_OUTPUT', 'PRINT_MARKET',
                             'PRINT_TRADE_OPS', 'PRINT_TRADE_RECORDS')}
            try:
                self._single.PRINT_ORIGINAL_OUTPUT = 0
                self._single.PRINT_MARKET = 0
                self._single.PRINT_TRADE_OPS = 0
                self._single.PRINT_TRADE_RECORDS = 0
                df_long_run = self._single._fetch_kline_data(inst_id, long_period)
                df_long_run = self._single.calculate_adx(df_long_run, period=14)
                self._single._init_df_columns(df_long_run)
                long_strat = self._single._run_strategy(df_long_run, long_direction=None, is_dual_period=False)
                long_latest = df_long_run.iloc[-1]
                long_price = float(long_latest.get('close', 0))
                long_last_trade = self._single._build_last_trade_data(long_strat, df_long_run)
                if long_last_trade is not None:
                    long_trade_price = float(long_last_trade.get('TRADE_PRICE', 0) or 0)
                    long_trade_time = long_last_trade.get('TRADE_TIME', None)
                if long_trade_price == 0.0:
                    long_trade_price = long_price
                # 长周期当前持仓盈亏（根据当前方向和最新价格计算未实现盈亏）
                long_flag = long_latest.get('MODIFY_FLAG', None)
                if long_trade_price > 0:
                    if long_flag == 'rise':
                        long_profit = ((long_price / long_trade_price) - 1) * 100 * 10.0
                    elif long_flag == 'fall':
                        long_profit = (1 - (long_price / long_trade_price)) * 100 * 10.0
            except Exception:
                pass  # 长周期信息非关键，失败不影响主流程
            finally:
                for _k, _v in _saved_print.items():
                    setattr(self._single, _k, _v)

            # === 短周期布林带 + 反转 bar 价格锤点（供多点位分批限价交易使用） ===
            # 均从 df_short 直接计算，不修改核心策略引擎
            boll_upper = 0.0
            boll_middle = 0.0
            boll_lower = 0.0
            reversal_open = 0.0
            reversal_close = 0.0
            try:
                if df_short is not None and 'close' in df_short.columns:
                    n = len(df_short)
                    # 布林带（短周期，default period=20, dev=2.0）
                    if n >= boll_period:
                        closes = df_short['close'].astype(float)
                        mid = closes.rolling(boll_period).mean()
                        std = closes.rolling(boll_period).std(ddof=0)
                        m = float(mid.iloc[-1])
                        s = float(std.iloc[-1])
                        if not math.isnan(m) and not math.isnan(s):
                            boll_middle = m
                            boll_upper = m + boll_dev * s
                            boll_lower = m - boll_dev * s
                    # 反转 bar = 上一个已收线时段（窗口确认中信号翻转那根 bar）
                    if n >= 2:
                        rev = df_short.iloc[-2]
                        if 'open' in df_short.columns:
                            reversal_open = float(rev.get('open', 0) or 0)
                        reversal_close = float(rev.get('close', 0) or 0)
            except Exception:
                pass  # 非关键信息，失败不影响主流程

            # === 短周期 MACD 柱与收盘价序列（供止盈引擎动能衰竭/背离判定使用） ===
            macd_hist_series = []
            close_series = []
            try:
                if df_short is not None:
                    if 'MACD' in df_short.columns:
                        hs = df_short['MACD'].dropna().astype(float)
                        macd_hist_series = [float(x) for x in hs.tail(40).tolist()]
                    if 'close' in df_short.columns:
                        cs = df_short['close'].dropna().astype(float)
                        close_series = [float(x) for x in cs.tail(40).tolist()]
            except Exception:
                pass  # 非关键信息，失败不影响主流程

            # 日志
            dir_cn = "多" if direction == 'long' else "空"
            long_dir_cn = "多" if long_dir_str == 'long' else ("空" if long_dir_str == 'short' else "--")
            prev_dir_cn = "多" if short_prev_direction == 'long' else ("空" if short_prev_direction == 'short' else "--")
            prev_prev_dir_cn = "多" if short_prev_prev_direction == 'long' else ("空" if short_prev_prev_direction == 'short' else "--")
            long_prev_cn = "多" if long_prev_dir_str == 'long' else ("空" if long_prev_dir_str == 'short' else "--")
            print(f"[Pro3] {inst_id} {short_period}/{long_period} 短={dir_cn} 长={long_dir_cn} "
                  f"短前={prev_dir_cn} 短短前={prev_prev_dir_cn} 长前={long_prev_cn} "
                  f"价格={short_trade_price:.4f}")

            return {
                'currency_code': inst_id,
                'time_period': short_period,
                'last_price': price,
                'latest_trade_price': short_trade_price,
                'direction': direction,
                'signal_confirmed': (modify_flag is not None and modify_flag in ('rise', 'fall')),
                'trade_price': short_trade_price,
                'trade_time': short_trade_time,
                'current_profit': short_profit,
                'win_rate': 0,
                'total_trades': 0,
                'compound_return': 1.0,
                'signal_strength': 0,
                'atr_value': current_atr,
                'atr_percentage': atr_percentage,
                'price_volatility': current_atr,
                'strategy_version': 'Pro3_Dual_Direct',
                'long_direction': long_dir_str,
                # 信号稳定性确认所需的历史方向
                'short_prev_direction': short_prev_direction,
                'short_prev_prev_direction': short_prev_prev_direction,
                'long_prev_direction': long_prev_dir_str,
                # 短周期交易详情
                'short_trade_price': short_trade_price,
                'short_trade_time': short_trade_time,
                'short_profit': short_profit,
                # 长周期交易详情
                'long_trade_price': long_trade_price,
                'long_trade_time': long_trade_time,
                'long_profit': long_profit,
                # 多点位分批限价交易：短周期布林带 + 反转 bar 价格锤点
                'boll_upper': boll_upper,
                'boll_middle': boll_middle,
                'boll_lower': boll_lower,
                'reversal_open': reversal_open,
                'reversal_close': reversal_close,
                # 止盈引擎所需的短周期指标序列
                'macd_hist_series': macd_hist_series,
                'close_series': close_series,
            }

        except Exception as e:
            print(f"[Pro3] {inst_id} {short_period}/{long_period} 策略分析失败: {e}")
            import traceback
            traceback.print_exc()
            return None


# =============================================================================
# 提醒邮件用的「当前策略计算行情」
# =============================================================================

_DIR_CN = {'long': '上涨', 'short': '下跌'}


def snapshot_item(cur: Dict, analysis: Dict, ts: str) -> Dict:
    """把一个币种的交易配置 + analyze() 结果拼成一行行情。

    字段与 app.py 的 /api/task/analysis/snapshot(_batch) 保持一致，前端表格
    与邮件表格才能共用一套语义；改了这边要同步那边。
    """
    return {
        'instId': str(cur.get('instId') or '').strip(),
        'ts': ts,
        'price': analysis.get('last_price', 0),
        'short_period': cur.get('short_period', '5m'),
        'long_period': cur.get('long_period', '4H'),
        'short_dir': analysis.get('direction') or '',
        'long_dir': analysis.get('long_direction') or '',
        'long_dir_prev': analysis.get('long_prev_direction') or '',
        'atr_pct': analysis.get('atr_percentage', 0),
        'boll_upper': analysis.get('boll_upper', 0),
        'boll_middle': analysis.get('boll_middle', 0),
        'boll_lower': analysis.get('boll_lower', 0),
    }


def dir_cn(dir_key: str) -> str:
    """long/short → 上涨/下跌（邮件正文给人看的字面）"""
    return _DIR_CN.get(str(dir_key or ''), str(dir_key or '') or '—')


def market_snapshot(currencies: List[Dict], lock_timeout: float = 8.0,
                    budget_sec: float = 25.0) -> Tuple[List[Dict], List[str], str]:
    """为提醒邮件现取一次全量跟踪币种的策略计算行情。

    为何不用现成缓存（crypto_coins 表 / batch_trend_updater 产出）：那是
    前端手工点出来的快照，实测跟踪的 5 个币只覆盖 2 个、且已 29 小时没更新；
    拿 29 小时前的方向冒充「当前行情」发给用户，比不发更糟。

    串行不并发：analyze() 已在策略计算全局锁下执行，并发只会在锁上排队，
    还多占线程；串行总耗时与前端逐币刷新同量级。

    Args:
        currencies: 交易配置里的 currencies 数组（带策略参数，不是只有 instId）
        lock_timeout: 单币最多等多久拿策略锁（实盘正在算就让位）
        budget_sec: 本函数总时长预算，超预算就剩下币全部标为未取到

    Returns:
        (items, failed, note)：成功行 / 失败币种列表 / 对人说明。
        items 为空但 failed 非空时，note 会说清原因，调用方不能把空表
        当成「没有行情」静默发出去。
    """
    items: List[Dict] = []
    failed: List[str] = []
    note = ''
    curs = [c for c in (currencies or []) if str(c.get('instId') or '').strip()]
    if not curs:
        return items, failed, '交易配置里没有币种'

    started = datetime.datetime.now()
    adapter = DualPeriodStrategyAdapter()
    for cur in curs:
        inst_id = str(cur.get('instId') or '').strip()
        if (datetime.datetime.now() - started).total_seconds() > budget_sec:
            failed.append(inst_id)
            note = f'取价超时预算 {int(budget_sec)}s，其余 {len(curs) - len(items) - len(failed) + 1} 个未取'
            break
        rcfg = cur.get('range_position') or {}
        try:
            analysis = adapter.analyze(
                inst_id, cur.get('short_period', '5m'), cur.get('long_period', '4H'),
                boll_period=int(rcfg.get('boll_period', 20) or 20),
                boll_dev=float(rcfg.get('boll_dev', 2.0) or 2.0),
                signal_algo=cur.get('signal_algo'),
                entry_price_type=cur.get('entry_price_type', 'close'),
                exit_price_type=cur.get('exit_price_type', 'open'),
                lock_timeout=lock_timeout)
        except Exception as e:                       # 单币失败不拖垮整张表
            logger.warning(f'[StrategyGate] {inst_id} 行情取数异常: {e}')
            analysis = None
        if analysis:
            items.append(snapshot_item(
                cur, analysis, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        else:
            failed.append(inst_id)
    if failed and not note:
        note = ('策略计算通道被占用（实盘/看板正在算），本轮不附行情' if len(failed) == len(curs)
                else f'{len(failed)} 个币种未取到')
    return items, failed, note
