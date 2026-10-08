"""
策略工具类 —— 封装多策略的完整数据提取
==========================================
支持三种策略：
  - single: 单周期Pro3（混合状态机趋势指标）
  - dual:   双周期Pro3（短周期信号 + 长周期方向确认，均为混合状态机）
  - boll:   BOLL限价策略（布林带边界交易 + 长周期过滤）

趋势策略（single/dual）支持开/平仓价格取值与短周期信号算法配置：
  - entry_price: 'open' / 'close'，默认 'close'（收盘价开仓）
  - exit_price:  'open' / 'close'，默认 'open'（开盘价平仓）
  - signal_algo: 'diff'（平滑差，灵敏）/ 'hybrid'（混合状态机，稳定），默认 'diff'
"""

import sys
import os
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_DIR = os.path.join(BASE_DIR, "strategy")

# 确保 strategy/ 目录在 sys.path 中
if STRATEGY_DIR not in sys.path:
    sys.path.insert(0, STRATEGY_DIR)

# 同时确保根目录在 sys.path 中（供 api_config 等模块导入）
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 策略计算全局串行锁：详情页会把引擎的 FAST_MODE 设成 False（要完整回测
# 拿交易列表），而实盘算信号时 FAST_MODE 必须是 True——两者并发时，详情页
# 的 finally 恢复会把正在运行的实盘算成“非快速模式”，在最后一根 K 线上多走
# 一次期末强平。锁必须挂在独立且复用的模块上，
# 原因见 task/strategy_gate 文档。
try:
    from .task.strategy_gate import pro3_locked
except ImportError:
    _TASK_DIR = os.path.join(BASE_DIR, 'task')
    if _TASK_DIR not in sys.path:
        sys.path.insert(0, _TASK_DIR)
    from strategy_gate import pro3_locked


# 默认双周期长周期映射（短周期 → 长周期）
DEFAULT_LONG_BAR_MAP = {
    "1m": "5m",
    "3m": "15m",
    "5m": "30m",
    "15m": "1H",
    "30m": "4H",
    "1H": "4H",
    "2H": "1D",
    "4H": "1D",
    "6H": "1W",
    "12H": "1W",
    "1D": "1W",
    "1W": "1W",
}

# Keltner×BOLL 专用短→长周期默认映射（与前端 detail.html 的 KB_DEFAULT_MAP 对齐）：
# 3m→1H、1m→30m（OKX 标记价格K线接口实测支持 30m）。独立于 DEFAULT_LONG_BAR_MAP，
# 避免改动波及 single/dual/boll 等其他策略的回退口径。
KB_LONG_BAR_MAP = {
    "1m": "30m",
    "3m": "1H",
    "5m": "1H",
    "15m": "4H",
    "30m": "4H",
    "1H": "1D",
}


# 趋势策略价格取值默认配置
DEFAULT_ENTRY_PRICE_TYPE = "close"   # 开仓默认用收盘价
DEFAULT_EXIT_PRICE_TYPE = "open"     # 平仓默认用开盘价
_VALID_PRICE_TYPES = ("open", "close")


def _normalize_price_type(value, default):
    """归一化价格取值参数：仅允许 open/close，非法或缺省时回退到默认值"""
    v = str(value or "").strip().lower()
    return v if v in _VALID_PRICE_TYPES else default


# 短周期信号算法默认配置：'diff'（平滑差，灵敏）/ 'hybrid'（混合状态机，稳定）
DEFAULT_SIGNAL_ALGO = "diff"
_VALID_SIGNAL_ALGOS = ("diff", "hybrid")
# 兼容别名：前端/调用方可能传入的其他写法
_SIGNAL_ALGO_ALIASES = {
    "smoothed_diff": "diff",
    "smooth_diff": "diff",
    "sensitive": "diff",
    "state_machine": "hybrid",
    "statemachine": "hybrid",
    "stable": "hybrid",
}


