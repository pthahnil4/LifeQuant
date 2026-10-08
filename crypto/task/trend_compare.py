#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
实盘 vs 趋势策略理论对比引擎
==============================

回答一个问题：同一时间段内，趋势策略的**理论交易序列**与实盘的
**实际成交流水**差在哪里、差多少、为什么差。

三步流程：
1. 理论重放 —— 用与实盘完全相同的信号源（pro3_dualtimeframe）拉取
   逐 bar 的 MODIFY_FLAG / LONG_DIRECTION，在本模块复现
   trend_range_trader 的三时段窗口确认逻辑，生成理论开/平仓序列。
   理论成交价 = 反转 bar 的 open/close（与实盘挂单取价规则一致）。
2. 实际读取 —— 从 trade_journal.jsonl 读取 bucket=trend 的结构化成交流水
   （bucket=account 的人工强平作为计划外记录一并展示）。
3. 匹配归因 —— 以理论信号为主轴做时间窗口匹配：
   matched=正常执行(算滑点) / missed=信号未成交 / extra=计划外平仓(止盈/止损/强平)。

注意：
- 理论重放假设限价单按理论价立即成交（不模拟排队/未触及），因此
  理论收益是该取价规则下的“满执行”上界，与实际的差距即执行损耗。
- K线覆盖范围受 API 翻页限制（约500根短周期bar），响应中带 coverage_start。
- 理论重放按策略参数缓存 _REPLAY_TTL 秒（不含 start/end），实盘流水每次重读；
  交易所网络抖动导致重放失败时回落到过期缓存，响应中带 stale_since 标明数据时点。

