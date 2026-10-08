#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
分批限价挂单回测模拟引擎（batch_order_manager.py 的回测复刻）
===========================================================

逐bar复刻实盘 BatchOrderManager.process 的挂单生命周期：
  0) 长周期方向反转 → 市价强平净头寸 + 撤所有 + 清点位/篮子
  1) 对账：检查 PENDING 挂单是否成交（low<=price<=high）+ entry_open/close 的 TTL 超时撤单
  2) 相位/窗口计算：aligned / open_confirmed / close_confirmed
  3) 相位/窗口边沿 → 重置对应点位标记
  3.5) BOLL 边界移动 → 动态改单（更新委托价，boll_amend_min_pct 门槛）
  4) 挂单决策：三开(entry_boll/open/close) + 三平(exit_boll/close/open)

点位隔离（A开A平/B开B平/C开C平）：软件账本 held{boll,open,close} + 每篮子加权均价，
平仓点位只平自己篮子的持仓 → PnL 按对应篮子均价结算。

因果性：bar i 下的挂单从 bar i+1 起才检查成交（reconcile 在 place 之前运行）。

资金/成本：限价成交按 maker_fee，市价强平按 taker_fee；PnL 以 USDT 计价
（价差 × ctVal × 张数 × 方向符号）。权益曲线含未实现盈亏用于回撤/夏普。
"""

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


# 点位常量（与 batch_order_manager 对齐）
ENTRY_POINTS = ['entry_open', 'entry_close', 'entry_boll']
EXIT_POINTS = ['exit_boll', 'exit_close', 'exit_open']
ALL_POINTS = ENTRY_POINTS + EXIT_POINTS
TTL_POINTS = {'entry_open', 'entry_close'}
POINT_KEY = {
    'entry_boll': 'boll', 'exit_boll': 'boll',
    'entry_open': 'open', 'exit_open': 'open',
    'entry_close': 'close', 'exit_close': 'close',
}
BASKETS = ['boll', 'open', 'close']

ST_IDLE, ST_PENDING, ST_FILLED, ST_EXPIRED = 'IDLE', 'PENDING', 'FILLED', 'EXPIRED'

# bar → 秒（用于年化夏普）
_BAR_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1H': 3600, '2H': 7200, '4H': 14400, '6H': 21600, '12H': 43200,
    '1D': 86400,
}


class BatchBacktester:
    """双周期多点位分批限价交易回测器（复刻实盘挂单生命周期）。"""

    def __init__(self, cfg: Dict):
        """
        cfg 关键字段：
          total_contracts, leverage, ttl_periods, boll_amend_min_pct,
          entry_ratios{open,close,boll}, exit_ratios{boll,close,open},
          ct_val(合约面值，默认1.0), maker_fee(默认2e-4), taker_fee(默认5e-4),
          init_capital(可选；缺省=起始价×ctVal×总张数/杠杆),
          short_period(用于年化), dynamic_ratio(bool),
          slippage(市价强平滑点，默认0)
        """
        self.cfg = cfg
        self.total = float(cfg.get('total_contracts', 1) or 1)
        self.ttl = int(cfg.get('ttl_periods', 4))
        self.min_pct = float(cfg.get('boll_amend_min_pct', 0.001) or 0)
        self.ct_val = float(cfg.get('ct_val', 1.0))
        self.maker_fee = float(cfg.get('maker_fee', 2e-4))
        self.taker_fee = float(cfg.get('taker_fee', 5e-4))
        self.leverage = float(cfg.get('leverage', 10) or 10)
        self.slippage = float(cfg.get('slippage', 0.0))
        self.dynamic = bool(cfg.get('dynamic_ratio', False))
        self.short_period = cfg.get('short_period', '15m')

        self.entry_ratios = dict(cfg.get('entry_ratios', {'open': 0.3, 'close': 0.3, 'boll': 0.4}))
        self.exit_ratios = dict(cfg.get('exit_ratios', {'boll': 0.4, 'close': 0.3, 'open': 0.3}))

        # 运行态
        self._reset_state()
        self.trades: List[Dict] = []      # 平仓成交（round-trip）记录
        self.fills: List[Dict] = []       # 所有成交明细
        self.equity_curve: List[float] = []
        self.equity_index: List = []
        self.total_fee = 0.0
        self.realized = 0.0
        self.init_capital = float(cfg.get('init_capital', 0) or 0)

    def _reset_state(self):
        self.points = {p: {'state': ST_IDLE, 'price': 0.0, 'amount': 0.0,
                           'placed_bar': -1} for p in ALL_POINTS}
        self.held = {k: 0.0 for k in BASKETS}
        self.avg = {k: 0.0 for k in BASKETS}   # 篮子加权均价
        self.prev_aligned = None
        self.prev_open_confirmed = False
        self.prev_close_confirmed = False
        self.long_dir = None

    # -----------------------------------------------------------------
    # 比例（静态 or 基于 ADX regime 动态）
    # -----------------------------------------------------------------
    def _entry_ratio_map(self, adx: float) -> Dict:
        if not self.dynamic:
            return self.entry_ratios
        # ADX regime 动态调整：
        #  强趋势(ADX>=30) → 侧重反转确认开仓(open/close)，弱化布林抄底
        #  震荡(ADX<20)    → 侧重布林边界(boll)均值回归
        #  过渡(20~30)     → 默认均衡
        if adx >= 30:
            return {'boll': 0.2, 'open': 0.3, 'close': 0.5}
        if adx < 20:
            return {'boll': 0.5, 'open': 0.3, 'close': 0.2}
        return {'boll': 0.4, 'open': 0.3, 'close': 0.3}

    # -----------------------------------------------------------------
    # 持仓/PnL 辅助
    # -----------------------------------------------------------------
    def _position_abs(self) -> float:
        return round(sum(self.held.values()), 4)

    def _sign(self) -> int:
        return 1 if self.long_dir == 'long' else -1

    def _credit_entry(self, key: str, amount: float, price: float):
        """entry 成交 → 篮子加权均价更新 + 持仓增加 + maker 手续费。"""
        prev_qty = self.held[key]
        new_qty = prev_qty + amount
        if new_qty > 0:
            self.avg[key] = (self.avg[key] * prev_qty + price * amount) / new_qty
        self.held[key] = round(new_qty, 6)
        fee = price * amount * self.ct_val * self.maker_fee
        self.total_fee += fee
        return fee

    def _credit_exit(self, key: str, amount: float, price: float, taker: bool = False):
        """exit 成交 → 按篮子均价结算已实现盈亏 - 双边手续费。返回该笔净盈亏。"""
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
        self.total_fee += exit_fee   # 入场费已在开仓时计入；此处仅计出场费
        self.trades.append({
            'basket': key, 'dir': self.long_dir, 'entry': entry_px, 'exit': price,
            'amount': amount, 'gross': gross, 'entry_fee': entry_fee,
            'exit_fee': exit_fee, 'net': net,
        })
        return net

    def _unrealized(self, mark: float) -> float:
        u = 0.0
        for k in BASKETS:
            if self.held[k] > 0:
                u += (mark - self.avg[k]) * self.held[k] * self.ct_val * self._sign()
        return u

    def _force_close(self, price: float, reason: str):
        """市价强平全部篮子（taker + 滑点）。"""
        px = price * (1 - self.slippage * self._sign())
        for k in BASKETS:
            if self.held[k] > 0:
                self._credit_exit(k, self.held[k], px, taker=True)
        for p in ALL_POINTS:
            self.points[p] = {'state': ST_IDLE, 'price': 0.0, 'amount': 0.0, 'placed_bar': -1}

    # -----------------------------------------------------------------
    # 点位重置
    # -----------------------------------------------------------------
    def _reset_point(self, point: str):
        self.points[point] = {'state': ST_IDLE, 'price': 0.0, 'amount': 0.0, 'placed_bar': -1}

    # -----------------------------------------------------------------
    # 主回测循环
    # -----------------------------------------------------------------
    def run(self, data: pd.DataFrame) -> Dict:
        self._reset_state()
        self.trades.clear()
        self.fills.clear()
        self.equity_curve.clear()
        self.equity_index.clear()
        self.total_fee = 0.0
        self.realized = 0.0

        if self.init_capital <= 0:
            start_px = float(data['close'].iloc[0])
            self.init_capital = max(1.0, start_px * self.ct_val * self.total / self.leverage)

        idx = data.index
        o = data['open'].values
        hi = data['high'].values
        lo = data['low'].values
        cl = data['close'].values
        adx = data['ADX'].values
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
            long_dir = ldir[i]
            short_dir = sdir[i]
            if long_dir is None:
                self.equity_curve.append(self.init_capital + self.realized)
                self.equity_index.append(idx[i])
                continue

            # --- 0. 长周期反转 → 市价强平 + 撤所有 + 清标记 ---
            if lchg[i] and self.long_dir is not None and self.long_dir != long_dir:
                self._force_close(cl[i], '长周期反转')
                self.prev_aligned = None
                self.prev_open_confirmed = False
                self.prev_close_confirmed = False
            self.long_dir = long_dir

            # --- 1. 对账（成交判定 + TTL 超时）---
            for p in ALL_POINTS:
                pt = self.points[p]
                if pt['state'] != ST_PENDING:
                    continue
                if pt['placed_bar'] >= i:   # bar i 下的单从 i+1 起才检查
                    continue
                price = pt['price']
                if lo[i] <= price <= hi[i]:
                    key = POINT_KEY[p]
                    if p in ENTRY_POINTS:
                        fee = self._credit_entry(key, pt['amount'], price)
                        self.fills.append({'bar': idx[i], 'point': p, 'kind': 'entry',
                                           'price': price, 'amount': pt['amount'], 'fee': fee})
                    else:
                        net = self._credit_exit(key, pt['amount'], price)
                        self.fills.append({'bar': idx[i], 'point': p, 'kind': 'exit',
                                           'price': price, 'amount': pt['amount'], 'net': net})
                    pt['state'] = ST_FILLED
                    continue
                # TTL 超时（仅 entry_open/entry_close）
                if p in TTL_POINTS and (i - pt['placed_bar']) >= self.ttl:
                    pt['state'] = ST_EXPIRED

            # --- 2. 相位/窗口 ---
            aligned = (short_dir == long_dir)
            opposite = 'short' if long_dir == 'long' else 'long'
            open_confirmed = (sprev[i] == long_dir and spp[i] == opposite)
            close_confirmed = (sprev[i] == opposite and spp[i] == long_dir)

            # --- 3. 相位/窗口边沿 → 重置点位 ---
            if self.prev_aligned is not None and self.prev_aligned != aligned:
                if aligned:
                    self._reset_point('exit_boll')
                else:
                    self._reset_point('entry_boll')
            if open_confirmed and not self.prev_open_confirmed:
                self._reset_point('entry_open')
                self._reset_point('entry_close')
            if close_confirmed and not self.prev_close_confirmed:
                self._reset_point('exit_close')
                self._reset_point('exit_open')
            self.prev_aligned = aligned
            self.prev_open_confirmed = open_confirmed
            self.prev_close_confirmed = close_confirmed

            # --- 3.5 BOLL 动态改单 ---
            if long_dir == 'long':
                b_entry, b_exit = bl[i], bu[i]
            else:
                b_entry, b_exit = bu[i], bl[i]
            for point, new_px in (('entry_boll', b_entry), ('exit_boll', b_exit)):
                pt = self.points[point]
                if pt['state'] != ST_PENDING or new_px <= 0:
                    continue
                old_px = pt['price']
                if old_px <= 0:
                    continue
                if self.min_pct > 0 and abs(new_px - old_px) / old_px < self.min_pct:
                    continue
                pt['price'] = round(new_px, 6)

            # --- 4. 挂单决策 ---
            self._place_orders(i, aligned, open_confirmed, close_confirmed,
                               adx[i], b_entry, b_exit, rvo[i], rvc[i])

            # 权益曲线（含未实现）
            eq = self.init_capital + self.realized - 0.0 + self._unrealized(cl[i])
            self.equity_curve.append(eq)
            self.equity_index.append(idx[i])

        # 收尾：强平剩余持仓
        self._force_close(cl[-1], '回测结束平仓')
        # 修正最终权益点（已全部实现）
        if self.equity_curve:
            self.equity_curve[-1] = self.init_capital + self.realized

        return self._metrics()

    def _place_orders(self, i, aligned, open_confirmed, close_confirmed,
                      adx_v, b_entry, b_exit, rev_open, rev_close):
        total = self.total
        if total <= 0:
            return
        position_abs = self._position_abs()
        ratios = self._entry_ratio_map(adx_v)

        def amt(key):
            return round(total * float(ratios.get(key, 0) or 0), 1)

        pts = self.points

        # 开仓：entry_boll（异向 + 未达目标）
        if (not aligned) and pts['entry_boll']['state'] == ST_IDLE and position_abs < total:
            a = amt('boll')
            if a > 0 and b_entry > 0:
                self._place(i, 'entry_boll', a, b_entry)
        # 开仓：entry_open/entry_close（开仓窗口确认 + 未达目标）
        if open_confirmed and position_abs < total:
            for pt_name, px, key in (('entry_open', rev_open, 'open'),
                                     ('entry_close', rev_close, 'close')):
                if pts[pt_name]['state'] == ST_IDLE:
                    a = amt(key)
                    if a > 0 and px > 0:
                        self._place(i, pt_name, a, px)

        # 平仓（点位隔离：只平自己篮子）
        if aligned and pts['exit_boll']['state'] == ST_IDLE and self.held['boll'] > 0:
            a = min(round(self.held['boll'], 1), round(position_abs, 1))
            if a > 0 and b_exit > 0:
                self._place(i, 'exit_boll', a, b_exit)
        if close_confirmed:
            for pt_name, px, key in (('exit_close', rev_close, 'close'),
                                     ('exit_open', rev_open, 'open')):
                if pts[pt_name]['state'] == ST_IDLE and self.held[key] > 0:
                    a = min(round(self.held[key], 1), round(position_abs, 1))
                    if a > 0 and px > 0:
                        self._place(i, pt_name, a, px)

    def _place(self, i, point, amount, price):
        self.points[point] = {'state': ST_PENDING, 'price': round(price, 6),
                              'amount': amount, 'placed_bar': i}

    # -----------------------------------------------------------------
    # 指标
    # -----------------------------------------------------------------
    def _metrics(self) -> Dict:
        eq = np.array(self.equity_curve, dtype=float)
        trades = self.trades
        nets = [t['net'] for t in trades]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]

        # 最大回撤
        max_dd = 0.0
        if len(eq) > 0:
            peak = eq[0]
            for v in eq:
                peak = max(peak, v)
                if peak > 0:
                    dd = (peak - v) / peak
                    max_dd = max(max_dd, dd)

        # 夏普（按bar收益年化）
        sharpe = 0.0
        if len(eq) > 2:
            rets = np.diff(eq) / np.where(eq[:-1] == 0, np.nan, eq[:-1])
            rets = rets[~np.isnan(rets)]
            if len(rets) > 1 and rets.std(ddof=1) > 1e-12:
                bar_sec = _BAR_SECONDS.get(self.short_period, 900)
                bars_per_year = (365 * 24 * 3600) / bar_sec
                sharpe = (rets.mean() / rets.std(ddof=1)) * math.sqrt(bars_per_year)

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
            'win_rate': (len(wins) / len(trades) * 100) if trades else 0.0,
            'profit_factor': (gross_win / gross_loss) if gross_loss > 1e-9 else float('inf'),
            'avg_win': (gross_win / len(wins)) if wins else 0.0,
            'avg_loss': (-gross_loss / len(losses)) if losses else 0.0,
            'max_drawdown_pct': max_dd * 100,
            'sharpe': sharpe,
            'num_fills': len(self.fills),
            'final_equity': eq[-1] if len(eq) else self.init_capital,
        }
