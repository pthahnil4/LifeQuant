#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
clOrdId 字符集定性探针（只读，绝不下单）
========================================
背景：2026-09-11 实盘打标上线后，每一笔委托都被交易所回
`51000: Parameter clOrdId error`，双仓位挂单全灭。根因是 `gen_cl_ord_id`
按「字母/数字/下划线/连字符，1~64 位」的假设生成 `ct_<token>_<hash>`，
而 OKX 的实测口径是「**只允许字母与数字、长度 1~32**」。

做法：用**只读**的 GET /api/v5/trade/order?clOrdId=xxx 逐个形态探测——
- 若格式非法 → `51000 Parameter clOrdId error`（校验阶段就被挡）
- 若格式合法 → `51603 Order does not exist`（说明服务器已接受该客户号写法）
两者一比一对照即可定性，且完全不碰下单接口。

何时重跑：改 `utils/trade_executor.py` 的 `gen_cl_ord_id`，或 OKX 调整客户号规则
（例如放开分隔符/改长度上限）时。结论请回写到 gen_cl_ord_id 的注释里。

**两个接口分开验，别拿一个的结论替另一个背书**：普通委托走
`clOrdId`（get_order），策略委托（追逐限价/TWAP/移动止盈止损）走
`algoClOrdId`（get_algo_order_details）。本脚本第一版只查了前者，末尾却
打印"clOrdId/algoClOrdId 口径已确认"——正好是它要防的那种假完备，故现在
两个接口各跑一组，结论由实测结果现场生成（见 `_verdict()`）。

安全边界：全程只调 get_order / get_algo_order_details / get_instruments
三个查询接口，绝不调 place_order / place_algo_order / cancel_order / amend_order。
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import api_config  # noqa: E402
import httpx  # noqa: E402
from okx.Trade import TradeAPI  # noqa: E402
from okx.PublicData import PublicAPI  # noqa: E402

# 任意一个真实存在的合约（只为通过 instId 校验，查无订单不影响结论）
INST_ID = 'BTC-USDT-SWAP'
# 合法形态 = 51603（格式通过、订单不存在）；非法形态 = 51000（参数被拒）
CODE_OK_FORMAT = '51603'
CODE_BAD_FORMAT = '51000'


def _probe(api, cid, tries=3):
    """同一客户号连查若干次：网络抖动（现网 SDK 默认读超时仅约 5s）时
    至少拿到一次交易所的权威答复，避免把超时误判成'参数错误'。"""
    last = {'code': '-', 'msg': '请求异常（未执行）'}
    for i in range(tries):
        try:
            return api.get_order(instId=INST_ID, clOrdId=cid)
        except Exception as e:
            last = {'code': 'EXC', 'msg': str(e)}
            time.sleep(1.0)
    return last


def _probe_algo(api, cid, tries=3):
    """策略委托侧的同类探测：只查 get_algo_order_details（按 algoClOrdId）。"""
    last = {'code': '-', 'msg': '请求异常（未执行）'}
    for i in range(tries):
        try:
            return api.get_algo_order_details(algoClOrdId=cid)
        except Exception as e:
            last = {'code': 'EXC', 'msg': str(e)}
            time.sleep(1.0)
    return last


def _classify(r):
    """把一次响应定性成 非法 / 合法 / 未知。

    只有"交易所明确针对这个客户号答复了"才算合法：51000（或报文点名 clOrdId）
    是参数校验就没过；'0'/查无此单/不存在 都说明服务器已接受这种写法。
    鉴权失败、限频等一律记 '未知'，绝不猜成合法——猜错的方向是让实盘继续被拒。
    """
    code = str(r.get('code'))
    msg = str(r.get('msg') or '')
    if code == CODE_BAD_FORMAT or 'clordid' in msg.lower():
        return '非法'
    if code == CODE_OK_FORMAT or code == '0':
        return '合法'
    low = msg.lower()
    if 'not exist' in low or 'not found' in low or 'does not exist' in low:
        return '合法'
    return '未知'