作者：AI Assistant
创建时间：2026-07-29
"""

import time
import datetime
import threading
from typing import Dict, List, Optional

try:
    from .strategy_adapter import DualPeriodStrategyAdapter
    from .utils.trade_journal import read_journal, classify_reason
    from .strategy_gate import pro3_locked
except ImportError:
    from strategy_adapter import DualPeriodStrategyAdapter
    from utils.trade_journal import read_journal, classify_reason
    from strategy_gate import pro3_locked


# 周期字符串 → 分钟数（匹配窗口与覆盖范围计算用）
_PERIOD_MINUTES = {
    '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
    '1H': 60, '2H': 120, '4H': 240, '6H': 360, '12H': 720, '1D': 1440,
}

# 理论信号与实际成交的匹配窗口 = N 个短周期 bar
_MATCH_WINDOW_BARS = 3

# 理论重放缓存秒数（重放需拉K线+跑指标，避免前端反复点击重复请求交易所）
_REPLAY_TTL = 300

# 重放缓存最多保留的参数组合数
_REPLAY_CACHE_MAX = 16


def _period_minutes(period: str) -> int:
    return _PERIOD_MINUTES.get(str(period), 5)


def _friendly_err(e: Exception) -> str:
    """把K线获取异常翻译成可读文案。

    httpx 传输层异常的 str(e) 常为 'timed out' 甚至空串，直接回给前端看不懂，
    故统一带上异常类名并说明是交易所网络问题、非配置错误。
    """
    name = type(e).__name__
    if any(k in name for k in ('Timeout', 'Connect', 'Protocol', 'SSL', 'Proxy')):
        return f'OKX K线接口网络异常（{name}），重试多次仍失败，请稍后再试'
    return f'{name}: {e}' if str(e) else name


def _flag_to_dir(flag) -> Optional[str]:
    if flag == 'rise':
        return 'long'
    if flag == 'fall':
        return 'short'
    return None


def _ts_str(dt) -> str:
    """pandas Timestamp / datetime → 'YYYY-MM-DD HH:MM:SS'"""
    return dt.strftime('%Y-%m-%d %H:%M:%S')


class TrendCompareService:
    """理论重放 + 实盘流水匹配归因（线程安全，带结果缓存）"""

    def __init__(self):
        self._adapter = DualPeriodStrategyAdapter()
        # 策略参数 -> (写入时间戳, trades, coverage_start)
        # 只缓存昂贵的理论重放；实盘流水每次重读，保证新成交立即可见
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    # =================================================================
    # 对外入口
    # =================================================================

    def compare(self, inst_id: str, short_period: str, long_period: str,
                period_mode: str = 'dual', signal_algo: str = None,
                entry_price_type: str = 'close', exit_price_type: str = 'open',
                leverage: float = 10.0, start: str = None, end: str = None) -> Dict:
        """执行一次完整对比。start/end 为 'YYYY-MM-DD HH:MM:SS'，默认近1天。"""
        now = datetime.datetime.now()
        if not end:
            end = _ts_str(now)
        if not start:
            start = _ts_str(now - datetime.timedelta(days=1))

        theory_all, coverage_start, stale_since = self._replay_cached(
            inst_id, short_period, long_period, period_mode,
            signal_algo, entry_price_type, exit_price_type)
        theory = [t for t in theory_all if start <= t['time'] <= end]

        actual = read_journal(inst_id=inst_id, bucket='trend', start=start, end=end)
        actual += read_journal(inst_id=inst_id, bucket='account', start=start, end=end)
        actual.sort(key=lambda r: r.get('ts', ''))

        result = self._match_and_summarize(
            theory, actual, short_period, float(leverage or 10.0))
        result['params'] = {
            'inst_id': inst_id, 'short_period': short_period,
            'long_period': long_period, 'period_mode': period_mode,
            'signal_algo': signal_algo or 'diff',
            'entry_price_type': entry_price_type, 'exit_price_type': exit_price_type,
            'leverage': leverage, 'start': start, 'end': end,
            'coverage_start': coverage_start,
            'stale_since': stale_since,
        }
        return result

    # =================================================================
    # 第一步：理论信号重放
    # =================================================================

    def _replay_cached(self, inst_id: str, short_period: str, long_period: str,
                       period_mode: str, signal_algo: str,
                       entry_price_type: str, exit_price_type: str):
        """带 TTL 的理论重放，返回 (trades, coverage_start, stale_since)。

        重放不依赖 start/end（每次都拉取 API 能给到的完整K线范围），因此缓存键
        只按策略参数构成，前端反复点击/切换天数不会重复请求交易所。
        K线获取失败时优先回落到已过期的缓存并回报其写入时间（stale_since），
        避免一次交易所网络抖动就让整页报错；无任何缓存可用才抛出可读异常。
        """
        key = f"{inst_id}|{short_period}|{long_period}|{period_mode}|" \
              f"{signal_algo}|{entry_price_type}|{exit_price_type}"
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < _REPLAY_TTL:
            return hit[1], hit[2], None

        try:
            trades, coverage_start = self._replay(
                inst_id, short_period, long_period, period_mode,
                signal_algo, entry_price_type, exit_price_type)
        except Exception as e:
            if not hit:
                raise RuntimeError(_friendly_err(e)) from e
            stale_since = _ts_str(datetime.datetime.fromtimestamp(hit[0]))
            print(f"[对比] {inst_id} 理论重放失败，回落到 {stale_since} 的缓存结果 "
                  f"{type(e).__name__}: {e!r}")
            return hit[1], hit[2], stale_since

        with self._lock:
            self._cache[key] = (time.time(), trades, coverage_start)
            if len(self._cache) > _REPLAY_CACHE_MAX:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
        return trades, coverage_start, None

    @pro3_locked(default_timeout=90.0, on_busy='raise')
    def _replay(self, inst_id: str, short_period: str, long_period: str,
                period_mode: str, signal_algo: str,
                entry_price_type: str, exit_price_type: str):
        """重放理论交易序列。返回 (trades, coverage_start)。

        整段要拿策略计算独占权：里面改引擎全局再跑双周期引擎。拿不到锁时
        抛 StrategyBusy，上层已有的 except 会回落到过期缓存并标 stale_since
        （宁可给一份标了时点的旧结果，也不要被并发污染的新结果）。

        trades: [{'time','action'('open'/'close'),'direction','price','note'}]
        """
        self._adapter._ensure_initialized()
        single = self._adapter._single
        dual = self._adapter._dual

        # 抑制策略模块输出（与 strategy_adapter.analyze 相同的处理）
        saved = {k: getattr(single, k) for k in (
            'FAST_MODE', 'PRINT_ORIGINAL_OUTPUT', 'PRINT_MARKET',
            'PRINT_TRADE_OPS', 'PRINT_TRADE_RECORDS')}
        single.FAST_MODE = True
        single.PRINT_ORIGINAL_OUTPUT = 0
        single.PRINT_MARKET = 0
        single.PRINT_TRADE_OPS = 0
        single.PRINT_TRADE_RECORDS = 0
        try:
            _, _, _, _, df_short, df_long = dual.get_latest_data_dual(
                inst_id, short_period, long_period,
                entry_price_type=entry_price_type,
                exit_price_type=exit_price_type,
                signal_algo=signal_algo)
        finally:
            for k, v in saved.items():
                setattr(single, k, v)

        if df_short is None or len(df_short) < 3:
            return [], None
        coverage_start = _ts_str(df_short.index[0])

        # 短周期方向序列
        sdir = [_flag_to_dir(f) for f in df_short['MODIFY_FLAG']] \
            if 'MODIFY_FLAG' in df_short.columns else [None] * len(df_short)

        # 长周期"决策方向"序列：实盘用上一根长周期bar的方向（避免震荡期频繁反转），
        # 重放同样把长周期方向序列 shift(1) 后前向填充到短周期时间轴
        ldec = [None] * len(df_short)
        try:
            if df_long is not None and 'LONG_DIRECTION' in df_long.columns:
                lv = df_long['LONG_DIRECTION'].dropna()
                shifted = lv.shift(1).dropna()
                if len(shifted) > 0:
                    aligned = shifted.reindex(df_short.index, method='ffill')
                    ldec = [_flag_to_dir(x) for x in aligned]
        except Exception:
            pass

        opens = df_short['open'].astype(float).tolist() \
            if 'open' in df_short.columns else df_short['close'].astype(float).tolist()
        closes = df_short['close'].astype(float).tolist()
        idx = df_short.index

        def rev_px(i: int, price_type: str) -> float:
            """反转 bar（当前确认 bar 的前一根）的 open/close —— 与实盘挂单取价一致"""
            j = max(0, i - 1)
            return opens[j] if price_type == 'open' else closes[j]

        trades: List[Dict] = []
        holding: Optional[str] = None   # 当前理论持仓方向

        def do_open(i, direction, note=''):
            nonlocal holding
            trades.append({'time': _ts_str(idx[i]), 'action': 'open',
                           'direction': direction,
                           'price': rev_px(i, entry_price_type), 'note': note})
            holding = direction

        def do_close(i, price, note=''):
            nonlocal holding
            trades.append({'time': _ts_str(idx[i]), 'action': 'close',
                           'direction': holding, 'price': price, 'note': note})
            holding = None

        mode = (period_mode or 'dual').lower()
        for i in range(2, len(df_short)):
            s0, s1, s2 = sdir[i], sdir[i - 1], sdir[i - 2]
            if mode == 'single':
                # 单周期双向：短周期方向翻转即平旧开新
                if s0 and s0 != s1 and s1 is not None:
                    if holding and holding != s0:
                        do_close(i, rev_px(i, exit_price_type), '短周期翻转')
                    if holding is None:
                        do_open(i, s0, '短周期翻转')
                continue

            # 双周期单向：只做长周期方向，三时段窗口确认
            desired = ldec[i]
            if not desired:
                continue
            opp = 'short' if desired == 'long' else 'long'
            # 长周期反转 → 理论上立即离场旧方向（实盘白天仅提醒，是主要差异来源之一）
            if holding and holding != desired:
                do_close(i, closes[i], '长周期反转')
            open_window = (s0 == desired and s1 == desired and s2 == opp)
            close_window = (s0 == opp and s1 == opp and s2 == desired)
            if open_window and holding is None:
                do_open(i, desired, '开仓窗口')
            elif close_window and holding == desired:
                do_close(i, rev_px(i, exit_price_type), '平仓窗口')

        return trades, coverage_start

    # =================================================================
    # 第二步 + 第三步：匹配、归因、汇总
    # =================================================================

    @staticmethod
    def _pnl_pct(direction: str, entry_px: float, exit_px: float,
                 leverage: float) -> float:
        """含杠杆收益率%（与项目"当前盈亏"口径一致：幅度% × 杠杆）"""
        if not entry_px or not exit_px:
            return 0.0
        raw = (exit_px / entry_px - 1) * 100
        return round((raw if direction == 'long' else -raw) * leverage, 4)

    def _match_and_summarize(self, theory: List[Dict], actual: List[Dict],
                             short_period: str, leverage: float) -> Dict:
        window_min = _period_minutes(short_period) * _MATCH_WINDOW_BARS

        def within(sig_ts: str, act_ts: str) -> bool:
            try:
                t0 = datetime.datetime.strptime(sig_ts, '%Y-%m-%d %H:%M:%S')
                t1 = datetime.datetime.strptime(act_ts, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return False
            return datetime.timedelta(0) <= (t1 - t0) <= \
                datetime.timedelta(minutes=window_min)

        # --- 理论信号 → 实际成交匹配（只匹配 reason=signal 的流水） ---
        used = set()
        rows: List[Dict] = []
        for t in theory:
            hit = None
            for k, a in enumerate(actual):
                if k in used or classify_reason(a.get('reason')) != 'signal':
                    continue
                if a.get('action') == t['action'] and \
                        a.get('direction') == t['direction'] and \
                        within(t['time'], a.get('ts', '')):
                    hit = (k, a)
                    break
            if hit:
                k, a = hit
                used.add(k)
                slip = 0.0
                if t['price']:
                    slip = round((float(a.get('price', 0)) - t['price'])
                                 / t['price'] * 100, 4)
                rows.append({
                    'time': t['time'], 'action': t['action'],
                    'direction': t['direction'], 'status': 'matched',
                    'theory_price': round(t['price'], 8),
                    'actual_price': float(a.get('price', 0)),
                    'actual_ts': a.get('ts', ''),
                    'amount': float(a.get('amount', 0)),
                    'slippage_pct': slip, 'reason': 'signal',
                    'note': t.get('note', ''),
                })
            else:
                rows.append({
                    'time': t['time'], 'action': t['action'],
                    'direction': t['direction'], 'status': 'missed',
                    'theory_price': round(t['price'], 8),
                    'actual_price': None, 'actual_ts': '', 'amount': 0,
                    'slippage_pct': None, 'reason': '',
                    'note': t.get('note', ''),
                })

        # --- 未被匹配的实际成交 = 计划外（止盈/止损/强平/手动/窗口外成交） ---
        for k, a in enumerate(actual):
            if k in used:
                continue
            rows.append({
                'time': a.get('ts', ''), 'action': a.get('action', ''),
                'direction': a.get('direction', ''), 'status': 'extra',
                'theory_price': None,
                'actual_price': float(a.get('price', 0)),
                'actual_ts': a.get('ts', ''),
                'amount': float(a.get('amount', 0)),
                'slippage_pct': None,
                'reason': classify_reason(a.get('reason')),
                'note': a.get('reason', ''),
            })
        rows.sort(key=lambda r: r['time'])

        # --- 理论累计收益曲线（开→平配对） ---
        theory_curve, t_cum, t_entry, t_dir = [], 0.0, None, None
        for t in theory:
            if t['action'] == 'open':
                t_entry, t_dir = t['price'], t['direction']
            elif t['action'] == 'close' and t_entry:
                t_cum += self._pnl_pct(t_dir, t_entry, t['price'], leverage)
                theory_curve.append({'time': t['time'], 'cum_pnl': round(t_cum, 4)})
                t_entry = None

        # --- 实际累计收益曲线（按流水重建加权入场均价） ---
        actual_curve, a_cum = [], 0.0
        book = {'long': {'held': 0.0, 'avg': 0.0},
                'short': {'held': 0.0, 'avg': 0.0}}
        for a in actual:
            d = a.get('direction')
            if d not in book:
                continue
            px = float(a.get('price', 0) or 0)
            amt = float(a.get('amount', 0) or 0)
            b = book[d]
            if a.get('action') == 'open' and px > 0 and amt > 0:
                total = b['held'] + amt
                b['avg'] = (b['avg'] * b['held'] + px * amt) / total if total else 0.0
                b['held'] = total
            elif a.get('action') == 'close' and b['held'] > 0 and px > 0:
                a_cum += self._pnl_pct(d, b['avg'], px, leverage)
                actual_curve.append({'time': a.get('ts', ''),
                                     'cum_pnl': round(a_cum, 4)})
                b['held'] = max(0.0, b['held'] - amt)
                if b['held'] <= 0.01:
                    b['held'], b['avg'] = 0.0, 0.0

        # --- 汇总指标 ---
        matched = [r for r in rows if r['status'] == 'matched']
        missed = [r for r in rows if r['status'] == 'missed']
        extra = [r for r in rows if r['status'] == 'extra']
        slips = [abs(r['slippage_pct']) for r in matched
                 if r['slippage_pct'] is not None]
        extra_by_reason: Dict[str, int] = {}
        for r in extra:
            extra_by_reason[r['reason']] = extra_by_reason.get(r['reason'], 0) + 1

        summary = {
            'theory_signals': len(theory),
            'matched': len(matched),
            'missed': len(missed),
            'extra': len(extra),
            'exec_rate': round(len(matched) / len(theory) * 100, 2) if theory else None,
            'avg_slippage_pct': round(sum(slips) / len(slips), 4) if slips else None,
            'theory_cum_pnl': round(t_cum, 4),
            'actual_cum_pnl': round(a_cum, 4),
            'pnl_gap': round(a_cum - t_cum, 4),
            'extra_by_reason': extra_by_reason,
        }
        return {
            'summary': summary,
            'rows': rows,
            'theory_curve': theory_curve,
            'actual_curve': actual_curve,
        }


# 模块级单例（Flask 路由直接复用，避免重复加载策略模块）
_service: Optional[TrendCompareService] = None
_service_lock = threading.Lock()


def get_compare_service() -> TrendCompareService:
    global _service
    with _service_lock:
        if _service is None:
            _service = TrendCompareService()
        return _service
