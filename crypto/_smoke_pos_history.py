#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
主账号持仓盈亏查询（OKX 官方接口口径，严格区分两类）
==========================================================

1) 已实现盈亏：GET /api/v5/account/positions-history
   —— 交易所直接返回的官方数字（openAvgPx / closeAvgPx / pnl /
   pnlRatio / realizedPnl），非本地推算。分页取全量历史。
2) 浮动盈亏：GET /api/v5/account/positions
   —— 交易所直接返回的 upl（未实现盈亏）字段，基于实时标记价。

字段口径：
- pnl        : 已实现盈亏（不含手续费/资金费）
- realizedPnl: 净已实现盈亏 = pnl + fee + fundingFee + liqPenalty
- pnlRatio   : 已实现盈亏 / 开仓保证金（杠杆后收益率）
- type=1 完整平仓；type=2 部分平仓（仓位未了结，仍会继续产生盈亏）
"""

import sys
import os
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto.api_config import get_account_api

SIDE_MAP = {'long': '做多', 'short': '做空'}
TYPE_MAP = {'1': '完整平仓', '2': '部分平仓'}


def _fmt_ts(ms):
    if not ms:
        return ''
    return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M:%S')


def fetch_all_history(api):
    """分页取全量历史已了结仓位（after=本页最旧 uTime，翻页取更早记录）"""
    rows = []
    after = None
    for _ in range(50):  # 最多 50 页 × 100 条 = 5000 条，足够
        kwargs = {'instType': 'SWAP', 'limit': '100'}
        if after:
            kwargs['after'] = str(after)
        r = api.get_positions_history(**kwargs)
        if str(r.get('code')) != '0':
            raise RuntimeError(f"positions-history 接口报错: {r}")
        data = r.get('data') or []
        if not data:
            break
        rows.extend(data)
        after = min(int(x['uTime']) for x in data)
        if len(data) < 100:
            break
        time.sleep(0.25)  # 限速保护
    return rows


def main():
    api = get_account_api('main')

    # ── 第一类：已实现盈亏（OKX 官方接口直接返回）──
    print("=" * 118)
    print("第一类【已实现盈亏】 数据源: OKX GET /api/v5/account/positions-history（官方返回，非本地计算）")
    print("=" * 118)
    hist = fetch_all_history(api)
    if not hist:
        print("无任何历史已了结仓位。")
    else:
        hist.sort(key=lambda x: int(x['uTime']))
        header = (f"{'#':>3} {'instId':<16} {'方向':<4} {'模式':<8} {'杠杆':>4} "
                  f"{'开仓均价':>14} {'平仓均价':>14} {'已实现盈亏pnl':>14} {'盈亏比例':>9} "
                  f"{'净盈亏(含费)':>12} {'平仓时间':<19} {'类型'}")
        print(header)
        print('-' * 160)
        tot_pnl = tot_real = 0.0
        win = loss = 0
        for i, h in enumerate(hist, 1):
            pnl = float(h.get('pnl') or 0)
            real = float(h.get('realizedPnl') or 0)
            ratio = float(h.get('pnlRatio') or 0) * 100
            tot_pnl += pnl
            tot_real += real
            win += pnl > 0
            loss += pnl < 0
            print(f"{i:>3} {h['instId']:<16} {SIDE_MAP.get(h.get('direction'), h.get('direction')):<4} "
                  f"{h.get('mgnMode', ''):<8} {float(h.get('lever') or 0):>4.0f}x "
                  f"{float(h.get('openAvgPx') or 0):>14.6g} {float(h.get('closeAvgPx') or 0):>14.6g} "
                  f"{pnl:>+14.6f} {ratio:>+8.2f}% {real:>+12.6f} "
                  f"{_fmt_ts(h.get('uTime')):<19} {TYPE_MAP.get(h.get('type'), h.get('type'))}")
        print('-' * 160)
        print(f"已了结合计: {len(hist)} 笔 | 盈利 {win} / 亏损 {loss} | "
              f"累计 pnl = {tot_pnl:+.6f} USDT（不含费）| 累计净盈亏 realizedPnl = {tot_real:+.6f} USDT（含手续费+资金费）")

    # ── 第二类：浮动盈亏（get_positions 官方 upl 字段）──
    print()
    print("=" * 118)
    print("第二类【浮动盈亏】 数据源: OKX GET /api/v5/account/positions 的 upl 字段（实时标记价，官方返回）")
    print("=" * 118)
    r = api.get_positions(instType='SWAP')
    if str(r.get('code')) != '0':
        print(f"get_positions 接口报错: {r}")
        return
    poss = [p for p in (r.get('data') or []) if float(p.get('pos') or 0) != 0]
    if not poss:
        print("当前无任何持仓。")
        return
    header = (f"{'#':>3} {'instId':<16} {'方向':<4} {'模式':<8} {'持仓张数':>10} "
              f"{'开仓均价':>14} {'标记价格':>14} {'浮动盈亏upl':>13} {'浮动比例':>9} {'开仓时间':<19}")
    print(header)
    print('-' * 160)
    tot_upl = 0.0
    for i, p in enumerate(poss, 1):
        upl = float(p.get('upl') or 0)
        tot_upl += upl
        avg = float(p.get('avgPx') or 0)
        mark = float(p.get('markPx') or 0)
        upl_pct = float(p.get('uplRatio') or 0) * 100
        d = p.get('posSide') if p.get('posSide') in ('long', 'short') else \
            ('long' if float(p.get('pos') or 0) > 0 else 'short')
        print(f"{i:>3} {p['instId']:<16} {SIDE_MAP.get(d, d):<4} "
              f"{p.get('mgnMode', ''):<8} {p.get('pos', ''):>10} "
              f"{avg:>14.6g} {mark:>14.6g} {upl:>+13.6f} {upl_pct:>+8.2f}% "
              f"{_fmt_ts(p.get('cTime')):<19}")
    print('-' * 160)
    print(f"当前持仓合计: {len(poss)} 项 | 浮动盈亏合计 upl = {tot_upl:+.6f} USDT（实时变动，未了结不计入历史盈亏）")


if __name__ == '__main__':
    main()
