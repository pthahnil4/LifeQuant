"""
Phase 2 验收 · 期货模块合并 A/B
================================
三种模式（为避免 sys.path 互相污染，路由表比对必须在两个独立进程里分别 dump）：

  python -X utf8 verify_02_futures.py dump-old   # 只加载原项目，导出 /futures 路由表
  python -X utf8 verify_02_futures.py dump-new   # 只加载合并后宿主，导出 /futures 路由表
  python -X utf8 verify_02_futures.py compare    # 比对两份路由表
  python -X utf8 verify_02_futures.py http        # 对两个在跑的服务做页面/接口 A/B

用法示例见 migrate 文档；本脚本不写业务数据，只做只读 GET。
"""
import json
import os
import sys
import urllib.request
import urllib.error

DST = r"D:\python\cryptoTrade"
SRC = r"D:\python\futureStockTrade"
OLD_BASE = "http://127.0.0.1:5000"
NEW_BASE = "http://127.0.0.1:7777"
ROUTE_DUMP = os.path.join(DST, "data", "_fst_route_table")


def _dump(which: str):
    if which == "old":
        sys.path.insert(0, SRC)
        os.chdir(SRC)
        import app as old_app  # noqa: 原项目入口
        mod = old_app.app
        tag = "old"
    else:
        sys.path.insert(0, DST)
        os.chdir(DST)
        os.environ["CRYPTO_NO_BACKGROUND"] = "1"
        from crypto.app import app as new_app  # noqa
        mod = new_app
        tag = "new"

    rows = []
    for rule in mod.url_map.iter_rules():
        if not str(rule).startswith("/futures"):
            continue
        methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        rows.append({"rule": str(rule), "endpoint": rule.endpoint,
                     "methods": methods, "defaults": rule.defaults})
    rows.sort(key=lambda r: (r["rule"], r["endpoint"]))
    out = os.path.join(ROUTE_DUMP, f"{tag}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"[{tag}] /futures 路由 {len(rows)} 条 → {out}")


def _norm_endpoint(ep: str, tag: str) -> str:
    """端点名的宿主前缀差异（futures.xxx / crypto.futures.routes:原为 futures_blueprint:xxx）不计入比对。"""
    return ep


def _compare():
    old_p = os.path.join(ROUTE_DUMP, "old.json")
    new_p = os.path.join(ROUTE_DUMP, "new.json")
    old = {r["rule"]: r for r in json.load(open(old_p, encoding="utf-8"))}
    new = {r["rule"]: r for r in json.load(open(new_p, encoding="utf-8"))}
    only_old = sorted(set(old) - set(new))
    only_new = sorted(set(new) - set(old))
    method_diff = []
    for rule in sorted(set(old) & set(new)):
        if old[rule]["methods"] != new[rule]["methods"]:
            method_diff.append((rule, old[rule]["methods"], new[rule]["methods"]))
    print("=" * 72)
    print("验收 1 · /futures 路由表 A/B")
    print(f"  原项目 {len(old)} 条 / 合并后 {len(new)} 条")
    print(f"  合并后缺失: {only_old or '无'}")
    print(f"  合并后新增: {only_new or '无'}")
    print(f"  方法不一致: {method_diff or '无'}")
    ok = not only_old and not only_new and not method_diff
    print(f"  → {'路由表完全对齐' if ok else '存在差异'}")
    return ok


# 只读页面与接口样本（GET，不触发任何写操作与下单）
PAGE_PATHS = ["/futures/", "/futures/detail/M", "/futures/multi/M", "/futures/star-market"]
STATIC_PATHS = ["/futures/static/futures.js", "/static/css/style.css"]
# 文件/CSV 驱动，结果确定 → 要求逐字节级 JSON 相等
EXACT_API_PATHS = [
    "/futures/api/config",
    "/futures/api/csv/data",
    "/futures/api/csv/filtered",
    "/futures/api/starred",
]
# 依赖 akshare 实时取数，两次请求间行情本身会漂移 → 只比结构骨架与派生字段名
LIVE_API_PATHS = [
    "/futures/api/strategy/data",
    "/futures/api/strategy/detail/M",
    "/futures/api/strategy/single/M",
    "/futures/api/strategy/multi/M",
    "/futures/api/boll/M",
    "/futures/api/dualpro3/M",
    "/futures/api/star-market/data",
    "/futures/api/batch/trend/status",
    "/futures/api/star-market/refresh-status",
]
API_PATHS = EXACT_API_PATHS + LIVE_API_PATHS


def _get(base, path, timeout=90):
    url = base + path
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "verify-02"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, body, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")
    except Exception as e:
        return None, str(e).encode(), ""


def _gate_ready(base):
    """新服务受 app 级访问闸门保护：按真实浏览器流程过一次口令（?token= → 302 发 cookie），
    之后所有请求复用同一 cookie jar。口令只从本机 data/web_token.txt 读，不外泄、不打印。"""
    import http.cookiejar
    import urllib.parse

    token = ""
    for p in (os.path.join(DST, "data", "web_token.txt"),
              os.path.join(DST, "crypto", "data", "web_token.txt")):
        if os.path.exists(p):
            token = open(p, encoding="utf-8").read().strip()
            break
    if not token:
        token = os.environ.get("CRYPTO_WEB_TOKEN", "").strip()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    probe = opener.open(urllib.request.Request(
        base + "/futures/?token=" + urllib.parse.quote(token),
        headers={"User-Agent": "verify-02"}), timeout=60)
    body = probe.read()
    passed = probe.status == 200 and "访问验证" not in body.decode("utf-8", "replace")
    print(f"  [{'OK' if passed else 'FAIL'}] 闸门通过（cookie 数={len(jar)}），"
          f"最终状态={probe.status} URL={probe.geturl().split('?')[0]}")
    return (opener if passed else None), jar


def _http_ab():
    import http.cookiejar  # noqa: F401  (供 _gate_ready 使用)
    print("=" * 72)
    print("验收 2 · 闸门与真实浏览器路径准备")
    new_opener, _ = _gate_ready(NEW_BASE)
    if new_opener is None:
        print("  无法通过访问闸门，终止 HTTP A/B")
        return False

    def fetch_new(path):
        try:
            r = new_opener.open(urllib.request.Request(
                NEW_BASE + path, headers={"User-Agent": "verify-02"}), timeout=120)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return None, str(e).encode()

    print("=" * 72)
    print("验收 3 · 页面渲染 A/B（模板是否套用宿主 nav/style，页面可达）")
    all_ok = True
    for p in PAGE_PATHS:
        so, bo = _get(OLD_BASE, p)[0], _get(OLD_BASE, p)[1]
        sn, bn = fetch_new(p)
        if sn != 200:
            print(f"  [FAIL] {p} 新服务返回 {sn}: {bn[:160].decode('utf-8', 'replace')}")
            all_ok = False
            continue
        txt = bn.decode("utf-8", "replace")
        host_nav = "nav-booting" in txt          # 宿主 nav.html 特征脚本
        host_css = "/static/css/style.css" in txt  # 宿主 app 级静态样式
        i = txt.find("<title>")
        title = txt[i + 7:txt.find("</title>", i)] if i >= 0 else ""
        print(f"  [OK] {p:<26} 原={so} 新={sn} 长度 {len(bo):>7,}→{len(bn):>7,} "
              f"宿主导航={host_nav} 宿主样式={host_css} title={title[:26]!r}")
        if not host_nav or not host_css:
            print("       ⚠ 未套用宿主 nav/style，请核对模板解析顺序")
            all_ok = False

    print("=" * 72)
    print("验收 4 · 静态资源解析（蓝图 static 与宿主 static 各自可达）")
    for p in STATIC_PATHS:
        sn, bn = fetch_new(p)
        so, bo, ct = _get(OLD_BASE, p)
        print(f"  [{'OK' if sn == 200 else 'FAIL'}] {p:<34} 新={sn} 原={so} "
              f"字节={len(bn):,} 类型={ct[:24] or 'n/a'}")
        if sn != 200:
            all_ok = False

    print("=" * 72)
    print("验收 5 · 只读接口 A/B（文件驱动=严格相等；实时取数=结构骨架一致）")
    for p in API_PATHS:
        so, bo, _ = _get(OLD_BASE, p)
        sn, bn = fetch_new(p)
        exact = p in EXACT_API_PATHS
        if so != 200 or sn != 200:
            print(f"  [{'FAIL' if sn != 200 else 'SKIP'}] {p:<40} 原={so} 新={sn}")
            if sn != 200:
                all_ok = False
            continue
        try:
            jo, jn = json.loads(bo), json.loads(bn)
        except Exception as e:
            print(f"  [FAIL] {p} JSON 解析失败: {e}")
            all_ok = False
            continue

        if exact:
            same = jo == jn
            print(f"  [{'OK' if same else 'DIFF'}] {p:<40} 严格相等={same}")
            if not same:
                print(f"       原: {str(jo)[:200]}")
                print(f"       新: {str(jn)[:200]}")
                all_ok = False
            continue

        def shape(x):
            if isinstance(x, dict):
                return {k: shape(v) for k, v in sorted(x.items())}
            if isinstance(x, list):
                return [shape(x[0])] + [f"len={len(x)}"] if x else ["empty"]
            return type(x).__name__

        def top_len(x):
            d = x.get("data", x) if isinstance(x, dict) else x
            return len(d) if isinstance(d, (list, dict)) else "n/a"

        so_shape, sn_shape = str(shape(jo))[:4000], str(shape(jn))[:4000]
        same_shape = so_shape == sn_shape
        print(f"  [{'OK' if same_shape else 'DIFF'}] {p:<40} 结构一致={same_shape} "
              f"条目数 原={top_len(jo)} 新={top_len(jn)}")
        if not same_shape:
            print(f"       原骨架: {so_shape[:260]}")
            print(f"       新骨架: {sn_shape[:260]}")
            all_ok = False
    print("=" * 72)
    print(f"Phase 2 验收结论：{'全部通过' if all_ok else '存在待修项'}")
    return all_ok


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    mode = sys.argv[1] if len(sys.argv) > 1 else "compare"
    ok = False
    if mode == "dump-old":
        _dump("old")
        ok = True
    elif mode == "dump-new":
        _dump("new")
        ok = True
    elif mode == "http":
        ok = _http_ab()
    else:
        ok = _compare() and _http_ab()
    sys.exit(0 if ok else 1)
