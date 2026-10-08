# -*- coding: utf-8 -*-
"""
Phase 4 · K 线训练模块迁移脚本（一次性执行，可重复运行=覆盖同步）
==============================================================
把 futureStockTrade/kline 迁入 crypto/kline_training/，这是整个合并迁移里
最大的一块（1649 行蓝图 / 53 条路由 + 5482 行全套）。

映射
----
  kline/kline_blueprint.py    → crypto/kline_training/routes.py（蓝图名仍叫 'kline'，
                                模板里的 url_for('kline.static', ...) 才能原样解析）
  kline/db.py                 → crypto/kline_training/store.py（避开宿主 database.py）
  kline/auth_service.py       → crypto/kline_training/auth_service.py
  kline/mailer.py             → crypto/kline_training/mailer.py
  kline/registry.py           → crypto/kline_training/registry.py
  kline/training_service.py   → crypto/kline_training/training_service.py
  kline/chart_serialize.py    → crypto/kline_training/chart_serialize.py
  kline/kline_adapter.py      → crypto/kline_training/kline_adapter.py
  kline/providers/*.py        → crypto/kline_training/providers/*.py
  kline/database/*.py         → crypto/kline_training/td_cache/*.py（闲置能力，默认关闭）
  kline/templates/*.html      → crypto/kline_training/templates/
  kline/static/*              → crypto/kline_training/static/
  data/training.db            → data/kline_training.db（sqlite 备份 API 整库复制）

新建（非搬运）
--------------
  crypto/kline_training/__init__.py  包说明
  crypto/kline_training/settings.py  K 线训练专用配置：替代原根级 config.py 的那一半
  crypto/kline_training/templates/kline_account_bar.html  登录态工具条（原项目在
                                nav.html 里的用户区，宿主 nav.html 是数据驱动的，
                                不改动它，改由 K 线模板自带一条工具条）

导入改写（纯 mechanical，业务逻辑零改动）
----------------------------------------
  import db                      → from crypto.kline_training import store as db
  import config                  → from crypto.kline_training import settings as config
  import mailer/registry/...     → from crypto.kline_training import ...
  from providers[.base] import   → from crypto.kline_training.providers[.base] import
  from chart_serialize import    → from crypto.kline_training.chart_serialize import
  from kline_adapter import      → from crypto.kline_training.kline_adapter import
  from database import           → from crypto.kline_training.td_cache import
  import futures_adapter as fa   → from crypto.futures import futures_adapter as fa
  import stock_adapter as sa     → from crypto.stocks import stock_adapter as sa
  from strategies.indicators     → from crypto.market_strategy.indicators
  各文件顶部的 sys.path 兜底整段移除（宿主以 crypto.* 包路径导入，不再拼路径）

会话键（用户已拍板的 D2 决策）
------------------------------
  session['uid'] / session.get('uid') / session.pop('uid') → kline_uid
  原项目独立进程时 'uid' 不会撞名，合并进宿主后 'uid' 是个太通用的键名，
  统一改成带模块前缀的 kline_uid，登录态与宿主 web_auth 闸门互不干扰。

明确不迁（理由）
----------------
  kline/kline_trainer/       早期 CLI 训练器，Web 链路 0 引用（历史遗留）
  kline/db_probe_login.py    一次性登录探针脚本
  kline/verify_auth_login.py 已被仓库根 verify_04 取代的旧验收脚本
  kline/test_fetch.py        手工抓数小样，硬编码绝对路径

用法：python -X utf8 migrate_04_kline_training.py
"""
import os
import re
import sys
import glob
import shutil
import sqlite3

SRC = r"D:\python\futureStockTrade"
DST_ROOT = r"D:\python\cryptoTrade"
PKG = "crypto/kline_training"

