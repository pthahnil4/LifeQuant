#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性：对真实库跑 init_db 增量迁移（补 diary_entry_id 列），并核实列存在。
等价于生产后台启动时 warmup_async 会做的事；幂等，列已在则零 DDL。"""
import sys
from pathlib import Path
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crypto.database import init_db, get_engine  # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

init_db()
print('init_db done')
with get_engine().connect() as c:
    n = c.execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name='daily_checkins' "
        "AND column_name='diary_entry_id'")).scalar()
print('diary_entry_id column exists:', bool(n))
sys.exit(0 if n else 1)
