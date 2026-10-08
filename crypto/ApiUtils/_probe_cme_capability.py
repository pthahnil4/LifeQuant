#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CME 期货行情接口能力探测
========================

回答三个问题（针对 jmcmeqh 网关的"CME期货行情"数据源）：
1. 该数据源到底提供哪些能力？有没有隐藏的K线/历史接口？
2. 哪些品种代码可用？（供应商"品种代码表"未随文档提供，这里逐个实测）
3. 同一份密钥能否用于其它行情数据源（跨网关复用验证）？

运行：
    python crypto/ApiUtils/_probe_cme_capability.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')

import httpx  # noqa: E402

from cme_futures_utils import (  # noqa: E402
    CME_HOST, PRICE_PATH, KLINE_GUESS_PATHS, DEFAULT_APPCODE,
    CANDIDATE_SYMBOLS, SYMBOLS_UNAVAILABLE, CmeFuturesClient, CmeNoDataError,
)

HEADERS = {
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'Authorization': 'APPCODE ' + DEFAULT_APPCODE,
}

client = httpx.Client(timeout=httpx.Timeout(20.0, connect=15.0))


def probe_endpoints():
    print('=' * 68)
    print('[1] 端点能力探测：确认是否存在K线/历史接口')
    print('=' * 68)
    for path in [PRICE_PATH] + KLINE_GUESS_PATHS:
        try:
            resp = client.post(CME_HOST + path, data={'symbol': 'HG'}, headers=HEADERS)
            note = '成功' if resp.status_code == 200 else '无此接口/参数错'
            print(f'  POST {path:26s} HTTP={resp.status_code}  {note}  '
                  f'body={resp.text[:120]}')
        except Exception as exc:  # noqa: BLE001
            print(f'  POST {path:26s} EXC={exc}')
        time.sleep(0.2)

    # 同一端点带各种"疑似K线参数"，看返回体是否变成数组
    print('\n  同端点附加K线类参数（观察返回结构是否变化）：')
    for extra in ({'type': '1d'}, {'period': '1H'}, {'klineType': 'day'},
                  {'limit': '100'}, {'startTime': '1577836800000'}):
        body = {'symbol': 'HG'}
        body.update(extra)
        resp = client.post(CME_HOST + PRICE_PATH, data=body, headers=HEADERS)
        is_dict = '"price"' in resp.text
        print(f'    {str(extra):38s} HTTP={resp.status_code} '
              f'返回结构={"单条快照" if is_dict else resp.text[:80]}')
        time.sleep(0.2)


def probe_symbols():
    print('\n' + '=' * 68)
    print('[2] 品种代码逐个验证')
    print('=' * 68)
    ok, fail = [], []
    for sym in list(CANDIDATE_SYMBOLS):
        try:
            resp = client.post(CME_HOST + PRICE_PATH, data={'symbol': sym}, headers=HEADERS)
            payload = resp.json()
            if payload.get('success'):
                d = payload['data']
                ok.append(sym)
                print(f'  {sym:6s} OK  合约月={d.get("month")} 价={d.get("price")} '
                      f'涨跌={d.get("changeRate")}% 量={d.get("volume")} 持仓={d.get("position")}')
            else:
                fail.append(sym)
                print(f'  {sym:6s} FAIL code={payload.get("code")} msg={payload.get("msg")}')
        except Exception as exc:  # noqa: BLE001
            fail.append(sym)
            print(f'  {sym:6s} EXC {exc}')
        time.sleep(0.2)
    print(f'\n  可用 {len(ok)} 个：{ok}')
    print(f'  不可用 {len(fail)} 个：{fail}')
    if set(fail) != set(SYMBOLS_UNAVAILABLE):
        print('  ⚠ 与工具类登记的不可用清单不一致，请同步 SYMBOLS_UNAVAILABLE')
        print(f"    新增不可用：{sorted(set(fail) - set(SYMBOLS_UNAVAILABLE))}")
        print(f"    恢复可用：{sorted(set(SYMBOLS_UNAVAILABLE) - set(fail))}")
    else:
        print('  ✓ 与工具类 SYMBOLS_UNAVAILABLE 登记一致')


def check_client_normalization():
    print('\n' + '=' * 68)
    print('[3] 工具类归一化自检（CmeFuturesClient.get_quote）')
    print('=' * 68)
    with CmeFuturesClient() as cme:
        for sym in ('HG', 'ZN'):
            q = cme.get_quote(sym)
            print(f"  {sym}: {q['name']} 合约={q['expiry']} 最新价={q['price']} "
                  f"昨收={q['prev_close']} 涨跌={q['change']}({q['change_rate']}%) "
                  f"高/低={q['high']}/{q['low']} 量={q['volume']} 仓={q['position']}")
            print(f"        服务端时间={q['update_time']} -> epoch={q['update_ts']}，"
                  f"本机UTC now={int(time.time())}（差值应为分钟级，若是小时级说明时区假设错了）")
            assert isinstance(q['price'], float), '最新价应为 float'
            # 实测 updateTime 滞后本机时间约 3 分钟（供应商快照缓存），
            # 只校验它在 UTC+8 假设下不偏离超过 15 分钟
            assert q['update_ts'] and abs(int(time.time()) - q['update_ts']) < 900, \
                f"updateTime 时区解析偏差过大: {q['update_time']}"
        # 不可用品种应抛 CmeNoDataError（而不是默默返回空）
        try:
            cme.get_quote('KC')
            print('  ⚠ KC 意外返回数据，请检查品种表')
        except CmeNoDataError as exc:
            print(f'  ✓ 不可用品种抛异常：{type(exc).__name__}: {exc}')
        # 批量接口：失败品种默认跳过
        rows = cme.get_quotes(['ES', 'KC', 'GC'])
        print(f"  ✓ get_quotes 跳过不可用：请求3个/返回{len(rows)}个 -> "
              f"{[r['symbol'] for r in rows]}")


def probe_cross_gateway():
    print('\n' + '=' * 68)
    print('[4] 同一 AppCode 能否用于其它行情网关（跨产品复用验证）')
    print('=' * 68)
    for host, path, body in (
        ('https://jmjmhb.market.alicloudapi.com', '/crypto/coin/kline',
         {'symbol': 'BTC/USDT', 'interval': '1d', 'limit': '2'}),
        ('http://alirmgbdc.market.alicloudapi.com', '/query/comkm',
         {'symbol': 'BTC', 'period': 'D', 'pidx': '1', 'psize': '2', 'withlast': '0'}),
    ):
        try:
            resp = client.request('POST' if 'jmjmhb' in host else 'GET', host + path,
                                  data=body if 'jmjmhb' in host else None,
                                  params=None if 'jmjmhb' in host else body,
                                  headers=HEADERS)
            print(f'  {host.split("//")[1]:34s} {path:16s} HTTP={resp.status_code} '
                  f'body={resp.text[:140]}')
        except Exception as exc:  # noqa: BLE001
            print(f'  {host} {path} EXC={exc}')


if __name__ == '__main__':
    probe_endpoints()
    probe_symbols()
    check_client_normalization()
    probe_cross_gateway()