PY_MAP = [
    ("kline/kline_blueprint.py", f"{PKG}/routes.py"),
    ("kline/db.py", f"{PKG}/store.py"),
    ("kline/auth_service.py", f"{PKG}/auth_service.py"),
    ("kline/mailer.py", f"{PKG}/mailer.py"),
    ("kline/registry.py", f"{PKG}/registry.py"),
    ("kline/training_service.py", f"{PKG}/training_service.py"),
    ("kline/chart_serialize.py", f"{PKG}/chart_serialize.py"),
    ("kline/kline_adapter.py", f"{PKG}/kline_adapter.py"),
    ("kline/providers/__init__.py", f"{PKG}/providers/__init__.py"),
    ("kline/providers/base.py", f"{PKG}/providers/base.py"),
    ("kline/providers/crypto_provider.py", f"{PKG}/providers/crypto_provider.py"),
    ("kline/providers/futures_provider.py", f"{PKG}/providers/futures_provider.py"),
    ("kline/providers/stock_provider.py", f"{PKG}/providers/stock_provider.py"),
    ("kline/database/__init__.py", f"{PKG}/td_cache/__init__.py"),
    ("kline/database/import_csv.py", f"{PKG}/td_cache/import_csv.py"),
    ("kline/database/test_connection.py", f"{PKG}/td_cache/test_connection.py"),
    ("kline/database/test_crud.py", f"{PKG}/td_cache/test_crud.py"),
]

# 顺序敏感：更长的专项规则放前面
SUBS = [
    # —— 策略层（Phase 1 已迁入 crypto/market_strategy）——
    (r"(?<!market_)from strategies\.", "from crypto.market_strategy.", 0),
    (r"(?<!market_)import strategies\.", "import crypto.market_strategy.", 0),
    # —— K 线训练包内互引（缩进保持：蓝图里大量「函数内延迟导入」）——
    (r"(?m)^([ \t]*)import db[ \t]*(#.*)?$",
     r"\1from crypto.kline_training import store as db", 0),
    (r"(?m)^([ \t]*)import config[ \t]*(#.*)?$",
     r"\1from crypto.kline_training import settings as config", 0),
    (r"(?m)^([ \t]*)import mailer[ \t]*(#.*)?$",
     r"\1from crypto.kline_training import mailer", 0),
    (r"(?m)^([ \t]*)import auth_service as au[ \t]*(#.*)?$",
     r"\1from crypto.kline_training import auth_service as au", 0),
    (r"(?m)^([ \t]*)import registry[ \t]*(#.*)?$",
     r"\1from crypto.kline_training import registry", 0),
    (r"(?m)^([ \t]*)import training_service as ts[ \t]*(#.*)?$",
     r"\1from crypto.kline_training import training_service as ts", 0),
    (r"(?m)^([ \t]*)from providers\.base import",
     r"\1from crypto.kline_training.providers.base import", 0),
    (r"(?m)^([ \t]*)from providers import",
     r"\1from crypto.kline_training.providers import", 0),
    (r"(?m)^([ \t]*)from chart_serialize import",
     r"\1from crypto.kline_training.chart_serialize import", 0),
    (r"(?m)^([ \t]*)from kline_adapter import",
     r"\1from crypto.kline_training.kline_adapter import", 0),
    (r"(?m)^([ \t]*)import kline_adapter[ \t]*(#.*)?$",
     r"\1from crypto.kline_training import kline_adapter", 0),
    # —— 跨市场 provider 复用的适配器（Phase 1/3 已迁入宿主包）——
    (r"(?m)^([ \t]*)import futures_adapter as fa",
     r"\1from crypto.futures import futures_adapter as fa", 0),
    (r"(?m)^([ \t]*)import stock_adapter as sa",
     r"\1from crypto.stocks import stock_adapter as sa", 0),
    # —— TDengine 缓存工具包内互引 ——
    (r"(?m)^([ \t]*)from database import",
     r"\1from crypto.kline_training.td_cache import", 0),
    # —— 会话键（D2 决策）：'uid' → 'kline_uid' ——
    (r"session\[\s*'uid'\s*\]", "session['kline_uid']", 0),
    (r"session\.get\(\s*'uid'\s*\)", "session.get('kline_uid')", 0),
    (r"session\.pop\(\s*'uid'\s*,", "session.pop('kline_uid',", 0),
    # —— 文档字符串里的旧路径字样同步更正 ——
    (r"strategies/indicators\.py", "crypto/market_strategy/indicators.py", 0),
    (r"strategies/ 目录", "crypto/market_strategy/ 包", 0),
    (r"from kline\.kline_blueprint import", "from crypto.kline_training.routes import", 0),
]

