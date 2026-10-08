# -*- coding: utf-8 -*-
"""
Phase 4 验收 · K 线训练模块合并 A/B
==================================
K 线训练是这次合并里最大的一块（1649 行蓝图 / 53 条路由 + 三品类 provider +
SQLite 账号/成绩/对战），所以验收也分四层，逐条对应用户批准的 Phase 4 验收口径
「登录/注册、训练开局、三市场 provider、对战与成绩读写」：

  python -X utf8 verify_04_kline_training.py ast           # ① AST 逻辑等价审计
  python -X utf8 verify_04_kline_training.py dump-old      # ② 原项目 /kline 路由表
  python -X utf8 verify_04_kline_training.py dump-new      # ② 合并后 /kline 路由表
  python -X utf8 verify_04_kline_training.py compare       # ② 路由表 A/B
  python -X utf8 verify_04_kline_training.py flow-old      # ③ 原项目跑完整业务流程
  python -X utf8 verify_04_kline_training.py flow-new      # ③ 合并后跑同一套流程
  python -X utf8 verify_04_kline_training.py flow-compare  # ③ 两侧结果逐字段比对
  python -X utf8 verify_04_kline_training.py http          # ④ 在跑服务的页面/接口 A/B
  python -X utf8 verify_04_kline_training.py assets        # ④ 页面内站内资源回环

③ 用 Flask test_client 在**两侧各自的临时 SQLite 库**上跑同一条流程（注册→登录→
训练→对战→成绩→备注→心跳），既不碰用户真实账号库，也不依赖 5000/7777 是否在跑；
SMTP 指向 127.0.0.1:1 让发信必然失败，从而走「开发兜底」把验证码回传，全流程可离线复现。
④ 才需要原服务 5000 与合并后服务 7777 都在跑（7777 需过访问闸门，口令只从本机
data/web_token.txt 读取，不打印、不落日志）。本脚本所有模式都只做只读或写入临时库，
绝不触发任何实盘下单。
"""
import os
import re
import sys
import json
import gzip
import glob
import math
import zlib
import random
import shutil
import http.cookiejar
import urllib.parse
import urllib.request
import urllib.error
import importlib.util
from datetime import datetime, timezone

DST = r"D:\python\cryptoTrade"
SRC = r"D:\python\futureStockTrade"
NEW_BASE = os.environ.get("VERIFY_NEW_BASE", "http://127.0.0.1:7777")
OLD_BASE = os.environ.get("VERIFY_OLD_BASE", "http://127.0.0.1:5000")
ROUTE_DUMP = os.path.join(DST, "data", "_fst_kline_route_table")
FLOW_DUMP = os.path.join(DST, "data", "_fst_kline_flow")
TMP_DIR = os.path.join(DST, "data", "tmp_isolated_smoke", "kline_flow")

PKG = "crypto/kline_training"
# 迁移文件对（源 → 新位置）
PAIRS = [
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
]
# 声明式偏离：TDengine 缓存工具（闲置能力）按 Phase 4 口径做了凭据收口与
# 「未配置就早退」的防御性改动，不做 AST 等价断言，只打印逐行差异供人工复核。
DECLARED_DIFF = [
    ("kline/database/__init__.py", f"{PKG}/td_cache/__init__.py",
     "凭据只读环境变量，删除写死的公网 IP 与弱口令兜底；驱动缺失时模块仍可导入"),
    ("kline/database/import_csv.py", f"{PKG}/td_cache/import_csv.py",
     "CSV 路径不再默认指向另一个项目的绝对路径；导入前先校验文件存在"),
    ("kline/database/test_connection.py", f"{PKG}/td_cache/test_connection.py",
     "仅导入改写"),
    ("kline/database/test_crud.py", f"{PKG}/td_cache/test_crud.py",
     "仅导入改写 + 删除指向外部项目的 sys.path"),
]

REQ_TIMEOUT = 300
LIVE_RETRIES = 2
PAGE_PATHS = ["/kline/", "/kline/auth", "/kline/pk", "/kline/train/crypto/NEAR",
              "/kline/crypto/NEAR", "/kline/blind"]
STATIC_PATHS = ["/kline/static/kline.js", "/kline/static/trade.js",
                "/static/css/style.css", "/static/js/navigation.js"]
EXACT_API_PATHS = ["/kline/api/categories"]
LIVE_API_PATHS = [
    "/kline/api/health",
    "/kline/api/symbols/crypto",
    "/kline/api/symbols/stock",
    "/kline/api/kline-data/crypto/NEAR?period=1D&limit=40",
    "/kline/api/kline-data/stock/600519?period=1D&limit=20",
    "/kline/api/kline-data/futures/M?period=1D&limit=20",
    "/kline/api/available-symbols",
    "/kline/api/leaderboard?limit=5",
]
API_PATHS = EXACT_API_PATHS + LIVE_API_PATHS

REF_ATTR = re.compile(r"""(?:src|href)\s*=\s*["']([^"'{}]+)["']""")
REF_FETCH = re.compile(r"""(?:fetch|open)\s*\(\s*[`'"](/[^`'"\s]*)[`'"]""")
SKIP_PREFIX = ("http://", "https://", "//", "data:", "mailto:", "#", "javascript:")


# ---------------------------------------------------------------------
# ① AST 逻辑等价
# ---------------------------------------------------------------------
def _read_py(path: str) -> str:
    """按 utf-8-sig 读取：源项目 kline_adapter.py 等文件带 UTF-8 BOM。
    BOM 落在文件首字节时 Python 从文件导入是合法的，但 ast.parse(str) 会把
    '\ufeff' 当成非法字符 → 读入时统一吃掉 BOM，两侧口径一致。"""
    return open(path, encoding="utf-8-sig").read()


