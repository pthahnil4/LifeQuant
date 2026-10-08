"""
Phase 1 · 公共策略层迁移脚本（一次性执行，可重复运行=覆盖同步）
==============================================================
把 futureStockTrade 的公共策略层与期货数据层搬进 cryptoTrade，并把
"靠 sys.path 抢名字"的裸导入体系改为显式包路径导入。

映射关系：
  strategies/indicators.py                        → crypto/market_strategy/indicators.py
  strategies/pro3_strategy.py                     → crypto/market_strategy/pro3_strategy.py
  strategies/pro3_dualtimeframe.py                → crypto/market_strategy/pro3_dualtimeframe.py
  strategies/pro3_singletimeframe.py              → crypto/market_strategy/pro3_singletimeframe.py
  strategies/futures_boll_limit_dualtimeframe.py  → crypto/market_strategy/futures_boll_limit_dualtimeframe.py
  strategies/version_close_open.py                → crypto/market_strategy/version_close_open.py
  strategies/futuresStrategy/*                    → crypto/market_strategy/futures_strategy/*
  futures/akshare_api.py                          → crypto/futures/akshare_api.py（数据层提前）

明确不迁：strategies/cryptoStrategy/*（引用本仓库不存在的 api_config、
trend_strategy_boll_limit_scheduler_like）、futures/pro3_strategy.py（兼容 shim）。

改写规则全部为机械替换，逐文件打印命中次数，便于人工核对。
用法：python -X utf8 migrate_01_strategy_layer.py
"""
import os
import re
import sys

SRC = r"D:\python\futureStockTrade"
DST_ROOT = r"D:\python\cryptoTrade"

# (源相对路径, 目标相对路径)
FILE_MAP = [
    ("strategies/__init__.py", "crypto/market_strategy/__init__.py"),
    ("strategies/indicators.py", "crypto/market_strategy/indicators.py"),
    ("strategies/pro3_strategy.py", "crypto/market_strategy/pro3_strategy.py"),
    ("strategies/pro3_dualtimeframe.py", "crypto/market_strategy/pro3_dualtimeframe.py"),
    ("strategies/pro3_singletimeframe.py", "crypto/market_strategy/pro3_singletimeframe.py"),
    ("strategies/futures_boll_limit_dualtimeframe.py",
     "crypto/market_strategy/futures_boll_limit_dualtimeframe.py"),
    ("strategies/version_close_open.py", "crypto/market_strategy/version_close_open.py"),
    ("strategies/futuresStrategy/__init__.py",
     "crypto/market_strategy/futures_strategy/__init__.py"),
    ("strategies/futuresStrategy/pro3_dualtimeframe.py",
     "crypto/market_strategy/futures_strategy/pro3_dualtimeframe.py"),
    ("strategies/futuresStrategy/pro3_singletimeframe.py",
     "crypto/market_strategy/futures_strategy/pro3_singletimeframe.py"),
    ("futures/akshare_api.py", "crypto/futures/akshare_api.py"),
]

# ---------- 需要删除的 sys.path 兜底块 ----------
# 形如：
#   try:
#       from crypto.futures.akshare_api import ...
#   except ImportError:
#       # 注释
#       _project_root = os.path.dirname(...)
#       if _project_root not in sys.path:
#           sys.path.insert(0, _project_root)
#       from crypto.futures.akshare_api import ...
AKSHARE_TRY_FALLBACK = re.compile(
    r"try:\n"
    r"(?:[ \t]*from crypto\.futures\.akshare_api import [^\n]*\n)"
    r"[ \t]*except ImportError:\n"
    r"(?:[ \t]*#[^\n]*\n)?"
    r"[ \t]*_project_root = [^\n]*\n"
    r"[ \t]*if _project_root not in sys\.path:\n"
    r"[ \t]*sys\.path\.insert\(0, _project_root\)\n"
    r"[ \t]*from crypto\.futures\.akshare_api import [^\n]*\n",
    re.M,
)
REPL_AKSHARE = (
    "# 数据接口：统一走显式包路径（迁移前靠 sys.path 兜底，已移除）\n"
    "from crypto.futures.akshare_api import AkshareFuturesAPI, AkshareAPIError\n"
)

# 形如：
#   # 确保 xxx 目录在 sys.path 中
#   _STRATEGY_DIR = os.path.dirname(os.path.abspath(__file__))
#   if _STRATEGY_DIR not in sys.path:
#       sys.path.insert(0, _STRATEGY_DIR)
BARE_SYS_PATH_BLOCK = re.compile(
    r"(?:[ \t]*#[^\n]*sys\.path[^\n]*\n)?"
    r"[ \t]*_(?:STRATEGY|PROJECT)_(?:DIR|ROOT) = [^\n]*\n"
    r"[ \t]*if _(?:STRATEGY|PROJECT)_(?:DIR|ROOT) not in sys\.path:\n"
    r"[ \t]*sys\.path\.insert\(0, _(?:STRATEGY|PROJECT)_(?:DIR|ROOT)\)\n",
    re.M,
)

