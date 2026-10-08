# -*- coding: utf-8 -*-
"""只读诊断：从生产 MySQL 捞出「实盘真正生效」的配置/规格/杠杆缓存，核对 51008。

为什么需要它
------------
开发机上的 crypto/task/config/config_trend_range.json 只是**兜底文件**。
config_store_repo 的读取顺序是「DB 优先、文件兜底」——网页上改配置只写 DB，
开发机这份 JSON 可能早已过时。现网日志里 INJ 区间仓下单 346 张，而开发机
文件写的是 notional_usd=1（折算仅约 17 张），两者对不上，必须以 DB 为准。

51008「可用 USDT 保证金不足」的两种代码侧成因，都能用 DB 数据分辨：
  A. 配置把 notional_usd 调大了 → 每仓保证金预算 = notional_usd，
     5 币种 × 2 仓累加可能远超账户可用 USDT 现金；
  B. 交易所侧实际杠杆 < 配置杠杆（人工在 App 改过 / 校正只覆盖了 isolated，
     cross 没校正）→ 占用保证金按 配置杠杆÷实际杠杆 放大。
     占用保证金 = 张数 × ctVal × 价 ÷ 实际杠杆，而张数是按配置杠杆折算的，
     所以实际杠杆偏低时保证金需求 = notional_usd × (配置杠杆 ÷ 实际杠杆)。

本脚本把判定这两条所需的真实数字一次摊开：账号、每币种配置（size_mode /
notional_usd / leverage）、合约规格（ctVal / lotSz / minSz / maxLever）、
本地缓存的已设杠杆 lev_set（cross / isolated）、当前账本持仓。

安全边界：**纯只读**。只 SELECT kv_store 与持仓账本表，不下单、不改配置、
不写库、不碰 OKX 交易接口。凭证/连接串从 data/db_url.txt 读取且不打印密码。

用法
----
    python crypto/task/_diag_db_config_state.py
    python crypto/task/_diag_db_config_state.py --price INJ-USDT-SWAP=5.773 ...
        （可选：给某些币种传现价，用于 contracts 模式仓位的保证金折算与张数反算）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

from sqlalchemy import text  # noqa: E402

from crypto.database import session_scope, get_engine  # noqa: E402
from crypto import config_store_repo as csr  # noqa: E402
from crypto import trader_state_repo as state_repo  # noqa: E402


def _f(v, d=0.0):
    try:
        s = str(v if v not in (None, '') else '').strip()
        return float(s) if s else d
    except (TypeError, ValueError):
        return d


def _parse_price_args(argv):
    """--price INST=5.773 INST2=1.23 → {inst: price}；用于张数反算/contracts折算"""
    prices = {}
    for a in argv:
        if '=' in a and a.upper().endswith('-SWAP') is False and '-' in a.split('=')[0]:
            k, v = a.split('=', 1)
            prices[k.strip()] = _f(v)
    return prices


def show_runtime(session):
    print('\n──── 实盘运行状态（trading_runtime）────')
    rt = csr.load_json_config(session, csr.KEY_TRADING_RUNTIME) or {}
    print(f"  账号 account        = {rt.get('account')}")
    print(f"  期望运行 desired    = {rt.get('desired_running')}")
    print(f"  自动拉起 auto_resume= {rt.get('auto_resume')}")
    print(f"  最近事件 last_event = {rt.get('last_event')}")
    print(f"  更新时间 updated_at = {rt.get('updated_at')}")
    return rt


def show_config_and_spec(session, prices):
    cfg = csr.load_json_config(session, csr.KEY_STRATEGY_CONFIG)
    if not cfg:
        print('\n!! DB 里没有 strategy_config —— 实盘会回退开发机文件兜底。')
        return None, None
    spec_cache = csr.load_json_config(session, csr.KEY_INSTRUMENT_SPEC_CACHE) or {}
    coins = cfg.get('currencies') or []
    gs = cfg.get('global_settings') or {}
    rc = gs.get('risk_control') or {}
    print('\n──── 全局风控（risk_control）────')
    print(f"  emergency_stop={rc.get('emergency_stop')} "
          f"max_position_per_currency={rc.get('max_position_per_currency')} "
          f"max_position_per_currency_usd={rc.get('max_position_per_currency_usd')} "
          f"max_total_position={rc.get('max_total_position')}")
    print(f"  配置更新时间 _comment_update_time={cfg.get('_comment_update_time')} "
          f"version={cfg.get('_comment_version')}")

    print('\n──── 每币种「DB 真实生效」配置 + 规格 + 杠杆缓存 + 持仓 ────')
    pos_state = state_repo.load_position_state(session)

    total_margin_budget = 0.0   # 所有启用 usd 模式仓位的 notional_usd 之和（杠杆正确时的理论保证金）
    rows = []
    for c in coins:
        inst = c.get('instId')
        lev_cfg = _f(c.get('leverage'), 0)
        spec = spec_cache.get(inst) or {}
        ct_val = _f(spec.get('ct_val'))
        lot_sz = _f(spec.get('lot_sz'))
        min_sz = _f(spec.get('min_sz'))
        max_lev = _f(spec.get('max_lever'))
        st = pos_state.get(inst) or {}
        lev_set = st.get('lev_set') or {}
        price = prices.get(inst, 0.0)

        print(f"\n  ◆ {inst}  配置杠杆={lev_cfg:g}x  现价={price if price else '(未传)'}")
        print(f"     规格: ctVal={ct_val:g} lotSz={lot_sz:g} minSz={min_sz:g} maxLever={max_lev:g}"
              + ('' if spec else '   ← ⚠ DB 无此规格缓存'))
        print(f"     缓存已设杠杆 lev_set: cross={lev_set.get('cross')} isolated={lev_set.get('isolated')}"
              f"   （None=本进程从没设过/未记录）")

        for bucket, key in (('趋势', 'trend_position'), ('区间', 'range_position')):
            pc = c.get(key) or {}
            enabled = pc.get('enabled', True)
            mode = str(pc.get('size_mode', 'contracts') or 'contracts').lower()
            notional = _f(pc.get('notional_usd'))
            contracts = _f(pc.get('contracts'))
            held = (st.get('trend') if key == 'trend_position' else st.get('range')) or {}
            held_l = _f((held.get('held') or {}).get('long'))
            held_s = _f((held.get('held') or {}).get('short'))

            margin_note = ''
            if enabled and mode == 'usd':
                # 杠杆正确时，该仓占用保证金 ≈ notional_usd
                total_margin_budget += notional
                margin_note = f'保证金预算≈{notional:g}U(杠杆正确时)'
                if price > 0 and ct_val > 0 and lot_sz > 0:
                    eff_lev = lev_cfg if lev_cfg > 0 else 1
                    if max_lev > 0 and eff_lev > max_lev:
                        eff_lev = max_lev
                    lots = int(notional * eff_lev / (ct_val * price) / lot_sz + 1e-9)
                    calc = lots * lot_sz
                    margin_note += f' | 按配置{eff_lev:g}x折算≈{calc:g}张'
            elif enabled and mode == 'contracts':
                if price > 0 and ct_val > 0:
                    eff_lev = lev_cfg if lev_cfg > 0 else 1
                    m = contracts * ct_val * price / eff_lev
                    margin_note = f'保证金≈{m:.2f}U(杠杆{eff_lev:g}x)'
                    total_margin_budget += m
                else:
                    margin_note = f'{contracts:g}张(需现价折算保证金)'

            flag = '' if enabled else '  [停用]'
            print(f"     {bucket}仓{flag}: size_mode={mode} notional_usd={notional:g} "
                  f"contracts={contracts:g} | 持仓 多{held_l:g}/空{held_s:g} | {margin_note}")

        rows.append((inst, lev_cfg, lev_set, ct_val, price))

    print('\n──── 保证金需求汇总（关键）────')
    print(f"  若全部仓位杠杆都正确生效：本轮全部启用仓位占用保证金 ≈ {total_margin_budget:.2f} USDT")
    print(f"  → 账户「可用 USDT 现金」低于此值就会成片 51008（与总权益高不矛盾：")
    print(f"     钱可能在别的币/持仓保证金里，或被未成交挂单冻结）。")

    # 杠杆放大风险：cross 模式（做多）实际杠杆若低于配置，保证金按倍数放大
    print('\n──── 杠杆失真放大测算（做多走 cross）────')
    for inst, lev_cfg, lev_set, ct_val, price in rows:
        cross_cached = lev_set.get('cross')
        if cross_cached is None:
            verdict = 'cross 杠杆本进程无缓存记录 → 若交易所侧实际<配置，保证金会被放大'
        elif _f(cross_cached) < lev_cfg:
            ratio = lev_cfg / _f(cross_cached)
            verdict = f'⚠ 缓存 cross={cross_cached} < 配置{lev_cfg:g} → 保证金放大约{ratio:.2f}倍'
        else:
            verdict = f'cross 缓存={cross_cached} 与配置一致'
        print(f"  {inst}: {verdict}")

    return cfg, spec_cache


def main():
    argv = sys.argv[1:]
    prices = _parse_price_args([a for a in argv if a.startswith('--price')]) if False else {}
    # 简化：--price 后面的 INST=价 直接收集
    if '--price' in argv:
        idx = argv.index('--price')
        prices = _parse_price_args(argv[idx + 1:])

    eng = get_engine()
    url = str(eng.url)
    # 打印连接目标但抹掉密码
    safe = url.split('@')[-1] if '@' in url else url
    print(f"目标库: {safe}（只读）")
    with session_scope() as session:
        session.execute(text('SELECT 1'))
        show_runtime(session)
        show_config_and_spec(session, prices)
    print('\n(全程只读，未写库/未下单/未碰 OKX)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
