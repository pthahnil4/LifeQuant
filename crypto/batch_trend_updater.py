#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
"""
加密货币批量多周期趋势分析脚本
==============================
遍历 crypto_coins.csv 中的所有加密货币，对每个币种分别执行
15分钟/1小时/4小时/日线的 Pro3 策略计算，提取趋势方向、交易价格、
交易时间和当前盈亏百分比，结果批量写入 CSV 文件。

可独立运行，也可通过 Flask 作为后台任务调用。
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import csv
import datetime
import logging
import numpy as np
import os
import sys
import threading
import time
import tempfile
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

# 确保可以从 crypto 目录导入本地模块
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

logger = logging.getLogger(__name__)

# 只读接口限频/退避（问题#8）：本模块用线程池并发扫全市场 K 线，
# 是最容易打出 50011 的地方。导入失败时置 None，_rl() 直通不限频。
try:
    from task.utils.okx_ratelimit import limited as _rl_limited
except ImportError:
    try:
        from crypto.task.utils.okx_ratelimit import limited as _rl_limited
    except ImportError:
        _rl_limited = None


def _rl(group, func, *args, **kwargs):
    """只读 OKX 接口的统一出口：节流 + 50011 退避；限频模块缺失时直通。"""
    return _rl_limited(group, func, *args, **kwargs) if _rl_limited else func(*args, **kwargs)

CSV_PATH = os.path.join(BASE_DIR, 'crypto_coins.csv')

# 全币种行情已迁移 MySQL（迁移批次7b，crypto_coins 表）：
# 读 DB 优先、无数据/不可用时回退 CSV 文件；写 DB 主存 + CSV 双写（文件保留为兜底数据源）。
# 双模式导入：Flask 包内（cryptoTrade 根在 sys.path）/ 独立脚本（仅 crypto 目录）
try:
    from crypto.database import session_scope as _db_session_scope
    from crypto import market_data_repo as _market_repo
except ImportError:
    _db_session_scope = None
    _market_repo = None

# 预热阶段并发 worker 数：仅用于"锁外并发下载K线"，真实 QPS 由 market_candles
# 令牌桶（~5/s）封顶，worker 只是掩盖单请求网络延迟，不冲速率。5~6 足够把 220
# 个单页请求在 ~45s 内下完，同时把峰值并发压在 OKX IP 限流线以下很多。
_BATCH_WORKERS = 5

# 整表落库降频：每完成 N 个币种或任务收尾各写一次（原来每 5 个写一次，55 币→11 次）。
_SAVE_EVERY = 20

# 批量分析覆盖的周期（按时间尺度升序）：展示顺序与前端一致。
# 新增周期只需在此追加，并在 CSV/DB 列与 run() 写入处同步补齐对应字段。
_BATCH_BARS = ('15m', '1H', '4H', '1D')

# 共享 MarketAPI 客户端：okx SDK 基于 httpx.Client（http2=True），复用同一 HTTP/2
# 长连接可避免高频新建连接被 OKX 服务端主动断开（Server disconnected）导致的
# 请求失败与长时间挂起；httpx.Client 线程安全，可跨线程共享。
_shared_market_api = None
_market_api_lock = threading.Lock()

# SAR K线请求的显式超时（SDK 默认 5 秒偏短，弱网下给足余量）
_SAR_API_TIMEOUT = dict(connect=15.0, read=20.0, write=15.0, pool=15.0)

# SAR K线缓存：(instId, bar) -> (时间戳, K线列表)；指数K线几乎不变，
# 批量任务之间（重启后 5 分钟内）复用，避免重复下载。
_sar_kline_cache = {}
_sar_kline_cache_lock = threading.Lock()
_SAR_KLINE_CACHE_TTL = 300


def _reset_shared_market_api():
    """原子替换共享客户端引用（不主动 close 旧实例）。

    直接 close 共享客户端会让正在使用它的其他线程（监控台轮询/并发 worker）
    撞上 'Cannot send a request, as the client has been closed'；
    旧实例无引用后由 httpx 自动回收连接，因此只需替换引用即可安全重建。
    """
    global _shared_market_api
    with _market_api_lock:
        _shared_market_api = None


def _get_shared_market_api():
    """获取共享的 MarketAPI 客户端（首次创建时设置显式超时）"""
    global _shared_market_api
    with _market_api_lock:
        if _shared_market_api is not None:
            return _shared_market_api
        from api_config import get_api_config
        import okx.MarketData as MarketData
        import httpx
        config = get_api_config()
        api = MarketData.MarketAPI(flag=config.get('flag', '0'))
        try:
            api.timeout = httpx.Timeout(**_SAR_API_TIMEOUT)
        except Exception:
            pass
        _shared_market_api = api
        return api


# ====================================================================
#  进度状态管理（模块级，供 Flask 查询）
# ====================================================================

# 当前 Web 入口是单进程多线程；状态快照与日志共用短锁，任务执行另用互斥锁。
_batch_state_lock = threading.RLock()
_batch_run_lock = threading.Lock()


class BatchAlreadyRunning(RuntimeError):
    """已有批量任务占用执行通道。"""


class _ProgressState:
    """线程安全的进度状态"""

    def __init__(self):
        self._lock = _batch_state_lock
        self.reset()

    def reset(self):
        with self._lock:
            self.status = "idle"       # idle / running / completed / error
            self.run_id = ""
            self.phase = "idle"
            self.coin_total = 0
            self.total = 0
            self.current = 0
            self.success = 0
            self.error = 0
            self.current_symbol = ""
            self.message = ""
            self.start_time = None
            self.end_time = None

    def start(self, total: int):
        with self._lock:
            self.status = "running"
            self.run_id = uuid.uuid4().hex
            self.phase = "starting"
            self.coin_total = 0
            self.total = total
            self.current = 0
            self.success = 0
            self.error = 0
            self.current_symbol = ""
            self.message = "开始批量分析..."
            self.start_time = datetime.datetime.now()
            self.end_time = None

    def configure(self, total: int, coin_total: int):
        """初始化完成后设置工作量，不重置任务身份、日志或起始时间。"""
        with self._lock:
            self.total = total
            self.coin_total = coin_total
            self.phase = "warmup"

    def update(self, current: float, symbol: str = "",
               success: int = None, error: int = None, message: str = None,
               phase: str = None):
        with self._lock:
            self.current = min(self.total, max(self.current, current))
            if phase:
                self.phase = phase
            if symbol:
                self.current_symbol = symbol
            if success is not None:
                self.success = success
            if error is not None:
                self.error = error
            if message:
                self.message = message

    def finish(self, message: str = None):
        with self._lock:
            self.status = "completed"
            self.phase = "completed"
            self.current = self.total  # 保证进度条最终到 100%
            self.end_time = datetime.datetime.now()
            if message:
                self.message = message
            else:
                elapsed = (self.end_time - self.start_time).total_seconds() if self.start_time else 0
                self.message = (
                    "批量分析完成！成功 %d/%d，失败 %d，耗时 %.1f 秒"
                    % (self.success, self.coin_total, self.error, elapsed)
                )

    def set_error(self, message: str):
        with self._lock:
            self.status = "error"
            self.phase = "error"
            self.end_time = datetime.datetime.now()
            self.message = message

    def to_dict(self) -> Dict:
        with self._lock:
            elapsed = 0
            if self.start_time:
                if self.end_time:
                    elapsed = (self.end_time - self.start_time).total_seconds()
                else:
                    elapsed = (datetime.datetime.now() - self.start_time).total_seconds()
            return {
                "status": self.status,
                "run_id": self.run_id,
                "phase": self.phase,
                "coin_total": self.coin_total,
                "periods": list(_BATCH_BARS),
                "total": self.total,
                "current": self.current,
                "success": self.success,
                "error": self.error,
                "current_symbol": self.current_symbol,
                "message": self.message,
                "elapsed_seconds": round(elapsed, 1),
                "progress_pct": (100 if self.status == "completed" else
                                 min(99.9, round(self.current / self.total * 100, 1)))
                if self.total > 0 else 0,
            }


# 全局进度实例
_progress = _ProgressState()


def get_progress() -> Dict:
    """获取当前进度（供外部调用）"""
    return _progress.to_dict()


# ====================================================================
#  批量运行日志缓冲（供 SSE 实时推送前端）
# ====================================================================
_BATCH_LOG_LIMIT = 5000
_batch_log_lines = deque(maxlen=_BATCH_LOG_LIMIT)
_batch_log_lock = _batch_state_lock
_batch_log_seq = 0


def _batch_log(msg: str, level: str = 'info'):
    """追加一行结构化日志，供前端 SSE 消费。"""
    global _batch_log_seq
    with _batch_log_lock:
        _batch_log_seq += 1
        _batch_log_lines.append({
            'seq': _batch_log_seq,
            'run_id': _progress.run_id,
            'ts': datetime.datetime.now().strftime('%H:%M:%S'),
            'level': level,
            'msg': msg,
        })


def get_batch_logs_since(cursor: int):
    """返回 cursor 之后的新日志行与新游标（供 SSE 增量消费）。"""
    snapshot = get_batch_snapshot(cursor)
    return snapshot['logs'], snapshot['cursor']


def clear_batch_logs():
    """新一轮开始前清空日志缓冲。"""
    global _batch_log_seq
    with _batch_log_lock:
        _batch_log_lines.clear()
        _batch_log_seq = 0


def get_batch_snapshot(cursor: int = 0, run_id: str = None) -> Dict:
    """进度与日志原子快照；终态一定包含尾日志，跨轮次/进程重启显式重置游标。"""
    with _batch_state_lock:
        p = _progress.to_dict()
        reset = (run_id is not None and run_id != p['run_id']) or cursor > _batch_log_seq
        if reset:
            cursor = 0
        oldest = _batch_log_lines[0]['seq'] if _batch_log_lines else _batch_log_seq + 1
        p.update({
            'logs': [dict(line) for line in _batch_log_lines if line['seq'] > cursor],
            'cursor': _batch_log_seq,
            'reset': reset,
            'dropped_logs': max(0, oldest - 1),
            'log_limit': _BATCH_LOG_LIMIT,
        })
        return p


# ====================================================================
#  趋势信号提取
# ====================================================================

def _extract_trend_info(coin_result: Dict) -> Dict:
    """从 calculate_single_coin_data 返回中提取趋势+技术指标信息

    Parameters
    ----------
    coin_result : dict
        calculate_single_coin_data 返回的完整结果

    Returns
    -------
    dict
        {
            'trend': '上涨'/'下跌'/'观望'/'错误',
            'trend_flag': 'rise'/'fall'/'wait'/'error',
            'trade_price': float or '',
            'trade_time': str or '',
            'profit_pct': float or '',
            'close_price': float or '',
            'macd_hist': float or '',
            'dif': float or '',
            'adx': float or '',
            'atr_pct': float or '',
        }
    """
    if coin_result is None:
        return {
            'trend': '错误',
            'trend_flag': 'error',
            'trade_price': '',
            'trade_time': '',
            'profit_pct': '',
            'close_price': '',
            'macd_hist': '',
            'dif': '',
            'adx': '',
            'atr_pct': '',
        }

    analysis = coin_result.get('analysis', {})
    modify_flag = analysis.get('modify_flag', 'wait')

    if modify_flag == 'rise':
        trend = '上涨'
    elif modify_flag == 'fall':
        trend = '下跌'
    else:
        trend = '观望'
        modify_flag = 'wait'

    trade_info = coin_result.get('trade_info', {})
    trade_price = trade_info.get('last_trade_price', 0)
    trade_time = trade_info.get('trade_time', '')
    profit_pct = trade_info.get('current_profit', 0)

    # 当前收盘价（用于 ATR% 计算和 SAR 颜色判断）
    close_price = float(coin_result.get('price', 0)) if coin_result.get('price') else 0

    indicators = coin_result.get('indicators', {})
    macd_data = indicators.get('macd', {})
    adx_data = indicators.get('adx', {})

    macd_hist = macd_data.get('histogram', 0)
    dif_val = macd_data.get('dif', 0)
    adx_val = adx_data.get('adx', 0)
    atr_val = indicators.get('atr', 0)

    # ATR% = (ATR值 / 收盘价) * 100 * 10（10倍杠杆）
    atr_pct = ''
    if atr_val is not None and atr_val != '' and close_price > 0:
        try:
            atr_pct = round(float(atr_val) / close_price * 100 * 10, 4)
        except (ValueError, ZeroDivisionError):
            atr_pct = ''

    return {
        'trend': trend,
        'trend_flag': modify_flag,
        'trade_price': round(float(trade_price), 4) if trade_price and float(trade_price) != 0 else '',
        'trade_time': str(trade_time) if trade_time else '',
        'profit_pct': round(float(profit_pct), 2) if profit_pct is not None and profit_pct != '' else '',
        'close_price': round(close_price, 4) if close_price > 0 else '',
        'macd_hist': round(float(macd_hist), 4) if macd_hist is not None and macd_hist != '' else '',
        'dif': round(float(dif_val), 4) if dif_val is not None and dif_val != '' else '',
        'adx': round(float(adx_val), 2) if adx_val is not None and adx_val != '' else '',
        'atr_pct': atr_pct,
    }


# ====================================================================
#  Kaufman 效率系数（ER）计算
# ====================================================================

def _compute_er_from_klines(klines: List[List], window: int = 8) -> float:
    """从 K 线收盘价计算 Kaufman 效率系数 ER = |净变动| / Σ|逐根变动|

    口径与 market_scanner.kaufman_efficiency_ratio 一致：取最近 window 根
    收盘价（含最新一根），返回 0~1，越接近 1 越单边（趋势强），越接近 0 越震荡。

    Parameters
    ----------
    klines : list
        OKX API 返回的K线数据（最新在前），每条为 [ts, o, h, l, c, vol, ...]
    window : int
        参与计算的收盘价根数（默认 8，即最近 7 段变动）
    """
    if not klines or len(klines) < 2:
        return ''
    try:
        # OKX 返回最新在前 → 反转为旧→新，再取最近 window 根收盘价
        closes = [float(k[4]) for k in reversed(klines) if float(k[4]) > 0]
        closes = closes[-min(len(closes), window):]
        if len(closes) < 2:
            return ''
        net = abs(closes[-1] - closes[0])
        noise = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
        if noise == 0:
            return ''
        return round(net / noise, 4)
    except Exception as e:
        logger.warning("ER计算失败: %s", str(e))
        return ''


# ====================================================================
#  SAR 抛物线指标计算
# ====================================================================

def _compute_sar_from_klines(klines: List[List]) -> Dict:
    """从 OHLC K线数据计算最新 SAR 值和趋势方向

    基于标准抛物线 SAR 算法 (af_start=0.02, af_step=0.02, af_max=0.20)

    Parameters
    ----------
    klines : list
        OKX API 返回的K线数据，每条为 [ts, o, h, l, c, vol, ...]

    Returns
    -------
    dict
        {'sar': float, 'sar_trend': '↑'|'↓'|'--'}
    """
    if not klines or len(klines) < 2:
        return {'sar': '', 'sar_trend': '--'}

    try:
        # OKX 返回的 K 线为“最新在前”（降序），而抛物线 SAR 是有方向的递推指标，
        # 必须按时间正序（最旧在前）逐根推进，故先反转；反转后 sar[-1] 即最新值。
        klines = list(reversed(klines))
        highs = np.array([float(k[2]) for k in klines])
        lows = np.array([float(k[3]) for k in klines])
        n = len(highs)

        sar = np.zeros(n)
        ep = np.zeros(n)  # extreme point
        af = np.zeros(n)  # acceleration factor
        trend = np.ones(n, dtype=int)  # 1=uptrend, -1=downtrend

        # 初始化
        sar[0] = lows[0]
        ep[0] = highs[0]
        af[0] = 0.02
        trend[0] = 1

        for i in range(1, n):
            # 计算 SAR
            sar[i] = sar[i - 1] + af[i - 1] * (ep[i - 1] - sar[i - 1])

            if trend[i - 1] == 1:  # 上升趋势
                # SAR 不能高于前两根K线的最低点
                low_min = min(lows[i - 1], lows[i]) if i >= 1 else lows[i]
                sar[i] = min(sar[i], low_min)

                if lows[i] < sar[i]:  # 趋势反转
                    trend[i] = -1
                    sar[i] = max(highs[i], highs[i - 1]) if i >= 1 else highs[i]
                    ep[i] = lows[i]
                    af[i] = 0.02
                else:
                    trend[i] = 1
                    if highs[i] > ep[i - 1]:
                        ep[i] = highs[i]
                        af[i] = min(af[i - 1] + 0.02, 0.20)
                    else:
                        ep[i] = ep[i - 1]
                        af[i] = af[i - 1]
            else:  # 下降趋势
                # SAR 不能低于前两根K线的最高点
                high_max = max(highs[i - 1], highs[i]) if i >= 1 else highs[i]
                sar[i] = max(sar[i], high_max)

                if highs[i] > sar[i]:  # 趋势反转
                    trend[i] = 1
                    sar[i] = min(lows[i], lows[i - 1]) if i >= 1 else lows[i]
                    ep[i] = highs[i]
                    af[i] = 0.02
                else:
                    trend[i] = -1
                    if lows[i] < ep[i - 1]:
                        ep[i] = lows[i]
                        af[i] = min(af[i - 1] + 0.02, 0.20)
                    else:
                        ep[i] = ep[i - 1]
                        af[i] = af[i - 1]

        last_sar = round(float(sar[-1]), 4)
        last_trend = '↑' if trend[-1] == 1 else '↓'
        return {'sar': last_sar, 'sar_trend': last_trend}

    except Exception as e:
        logger.warning("SAR计算失败: %s", str(e))
        return {'sar': '', 'sar_trend': '--'}


def _df_to_klines_newest_first(df) -> List[List]:
    """把 _fetch_kline_data 返回的 df（时间升序，列 open/high/low/close）
    转成 `_compute_*_from_klines` 期望的"最新在前"K线行格式 [ts, o, h, l, c]。
    SAR/ER 只用 h/l/c，ts 位置填占位 0。"""
    arr = df[['open', 'high', 'low', 'close']].to_numpy(dtype=float)
    klines = [[0.0, arr[i][0], arr[i][1], arr[i][2], arr[i][3]] for i in range(len(arr))]
    klines.reverse()  # 旧→新 转成 新→旧，匹配既有函数口径
    return klines


def _compute_sar_from_df(df) -> Dict:
    """直接从趋势标记价K线（df）算 SAR，复用既有抛物线递推逻辑，0 额外请求。"""
    if df is None or len(df) < 2:
        return {'sar': '', 'sar_trend': '--'}
    return _compute_sar_from_klines(_df_to_klines_newest_first(df))


def _compute_er_from_df(df, window: int = 8):
    """直接从趋势标记价K线（df）算 Kaufman 效率系数 ER，复用既有逻辑。"""
    if df is None or len(df) < 2:
        return ''
    return _compute_er_from_klines(_df_to_klines_newest_first(df), window=window)


def _fetch_klines_for_sar(inst_id: str, bar: str, limit: int = 100) -> List[List]:
    """获取K线数据用于SAR计算（共享连接 + 缓存 + 自动重试）

    - 同一 (inst_id, bar) 在 _SAR_KLINE_CACHE_TTL 秒内直接复用缓存；
    - 前 2 次失败直接重试（httpx 会自动重连断开的 HTTP/2 连接），
      连续 2 次失败才重建共享客户端，且采用“替换引用”而非 close，
      避免并发线程撞上 'Cannot send a request, as the client has been closed'。
    """
    cache_key = (inst_id, bar)
    now = time.time()
    with _sar_kline_cache_lock:
        cached = _sar_kline_cache.get(cache_key)
        if cached and (now - cached[0]) < _SAR_KLINE_CACHE_TTL:
            return cached[1]

    last_err = None
    for attempt in range(1, 4):
        try:
            market_api = _get_shared_market_api()
            # 合约 instId（如 BTC-USDT-SWAP）去掉 '-SWAP' 后缀即为指数/现货 instId（BTC-USDT），
            # 供 get_index_candlesticks 使用（旧写法 replace('-SWAP','-USDT') 会得到 BTC-USDT-USDT 非法 ID，导致返回空、SAR 缺失）。
            index_inst_id = inst_id.replace('-SWAP', '')
            # 本函数已自带 3 次异常重试，只取节流与 50011 退避，不叠加两层重试
            result = _rl('market_candles', market_api.get_index_candlesticks,
                         retry_on_error=False,
                         instId=index_inst_id, bar=bar, limit=str(limit))
            if result.get('code') == '0' and result.get('data'):
                with _sar_kline_cache_lock:
                    _sar_kline_cache[cache_key] = (time.time(), result['data'])
                return result['data']
            return []
        except Exception as e:
            last_err = e
            logger.warning("获取SAR K线数据失败 [%s %s] 第%d次: %s", inst_id, bar, attempt, str(e))
            if attempt < 3:
                if attempt == 2:
                    # 连续失败才重建客户端（替换引用，不 close）
                    _reset_shared_market_api()
                time.sleep(1.0 * attempt)
    logger.warning("获取SAR K线数据失败 [%s %s] 已重试3次: %s", inst_id, bar, last_err)
    return []

def _read_csv() -> List[Dict]:
    """读取全币种行情：迁移批次7b 起 DB 优先，DB 无数据/不可用时回退 CSV 文件"""
    if _db_session_scope is not None and _market_repo is not None:
        try:
            with _db_session_scope() as _s:
                rows = _market_repo.load_coin_rows(_s)
            if rows:
                return rows
        except Exception as e:
            logger.warning("从DB读取 crypto_coins 失败，回退CSV文件: %s", e)

    rows = []
    if not os.path.exists(CSV_PATH):
        logger.warning("CSV文件不存在: %s", CSV_PATH)
        return rows
    # utf-8-sig 兼容存量 BOM（避免首列名变 '\ufeffrank'）
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    # 确保新增列存在（使用周期后缀命名规范）
    new_cols = ['4H_趋势', '4H_交易价格', '4H_交易时间', '4H_盈亏%', '4H_收盘价',
                '1D_趋势', '1D_交易价格', '1D_交易时间', '1D_盈亏%', '1D_收盘价',
                '1H_趋势', '1H_交易价格', '1H_交易时间', '1H_盈亏%', '1H_收盘价',
                'MACD_4H', 'DIF_4H', 'ADX_4H', 'ATR_4H', 'SAR_4H', 'SAR颜色_4H',
                'MACD_1D', 'DIF_1D', 'ADX_1D', 'ATR_1D', 'SAR_1D', 'SAR颜色_1D',
                'MACD_1H', 'DIF_1H', 'ADX_1H', 'ATR_1H', 'SAR_1H', 'SAR颜色_1H',
                '15m_趋势', '15m_交易价格', '15m_交易时间', '15m_盈亏%', '15m_收盘价',
                'MACD_15m', 'DIF_15m', 'ADX_15m', 'ATR_15m', 'SAR_15m', 'SAR颜色_15m',
                'ER_15m', 'ER_1H', 'ER_4H', 'ER_1D']
    if rows:
        for col in new_cols:
            if col not in rows[0]:
                for row in rows:
                    row[col] = ''

    return rows


def _write_csv(rows: List[Dict]):
    """写入全币种行情：DB 主存 + CSV 文件双写（文件保留为兜底数据源）"""
    if not rows:
        return
    db_error = None
    if _db_session_scope is not None and _market_repo is not None:
        try:
            with _db_session_scope() as _s:
                _market_repo.save_coin_rows(_s, rows)
        except Exception as e:
            db_error = e
            logger.warning("写入 crypto_coins 到DB失败，仅写CSV文件: %s", e)
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=os.path.dirname(CSV_PATH), delete=False) as f:
            temp_path = f.name
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CSV_PATH)
        temp_path = None
    finally:
        if temp_path is not None:
            os.unlink(temp_path)
    logger.info("CSV 已保存: %s (%d 行)", CSV_PATH, len(rows))
    if db_error is not None:
        # 页面优先读取 DB；不能把仅备份成功冒充全部保存成功。
        raise RuntimeError("数据库保存失败，CSV 备份已保存；页面可能仍显示旧数据，请稍后重试") from db_error


def read_csv_for_display() -> List[Dict]:
    """读取 CSV 数据供前端表格展示（使用周期后缀命名规范）"""
    rows = _read_csv()
    records = []
    for row in rows:
        records.append({
            '排名': str(row.get('rank', '')),
            '币种': str(row.get('symbol', '')),
            '交易对': str(row.get('inst_id', '')),
            '名称': str(row.get('name_cn', '')),
            # 15m 周期
            '15m_趋势': str(row.get('15m_趋势', '')),
            '15m_交易价格': str(row.get('15m_交易价格', '')),
            '15m_交易时间': str(row.get('15m_交易时间', '')),
            '15m_盈亏%': str(row.get('15m_盈亏%', '')),
            'MACD_15m': str(row.get('MACD_15m', '')),
            'DIF_15m': str(row.get('DIF_15m', '')),
            'ADX_15m': str(row.get('ADX_15m', '')),
            'ATR_15m': str(row.get('ATR_15m', '')),
            'SAR_15m': str(row.get('SAR_15m', '')),
            'SAR颜色_15m': str(row.get('SAR颜色_15m', '')),
            'ER_15m': str(row.get('ER_15m', '')),
            # 1H 周期
            '1H_趋势': str(row.get('1H_趋势', '')),
            '1H_交易价格': str(row.get('1H_交易价格', '')),
            '1H_交易时间': str(row.get('1H_交易时间', '')),
            '1H_盈亏%': str(row.get('1H_盈亏%', '')),
            'MACD_1H': str(row.get('MACD_1H', '')),
            'DIF_1H': str(row.get('DIF_1H', '')),
            'ADX_1H': str(row.get('ADX_1H', '')),
            'ATR_1H': str(row.get('ATR_1H', '')),
            'SAR_1H': str(row.get('SAR_1H', '')),
            'SAR颜色_1H': str(row.get('SAR颜色_1H', '')),
            'ER_1H': str(row.get('ER_1H', '')),
            # 4H 周期
            '4H_趋势': str(row.get('4H_趋势', '')),
            '4H_交易价格': str(row.get('4H_交易价格', '')),
            '4H_交易时间': str(row.get('4H_交易时间', '')),
            '4H_盈亏%': str(row.get('4H_盈亏%', '')),
            'MACD_4H': str(row.get('MACD_4H', '')),
            'DIF_4H': str(row.get('DIF_4H', '')),
            'ADX_4H': str(row.get('ADX_4H', '')),
            'ATR_4H': str(row.get('ATR_4H', '')),
            'SAR_4H': str(row.get('SAR_4H', '')),
            'SAR颜色_4H': str(row.get('SAR颜色_4H', '')),
            'ER_4H': str(row.get('ER_4H', '')),
            # 1D 周期
            '1D_趋势': str(row.get('1D_趋势', '')),
            '1D_交易价格': str(row.get('1D_交易价格', '')),
            '1D_交易时间': str(row.get('1D_交易时间', '')),
            '1D_盈亏%': str(row.get('1D_盈亏%', '')),
            'MACD_1D': str(row.get('MACD_1D', '')),
            'DIF_1D': str(row.get('DIF_1D', '')),
            'ADX_1D': str(row.get('ADX_1D', '')),
            'ATR_1D': str(row.get('ATR_1D', '')),
            'SAR_1D': str(row.get('SAR_1D', '')),
            'SAR颜色_1D': str(row.get('SAR颜色_1D', '')),
            'ER_1D': str(row.get('ER_1D', '')),
        })
    return records


def get_filtered_records() -> List[Dict]:
    """获取筛选后的一致性币种记录（4H 和 1D 趋势相同）"""
    all_records = read_csv_for_display()
    filtered = []
    for r in all_records:
        trend_4h = r.get('4H_趋势', '')
        trend_1d = r.get('1D_趋势', '')
        # 两个周期都为"上涨"或都为"下跌"
        if trend_4h in ('上涨', '下跌') and trend_4h == trend_1d:
            filtered.append(r)
    return filtered


# ====================================================================
#  批量趋势分析器
# ====================================================================

def _warm_kline(inst_id: str, bar: str) -> bool:
    """锁外并发预热：经 market_candles 令牌桶限频，把 (inst_id, bar) 的标记价K线
    下满并写入 pro3 的 run 级预热缓存，供随后锁内计算 0 网络直接命中。

    - 仅批量路径在此接限频；实盘主链路 pro3._request_candles 不经此函数，保持直连，
      不给调度主链路新增任何可能卡住取数的环节（符合 okx_ratelimit 顶部的边界约定）。
    - _KLINE_TARGET_COUNT 已=300（单次 limit 上限），正常一页即取满、免翻页；
      个别新上市/历史不足的币一页 <300 时按 after 向前补足。
    """
    import real_strategy_adapter  # noqa: F401  确保 strategy/ 目录已进 sys.path
    import pro3_singletimeframe as _pro3

    # retry_on_error=False：_request_candles 自带重试，这里只取节流与 50011 退避，不叠加网络重试
    data = _rl('market_candles', _pro3._request_candles, inst_id, bar,
               after=None, max_retries=2, retry_on_error=False)
    if not data:
        raise RuntimeError(f"预热返回空数据: {inst_id} {bar}")
    afterts = data[-1][0]
    while len(data) < _pro3._KLINE_TARGET_COUNT:
        page = _rl('market_candles', _pro3._request_candles, inst_id, bar,
                   after=str(afterts), max_retries=2, retry_on_error=False)
        if not page:
            break
        data.extend(page)
        next_ts = page[-1][0]
        if int(next_ts) >= int(afterts):
            raise RuntimeError("预热分页游标未前进: %s %s" % (inst_id, bar))
        afterts = next_ts
    _pro3.store_batch_kline(inst_id, bar, data)
    return True


class BatchTrendAnalyzer:
    """批量多周期趋势分析器

    使用示例::

        analyzer = BatchTrendAnalyzer()
        analyzer.run()              # 同步运行
        # 或
        analyzer.run_in_background()  # 后台线程运行
        progress = get_progress()     # 查询进度
    """

    def __init__(self):
        global _progress
        self.progress = _progress

    def _process_single_symbol(self, inst_id: str, on_step=None) -> Dict:
        """处理单个币种，返回各周期（_BATCH_BARS：15m/1H/4H/1D）的趋势+技术指标信息

        Parameters
        ----------
        inst_id : str
            币种交易对ID，如 'BTC-USDT-SWAP'
        on_step : callable, optional
            每完成一个周期后的回调，签名为 on_step(bar)，用于细化进度展示

        Returns
        -------
        dict
            {'15m': {...}, '1H': {...}, '4H': {...}, '1D': {...}}（失败时额外带 'error' 键）
        """
        from real_strategy_adapter import calculate_single_coin_data
        import pro3_singletimeframe as _pro3

        try:
            infos = {}
            for bar in _BATCH_BARS:
                result = calculate_single_coin_data(inst_id, bar=bar, max_retries=2)
                info = _extract_trend_info(result)
                if info['trend_flag'] == 'error':
                    raise RuntimeError("%s 周期未返回有效分析结果" % bar)
                # SAR/ER 直接复用趋势标记价K线（calculate_single_coin_data 已预热，0 额外请求）；
                # 与趋势列、实盘交易基准同为标记价口径，全表自洽，省去单独的指数K线取数。
                df = _pro3._fetch_kline_data(inst_id, bar)
                sar_result = _compute_sar_from_df(df)
                info['sar'] = sar_result['sar']
                close_p = info.get('close_price', 0)
                try:
                    sar_val = float(info['sar']) if info.get('sar') != '' else None
                    cp = float(close_p) if close_p != '' and close_p != 0 else None
                    if sar_val is not None and cp is not None and cp > 0:
                        info['sar_color'] = 'green' if sar_val < cp else 'red'
                    else:
                        info['sar_color'] = ''
                except (ValueError, TypeError):
                    info['sar_color'] = ''
                # ER 效率系数：与 SAR 同一批 K 线本地算，与 ADX/ATR 配合筛选"趋势强且波动大"的币种
                info['er'] = _compute_er_from_df(df)
                infos[bar] = info
                if on_step:
                    on_step(bar)

            return {bar: infos[bar] for bar in _BATCH_BARS}
        except Exception as e:
            logger.warning("[%s] 批量分析失败: %s", inst_id, str(e))
            empty_info = {
                'trend': '错误', 'trend_flag': 'error',
                'trade_price': '', 'trade_time': '', 'profit_pct': '',
                'close_price': '',
                'macd_hist': '', 'dif': '', 'adx': '', 'atr_pct': '',
                'sar': '', 'sar_color': '', 'er': '',
            }
            failed = {bar: dict(empty_info) for bar in _BATCH_BARS}
            failed['error'] = str(e)
            return failed

    def _claim(self):
        """在线程启动前原子占位；同步入口也经过同一互斥闸门。"""
        with _batch_state_lock:
            if not _batch_run_lock.acquire(blocking=False):
                raise BatchAlreadyRunning("已有批量分析任务正在运行中，请等待完成后再启动")
            try:
                self.progress.start(0)
                clear_batch_logs()
                _batch_log("任务已接收，正在加载币种与策略模块...")
            except BaseException:
                _batch_run_lock.release()
                raise

    def run(self, callback=None) -> Dict:
        """同步执行；与 Web 后台入口共享任务锁。"""
        self._claim()
        return self._execute(callback)

    def _execute(self, callback=None) -> Dict:
        result = None
        failure = None
        try:
            result = self._run_pipeline(callback)
        except BaseException as exc:
            failure = exc
            logger.exception("批量分析任务异常")
            raise
        finally:
            try:
                pro3 = sys.modules.get('pro3_singletimeframe')
                if pro3 is not None:
                    pro3.clear_batch_cache()
            except Exception as exc:
                failure = failure or exc
                logger.exception("批量分析缓存清理失败")
            finally:
                # 先清理缓存，最后原子发布尾日志、终态并释放执行通道。
                with _batch_state_lock:
                    if failure is not None:
                        message = "批量分析失败: %s" % (str(failure) or type(failure).__name__)
                        _batch_log(message, 'error')
                        self.progress.set_error(message)
                    else:
                        if result['success'] == 0 and result['error']:
                            message = "全部币种分析失败，请检查行情接口或网络后重试"
                            _batch_log(message, 'error')
                            self.progress.set_error(message)
                        else:
                            self.progress.finish()
                            _batch_log(self.progress.message,
                                       'warn' if result['error'] else 'info')
                    _batch_run_lock.release()
        return result

    def _run_pipeline(self, callback=None) -> Dict:
        """同步运行批量分析（两段式流水线）

        阶段1 预热：_BATCH_WORKERS 个线程【在锁外】并发下载各 (inst_id, bar) 的
                 标记价K线，经 market_candles 令牌桶限频（~5/s）。D1 取满一页 300 根
                 后，55 币 × 4 周期 = 220 组合仅 220 次请求，约 45s 下完。
        阶段2 计算：【在锁内】串行逐币调 calculate_single_coin_data；此时行情已预热，
                 _fetch_kline_data 命中 run 级缓存 0 网络，PRO3_LOCK 只锁住纯计算，
                 A 币下载不再阻塞 B 币计算（旧实现在锁内含网络等待，多线程形同串行）。

        进度状态线程安全；run 结束在 finally 清空预热缓存，避免残留数据泄漏给实盘/后续轮次。

        Parameters
        ----------
        callback : callable, optional
            每处理完一个币种后的回调函数，签名为 callback(progress_dict)。

        Returns
        -------
        dict
            {'total': int, 'success': int, 'error': int}
        """
        import real_strategy_adapter  # noqa: F401  确保 strategy/ 目录进 sys.path
        import pro3_singletimeframe  # noqa: F401  在预热前验证依赖，清理由 _execute 负责

        rows = _read_csv()
        total = len(rows)
        if not rows:
            raise ValueError("币种列表为空，无法启动批量分析")
        if any(not str(row.get('inst_id') or '').strip() for row in rows):
            raise ValueError("币种列表存在缺少交易对的记录，请先修正数据")
        combos = [(row.get('inst_id', ''), bar)
                  for row in rows for bar in _BATCH_BARS if row.get('inst_id', '')]
        n_combo = len(combos)

        # 进度总单位 = 预热请求数 + 币种计算数（保证 0→100% 线性推进）
        self.progress.configure(n_combo + total, total)
        _batch_log("批量更新启动：%d 币种 × %d 周期 = %d 组合" % (total, len(_BATCH_BARS), n_combo))

        # ---------------- 阶段1：锁外并发预热（受令牌桶限频） ----------------
        _batch_log("预热行情（并发 %d 线程，限频 5/s）..." % _BATCH_WORKERS)
        warm_done = 0
        warm_failed = 0
        with ThreadPoolExecutor(max_workers=_BATCH_WORKERS) as pool:
            fut_map = {pool.submit(_warm_kline, inst_id, bar): (inst_id, bar)
                       for inst_id, bar in combos}
            for fut in as_completed(fut_map):
                inst_id, bar = fut_map[fut]
                warm_done += 1
                try:
                    fut.result()
                except Exception as e:
                    warm_failed += 1
                    logger.warning("预热失败 [%s %s]（计算阶段回退直连重试）: %s", inst_id, bar, e)
                    _batch_log("预热失败 %s %s: %s" % (inst_id, bar, str(e)), 'warn')
                self.progress.update(
                    current=warm_done,
                    message="预热行情 %d/%d（失败 %d）" % (warm_done, n_combo, warm_failed))
        _batch_log("预热完成 %d/%d，失败 %d" % (warm_done, n_combo, warm_failed))

        # ---------------- 阶段2：锁内串行计算（命中预热缓存，0 网络） ----------------
        _batch_log("计算阶段（锁内串行，命中预热缓存）...")
        success_count = 0
        error_count = 0
        completed = 0
        for idx, row in enumerate(rows):
            inst_id = row.get('inst_id', '')
            symbol = row.get('symbol', inst_id)
            cur_base = n_combo + completed  # 本币计算开始时的进度基数
            self.progress.update(
                current=cur_base, symbol=symbol, phase='compute',
                message="计算中: %d/%d — %s" % (completed, total, symbol))
            _batch_log("开始计算 %s (%d/%d)" % (symbol, completed + 1, total))

            def on_step(bar):
                self.progress.update(
                    current=cur_base + (_BATCH_BARS.index(bar) + 1) / len(_BATCH_BARS),
                    message="计算中: %s — %s 完成 (%d/%d 币种)" % (symbol, bar, completed, total))

            info = self._process_single_symbol(inst_id, on_step=on_step)
            completed += 1

            if info and 'error' not in info:
                for bar in _BATCH_BARS:
                    info_bar = info[bar]
                    # 日志输出：模拟终端行格式，让用户看到每币每周期结果
                    cp = info_bar.get('close_price', '')
                    tp = info_bar.get('trade_price', '')
                    pp = info_bar.get('profit_pct', '')
                    _batch_log("%-16s %-4s | 现价: %-10s | 交易价: %-10s | 盈亏: %s%%"
                               % (inst_id, bar, cp, tp,
                                  ('%+.2f' % float(pp)) if pp != '' and pp is not None else '--'))
                    row['%s_趋势' % bar] = info_bar['trend']
                    row['%s_交易价格' % bar] = str(info_bar['trade_price']) if info_bar['trade_price'] != '' else ''
                    row['%s_交易时间' % bar] = info_bar['trade_time']
                    row['%s_盈亏%%' % bar] = str(info_bar['profit_pct']) if info_bar['profit_pct'] != '' else ''
                    row['%s_收盘价' % bar] = str(info_bar['close_price']) if info_bar['close_price'] != '' else ''
                    row['MACD_%s' % bar] = str(info_bar['macd_hist']) if info_bar['macd_hist'] != '' else ''
                    row['DIF_%s' % bar] = str(info_bar['dif']) if info_bar['dif'] != '' else ''
                    row['ADX_%s' % bar] = str(info_bar['adx']) if info_bar['adx'] != '' else ''
                    row['ATR_%s' % bar] = str(info_bar['atr_pct']) if info_bar['atr_pct'] != '' else ''
                    row['SAR_%s' % bar] = str(info_bar.get('sar', '')) if info_bar.get('sar', '') != '' else ''
                    row['SAR颜色_%s' % bar] = str(info_bar.get('sar_color', '')) if info_bar.get('sar_color', '') else ''
                    row['ER_%s' % bar] = str(info_bar.get('er', '')) if info_bar.get('er', '') != '' else ''
                success_count += 1
            else:
                error_count += 1
                err_msg = info.get('error', '未知') if info else '未知'
                logger.error("[%s] 处理失败: %s", symbol, err_msg)
                _batch_log("[%s] 处理失败: %s" % (symbol, err_msg), 'error')

            self.progress.update(
                current=n_combo + completed,
                success=success_count,
                error=error_count,
            )

            # 整表落库降频：每 _SAVE_EVERY 个写一次，收尾再兜底写一次
            if completed % _SAVE_EVERY == 0:
                self.progress.update(n_combo + completed, phase='saving', message='正在保存阶段结果...')
                _write_csv(rows)

            if callback:
                callback(self.progress.to_dict())

        self.progress.update(n_combo + completed, phase='saving', message='正在保存最终结果...')
        _write_csv(rows)

        return {
            'total': total,
            'success': success_count,
            'error': error_count,
        }

    def run_in_background(self) -> threading.Thread:
        """后台线程运行批量分析

        Returns
        -------
        threading.Thread
            已启动的后台线程
        """
        self._claim()

        def worker():
            try:
                self._execute()
            except Exception:
                pass  # _execute 已发布错误日志和终态，避免线程异常无人处理。

        try:
            thread = threading.Thread(target=worker, daemon=True, name="batch-trend-analyzer")
            thread.start()
        except BaseException as exc:
            with _batch_state_lock:
                message = "后台线程启动失败: %s" % exc
                _batch_log(message, 'error')
                self.progress.set_error(message)
                _batch_run_lock.release()
            raise
        return thread


# ====================================================================
#  命令行入口
# ====================================================================

def main():
    """命令行直接运行"""
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(message)s'
    )

    print("=" * 70)
    print("  加密货币批量多周期趋势分析")
    print("=" * 70)

    analyzer = BatchTrendAnalyzer()
    result = analyzer.run(callback=lambda p: print(
        "\r  进度: %s/%s (%.1f%%) — %s | 成功:%s 失败:%s   " % (
            p['current'], p['total'], p['progress_pct'],
            p['current_symbol'], p['success'], p['error'],
        ), end='', flush=True
    ))

    print("\n")
    print("  总计: %d  成功: %d  失败: %d" % (
        result['total'], result['success'], result['error']))

    # 打印前 10 条结果
    records = read_csv_for_display()
    filtered = [r for r in records
                if r['4H_趋势'] in ('上涨', '下跌')
                and r['4H_趋势'] == r['1D_趋势']]
    print("\n  双周期一致性币种 (%d 个):" % len(filtered))
    for r in filtered:
        print("    %-5s %-8s  4H: %s(%s%%)  1D: %s(%s%%)" % (
            r['币种'], r['名称'],
            r['4H_趋势'], r['4H_盈亏%'] or '--',
            r['1D_趋势'], r['1D_盈亏%'] or '--',
        ))


if __name__ == '__main__':
    main()