# 残留体检：迁移后不允许再出现的裸导入 / sys.path 拼接 / 旧会话键
BARE_MODULES = ("db", "config", "mailer", "registry", "auth_service", "training_service",
                "providers", "chart_serialize", "kline_adapter", "futures_adapter",
                "stock_adapter", "strategies", "database")
RESIDUE_IMPORT = re.compile(r"^(from|import)\s+(%s)\b" % "|".join(BARE_MODULES))

# ---------------------------------------------------------------------
#  sys.path 兜底：逐行分类 + 安全校验后整段删除
# ---------------------------------------------------------------------
BOOTSTRAP_NAMES = {"_ROOT", "_BASE", "_THIS_DIR", "_PROJECT_ROOT", "_KLINE_DIR",
                   "_FUTURES_DIR", "_STOCKS_DIR", "_p"}
LINE_DIR_ASSIGN = re.compile(
    r"^[ \t]*(" + "|".join(BOOTSTRAP_NAMES) + r")[ \t]*=[ \t]*"
    r"(os\.path\.(dirname|join|abspath)|Path\()[^\n]*$")
LINE_SYS_PATH_IF = re.compile(r"^[ \t]*if[^\n]*sys\.path[^\n]*:[ \t]*$")
LINE_SYS_PATH_CALL = re.compile(r"^[ \t]*sys\.path\.(insert|append)\([^\n]*$")
LINE_SYS_PATH_FOR = re.compile(r"^[ \t]*for[ \t]+_p[ \t]+in[ \t]+\([^\n]*\):[ \t]*$")
LINE_BOOT_COMMENT = re.compile(r"^[ \t]*#[^\n]*(sys\.path|导入根级)[^\n]*$")


def drop_sys_path_bootstrap(text: str):
    """删除「算目录 + 塞 sys.path」这类兜底样板。

    安全阀：样板块里赋值的名字若在块外仍被业务逻辑使用，就保留该赋值行不动，
    并打印提醒（本仓库实测没有这种情况，全部只服务于 sys.path）。
    """
    lines = text.splitlines(keepends=True)
    marked = [False] * len(lines)
    for i, ln in enumerate(lines):
        if (LINE_DIR_ASSIGN.match(ln) or LINE_SYS_PATH_IF.match(ln)
                or LINE_SYS_PATH_CALL.match(ln) or LINE_SYS_PATH_FOR.match(ln)
                or LINE_BOOT_COMMENT.match(ln)):
            marked[i] = True

    # 块外引用检查： marked 行里赋值的名字，是否出现在未 marked 的行上
    assigned = set()
    for i, ln in enumerate(lines):
        if marked[i]:
            m = LINE_DIR_ASSIGN.match(ln)
            if m:
                assigned.add(m.group(1))
    kept = set()
    for i, ln in enumerate(lines):
        if marked[i]:
            continue
        for name in assigned:
            if re.search(r"\b%s\b" % re.escape(name), ln):
                kept.add(name)
    if kept:
        for i, ln in enumerate(lines):
            m = LINE_DIR_ASSIGN.match(ln)
            if marked[i] and m and m.group(1) in kept:
                marked[i] = False
        print(f"       [保留] 块外仍被使用的目录常量: {', '.join(sorted(kept))}")

    removed = sum(1 for m in marked if m)
    out = "".join(ln for i, ln in enumerate(lines) if not marked[i])
    return out, removed


# 编码声明行（PEP 263 要求落在前两行，迁移说明必须插在它之后）
CODING_RE = re.compile(r"^#\s*.*\bcoding[:=]")


def header_insert_pos(lines):
    """返回迁移说明的插入下标：跳过 shebang，再跳过紧随其后的编码声明行。"""
    at = 1 if lines and lines[0].strip().startswith("#!") else 0
    for i in range(at, min(at + 3, len(lines))):
        if CODING_RE.match(lines[i].strip()):
            at = i + 1
            break
    return at