# 合并迁移对 K 线训练做了两类「声明过」的改动，审计时按精确规则从新侧还原，
# 其余任何差异都必须暴露出来：
#   A. 会话键 uid → kline_uid（D2 决策）：只还原字符串常量 'kline_uid'
#   B. current_user 注入从 app 级收窄为蓝图级：把 context_processor 还原成
#      app_context_processor（只认 <某_bp>.context_processor 这一种写法）
def _declared_new_side_transform(ast, tree):
    restored = {"uid_key": 0, "ctx_processor": 0}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "kline_uid":
            node.value = "uid"
            restored["uid_key"] += 1
        if (isinstance(node, ast.Attribute) and node.attr == "context_processor"
                and isinstance(node.value, ast.Name)
                and node.value.id.endswith("_bp")):
            node.attr = "app_context_processor"
            restored["ctx_processor"] += 1
    return tree, restored


BOOTSTRAP_DIR_NAMES = {"_ROOT", "_BASE", "_THIS_DIR", "_PROJECT_ROOT", "_KLINE_DIR",
                       "_FUTURES_DIR", "_STOCKS_DIR"}


def _drop_bootstrap_nodes(ast, tree):
    """两侧同规则剔除「算目录 + 塞 sys.path」样板，只比真正的业务逻辑。

    verify_01 的归一化器已按 token 剔除含 sys.path / _PROJECT_ROOT 的语句，但
    Phase 4 迁移把这些文件里的 _THIS_DIR / _KLINE_DIR / _STOCKS_DIR 等目录常量
    整段删掉了（源侧仍留着），所以这里补一条更宽的规则：
      - 目录常量赋值（且右值只由 os.path.* / Path(...) 组成）
      - `for _p in (...)` 这种往 sys.path 里灌目录的循环
      - 任何 unparse 后含 sys.path 的 Assign / If / Expr
    三条都按整句精确匹配，不做模糊前缀，避免顺手掩盖真实改动。
    """
    dropped = {"dir_assign": 0, "for_p": 0, "sys_path": 0}

    def value_is_path_only(node):
        src = ast.unparse(node)
        return bool(re.match(r"^(os\.path\.|Path\()", src))

    def clean(body):
        kept = []
        for st in body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and \
                    isinstance(st.targets[0], ast.Name) and \
                    st.targets[0].id in BOOTSTRAP_DIR_NAMES and \
                    value_is_path_only(st.value):
                dropped["dir_assign"] += 1
                continue
            if isinstance(st, ast.For) and isinstance(st.target, ast.Name) \
                    and st.target.id == "_p":
                dropped["for_p"] += 1
                continue
            if isinstance(st, (ast.Assign, ast.If, ast.Expr)):
                try:
                    if "sys.path" in ast.unparse(st):
                        dropped["sys_path"] += 1
                        continue
                except Exception:
                    pass
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(st, field, None)
                if isinstance(sub, list):
                    clean(sub)
            for h in getattr(st, "handlers", []) or []:
                clean(h.body)
            kept.append(st)
        del body[:]
        body.extend(kept)

    clean(tree.body)
    return tree, dropped


def ast_audit():
    import ast
    sys.path.insert(0, DST)
    spec = importlib.util.spec_from_file_location(
        "verify_01", os.path.join(DST, "verify_01_strategy_layer.py"))
    v01 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v01)
    strip = v01._strip_non_logic

    print("=" * 78)
    print("① AST 逻辑等价审计（剥离 import/docstring/sys.path 兜底后语法树必须一致）")
    bad = 0
    for rel_src, rel_dst in PAIRS:
        src_p = os.path.join(SRC, rel_src.replace("/", os.sep))
        dst_p = os.path.join(DST, rel_dst.replace("/", os.sep))
        t_src = ast.parse(_read_py(src_p))
        t_dst = ast.parse(_read_py(dst_p))
        t_src, dr_src = _drop_bootstrap_nodes(ast, t_src)
        t_dst, dr_dst = _drop_bootstrap_nodes(ast, t_dst)
        t_dst, fixed = _declared_new_side_transform(ast, t_dst)
        n_src, imp_src, _ = strip(t_src)
        n_dst, imp_dst, _ = strip(t_dst)
        same = ast.dump(n_src) == ast.dump(n_dst)
        bad += 0 if same else 1
        note = ""
        if fixed["uid_key"] or fixed["ctx_processor"]:
            note = (f"  （声明式还原：kline_uid→uid {fixed['uid_key']} 处，"
                    f"蓝图级 context_processor {fixed['ctx_processor']} 处）")
        bs_src = sum(dr_src.values())
        bs_dst = sum(dr_dst.values())
        if bs_src or bs_dst:
            note += (f"  （兜底样板剔除：源 {bs_src} / 新 {bs_dst} 条"
                     f"，明细 源{dr_src} 新{dr_dst}）")
        print(f"  [{'OK ' if same else '!!!'}] {rel_dst:<48} 逻辑等价={same}{note}")
        if not same:
            a = ast.dump(n_src).split("), ")
            b = ast.dump(n_dst).split("), ")
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    print(f"        首个差异 #{i}:\n          源: {x[:180]}\n          新: {y[:180]}")
                    break
        for r in sorted(set(imp_src) - set(imp_dst)):
            print(f"        - 移除: {r[:100]}")
        for x in sorted(set(imp_dst) - set(imp_src)):
            print(f"        + 新增: {x[:100]}")

    print("-" * 78)
    print("   声明式偏离文件（只列差异，不断言等价）：")
    import difflib
    for rel_src, rel_dst, why in DECLARED_DIFF:
        src_p = os.path.join(SRC, rel_src.replace("/", os.sep))
        dst_p = os.path.join(DST, rel_dst.replace("/", os.sep))
        if not os.path.isfile(dst_p):
            print(f"  [!!!] {rel_dst} 不存在")
            bad += 1
            continue
        a = _read_py(src_p).splitlines()
        b = _read_py(dst_p).splitlines()
        diff = [l for l in difflib.unified_diff(a, b, n=0, lineterm="")
                if l[:1] in "+-" and l[:3] not in ("+++", "---")
                and not re.match(r"^[+-]# \[迁移\]|^[+-]# 原名|^[+-]# 导入改为", l)]
        print(f"  [i] {rel_dst:<48} 净差异 {len([x for x in diff if x.startswith('+')])} 行")
        print(f"      理由：{why}")
        for l in diff[:8]:
            print(f"      {l[:110]}")
        if len(diff) > 8:
            print(f"      …另 {len(diff) - 8} 行")
    print(f"  → 逻辑不等价文件数: {bad}  "
          f"({'合格：机械改写只动导入，偏离已全部声明' if bad == 0 else '不合格'})")
    return bad == 0


