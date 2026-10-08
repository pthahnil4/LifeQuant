#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JumData 加密货币K线 - 分钟级数据深度探测
=========================================

_probe_jumdata.py 已确认：周期最小到 1m、单次上限 1000 根、日线可追溯到
2017-08-17（3330 根）。本脚本补齐关键缺口：**1m / 1h 这类细周期到底能往前
追到多久**，从而算出"单个周期最多能拿到多少根K线"。

原理：该接口的 endTime 语义是"返回严格早于等于 endTime 的最近 N 根"。
      因此 exists(T) = (取 endTime=T 能拿到数据) 随 T 单调，可用二分定位
      最早一根K线的时间（12 次请求收敛到 1 天精度）。

运行：python crypto/ApiUtils/_probe_jumdata_depth.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')

from jumdata_kline_utils import JumdataKlineClient, INTERVAL_MS  # noqa: E402

SYMBOL = 'BTC/USDT'
DAY_MS = 86_400_000
PROBE_INTERVALS = ['1m', '1h']


def exists_before(client: JumdataKlineClient, interval: str, end_ms: int) -> bool:
    """endTime=end_ms 时能否取到数据（能 = 该时间点之前还有K线）。"""
    bars = client.get_kline(SYMBOL, interval, end_time=end_ms, limit=1)
    if not bars:
        return False
    ts = JumdataKlineClient._bar_ts(bars[0])
    # 语义校验：返回的那根必须确实早于等于请求的 endTime
    return ts is not None and ts <= end_ms + 1000


def main():
    with JumdataKlineClient() as c:
        now = int(time.time() * 1000)

        # 0. 语义校验：endTime 在 2015 年（该交易对尚未上线）应取不到数据
        print('=' * 66)
        print('[0] endTime 语义校验')
        print('=' * 66)
        for label, end in (('2015-01-01', 1420070400000), ('当前时间', now)):
            bars = c.get_kline(SYMBOL, '1d', end_time=end, limit=1)
            ts = JumdataKlineClient._bar_ts(bars[0]) if bars else None
            print(f'  endTime={label:12s} -> 返回 {len(bars)} 根，'
                  f'该根时间={c._fmt(ts)}')

        # 1. 逐周期二分定位最早一根
        for iv in PROBE_INTERVALS:
            print('\n' + '=' * 66)
            print(f'[{iv}] 二分定位最早一根K线（12次请求）')
            print('=' * 66)
            left, right = now - 12 * 365 * DAY_MS, now
            steps = 0
            while right - left > DAY_MS and steps < 14:
                mid = (left + right) // 2
                if exists_before(c, iv, mid):
                    right = mid
                else:
                    left = mid
                steps += 1
                print(f'  step{steps:02d} 试探 {time.strftime("%Y-%m-%d", time.gmtime(mid/1000))}'
                      f' -> {"有数据" if right == mid else "无数据"}')
            span_days = (now - right) / DAY_MS
            total = int(span_days * 86_400_000 / INTERVAL_MS[iv])
            print(f'  => {iv} 最早可追到约 {time.strftime("%Y-%m-%d", time.gmtime(right/1000))}'
                  f'（距今 {span_days:.0f} 天），推算约 {total:,} 根，'
                  f'需翻页 {total // 1000:,} 次（单次上限1000根）')


if __name__ == '__main__':
    main()
