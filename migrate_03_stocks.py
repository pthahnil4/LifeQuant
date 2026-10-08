# -*- coding: utf-8 -*-
"""
Phase 3 · 股票模块迁移脚本（一次性执行，可重复运行=覆盖同步）
==========================================================
把 futureStockTrade/stocks 迁入 crypto/stocks/，并把 Phase 2 推迟的
CME 期货行情客户端（futures/cme_api.py）一起收口到显式包路径。

映射：
  stocks/__init__.py            → crypto/stocks/__init__.py
  stocks/stocks_blueprint.py    → crypto/stocks/routes.py（蓝图，模板/静态目录自带）
  stocks/stock_adapter.py       → crypto/stocks/stock_adapter.py
  stocks/stock_akshare_api.py   → crypto/stocks/stock_akshare_api.py
  stocks/jumei_api.py           → crypto/stocks/jumei_api.py（A股行情网关，未接线）
  futures/cme_api.py            → crypto/futures/cme_api.py（CME 期货网关，未接线）
  stocks/templates/*.html       → crypto/stocks/templates/
  stocks/static/*               → crypto/stocks/static/
  stocks/stock_config.json      → crypto/stocks/stock_config.json

新建（非搬运）：
  crypto/stocks/gateway_config.py —— 只暴露 6 个 JUMEI_* 名字的凭据解析模块，
  用来替代原项目根级 config.py。原 config.py 里的 SECRET_KEY 默认值、SMTP 授权码
  明文、TDengine 口令、MySQL 连接串等敏感项**一律不进** cryptoTrade。
  jumei_api / cme_api 的 `import config` 改写为
  `from crypto.stocks import gateway_config as config`，属纯导入改写，逻辑不变。

明确不迁（理由）：
  - stocks/stock_pro3_strategy.py：纯兼容 shim（重导出 strategies.pro3_strategy），
    仓库内除自身文档外无任何引用，合并后统一由 crypto.market_strategy 提供；
  - stocks/demo_kline.py：K 线获取示例脚本，无任何模块 import；
  - strategies/cryptoStrategy/、calc_today.py、verify_indicators.py、
    run_strategy_comparison.py：一次性脚本与跨项目残留（见方案文档第六节）。

模板侧统一（与 Phase 2 同一口径，不做样式复刻）：
  模板中的 url_for('static', ...) 与 {% include 'nav.html' %} 迁移后解析到宿主的
  crypto/static 与 crypto/templates/nav.html；/stocks/api/* 为绝对路径，URL 前缀不变
  故无需改写。

用法：python -X utf8 migrate_03_stocks.py
"""
import os
import re
import sys
import shutil

SRC = r"D:\python\futureStockTrade"
DST_ROOT = r"D:\python\cryptoTrade"

PY_MAP = [
    ("stocks/__init__.py", "crypto/stocks/__init__.py"),
    ("stocks/stocks_blueprint.py", "crypto/stocks/routes.py"),
    ("stocks/stock_adapter.py", "crypto/stocks/stock_adapter.py"),
    ("stocks/stock_akshare_api.py", "crypto/stocks/stock_akshare_api.py"),
    ("stocks/jumei_api.py", "crypto/stocks/jumei_api.py"),
    ("futures/cme_api.py", "crypto/futures/cme_api.py"),
]

DATA_FILES = [
    "stocks/stock_config.json",
]

# 顺序敏感：更长的专项规则放前面
SUBS = [
    (r"(?<!market_)from strategies\.", "from crypto.market_strategy.", 0),
    (r"(?<!market_)import strategies\.", "import crypto.market_strategy.", 0),
    (r"from stocks\.jumei_api import", "from crypto.stocks.jumei_api import", 0),
    (r"from \.stock_akshare_api import", "from crypto.stocks.stock_akshare_api import", 0),
    (r"from \.stock_adapter import", "from crypto.stocks.stock_adapter import", 0),
    (r"(?<!crypto\.)(?<!stocks\.)from stock_akshare_api import",
     "from crypto.stocks.stock_akshare_api import", 0),
    (r"(?<!crypto\.)(?<!stocks\.)from stock_adapter import",
     "from crypto.stocks.stock_adapter import", 0),
    (r"(?<!crypto\.)(?<!stocks\.)import stock_adapter\b",
     "from crypto.stocks import stock_adapter", 0),
    (r"(?<!crypto\.)(?<!stocks\.)from jumei_api import",
     "from crypto.stocks.jumei_api import", 0),
    (r"(?<!crypto\.)(?<!futures\.)from futures\.akshare_api",
     "from crypto.futures.akshare_api", 0),
    # 根级 config → 本模块专用凭据配置（缩进保持，属导入改写）
    (r"(?m)^([ \t]*)import config[ \t]*$",
     r"\1from crypto.stocks import gateway_config as config", 0),
    # 文档字符串里的旧路径字样同步更正，避免留下与仓库结构不符的说明
    (r"strategies/pro3_strategy\.py", "crypto/market_strategy/pro3_strategy.py", 0),
    (r"strategies/indicators\.py", "crypto/market_strategy/indicators.py", 0),
    (r"[ \t]*- demo_kline\.py: K线数据获取示例脚本\n", "", 0),
]

# 「允许脚本方式直接运行时导入根级包」的 sys.path 兜底整段移除
ROOT_SYS_PATH_BLOCK = re.compile(
    r"[ \t]*# 允许脚本方式直接运行时导入根级[^\n]*\n"
    r"[ \t]*_ROOT = os\.path\.dirname\(os\.path\.dirname\(os\.path\.abspath\(__file__\)\)\)\n"
    r"[ \t]*if _ROOT not in sys\.path:\n"
    r"[ \t]*sys\.path\.insert\(0, _ROOT\)\n")