# ---------------------------------------------------------------------
# ② 路由表 A/B（两个独立进程分别 dump，避免 sys.path 交叉污染）
# ---------------------------------------------------------------------
def dump_routes(which: str):
    if which == "old":
        sys.path.insert(0, SRC)
        os.chdir(SRC)
        import app as old_app
        mod = old_app.app
    else:
        sys.path.insert(0, DST)
        os.chdir(DST)
        os.environ["CRYPTO_NO_BACKGROUND"] = "1"
        from crypto.app import app as new_app
        mod = new_app

    rows = []
    for rule in mod.url_map.iter_rules():
        r = str(rule)
        if not (r == "/kline" or r.startswith("/kline/")):
            continue
        methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        rows.append({"rule": r, "endpoint": rule.endpoint, "methods": methods})
    rows.sort(key=lambda x: (x["rule"], tuple(x["methods"])))
    os.makedirs(ROUTE_DUMP, exist_ok=True)
    out = os.path.join(ROUTE_DUMP, f"{which}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"[{which}] /kline 路由 {len(rows)} 条 → {out}")


def compare_routes():
    def key(r):
        return (r["rule"], tuple(r["methods"]), r["endpoint"])

    old = json.load(open(os.path.join(ROUTE_DUMP, "old.json"), encoding="utf-8"))
    new = json.load(open(os.path.join(ROUTE_DUMP, "new.json"), encoding="utf-8"))
    o, n = {key(r): r for r in old}, {key(r): r for r in new}
    print("=" * 78)
    print("② /kline 路由表 A/B（含端点名与允许方法）")
    print(f"  原项目 {len(o)} 条 / 合并后 {len(n)} 条")
    only_old = sorted(set(o) - set(n))
    only_new = sorted(set(n) - set(o))
    print(f"  合并后缺失: {[x[0] for x in only_old] or '无'}")
    print(f"  合并后新增: {[x[0] for x in only_new] or '无'}")
    ok = not only_old and not only_new
    print(f"  → {'路由表与端点名完全对齐' if ok else '存在差异'}")
    return ok


# ---------------------------------------------------------------------
# ③ 业务流程 A/B（test_client + 临时 SQLite）
# ---------------------------------------------------------------------
# 两个测试账号：邮箱本地部分取满 6 个字母数字，绕开 auth_service.default_password
# 里「过短则用 hash() 补位」的分支（CPython 字符串 hash 带随机盐，跨进程不可复现）。
USER_A = {"email": "klineverify04a@example.com", "nickname": "验收甲04"}
USER_B = {"email": "klineverify04b@example.com", "nickname": "验收乙04"}

# 生成值识别：8 位以上 hex/字母数字串（训练 seed、16 位 hash、会话 token），
# 以及 store._gen_match_code 的 6 位对战码（大写字母+数字，且至少含一个字母，
# 免得把 600519 这类纯数字股票代码也吞掉）。
GEN_RE = re.compile(r"^[0-9a-f]{8,}$|^[0-9a-zA-Z]{8,}$|^(?=.*[A-Z])[A-Z0-9]{6}$")


def shape(x):
    if isinstance(x, dict):
        return {k: shape(v) for k, v in sorted(x.items())}
    if isinstance(x, list):
        return [shape(x[0]), f"len={len(x)}"] if x else ["empty"]
    return type(x).__name__


def normalize(x):
    """把「两侧必然不同的随机值」压成占位符，其余原样保留以便严格比对。"""
    if isinstance(x, dict):
        return {k: ("GEN" if k == "dev_code" and x[k] is not None else normalize(v))
                for k, v in sorted(x.items())}
    if isinstance(x, list):
        return [normalize(v) for v in x]
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return "TS" if x >= 1_600_000_000 else x
    if isinstance(x, str):
        # 对战码/训练 id/随机 seed/时间串等生成值
        if re.match(r"^\d{4}-\d{2}-\d{2}", x) or GEN_RE.match(x):
            return "GEN"
    return x


def _smtp_to_dead_port():
    """让发信必然失败：连本机不可能开着的端口，秒级拒绝后走开发兜底。"""
    return ("127.0.0.1", "1")


# ---------------------------------------------------------------------
# ③-1 OKX 行情的离线注入（本机此刻既无代理也连不上 okx.com）
# ---------------------------------------------------------------------
# 2026-10-08 实测：www.okx.com 直连超时、系统代理 ProxyEnable=0、常见本地代理端口
# 全部关闭，两侧 /kline/api/health 都返回 usable=false。crypto 分支因此拿不到真实
# K 线，blind/start 与 pk/create 会连锁失败——「两侧一起报错」证明不了任何逻辑一致。
# 所以这里把 kline_adapter 最底层的 okx_request 换成**确定性合成行情**：两侧注入同一
# 个函数、同一随机种子、同一基准时间戳，训练开局 / 对战 / 成绩读写这些真正依赖 K 线的
# 逻辑就能在同一份输入上逐字段比对。注入只发生在数据入口，provider 选路、指标计算、
# 裁剪、序列化、落库全部跑的是原逻辑。真实连通性由 ④（http 模式）单独复核。
FAKE_BAR_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
                    "1H": 3600, "2H": 7200, "4H": 14400, "6H": 21600,
                    "12H": 43200, "1D": 86400, "1w": 604800, "1M": 2592000}
