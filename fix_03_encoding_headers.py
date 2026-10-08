# -*- coding: utf-8 -*-
"""
迁移收尾体检 · 修正 PEP 263 编码声明位置（一次性，可重复执行=幂等）
================================================================
Phase 1/2 的搬运脚本把「[迁移] 本文件由 …」说明块插在了 shebang 之后，
于是原先位于第 2 行的 `# -*- coding: utf-8 -*-` 被顶到第 5 行以后。
Python 3 源码默认按 UTF-8 解析，功能上无影响，但编码声明只有落在前两行才符合
PEP 263，编辑器/静态检查工具会误判，所以统一修正为：

    1: #!/usr/bin/env python
    2: # -*- coding: utf-8 -*-
    3: # [迁移] …（三行说明块）

只调整行位置，不改任何代码内容 → AST 不变，与 verify_01 的等价性结论不冲突。
同时已把 migrate_01/02/03 的插入逻辑改为 header_insert_pos()，后续重跑不会再出现。

用法：python -X utf8 fix_03_encoding_headers.py
"""
import os
import re
import sys

DST_ROOT = r"D:\python\cryptoTrade"
SCOPE_DIRS = [
    os.path.join(DST_ROOT, "crypto", "market_strategy"),
    os.path.join(DST_ROOT, "crypto", "futures"),
    os.path.join(DST_ROOT, "crypto", "stocks"),
]
CODING_RE = re.compile(r"^#\s*.*\bcoding[:=]")


def fix_file(path):
    """把被迁移说明块顶下去的编码声明行移回第 2 行。返回是否改动。"""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    if not lines or not lines[0].strip().startswith("#!"):
        return False
    # 编码声明已在前两行 → 无需处理
    cidx = next((i for i in range(1, min(8, len(lines)))
                 if CODING_RE.match(lines[i].strip())), None)
    if cidx is None or cidx == 1:
        return False
    coding = lines.pop(cidx)
    lines.insert(1, coding)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)
    print(f"  [FIX] {os.path.relpath(path, DST_ROOT)}  编码声明 L{cidx + 1} → L2")
    return True


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    total = fixed = 0
    for d in SCOPE_DIRS:
        if not os.path.isdir(d):
            continue
        print("=" * 70)
        print(f"扫描 {os.path.relpath(d, DST_ROOT)}")
        for root, _dirs, files in os.walk(d):
            if "__pycache__" in root:
                continue
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                total += 1
                if fix_file(os.path.join(root, name)):
                    fixed += 1
    print("=" * 70)
    print(f"完成：扫描 {total} 个 py，修正 {fixed} 个")


if __name__ == "__main__":
    main()
