# -*- coding: utf-8 -*-
"""账本幽灵条目体检与清理（实盘交易账本 maintenance 工具）

要解决的问题
------------
实盘每轮开头的账户对账范围是「配置币种 ∪ 本地账本 ∪ 交易所持仓目录 ∪ 未成交挂单」
的并集。从配置里移除的币种不会因为移除而离开这个并集 —— 它的账本行还在 MySQL 里。
其中：
  · 空账本残留（币种存在、持仓为 0）：每币白跑一次五步稳定性快照，实测 20s+/币；
  · 幽灵合约（交易所已无此 instId，如误配 NAER-USDT-SWAP 回 51001）：这次快照
    **永远**失败，failed_sync 非空。【2026-09-28 按用户要求移除 G2】对账失败
    不再把 _account_sync_ok 置否、不再让全账户开仓额度归零，但幽灵条目仍会
    每轮白跑一次快照并留下 sync_error 噪声，故仍需清理。
调度器已内置运行时豁免（trend_range_trader._partition_sync_scope 会把这类条目
从本轮同步与内存账本里摘掉），但库里那行记录还在，重启后又会回来 —— 本脚本负责
彻底清除。

安全边界（三条硬约束）
----------------------
1. **默认只读**。不带 --apply 只打印体检结果，一个字都不改。
2. **永不删除有事实的条目**：在当前配置里、或账本记着持仓/在途委托/快照真实持仓
   的合约一律保护（保护项即使命名可疑也绝不碰），需要人工自行处理。
3. **要求实盘调度器已停止**（--scheduler-stopped）。账本用 _revision 乐观锁，
   进程在跑时改库会让它下一轮写入撞版本冲突。【2026-09-28 按用户要求移除 G3】
   冲突不再被永久 latch、不再 fail-stop，但仍会与调度器互相覆盖账本行，故清理
   时仍须先停调度器，避免脏写。

用法
----
    # 体检（联网查合约是否存在，不改任何东西）
    python crypto/task/_cleanup_ghost_ledger.py --account quantlimit

    # 确认干净后再删（必须先停实盘调度器）
    python crypto/task/_cleanup_ghost_ledger.py --account quantlimit \
        --apply --scheduler-stopped

    # 连"币种仍存在但账本已空且不在配置"的闲置条目一起清
    python crypto/task/_cleanup_ghost_ledger.py --account quantlimit \
        --apply --scheduler-stopped --include-idle

    # 体检时附带打印每个受保护条目的持仓量/槽位/风险标记（只读）
    python crypto/task/_cleanup_ghost_ledger.py --account quantlimit --detail
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

from sqlalchemy import delete  # noqa: E402

from crypto.database import session_scope, get_engine  # noqa: E402
from crypto.models import KVStore, PosBook, PosSlot, PosAlgo, PosLev  # noqa: E402
from crypto import config_store_repo as csr  # noqa: E402
from crypto import trader_state_repo as state_repo  # noqa: E402

DUST = 1e-10            # 与 DualPositionOrderManager.POS_DUST 同口径的粉尘阈值
RISK_KEYS = ('settlement_pending', 'external_order_watch',
             'external_open_orders', 'algo_unknown', 'sync_error')
# 槽位终态：与 trend_range_trader._SLOT_SETTLED（即 position_order_manager 的
# ST_IDLE/ST_FILLED/ST_EXPIRED）同口径。已移除币种的账本常年拖着 FILLED/EXPIRED
# 残留记录，把它们当敞口会让体检与运行时分两套标准。
SLOT_SETTLED = ('IDLE', 'FILLED', 'EXPIRED')


def _f(v, d=0.0):
    try:
        s = str(v if v not in (None, '') else '').strip()
        return float(s) if s else d
    except (TypeError, ValueError):
        return d


def slot_unsettled(pt) -> bool:
    """单个槽位/市价单记录是否仍代表在途未确认的现场。"""
    if not isinstance(pt, dict):
        return True
    if pt.get('unknown') or pt.get('qfail_logged') or pt.get('cancel_requested'):
        return True
    return str(pt.get('state') or 'IDLE') not in SLOT_SETTLED


def has_open_interest(s: dict) -> bool:
    """账本是否记着真实持仓或在途委托（含交易所侧兜底委托）。

    判定口径与调度器的 _ledger_open_interest 严格一致（含终态豁免），避免
    "体检说可删、运行时却认为有敞口"两套标准。
    """
    s = s or {}
    for b in ('trend', 'range'):
        bk = s.get(b) or {}
        if any(abs(_f((bk.get('held') or {}).get(d))) > DUST for d in ('long', 'short')):
            return True
        if any(slot_unsettled(sl) for sl in (bk.get('slots') or {}).values()):
            return True
        if any(r for r in (bk.get('algo') or {}).values()):
            return True
    if any(abs(_f(v)) > DUST for v in (s.get('exchange_totals') or {}).values()):
        return True
    return any(slot_unsettled(pt) for pt in (s.get('market_orders') or {}).values())


def configured_insts(account=None):
    """当前实盘交易配置里的币种集合（DB 优先、文件兜底，与调度器同口径）。

    必须按账号读 'strategy_config:{account}'（专属→全局兜底），与
    trend_range_trader._load_config 一致；若只读全局键会把本账号专属配置
    里的币种（如 quantlimit 下的 ETH/LTC）误判为“不在配置→可删的闲置账本”，
    --include-idle 时会误删正在交易币种的账本。
    """
    cfg = csr.load_strategy_config_cached(account)
    if not isinstance(cfg, dict) or not cfg.get('currencies'):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'config', 'config_trend_range.json')
        import json
        try:
            with open(path, 'r', encoding='utf-8') as fp:
                cfg = json.load(fp) or {}
        except Exception:
            cfg = {}
    return {str(c.get('instId') or '').strip()
            for c in (cfg.get('currencies') or []) if c.get('instId')}


def _slot_states(s: dict) -> str:
    """把“让该条目被归为有敞口”的槽位逐个写出来（状态|标记|挂单年龄）。"""
    out = []
    for b in ('trend', 'range'):
        for name, sl in (((s or {}).get(b) or {}).get('slots') or {}).items():
            if not slot_unsettled(sl):
                continue
            if not isinstance(sl, dict):
                out.append(f'{b}.{name}=非字典')
                continue
            st = str(sl.get('state') or 'IDLE')
            tags = [t for t, on in (('unknown', sl.get('unknown')),
                                    ('cancel_req', sl.get('cancel_requested')),
                                    ('qfail', sl.get('qfail_logged'))) if on]
            age = ((time.time() - float(sl.get('placed_ts') or 0)) / 60
                   if sl.get('placed_ts') else 0)
            out.append(f'{b}.{name}={st}'
                       f'{"|" + ",".join(tags) if tags else ""}'
                       f'(存续{age:.0f}min,量{sl.get("amount", 0)})')
    for k, v in ((s or {}).get('market_orders') or {}).items():
        if slot_unsettled(v):
            out.append(f'market[{k}]={str((v or {}).get("state") or "?")}')
    for d, r in (((s or {}).get('trend') or {}).get('algo') or {}).items():
        if r:
            out.append(f'algo.trend.{d}在位')
    for d, r in (((s or {}).get('range') or {}).get('algo') or {}).items():
        if r:
            out.append(f'algo.range.{d}在位')
    return ' | '.join(out) or '（无在途槽位，应是快照/持仓残留）'


def _detail(s: dict) -> str:
    """--detail 用的一行摘要：账本记着的持仓量/在途槽位/风险标记。

    用途是区分"真敞口要人工平仓"与"残留脏数据要人工清账本"——两者在
    PROTECTED 分类里长得一样，但处置方式完全不同。
    """
    s = s or {}
    held = {d: round(sum(abs(_f((s.get(b) or {}).get('held', {}).get(d)))
                         for b in ('trend', 'range')), 4) for d in ('long', 'short')}
    snap = {d: _f(v) for d, v in (s.get('exchange_totals') or {}).items()}
    flags = [k for k in RISK_KEYS if s.get(k)]
    return (f'账本持多{held["long"]}/持空{held["short"]}张 '
            f'快照{snap or "-"} 标记{",".join(flags) or "无"}')


def classify(ledger, configured, exists_of):
    """把账本条目分成 PROTECTED / GHOST / IDLE 三类，返回 {类别: [(inst, 说明)]}"""
    out = {'PROTECTED': [], 'GHOST': [], 'IDLE': []}
    for inst, s in sorted((ledger or {}).items()):
        flags = [k for k in RISK_KEYS if (s or {}).get(k)]
        if not inst:
            continue
        if inst in configured:
            out['PROTECTED'].append((inst, '在当前交易配置中'))
            continue
        if has_open_interest(s):
            out['PROTECTED'].append((inst, '账本记着持仓/在途委托，需人工处理'))
            continue
        exists = exists_of(inst)
        if exists is False:
            out['GHOST'].append(
                (inst, f'交易所已无此合约（51001）'
                       f'{("；残留风险标记 " + ",".join(flags)) if flags else ""}'))
        elif exists is None:
            out['PROTECTED'].append((inst, '合约存在性未知（OKX 查询失败），保守不删'))
        else:
            out['IDLE'].append(
                (inst, f'合约存在但账本已空且不在配置'
                       f'{("；残留风险标记 " + ",".join(flags)) if flags else ""}'))
    return out


def _load_ledger(account, environment):
    with session_scope() as s:
        return state_repo.load_position_state(
            s, account=account, environment=environment) or {}


def _delete(insts, account, environment):
    """删除指定合约的账本条目（V2 走 kv_store 行，legacy 走四张拆表）"""
    deleted = []
    with session_scope() as session:
        if account is not None:
            prefix = state_repo._position_prefix(account, environment)
            for inst in insts:
                key = prefix + inst
                if session.get(KVStore, key) is not None:
                    csr.delete_json_config(session, key)
                    deleted.append(key)
        else:
            for model in (PosBook, PosSlot, PosAlgo, PosLev):
                for inst in insts:
                    session.execute(delete(model).where(model.inst_id == inst))
            deleted.extend(f'{model.__tablename__}:{i}' for model
                           in (PosBook, PosSlot, PosAlgo, PosLev) for i in insts)
    return deleted


def main(argv):
    apply = '--apply' in argv
    stopped = '--scheduler-stopped' in argv
    include_idle = '--include-idle' in argv
    detail = '--detail' in argv
    account = environment = None
    for a in argv:
        if a.startswith('--account='):
            account = a.split('=', 1)[1].strip()
    if account in (None, '') and '--account' in argv:
        i = argv.index('--account')
        if i + 1 < len(argv) and not argv[i + 1].startswith('--'):
            account = argv[i + 1].strip()
    if account == 'legacy':
        account = None

    from crypto.api_config import DEFAULT_ACCOUNT, get_api_config
    if account is None:
        from crypto.trading_runtime_repo import load_runtime
        account = (load_runtime() or {}).get('account') or DEFAULT_ACCOUNT
        print(f'未指定 --account，按实盘运行状态记录取 {account!r}；'
              f'如要清无账号的旧表请显式传 --account legacy')
    if account is not None:
        environment = str(get_api_config(account).get('flag', '0') or '0')

    eng = get_engine()
    print('═' * 68)
    print(f' 目标库    : {eng.url.render_as_string(hide_password=True)}')
    print(f' 账号/环境 : {account} / flag={environment}'
          f'（{"实盘" if environment == "0" else "模拟盘"}）')
    print(f' 模式      : {"★ 写入（删除账本条目）" if apply else "只读体检"}')
    print('═' * 68)

    if apply and not stopped:
        print('✗ 缺 --scheduler-stopped：实盘调度器在跑时改账本会撞上 _revision 乐观锁，')
        print('  导致其被永久 latch 而彻底停止开仓。请先停调度器再带该确认旗标。')
        return 2

    ledger = _load_ledger(account, environment)
    configured = configured_insts(account)
    print(f'\n账本条目 {len(ledger)} 个 / 当前配置币种 {len(configured)} 个'
          f'（{",".join(sorted(configured)) or "无"}）')

    from crypto.task.utils.instrument_spec import get_instrument_spec_cache
    cache = get_instrument_spec_cache(environment)

    def exists_of(inst):
        try:
            return cache.instrument_exists(inst)
        except Exception as e:
            print(f'  ! {inst} 合约存在性查询异常，按未知处理: {e}')
            return None

    groups = classify(ledger, configured, exists_of)
    for label, title in (('PROTECTED', '受保护（绝不删除）'),
                         ('GHOST', '幽灵合约（建议删除）'),
                         ('IDLE', '闲置空账本（仅 --include-idle 才删）')):
        print(f'\n──── {title}：{len(groups[label])} 个 ────')
        for inst, why in groups[label]:
            print(f'  {inst:20} {why}')
            if detail and inst not in configured:
                print(f'  {"":20} {_detail(ledger.get(inst))}')
                if label == 'PROTECTED':
                    print(f'  {"":20} 被保留原因→ {_slot_states(ledger.get(inst))}')

    targets = [i for i, _ in groups['GHOST']] + ([i for i, _ in groups['IDLE']]
                                                if include_idle else [])
    if not targets:
        print('\n无需清理：没有发现可安全删除的账本条目。')
        return 0
    if not apply:
        print(f'\n体检完成：可安全删除 {len(targets)} 个 → {",".join(targets)}')
        print('加 --apply --scheduler-stopped 才会真正删除。')
        return 0

    print(f'\n即将删除 {len(targets)} 个账本条目: {",".join(targets)}')
    done = _delete(targets, account, environment)
    print(f'已删除 {len(done)} 项。剩余账本条目 {len(_load_ledger(account, environment))} 个。')
    print('下一步：重新启动实盘调度器，第一轮起这些条目不会再进入对账范围。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
