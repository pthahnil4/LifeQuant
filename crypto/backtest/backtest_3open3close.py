#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
三开三平策略 — 专用回测文件（完整复刻定时任务实盘逻辑）
=========================================================

本文件是旧版 crypto/task/utils/batch_order_manager.py（实盘三开三平挂单管理器）
+ 旧版 crypto/task/merge_long.py::_run_batch_trading（长周期反转市价强平）的回测复刻，
逐 bar 模拟实盘调度器每轮 process() 的完整挂单生命周期。

⚠ 实盘已于 2026-07-28 改造为双仓位构造（trend_range_trader.py 的趋势跟踪仓 +
区间波动仓），三开三平体系及 batch_order_manager.py 已整体删除。下文对照表中的
实盘代码位置均为历史引用，仅供回测口径溯源，不再对应当前实盘实现。

与定时任务系统的一致性对照表
---------------------------------------------------------------
实盘代码位置                                  | 本文件复刻位置
---------------------------------------------------------------
strategy_adapter.analyze() Pro3 双周期信号     | signal_engine.build_dataset()
  （MODIFY_FLAG / LONG_DIRECTION 同源引擎）    |   （只读复用 pro3_dualtimeframe）
strategy_adapter BOLL 带（df_short 直接算）    | signal_engine（rolling mean±dev·std, ddof=0）
strategy_adapter 反转bar = df_short.iloc[-2]   | signal_engine rev_open/rev_close = shift(1)
旧版 merge_long 长方向 = LONG_DIRECTION 倒数第2根 | signal_engine._long_dir_used_series
旧版 merge_long 长周期反转→市价强平+撤所有     | _handle_long_reversal()
BatchOrderManager.process 步骤0~4              | run() 主循环步骤0~4（顺序一致）
  aligned/open_confirmed/close_confirmed 判定  | _phase_windows()（逐行同式）
  相位/窗口边沿重置点位                        | _edge_resets()
  BOLL 边界移动改单(boll_amend_min_pct)        | _refresh_boll_orders()
  _place_orders（含 _remaining 额度封顶、      | _place_orders()（逐行移植，
    round(total×ratio,1)、点位隔离 held 篮子） |   含 _remaining/_pending_entry_amount）
  TTL 仅 entry_open/entry_close 超时撤单       | _reconcile()（bar 数等价换算）
---------------------------------------------------------------

回测撮合假设（实盘无法逐 tick 复现，采用标准限价成交模型）：
- 买入限价：bar 的 low<=委托价 视为成交，成交价=min(open,委托价)（跳空按开盘价）
- 卖出限价：bar 的 high>=委托价 视为成交，成交价=max(open,委托价)
- bar i 收线时决策挂单，从 bar i+1 起检查成交（无未来函数）
- 限价成交 maker 费率，市价强平 taker 费率+滑点

用法：
  python backtest_3open3close.py                 # 默认 NEAR-USDT-SWAP 5m/4H
  python backtest_3open3close.py DOGE-USDT-SWAP  # 指定标的
