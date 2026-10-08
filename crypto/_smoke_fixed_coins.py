#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：固定币种动态管理（移除选中 / 提升为固定 / CSV 同步固定）+ 宇宙一致性
================================================================================
安全等级：🔒 纯离线 —— config.json、crypto_coins 宇宙(DB表 + CSV文件) 全部打桩到
临时目录/内存假 repo，不连生产库、不改真 config.json、不改真 crypto_coins.csv、
不调 OKX、不发信。

覆盖 crypto/real_strategy_adapter.py：
  remove_fixed_coins / promote_floating_to_fixed / sync_fixed_from_csv
  以及它们对 crypto_coins「宇宙」的同步维护（_universe_remove_coins/_universe_add_coins）。

⚠️ 契约要点（本次修复的核心）：
  - 宇宙 = batch_trend_updater 的输入清单，也是 get_csv_coins()/「CSV同步固定」的数据源。
  - 「移除选中」必须同时删宇宙，否则一按「CSV同步固定」下架币就复活（历史 Bug）。
  - 「提升为固定」必须同时写宇宙，否则一按「CSV同步固定」刚提升的币被丢弃（历史 Bug）。
  - DB 与 CSV 列数可能不同（真库 DB=48列含15m，旧 CSV=37列）；CSV 写回须保留自身表头。

场景清单：
  A. 移除：config(all_coins/selected/starred/floating) + 宇宙(DB & CSV) 同步剔除
  B. 【问题二】移除后「CSV同步固定」不复活下架币（all_coins == 宇宙 == 移除后集合）
  C. 【问题一】提升后写入宇宙(DB & CSV)，且「CSV同步固定」不丢弃已提升币
  D. 护栏：移除到固定列表为空 → ValueError，且宇宙不被误清
  E. 去重：提升一个"既在固定又在浮动"的币 → 不重复加入固定、从浮动清除
  F. 宇宙新增行内容：symbol/inst_id/rank 正确；CSV 表头保持不变（列数不漂移）
  G. 护栏：宇宙为空 → sync 抛 ValueError

