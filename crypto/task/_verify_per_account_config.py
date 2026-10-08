# -*- coding: utf-8 -*-
"""验证按账号隔离生效：main 读 20U、stageone 读 1U、全局兜底仍在、runtime.account=main。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

from crypto.database import session_scope          # noqa: E402
from crypto import config_store_repo as csr        # noqa: E402


def summary(cfg):
    if not cfg:
        return '(无)'
    return ' '.join(
        "{}(趋{}/区{})".format(
            str(c.get('instId', '?')).split('-')[0],
            (c.get('trend_position') or {}).get('notional_usd'),
            (c.get('range_position') or {}).get('notional_usd'),
        )
        for c in (cfg.get('currencies') or [])
    )


with session_scope() as ses:
    print('strategy_config:main     ->', summary(csr.load_json_config(ses, csr.strategy_config_key('main'))))
    print('strategy_config:stageone ->', summary(csr.load_json_config(ses, csr.strategy_config_key('stageone'))))
    print('全局兜底 strategy_config ->', summary(csr.load_json_config(ses, csr.KEY_STRATEGY_CONFIG)))
    print('runtime.account          ->', (csr.load_json_config(ses, csr.KEY_TRADING_RUNTIME) or {}).get('account'))

print('--- 缓存读取链（交易端/网页实际走的路径）---')
print('load_strategy_config_cached(main)     ->', summary(csr.load_strategy_config_cached('main')))
print('load_strategy_config_cached(stageone) ->', summary(csr.load_strategy_config_cached('stageone')))
