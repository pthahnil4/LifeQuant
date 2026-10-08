# -*- coding: utf-8 -*-
"""
Phase 3 验收 · 股票模块合并 A/B
================================
四种模式（路由表比对必须在两个独立进程里分别 dump，避免两侧 sys.path 互相污染）：

  python -X utf8 verify_03_stocks.py ast        # AST 逻辑等价审计（复用 verify_01 归一化器）
  python -X utf8 verify_03_stocks.py dump-old    # 只加载原项目，导出 /stocks 路由表
  python -X utf8 verify_03_stocks.py dump-new    # 只加载合并后宿主，导出 /stocks 路由表
  python -X utf8 verify_03_stocks.py compare     # 比对两份路由表
  python -X utf8 verify_03_stocks.py http        # 对两个在跑的服务做页面/静态/接口 A/B
  python -X utf8 verify_03_stocks.py assets      # 页面内站内资源真实请求回环

前置：原服务 5000 与合并后服务 7777 都在跑；7777 需过访问闸门
     （口令只从本机 data/web_token.txt 读取，不打印、不落日志）。
本脚本只做只读 GET，不触发任何写操作与下单。
"""
import os
import re
import sys
import json
import gzip
import http.cookiejar
import urllib.parse
import urllib.request
import urllib.error
import importlib.util

DST = r"D:\python\cryptoTrade"
SRC = r"D:\python\futureStockTrade"
NEW_BASE = os.environ.get("VERIFY_NEW_BASE", "http://127.0.0.1:7777")
OLD_BASE = "http://127.0.0.1:5000"
ROUTE_DUMP = os.path.join(DST, "data", "_fst_stock_route_table")

# 迁移文件对（源 → 新位置），用于 AST 等价审计
PAIRS = [
    ("stocks/__init__.py", "crypto/stocks/__init__.py"),
    ("stocks/stocks_blueprint.py", "crypto/stocks/routes.py"),
    ("stocks/stock_adapter.py", "crypto/stocks/stock_adapter.py"),
    ("stocks/stock_akshare_api.py", "crypto/stocks/stock_akshare_api.py"),
    ("stocks/jumei_api.py", "crypto/stocks/jumei_api.py"),
    ("futures/cme_api.py", "crypto/futures/cme_api.py"),
    # Phase 2 一并核验：重跑 migrate_02 后仍须保持逻辑等价
    ("futures/futures_blueprint.py", "crypto/futures/routes.py"),
    ("futures/futures_adapter.py", "crypto/futures/futures_adapter.py"),
    ("futures/star_market.py", "crypto/futures/star_market.py"),
    ("futures/batch_trend_analysis.py", "crypto/futures/batch_trend_analysis.py"),
]

PAGE_PATHS = ["/stocks/", "/stocks/detail/600519", "/stocks/multi/600519"]
STATIC_PATHS = ["/stocks/static/stocks.js", "/static/css/style.css"]
# 文件/配置驱动，结果确定 → 要求 JSON 严格相等
EXACT_API_PATHS = ["/stocks/api/config"]
# 依赖 akshare 实时取数，两次请求行情本身会漂移 → 只比结构骨架与派生字段名
LIVE_API_PATHS = [
    "/stocks/api/strategy/data",
    "/stocks/api/strategy/single/600519",
    "/stocks/api/strategy/detail/600519",
    "/stocks/api/strategy/multi/600519",
    "/stocks/api/strategy/detail/600519?strategy=traditional",
]
API_PATHS = EXACT_API_PATHS + LIVE_API_PATHS

# 股票多周期接口要串行拉 6 个周期（5m/15m/30m/1H/4H/1D）的 akshare 数据，
# 单次实测 54~59 秒；连续请求时上游会限流抖动 → 超时给到 300s，并对实时接口重试一次。
REQ_TIMEOUT = 300
LIVE_RETRIES = 2

REF_ATTR = re.compile(r"""(?:src|href)\s*=\s*["']([^"'{}]+)["']""")
REF_FETCH = re.compile(r"""(?:fetch|open)\s*\(\s*[`'"](/[^`'"\s]*)[`'"]""")
SKIP_PREFIX = ("http://", "https://", "//", "data:", "mailto:", "#", "javascript:")


