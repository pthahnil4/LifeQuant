# -*- coding: utf-8 -*-
"""只读诊断：51008「可用 USDT 不足」到底是真没钱、钱被冻住、还是被口径算错。

为什么需要它
------------
2026-09-11 19:20 起，实盘开始成片刷这种拒单：

    [双仓位] DASH-USDT-SWAP 区间波动·开仓 挂单失败: All operations failed |
    51008: Order failed. Your available USDT balance is insufficient, and
    your available margin (in USD) is too low for borrowing.

而策略配置里区间仓下单口径是 ``size_mode="usd"`` + ``notional_usd=1``——**1 美元名义、
10 倍杠杆、约 0.1 USDT 保证金**。连这个量级都被拒，只有三种可能，且处置方式完全不同：

1. 账户可用 USDT 真的见底（钱都在持仓保证金里 / 亏掉了）→ 要去查权益与保证金占用；
2. 余额还在但被**挂单冻结**吃掉（区间仓是限价挂单，未成交就冻保证金）→ 要撤单释放；
3. 代码里的可用余额口径读错了，照计划本不该下这一单 → 是程序 bug，要改代码。

分不清这三者就只能猜。本脚本把交易所侧的原始事实一次摊开：账户总权益 / 可用 / 冻结 /
负债 / 账户等级（借款能力），逐笔持仓的保证金占用，以及**每个配置币种按当前杠杆还能开
多大**。只读，不下单、不改配置。

注意 51008 的措辞里有 "too low for borrowing"：全仓（cross）下的借款能力受**账户等级
acctLv** 限制，所以本脚本一并打 acctLv —— 等级 1 的账户跨币种借款额度极小，容易被这条
卡住，而这不是"没钱"，是"权限不够"。

用法
----
    python crypto/task/_diag_margin_state.py                 # 默认账号（DEFAULT_ACCOUNT）
    python crypto/task/_diag_margin_state.py stageone        # 指定账号

安全边界：**纯只读**。只调 get_account_balance / get_positions /
get_account_position_risk / get_max_avail_size 四个查询接口，凭证从 crypto/api_config
读取且绝不打印任何密钥。get_max_avail_size 是查询类接口，不会下单。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
# ↑ line_buffering 必须有：本脚本要联网，默认块缓冲下如果被超时杀掉，前面查到的
#   数字会全部留在缓冲区里一起丢掉（实测跑一次 600s 超时，输出一个字都没有）。

try:
    import httpx
except ImportError:
    httpx = None

from okx import Account  # noqa: E402
from crypto.api_config import get_api_config  # noqa: E402

_TIMEOUT_SEC = 12.0      # 查询接口不该长等；宁可失败得快、下面重试
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'config', 'config_trend_range.json')
_OKX_HOST = 'www.okx.com'


def link_precheck():
    """出口链路体检：不通就直接说清楚，别把 10 个 × 3 次重试慢慢磨完。

    为什么专门做这一步：本机靠代理客户端接管出海外连接，而**不是每个进程都被接管**。
    被接管时 www.okx.com 解析到可用地址；没被接管时解析到 169.254.0.2 这类 link-local
    假地址（fake-IP），然后 TCP 一直等到超时。实盘进程能拿到交易所应答、而同一个机器上
    手起的诊断脚本连不上，就是这个原因。看到这个结果不是"账户查不到"，而是"你换了个
    没被代理接管的终端"。
    """
    import socket
    try:
        ip = socket.gethostbyname(_OKX_HOST)
    except Exception as e:
        print(f'DNS 解析 {_OKX_HOST} 失败：{type(e).__name__}: {e}')
        return False
    print(f'DNS: {_OKX_HOST} -> {ip}')
    if ip.startswith('169.254.') or ip.startswith('198.18.'):
        print(f'  ⚠ 解析到 {ip}：这是代理客户端的 fake-IP 段，说明**当前这个终端进程**')
        print('    没有被接管规则命中，连出去必然超时。请换到你平时能访问交易所的终端里跑本脚本')
        print('    （实盘 app.py 所在的那类环境是可以的），或直接看网页上的账户信息。')
        return False
    try:
        socket.create_connection((ip, 443), timeout=8).close()
    except Exception as e:
        print(f'  ⚠ TCP 连 {ip}:443 失败：{type(e).__name__}: {e}')
        return False
    return True


def _f(v, default=0.0):
    """OKX 把数字全返回成字符串，空串/None 都按 default 处理（不抛异常打断整张表）"""
    try:
        s = str(v if v not in (None, '') else '').strip()
        return float(s) if s else default
    except (TypeError, ValueError):
        return default


def _mk_api(account):
    cfg = get_api_config(account)
    api = Account.AccountAPI(cfg['api_key'], cfg['secret_key'], cfg['passphrase'],
                             False, cfg['flag'])
    if httpx is not None:
        try:
            api.timeout = httpx.Timeout(_TIMEOUT_SEC)
        except Exception:
            pass  # SDK 结构随版本变，设不上就用默认超时，不该让诊断整体失败
    return api, cfg


def _rows(result):
    """统一取 data 列表；code != 0 时原样报出来（别静默成空表，那会被读成"没持仓"）"""
    if not isinstance(result, dict):
        # None 是 _call 重试三次仍失败的信号：这里不再重复报文本，交给 _call 的那行说清
        if result is not None:
            print(f'  (接口返回异常: {str(result)[:200]})')
        return None
    if str(result.get('code', '0')) != '0':
        print(f"  (接口报错 code={result.get('code')} msg={result.get('msg')})")
        return None
    return result.get('data') or []


def _call(what, fn, *args, **kwargs):
    """带退避重试的只读调用。

    为什么要重试：这台机器到交易所的出口链路本来就在抖（实盘那边同一小时就刷了
    10 次 SSL UNEXPECTED_EOF / 2 次 Server disconnected），诊断脚本被一次抖动打死、
    再吐半屏 traceback，等于把要看的数字换成噪声。
    """
    last = None
    for i in range(1, 3):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            wait = 3 * i
            print(f'  [{what}] 第 {i}/2 次调用失败：{type(e).__name__}: {str(e)[:120]}'
                  f'（{wait}s 后重试）')
            time.sleep(wait)
    print(f'  [{what}] 两次都没通，跳过本节：{type(last).__name__}: {str(last)[:160]}')
    return None


def show_balance(api):
    print('\n──── 账户余额（USDT） ────')
    data = _rows(_call('余额', api.get_account_balance, ccy='USDT'))
    if not data:
        print('  (无数据)' if data == [] else '')
        return None
    acct = data[0]
    print(f"  总权益 totalEq      = {_f(acct.get('totalEq')):.2f} USD")
    print(f"  调整权益 adjEq      = {_f(acct.get('adjEq')):.2f} USD")
    print(f"  逐仓权益 isoEq      = {_f(acct.get('isoEq')):.2f} USD")
    print(f"  挂单冻结 ordFroz    = {_f(acct.get('ordFroz')):.2f} USDT   ← 未成交限价单占用的"
          f"（区间仓是挂单，这部分没成交也一直冻着）")
    print(f"  未实现盈亏 upl      = {_f(acct.get('upl')):+.2f} USDT")
    print(f"  指数抵扣 imLn       = {_f(acct.get('imLn')):.2f} USDT")
    for d in (acct.get('detail') or []):
        if str(d.get('ccy')) != 'USDT':
            continue
        print('  --- USDT 明细 ---')
        print(f"      权益 eq           = {_f(d.get('eq')):.4f}")
        print(f"      可用 availBal     = {_f(d.get('availBal')):.4f}   ← 51008 卡的就是它")
        print(f"      冻结 frozenBal    = {_f(d.get('frozenBal')):.4f}")
        print(f"      负债 liab          = {_f(d.get('liab')):.4f}")
        print(f"      最大借款 maxLoan   = {_f(d.get('maxLoan')):.2f}")
        print(f"      名义 notionalUsd   = {_f(d.get('notionalUsd')):.2f}")
    return acct


def show_position_risk(api):
    """账户等级决定全仓借款能力：51008 那句 too low for borrowing 常与此相关。"""
    print('\n──── 账户风险/等级（全仓借款能力） ────')
    data = _rows(_call('账户风险', api.get_account_position_risk))
    if not data:
        return
    for r in data:
        print(f"  acctLv(账户等级) = {r.get('acctLv')}   liqRank={r.get('liqRank')}")
        print(f"  总保证金 maintMargin = {_f(r.get('maintMargin')):.4f} USDT"
              f"   仓位风险 value={_f(r.get('totalMgn')):.4f}")
        print(f"  原始返回键: {sorted(r.keys())}")


def show_positions(api):
    print('\n──── 当前持仓（SWAP） ────')
    data = _rows(_call('持仓', api.get_positions, instType='SWAP'))
    if data is None:
        return 0.0
    if not data:
        print('  无持仓')
        return 0.0
    total_margin = 0.0
    print(f"  {'instId':<20}{'模式':<9}{'方向':<7}{'张数':>8}{'杠杆':>6}"
          f"{'名义USD':>11}{'保证金':>10}{'浮动盈亏':>11}")
    for p in data:
        total_margin += _f(p.get('margin'))
        print(f"  {str(p.get('instId')):<20}{str(p.get('mgnMode')):<9}"
              f"{str(p.get('posSide')):<7}{str(p.get('pos')):>8}{str(p.get('lever')):>6}"
              f"{_f(p.get('notionalUsd')):>11.2f}{_f(p.get('margin')):>10.4f}"
              f"{_f(p.get('upl')):>+11.2f}")
    print(f"  持仓保证金合计（margin 字段）≈ {total_margin:.4f} USDT")
    return total_margin


def show_max_avail(api, account):
    """按策略配置逐币种查「现在还能开多大」——这直接回答"下一单会不会又被拒"。"""
    print('\n──── 各币种最大可开（按配置杠杆） ────')
    try:
        with open(_CONFIG_PATH, encoding='utf-8') as f:
            cfg = json.load(f)
        coins = cfg.get('currencies') or []
    except Exception as e:
        print(f'  (读策略配置失败，跳过本节: {e})')
        return
    for c in coins:
        inst_id = c.get('instId')
        lev = str(c.get('leverage') or '10')
        rp = c.get('range_position') or {}
        tp = c.get('trend_position') or {}
        for td_mode in ('cross', 'isolated'):
            try:
                r = _rows(_call(f'可开量 {inst_id}/{td_mode}',
                                api.get_max_avail_size, instId=inst_id, tdMode=td_mode, ccy='USDT'))
            except Exception as e:
                print(f'  {inst_id} {td_mode}: 调用异常 {e}')
                continue
            if not r:
                continue
            row = r[0] if isinstance(r, list) else r
            print(f"  {inst_id:<20}{td_mode:<10}lev={lev:<4} "
                  f"可开多={_f(row.get('maxAvailBuySize')):>10} 张 "
                  f"可开空={_f(row.get('maxAvailSellSize')):>10} 张  "
                  f"err={row.get('errMsg') or row.get('success') or '-'}")
        print(f"      配置意图: 区间 {rp.get('size_mode')}={rp.get('notional_usd')}"
              f"/{rp.get('contracts')}张 (enabled={rp.get('enabled')}), "
              f"趋势 {tp.get('size_mode')}={tp.get('notional_usd')}/{tp.get('contracts')}张 "
              f"(enabled={tp.get('enabled')})")


def verdict(acct, pos_margin):
    print('\n──── 结论提示（按上面真实数字自己核，别照抄这一行） ────')
    if not acct:
        print('  余额没查到，先排链路/权限')
        return
    avail = 0.0
    for d in (acct.get('detail') or []):
        if str(d.get('ccy')) == 'USDT':
            avail = _f(d.get('availBal'))
    frozen = _f(acct.get('ordFroz'))
    print(f'  可用 {avail:.4f} USDT | 挂单冻结 {frozen:.4f} USDT | 持仓保证金 {pos_margin:.4f} USDT')
    if avail < 1.0 and frozen > avail:
        print('  → 冻结比可用还大：先撤掉不打算成交的区间挂单，保证金会释放回来。')
    if avail < 0.5 and frozen < 0.5:
        print('  → 可用与冻结都很小：是账户真的没有可用 USDT（不是冻结问题），'
              '要么入金，要么把区间仓挂单量/币种数降下来，要么停掉部分币种。')


def main():
    account = sys.argv[1] if len(sys.argv) > 1 else None
    t0 = time.time()
    api, cfg = _mk_api(account)
    print(f"账号: {cfg.get('account')} ({cfg.get('account_name')})  flag={cfg.get('flag')}"
          f"（模拟盘=1 时数字与实盘无关，务必先确认这一行）")

    if not link_precheck():
        print('\n出口链路不通，先解决"在哪个终端里跑"再查账户 —— 这不是账户没问题，也不是没持仓。')
        return 2

    acct = show_balance(api)
    show_position_risk(api)
    pos_margin = show_positions(api) or 0.0
    show_max_avail(api, account)
    verdict(acct, pos_margin)
    print(f'\n(耗时 {time.time() - t0:.1f}s，全程只读)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