PROVENANCE = (
    "# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 3 自动搬运（2026-10-08）。\n"
    "# 原名: {src}  →  现位置: {dst}\n"
    "# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底与根级 config 依赖已移除。\n"
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
            hits.append(f"{pat[:48]:<48} x{n}")
    text, n = ROOT_SYS_PATH_BLOCK.subn("", text)
    if n:
        hits.append(f"{'根级 sys.path 兜底整段移除':<48} x{n}")
    return text, hits


def copy_py():
    print("=" * 74)
    print("Step 1 · Python 文件搬运与导入改写")
    bad = 0
    for rel_src, rel_dst in PY_MAP:
        src = os.path.join(SRC, rel_src.replace("/", os.sep))
        dst = os.path.join(DST_ROOT, rel_dst.replace("/", os.sep))
        if not os.path.exists(src):
            print(f"  [缺失] {rel_src}")
            bad += 1
            continue
        text = open(src, encoding="utf-8").read()
        new, hits = transform(text)

        lines = new.splitlines(keepends=True)
        at = header_insert_pos(lines)
        new = "".join(lines[:at] + [PROVENANCE.format(src=rel_src, dst=rel_dst)] + lines[at:])

        # 残留体检：不允许再出现裸模块名导入 / 根级 config / sys.path 拼接
        residue = []
        for i, ln in enumerate(new.splitlines(), 1):
            s = ln.strip()
            if s.startswith("#"):
                continue
            if (re.match(r"^(from|import)\s+(stock_adapter|stock_akshare_api|jumei_api|"
                         r"cme_api|strategies|config|pro3_\w+)\b", s)
                    or re.match(r"^import config$", s)
                    or "sys.path.insert" in s):
                residue.append(f"      L{i}: {s[:92]}")

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        tag = "  ⚠残留" if residue else ""
        print(f"  [OK] {rel_src:<30} → {rel_dst:<38} 改写{len(hits)}类{tag}")
        for h in hits:
            print(f"       {h}")
        for r in residue:
            print(r)
        bad += len(residue)
    return bad


def copy_assets():
    print("=" * 74)
    print("Step 2 · 模板 / 静态资源 / 数据文件")
    for sub in ("templates", "static"):
        src_dir = os.path.join(SRC, "stocks", sub)
        dst_dir = os.path.join(DST_ROOT, "crypto", "stocks", sub)
        if not os.path.isdir(src_dir):
            print(f"  [缺失] stocks/{sub}")
            continue
        os.makedirs(dst_dir, exist_ok=True)
        cnt = 0
        for name in sorted(os.listdir(src_dir)):
            s = os.path.join(src_dir, name)
            if not os.path.isfile(s):
                continue
            shutil.copyfile(s, os.path.join(dst_dir, name))
            cnt += 1
        print(f"  [OK] stocks/{sub:<9} → crypto/stocks/{sub:<9} {cnt} 个文件")

    for rel in DATA_FILES:
        src = os.path.join(SRC, rel.replace("/", os.sep))
        dst = os.path.join(DST_ROOT, "crypto", "stocks", os.path.basename(rel))
        if not os.path.exists(src):
            print(f"  [缺失] {rel}")
            continue
        shutil.copyfile(src, dst)
        print(f"  [OK] {rel:<34} → crypto/stocks/{os.path.basename(rel):<24} "
              f"{os.path.getsize(dst):,} 字节")


def scan_secret_leak():
    """凭据体检：迁入文件里不得出现明文 AppCode / SMTP 授权码 / 密钥默认值。"""
    print("=" * 74)
    print("Step 3 · 凭据泄漏体检（只报文件名与行号，不打印内容）")
    patterns = [
        ("明文 AppCode 赋值", re.compile(r"APPCODE[ \t]*=[ \t]*[\"'][A-Za-z0-9]{16,}[\"']")),
        ("SMTP 授权码明文", re.compile(r"['\"]password['\"]\s*:\s*['\"][A-Za-z0-9]{12,}['\"]")),
        ("SECRET_KEY 默认值", re.compile(r"SECRET_KEY[^\n]*or[ \t]*['\"][^'\"]+['\"]")),
        ("MySQL/TDengine 口令明文", re.compile(r"(DB_PASSWORD|TDENGINE_PASSWORD)[^\n]*or[ \t]*['\"][^'\"]{4,}['\"]")),
    ]
    hits = 0
    for rel_dst in [d for _, d in PY_MAP] + ["crypto/stocks/gateway_config.py"]:
        p = os.path.join(DST_ROOT, rel_dst.replace("/", os.sep))
        if not os.path.isfile(p):
            continue
        for i, ln in enumerate(open(p, encoding="utf-8").read().splitlines(), 1):
            for name, pat in patterns:
                if pat.search(ln):
                    print(f"  ⚠ {rel_dst}:L{i}  {name}")
                    hits += 1
    print(f"  结论：命中 {hits} 处" + ("（需处理后再提交）" if hits else "（干净）"))
    return hits


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    residue = copy_py()
    copy_assets()
    leaks = scan_secret_leak()
    print("=" * 74)
    print(f"完成。残留 {residue} 项 / 凭据命中 {leaks} 项。"
          "下一步：在 crypto/app.py 注册股票蓝图（闸门之前）并加导航项，再跑 verify_03。")