def _normalize_signal_algo(value):
    """归一化短周期信号算法参数：支持别名映射，非法或缺省时回退到默认值"""
    v = str(value or "").strip().lower()
    v = _SIGNAL_ALGO_ALIASES.get(v, v)
    return v if v in _VALID_SIGNAL_ALGOS else DEFAULT_SIGNAL_ALGO


def get_strategy_detail(instId, bar="1H", strategy="single", long_bar=None,
                        entry_price=None, exit_price=None, signal_algo=None,
                        trend_indicator=None, entry_atr_mult=None,
                        exit_atr_mult=None, atr_period=None):
    """
    运行指定策略并返回结构化数据，供详情页使用。

    参数:
        instId:      币种ID，如 "BTC-USDT-SWAP"
        bar:         短周期K线，如 "1H"
        strategy:    策略类型 - "single" / "dual" / "boll" / "keltner_boll"
        long_bar:    长周期（仅dual/boll/keltner_boll使用），默认自动映射
        entry_price: 开仓价格取值 'open'/'close'（仅single/dual），默认 'close'
        exit_price:  平仓价格取值 'open'/'close'（仅single/dual），默认 'open'
        signal_algo: 短周期信号算法 'diff'（平滑差，灵敏）/'hybrid'（状态机，稳定）（仅single/dual），默认 'diff'
        trend_indicator: 长周期趋势方向方法名（仅keltner_boll），默认 'keltner'
        entry_atr_mult:  开仓轨 ATR 外扩倍数（仅keltner_boll），默认 0.0
        exit_atr_mult:   平仓轨 ATR 外扩倍数（仅keltner_boll），默认 0.0
        atr_period:      外扩用 ATR 周期（仅keltner_boll），默认 14

    返回:
        dict: 包含 market / current_position / trade_records / stats 的完整字典
    """
    strategy = (strategy or "single").strip().lower()
    entry_price_type = _normalize_price_type(entry_price, DEFAULT_ENTRY_PRICE_TYPE)
    exit_price_type = _normalize_price_type(exit_price, DEFAULT_EXIT_PRICE_TYPE)
    signal_algo_type = _normalize_signal_algo(signal_algo)

    if strategy == "single":
        return _get_single_period_detail(instId, bar, entry_price_type, exit_price_type, signal_algo_type)
    elif strategy == "dual":
        _long_bar = long_bar or DEFAULT_LONG_BAR_MAP.get(bar, "1D")
        return _get_dual_period_detail(instId, bar, _long_bar, entry_price_type, exit_price_type, signal_algo_type)
    elif strategy == "boll":
        _long_bar = long_bar or DEFAULT_LONG_BAR_MAP.get(bar, "4H")
        return _get_boll_detail(instId, bar, _long_bar)
    elif strategy == "keltner_boll":
        _long_bar = long_bar or KB_LONG_BAR_MAP.get(bar, "4H")
        return _get_keltner_boll_detail(instId, bar, _long_bar, trend_indicator,
                                        entry_atr_mult, exit_atr_mult, atr_period)
    else:
        raise ValueError(f"不支持的策略类型: {strategy}")


@contextmanager
def _detail_settings(module):
    """在已有策略锁内临时设置展示参数，成功或失败都还原共享模块状态。"""
    names = ('FAST_MODE', 'PRINT_MARKET', 'PRINT_TRADE_OPS', 'PRINT_TRADE_RECORDS',
             'PRINT_ORIGINAL_OUTPUT', 'ENTRY_PRICE_TYPE', 'EXIT_PRICE_TYPE',
             'SHORT_SIGNAL_ALGO', 'CURRENT_INSTID', 'CURRENT_BAR',
             'CURRENT_SHORT_BAR', 'CURRENT_LONG_BAR')
    saved = {name: getattr(module, name) for name in names if hasattr(module, name)}
    try:
        module.PRINT_MARKET = 0
        module.PRINT_TRADE_OPS = 0
        module.PRINT_TRADE_RECORDS = 0
        yield
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


