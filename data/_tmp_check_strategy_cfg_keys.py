# -*- coding: utf-8 -*-
"""只读排查：KV 里各账号交易配置的币种清单（临时脚本，可删）"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from sqlalchemy import text
from crypto.database import session_scope

with session_scope() as s:
    rows = s.execute(text(
        "SELECT `key`, LENGTH(value) AS len, updated_at FROM kv_store "
        "WHERE `key` LIKE 'strategy%' OR `key` = 'trading_runtime'"
    )).fetchall()
    for k, ln, upd in rows:
        print(f"== {k}  (len={ln}, updated_at={upd})")
        v = s.execute(text("SELECT value FROM kv_store WHERE `key`=:k"), {"k": k}).scalar()
        try:
            data = json.loads(v)
        except Exception as e:
            print("   JSON解析失败:", e)
            continue
        if k == 'trading_runtime':
            print("  ", data)
            continue
        coins = data.get('currencies') or []
        print(f"   币种数={len(coins)}")
        for c in coins:
            rp = c.get('range_position') or {}
            tp = c.get('trend_position') or {}
            print(f"   - {c.get('instId')} trade_enabled={c.get('trade_enabled')} "
                  f"manual_direction={c.get('manual_direction')} "
                  f"range.enabled={rp.get('enabled')} trend.enabled={tp.get('enabled')}")
