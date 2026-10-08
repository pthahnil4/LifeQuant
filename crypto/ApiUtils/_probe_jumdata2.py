#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测2：5m历史深度 / 支持币种范围 / 数据源平台比对。"""
import sys
import os
import time
import calendar
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import httpx
from jumdata_kline_utils import JumdataKlineClient, MAX_LIMIT

PAIRS_XLSX = "https://file.jumdata.com/api-document/crypto/%E6%94%AF%E6%8C%81%E7%9A%84%E4%BA%A4%E6%98%93%E5%AF%B9.xlsx"


def q_5m_depth(c):
    print("=" * 60)
    print("Q1  5m 能否追到 2017 年？")
    print("=" * 60)
    # 直接把 endTime 设到 2017-08-20，看能否取回那一周的 5m 数据
    end = int(calendar.timegm((2017, 8, 20, 0, 0, 0, 0, 0, 0)) * 1000)
    bars = c.get_kline("BTC/USDT", "5m", end_time=end, limit=MAX_LIMIT)
    if bars:
        e = JumdataKlineClient._bar_ts(bars[0])
        l = JumdataKlineClient._bar_ts(bars[-1])
        print(f"  endTime=2017-08-20 前取回 {len(bars)} 根 5m")
        print(f"  区间 {c._fmt(e)} ~ {c._fmt(l)}")
        print(f"  => 5m 最早可追到 {time.strftime('%Y-%m-%d', time.gmtime(e/1000))}")
    else:
        print("  2017-08 无 5m 数据")


def q_supported(c):
    print("\n" + "=" * 60)
    print("Q2  支持的交易对范围（下载官方 xlsx 清单）")
    print("=" * 60)
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as hc:
            r = hc.get(PAIRS_XLSX)
        print(f"  下载状态 {r.status_code}，字节 {len(r.content)}")
        open(os.path.join(os.path.dirname(__file__), "_pairs.xlsx"), "wb").write(r.content)
        import openpyxl
        wb = openpyxl.load_workbook(os.path.join(os.path.dirname(__file__), "_pairs.xlsx"), read_only=True)
        ws = wb.active
        rows = [row for row in ws.iter_rows(values_only=True)]
        header = rows[0]
        data = [x for x in rows[1:] if x and x[0]]
        print(f"  表头: {header}")
        print(f"  交易对总数（不含表头）: {len(data)}")
        print(f"  前5行: {data[:5]}")
        print(f"  后5行: {data[-5:]}")
    except Exception as ex:  # noqa: BLE001
        print(f"  xlsx 获取/解析失败：{ex}")


def q_source(c):
    print("\n" + "=" * 60)
    print("Q3  数据源平台：取一根已完成的1h K线，与币安/OKX逐字段比对")
    print("=" * 60)
    jm = c.get_kline("BTC/USDT", "1h", limit=5)
    # 倒数第二根是已收盘的，避免最新未收盘抖动
    bar = jm[-2]
    ot = JumdataKlineClient._bar_ts(bar)
    print(f"  [jumdata] ot={ot}({c._fmt(ot)}) o/h/l/c={bar['o']}/{bar['h']}/{bar['l']}/{bar['c']} v={bar['v']} q={bar['q']}")
    # 币安
    try:
        with httpx.Client(timeout=20.0) as hc:
            b = hc.get("https://api.binance.com/api/v3/klines",
                       params={"symbol": "BTCUSDT", "interval": "1h", "startTime": ot, "limit": 1}).json()
        if b and b[0][0] == ot:
            k = b[0]
            print(f"  [币安   ] o/h/l/c={k[1]}/{k[2]}/{k[3]}/{k[4]} v={k[5]} q={k[7]}")
        else:
            print(f"  [币安   ] 无对应K线或时间不匹配: {b[:1]}")
    except Exception as ex:  # noqa: BLE001
        print(f"  [币安   ] 请求失败：{type(ex).__name__} {ex}")
    # OKX
    try:
        with httpx.Client(timeout=20.0) as hc:
            o = hc.get("https://www.okx.com/api/v5/market/history-candles",
                       params={"instId": "BTC-USDT", "bar": "1H", "after": str(ot), "limit": "2"}).json()
            rows = o.get("data", [])
            match = next((r for r in rows if int(r[0]) == ot), None)
        if match:
            print(f"  [OKX   ] o/h/l/c={match[1]}/{match[2]}/{match[3]}/{match[4]} v={match[5]} q={match[6]}")
        else:
            print(f"  [OKX   ] 无对应K线: {rows[:1]}")
    except Exception as ex:  # noqa: BLE001
        print(f"  [OKX   ] 请求失败：{type(ex).__name__} {ex}")


if __name__ == "__main__":
    with JumdataKlineClient() as c:
        q_5m_depth(c)
        q_supported(c)
        q_source(c)
