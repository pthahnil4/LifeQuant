# -*- coding: utf-8 -*-
"""task_analysis_records 被清空事件的现场诊断（只读）"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from sqlalchemy import text
from crypto.database import session_scope

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

with session_scope() as s:
    url = str(s.bind.url)
    print('db target:', url.split('@')[-1] if '@' in url else url.split(':///')[-1])
    r = s.execute(text('SELECT COUNT(*), MIN(id), MAX(id), MAX(ts) FROM task_analysis_records')).one()
    print('tar count/min_id/max_id/max_ts:', tuple(r))
    # 兄弟表对照：判断是否只有 tar 被清
    for t in ('plan_slots', 'journal_notes', 'instinct_corpus', 'trading_runtime'):
        try:
            c = s.execute(text(f'SELECT COUNT(*), MIN(id), MAX(id) FROM {t}')).one()
            print(f'{t}:', tuple(c))
        except Exception as e:
            print(f'{t}: ERR {type(e).__name__}')
    # 最近其它表写入时间（看实盘 app 是否还活着）
    try:
        c = s.execute(text('SELECT MAX(ts) FROM plan_slots')).scalar()
        print('plan_slots max ts:', c)
    except Exception:
        pass
