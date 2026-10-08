#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
止盈评估引擎（TakeProfitEngine）
================================
六类主止盈方式（单选互斥）+ 时间止盈独立兜底开关：

1. fixed_target 固定目标止盈
   - mode='pct'          盈利达到 pct% 平仓
   - mode='r_multiple'   盈利距离达到 N 倍 R（R=止损距离，止损未启用时 1R=1ATR）
   - mode='atr_multiple' 盈利距离达到 N 倍 ATR
2. trailing 追踪止盈
   - mode='pullback_pct' 盈利达到 activate_pct% 后，从峰值回撤 pullback_pct% 平仓
   - mode='chandelier'   吊灯：曾进入盈利后，价格从峰值回撤 N 倍 ATR 平仓
   - mode='r_ladder'     R乘数阶梯：盈利每达到 reach_r 倍R，锁定线抬到 lock_r 倍R，跌破锁定线平仓
3. momentum 动能衰竭止盈
   - mode='hist_decay'   MACD柱同向连续收缩 decay_bars 根 且盈利≥min_profit_pct% 平仓
   - mode='divergence'   MACD背离：价格创新高(低)但MACD柱峰值降低 且盈利≥min_profit_pct% 平仓
4. channel 通道边界止盈
   - mode='outer'        多头触及BOLL上轨 / 空头触及BOLL下轨（需处于盈利）平仓
   - mode='middle'       多头跌破BOLL中轨 / 空头升破BOLL中轨 且盈利≥min_profit_pct% 平仓
5. ladder 分批止盈
   - levels=[{pct, close_ratio}]：盈利达到各档 pct% 时按 close_ratio 比例平当前仓位，
     最后一档 close_ratio=1.0 表示清仓；已触发档位持久化，不重复触发
6. time_stop 时间止盈（独立兜底，可与任意主止盈共存）
   - 持仓超过 max_bars 根短周期K线且盈利仍 < min_profit_pct% → 平仓

评估优先级（由调用方保证）：止损 → 时间兜底 → 主止盈（本引擎内部为 时间兜底 → 主止盈）

运行时状态（峰值价/入场时间/已触发档位）持久化到 MySQL tp_runtime_state 表
（迁移批次5，原 tp_runtime_state.json），重启不丢失；持仓归零后由调用方
clear_state() 清理。

统一接口：
    engine = TakeProfitEngine()
    decision = engine.evaluate(tp_cfg, ctx)
    # decision = {'action': 'none'|'close_all'|'close_partial', 'close_ratio': float, 'reason': str}

交易所兜底委托：
    levels = engine.exchange_backup_levels(tp_cfg, sl_cfg, ctx)
    # levels = {'tp_trigger_px': float|None, 'sl_trigger_px': float|None, 'note': str}
    六类止盈中只有 fixed_target 与固定/ATR 止损能表达为交易所触发价，
    追踪、动能衰竭、通道边界、分批、时间兜底依赖运行时序列，必须本地评估。
    兜底委托的作用是防范程序宕机/网络中断期间的极端行情，不替代本地评估。

ctx 上下文字段：
    inst_id        合约ID
    is_long        bool 多头(True)/空头(False)
    pos_size       当前持仓张数（绝对值）
    avg_px         持仓均价
    current_price  最新价格
    pnl_pct        当前盈亏百分比（正=盈利）
    atr_value      当前 ATR（价格单位）
    boll_upper / boll_middle / boll_lower   短周期布林带（可为 0 表示不可用）
    macd_hist_series  短周期 MACD 柱序列（旧→新，list[float]，可为空）
    close_series      短周期收盘价序列（旧→新，list[float]，可为空）
    short_period      短周期字符串（'1m'/'5m'/'15m'/'1H'...，用于折算持仓K线数）
    sl_cfg            止损配置 dict（用于计算 R 距离）