PROVENANCE = (
    "# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（{date}）。\n"
    "# 原名: {{src}}  →  现位置: {{dst}}\n"
    "# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。\n"
)


def transform(text: str):
    hits = []
    for pat, rep, flags in SUBS:
        text, n = re.subn(pat, rep, text, flags=flags)
        if n:
            hits.append(f"{pat[:50]:<50} x{n}")
    text, removed = drop_sys_path_bootstrap(text)
    if removed:
        hits.append(f"{'sys.path 兜底样板删除':<50} x{removed} 行")
    return text, hits


def copy_py():
    print("=" * 78)
    print("Step 1 · Python 文件搬运与导入改写")
    problems = 0
    for rel_src, rel_dst in PY_MAP:
        src = os.path.join(SRC, rel_src.replace("/", os.sep))
        dst = os.path.join(DST_ROOT, rel_dst.replace("/", os.sep))
        if not os.path.exists(src):
            print(f"  [缺失] {rel_src}")
            problems += 1
            continue
        text = open(src, encoding="utf-8").read()
        new, hits = transform(text)

        lines = new.splitlines(keepends=True)
        at = header_insert_pos(lines)
        note = PROVENANCE.format(date="2026-10-08").format(src=rel_src, dst=rel_dst)
        new = "".join(lines[:at] + [note] + lines[at:])

        residue = []
        for i, ln in enumerate(new.splitlines(), 1):
            s = ln.strip()
            if s.startswith("#") or s.startswith('"""') or s.startswith("'''"):
                continue
            if RESIDUE_IMPORT.match(s) or "sys.path.insert" in s or "sys.path.append" in s:
                residue.append(f"      L{i}: {s[:96]}")
            if re.search(r"session\[\s*'uid'\s*\]|session\.(get|pop)\(\s*'uid'\s*[,)]", s):
                residue.append(f"      L{i}: 旧会话键 uid → {s[:80]}")

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        print(f"  [{'⚠' if residue else 'OK'}] {rel_src:<32} → {rel_dst:<44} "
              f"改写 {len(hits)} 类")
        for h in hits:
            print(f"       {h}")
        for r in residue:
            print(r)
        problems += len(residue)
    return problems


def copy_assets():
    print("=" * 78)
    print("Step 2 · 模板 / 静态资源（模板补挂 K 线登录工具条）")
    for sub in ("templates", "static"):
        src_dir = os.path.join(SRC, "kline", sub)
        dst_dir = os.path.join(DST_ROOT, "crypto", "kline_training", sub)
        if not os.path.isdir(src_dir):
            print(f"  [缺失] kline/{sub}")
            continue
        os.makedirs(dst_dir, exist_ok=True)
        cnt = 0
        for name in sorted(os.listdir(src_dir)):
            s = os.path.join(src_dir, name)
            if not os.path.isfile(s):
                continue
            shutil.copyfile(s, os.path.join(dst_dir, name))
            cnt += 1
        print(f"  [OK] kline/{sub:<9} → {PKG}/{sub:<9} {cnt} 个文件")

    # 宿主 nav.html 是数据驱动导航（无用户区），且正在被并行修改 —— 不去动它。
    # K 线页面原本靠 nav.html 的 current_user 区块提供「登录/注册/个人中心/登出」
    # 与在线心跳，这里给每个 K 线模板在 nav 之后补一条自带工具条。
    patched = 0
    for p in sorted(glob.glob(os.path.join(DST_ROOT, PKG.replace("/", os.sep),
                                           "templates", "*.html"))):
        text = open(p, encoding="utf-8").read()
        if "kline_account_bar.html" in text:
            continue
        new, n = re.subn(r"(\{%\s*include\s+'nav\.html'\s*%\})",
                         r"\1" + "\n" + "{% include 'kline_account_bar.html' %}", text)
        if n:
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
            patched += 1
        else:
            print(f"  [提醒] {os.path.basename(p)} 未找到 nav.html include，需人工补工具条")
    print(f"  [OK] 已为 {patched} 个 K 线模板补挂登录工具条")