@pro3_locked(default_timeout=90.0, on_busy='raise')
def _get_single_period_detail(instId, bar, entry_price_type, exit_price_type, signal_algo):
    """单周期Pro3策略详情（在策略计算独占权下跑，拿不到锁则抛）"""
    # 复用模块；逐请求重新导入会再次注册 Backtrader 类并拆散共享缓存。
    import pro3_singletimeframe as strategy_module

    if not hasattr(strategy_module, 'get_strategy_full_data'):
        raise ImportError("策略模块缺少 get_strategy_full_data 函数")

    with _detail_settings(strategy_module):
        return strategy_module.get_strategy_full_data(
            instId, bar, detail_mode=True,
            entry_price_type=entry_price_type, exit_price_type=exit_price_type,
            signal_algo=signal_algo)


@pro3_locked(default_timeout=90.0, on_busy='raise')
def _get_dual_period_detail(instId, short_bar, long_bar, entry_price_type, exit_price_type, signal_algo):
    """双周期Pro3策略详情（在策略计算独占权下跑，拿不到锁则抛）"""
    import pro3_dualtimeframe as strategy_module

    if not hasattr(strategy_module, 'get_strategy_full_data_dual'):
        raise ImportError("策略模块缺少 get_strategy_full_data_dual 函数")

    with _detail_settings(strategy_module):
        return strategy_module.get_strategy_full_data_dual(
            instId, short_bar, long_bar,
            entry_price_type=entry_price_type, exit_price_type=exit_price_type,
            signal_algo=signal_algo)


def _get_boll_detail(instId, short_bar, long_bar):
    """BOLL限价策略详情"""
    from boll_limit_dualtimeframe import get_boll_strategy_full_data
    result = get_boll_strategy_full_data(instId, short_bar, long_bar)
    return result


def _get_keltner_boll_detail(instId, short_bar, long_bar, trend_indicator=None,
                             entry_atr_mult=None, exit_atr_mult=None, atr_period=None):
    """Keltner×BOLL 区间策略详情（B 仓位默认引擎的回测视图）。

    参数 fail-fast 校验：非法值直接抛 ValueError，由详情页路由转成错误提示，
    绝不静默回退成别的趋势方法（静默回退会让用户以为在看 A 方法其实在看 B）。
    keltner_boll 无 backtrader 全局状态、不触碰 pro3 引擎参数，故与 boll 详情
    一样无需占用 pro3_locked 策略计算锁。
    """
    from keltner_boll import (get_keltner_boll_full_data, list_indicators,
                              DEFAULT_TREND_INDICATOR)

    ti = str(trend_indicator or DEFAULT_TREND_INDICATOR).strip().lower()
    valid = list_indicators()
    if ti not in valid:
        raise ValueError(f"未知长周期趋势方法 '{trend_indicator}'，可选: {'/'.join(valid)}")

    def _non_neg_float(v, default, name):
        if v is None or v == "":
            return default
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须为数字，收到 '{v}'")
        if f < 0:
            raise ValueError(f"{name} 不能为负数，收到 '{v}'")
        return f

    def _pos_int(v, default, name):
        if v is None or v == "":
            return default
        try:
            i = int(float(v))
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须为整数，收到 '{v}'")
        if i <= 0:
            raise ValueError(f"{name} 必须为正整数，收到 '{v}'")
        return i

    entry_mult = _non_neg_float(entry_atr_mult, 0.0, 'entry_atr_mult')
    exit_mult = _non_neg_float(exit_atr_mult, 0.0, 'exit_atr_mult')
    atr_p = _pos_int(atr_period, 14, 'atr_period')

    return get_keltner_boll_full_data(
        instId, short_bar, long_bar, trend_indicator=ti,
        entry_atr_mult=entry_mult, exit_atr_mult=exit_mult, atr_period=atr_p)