def _verdict(tag, results):
    """结论由实测结果现场生成——绝不再硬编码一句"口径已确认"。"""
    unknown = [x for x in results if x[2] == '未知']
    if unknown:
        return (f"{tag}：{len(unknown)} 项定性为未知（鉴权/限频/未识别错误码），"
                f"本次结论不完整，别照着写注释")
    acc_bad = [x for x in results if x[1] == '非法' and x[2] == '非法']
    acc_ok = [x for x in results if x[1] == '合法' and x[2] == '合法']
    mismatch = len(acc_bad) + len(acc_ok)
    return (f"{tag}：分隔符/超长共 {mismatch} 种形态与预期一致 —— "
            f"非法={sorted(set(x[0] for x in acc_bad)) or '无'}，"
            f"合法={sorted(set(x[0] for x in acc_ok)) or '无'}")


def main():
    acct = 'stageone' if 'stageone' in getattr(api_config, 'ACCOUNTS', {}) else None
    cfg = api_config.get_api_config(acct)
    api = TradeAPI(cfg['api_key'], cfg['secret_key'], cfg['passphrase'],
                   False, cfg.get('flag', '0'))
    # SDK 未暴露超时参数，httpx 默认 5s（现网日志里的 read timed out 就是它）
    api.timeout = httpx.Timeout(30.0)
    print(f"账号={cfg.get('account')} flag={cfg.get('flag')} instId={INST_ID}")

    # 链路自检：公共接口免鉴权，先确认 OKX 可达，再谈参数定性
    pub = PublicAPI(flag=cfg.get('flag', '0'))
    pub.timeout = httpx.Timeout(30.0)
    try:
        st = pub.get_instruments(instType='SWAP', instId=INST_ID)
        print(f"链路自检 get_instruments → code={st.get('code')} 条数={len(st.get('data') or [])}")
        if str(st.get('code')) != '0':
            print('!! 公共接口异常，后续定性结果不可信，请先排查网络/代理')
    except Exception as e:
        print(f"!! 链路自检失败（OKX 不可达，定性结论待定）: {e}")

    stamp = f"{int(time.time() * 1000):x}{os.urandom(4).hex()}"   # 19 位十六进制
    hex32 = ('a' + stamp + 'c' * 40)[:32]
    cases = [
        # (说明, 客户号, 预期)
        ('下划线（改前现网产物）', f'ct_{stamp}_abcdef', '非法'),
        ('连字符', f'ct-{stamp}-abcdef', '非法'),
        ('点号', f'ct.{stamp}', '非法'),
        ('纯字母数字 26 位（改后产物）', f'ct{stamp}abcdef', '合法'),
        ('纯字母数字 32 位（上限）', hex32, '合法'),
        ('纯字母数字 33 位（超上限）', hex32 + 'd', '非法'),
    ]

    groups = [
        ('普通委托 clOrdId (get_order)', _probe, 'clOrdId'),
        ('策略委托 algoClOrdId (get_algo_order_details)', _probe_algo, 'algoClOrdId'),
    ]

    bad = 0
    verdicts = []
    for gname, fn, idfield in groups:
        print(f"\n──── {gname} ────")
        results = []
        for desc, cid, expect in cases:
            r = fn(api, cid)
            code = str(r.get('code'))
            got = _classify(r)
            hit = '' if got == expect else '   <<< 与预期不符，需回写结论'
            if got != expect:
                bad += 1
            results.append((desc, expect, got))
            print(f"[{expect}｜{'通过' if got == expect else got}] {desc:24s} "
                  f"len={len(cid):3d} {idfield}={cid} → code={code} msg={r.get('msg')!r}{hit}")
            time.sleep(0.3)
        verdicts.append(_verdict(gname, results))

    print()
    for v in verdicts:
        print('结论：' + v)
    if bad:
        print(f"注意：{bad} 项与预期不符，请以本次实测为准修正 gen_cl_ord_id")
    return 0


if __name__ == '__main__':
    sys.exit(main())