def copy_database():
    """training.db 整库备份到宿主 data/kline_training.db（WAL 安全）。"""
    print("=" * 78)
    print("Step 3 · SQLite 数据搬运（备份 API，避免复制半截 WAL）")
    src_db = os.path.join(SRC, "data", "training.db")
    dst_dir = os.path.join(DST_ROOT, "data")
    dst_db = os.path.join(dst_dir, "kline_training.db")
    if not os.path.isfile(src_db):
        print(f"  [缺失] {src_db}")
        return 1
    os.makedirs(dst_dir, exist_ok=True)
    if os.path.isfile(dst_db):
        bak = dst_db + ".bak"
        shutil.copyfile(dst_db, bak)
        print(f"  [备份] 既有 {os.path.basename(dst_db)} → kline_training.db.bak")
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dst_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    verify = sqlite3.connect(dst_db)
    try:
        tables = [r[0] for r in verify.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        users = verify.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = verify.execute(
            "SELECT (SELECT COUNT(*) FROM sessions) +"
            " (SELECT COUNT(*) FROM results)").fetchone()[0]
    finally:
        verify.close()
    src_cnt = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        s_users = src_cnt.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        s_rows = src_cnt.execute(
            "SELECT (SELECT COUNT(*) FROM sessions) +"
            " (SELECT COUNT(*) FROM results)").fetchone()[0]
    finally:
        src_cnt.close()
    ok = (users == s_users and rows == s_rows)
    print(f"  [{'OK' if ok else '⚠'}] {os.path.basename(dst_db)} "
          f"{os.path.getsize(dst_db):,} 字节 / {len(tables)} 张表 / "
          f"用户 {users}(源 {s_users}) / 训练+成绩 {rows}(源 {s_rows})")
    return 0 if ok else 1


def scan_secret_leak():
    print("=" * 78)
    print("Step 4 · 凭据泄漏体检（只报文件与行号，不打印内容）")
    patterns = [
        ("SECRET_KEY 硬编码默认值", re.compile(r"SECRET_KEY[^\n]*or[ \t]*['\"][^'\"]+['\"]")),
        ("SMTP 授权码明文", re.compile(r"['\"]password['\"]\s*:\s*['\"][A-Za-z0-9]{12,}['\"]")),
        ("TDengine/MySQL 口令默认值", re.compile(r"(DB_PASSWORD|TDENGINE_PASSWORD)[^\n]*or[ \t]*['\"][^'\"]{4,}['\"]")),
        ("写死的公网数据库地址", re.compile(r"http://\d+\.\d+\.\d+\.\d+:16041")),
        ("旧项目绝对路径", re.compile(r"[A-Za-z]:[\\\\/](?:python[\\\\/])?futureStockTrade", re.I)),
        ("源码内个人邮箱", re.compile(r"2441940565@qq\.com")),
    ]
    files = [d for _, d in PY_MAP] + [f"{PKG}/settings.py", f"{PKG}/__init__.py"]
    hits = 0
    for rel in files:
        p = os.path.join(DST_ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            continue
        for i, ln in enumerate(open(p, encoding="utf-8").read().splitlines(), 1):
            for name, pat in patterns:
                if pat.search(ln):
                    print(f"  ⚠ {rel}:L{i}  {name}")
                    hits += 1
    print(f"  结论：命中 {hits} 处" + ("（需处理后再提交）" if hits else "（干净）"))
    return hits


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    problems = copy_py()
    copy_assets()
    db_bad = copy_database()
    leaks = scan_secret_leak()
    print("=" * 78)
    print(f"完成。导入残留 {problems} 项 / 数据校验问题 {db_bad} 项 / 凭据命中 {leaks} 项。")
    print("下一步：写 kline_account_bar.html、改 td_cache 凭据、在 crypto/app.py 注册"
          "K 线蓝图（闸门之前）并配好 app.secret_key，再跑 verify_04。")
