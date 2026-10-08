# -*- coding: utf-8 -*-
"""只读诊断：列出交易所当前挂单及其 clOrdId（不打标就看不见归属）。

用途
----
`gen_cl_ord_id` 给每笔委托打了客户订单号，但 Web 的 `/api/trade/open_orders`
返回体里没有 clOrdId 字段（2026-09-11 补上），排障时常常想知道：

    "交易所里这笔单，到底是我哪一轮、哪个篮子下的？还挂不挂着？"

本脚本直接查交易所，把 ordId / instId / 方向 / 量 / 价 / clOrdId 一行行摊开，
并和 task_scheduler.log 里的挂单行对照，即可确认：
  1) 客户号是否真的写进交易所（= OKX 接受了这个格式，51000 事故的反证）；
  2) 本侧账本与交易所有没有对不上的单（幽灵挂单 / 漏撤）。

安全边界
--------
**纯只读**：只调 get_order_list / get_order_history，不下单、不撤单、不改单。
凭证从 crypto/api_config 读，不打印任何密钥。

用法
----
    python crypto/task/_diag_open_orders_clordid.py                # 默认账号
    python crypto/task/_diag_open_orders_clordid.py stageone       # 指定账号
    python crypto/task/_diag_open_orders_clordid.py stageone hist  # 再看已终结的委托

属于 🌐 连真实 API 类（见 crypto/SMOKE_TESTS.md）。单次跑最多 2 个请求，用不上
限频包层（`okx_ratelimit` 是给常驻调度里的高频轮询用的），但**别拿它做循环轮询**。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    import httpx
except ImportError:
    httpx = None

from okx import Trade  # noqa: E402
from crypto.api_config import ACCOUNTS, get_api_config  # noqa: E402

# 跨境链路抖动时 SDK 默认 5s 读超时太紧，与 trade_executor 保持同样的放宽口径
_TIMEOUT_SEC = 30.0


def _mk_client(account):
    cfg = get_api_config(account)
    api = Trade.TradeAPI(cfg['api_key'], cfg['secret_key'], cfg['passphrase'],
                         False, cfg['flag'])
    if httpx is not None:
        try:
            api.timeout = httpx.Timeout(_TIMEOUT_SEC)
        except Exception:
            pass  # SDK 结构变了就用默认超时，只读诊断不该因此挂掉
    return api, cfg


def _show(rows, title):
    print(f"\n=== {title} ===")
    if not rows:
        print("  (空)")
        return
    no_cid = 0
    for o in rows:
        cid = o.get('clOrdId') or ''
        if not cid:
            no_cid += 1
        print(f"  {o.get('instId', ''):20} {o.get('side', ''):5} {o.get('posSide', ''):5} "
              f"sz={o.get('sz', ''):>8} px={o.get('px', '') or '-':>12} "
              f"{o.get('state', ''):10} clOrdId={cid or '(无打标)'} ordId={o.get('ordId', '')}")
    if no_cid:
        print(f"  其中 {no_cid} 笔没有 clOrdId —— 多为手工下单或本侧打标上线前的旧单")


def main():
    account = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else None
    want_hist = any('hist' in a.lower() for a in sys.argv[1:])

    if account and account not in ACCOUNTS:
        print(f"未知账号 {account}，可选: {list(ACCOUNTS.keys())}")
        return 2

    cfg = get_api_config(account)
    print(f"账号: {cfg['account']} ({cfg['account_name']}) | flag={cfg['flag']} "
          f"({'实盘' if str(cfg['flag']) == '0' else '模拟盘'})")

    api = _mk_client(account)[0]
    try:
        live = api.get_order_list(instType='SWAP', state='live')
        if str(live.get('code')) != '0':
            print(f"查询挂单失败: code={live.get('code')} msg={live.get('msg')}")
            return 1
        _show(live.get('data') or [], "当前未成交挂单 (state=live)")
    except Exception as e:
        print(f"查询挂单异常: {type(e).__name__}: {e}")
        return 1

    if want_hist:
        try:
            # 方法名是 get_orders_history（带 s），写成 get_order_history 会
            # AttributeError —— 这条分支平时不跑，最容易藏这种错
            hist = api.get_orders_history(instType='SWAP', limit='20')
            _show(hist.get('data') or [], "最近 20 笔已终结委托 (history)")
        except Exception as e:
            print(f"查询历史委托异常: {type(e).__name__}: {e}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
