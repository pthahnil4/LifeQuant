"""
Phase 2 · 期货模块迁移脚本（一次性执行，可重复运行=覆盖同步）
============================================================
把 futureStockTrade/futures 迁入 crypto/futures/，与 Phase 1 已就位的
数据层 akshare_api、策略层 crypto/market_strategy 接上显式包路径。

映射：
  futures/futures_blueprint.py      → crypto/futures/routes.py（蓝图，路由与静态目录自带）
  futures/futures_adapter.py        → crypto/futures/futures_adapter.py
  futures/star_market.py            → crypto/futures/star_market.py
  futures/batch_trend_analysis.py   → crypto/futures/batch_trend_analysis.py
  futures/__init__.py               → crypto/futures/__init__.py
  futures/templates/*.html          → crypto/futures/templates/
  futures/static/*                  → crypto/futures/static/
  future.csv / futures_config.json / star品种行情.csv / trend_atr_all_result.csv → crypto/futures/

明确不迁（理由）：
  - pro3_strategy.py / futures_pro3_dualtimeframe.py / futures_pro3_singletimeframe.py：
    纯兼容 shim，仓库内无任何引用；
  - diagnose_trend.py / filter_trend_atr.py / test_weekly_data.py /
    run_batch_backtest.py / run_full_backtest.py：一次性调试与回测脚本，无人 import；
  - cme_api.py：模块级依赖 stocks.jumei_api，改到 Phase 3 与股票模块一起迁，避免留下坏 import；
  - results/、futures模块代码汇总.md：产物与文档（文档 Phase 7 归档）。

模板侧统一（不做样式复刻）：
  模板中 url_for('static', ...) 与 {% include 'nav.html' %} 迁移后会解析到宿主的
  crypto/static 与 crypto/templates/nav.html —— 宿主样式是原样式的超集、导航为数据驱动，
  因此沿用宿主设计语言；/futures/api/* 为绝对路径，URL 前缀不变故无需改写。
用法：python -X utf8 migrate_02_futures.py
"""
import os
import re
import sys
import shutil

SRC = r"D:\python\futureStockTrade"
DST_ROOT = r"D:\python\cryptoTrade"

PY_MAP = [
    ("futures/__init__.py", "crypto/futures/__init__.py"),
    ("futures/futures_blueprint.py", "crypto/futures/routes.py"),
    ("futures/futures_adapter.py", "crypto/futures/futures_adapter.py"),
    ("futures/star_market.py", "crypto/futures/star_market.py"),
    ("futures/batch_trend_analysis.py", "crypto/futures/batch_trend_analysis.py"),
]

DATA_FILES = [
    "futures/future.csv",
    "futures/futures_config.json",
    "futures/star品种行情.csv",
    "futures/trend_atr_all_result.csv",
]

# 顺序敏感：更长的专项规则放前面
SUBS = [
    (r"strategies\.futuresStrategy", "crypto.market_strategy.futures_strategy", 0),
    (r"(?<!market_)from strategies\.", "from crypto.market_strategy.", 0),
    (r"(?<!market_)import strategies\.", "import crypto.market_strategy.", 0),
    (r"(?<!crypto\.)(?<!futures\.)from akshare_api import", "from crypto.futures.akshare_api import", 0),
    (r"(?<!crypto\.)from futures\.akshare_api", "from crypto.futures.akshare_api", 0),
    (r"from futures_adapter import", "from crypto.futures.futures_adapter import", 0),
    (r"import futures_adapter\b", "from crypto.futures import futures_adapter", 0),
    (r"from batch_trend_analysis import", "from crypto.futures.batch_trend_analysis import", 0),
    (r"from star_market import", "from crypto.futures.star_market import", 0),
]

# BASE_DIR 式 sys.path 兜底：保留 BASE_DIR（CSV 定位仍需要），删掉拼接两行
SYS_PATH_BY_BASEDIR = re.compile(
    r"[ \t]*if BASE_DIR not in sys\.path:\n[ \t]*sys\.path\.insert\(0, BASE_DIR\)\n", re.M)
# 相应注释文案同步更正，避免留下与代码不符的说明
BASEDIR_COMMENT = re.compile(
    r"[ \t]*# 确保可从? futures 目录导入本地模块[^\n]*\n")

PROVENANCE = (
    "# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 2 自动搬运（2026-10-08）。\n"
    "# 原名: {src}  →  现位置: {dst}\n"
    "# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底已移除。\n"
)

# 编码声明行（PEP 263 要求落在前两行，迁移说明必须插在它之后）
CODING_RE = re.compile(r"^#\s*.*\bcoding[:=]")


