#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
阿里云市场行情源 - 数据深度探测脚本
==================================

目的：回答三个问题
1. 最早能取到哪一年的数据？
2. 单个品种最多能取到多少根K线？
3. 最小能取到什么周期？

运行：
    python -m crypto.ApiUtils._probe_alicloud_depth
或
    python crypto/ApiUtils/_probe_alicloud_depth.py
"""

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alicloud_market import (  # noqa: E402
    AlicloudMarketClient, KLINE_PERIODS, to_pandas,
)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

SYMBOL = 'BTC'


def fmt(v, width=14):
    return ('%-' + str(width) + 's') % ('-' if v is None else v)


def main():
    client = AlicloudMarketClient()

    # 0. 连通性
    quotes = client.get_realtime_quotes([SYMBOL, 'ETH', 'LTC'])
    print('\n[0] 实时行情连通性检查')
    for q in quotes:
        print(f"    {q['S']:6s} {q['N']:12s} 最新价={q['P']} 时间={q['Time']}")

    # 1. 逐周期探测数据深度
    print(f'\n[1] {SYMBOL} 各周期数据深度探测（二分定位末尾页，不下载全量）')
    header = (f"    {fmt('周期', 6)}{fmt('总根数', 10)}{fmt('最早K线', 21)}"
              f"{fmt('最新K线', 21)}{fmt('覆盖年数', 10)}{fmt('间隔推算跨度')}")
    print(header)
    rows = []
    for cov in client.probe_periods(SYMBOL, KLINE_PERIODS, sleep=0.3):
        if 'error' in cov:
            print(f"    {fmt(cov['period'], 6)}探测失败：{cov['error']}")
            rows.append(cov)
            continue
        rows.append(cov)
        print(f"    {fmt(cov['period'], 6)}{fmt(cov['total_bars'], 10)}"
              f"{fmt(cov['earliest_time'], 21)}{fmt(cov['latest_time'], 21)}"
              f"{fmt(cov['years_covered'], 10)}{fmt(cov['span_days_by_interval'])}")

    # 2. 结论汇总
    ok = [r for r in rows if 'error' not in r]
    if ok:
        deepest = max(ok, key=lambda r: r['total_bars'])
        oldest = min(ok, key=lambda r: r['earliest_ts'])
        finest = min(ok, key=lambda r: KLINE_PERIODS.index(r['period']))
        print('\n[2] 结论')
        print(f"    最小可用周期：{finest['period']}（间隔 {finest['span_days_by_interval']} 天内，"
              f"共 {finest['total_bars']} 根）")
        print(f"    单周期最多K线：{deepest['period']} -> {deepest['total_bars']} 根")
        print(f"    最早可追溯到：{oldest['earliest_time']}（{oldest['period']} 周期，"
              f"约 {oldest['years_covered']} 年）")

    # 3. 抽样验证：拉一页 1M 看是否真的有分钟级数据 + pandas 转换
    print('\n[3] 最小周期抽样验证（1M 取最近3根）')
    bars = client.get_klines(SYMBOL, '1M', pidx=1, psize=3, withlast=0)
    df = to_pandas(bars)
    print(df.to_string(index=False))

    # 4. 多品种最早时间抽查（月线，最快看到历史起点）
    print('\n[4] 多品种最早月线抽查')
    for sym in (SYMBOL, 'ETH', 'LTC', 'XRP'):
        try:
            cov = client.get_data_coverage(sym, 'M', psize=200)
            if cov:
                print(f"    {sym:5s} 月线 {cov['total_bars']:4d} 根，最早 {cov['earliest_time']}"
                      f"，最新 {cov['latest_time']}")
            else:
                print(f'    {sym:5s} 无数据')
        except Exception as exc:  # noqa: BLE001
            print(f'    {sym:5s} 探测失败：{exc}')


if __name__ == '__main__':
    main()