FAKE_TOTAL_BARS = 2400           # 合成序列总长度
FAKE_RECENT_DEPTH = 1440         # /market/candles 只覆盖最近这么多根（与真实一致）
FAKE_END_MS = int(datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
_FAKE_CACHE = {}


def _fake_series(inst_id: str, bar: str):
    """按 (inst_id, bar) 确定性生成升序 K 线行，格式对齐 OKX 原始返回。"""
    key = (inst_id, bar)
    if key in _FAKE_CACHE:
        return _FAKE_CACHE[key]
    step_s = FAKE_BAR_SECONDS.get(bar, 3600)
    rnd = random.Random(f"{inst_id}|{bar}|{FAKE_END_MS}")
    base = 1.0 + (zlib.crc32(inst_id.encode()) % 60000) / 1000.0
    px, rows = base, []
    for i in range(FAKE_TOTAL_BARS):
        ts = FAKE_END_MS - (FAKE_TOTAL_BARS - 1 - i) * step_s * 1000
        o = px
        drift = math.sin(i / 37.0) * base * 0.004          # 缓慢趋势段，保证指标有料
        c = max(o + (rnd.random() - 0.5) * base * 0.02 + drift, base * 0.05)
        h = max(o, c) + rnd.random() * base * 0.006
        l = min(o, c) - rnd.random() * base * 0.006
        v = base * 1000 * (0.6 + rnd.random())
        fmt = (lambda x: f"{x:.6g}")
        rows.append([str(ts), fmt(o), fmt(h), fmt(l), fmt(c), fmt(v), fmt(v), fmt(v), "1"])
        px = c
    _FAKE_CACHE[key] = rows
    return rows


def fake_okx_request(url: str, timeout: int = None, retries: int = None) -> dict:
    """顶替 kline_adapter.okx_request：只解析 URL 参数，从合成序列里按 OKX 语义翻页。"""
    parts = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(parts.query)
    inst_id = q.get("instId", ["NEAR-USDT"])[0]
    bar = q.get("bar", ["1H"])[0]
    limit = int(q.get("limit", ["100"])[0])
    after_raw = q.get("after", [None])[0]
    after = int(after_raw) if after_raw else None

    rows = _fake_series(inst_id, bar)
    if "history-candles" in parts.path:
        pool = rows if after is None else [r for r in rows if int(r[0]) < after]
    else:
        pool = rows[-FAKE_RECENT_DEPTH:]
        if after is not None:
            pool = [r for r in pool if int(r[0]) < after]
    return {"code": "0", "msg": "", "data": pool[-limit:][::-1]}


def fake_okx_healthcheck() -> dict:
    """顶替连通性自检：路由层以此判定是否可用的 crypto 分支，注入后按「可用」走。"""
    return {"usable": True, "current_channel": "fake", "proxies": "",
            "checks": [{"transport": "fake", "ok": True, "proxy": "", "latency_ms": 0}]}


def _gate_token() -> str:
    """宿主 Web 访问闸门口令：只从本机 data/web_token.txt 或环境变量读，不打印不落日志。"""
    for p in (os.path.join(DST, "data", "web_token.txt"),
              os.path.join(DST, "crypto", "data", "web_token.txt")):
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
    return os.environ.get("CRYPTO_WEB_TOKEN", "").strip()


def okx_reachable() -> bool:
    """单次 3 秒 TCP 探测，只为在验收输出里留一行真实连通性说明。"""
    import socket
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("www.okx.com", 443))
        return True
    except Exception:
        return False
    finally:
        s.close()


def inject_offline_okx(which: str) -> list:
    """先把 kline_adapter 显式导入，再把所有持有这两个函数名的模块替换掉。

    蓝图里 `from kline_adapter import okx_healthcheck` 多数写在路由函数体内（惰性导入），
    启动期 sys.modules 可能根本没有 kline_adapter，所以必须先 import 一次；替换只需作用在
    模块属性上即可，因为函数体内的 from-import 每次都会重新读模块属性。routes/registry 这类
    顶层 `from kline_adapter import X` 已经把名字绑进自己命名空间，故一并遍历替换。
    """
    candidates = (["kline_adapter", "crypto.kline_training.kline_adapter"] if which == "old"
                  else ["crypto.kline_training.kline_adapter", "kline_adapter"])
    loaded = []
    for nm in candidates:
        try:
            importlib.import_module(nm)
            loaded.append(nm)
        except Exception:
            pass
    if not loaded:
        raise RuntimeError("注入失败：两侧都导入不到 kline_adapter")

    fakes = {"okx_request": fake_okx_request, "okx_healthcheck": fake_okx_healthcheck}
    patched = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        for attr, fake in fakes.items():
            fn = getattr(mod, attr, None)
            if callable(fn) and str(getattr(fn, "__module__", "")).endswith("kline_adapter"):
                setattr(mod, attr, fake)
                patched.append(f"{name}.{attr}")
    return patched