# ---------------------------------------------------------------------
# 验收 1 · AST 逻辑等价（复用 verify_01 的归一化器，保证口径一致）
# ---------------------------------------------------------------------
# jumei_api / cme_api 里 `_ROOT = os.path.dirname(os.path.dirname(...))` 只服务于
# 紧随其后的 `sys.path.insert(0, _ROOT)` 兜底（verify_01 的归一化器已识别并剔除该行，
# 但变量名 _ROOT 不在它的 token 清单里）。迁移把整段兜底删掉了，留下这条赋值就是死代码，
# 故审计时按"精确整句匹配"从两侧同时剔除，不做模糊前缀匹配，避免过度剔除掩盖真实改动。
ROOT_ASSIGN_SRC = "_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"


def _drop_root_assign(tree):
    import ast
    kept = [st for st in tree.body
            if not (isinstance(st, ast.Assign) and ast.unparse(st) == ROOT_ASSIGN_SRC)]
    dropped = len(tree.body) - len(kept)
    tree.body = kept
    return tree, dropped


def ast_audit():
    import ast
    sys.path.insert(0, DST)
    spec = importlib.util.spec_from_file_location(
        "verify_01", os.path.join(DST, "verify_01_strategy_layer.py"))
    v01 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v01)          # 仅定义函数，__main__ 守卫不会执行验收
    strip = v01._strip_non_logic

    print("=" * 78)
    print("验收 1 · AST 逻辑等价审计（剥离 import/docstring/sys.path 兜底后语法树必须一致）")
    bad = 0
    for rel_src, rel_dst in PAIRS:
        src_p = os.path.join(SRC, rel_src.replace("/", os.sep))
        dst_p = os.path.join(DST, rel_dst.replace("/", os.sep))
        t_src = ast.parse(open(src_p, encoding="utf-8").read())
        t_dst = ast.parse(open(dst_p, encoding="utf-8").read())
        t_src, d_old = _drop_root_assign(t_src)
        t_dst, d_new = _drop_root_assign(t_dst)
        n_src, imp_src, doc_src = strip(t_src)
        n_dst, imp_dst, doc_dst = strip(t_dst)
        same = ast.dump(n_src) == ast.dump(n_dst)
        added = sorted(set(imp_dst) - set(imp_src))
        removed = sorted(set(imp_src) - set(imp_dst))
        bad += 0 if same else 1
        note = f"  （另剔除 _ROOT 兜底赋值 {d_old} 处）" if d_old or d_new else ""
        print(f"  [{'OK ' if same else '!!!'}] {rel_dst:<44} 逻辑等价={same} "
              f"导入 {len(added)} 增/{len(removed)} 删{note}")
        if not same:
            a, b = ast.dump(n_src).split("), "), ast.dump(n_dst).split("), ")
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    print(f"        首个差异 #{i}:\n          源: {x[:160]}\n          新: {y[:160]}")
                    break
        for r in removed:
            print(f"        - 移除: {r[:100]}")
        for x in added:
            print(f"        + 新增: {x[:100]}")
    print(f"  → 逻辑不等价文件数: {bad}  "
          f"({'合格：只改导入未改逻辑' if bad == 0 else '不合格'})")
    return bad == 0


