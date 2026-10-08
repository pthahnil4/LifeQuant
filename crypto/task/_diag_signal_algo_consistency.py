# -*- coding: utf-8 -*-
"""核查按账号配置覆盖 + signal_algo 全链路一致性（只读，不写库）。

输出：
  A. kv_store 里所有 strategy_config* key 清单（哪些账号已落盘专属配置）
  B. 每个配置源里 5 币种的 signal_algo（趋/长周期算法口径）
  C. 本地文件 config_trend_range.json 的 signal_algo
  D. trading_runtime.account（当前实盘运行账号）
"""
import os
import sys
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

from crypto.database import session_scope, get_engine       # noqa: E402
from crypto import config_store_repo as csr                 # noqa: E402
from crypto.models import KVStore                           # noqa: E402
from sqlalchemy import select                               # noqa: E402

_FILE_CFG = os.path.join(_HERE, 'config', 'config_trend_range.json')


def algo_summary(cfg):
    if not cfg:
        return '(无)'
    out = []
    for c in (cfg.get('currencies') or []):
        inst = str(c.get('instId', '?')).split('-')[0]
        out.append(f"{inst}={c.get('signal_algo', '(缺省→diff)')}")
    return ' '.join(out) or '(无币种)'


eng = get_engine()
safe = str(eng.url).split('@')[-1] if '@' in str(eng.url) else str(eng.url)
print(f"目标库: {safe}\n")

with session_scope() as ses:
    rows = ses.execute(
        select(KVStore.key, KVStore.updated_at)
        .where(KVStore.key.like('strategy_config%'))
        .order_by(KVStore.key)
    ).all()
    print('A. kv_store 中所有 strategy_config* key：')
    for k, u in rows:
        print(f'   - {k}   updated_at={u}')
    if not rows:
        print('   (无任何 strategy_config* key)')

    print('\nB. 各配置源 signal_algo：')
    for acct in ('main', 'stageone', 'quantlimit'):
        key = csr.strategy_config_key(acct)
        cfg = csr.load_json_config(ses, key)
        tag = '专属存在' if cfg is not None else '专属缺失→回退全局'
        print(f'   [{acct:10}] {key:28} ({tag})')
        print(f'                signal_algo: {algo_summary(cfg)}')

    gcfg = csr.load_json_config(ses, csr.KEY_STRATEGY_CONFIG)
    print(f'   [全局兜底  ] {csr.KEY_STRATEGY_CONFIG:28}')
    print(f'                signal_algo: {algo_summary(gcfg)}')

    rt = csr.load_json_config(ses, csr.KEY_TRADING_RUNTIME) or {}
    print(f'\nD. trading_runtime.account = {rt.get("account")!r}  '
          f'desired_running={rt.get("desired_running")} auto_resume={rt.get("auto_resume")}')

print('\nC. 本地文件 config_trend_range.json：')
try:
    with open(_FILE_CFG, 'r', encoding='utf-8') as f:
        fcfg = json.load(f)
    print(f'   signal_algo: {algo_summary(fcfg)}')
except Exception as e:
    print(f'   读取失败: {e}')

print('\n--- 缓存读取链（交易端 trend_range_trader 实际走的路径）---')
for acct in ('main', 'stageone', 'quantlimit'):
    print(f'   load_strategy_config_cached({acct:10}) -> {algo_summary(csr.load_strategy_config_cached(acct))}')