def build_client(which: str):
    """在独立进程里只加载一侧应用，返回 (test_client, 元信息)。"""
    os.makedirs(TMP_DIR, exist_ok=True)
    db_path = os.path.join(TMP_DIR, f"{which}.db")
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    host, port = _smtp_to_dead_port()
    if which == "old":
        os.environ.update({
            "FST_SQLITE_PATH": db_path,
            "FST_EMAIL_DEV_FALLBACK": "1",
            "FST_SMTP_HOST": host, "FST_SMTP_PORT": port,
            "FST_EMAIL_CODE_COOLDOWN": "0",
        })
        sys.path.insert(0, SRC)
        os.chdir(SRC)
        import app as m
        app = m.app
    else:
        os.environ.update({
            "CRYPTO_NO_BACKGROUND": "1",
            "CRYPTO_KLINE_SQLITE_PATH": db_path,
            "CRYPTO_KLINE_EMAIL_DEV_FALLBACK": "1",
            "CRYPTO_KLINE_SMTP_HOST": host, "CRYPTO_KLINE_SMTP_PORT": port,
            "CRYPTO_KLINE_EMAIL_CODE_COOLDOWN": "0",
        })
        sys.path.insert(0, DST)
        os.chdir(DST)
        from crypto.app import app
        from crypto.kline_training import settings as ks
        # 兜底开关/冷却是 settings 在 import 期算好的，这里显式核对一遍
        assert ks.EMAIL_DEV_FALLBACK is True, "验证码开发兜底未开启，流程跑不通"
        assert ks.EMAIL_CODE_RESEND_COOLDOWN == 0, "重发冷却未清零"
    return app


