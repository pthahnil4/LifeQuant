# -*- coding: utf-8 -*-
"""静态扫描：确认 plan_routes.py 的 GET（读）路径不再包含任何写库调用。

原理：按行扫描，记录"当前所处路由 + 其 methods + 所处函数名"，
凡是出现 _save_plans / save_plans_data / session.add / session.delete / commit
的行，若归属的路由是纯 GET，即判为读路径带写入（缺陷）。
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'crypto', 'plan_routes.py')

WRITE_PAT = re.compile(r'_save_plans\(|save_plans_data\(|session\.add\(|session\.delete\(|\.commit\(')
ROUTE_PAT = re.compile(r"@plan_bp\.route\(\s*'([^']+)'(.*)$", re.S)
METHOD_PAT = re.compile(r"methods=\[([^\]]*)\]")
FUNC_PAT = re.compile(r"^def (\w+)")

cur_route, cur_methods, cur_func = None, [], None
GET_WRITES = []
ALL_WRITES = []

with io.open(TARGET, encoding='utf-8') as f:
    for lineno, line in enumerate(f, 1):
        m = ROUTE_PAT.search(line)
        if m:
            cur_route = m.group(1)
            mm = METHOD_PAT.search(m.group(2))
            cur_methods = [x.strip().strip("'\"") for x in mm.group(1).split(',')] if mm else ['GET']
        fm = FUNC_PAT.match(line)
        if fm:
            cur_func = fm.group(1)
        if WRITE_PAT.search(line):
            item = (lineno, cur_route, tuple(cur_methods), cur_func, line.strip()[:80])
            ALL_WRITES.append(item)
            if cur_route and all(x.upper() == 'GET' for x in cur_methods):
                GET_WRITES.append(item)

print(f'扫描 {TARGET}')
print(f'写库调用总数：{len(ALL_WRITES)}')
print(f'其中位于纯 GET 路由内（读路径带写入）：{len(GET_WRITES)}')
for it in GET_WRITES:
    print('  [缺陷] L%d %s %s -> %s | %s' % it)

# 迁移函数（惰性归一化）必须不含写库
from crypto import plan_routes as pr  # noqa: E402

for fn in (pr._migrate_daily_rule, pr._migrate_record_fields, pr._migrate_tasks):
    body = fn.__code__.co_names
    bad = [n for n in body if n in ('_save_plans',)]
    print(f'{fn.__name__}: 引用写库符号={bad or "无"} 参数={fn.__code__.co_varnames[:fn.__code__.co_argcount]}')

sys.exit(1 if GET_WRITES else 0)