# ---------------------------------------------------------------------
# 验收 2 · 路由表 A/B（两进程分别 dump 后比对）
# ---------------------------------------------------------------------
def dump_routes(which: str):
    if which == "old":
        sys.path.insert(0, SRC)
        os.chdir(SRC)
        import app as old_app
        mod, tag = old_app.app, "old"
    else:
        sys.path.insert(0, DST)
        os.chdir(DST)
        os.environ["CRYPTO_NO_BACKGROUND"] = "1"
        from crypto.app import app as new_app
        mod, tag = new_app, "new"

    rows = []
    for rule in mod.url_map.iter_rules():
        if not str(rule).startswith("/stocks"):
            continue
        methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        rows.append({"rule": str(rule), "endpoint": rule.endpoint, "methods": methods})
    rows.sort(key=lambda r: (r["rule"], tuple(r["methods"])))
    os.makedirs(ROUTE_DUMP, exist_ok=True)
    out = os.path.join(ROUTE_DUMP, f"{tag}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"[{tag}] /stocks 路由 {len(rows)} 条 → {out}")


def compare_routes():
    # 端点名两侧必然不同（原为 stocks_blueprint.xxx，现为 stocks.xxx），不计入比对
    def key(r):
        return (r["rule"], tuple(r["methods"]))

    old = {key(r): r for r in json.load(open(os.path.join(ROUTE_DUMP, "old.json"), encoding="utf-8"))}
    new = {key(r): r for r in json.load(open(os.path.join(ROUTE_DUMP, "new.json"), encoding="utf-8"))}
    only_old = sorted(set(old) - set(new))
    only_new = sorted(set(new) - set(old))
    print("=" * 78)
    print("验收 2 · /stocks 路由表 A/B")
    print(f"  原项目 {len(old)} 条 / 合并后 {len(new)} 条")
    print(f"  合并后缺失: {[r[0] for r in only_old] or '无'}")
    print(f"  合并后新增: {[r[0] for r in only_new] or '无'}")
    ok = not only_old and not only_new
    print(f"  → {'路由表完全对齐' if ok else '存在差异'}")
    return ok


# ---------------------------------------------------------------------
# 闸门 + HTTP A/B
# ---------------------------------------------------------------------
def gate_opener(entry="/stocks/"):
    token = ""
    for p in (os.path.join(DST, "data", "web_token.txt"),
              os.path.join(DST, "crypto", "data", "web_token.txt")):
        if os.path.exists(p):
            token = open(p, encoding="utf-8").read().strip()
            break
    token = token or os.environ.get("CRYPTO_WEB_TOKEN", "").strip()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    resp = opener.open(urllib.request.Request(
        NEW_BASE + entry + "?token=" + urllib.parse.quote(token),
        headers={"User-Agent": "verify-03"}), timeout=60)
    body = resp.read()
    passed = resp.status == 200 and "访问验证" not in body.decode("utf-8", "replace")
    print(f"  [{'OK' if passed else 'FAIL'}] 闸门通过（cookie 数={len(jar)}） 最终={resp.status}")
    return opener if passed else None


def fetch_new(opener, path):
    try:
        with opener.open(urllib.request.Request(
                NEW_BASE + path, headers={"User-Agent": "verify-03"}), timeout=REQ_TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode()


def get(base, path, opener=None):
    url = base + path
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "verify-03",
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


def http_ab():
    print("=" * 78)
    print("验收 3 · 闸门准备")
    opener = gate_opener()
    if opener is None:
        print("  无法通过访问闸门，终止 HTTP A/B")
        return False

    all_ok = True
    print("=" * 78)
    print("验收 4 · 页面渲染 A/B（是否套用宿主 nav/style，页面是否可达）")
    for p in PAGE_PATHS:
        so, bo = get(OLD_BASE, p)
        sn, bn = fetch_new(opener, p)
        if sn != 200:
            print(f"  [FAIL] {p} 新服务 {sn}: {bn[:160].decode('utf-8', 'replace')}")
            all_ok = False
            continue
        txt = bn.decode("utf-8", "replace")
        host_nav = "nav-booting" in txt
        host_css = "/static/css/style.css" in txt
        print(f"  [OK] {p:<28} 原={so} 新={sn} 长度 {len(bo):>7,}→{len(bn):>7,} "
              f"宿主导航={host_nav} 宿主样式={host_css}")
        if not (host_nav and host_css):
            print("       ⚠ 未套用宿主 nav/style，请核对模板解析顺序")
            all_ok = False

    print("=" * 78)
    print("验收 5 · 静态资源解析（蓝图 static 与宿主 static 各自可达）")
    for p in STATIC_PATHS:
        sn, bn = fetch_new(opener, p)
        so, bo = get(OLD_BASE, p)
        print(f"  [{'OK' if sn == 200 else 'FAIL'}] {p:<34} 新={sn} 原={so} 字节={len(bn):,}")
        if sn != 200:
            all_ok = False

    print("=" * 78)
    print("验收 6 · 只读接口 A/B（文件驱动=严格相等；实时取数=结构骨架一致）")

    def live(base_path, is_new):
        """实时接口偶发上游限流/慢响应，重试若干次再判定。"""
        last = (None, b"")
        for attempt in range(1, LIVE_RETRIES + 1):
            res = fetch_new(opener, base_path) if is_new else get(OLD_BASE, base_path)
            if res[0] == 200:
                return res
            last = res
            if attempt < LIVE_RETRIES:
                print(f"       （{base_path} 第{attempt}次返回 {res[0]}，重试）")
        return last

    for p in API_PATHS:
        if p in LIVE_API_PATHS:
            so, bo = live(p, False)
            sn, bn = live(p, True)
        else:
            so, bo = get(OLD_BASE, p)
            sn, bn = fetch_new(opener, p)
        if so != 200 or sn != 200:
            tag = 'FAIL' if sn != 200 else 'SKIP'
            print(f"  [{tag}] {p:<50} 原={so} 新={sn}  {bn[:120].decode('utf-8', 'replace')}")
            if sn != 200:
                all_ok = False
            continue
        try:
            jo, jn = json.loads(bo), json.loads(bn)
        except Exception as e:
            print(f"  [FAIL] {p} JSON 解析失败: {e}")
            all_ok = False
            continue

        if p in EXACT_API_PATHS:
            same = jo == jn
            print(f"  [{'OK' if same else 'DIFF'}] {p:<50} 严格相等={same}")
            if not same:
                print(f"       原: {str(jo)[:220]}\n       新: {str(jn)[:220]}")
                all_ok = False
            continue

        def shape(x):
            if isinstance(x, dict):
                return {k: shape(v) for k, v in sorted(x.items())}
            if isinstance(x, list):
                return [shape(x[0]), f"len={len(x)}"] if x else ["empty"]
            return type(x).__name__

        def top_len(x):
            d = x.get("data", x) if isinstance(x, dict) else x
            return len(d) if isinstance(d, (list, dict)) else "n/a"

        sh_o, sh_n = str(shape(jo))[:4000], str(shape(jn))[:4000]
        same = sh_o == sh_n
        print(f"  [{'OK' if same else 'DIFF'}] {p:<50} 结构一致={same} "
              f"条目数 原={top_len(jo)} 新={top_len(jn)}")
        if not same:
            print(f"       原骨架: {sh_o[:260]}\n       新骨架: {sh_n[:260]}")
            all_ok = False

    print("=" * 78)
    print(f"Phase 3 HTTP 验收结论：{'全部通过' if all_ok else '存在待修项'}")
    return all_ok


def assets_roundtrip():
    print("=" * 78)
    opener = gate_opener()
    if opener is None:
        print("  无法通过访问闸门")
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
            # JS 里 `前缀 + code` 的拼接片段，真实请求已由页面路由覆盖 → 跳过避免假 404
            if m.endswith("/") and re.search(r"/(detail|multi|api/strategy/multi)/$", m):
                continue
            refs.add(m)
        bad, ok, post_only = [], 0, []
        for r in sorted(refs):
            c, _b = fetch_new(opener, r)
            if c in (200, 304):
                ok += 1
            elif c == 405:
                post_only.append(r)      # 路由已解析，只是 GET 打了 POST 端点
            else:
                bad.append((r, c))
        print(f"  页面 {page:<28} 200 / {len(body):,} 字节 → 站内引用 {len(refs)}，"
              f"可取 {ok}，POST端点(405) {len(post_only)}，失败 {len(bad)}")
        if post_only:
            print(f"      仅方法不匹配: {', '.join(post_only)}")
        for r, c in bad:
            oc, _ob = get(OLD_BASE, r)
            print(f"      [FAIL] {r:<46} 新={c}  原服务对照={oc}"
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
    elif mode == "dump-old":
        dump_routes("old")
        ok = True
    elif mode == "dump-new":
        dump_routes("new")
        ok = True
    elif mode == "http":
        ok = http_ab()
    elif mode == "assets":
        ok = assets_roundtrip()
    else:
        ok = ast_audit() and compare_routes()
    sys.exit(0 if ok else 1)
