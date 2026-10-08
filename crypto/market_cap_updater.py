#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
"""
市值榜固定币种池刷新与明细（CoinGecko + OKX 公共接口）
==============================================================================
与 data/update_fixed_coins_top50.py 同源逻辑的模块版，供 Flask 接口
/task 交易配置页与命令行脚本共同调用，避免两处逻辑漂移。

口径：
  - 排名源：CoinGecko /coins/markets?order=market_cap_desc（公开接口，无 key，
    一次拉取即含价格/24H涨跌/成交额/流通市值，供明细面板复用）
  - 可交易校验：OKX /api/v5/public/instruments?instType=SWAP（线性 + USDT 保证金）；
    本机网络对 www.okx.com 的 TLS 握手可能被中断，按域名逐个回退
  - 剔除法币稳定币与包装/合成资产（WBTC/WETH/STETH 等，锚定底层币，
    纳入只会重复占用池位）
  - 落库走 real_strategy_adapter 既有双写通道（DB 主存 + config.json 兜底），
    并把新币种同步进 crypto_coins「宇宙」（DB 表 + CSV），否则下次
    「CSV同步固定」会丢新币、批量趋势分析也不会覆盖它们。
  - 刷新成功后把 inst_id → 市值排名 写入 config['market_ranks']，
    real_strategy_adapter 读取固定池时据此按市值降序输出（前端所有展示点同源）。

安全：只读行情接口 + 本地配置/DB 写入，不下单、不动交易状态。
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# 双模式导入：Flask 包内（cryptoTrade 根在 sys.path）/ 独立脚本（仅 crypto 目录）
try:
    from crypto import real_strategy_adapter as _rsa
except ImportError:
    import real_strategy_adapter as _rsa

TOP_N = 50          # 固定池目标规模
FETCH_TOP = 100     # 市值榜取前 100 再过滤（剔除稳定/包装币后仍能凑足 50）
_CACHE_TTL = 120    # 市值快照缓存秒数（面板一键刷新会显式 force 绕开）

# 法币稳定币 + 锚定/包装资产：与底层资产价格联动，作为趋势监控标的没有意义
EXCLUDE_SYMBOLS = {
    'USDT', 'USDC', 'DAI', 'FDUSD', 'USDE', 'TUSD', 'USDS', 'PYUSD', 'BUSD',
    'USDD', 'GUSD', 'LUSD', 'FRAX', 'USD1', 'USDF', 'BFUSD', 'XUSD',
    'WBTC', 'CBBTC', 'TBTC', 'BTCB',          # 包装/锚定 BTC
    'WETH', 'STETH', 'WSTETH', 'WEETH', 'EETH', 'RSETH', 'LSETH', 'OSETH',
    'WBETH',
}
# 注：ETHFI 等生态治理币波动充分，属于有效趋势标的，不在剔除之列。

_HEADERS = {'User-Agent': 'Mozilla/5.0 (market-cap-updater)'}

# 市值快照缓存：CoinGecko 免费档限频严格，面板刷新 + 排序刷新共用一份；
# 拉取失败时沿用最后一次成功数据（标记 stale），保证界面不因外网抖动开天窗。
_cache_lock = threading.Lock()
_snapshot_cache = {'ts': 0.0, 'rows': None}
_refresh_lock = threading.Lock()   # 防并发点击「更新市值列表」


def fetch_marketcap_top(n: int = FETCH_TOP) -> List[Dict]:
    """CoinGecko 市值榜（降序）。失败时回退缓存（哪怕是过期的）。"""
    url = ('https://api.coingecko.com/api/v3/coins/markets'
           f'?vs_currency=usd&order=market_cap_desc&per_page={n}&page=1'
           '&sparkline=false')
    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=30)
            if r.status_code == 200:
                rows = r.json()
                if isinstance(rows, list) and rows:
                    return rows
                last_err = 'empty payload'
            else:
                last_err = f'HTTP {r.status_code}'
        except Exception as e:
            last_err = repr(e)[:160]
        wait = 3 * attempt
        logger.warning("市值榜获取失败(%s)，%ds 后重试", last_err, wait)
        if attempt < 3:
            time.sleep(wait)
    with _cache_lock:
        cached = _snapshot_cache['rows']
        ts = _snapshot_cache['ts']
    if cached:
        logger.warning("市值榜拉取失败，沿用 %ds 前的缓存（标记 stale）", int(time.time() - ts))
        return cached
    raise RuntimeError(f'CoinGecko 市值榜获取失败：{last_err}')


def get_market_snapshot(force: bool = False) -> Tuple[List[Dict], float, bool]:
    """带 TTL 缓存的市值快照 → (rows, fetched_at, stale)

    stale=True 表示本次外网拉取失败、返回的是最后一次成功的旧数据。
    """
    now = time.time()
    with _cache_lock:
        rows, ts = _snapshot_cache['rows'], _snapshot_cache['ts']
    if rows and not force and (now - ts) < _CACHE_TTL:
        return rows, ts, False
    try:
        fresh = fetch_marketcap_top()
        ok = True
    except RuntimeError:
        if rows:
            fresh, ok = rows, False     # 失败回退旧数据
        else:
            raise
    if ok:
        with _cache_lock:
            _snapshot_cache['rows'] = fresh
            _snapshot_cache['ts'] = time.time()
        return fresh, _snapshot_cache['ts'], False
    return fresh, ts, True


def fetch_okx_swap_insts() -> set:
    """OKX 公共接口：全部 USDT 线性永续合约 instId 集合（域名回退）"""
    last_err = None
    for host in ('https://www.okx.com', 'https://okx.com', 'https://app.okx.com'):
        try:
            r = requests.get(host + '/api/v5/public/instruments',
                             params={'instType': 'SWAP'}, headers=_HEADERS, timeout=30)
            data = r.json()
            if data.get('code') != '0':
                raise RuntimeError(f'OKX 合约列表返回异常: {data.get("msg")}')
            # 注：SWAP 品种的 quoteCcy 返回为空，USDT 保证金直接用 instId 后缀判定
            insts = {it['instId'] for it in data['data']
                     if it.get('ctType') == 'linear'
                     and it.get('instId', '').endswith('-USDT-SWAP')}
            if insts:
                return insts
            last_err = 'empty instrument list'
        except Exception as e:
            last_err = repr(e)[:160]
            logger.warning("OKX 合约列表 %s 获取失败: %s", host, last_err)
    raise RuntimeError(f'OKX 合约列表获取失败：{last_err}')


def build_top_fixed_coins(market_rows: List[Dict], okx_insts: set,
                          top_n: int = TOP_N) -> Tuple[List[str], Dict[str, int]]:
    """市值降序 → 过滤稳定/包装币与无合约币 → 前 top_n 个 → (inst列表, rank映射)"""
    picked, ranks = [], {}
    for row in market_rows:
        sym = (row.get('symbol') or '').upper()
        rank = row.get('market_cap_rank')
        if not sym or sym in EXCLUDE_SYMBOLS or not rank:
            continue
        inst = f'{sym}-USDT-SWAP'
        if inst not in okx_insts:
            continue
        picked.append(inst)
        ranks[inst] = int(rank)
        if len(picked) >= top_n:
            break
    return picked, ranks


def refresh_fixed_coins(top_n: int = TOP_N) -> Dict:
    """按实时市值榜替换固定币种池（config + 宇宙 DB/CSV 全链路落库）

    Returns dict: {total, added, removed, ranks, universe_added, db_ok,
                   old_coins, new_coins}
    """
    if not _refresh_lock.acquire(blocking=False):
        raise RuntimeError('已有一轮市值列表更新正在进行，请稍候再试')
    try:
        rows, _, stale = get_market_snapshot(force=True)
        if stale:
            raise RuntimeError('CoinGecko 市值榜拉取失败（外网不可达），本次未做任何改动')
        okx_insts = fetch_okx_swap_insts()
        new_fixed, ranks = build_top_fixed_coins(rows, okx_insts, top_n)
        if len(new_fixed) < top_n:
            logger.warning("市值榜过滤后仅 %d 个可交易币种（目标 %d）", len(new_fixed), top_n)

        cfg = _rsa.load_config()
        old_fixed = list(cfg.get('all_coins', []))
        floating = list(cfg.get('floating_coins', []))
        new_set = set(new_fixed)

        # 新币先入宇宙（DB + CSV），再换固定池 —— 顺序保证「CSV同步固定」不丢新币
        universe_added = _rsa._universe_add_coins(new_fixed)

        keep = new_set | set(floating)
        cfg['all_coins'] = new_fixed
        cfg['market_ranks'] = ranks            # 读取侧按市值降序输出的依据
        sel = [c for c in cfg.get('default_selected', []) if c in new_set]
        cfg['default_selected'] = sel or new_fixed[:3]
        cfg['starred_coins'] = [c for c in cfg.get('starred_coins', []) if c in keep]
        cfg['floating_coins'] = [c for c in floating if c not in new_set]
        db_ok = _rsa.save_config(cfg)

        _rsa.log(f"【市值榜刷新】固定池 {len(old_fixed)}->{len(new_fixed)} | "
                 f"宇宙新增 {len(universe_added)} | DB={db_ok}")
        return {
            'total': len(new_fixed),
            'added': [c for c in new_fixed if c not in set(old_fixed)],
            'removed': [c for c in old_fixed if c not in new_set],
            'ranks': ranks,
            'universe_added': universe_added,
            'db_ok': db_ok,
            'old_coins': old_fixed,
            'new_coins': new_fixed,
        }
    finally:
        _refresh_lock.release()


def fixed_coins_detail() -> Dict:
    """固定币种实时明细（面板数据源）：排名/价格/24H涨跌/成交额/流通市值

    流动性评分：CoinGecko 公开接口无现成字段，用 24H成交额/流通市值 比率
    （volume/mcap，越高越活跃）作代理指标并如实标注口径。
    """
    cfg = _rsa.load_config()
    fixed = _sorted_fixed_for_detail(cfg)
    rows, fetched_at, stale = get_market_snapshot()
    by_symbol = {}
    for row in rows:
        sym = (row.get('symbol') or '').upper()
        if sym and sym not in by_symbol:
            by_symbol[sym] = row

    out = []
    for inst in fixed:
        sym = inst.split('-')[0]
        cg = by_symbol.get(sym) or {}
        price = cg.get('current_price')
        chg = cg.get('price_change_percentage_24h')
        vol = cg.get('total_volume')
        mcap = cg.get('market_cap')
        liq = round(vol / mcap * 100, 1) if (vol and mcap) else None
        out.append({
            'symbol': sym,
            'inst_id': inst,
            'market_cap_rank': cg.get('market_cap_rank'),
            'price_usd': price,
            'chg_24h_pct': round(chg, 2) if isinstance(chg, (int, float)) else None,
            'volume_24h_usd': vol,
            'market_cap_usd': mcap,
            'liquidity_pct': liq,             # 成交额/流通市值 %（流动性代理）
            'listed_on_gecko': bool(cg),      # 未进市值榜前100（如手动提升的币）
            'source': 'CoinGecko' if cg else '—',
        })
    return {'coins': out, 'fetched_at': fetched_at, 'stale': stale}


def _sorted_fixed_for_detail(cfg: Dict) -> List[str]:
    ranks = cfg.get('market_ranks') or {}
    fixed = list(cfg.get('all_coins', []))
    return sorted(fixed, key=lambda c: ranks.get(c, 10 ** 9))