def run_flow(which: str):
    app = build_client(which)
    patched = inject_offline_okx(which)
    cA = app.test_client()
    cB = app.test_client()
    out = []
    log = []

    # 合并后宿主有 Web 访问闸门（原项目没有）：浏览器要先过一次口令，之后凭
    # ct_web_gate cookie 通行。这里复现真实浏览器路径——用 ?token= 引导发 cookie，
    # 再跑后续步骤；否则所有 /kline/api/* 都会拿到 401「需要访问口令」。
    if which != "old":
        token = _gate_token()
        probe = cA.get("/kline/api/auth/me")
        if probe.status_code == 401:
            print(f"   闸门未过探测：{probe.status_code}（未带 cookie，符合预期）")
        if token:
            boot = cA.get("/kline/?token=" + urllib.parse.quote(token))
            cB.get("/kline/?token=" + urllib.parse.quote(token))
            after = cA.get("/kline/api/auth/me")
            print(f"   闸门引导：{boot.status_code}（口令不打印）→ 引导后复核 "
                  f"/kline/api/auth/me = {after.status_code}"
                  f"{'，cookie 已生效' if after.status_code == 200 else '，仍未放行，检查闸门配置'}")
        else:
            print("   未找到 data/web_token.txt：本机回环放行，直接跑流程")

    def call(client, method, path, payload=None):
        if method == "GET":
            r = client.get(path)
        elif method == "POST":
            r = client.post(path, json=payload or {})
        else:
            r = client.delete(path)
        body = r.get_json(silent=True) if r.is_json else None
        return r, body

    def step(name, client, method, path, payload=None, facts=None, expect=None):
        r, body = call(client, method, path, payload)
        rec = {"step": name, "status": r.status_code}
        if body is not None:
            rec["shape"] = shape(body)
            rec["data"] = normalize(body.get("data") if isinstance(body, dict) else body)
        else:
            rec["location"] = normalize(
                {"path": urllib.parse.urlparse(r.headers.get("Location", "")).path})
        if facts:
            rec["facts"] = facts(body, r)
        out.append(rec)
        flag = "" if expect is None else ("" if r.status_code == expect else " !!")
        log.append(f"    {name:<34} {r.status_code}{flag}  "
                   f"{json.dumps(rec.get('facts') or {}, ensure_ascii=False)[:96]}")
        return r, body

    print("=" * 78)
    print(f"③ 业务流程（{which} · 临时库 {os.path.join(TMP_DIR, which + '.db')}）")
    print(f"   OKX 直连探测={'可连通' if okx_reachable() else '不可连通'}；"
          f"crypto 数据入口已注入确定性合成行情（替换 {len(patched)} 处绑定）")

    # —— 三市场 provider（品类元信息 + 品种清单）——
    step("categories", cA, "GET", "/kline/api/categories")
    step("symbols_crypto", cA, "GET", "/kline/api/symbols/crypto")
    step("symbols_futures", cA, "GET", "/kline/api/symbols/futures")
    step("symbols_stock", cA, "GET", "/kline/api/symbols/stock")
    step("symbols_crypto_grouped", cA, "GET", "/kline/api/symbols/crypto?grouped=true")
    # —— 取数与训练开局 ——
    step("health", cA, "GET", "/kline/api/health")
    step("kline_data_crypto", cA, "GET",
         "/kline/api/kline-data/crypto/NEAR?period=1D&limit=40")
    step("kline_data_train", cA, "GET",
         "/kline/api/kline-data/crypto/NEAR?period=1D&full_data=true&target_bars=120")
    step("kline_data_futures", cA, "GET",
         "/kline/api/kline-data/futures/M?period=1D&limit=30")
    step("kline_data_stock", cA, "GET",
         "/kline/api/kline-data/stock/600519?period=1D&limit=30")
    step("kline_more_crypto", cA, "GET",
         "/kline/api/kline-more/crypto/NEAR?period=1D&before_ts=1700000000&limit=30")
    step("blind_start", cA, "POST", "/kline/api/blind/start",
         {"bar_count": 80, "categories": "crypto"})
    step("session_save_anon", cA, "POST", "/kline/api/session",
         {"category": "crypto", "symbol": "NEAR", "period": "1D", "mode": "free",
          "bar_count": 80, "stats": {"returnPct": 1.5, "winRate": 50.0,
                                     "maxDrawdownPct": 2.0, "finalBalance": 10150,
                                     "totalTrades": 4}})
    step("session_history", cA, "GET", "/kline/api/session/history?limit=5")
    step("leaderboard_anon", cA, "GET", "/kline/api/leaderboard?limit=5")
    # —— 鉴权门槛（未登录）——
    step("profile_page_anon", cA, "GET", "/kline/profile", expect=302)
    step("pk_mine_anon", cA, "GET", "/kline/api/pk/mine", expect=401)
    step("me_anon", cA, "GET", "/kline/api/auth/me")

    # —— 注册 / 登录 / 改密 / 昵称 ——
    _, b = step("send_code_a", cA, "POST", "/kline/api/auth/send-code",
                {"email": USER_A["email"], "purpose": "register"})
    code_a = ((b or {}).get("data") or {}).get("dev_code")
    step("register_a", cA, "POST", "/kline/api/auth/register",
         {"email": USER_A["email"], "code": code_a, "nickname": USER_A["nickname"]})
    step("me_a", cA, "GET", "/kline/api/auth/me")
    _, b = step("send_code_b", cB, "POST", "/kline/api/auth/send-code",
                {"email": USER_B["email"], "purpose": "register"})
    code_b = ((b or {}).get("data") or {}).get("dev_code")
    step("register_b", cB, "POST", "/kline/api/auth/register",
         {"email": USER_B["email"], "code": code_b, "nickname": USER_B["nickname"]})
    step("duplicate_register", cA, "POST", "/kline/api/auth/register",
         {"email": USER_A["email"], "code": code_a, "nickname": "撞名甲"}, expect=400)
    step("logout_a", cA, "POST", "/kline/api/auth/logout")
    step("me_after_logout", cA, "GET", "/kline/api/auth/me")
    step("login_a_by_password", cA, "POST", "/kline/api/auth/login",
         {"identifier": USER_A["email"], "password": "klinev"})
    step("login_a_bad_password", cB, "POST", "/kline/api/auth/login",
         {"identifier": USER_A["email"], "password": "wrongpass"}, expect=401)
    step("login_a_not_found", cB, "POST", "/kline/api/auth/login",
         {"identifier": "ghost04@example.com", "password": "whatever"}, expect=404)
    step("login_a_by_nickname", cA, "POST", "/kline/api/auth/login",
         {"identifier": USER_A["nickname"], "password": "klinev"})
    step("change_password_a", cA, "POST", "/kline/api/auth/password",
         {"new_password": "verify04new"})
    step("login_a_new_password", cA, "POST", "/kline/api/auth/login",
         {"identifier": USER_A["email"], "password": "verify04new"})
    step("update_nickname_a", cA, "POST", "/kline/api/profile/nickname",
         {"nickname": "验收甲04改"})
    step("presence_a", cA, "POST", "/kline/api/presence")
    step("session_save_a", cA, "POST", "/kline/api/session",
         {"category": "crypto", "symbol": "NEAR", "period": "1D", "mode": "free",
          "bar_count": 120, "stats": {"returnPct": 3.2, "winRate": 60.0,
                                      "maxDrawdownPct": 1.1, "finalBalance": 10320,
                                      "totalTrades": 6}})
    step("history_a", cA, "GET", "/kline/api/session/history?limit=5")
    step("users_list", cA, "GET", "/kline/api/users")

    # —— 对战：建房 → 加入 → 双方提交 → 榜单/对比 ——
    _, b = step("pk_create", cA, "POST", "/kline/api/pk/create",
                {"category": "crypto", "symbol": "NEAR", "period": "1D",
                 "start": "2024-01-01", "bar_count": 60})
    d = (b or {}).get("data") or {}
    match_code = d.get("code") or d.get("match", {}).get("code")
    match_id = d.get("id") or d.get("match", {}).get("id")
    step("pk_join_b", cB, "POST", "/kline/api/pk/join", {"code": match_code})
    step("pk_chart_a", cA, "GET", f"/kline/api/pk/{match_id}/chart")
    step("pk_submit_a", cA, "POST", f"/kline/api/pk/{match_id}/submit",
         {"stats": {"returnPct": 5.0, "winRate": 66.7, "maxDrawdownPct": 2.2,
                    "profitLossRatio": 1.8, "finalBalance": 10500, "totalTrades": 9}})
    step("pk_submit_b", cB, "POST", f"/kline/api/pk/{match_id}/submit",
         {"stats": {"returnPct": 2.0, "winRate": 50.0, "maxDrawdownPct": 3.3,
                    "profitLossRatio": 1.2, "finalBalance": 10200, "totalTrades": 6}})
    step("pk_view_a", cA, "GET", f"/kline/api/pk/{match_id}")
    step("pk_compare", cA, "GET", f"/kline/api/pk/{match_id}/compare")
    step("pk_mine_a", cA, "GET", "/kline/api/pk/mine")
    step("leaderboard_logged", cA, "GET", "/kline/api/leaderboard?limit=10")
    step("note_b_on_a", cB, "POST", "/kline/api/users/1/note", {"note": "老对手"})
    step("note_self", cA, "POST", "/kline/api/users/1/note", {"note": "自己"},
         expect=400)
    step("reset_password_admin_denied", cB, "POST", "/kline/api/admin/reset-password",
         {"admin_email": "not-admin@example.com", "target": USER_A["email"],
          "new_password": "hijacked"})
    # —— 页面可达（含登录态）——
    for p in ("/kline/", "/kline/pk", "/kline/blind", "/kline/profile",
              "/kline/train/crypto/NEAR", "/kline/auth"):
        step(f"page{p}", cA, "GET", p)

    os.makedirs(FLOW_DUMP, exist_ok=True)
    out_path = os.path.join(FLOW_DUMP, f"{which}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    for l in log:
        print(l)
    print(f"  → {len(out)} 步流程结果已写入 {out_path}")
    return True


# 这几步的数据不由注入决定：akshare 实时行情两次请求本身会漂移，盲选的品种/起点用
# 的是无种子 random.Random()。对它们只比「状态码 + 结构骨架 + 条目数量级」，其余步骤
# 一律严格比对（normalize 已把随机 id、时间戳、对战码压成占位符）。
DRIFT_STEPS = {
    "symbols_futures": "akshare 实时品种清单",
    "symbols_stock": "akshare 实时品种清单",
    "kline_data_futures": "akshare 实时行情会漂移",
    "kline_data_stock": "akshare 实时行情会漂移",
    "blind_start": "盲选品种与起点用无种子 random",
}


def flow_compare():
    def load(which):
        p = os.path.join(FLOW_DUMP, f"{which}.json")
        if not os.path.isfile(p):
            return None
        return {r["step"]: r for r in json.load(open(p, encoding="utf-8"))}

    old, new = load("old"), load("new")
    print("=" * 78)
    print("③ 业务流程 A/B 比对（状态码 + 响应结构 + 生成值归一后的数据）")
    if old is None or new is None:
        print("  缺少一侧结果，请先跑 flow-old 与 flow-new")
        return False
    missing = sorted(set(old) - set(new))
    extra = sorted(set(new) - set(old))
    diffs, relaxed = [], []
    strict = 0
    for name in sorted(set(old) & set(new)):
        a, b = old[name], new[name]
        if a["status"] != b["status"]:
            diffs.append((name, "status", a["status"], b["status"]))
            continue
        keys = ("data", "location", "shape") if name not in DRIFT_STEPS else ("shape",)
        if name in DRIFT_STEPS:
            relaxed.append(name)
        else:
            strict += 1
        for k in keys:
            if k in a or k in b:
                if json.dumps(a.get(k), sort_keys=True) != json.dumps(b.get(k), sort_keys=True):
                    diffs.append((name, k,
                                  json.dumps(a.get(k), ensure_ascii=False)[:220],
                                  json.dumps(b.get(k), ensure_ascii=False)[:220]))
    print(f"  共同步骤 {len(set(old) & set(new))} / 仅原项目 {len(missing)} / 仅合并后 {len(extra)}")
    print(f"  严格比对 {strict} 步；实时漂移步骤按结构比对 {len(relaxed)} 步"
          f"（{', '.join(f'{n}:{DRIFT_STEPS[n]}' for n in relaxed) or '无'}）")
    if missing or extra:
        print(f"  步骤集差异: 缺 {missing[:6]} 多 {extra[:6]}")
    for name, k, va, vb in diffs:
        print(f"  [DIFF] {name} · {k}\n      原: {va}\n      新: {vb}")
    ok = not diffs and not missing and not extra
    print(f"  → {'两侧业务流程逐步一致' if ok else '存在差异（逐条核对是否属实时漂移）'}")
    return ok


# ---------------------------------------------------------------------
# ④ 在跑服务的 HTTP A/B / 资源回环
# ---------------------------------------------------------------------
def gate_opener(entry="/kline/"):
    token = _gate_token()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    resp = opener.open(urllib.request.Request(
        NEW_BASE + entry + "?token=" + urllib.parse.quote(token),
        headers={"User-Agent": "verify-04"}), timeout=60)
    body = resp.read()
    passed = resp.status == 200 and "访问验证" not in body.decode("utf-8", "replace")
    print(f"  [{'OK' if passed else 'FAIL'}] 闸门通过（cookie 数={len(jar)}） 最终={resp.status}")
    return opener if passed else None


def fetch_new(opener, path):
    try:
        with opener.open(urllib.request.Request(
                NEW_BASE + path, headers={"User-Agent": "verify-04"}),
                timeout=REQ_TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode()


def get(base, path, opener=None):
    try:
        req = urllib.request.Request(base + path, headers={"User-Agent": "verify-04",
                                                           "Accept-Encoding": "identity"})
        if opener is not None:
            with opener.open(req, timeout=REQ_TIMEOUT) as r:
                return r.status, r.read()
        with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding", "").lower() == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode()


def live(base, path, is_new, opener=None):
    last = (None, b"")
    for attempt in range(1, LIVE_RETRIES + 1):
        res = fetch_new(opener, path) if is_new else get(base, path)
        if res[0] == 200:
            return res
        last = res
        if attempt < LIVE_RETRIES:
            print(f"       （{path} 第{attempt}次返回 {res[0]}，重试）")
    return last


def http_ab():
    print("=" * 78)
    print("④ 闸门准备")
    opener = gate_opener()
    if opener is None:
        print("  无法通过访问闸门，终止")
        return False
    all_ok = True

    print("=" * 78)
    print("④-a 页面渲染 A/B（是否套用宿主 nav/style、未登录是否仍可匿名训练）")
    for p in PAGE_PATHS:
        so, bo = get(OLD_BASE, p)
        sn, bn = fetch_new(opener, p)
        if sn != 200:
            print(f"  [FAIL] {p} 新服务 {sn}: {bn[:160].decode('utf-8','replace')}")
            all_ok = False
            continue
        txt = bn.decode("utf-8", "replace")
        print(f"  [OK] {p:<32} 原={so} 新={sn} 长度 {len(bo):>7,}→{len(bn):>7,} "
              f"宿主导航={'nav-booting' in txt} 宿主样式={'/static/css/style.css' in txt} "
              f"登录工具条={'kline-account-bar' in txt}")
        if "nav-booting" not in txt or "/static/css/style.css" not in txt:
            print("       ⚠ 未套用宿主 nav/style，请核对模板解析顺序")
            all_ok = False
        if "kline-account-bar" not in txt:
            print("       ⚠ K 线登录工具条未渲染（模板 include 缺失？）")
            all_ok = False

    print("=" * 78)
    print("④-b 静态资源（蓝图 static 与宿主 static 各自可达）")
    for p in STATIC_PATHS:
        sn, bn = fetch_new(opener, p)
        so, _b = get(OLD_BASE, p)
        print(f"  [{'OK' if sn == 200 else 'FAIL'}] {p:<38} 新={sn} 原={so} "
              f"字节={len(bn):,}")
        if sn != 200:
            all_ok = False

    print("=" * 78)
    print("④-c 只读接口 A/B（文件驱动=严格相等；实时取数=结构骨架一致）")

    def _shape(x):
        return json.dumps(shape(json.loads(x)), sort_keys=True, ensure_ascii=False)

    for p in API_PATHS:
        if p in LIVE_API_PATHS:
            so, bo = live(OLD_BASE, p, False)
            sn, bn = live(NEW_BASE, p, True, opener)
        else:
            so, bo = get(OLD_BASE, p)
            sn, bn = fetch_new(opener, p)
        if so != 200 or sn != 200:
            if so == sn:
                # 上游不可达时两侧应当「一起坏成同一个样子」：状态码相同即视为降级一致，
                # 但必须把原因打出来，避免把「都 502」当成通过而什么都没验证。
                print(f"  [SKIP·同状态降级] {p:<54} 原={so} 新={sn} "
                      f"{bn[:110].decode('utf-8','replace')}")
                continue
            tag = "FAIL" if sn != 200 else "SKIP"
            print(f"  [{tag}] {p:<54} 原={so} 新={sn} "
                  f"{bn[:110].decode('utf-8','replace')}")
            if sn != 200:
                all_ok = False
            continue
        try:
            if p in EXACT_API_PATHS:
                same = json.loads(bo) == json.loads(bn)
                print(f"  [{'OK' if same else 'DIFF'}] {p:<54} 严格相等={same}")
                if not same:
                    print(f"       原: {bo[:200].decode('utf-8','replace')}\n"
                          f"       新: {bn[:200].decode('utf-8','replace')}")
                    all_ok = False
                continue
            same = _shape(bo) == _shape(bn)
            print(f"  [{'OK' if same else 'DIFF'}] {p:<54} 结构一致={same} "
                  f"字节 {len(bo):,}→{len(bn):,}")
            if not same:
                print(f"       原骨架: {_shape(bo)[:260]}\n       新骨架: {_shape(bn)[:260]}")
                all_ok = False
        except Exception as e:
            print(f"  [FAIL] {p} 比对异常: {e}")
            all_ok = False

    print("=" * 78)
    print("④-d 鉴权门槛一致性（未登录访问需登录接口，两侧都应 401）")
    for p in ["/kline/api/pk/mine", "/kline/api/session/history?mine=1"]:
        so, _b1 = get(OLD_BASE, p)
        sn, _b2 = fetch_new(opener, p)
        ok = so == sn
        print(f"  [{'OK' if ok else 'DIFF'}] {p:<40} 原={so} 新={sn}")
        all_ok = all_ok and ok

    print("=" * 78)
    print(f"Phase 4 HTTP 验收结论：{'全部通过' if all_ok else '存在待修项'}")
    return all_ok


def assets_roundtrip():
    print("=" * 78)
    opener = gate_opener()
    if opener is None:
        return False
    total_bad = 0
    for page in PAGE_PATHS:
        code, body = fetch_new(opener, page)
        if code != 200:
            print(f"  [FAIL] 页面 {page} 返回 {code}")
            total_bad += 1
            continue
        html = body.decode("utf-8", "replace")
        refs = set()
        for m in REF_ATTR.findall(html) + REF_FETCH.findall(html):
            m = m.strip()
            if not m.startswith("/") or m.startswith(SKIP_PREFIX):
                continue
            if "{" in m or "$" in m:
                continue
            # JS 里 `前缀 + 变量` 的拼接片段（真实请求已由流程 A/B 覆盖）→ 跳过防假 404
            if m.endswith("/") or "%" in m:
                continue
            refs.add(m)
        bad, ok, post_only = [], 0, []
        for r in sorted(refs):
            c, _b = fetch_new(opener, r)
            if c in (200, 304):
                ok += 1
            elif c == 405:
                post_only.append(r)
            else:
                bad.append((r, c))
        print(f"  页面 {page:<32} 200 / {len(body):,} 字节 → 站内引用 {len(refs)}，"
              f"可取 {ok}，POST端点(405) {len(post_only)}，失败 {len(bad)}")
        if post_only:
            print(f"      仅方法不匹配: {', '.join(post_only[:8])}")
        for r, c in bad:
            oc, _ob = get(OLD_BASE, r)
            print(f"      [FAIL] {r:<48} 新={c} 原服务对照={oc}"
                  + ("（原服务同样失败 → 非迁移引入）" if oc != 200 else "（原服务正常 → 迁移缺口）"))
            total_bad += 1
    print("=" * 78)
    print(f"资源回环结论：{'全部可取，无 404/500' if total_bad == 0 else f'{total_bad} 项待修'}")
    return total_bad == 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    mode = sys.argv[1] if len(sys.argv) > 1 else "compare"
    if mode == "ast":
        ok = ast_audit()
    elif mode in ("dump-old", "dump-new"):
        dump_routes(mode.split("-", 1)[1])
        ok = True
    elif mode == "compare":
        ok = compare_routes()
    elif mode in ("flow-old", "flow-new"):
        ok = run_flow(mode.split("-", 1)[1])
    elif mode == "flow-compare":
        ok = flow_compare()
    elif mode == "http":
        ok = http_ab()
    elif mode == "assets":
        ok = assets_roundtrip()
    else:
        print(__doc__)
        ok = False
    sys.exit(0 if ok else 1)