def header_insert_pos(lines):
    """返回迁移说明的插入下标：跳过首行 shebang，并跳过紧随其后的编码声明行。"""
    at = 1 if lines and lines[0].strip().startswith("#!") else 0
    for i in range(at, min(at + 3, len(lines))):
        if CODING_RE.match(lines[i].strip()):
            at = i + 1
            break
    return at


def transform(text: str):
    hits = []
    for pat, rep, flags in SUBS:
        text, n = re.subn(pat, rep, text, flags=flags)
        if n:
            hits.append(f"{pat[:46]:<46} x{n}")
    text, n = SYS_PATH_BY_BASEDIR.subn("", text)
    if n:
        hits.append(f"{'BASE_DIR sys.path 拼接移除':<46} x{n}")
    text, n = BASEDIR_COMMENT.subn(
        "# 数据文件定位基准目录（原用于 sys.path 兜底，现仅用于定位同目录 CSV/JSON）\n", text)
    if n:
        hits.append(f"{'兜底注释文案更正':<46} x{n}")
    return text, hits


def copy_py():
    print("=" * 72)
    print("Step 1 · Python 文件搬运与导入改写")
    for rel_src, rel_dst in PY_MAP:
        src = os.path.join(SRC, rel_src.replace("/", os.sep))
        dst = os.path.join(DST_ROOT, rel_dst.replace("/", os.sep))
        if not os.path.exists(src):
            print(f"  [缺失] {rel_src}")
            continue
        text = open(src, encoding="utf-8").read()
        new, hits = transform(text)

        lines = new.splitlines(keepends=True)
        at = header_insert_pos(lines)
        new = "".join(lines[:at] + [PROVENANCE.format(src=rel_src, dst=rel_dst)] + lines[at:])

        # 残留体检：不允许再出现裸模块名导入或 sys.path 拼接
        residue = []
        for i, ln in enumerate(new.splitlines(), 1):
            s = ln.strip()
            if s.startswith("#"):
                continue
            if re.match(r"^(from|import)\s+(futures_adapter|akshare_api|star_market|"
                        r"batch_trend_analysis|strategies|pro3_\w+)\b", s) or "sys.path.insert" in s:
                residue.append(f"      L{i}: {s[:92]}")

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        tag = "  ⚠残留" if residue else ""
        print(f"  [OK] {rel_src:<34} → {rel_dst:<40} 改写{len(hits)}类{tag}")
        for h in hits:
            print(f"       {h}")
        for r in residue:
            print(r)


def copy_assets():
    print("=" * 72)
    print("Step 2 · 模板 / 静态资源 / 数据文件")
    for sub in ("templates", "static"):
        src_dir = os.path.join(SRC, "futures", sub)
        dst_dir = os.path.join(DST_ROOT, "crypto", "futures", sub)
        if not os.path.isdir(src_dir):
            print(f"  [缺失] futures/{sub}")
            continue
        os.makedirs(dst_dir, exist_ok=True)
        cnt = 0
        for name in sorted(os.listdir(src_dir)):
            s = os.path.join(src_dir, name)
            if not os.path.isfile(s):
                continue
            shutil.copyfile(s, os.path.join(dst_dir, name))
            cnt += 1
        print(f"  [OK] futures/{sub:<9} → crypto/futures/{sub:<9} {cnt} 个文件")

    for rel in DATA_FILES:
        src = os.path.join(SRC, rel.replace("/", os.sep))
        dst = os.path.join(DST_ROOT, "crypto", "futures", os.path.basename(rel))
        if not os.path.exists(src):
            print(f"  [缺失] {rel}")
            continue
        shutil.copyfile(src, dst)
        print(f"  [OK] {rel:<40} → crypto/futures/{os.path.basename(rel):<28} "
              f"{os.path.getsize(dst):,} 字节")


def patch_routes_template_ref():
    """确认 routes.py 的蓝图模板/静态目录仍为相对自身目录（迁移后自动指向 crypto/futures/*）"""
    p = os.path.join(DST_ROOT, "crypto", "futures", "routes.py")
    txt = open(p, encoding="utf-8").read()
    has_tf = "template_folder='templates'" in txt
    has_sf = "static_folder='static'" in txt
    print("=" * 72)
    print("Step 3 · 蓝图目录声明体检")
    print(f"  template_folder 相对声明: {has_tf} / static_folder 相对声明: {has_sf}")
    print("  （Flask 以蓝图所在包目录为根解析 → crypto/futures/templates 与 static）")
    return has_tf and has_sf


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    copy_py()
    copy_assets()
    patch_routes_template_ref()
    print("=" * 72)
    print("完成。下一步：在 crypto/app.py 注册蓝图（闸门之前）并加导航项，再跑 verify_02。")