何时重跑：改上述三个函数、_universe_* helper、get_csv_coins 语义，
或 load_config/save_config、market_data_repo 契约时。
"""
import os
import sys
import json
import csv as _csv
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    sys.path.insert(0, _p)

import crypto.real_strategy_adapter as rsa  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


# ------------------------------------------------------------------ 沙箱装配
_TMP = tempfile.mkdtemp(prefix='smoke_fixed_coins_')
_ORIG = {
    'CONFIG_PATH': rsa.CONFIG_PATH,
    '_UNIVERSE_CSV': rsa._UNIVERSE_CSV,
    '_config_store_repo': rsa._config_store_repo,
    '_db_session_scope': rsa._db_session_scope,
    '_market_repo': rsa._market_repo,
    'get_csv_coins': rsa.get_csv_coins,
}

rsa.CONFIG_PATH = os.path.join(_TMP, 'config.json')        # 别写真 config.json
rsa._UNIVERSE_CSV = os.path.join(_TMP, 'crypto_coins.csv')  # 别写真 crypto_coins.csv

# DB 行用「宽表头」(7列，含 MACD/15m)，CSV 用「窄表头」(5列) —— 复现真库 48 vs 37 差异
_DB_KEYS = ['rank', 'symbol', 'inst_id', 'name_cn', '1H_趋势', 'MACD_1H', '15m_趋势']
_CSV_KEYS = ['rank', 'symbol', 'inst_id', 'name_cn', '1H_趋势']


class FakeMarketRepo:
    """忠实模拟 market_data_repo：save_coin_rows 为整表覆盖，load 按 rank 升序。"""

    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]
        self.save_calls = 0

    def load_coin_rows(self, session):
        rows = [dict(r) for r in self._rows]
        rows.sort(key=lambda r: (int(r['rank']) if str(r.get('rank', '')).strip().isdigit() else 10 ** 9))
        return rows

    def save_coin_rows(self, session, rows):
        self._rows = [dict(r) for r in rows]   # 整表覆盖（与真实语义一致）
        self.save_calls += 1

    def coin_inst_ids(self, session):
        return [r['inst_id'].strip() for r in self.load_coin_rows(session)
                if r.get('inst_id', '').strip()]


class _GoodRepo:
    """只桩 save（成功）；不提供 load_json_config_cached → load_config 回退临时文件。"""
    KEY_COIN_SELECTION = 'coin_selection'

    @staticmethod
    def save_json_config(session, key, value):
        return True


def _scope_ok():
    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False
    return _Ctx()


def _mk_row(keys, rank, inst_id):
    row = {k: '' for k in keys}
    row['rank'] = str(rank)
    row['symbol'] = inst_id.split('-')[0]
    row['inst_id'] = inst_id
    return row


def seed_universe(inst_ids):
    """按同一批 inst_id 同时初始化「假 DB(宽表头)」与「临时 CSV(窄表头)」。"""
    db_rows = [_mk_row(_DB_KEYS, i + 1, iid) for i, iid in enumerate(inst_ids)]
    csv_rows = [_mk_row(_CSV_KEYS, i + 1, iid) for i, iid in enumerate(inst_ids)]
    rsa._market_repo = FakeMarketRepo(db_rows)
    with open(rsa._UNIVERSE_CSV, 'w', encoding='utf-8', newline='') as f:
        w = _csv.DictWriter(f, fieldnames=_CSV_KEYS)
        w.writeheader()
        w.writerows(csv_rows)


def db_ids():
    return rsa._market_repo.coin_inst_ids(object())


def csv_ids():
    with open(rsa._UNIVERSE_CSV, 'r', encoding='utf-8', newline='') as f:
        return [r['inst_id'] for r in _csv.DictReader(f) if r.get('inst_id')]


def csv_header():
    with open(rsa._UNIVERSE_CSV, 'r', encoding='utf-8', newline='') as f:
        return next(_csv.reader(f))


def seed_cfg(cfg):
    with open(rsa.CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False)


def read_cfg():
    with open(rsa.CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


_BTC, _ETH, _XRP, _DOGE, _SHIB = (f'{s}-USDT-SWAP' for s in ('BTC', 'ETH', 'XRP', 'DOGE', 'SHIB'))
_PUMP = 'PUMP-USDT-SWAP'

try:
    rsa._config_store_repo, rsa._db_session_scope = _GoodRepo, _scope_ok

    # ============ A + B：移除同步删宇宙，且 CSV 同步不复活（问题二）============
    seed_universe([_BTC, _ETH, _XRP, _DOGE, _SHIB])           # 宇宙 5 个
    seed_cfg({"all_coins": [_BTC, _ETH, _XRP, _DOGE, _SHIB],  # 固定 5 个
              "default_selected": [_BTC, _SHIB],
              "starred_coins": [_SHIB, _PUMP],
              "floating_coins": [_PUMP]})
    removed = rsa.remove_fixed_coins([_SHIB, _DOGE])
    cfg = read_cfg()
    check("A1 返回被移除项(保持固定列表顺序)", removed == [_DOGE, _SHIB], removed)
    check("A2 all_coins 剔除", cfg["all_coins"] == [_BTC, _ETH, _XRP], cfg["all_coins"])
    check("A3 default_selected 同步剔除", cfg["default_selected"] == [_BTC], cfg["default_selected"])
    check("A4 starred 同步剔除(SHIB走/PUMP留)", cfg["starred_coins"] == [_PUMP], cfg["starred_coins"])
    check("A5 floating 不受影响", cfg["floating_coins"] == [_PUMP], cfg["floating_coins"])
    check("A6 宇宙DB 同步删除", db_ids() == [_BTC, _ETH, _XRP], db_ids())
    check("A7 宇宙CSV 同步删除", csv_ids() == [_BTC, _ETH, _XRP], csv_ids())

    # B：移除后按「CSV同步固定」→ 不能把 DOGE/SHIB 复活
    new_fixed = rsa.sync_fixed_from_csv()   # get_csv_coins 走假 DB → [BTC,ETH,XRP]
    cfg = read_cfg()
    check("B1 同步后 all_coins==宇宙(不复活)", new_fixed == [_BTC, _ETH, _XRP], new_fixed)
    check("B2 同步后 all_coins 落库一致", cfg["all_coins"] == [_BTC, _ETH, _XRP], cfg["all_coins"])

    # ============ C：提升写入宇宙，且 CSV 同步不丢弃（问题一）============
    seed_cfg({"all_coins": [_BTC, _ETH, _XRP],
              "default_selected": [_BTC, _PUMP],
              "starred_coins": [_PUMP],
              "floating_coins": [_PUMP]})
    promoted = rsa.promote_floating_to_fixed([_PUMP])
    cfg = read_cfg()
    check("C1 返回被提升项", promoted == [_PUMP], promoted)
    check("C2 all_coins 追加", cfg["all_coins"] == [_BTC, _ETH, _XRP, _PUMP], cfg["all_coins"])
    check("C3 floating 清空", cfg["floating_coins"] == [], cfg["floating_coins"])
    check("C4 宇宙DB 新增 PUMP", db_ids() == [_BTC, _ETH, _XRP, _PUMP], db_ids())
    check("C5 宇宙CSV 新增 PUMP", csv_ids() == [_BTC, _ETH, _XRP, _PUMP], csv_ids())
    # 提升后按「CSV同步固定」→ PUMP 不能被丢弃
    new_fixed = rsa.sync_fixed_from_csv()
    cfg = read_cfg()
    check("C6 同步后 PUMP 仍在固定(不丢弃)", new_fixed == [_BTC, _ETH, _XRP, _PUMP], new_fixed)
    check("C7 同步后 all_coins 落库含 PUMP", cfg["all_coins"] == [_BTC, _ETH, _XRP, _PUMP], cfg["all_coins"])

    # ============ D：护栏 —— 移除到空 → ValueError，宇宙不被误清 ============
    before_db, before_csv = db_ids(), csv_ids()
    try:
        rsa.remove_fixed_coins([_BTC, _ETH, _XRP, _PUMP])   # 试图移除全部固定
        check("D1 移除全部固定被拒(抛ValueError)", False, "未抛异常")
    except ValueError as ve:
        check("D1 移除全部固定被拒(抛ValueError)", "至少保留一个固定币种" in str(ve), str(ve))
    check("D2 宇宙DB 未被误清", db_ids() == before_db, db_ids())
    check("D3 宇宙CSV 未被误清", csv_ids() == before_csv, csv_ids())

    # ============ E：去重 —— 提升"既固定又浮动"的币 ============
    seed_universe([_BTC, _ETH])
    seed_cfg({"all_coins": [_BTC, _ETH], "default_selected": [_BTC],
              "starred_coins": [], "floating_coins": [_BTC]})   # BTC 同时在固定与浮动
    promoted = rsa.promote_floating_to_fixed([_BTC])
    cfg = read_cfg()
    check("E1 返回处理项", promoted == [_BTC], promoted)
    check("E2 all_coins 不重复加入", cfg["all_coins"] == [_BTC, _ETH], cfg["all_coins"])
    check("E3 floating 清除重复态", cfg["floating_coins"] == [], cfg["floating_coins"])
    check("E4 宇宙未重复新增", db_ids() == [_BTC, _ETH] and csv_ids() == [_BTC, _ETH],
          f"db={db_ids()} csv={csv_ids()}")

    # ============ F：新增行内容 + CSV 表头不漂移 ============
    seed_universe([_BTC])
    seed_cfg({"all_coins": [_BTC], "default_selected": [], "starred_coins": [],
              "floating_coins": [_PUMP]})
    rsa.promote_floating_to_fixed([_PUMP])
    with open(rsa._UNIVERSE_CSV, 'r', encoding='utf-8', newline='') as f:
        crows = list(_csv.DictReader(f))
    pump_csv = next((r for r in crows if r['inst_id'] == _PUMP), None)
    pump_db = next((r for r in rsa._market_repo.load_coin_rows(object())
                    if r['inst_id'] == _PUMP), None)
    check("F1 CSV表头列数不变(窄表头)", csv_header() == _CSV_KEYS, csv_header())
    check("F2 CSV新增行 symbol 正确", pump_csv and pump_csv['symbol'] == 'PUMP', pump_csv)
    check("F3 CSV新增行 rank 递增", pump_csv and pump_csv['rank'] == '2', pump_csv)
    check("F4 DB新增行用宽表头(含15m列)", pump_db and set(_DB_KEYS).issubset(pump_db.keys()),
          list(pump_db.keys()) if pump_db else None)

    # ============ G：护栏 —— 宇宙为空 → sync 抛 ValueError ============
    rsa.get_csv_coins = lambda: []   # 直接桩空，避免回退去读真 CSV
    seed_cfg({"all_coins": [_BTC], "default_selected": [], "starred_coins": [], "floating_coins": []})
    try:
        rsa.sync_fixed_from_csv()
        check("G1 宇宙为空取消同步(抛ValueError)", False, "未抛异常")
    except ValueError as ve:
        check("G1 宇宙为空取消同步(抛ValueError)", "CSV" in str(ve), str(ve))

finally:
    for k, v in _ORIG.items():
        setattr(rsa, k, v)
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n" + "=" * 56)
print(f"  PASS {len(PASS)}  /  FAIL {len(FAIL)}")
if FAIL:
    print("  失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