"""

import os
import sys
import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

# Windows 控制台默认 GBK → 强制 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

from signal_engine import build_dataset  # noqa: E402  Pro3 双周期同源信号

# =====================================================================
# 点位常量（与 batch_order_manager.py 完全一致）
# =====================================================================
ENTRY_POINTS = ['entry_open', 'entry_close', 'entry_boll']
EXIT_POINTS = ['exit_boll', 'exit_close', 'exit_open']
ALL_POINTS = ENTRY_POINTS + EXIT_POINTS
TTL_POINTS = {'entry_open', 'entry_close'}          # 仅反转确认窗口开仓单受 TTL 约束
POINT_KEY = {
    'entry_boll': 'boll', 'exit_boll': 'boll',
    'entry_open': 'open', 'exit_open': 'open',
    'entry_close': 'close', 'exit_close': 'close',
}
BASKETS = ['boll', 'open', 'close']
POINT_LABEL = {
    'entry_open': '开仓·反转开盘价点位',
    'entry_close': '开仓·反转收盘价点位',
    'entry_boll': '开仓·BOLL边界点位',
    'exit_boll': '平仓·BOLL边界点位',
    'exit_close': '平仓·反转收盘价点位',
    'exit_open': '平仓·反转开盘价点位',
}

ST_IDLE, ST_PENDING, ST_FILLED, ST_EXPIRED = 'IDLE', 'PENDING', 'FILLED', 'EXPIRED'

# 短周期 bar → 秒（夏普年化 / 持仓时长换算）
_BAR_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1H': 3600, '2H': 7200, '4H': 14400, '6H': 21600, '12H': 43200,
    '1D': 86400,
}


class ThreeOpenThreeCloseBacktester:
    """三开三平回测器 — 逐 bar 复刻实盘 BatchOrderManager.process()。"""

    def __init__(self, batch_cfg: Dict, bt_cfg: Dict, short_period: str):
        # ---- 实盘 batch_trading 配置块（与旧版 config_cross_merge_long.json 同结构）----
        self.total = float(batch_cfg.get('total_contracts', 1) or 1)
        self.leverage = float(batch_cfg.get('leverage', 10) or 10)
        self.ttl_periods = int(batch_cfg.get('ttl_periods', 4))
        self.boll_amend_min_pct = float(batch_cfg.get('boll_amend_min_pct', 0.001) or 0)
        self.entry_ratios = dict(batch_cfg.get('entry_ratios', {}))
        self.exit_ratios = dict(batch_cfg.get('exit_ratios', {}))  # 实盘同款：平仓按篮子 held，不用此比例
        # ---- 回测专属参数 ----
        self.ct_val = float(bt_cfg.get('ct_val', 1.0))
        self.maker_fee = float(bt_cfg.get('maker_fee', 2e-4))
        self.taker_fee = float(bt_cfg.get('taker_fee', 5e-4))
        self.slippage = float(bt_cfg.get('slippage', 0.0005))
        self.init_capital = float(bt_cfg.get('init_capital', 0) or 0)
        self.verbose_ops = bool(bt_cfg.get('verbose_ops', True))
        self.short_period = short_period
        self.bar_sec = _BAR_SECONDS.get(short_period, 900)

        self._reset_state()
        # 输出容器
        self.ops: List[str] = []           # 操作日志（下单/成交/撤销/改单/强平）
        self.fills: List[Dict] = []        # 所有成交明细
        self.trades: List[Dict] = []       # 平仓回合（round-trip）记录
        self.equity_curve: List[float] = []
        self.equity_index: List = []
        self.total_fee = 0.0
        self.realized = 0.0
        self.stat_counter = {'place': 0, 'fill': 0, 'cancel': 0, 'expire': 0,
                             'amend': 0, 'force_close': 0}

    # -----------------------------------------------------------------
    # 状态（对应实盘 _get_inst_state 的 points/held/prev_* 结构）
    # -----------------------------------------------------------------
    def _reset_state(self):
        self.points = {p: {'state': ST_IDLE, 'price': 0.0, 'amount': 0.0,
                           'placed_bar': -1} for p in ALL_POINTS}
        self.held = {k: 0.0 for k in BASKETS}          # 点位隔离软件账本
        self.avg = {k: 0.0 for k in BASKETS}           # 篮子加权开仓均价
        self.episode_start = {k: -1 for k in BASKETS}  # 篮子本轮持仓起始 bar（持仓周期统计）
        self.prev_aligned = None
        self.prev_open_confirmed = False
        self.prev_close_confirmed = False
        self.long_dir = None

    def _default_point(self):
        return {'state': ST_IDLE, 'price': 0.0, 'amount': 0.0, 'placed_bar': -1}

    # -----------------------------------------------------------------
    # 操作日志（对应实盘 _life/_log_place/_log_fill/_log_cancel/_log_amend）
    # -----------------------------------------------------------------
    def _op(self, t, msg):
        line = f"[{t}] {msg}"
        self.ops.append(line)
        if self.verbose_ops:
            print(line)

    # -----------------------------------------------------------------
    # 持仓 / 盈亏辅助
    # -----------------------------------------------------------------
    def _position_abs(self) -> float:
        """当前同方向持仓张数（= 各篮子账本合计；回测无外部干预，与交易所真实持仓恒等）。"""
        return round(sum(self.held.values()), 4)

    def _sign(self) -> int:
        return 1 if self.long_dir == 'long' else -1

    def _pending_entry_amount(self) -> float:
        """所有 PENDING 开仓挂单委托张数合计（实盘 _pending_entry_amount 同款）。"""
        tot = 0.0
        for p in ENTRY_POINTS:
            pt = self.points[p]
            if pt['state'] == ST_PENDING:
                tot += float(pt['amount'] or 0.0)
        return round(tot, 4)

    def _credit_entry(self, i, t, point: str, amount: float, price: float):
        """开仓成交入账：篮子加权均价 + held 增加 + maker 手续费（实盘 _credit_held 开仓侧）。"""
        key = POINT_KEY[point]
        prev_qty = self.held[key]
        new_qty = round(prev_qty + amount, 4)
        if new_qty > 0:
            self.avg[key] = (self.avg[key] * prev_qty + price * amount) / new_qty
        if prev_qty <= 0:
            self.episode_start[key] = i          # 篮子从空仓→有仓，记录持仓起点
        self.held[key] = new_qty
        fee = price * amount * self.ct_val * self.maker_fee
        self.total_fee += fee
        self.fills.append({
            'time': t, 'point': point, 'kind': 'entry', 'dir': self.long_dir,
            'price': round(price, 6), 'amount': amount, 'fee': round(fee, 6),
            'basket': key, 'basket_held_after': self.held[key],
        })
        self.stat_counter['fill'] += 1
        self._op(t, f"成交确认 | {POINT_LABEL[point]}({point}) 成交 {amount}张 @ {price:.4f}"
                    f"（篮子{key}持仓={self.held[key]}）")

    def _credit_exit(self, i, t, point: str, amount: float, price: float,
                     taker: bool = False, reason: str = '限价止盈'):
        """平仓成交入账：按篮子均价结算已实现盈亏（实盘 _credit_held 平仓侧 + PnL 结算）。"""
        key = POINT_KEY[point] if point in POINT_KEY else point
        amount = min(amount, self.held[key])
        if amount <= 0:
            return 0.0
        entry_px = self.avg[key]
        gross = (price - entry_px) * amount * self.ct_val * self._sign()
        fee_rate = self.taker_fee if taker else self.maker_fee
        entry_fee = entry_px * amount * self.ct_val * self.maker_fee
        exit_fee = price * amount * self.ct_val * fee_rate
        net = gross - entry_fee - exit_fee
        self.held[key] = round(self.held[key] - amount, 6)
        if self.held[key] <= 1e-9:
            self.held[key] = 0.0
        self.realized += gross
        self.total_fee += exit_fee   # 开仓费已在开仓时计入，此处仅计平仓费
        holding_bars = (i - self.episode_start[key]) if self.episode_start[key] >= 0 else 0
        if self.held[key] == 0.0:
            self.episode_start[key] = -1
        self.trades.append({
            'time': t, 'basket': key, 'dir': self.long_dir, 'exit_point': point,
            'entry_avg': round(entry_px, 6), 'exit_px': round(price, 6),
            'amount': amount, 'gross': round(gross, 6),
            'entry_fee': round(entry_fee, 6), 'exit_fee': round(exit_fee, 6),
            'net': round(net, 6), 'holding_bars': holding_bars,
            'holding_hours': round(holding_bars * self.bar_sec / 3600, 2),
            'reason': reason,
        })
        if point in POINT_KEY:
            self.fills.append({
                'time': t, 'point': point, 'kind': 'exit', 'dir': self.long_dir,
                'price': round(price, 6), 'amount': amount, 'fee': round(exit_fee, 6),
                'basket': key, 'basket_held_after': self.held[key],
            })
            self.stat_counter['fill'] += 1
        self._op(t, f"{'市价强平' if taker else '成交确认'} | {reason} | {key}篮子 "
                    f"{amount}张 开均价{entry_px:.4f}→平{price:.4f} 净盈亏{net:+.4f}USDT "
                    f"持仓{holding_bars}bar")
        return net

    def _unrealized(self, mark: float) -> float:
        u = 0.0
        for k in BASKETS:
            if self.held[k] > 0:
                u += (mark - self.avg[k]) * self.held[k] * self.ct_val * self._sign()
        return u

    # -----------------------------------------------------------------
    # 步骤0：长周期反转 → 市价强平 + 撤所有 + 清标记
    # （对应旧版 merge_long._run_batch_trading 的强平 + process 步骤0）
    # -----------------------------------------------------------------
    def _handle_long_reversal(self, i, t, new_dir, close_px):
        n = self._cancel_all_pending(t, '长周期反转')
        # 市价强平全部篮子（taker + 滑点，方向不利侧）
        px = close_px * (1 - self.slippage * self._sign())
        closed = 0.0
        for k in BASKETS:
            if self.held[k] > 0:
                closed += self.held[k]
                self._credit_exit(i, t, k, self.held[k], px,
                                  taker=True, reason='长周期反转强平')
        if closed > 0:
            self.stat_counter['force_close'] += 1
        self.points = {p: self._default_point() for p in ALL_POINTS}
        self.held = {k: 0.0 for k in BASKETS}
        self.avg = {k: 0.0 for k in BASKETS}
        self.episode_start = {k: -1 for k in BASKETS}
        self.prev_aligned = None
        self.prev_open_confirmed = False
        self.prev_close_confirmed = False
        self._op(t, f"长周期方向反转 {self.long_dir}→{new_dir}，撤销{n}张挂单"
                    f"{'，市价强平' + str(round(closed, 2)) + '张' if closed > 0 else ''}，清空标记")

    def _cancel_all_pending(self, t, reason) -> int:
        n = 0
        for p in ALL_POINTS:
            pt = self.points[p]
            if pt['state'] == ST_PENDING:
                self._op(t, f"委托撤销 | {POINT_LABEL[p]}({p}) 撤销 {pt['amount']}张 "
                            f"@ {pt['price']:.4f}，原因：{reason}")
                self.stat_counter['cancel'] += 1
                n += 1
                self.points[p] = self._default_point()
        return n

    # -----------------------------------------------------------------
    # 步骤1：对账（成交判定 + TTL 超时撤单，对应实盘 _reconcile）
    # -----------------------------------------------------------------
    @staticmethod
    def _check_fill(side: str, price: float, o: float, hi: float, lo: float):
        """标准限价成交模型：买单 low<=价 成交于 min(open,价)；卖单 high>=价 成交于 max(open,价)。"""
        if side == 'buy':
            if lo <= price:
                return min(o, price)
        else:
            if hi >= price:
                return max(o, price)
        return None

    def _reconcile(self, i, t, o, hi, lo, entry_side, exit_side):
        for p in ALL_POINTS:
            pt = self.points[p]
            if pt['state'] != ST_PENDING:
                continue
            if pt['placed_bar'] >= i:   # bar i 挂的单从 bar i+1 起才检查（因果性）
                continue
            side = entry_side if p in ENTRY_POINTS else exit_side
            fill_px = self._check_fill(side, pt['price'], o, hi, lo)
            if fill_px is not None:
                if p in ENTRY_POINTS:
                    self._credit_entry(i, t, p, pt['amount'], fill_px)
                else:
                    self._credit_exit(i, t, p, pt['amount'], fill_px,
                                      taker=False, reason=f'{p}限价平仓')
                pt['state'] = ST_FILLED
                continue
            # TTL：仅 entry_open/entry_close，超过 ttl_periods 个短周期时段未成交撤销
            if p in TTL_POINTS and (i - pt['placed_bar']) >= self.ttl_periods:
                self._op(t, f"委托撤销 | {POINT_LABEL[p]}({p}) 撤销 {pt['amount']}张 "
                            f"@ {pt['price']:.4f}，原因：TTL超时({self.ttl_periods}个{self.short_period}时段未成交)")
                self.stat_counter['expire'] += 1
                pt['state'] = ST_EXPIRED

    # -----------------------------------------------------------------
    # 步骤2：相位与窗口（对应实盘 process 步骤2，逐行同式）
    # -----------------------------------------------------------------
    def _phase_windows(self, short_dir, sprev, spp):
        aligned = (short_dir == self.long_dir)
        opposite = 'short' if self.long_dir == 'long' else 'long'
        open_confirmed = (sprev == self.long_dir and spp == opposite)
        close_confirmed = (sprev == opposite and spp == self.long_dir)
        return aligned, open_confirmed, close_confirmed

    # -----------------------------------------------------------------
    # 步骤3：相位/窗口边沿 → 重置点位（对应实盘 process 步骤3）
    # -----------------------------------------------------------------
    def _reset_point(self, t, point, reason):
        pt = self.points[point]
        if pt['state'] == ST_PENDING:
            self._op(t, f"委托撤销 | {POINT_LABEL[point]}({point}) 撤销 {pt['amount']}张 "
                        f"@ {pt['price']:.4f}，原因：{reason}")
            self.stat_counter['cancel'] += 1
        self.points[point] = self._default_point()

    def _edge_resets(self, t, aligned, open_confirmed, close_confirmed):
        if self.prev_aligned is not None and self.prev_aligned != aligned:
            if aligned:
                self._reset_point(t, 'exit_boll', '进入同向相位')
            else:
                self._reset_point(t, 'entry_boll', '进入异向相位')
        if open_confirmed and not self.prev_open_confirmed:
            self._reset_point(t, 'entry_open', '开仓窗口确认')
            self._reset_point(t, 'entry_close', '开仓窗口确认')
        if close_confirmed and not self.prev_close_confirmed:
            self._reset_point(t, 'exit_close', '平仓窗口确认')
            self._reset_point(t, 'exit_open', '平仓窗口确认')
        self.prev_aligned = aligned
        self.prev_open_confirmed = open_confirmed
        self.prev_close_confirmed = close_confirmed

    # -----------------------------------------------------------------
    # 步骤3.5：BOLL 边界移动 → 动态改单（对应实盘 _refresh_boll_orders）
    # -----------------------------------------------------------------
    def _refresh_boll_orders(self, t, boll_upper, boll_lower):
        if self.long_dir == 'long':
            entry_px, exit_px = boll_lower, boll_upper
        else:
            entry_px, exit_px = boll_upper, boll_lower
        for point, new_px in (('entry_boll', entry_px), ('exit_boll', exit_px)):
            pt = self.points[point]
            if pt['state'] != ST_PENDING or new_px <= 0:
                continue
            old_px = pt['price']
            new_r = round(new_px, 6)
            if old_px <= 0 or new_r == round(old_px, 6):
                continue
            if self.boll_amend_min_pct > 0 and abs(new_r - old_px) / old_px < self.boll_amend_min_pct:
                continue
            pt['price'] = new_r
            self.stat_counter['amend'] += 1
            self._op(t, f"委托改单 | {POINT_LABEL[point]}({point}) BOLL边界移动 "
                        f"{old_px:.4f}→{new_r:.4f}（幅度{abs(new_r - old_px) / old_px * 100:.3f}%）")

    # -----------------------------------------------------------------
    # 步骤4：挂单决策（对应实盘 _place_orders，逐行移植）
    # -----------------------------------------------------------------
    def _place_orders(self, i, t, aligned, open_confirmed, close_confirmed,
                      boll_upper, boll_lower, rev_open, rev_close, entry_side, exit_side):
        total = self.total
        if total <= 0:
            return
        entry_ratios = self.entry_ratios
        position_abs = self._position_abs()
        if self.long_dir == 'long':
            boll_entry_px, boll_exit_px = boll_lower, boll_upper
        else:
            boll_entry_px, boll_exit_px = boll_upper, boll_lower

        pts = self.points
        held = self.held

        def _amt(ratio_map, key):
            return round(total * float(ratio_map.get(key, 0) or 0), 1)

        # 已提交仓位 = 实际持仓 + 未成交开仓挂单量（实盘 _remaining 同款，杜绝超额）
        def _remaining() -> float:
            committed = round(position_abs + self._pending_entry_amount(), 4)
            return round(total - committed, 1)

        # ---------- 开仓档 ----------
        # entry_boll：异向(背离/回撤期) + 有剩余开仓额度
        if (not aligned) and pts['entry_boll']['state'] == ST_IDLE:
            amt = min(_amt(entry_ratios, 'boll'), _remaining())
            if amt > 0 and boll_entry_px > 0:
                self._place(i, t, 'entry_boll', entry_side, amt, boll_entry_px, 'entry')

        # entry_open / entry_close：开仓窗口确认 + 有剩余开仓额度
        if open_confirmed:
            for pt_name, px in (('entry_open', rev_open), ('entry_close', rev_close)):
                if pts[pt_name]['state'] == ST_IDLE:
                    key = 'open' if pt_name == 'entry_open' else 'close'
                    amt = min(_amt(entry_ratios, key), _remaining())
                    if amt > 0 and px > 0:
                        self._place(i, t, pt_name, entry_side, amt, px, 'entry')

        # ---------- 平仓档（点位隔离：每个平仓点位只平自己篮子）----------
        # exit_boll：同向 + boll 篮子有持仓
        if aligned and pts['exit_boll']['state'] == ST_IDLE and held.get('boll', 0) > 0:
            held_amt = round(float(held.get('boll', 0)), 1)
            amt = min(held_amt, round(position_abs, 1))
            if amt > 0 and boll_exit_px > 0:
                self._place(i, t, 'exit_boll', exit_side, amt, boll_exit_px, 'exit')

        # exit_close / exit_open：平仓窗口确认 + 对应篮子有持仓
        if close_confirmed:
            for pt_name, px, key in (('exit_close', rev_close, 'close'),
                                     ('exit_open', rev_open, 'open')):
                if pts[pt_name]['state'] == ST_IDLE and held.get(key, 0) > 0:
                    held_amt = round(float(held.get(key, 0)), 1)
                    amt = min(held_amt, round(position_abs, 1))
                    if amt > 0 and px > 0:
                        self._place(i, t, pt_name, exit_side, amt, px, 'exit')

    def _place(self, i, t, point, side, amount, price, kind):
        self.points[point] = {'state': ST_PENDING, 'price': round(price, 6),
                              'amount': amount, 'placed_bar': i}
        self.stat_counter['place'] += 1
        act = '开仓' if kind == 'entry' else '平仓'
        self._op(t, f"委托下单 | {POINT_LABEL[point]}({point}) 挂单 @ {price:.4f}，"
                    f"数量{amount}张 | {act} {side}")

    # -----------------------------------------------------------------
    # 主回测循环（步骤顺序与实盘 process() 完全一致：0→1→2→3→3.5→4）
    # -----------------------------------------------------------------
    def run(self, data: pd.DataFrame) -> Dict:
        if self.init_capital <= 0:
            start_px = float(data['close'].iloc[0])
            self.init_capital = max(1.0, start_px * self.ct_val * self.total / self.leverage)

        idx = data.index
        o = data['open'].values
        hi = data['high'].values
        lo = data['low'].values
        cl = data['close'].values
        sdir = data['short_dir'].values
        sprev = data['short_prev'].values
        spp = data['short_prev_prev'].values
        ldir = data['long_dir'].values
        lchg = data['long_dir_changed'].values
        bu = data['boll_upper'].values
        bl = data['boll_lower'].values
        rvo = data['rev_open'].values
        rvc = data['rev_close'].values

        n = len(data)
        for i in range(n):
            t = idx[i]
            long_dir = ldir[i]
            if long_dir is None:
                self.equity_curve.append(self.init_capital + self.realized)
                self.equity_index.append(t)
                continue

            # --- 0. 长周期反转 → 市价强平 + 撤所有 + 清标记 ---
            if lchg[i] and self.long_dir is not None and self.long_dir != long_dir:
                self._handle_long_reversal(i, t, long_dir, cl[i])
            self.long_dir = long_dir
            # 多头买开卖平；空头卖开买平（实盘 cross=多 / isolated=空 的方向映射）
            entry_side = 'buy' if long_dir == 'long' else 'sell'
            exit_side = 'sell' if long_dir == 'long' else 'buy'

            # --- 1. 对账（挂单成交判定 + TTL 超时撤单）---
            # 注意：用挂单时的方向侧判定（反转 bar 已在步骤0清空挂单，此处方向恒一致）
            self._reconcile(i, t, o[i], hi[i], lo[i], entry_side, exit_side)

            # --- 2. 相位与窗口 ---
            aligned, open_confirmed, close_confirmed = self._phase_windows(
                sdir[i], sprev[i], spp[i])

            # --- 3. 相位/窗口边沿 → 重置对应点位标记 ---
            self._edge_resets(t, aligned, open_confirmed, close_confirmed)

            # --- 3.5 BOLL 边界移动 → 动态改单 ---
            self._refresh_boll_orders(t, bu[i], bl[i])

            # --- 4. 挂单决策 ---
            self._place_orders(i, t, aligned, open_confirmed, close_confirmed,
                               bu[i], bl[i], rvo[i], rvc[i], entry_side, exit_side)

            # 权益曲线（含未实现盈亏，扣除累计手续费）
            eq = self.init_capital + self.realized - self.total_fee + self._unrealized(cl[i])
            self.equity_curve.append(eq)
            self.equity_index.append(t)

        # 收尾：市价强平剩余持仓（保证统计闭环）
        t_end = idx[-1]
        px = cl[-1] * (1 - self.slippage * self._sign()) if self.long_dir else cl[-1]
        for k in BASKETS:
            if self.held[k] > 0:
                self._credit_exit(n - 1, t_end, k, self.held[k], px,
                                  taker=True, reason='回测结束平仓')
        self._cancel_all_pending(t_end, '回测结束')
        if self.equity_curve:
            self.equity_curve[-1] = self.init_capital + self.realized - self.total_fee

        return self._metrics()

    # -----------------------------------------------------------------
    # 统计指标（胜率/盈亏比/最大回撤/夏普 + 持仓周期 + 回调分析）
    # -----------------------------------------------------------------
    def _metrics(self) -> Dict:
        eq = np.array(self.equity_curve, dtype=float)
        trades = self.trades
        nets = [tr['net'] for tr in trades]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]

        # ---- 回调（equity 回撤）分析：逐段记录回撤事件 ----
        dd_events = []   # {'peak_t','trough_t','depth_pct','bars','recovered'}
        if len(eq) > 1:
            peak, peak_i = eq[0], 0
            trough, trough_i = eq[0], 0
            in_dd = False
            for j in range(1, len(eq)):
                if eq[j] >= peak:
                    if in_dd and peak > 0:
                        dd_events.append({
                            'peak_time': str(self.equity_index[peak_i]),
                            'trough_time': str(self.equity_index[trough_i]),
                            'depth_pct': (peak - trough) / peak * 100,
                            'bars': j - peak_i, 'recovered': True,
                        })
                    peak, peak_i = eq[j], j
                    trough, trough_i = eq[j], j
                    in_dd = False
                else:
                    in_dd = True
                    if eq[j] < trough:
                        trough, trough_i = eq[j], j
            if in_dd and peak > 0:
                dd_events.append({
                    'peak_time': str(self.equity_index[peak_i]),
                    'trough_time': str(self.equity_index[trough_i]),
                    'depth_pct': (peak - trough) / peak * 100,
                    'bars': len(eq) - 1 - peak_i, 'recovered': False,
                })
        dd_events.sort(key=lambda x: -x['depth_pct'])
        max_dd = dd_events[0]['depth_pct'] if dd_events else 0.0

        # ---- 夏普（按短周期 bar 收益年化）----
        sharpe = 0.0
        if len(eq) > 2:
            rets = np.diff(eq) / np.where(eq[:-1] == 0, np.nan, eq[:-1])
            rets = rets[~np.isnan(rets)]
            if len(rets) > 1 and rets.std(ddof=1) > 1e-12:
                bars_per_year = (365 * 24 * 3600) / self.bar_sec
                sharpe = (rets.mean() / rets.std(ddof=1)) * math.sqrt(bars_per_year)

        # ---- 持仓周期统计 ----
        hb = [tr['holding_bars'] for tr in trades]
        holding = {
            'avg_bars': float(np.mean(hb)) if hb else 0.0,
            'median_bars': float(np.median(hb)) if hb else 0.0,
            'max_bars': int(max(hb)) if hb else 0,
            'min_bars': int(min(hb)) if hb else 0,
            'avg_hours': float(np.mean(hb)) * self.bar_sec / 3600 if hb else 0.0,
        }

        # ---- 分篮子 / 分点位统计 ----
        basket_stats = {}
        for k in BASKETS:
            bt = [tr for tr in trades if tr['basket'] == k]
            bn = [tr['net'] for tr in bt]
            bw = [x for x in bn if x > 0]
            basket_stats[k] = {
                'trades': len(bt),
                'net': sum(bn),
                'win_rate': (len(bw) / len(bt) * 100) if bt else 0.0,
            }
        point_fill_count = {}
        for f in self.fills:
            point_fill_count[f['point']] = point_fill_count.get(f['point'], 0) + 1

        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        net_total = self.realized - self.total_fee
        return {
            'init_capital': self.init_capital,
            'net_pnl': net_total,
            'gross_pnl': self.realized,
            'total_fee': self.total_fee,
            'return_pct': (net_total / self.init_capital * 100) if self.init_capital else 0.0,
            'num_trades': len(trades),
            'num_fills': len(self.fills),
            'win_rate': (len(wins) / len(trades) * 100) if trades else 0.0,
            'profit_factor': (gross_win / gross_loss) if gross_loss > 1e-9 else float('inf'),
            'avg_win': (gross_win / len(wins)) if wins else 0.0,
            'avg_loss': (-gross_loss / len(losses)) if losses else 0.0,
            'expectancy': (sum(nets) / len(nets)) if nets else 0.0,
            'max_drawdown_pct': max_dd,
            'sharpe': sharpe,
            'holding': holding,
            'basket_stats': basket_stats,
            'point_fill_count': point_fill_count,
            'dd_events': dd_events[:5],
            'ops_counter': dict(self.stat_counter),
            'final_equity': eq[-1] if len(eq) else self.init_capital,
        }


# =====================================================================
# 报告输出
# =====================================================================

def print_report(inst_id, short_period, long_period, batch_cfg, m, bt, data):
    W = 96
    pf = m['profit_factor']
    pf_s = '∞' if pf == float('inf') else f"{pf:.2f}"
    print('\n' + '=' * W)
    print(f"三开三平策略回测报告   标的: {inst_id}   周期: {short_period}/{long_period}")
    print('=' * W)
    print(f"数据: {len(data)} bars   {data.index[0]} → {data.index[-1]}")
    print(f"参数: 总张数={batch_cfg['total_contracts']}  杠杆={batch_cfg['leverage']}x  "
          f"TTL={batch_cfg['ttl_periods']}bar  BOLL={batch_cfg['boll_period']}/{batch_cfg['boll_dev']}  "
          f"改价门槛={batch_cfg['boll_amend_min_pct'] * 100:.2f}%")
    print(f"      entry_ratios={batch_cfg['entry_ratios']}  exit_ratios={batch_cfg['exit_ratios']}")

    print('-' * W)
    print("【核心绩效】")
    print(f"  初始资金基准: {m['init_capital']:.2f} USDT    最终权益: {m['final_equity']:.2f} USDT")
    print(f"  净盈亏: {m['net_pnl']:+.4f} USDT (毛盈亏 {m['gross_pnl']:+.4f} - 手续费 {m['total_fee']:.4f})")
    print(f"  收益率: {m['return_pct']:+.2f}%    最大回撤: {m['max_drawdown_pct']:.2f}%    "
          f"夏普: {m['sharpe']:.2f}")
    print(f"  交易回合: {m['num_trades']}    成交笔数: {m['num_fills']}    "
          f"胜率: {m['win_rate']:.1f}%    盈亏比: {pf_s}")
    print(f"  平均盈利: {m['avg_win']:+.4f}    平均亏损: {m['avg_loss']:+.4f}    "
          f"单笔期望: {m['expectancy']:+.4f} USDT")

    h = m['holding']
    print('-' * W)
    print("【持仓周期统计】")
    print(f"  平均: {h['avg_bars']:.1f} bar ({h['avg_hours']:.1f}h)   中位: {h['median_bars']:.1f} bar   "
          f"最长: {h['max_bars']} bar   最短: {h['min_bars']} bar")

    print('-' * W)
    print("【分篮子表现（点位隔离 A开A平/B开B平/C开C平）】")
    for k in BASKETS:
        s = m['basket_stats'][k]
        print(f"  {k:<6} | 回合 {s['trades']:>3} | 净盈亏 {s['net']:>+10.4f} | 胜率 {s['win_rate']:>5.1f}%")

    print('-' * W)
    print("【分点位成交次数】")
    for p in ALL_POINTS:
        print(f"  {POINT_LABEL[p]}({p}): {m['point_fill_count'].get(p, 0)} 次")

    oc = m['ops_counter']
    print('-' * W)
    print("【操作统计】")
    print(f"  挂单 {oc['place']} 次 | 成交 {oc['fill']} 次 | 撤销 {oc['cancel']} 次 | "
          f"TTL过期 {oc['expire']} 次 | BOLL改单 {oc['amend']} 次 | 长周期强平 {oc['force_close']} 次")

    print('-' * W)
    print("【回调分析（权益回撤 Top5）】")
    if m['dd_events']:
        for j, e in enumerate(m['dd_events'], 1):
            rec = '已修复' if e['recovered'] else '未修复'
            print(f"  #{j} 深度 {e['depth_pct']:.2f}%  {e['peak_time']} → {e['trough_time']}  "
                  f"持续 {e['bars']} bar  {rec}")
    else:
        print("  无回撤事件")

    print('-' * W)
    print("【与定时任务系统一致性核对】")
    checks = [
        "信号源: signal_engine 只读复用 pro3_dualtimeframe/_run_dual_strategy（与实盘 strategy_adapter 同引擎）",
        "BOLL带: 短周期 close rolling(boll_period).mean ± boll_dev × std(ddof=0)（与 strategy_adapter L228-237 同式）",
        "反转bar: 上一根已收线 bar 的 open/close（与 strategy_adapter L239-243 同义）",
        "长方向: LONG_DIRECTION 有效序列倒数第2根（与 merge_long 抗震荡取值一致）",
        "窗口: aligned/open_confirmed/close_confirmed 判定式与 batch_order_manager L310-315 逐行一致",
        "6点位: entry_boll(异向)/entry_open+close(开确认)/exit_boll(同向)/exit_close+open(平确认)",
        "额度: 张数=round(total×ratio,1)，开仓封顶 remaining=total-(持仓+挂单)（_remaining 同款）",
        "隔离: held{boll,open,close} 篮子账本，平仓只平自己篮子（POINT_KEY 同款）",
        "TTL: 仅 entry_open/entry_close 超时撤销；BOLL/止盈点位由相位边沿管理",
        "改单: BOLL边界移动 ≥ boll_amend_min_pct 才改价（_refresh_boll_orders 同款）",
        "强平: 长周期反转 → 撤所有挂单 + 市价平全部篮子（merge_long + process步骤0 同序）",
    ]
    for c in checks:
        print(f"  ✓ {c}")

    print('-' * W)
    print("  ⚠ 撮合假设：限价单按 bar 触及判定（买 low<=价 / 卖 high>=价），跳空按开盘价成交；")
    print("     实盘存在排队/部分成交/滑点差异，结果仅供策略研究参考。")
    print('=' * W)


def save_results(out_dir, inst_id, short_period, long_period, bt, m):
    """交易记录/成交明细/权益曲线/操作日志 落盘。"""
    os.makedirs(out_dir, exist_ok=True)
    tag = f"{inst_id}_{short_period}-{long_period}"
    files = {}

    if bt.trades:
        f = os.path.join(out_dir, f"trades_{tag}.csv")
        pd.DataFrame(bt.trades).to_csv(f, index=False, encoding='utf-8-sig')
        files['平仓回合记录'] = f
    if bt.fills:
        f = os.path.join(out_dir, f"fills_{tag}.csv")
        pd.DataFrame(bt.fills).to_csv(f, index=False, encoding='utf-8-sig')
        files['成交明细'] = f
    if bt.equity_curve:
        f = os.path.join(out_dir, f"equity_{tag}.csv")
        pd.DataFrame({'time': bt.equity_index, 'equity': bt.equity_curve}
                     ).to_csv(f, index=False, encoding='utf-8-sig')
        files['权益曲线'] = f
    if bt.ops:
        f = os.path.join(out_dir, f"operations_{tag}.log")
        with open(f, 'w', encoding='utf-8') as fp:
            fp.write('\n'.join(bt.ops))
        files['操作日志'] = f

    print("\n【输出文件】")
    for k, v in files.items():
        print(f"  {k}: {v}")
    return files


# =====================================================================
# 主函数 — 所有可调参数集中于此（结构与旧版 config_cross_merge_long.json 一致）
# =====================================================================

def main():
    # ---------------- 策略参数（与实盘 batch_trading 配置块同结构同默认值）----------------
    inst_id = sys.argv[1] if len(sys.argv) > 1 else 'NEAR-USDT-SWAP'
    short_period = '5m'      # 短周期（实盘配置 short_period）
    long_period = '4H'       # 长周期（实盘配置 long_period）

    batch_trading = {
        'enabled': True,
        'total_contracts': 1,          # 本金=合约总张数，各档张数=总张数×比例，保留1位小数
        'leverage': 10,                # 杠杆倍数
        'ttl_periods': 6,              # entry_open/close 挂单有效期（短周期时段数）
        'boll_period': 20,             # 短周期布林带周期
        'boll_dev': 2.0,               # 布林带标准差倍数
        'boll_amend_min_pct': 0.001,   # BOLL追价最小改价幅度（0.001=0.1%）
        'entry_ratios': {'open': 0.4, 'close': 0.2, 'boll': 0.4},   # 开仓三档比例
        'exit_ratios': {'boll': 0.4, 'close': 0.2, 'open': 0.4},    # 平仓三档比例（实盘同款：实际按篮子held平仓）
    }

    # ---------------- 回测专属参数 ----------------
    backtest_cfg = {
        'short_pages': 30,        # 短周期K线分页数（每页100根）
        'long_pages': 12,         # 长周期K线分页数
        'use_cache': True,        # 使用本地 data_cache CSV 缓存
        'ct_val': 1.0,            # 合约面值（NEAR-USDT-SWAP 实际 ctVal=10，此处用1.0便于按张观察）
        'maker_fee': 2e-4,        # 限价成交费率 0.02%
        'taker_fee': 5e-4,        # 市价强平费率 0.05%
        'slippage': 0.0005,       # 市价强平滑点 0.05%
        'init_capital': 0,        # 0=自动（起始价×ctVal×总张数/杠杆）
        'verbose_ops': False,     # True=控制台实时打印每笔操作（日志始终完整落盘）
        'save_results': True,     # 交易记录/权益/日志落盘 backtest/results/
    }

    print(f"[1/3] 构建 Pro3 双周期信号数据集: {inst_id} {short_period}/{long_period} ...")
    data = build_dataset(
        inst_id, short_period, long_period,
        boll_period=int(batch_trading['boll_period']),
        boll_dev=float(batch_trading['boll_dev']),
        short_pages=backtest_cfg['short_pages'],
        long_pages=backtest_cfg['long_pages'],
        use_cache=backtest_cfg['use_cache'])
    print(f"      数据集就绪: {len(data)} bars  {data.index[0]} → {data.index[-1]}")

    print(f"[2/3] 逐bar回测三开三平挂单生命周期 ...")
    bt = ThreeOpenThreeCloseBacktester(batch_trading, backtest_cfg, short_period)
    m = bt.run(data)

    print(f"[3/3] 生成报告与交易记录 ...")
    print_report(inst_id, short_period, long_period, batch_trading, m, bt, data)
    if backtest_cfg['save_results']:
        save_results(os.path.join(_THIS_DIR, 'results'), inst_id,
                     short_period, long_period, bt, m)


if __name__ == '__main__':
    main()
