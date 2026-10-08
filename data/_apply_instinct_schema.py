# -*- coding: utf-8 -*-
"""批次12建表执行器：instinct_* 四张表（checkfirst，已存在则零 DDL）

与存量库升级路径等价：应用侧 database.init_db() 的 _NEW_TABLE_NAMES
也会自动补建这四张表；本脚本用于 P1 阶段显式执行 + 结果核验，
不 DROP、不改任何存量表，可安全重复执行。

用法：python data/_apply_instinct_schema.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import text

from crypto.database import get_engine, Base, resolve_db_url  # noqa: E402
from crypto import models  # noqa: F401,E402  确保模型注册到 Base.metadata

TABLES = ('instinct_corpus', 'instinct_wiki_rules',
          'instinct_predictions', 'instinct_embeddings')

print(f'目标库: {resolve_db_url().split("@")[-1].split("?")[0]}')

engine = get_engine()
tables = [Base.metadata.tables[n] for n in TABLES]
Base.metadata.create_all(engine, tables=tables, checkfirst=True)
print('[OK] create_all(checkfirst) 执行完成')

with engine.connect() as conn:
    dbname = conn.execute(text('SELECT DATABASE()')).scalar()
    for t in TABLES:
        n = conn.execute(text(
            'SELECT COUNT(*) FROM information_schema.tables '
            'WHERE table_schema = :db AND table_name = :t'),
            {'db': dbname, 't': t}).scalar()
        cnt = conn.execute(text(f'SELECT COUNT(*) FROM `{t}`')).scalar() if n else -1
        print(f'  {t}: exists={bool(n)} rows={cnt}')