"""

import time
import threading

# 运行时状态已迁移 MySQL（tp_runtime_state 表）：DB 不可用时仅静默降级，
# 与 JSON 版“状态落盘失败不影响交易主流程”的容错行为一致
try:
    from crypto.database import session_scope
    from crypto import trader_state_repo as state_repo
except ImportError:
    session_scope = None
    state_repo = None

# 周期 → 秒数（用于折算持仓K线根数）
_BAR_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1H': 3600, '2H': 7200, '4H': 14400, '6H': 21600, '12H': 43200,
    '1D': 86400,
}

VALID_CATEGORIES = ('none', 'fixed_target', 'trailing', 'momentum', 'channel', 'ladder', 'combo')


def normalize_tp_config(cfg):
    """归一化止盈配置，兼容旧版结构。

    旧版: {'enabled', 'type': 'fixed'/'trailing', 'fixed_pct', 'trailing_atr'}
    新版: {'enabled', 'category', 'fixed_target':{}, 'trailing':{}, 'momentum':{},
           'channel':{}, 'ladder':{}, 'time_stop':{}}
    """
    cfg = dict(cfg or {})
    if 'category' not in cfg and 'type' in cfg:
        # 旧结构映射：fixed → 固定百分比；trailing → 固定ATR倍数（旧实现即"盈利达N倍ATR即平"）
        old_type = cfg.get('type', 'fixed')
        if old_type == 'trailing':
            cfg['category'] = 'fixed_target'
            cfg['fixed_target'] = {'mode': 'atr_multiple',
                                   'atr_multiple': float(cfg.get('trailing_atr', 2.0))}
        else:
            cfg['category'] = 'fixed_target'
            cfg['fixed_target'] = {'mode': 'pct', 'pct': float(cfg.get('fixed_pct', 5.0))}
    if 'category' not in cfg:
        cfg['category'] = 'none'
    return cfg


class TakeProfitEngine:
    """六类止盈评估引擎（含运行时状态持久化）"""

    def __init__(self, state_file: str = None):
        """state_file 已废弃（保留签名兼容旧调用方）——状态已迁移 MySQL"""
        self.state_file = state_file  # 仅兼容保留，不再读写文件
        self._lock = threading.Lock()
        # 与 JSON 版一致：构造时整份加载（持仓状态行数极少）
        self._state = self._load_state()

    # ------------------------------------------------------------
    # 状态持久化（MySQL：迁移批次5）
    # ------------------------------------------------------------

    def _load_state(self):
        try:
            with session_scope() as session:
                return state_repo.load_tp_state(session)
        except Exception:
            pass
        return {}

    def _persist(self, key, st):
        """单 key 落库（evaluate 高频调用，只写变更的持仓状态行）"""
        try:
            with session_scope() as session:
                state_repo.upsert_tp_state(session, key, st)
        except Exception:
            pass  # 状态落库失败不影响交易主流程

    @staticmethod
    def _key(inst_id, is_long):
        return f"{inst_id}:{'long' if is_long else 'short'}"

    def clear_state(self, inst_id, is_long):
        """持仓归零后清理运行时状态（峰值/入场时间/已触发档位）"""
        with self._lock:
            key = self._key(inst_id, is_long)
            self._state.pop(key, None)
            try:
                with session_scope() as session:
                    state_repo.delete_tp_state(session, key)
            except Exception:
                pass

    def _get_or_init_state(self, ctx, persist=True):
        """获取/初始化当前持仓的运行时状态，并同步更新峰值与谷值

        persist=False 用于观察模式（ctx.observe_only）：只更新内存，
        不写入 DB，避免预览行为实际推进成交状态机。combo 分支内部会自行控制写入。
        """
        key = self._key(ctx['inst_id'], ctx['is_long'])
        price = float(ctx['current_price'])
        st = self._state.get(key)
        if st is None:
            st = {
                'entry_ts': time.time(),
                'peak': price,
                'trough': price,
                'avg_px': float(ctx.get('avg_px', 0)),
                'ladder_done': [],
                'extra': {},
            }
            self._state[key] = st
        # 峰值/谷值更新
        if price > st.get('peak', price):
            st['peak'] = price
        if price < st.get('trough', price):
            st['trough'] = price
        if persist:
            self._persist(key, st)
        return st

    # ------------------------------------------------------------
    # 辅助计算
    # ------------------------------------------------------------

    @staticmethod
    def _bar_seconds(period):
        return _BAR_SECONDS.get(str(period), 300)

    def _bars_held(self, st, ctx):
        """按入场时间折算持仓K线根数"""
        elapsed = max(0.0, time.time() - float(st.get('entry_ts', time.time())))
        return int(elapsed / self._bar_seconds(ctx.get('short_period', '5m')))

    @staticmethod
    def _r_distance(ctx):
        """R = 止损距离（价格单位）。止损未启用/不可算时回退 1R = 1ATR。"""
        sl = ctx.get('sl_cfg') or {}
        avg = float(ctx.get('avg_px', 0) or 0)
        atr = float(ctx.get('atr_value', 0) or 0)
        if sl.get('enabled'):
            if sl.get('type') == 'fixed' and avg > 0:
                return avg * float(sl.get('fixed_pct', 3.0)) / 100.0
            if sl.get('type') == 'atr' and atr > 0:
                return float(sl.get('atr_multiple', 1.5)) * atr
        return atr  # 兜底：1R = 1ATR（atr=0 时 R=0，调用处需判 R>0）

    @staticmethod
    def _none():
        return {'action': 'none', 'close_ratio': 0.0, 'reason': ''}

    @staticmethod
    def _close_all(reason):
        return {'action': 'close_all', 'close_ratio': 1.0, 'reason': reason}

    @staticmethod
    def _close_partial(ratio, reason):
        return {'action': 'close_partial', 'close_ratio': float(ratio), 'reason': reason}

    # ------------------------------------------------------------
    # 统一评估入口
    # ------------------------------------------------------------

    def evaluate(self, tp_cfg, ctx):
        """评估止盈。返回 {'action','close_ratio','reason'}。

        优先级：时间兜底 → 主止盈（止损由调用方在本方法之前处理）
        观察模式（ctx.observe_only=True）下不写入 DB、不推进 combo 成交状态机。
        """
        cfg = normalize_tp_config(tp_cfg)
        if not cfg.get('enabled', False):
            return self._none()

        observe_only = bool(ctx.get('observe_only'))
        with self._lock:
            st = self._get_or_init_state(ctx, persist=not observe_only)

            # 1) 时间止盈兜底（独立开关，可与任意主止盈共存）
            ts_cfg = cfg.get('time_stop') or {}
            if ts_cfg.get('enabled', False):
                decision = self._eval_time_stop(ts_cfg, st, ctx)
                if decision['action'] != 'none':
                    return decision

            # 2) 主止盈（六选一 + 组合出场）
            category = cfg.get('category', 'none')
            if category == 'fixed_target':
                return self._eval_fixed_target(cfg.get('fixed_target') or {}, ctx)
            if category == 'trailing':
                return self._eval_trailing(cfg.get('trailing') or {}, st, ctx)
            if category == 'momentum':
                return self._eval_momentum(cfg.get('momentum') or {}, ctx)
            if category == 'channel':
                return self._eval_channel(cfg.get('channel') or {}, ctx)
            if category == 'ladder':
                return self._eval_ladder(cfg.get('ladder') or {}, st, ctx)
            if category == 'combo':
                return self._eval_combo(cfg.get('combo') or {}, st, ctx,
                                        persist=not observe_only)
            return self._none()

    # ------------------------------------------------------------
    # 交易所侧兜底委托触发价推导
    # ------------------------------------------------------------

    def exchange_backup_levels(self, tp_cfg, sl_cfg, ctx):
        """推导可下发到交易所的止盈/止损触发价（本地评估的兜底保险）。

        能表达为交易所静态触发价的只有：
            止损 —— type='fixed'（均价百分比）/ type='atr'（均价±N倍ATR）
            止盈 —— category='fixed_target' 的三种 mode（pct / r_multiple / atr_multiple）
        追踪(峰值)、动能衰竭(MACD序列)、通道边界(实时BOLL)、分批(多档)、时间兜底
        都依赖运行时序列或状态，无法表达为静态触发价，只能由 evaluate() 本地评估。

        Returns:
            dict: {'tp_trigger_px': float|None, 'sl_trigger_px': float|None, 'note': str}
        """
        avg = float((ctx or {}).get('avg_px', 0) or 0)
        if avg <= 0:
            return {'tp_trigger_px': None, 'sl_trigger_px': None, 'note': '无持仓均价'}
        is_long = bool((ctx or {}).get('is_long'))
        atr = float((ctx or {}).get('atr_value', 0) or 0)
        sign = 1.0 if is_long else -1.0
        notes = []

        # ---- 止损触发价 ----
        sl_px = None
        sl = dict(sl_cfg or {})
        if sl.get('enabled'):
            sl_type = sl.get('type', 'fixed')
            if sl_type == 'fixed':
                sl_px = avg * (1 - sign * float(sl.get('fixed_pct', 3.0)) / 100.0)
            elif sl_type == 'atr' and atr > 0:
                sl_px = avg - sign * float(sl.get('atr_multiple', 1.5)) * atr
            else:
                notes.append(f'止损类型{sl_type}无法下发（ATR不可用）')

        # ---- 止盈触发价 ----
        tp_px = None
        cfg = normalize_tp_config(tp_cfg)
        if cfg.get('enabled'):
            category = cfg.get('category', 'none')
            if category == 'fixed_target':
                f = cfg.get('fixed_target') or {}
                mode = f.get('mode', 'pct')
                if mode == 'pct':
                    tp_px = avg * (1 + sign * float(f.get('pct', 5.0)) / 100.0)
                elif mode == 'r_multiple':
                    r = self._r_distance(ctx)
                    if r > 0:
                        tp_px = avg + sign * float(f.get('r_multiple', 2.0)) * r
                    else:
                        notes.append('R距离不可算，止盈未下发')
                elif mode == 'atr_multiple':
                    if atr > 0:
                        tp_px = avg + sign * float(f.get('atr_multiple', 3.0)) * atr
                    else:
                        notes.append('ATR不可用，止盈未下发')
            elif category != 'none':
                notes.append(f'{category}止盈依赖运行时状态，仅本地评估')

        # 触发价必须落在正确一侧，否则说明参数异常（如止损百分比≥100%）
        if sl_px is not None and sl_px <= 0:
            notes.append('止损触发价非正，已丢弃')
            sl_px = None
        if tp_px is not None and tp_px <= 0:
            notes.append('止盈触发价非正，已丢弃')
            tp_px = None

        return {'tp_trigger_px': tp_px, 'sl_trigger_px': sl_px,
                'note': '；'.join(notes)}

    # ------------------------------------------------------------
    # 各类止盈评估
    # ------------------------------------------------------------

    def _eval_time_stop(self, ts_cfg, st, ctx):
        """时间止盈：持仓超 max_bars 根K线且盈利仍 < min_profit_pct% → 平仓"""
        max_bars = int(ts_cfg.get('max_bars', 24))
        min_profit = float(ts_cfg.get('min_profit_pct', 1.0))
        bars = self._bars_held(st, ctx)
        pnl = float(ctx.get('pnl_pct', 0))
        if bars >= max_bars and pnl < min_profit:
            return self._close_all(
                f'时间止盈 持仓{bars}根K线≥{max_bars} 盈利{pnl:+.2f}%<{min_profit}%')
        return self._none()

    def _eval_fixed_target(self, fcfg, ctx):
        """固定目标：百分比 / R倍数 / ATR倍数"""
        mode = fcfg.get('mode', 'pct')
        pnl = float(ctx.get('pnl_pct', 0))
        if pnl <= 0:
            return self._none()
        avg = float(ctx.get('avg_px', 0) or 0)
        price = float(ctx.get('current_price', 0) or 0)
        profit_dist = abs(price - avg)

        if mode == 'pct':
            target = float(fcfg.get('pct', 5.0))
            if pnl >= target:
                return self._close_all(f'固定止盈 {pnl:.2f}%≥{target}%')
        elif mode == 'r_multiple':
            r = self._r_distance(ctx)
            n = float(fcfg.get('r_multiple', 2.0))
            if r > 0 and profit_dist >= n * r:
                return self._close_all(f'R倍数止盈 盈利距离{profit_dist:.4f}≥{n}R(R={r:.4f})')
        elif mode == 'atr_multiple':
            atr = float(ctx.get('atr_value', 0) or 0)
            n = float(fcfg.get('atr_multiple', 3.0))
            if atr > 0 and profit_dist >= n * atr:
                return self._close_all(f'ATR倍数止盈 盈利距离{profit_dist:.4f}≥{n}ATR({atr:.4f})')
        return self._none()

    def _eval_trailing(self, tcfg, st, ctx):
        """追踪止盈：回撤百分比 / 吊灯ATR / R乘数阶梯"""
        mode = tcfg.get('mode', 'pullback_pct')
        is_long = ctx['is_long']
        avg = float(ctx.get('avg_px', 0) or 0)
        price = float(ctx.get('current_price', 0) or 0)
        atr = float(ctx.get('atr_value', 0) or 0)
        peak = float(st.get('peak', price))
        trough = float(st.get('trough', price))
        if avg <= 0 or price <= 0:
            return self._none()

        if mode == 'pullback_pct':
            activate = float(tcfg.get('activate_pct', 1.0))
            pullback = float(tcfg.get('pullback_pct', 2.0))
            if is_long:
                peak_pnl = (peak - avg) / avg * 100
                dd = (peak - price) / peak * 100 if peak > 0 else 0
            else:
                peak_pnl = (avg - trough) / avg * 100
                dd = (price - trough) / trough * 100 if trough > 0 else 0
            if peak_pnl >= activate and dd >= pullback:
                return self._close_all(
                    f'追踪止盈(回撤%) 峰值盈利{peak_pnl:.2f}%≥{activate}% 回撤{dd:.2f}%≥{pullback}%')
        elif mode == 'chandelier':
            n = float(tcfg.get('chandelier_atr', 3.0))
            if atr > 0:
                if is_long and peak > avg and price <= peak - n * atr:
                    return self._close_all(
                        f'吊灯止盈 峰值{peak:.4f}回撤{peak - price:.4f}≥{n}ATR({atr:.4f})')
                if (not is_long) and trough < avg and price >= trough + n * atr:
                    return self._close_all(
                        f'吊灯止盈 谷值{trough:.4f}反弹{price - trough:.4f}≥{n}ATR({atr:.4f})')
        elif mode == 'r_ladder':
            r = self._r_distance(ctx)
            # levels: [[reach_r, lock_r], ...] 按 reach_r 升序
            levels = tcfg.get('r_levels') or [[1.0, 0.0], [2.0, 1.0], [3.0, 2.0]]
            if r > 0:
                if is_long:
                    peak_r = (peak - avg) / r
                else:
                    peak_r = (avg - trough) / r
                lock_r = None
                for lv in sorted(levels, key=lambda x: float(x[0])):
                    if peak_r >= float(lv[0]):
                        lock_r = float(lv[1])
                if lock_r is not None:
                    lock_line = avg + lock_r * r if is_long else avg - lock_r * r
                    hit = price <= lock_line if is_long else price >= lock_line
                    if hit:
                        return self._close_all(
                            f'R阶梯止盈 峰值{peak_r:.2f}R 锁定线{lock_r}R({lock_line:.4f}) 已触及')
        return self._none()

    def _eval_momentum(self, mcfg, ctx):
        """动能衰竭：MACD柱衰减 / MACD背离"""
        mode = mcfg.get('mode', 'hist_decay')
        min_profit = float(mcfg.get('min_profit_pct', 0.5))
        pnl = float(ctx.get('pnl_pct', 0))
        if pnl < min_profit:
            return self._none()
        hist = [float(x) for x in (ctx.get('macd_hist_series') or [])]
        is_long = ctx['is_long']

        if mode == 'hist_decay':
            decay_bars = int(mcfg.get('decay_bars', 3))
            if len(hist) < decay_bars + 1:
                return self._none()
            recent = hist[-(decay_bars + 1):]
            if is_long:
                # 多头：柱体仍为正但连续收缩
                ok = all(v > 0 for v in recent) and all(
                    recent[i + 1] < recent[i] for i in range(len(recent) - 1))
            else:
                # 空头：柱体仍为负但绝对值连续收缩
                ok = all(v < 0 for v in recent) and all(
                    abs(recent[i + 1]) < abs(recent[i]) for i in range(len(recent) - 1))
            if ok:
                return self._close_all(
                    f'MACD柱衰减止盈 连续{decay_bars}根收缩 盈利{pnl:+.2f}%')
        elif mode == 'divergence':
            closes = [float(x) for x in (ctx.get('close_series') or [])]
            lookback = int(mcfg.get('lookback', 30))
            n = min(len(hist), len(closes), lookback)
            if n < 10:
                return self._none()
            h = hist[-n:]
            c = closes[-n:]
            half = n // 2
            if is_long:
                # 价格创新高但MACD柱峰值降低 → 顶背离
                if max(c[half:]) > max(c[:half]) and max(h[half:]) < max(h[:half]):
                    return self._close_all(f'MACD顶背离止盈 价格新高但动能减弱 盈利{pnl:+.2f}%')
            else:
                # 价格创新低但MACD柱谷值抬高 → 底背离
                if min(c[half:]) < min(c[:half]) and min(h[half:]) > min(h[:half]):
                    return self._close_all(f'MACD底背离止盈 价格新低但动能减弱 盈利{pnl:+.2f}%')
        return self._none()

    def _eval_channel(self, ccfg, ctx):
        """通道边界：碰BOLL外轨 / 跌破BOLL中轨"""
        mode = ccfg.get('mode', 'outer')
        pnl = float(ctx.get('pnl_pct', 0))
        price = float(ctx.get('current_price', 0) or 0)
        upper = float(ctx.get('boll_upper', 0) or 0)
        middle = float(ctx.get('boll_middle', 0) or 0)
        lower = float(ctx.get('boll_lower', 0) or 0)
        is_long = ctx['is_long']

        if mode == 'outer':
            # 触及外轨（需处于盈利，避免开仓即在轨外时误平）
            if pnl <= 0:
                return self._none()
            if is_long and upper > 0 and price >= upper:
                return self._close_all(f'BOLL上轨止盈 价格{price:.4f}≥上轨{upper:.4f}')
            if (not is_long) and lower > 0 and price <= lower:
                return self._close_all(f'BOLL下轨止盈 价格{price:.4f}≤下轨{lower:.4f}')
        elif mode == 'middle':
            min_profit = float(ccfg.get('min_profit_pct', 0.0))
            if pnl < min_profit:
                return self._none()
            if is_long and middle > 0 and price < middle:
                return self._close_all(f'跌破BOLL中轨止盈 价格{price:.4f}<中轨{middle:.4f}')
            if (not is_long) and middle > 0 and price > middle:
                return self._close_all(f'升破BOLL中轨止盈 价格{price:.4f}>中轨{middle:.4f}')
        return self._none()

    def _eval_ladder(self, lcfg, st, ctx):
        """分批止盈：阶梯档位，已触发档位持久化不重复触发"""
        levels = lcfg.get('levels') or [
            {'pct': 2.0, 'close_ratio': 0.33},
            {'pct': 4.0, 'close_ratio': 0.5},
            {'pct': 6.0, 'close_ratio': 1.0},
        ]
        pnl = float(ctx.get('pnl_pct', 0))
        if pnl <= 0:
            return self._none()
        done = set(int(i) for i in st.get('ladder_done', []))
        # 收集本轮达到且未触发的档位（价格跳档时合并触发）
        reached = []
        for i, lv in enumerate(levels):
            if i in done:
                continue
            if pnl >= float(lv.get('pct', 0)):
                reached.append((i, lv))
        if not reached:
            return self._none()

        idx_list = [i for i, _ in reached]
        # 命中最后一档或合并比例≥1 → 清仓
        combined = min(1.0, sum(float(lv.get('close_ratio', 0)) for _, lv in reached))
        hit_last = any(i == len(levels) - 1 for i, _ in reached) or combined >= 0.999
        st['ladder_done'] = sorted(done.union(idx_list))
        self._persist(self._key(ctx['inst_id'], ctx['is_long']), st)

        pcts = '/'.join(f"{float(lv.get('pct', 0)):g}%" for _, lv in reached)
        if hit_last:
            return self._close_all(f'分批止盈清仓档 盈利{pnl:.2f}%达到{pcts}')
        return self._close_partial(combined, f'分批止盈 盈利{pnl:.2f}%达到{pcts} 平{combined * 100:.0f}%仓位')

    # ------------------------------------------------------------
    # 组合出场（固定初始风险 + 棘轮保护线 + 吊灯叠加 + 一次部分止盈）
    # ------------------------------------------------------------
    # 方案来源：双仓位出场与风控增强_6596c3cb.md
    #   1. 只在当前周期首次观察到持仓时锁定 P0/ATR0/R0（不随后续 ATR 变化而放宽）
    #   2. 多头保护线只升不降，空头只降不升（min/max 候选集内永远包含上一轮值）
    #   3. 吊灯作为额外候选，激活前不参与收紧（避免未盈利就被当开仓价 N 倍 ATR 拖回）
    #   4. 部分止盈只触发一次，pending → commit(filled) 两阶段提交，观察模式不推进
    #   5. Q0 在首次触发部分止盈时冻结（即当时实际建仓量）；部分止盈后调用方需
    #      自行拒绝同周期补回（trend_range_trader 会读取 partial_done 标志位）

    _COMBO_DEFAULTS = {
        'init_r_atr': 2.0,            # R0 = init_r_atr × ATR0
        'activate_r': 1.5,            # 最大有利变动 ≥ 1.5R 后激活追踪
        'lock_steps': [[1.5, 0.0], [2.5, 1.0], [4.0, 2.0]],  # [reach_r, lock_r]
        'chandelier_atr': 3.0,        # 吊灯候选线：峰值−N×ATR
        'partial_r': 2.0,             # 部分止盈触发 MFE
        'partial_ratio': 0.30,        # 部分止盈平 Q0 比例
        'breakeven_buffer_pct': 0.10,  # 保本价上浮 %，涵盖手续费/滑点
    }

    def _eval_combo(self, ccfg, st, ctx, persist=True):
        """组合出场评估：棘轮 + 吊灯单调保护线 + 一次部分止盈。

        ccfg 缺省字段自动补 _COMBO_DEFAULTS；ccfg=None 亦可。
        ctx 需带：inst_id/is_long/pos_size/avg_px/current_price/atr_value；
        可选 closed_close （上一已收盘 S 收盘价，供吊灯参考）。
        返回同 evaluate()，close_partial 后需调用方 commit_partial() 确认。
        """
        avg = float(ctx.get('avg_px', 0) or 0)
        price = float(ctx.get('current_price', 0) or 0)
        atr = float(ctx.get('atr_value', 0) or 0)
        pos = float(ctx.get('pos_size', 0) or 0)
        is_long = bool(ctx.get('is_long'))
        if avg <= 0 or price <= 0 or atr <= 0 or pos <= 0:
            return self._none()

        cfg = dict(self._COMBO_DEFAULTS)
        cfg.update(ccfg or {})

        ex = st.get('extra')
        if not isinstance(ex, dict):
            ex = {}
            st['extra'] = ex
        dirty = False

        # 当前周期首次进入 combo：锁定 P0/ATR0/R0/初始硬止损（S0）
        if not ex.get('locked'):
            r0 = float(cfg['init_r_atr']) * atr
            if r0 <= 0:
                return self._none()
            p0 = avg
            s0 = p0 - r0 if is_long else p0 + r0
            confirm = float(ctx.get('closed_close') or price)
            ex.update({
                'locked': True,
                'p0': p0,
                'atr0': atr,
                'r0': r0,
                'protect': s0,
                'lock_stage_r': None,
                'partial_done': False,
                'partial_pending': False,
                'q0': 0.0,          # 首次部分止盈触发时冻结
                'h_peak': max(p0, confirm, price),
                'l_trough': min(p0, confirm, price),
                'cycle_period': ctx.get('short_period'),
            })
            dirty = True
        else:
            # 吊灯参考量：优先用上一已收盘价，没提供时退而使用当前价（保持与 peak/trough 同步）
            confirm = float(ctx.get('closed_close') or price)
            if confirm > float(ex.get('h_peak', confirm)):
                ex['h_peak'] = confirm
                dirty = True
            if confirm < float(ex.get('l_trough', confirm)):
                ex['l_trough'] = confirm
                dirty = True

        p0 = float(ex['p0']); r0 = float(ex['r0'])
        protect = float(ex['protect'])
        h_peak = float(ex['h_peak']); l_trough = float(ex['l_trough'])
        if r0 <= 0:
            return self._none()

        # 最大有利变动（MFE）以 R 为单位
        mfe_r = (h_peak - p0) / r0 if is_long else (p0 - l_trough) / r0

        activate_r = float(cfg['activate_r'])
        chand_mult = float(cfg['chandelier_atr'])
        fee_buf_pct = float(cfg['breakeven_buffer_pct']) / 100.0

        # 棘轮台阶：取当前已达到的最高一档 lock_r，lock_r=0.0 代表保本（加费用缓冲）
        ratchet_line = None
        reached_lock = None
        for lv in sorted((cfg.get('lock_steps') or []), key=lambda x: float(x[0])):
            if len(lv) < 2:
                continue
            if mfe_r >= activate_r and mfe_r >= float(lv[0]):
                lock_r = float(lv[1])
                if lock_r == 0.0:
                    line = p0 * (1 + fee_buf_pct) if is_long else p0 * (1 - fee_buf_pct)
                else:
                    line = p0 + lock_r * r0 if is_long else p0 - lock_r * r0
                ratchet_line = line
                reached_lock = lock_r

        # 吊灯候选（仅激活后参与，防止初始未盈利区间被 ATR 拉回）
        chand_line = None
        if mfe_r >= activate_r and atr > 0:
            chand_line = h_peak - chand_mult * atr if is_long else l_trough + chand_mult * atr

        # 单调保护线：新候选 = max/min(上一轮, 棘轮, 吊灯) 与 S0。上一轮已含在候选集里，
        # 因此只会向有利方向推进，不会回拉。
        candidates = [protect]
        if ratchet_line is not None:
            candidates.append(ratchet_line)
        if chand_line is not None:
            candidates.append(chand_line)
        new_protect = max(candidates) if is_long else min(candidates)
        if new_protect != protect:
            ex['protect'] = new_protect
            dirty = True
        if reached_lock is not None and reached_lock != ex.get('lock_stage_r'):
            ex['lock_stage_r'] = reached_lock
            dirty = True

        # 触发保护线→全平（硬止损 / 棘轮 / 吊灯同一出口）
        hit = (price <= new_protect) if is_long else (price >= new_protect)
        if hit:
            if persist and dirty:
                self._persist(self._key(ctx['inst_id'], ctx['is_long']), st)
            src = '初始硬止损' if new_protect == (p0 - r0 if is_long else p0 + r0) else \
                  ('棘轮锁线' if (ratchet_line is not None and new_protect == ratchet_line) else
                   ('吊灯线' if (chand_line is not None and new_protect == chand_line) else '上一轮保护'))
            return self._close_all(
                f'组合出场触发({src}) 现价{price:.6g} {"≤" if is_long else "≥"}'
                f' 保护{new_protect:.6g} P0={p0:.6g} R0={r0:.6g} MFE={mfe_r:.2f}R')

        # 一次部分止盈（pending → commit 两阶段，pending 存在时不重复触发）
        partial_r = float(cfg['partial_r'])
        partial_ratio = float(cfg['partial_ratio'])
        if (not ex.get('partial_done')) and (not ex.get('partial_pending')) \
                and partial_ratio > 0 and mfe_r >= partial_r:
            q0 = float(ex.get('q0') or 0) or pos
            amt_q0 = q0 * partial_ratio
            if amt_q0 <= 0 or amt_q0 >= pos:
                # 当前持仓 <= Q0×ratio 时不拆（或残仓不足），直接交给追踪保护线处理
                pass
            else:
                ex['q0'] = q0
                # 观察模式（persist=False）：仅输出决策，不推进成交状态机，
                # 不置 partial_pending，避免预览行为让下一轮真实评估误以为已挂单
                if persist:
                    ex['partial_pending'] = True
                    self._persist(self._key(ctx['inst_id'], ctx['is_long']), st)
                ratio_now = amt_q0 / pos
                return self._close_partial(
                    min(1.0, ratio_now),
                    f'组合出场-部分止盈 MFE{mfe_r:.2f}R≥{partial_r}R '
                    f'平Q0({q0:g}张)的{partial_ratio*100:.0f}%={amt_q0:g}张'
                    f' 当前持仓{pos:g}张')

        if persist and dirty:
            self._persist(self._key(ctx['inst_id'], ctx['is_long']), st)
        return self._none()

    def commit_partial(self, inst_id, is_long, filled: bool):
        """部分止盈成交回调（两阶段提交）：

        - filled=True  → 本次部分止盈已落地，写 partial_done，释放 pending；同周期不再拆仓
        - filled=False → 下单失败/交易所拒，释放 pending，下轮可重试
        仅对 combo 分支有意义；非 combo 状态无 extra 时本函数为空操作。
        """
        with self._lock:
            key = self._key(inst_id, is_long)
            st = self._state.get(key)
            if not isinstance(st, dict):
                return False
            ex = st.get('extra')
            if not isinstance(ex, dict) or not ex:
                return False
            if filled:
                ex['partial_done'] = True
            ex['partial_pending'] = False
            self._persist(key, st)
            return True

    def is_partial_blocked(self, inst_id, is_long):
        """查询当前周期是否已因 combo 部分止盈成交而需禁止补回。"""
        with self._lock:
            st = self._state.get(self._key(inst_id, is_long)) or {}
            ex = st.get('extra') or {}
            return bool(isinstance(ex, dict) and ex.get('partial_done'))