PROVENANCE = (
    "# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。\n"
    "# 原名: {src}  →  现位置: {dst}\n"
    "# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。\n"
)


def transform(text: str):
    """返回 (新文本, 命中统计列表)。"""
    hits = []
    # 逐条应用导入改写
    subs = [
        (r"(?<!market_)from strategies\.", "from crypto.market_strategy.", 0),
        (r"(?<!market_)import strategies\.", "import crypto.market_strategy.", 0),
        (r"\bfrom strategies import\b", "from crypto.market_strategy import", 0),
        (r"(?<!crypto\.)futures\.akshare_api", "crypto.futures.akshare_api", 0),
        (r"\bfuturesStrategy\b", "futures_strategy", 0),
        (r"^(\s*)from pro3_singletimeframe import\b",
         r"\1from crypto.market_strategy.futures_strategy.pro3_singletimeframe import", re.M),
        (r"^(\s*)import pro3_singletimeframe as (\w+)",
         r"\1from crypto.market_strategy.futures_strategy import pro3_singletimeframe as \2", re.M),
    ]
    for pat, rep, flags in subs:
        text, n = re.subn(pat, rep, text, flags=flags)
        if n:
            hits.append(f"{pat[:44]:<44} x{n}")

    text, n = AKSHARE_TRY_FALLBACK.subn(REPL_AKSHARE, text)
    if n:
        hits.append(f"{'akshare try/except sys.path 兜底块移除':<44} x{n}")

    text, n = BARE_SYS_PATH_BLOCK.subn("", text)
    if n:
        hits.append(f"{'裸 sys.path.insert 块移除':<44} x{n}")

    return text, hits


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    total_hits = 0
    for rel_src, rel_dst in FILE_MAP:
        src = os.path.join(SRC, rel_src.replace("/", os.sep))
        dst = os.path.join(DST_ROOT, rel_dst.replace("/", os.sep))
        if not os.path.exists(src):
            print(f"[缺失] {rel_src} 不存在，跳过")
            continue
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
        new_text, hits = transform(text)

        # 插入来源说明（紧跟 shebang 与 PEP 263 编码声明之后，编码声明只在前两行才生效）
        header = PROVENANCE.format(src=rel_src, dst=rel_dst)
        lines = new_text.splitlines(keepends=True)
        insert_at = 1 if lines and lines[0].strip().startswith("#!") else 0
        for i in range(insert_at, min(insert_at + 3, len(lines))):
            if re.match(r"^#\s*.*\bcoding[:=]", lines[i].strip()):
                insert_at = i + 1
                break
        new_text = "".join(lines[:insert_at] + [header] + lines[insert_at:])

        # 残留检查：不应再有裸导入 / sys.path 拼接
        residue = []
        for i, ln in enumerate(new_text.splitlines(), 1):
            s = ln.strip()
            if s.startswith("#"):
                continue
            if re.search(r"^\s*(from|import)\s+(strategies|futuresStrategy|pro3_\w+)\b", s) or \
               re.search(r"^\s*from (futures|stocks|kline|db|config|registry|mailer)\b", s) or \
               "sys.path.insert" in s:
                residue.append(f"    L{i}: {s[:90]}")

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_text)

        mark = f"  ⚠ 残留 {len(residue)} 处" if residue else ""
        print(f"[OK] {rel_src}  →  {rel_dst}   (改写 {len(hits)} 类{mark})")
        for h in hits:
            print(f"     {h}")
            total_hits += 1
        for r in residue:
            print(r)

    # crypto/futures/__init__.py：新建包声明
    init_dst = os.path.join(DST_ROOT, "crypto", "futures", "__init__.py")
    if not os.path.exists(init_dst):
        with open(init_dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                '# -*- coding: utf-8 -*-\n'
                '"""\n'
                '期货模块包（futureStockTrade 合并迁移 Phase 1 起用）\n'
                '=====================================================\n'
                'Phase 1 仅迁入数据层 akshare_api；Phase 2 迁入适配器与蓝图。\n'
                '与宿主 crypto/strategy（加密货币策略）职责隔离，互不引用。\n'
                '"""\n'
            )
        print(f"[OK] 新建包声明 crypto/futures/__init__.py")

    print("=" * 72)
    print(f"完成。共 {len(FILE_MAP)} 个文件搬运，改写规则命中 {total_hits} 次。")
    print("下一步：python -X utf8 -c \"import crypto.market_strategy\" 验证")


if __name__ == "__main__":
    main()
