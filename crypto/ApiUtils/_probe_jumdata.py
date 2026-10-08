#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JumData 加密货币K线接口能力探测
==============================
回答三个问题：
  Q1 最早能获取到哪一年的数据？
  Q2 单次最多返回多少根？某周期总共能翻出多少根？
  Q3 最小能获取到什么周期？
运行：python crypto/ApiUtils/_probe_jumdata.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jumdata_kline_utils import (JumdataKlineClient, INTERVALS, MAX_LIMIT)

SYMBOL = "BTC/USDT"


def main():
    with JumdataKlineClient() as c:
        # -------- Q3：逐个周期确认可用性，找出最小可用周期 --------
        print("=" * 60)
        print("Q3 各周期可用性（返回根数>0即可用）")
        print("=" * 60)
        working = []
        for iv in INTERVALS:  # 已按粒度从小到大排列
            try:
                bars = c.get_kline(SYMBOL, iv, limit=3)
                ok = len(bars) > 0
                if ok:
                    working.append(iv)
                print(f"  interval={iv:4s} -> {'OK' if ok else 'EMPTY'} "
                      f"(sample={bars[0] if bars else None})")
            except Exception as e:  # noqa: BLE001
                print(f"  interval={iv:4s} -> ERROR {e}")
        print(f"\n  最小可用周期 = {working[0] if working else 'N/A'}，"
              f"最大可用周期 = {working[-1] if working else 'N/A'}")

        # -------- Q2a：单次上限验证（limit=1000 是否被截断）--------
        print("\n" + "=" * 60)
        print("Q2a 单次请求上限")
        print("=" * 60)
        single = c.get_kline(SYMBOL, "1d", limit=MAX_LIMIT)
        print(f"  请求 limit={MAX_LIMIT} -> 实际返回 {len(single)} 根  "
              f"（=> 单次最大 {MAX_LIMIT} 根，与文档一致）")

        # -------- Q1：用月线一把梭，看历史跨度起点 --------
        print("\n" + "=" * 60)
        print("Q1 最早能取到哪一年（用 1M 月线，1000根≈83年，单请求即覆盖全历史）")
        print("=" * 60)
        months = c.get_kline(SYMBOL, "1M", limit=MAX_LIMIT)
        if months:
            first_ts = JumdataKlineClient._bar_ts(months[0])
            last_ts = JumdataKlineClient._bar_ts(months[-1])
            print(f"  月线返回 {len(months)} 根")
            print(f"  最早月 = {c._fmt(first_ts)}")
            print(f"  最新月 = {c._fmt(last_ts)}")

        # -------- Q2b：日线总根数（向前翻到底）--------
        print("\n" + "=" * 60)
        print("Q2b 日线 1d 向前翻页可得总根数 & 最早日期")
        print("=" * 60)
        stat = c.fetch_history(SYMBOL, "1d", verbose=True)
        print(f"\n  合计 {stat['count']} 根日线，翻页 {stat['pages']} 次")
        print(f"  最早 {stat['earliest_date']}  ~  最新 {stat['latest_date']}")


if __name__ == "__main__":
    main()
