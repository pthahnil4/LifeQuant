# -*- coding: utf-8 -*-
"""
Phase 2 补充验收 · 页面资源真实请求回环
======================================
HTML 返回 200 只代表模板渲染成功，不代表页面里引用的 css/js/接口都取得到。
本脚本按浏览器行为把每个页面内出现的站内资源与接口全部实际请求一遍，
任何非 200/304 都视为迁移缺口（典型如蓝图 static 路径没接上）。

用法：python -X utf8 verify_02_assets.py
前置：原服务 5000 与合并后服务 7777 都在跑，7777 需要过访问闸门
     （口令只从本机 data/web_token.txt 读取，不打印、不落日志）。
"""
import os
import re
import sys
import json
import gzip
import urllib.parse
import urllib.request
import http.cookiejar

DST = r"D:\python\cryptoTrade"
NEW_BASE = os.environ.get("VERIFY_NEW_BASE", "http://127.0.0.1:7777")
OLD_BASE = "http://127.0.0.1:5000"
PAGES = ["/futures/", "/futures/detail/M", "/futures/multi/M", "/futures/star-market"]

# 站内引用：src="..." / href="..." / fetch('/...')
REF_ATTR = re.compile(r"""(?:src|href)\s*=\s*["']([^"'{}]+)["']""")
REF_FETCH = re.compile(r"""(?:fetch|open)\s*\(\s*[`'"](/[^`'"\s]*)[`'"]""")
SKIP_PREFIX = ("http://", "https://", "//", "data:", "mailto:", "#", "javascript:")


def gate_opener():
    token = ""
    for p in (os.path.join(DST, "data", "web_token.txt"),
              os.path.join(DST, "crypto", "data", "web_token.txt")):
        if os.path.exists(p):
            token = open(p, encoding="utf-8").read().strip()
            break
    token = token or os.environ.get("CRYPTO_WEB_TOKEN", "").strip()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(urllib.request.Request(
        NEW_BASE + "/futures/?token=" + urllib.parse.quote(token),
        headers={"User-Agent": "verify-02-assets"}), timeout=60).read()
    return opener


def get(opener, url):
    """返回 (状态码, 字节数, 内容类型, body)。opener 为空时走标准库 urlopen。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "verify-02-assets",
                                                   "Accept-Encoding": "identity"})
        if opener is not None:
            with opener.open(req, timeout=60) as r:
                body = r.read()
                return r.status, len(body), r.headers.get("Content-Type", ""), body
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, len(body), r.headers.get("Content-Type", ""), body
    except urllib.error.HTTPError as e:
        return e.code, 0, "", e.read()
    except Exception as e:
        return None, 0, "", str(e).encode()


def collect_refs(html):
    refs = set()
    for m in REF_ATTR.findall(html) + REF_FETCH.findall(html):
        m = m.strip()
        if not m.startswith("/") or m.startswith(SKIP_PREFIX):
            continue
        if "{" in m or "$" in m:      # 模板变量/JS 拼接的接口不参与静态探测
            continue
        # 以 '/' 结尾且形如 /detail/ /multi/ 的是 JS 里 `前缀 + symbol` 的片段，
        # 真实请求带 symbol 段（已由页面路由覆盖），静态探测会假 404 → 跳过
        if m.endswith("/") and re.search(r"/(detail|multi|api/strategy/multi)/$", m):
            continue
        refs.add(m)
    return refs


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 78)
    opener = gate_opener()
    total_bad = 0
    for page in PAGES:
        code, size, ctype, body = get(opener, NEW_BASE + page)
        if code != 200:
            print(f"[FAIL] 页面 {page} 返回 {code}")
            total_bad += 1
            continue
        html = body.decode("utf-8", "replace")
        refs = sorted(collect_refs(html))
        bad, ok, method_only = [], [], []
        for r in refs:
            c, n, ct, _b = get(opener, NEW_BASE + r)
            if c in (200, 304):
                ok.append((r, c, ct))
            elif c == 405:
                # 路由存在、只是本脚本用 GET 打了一个 POST 端点 → 视为链路已接上
                method_only.append(r)
            else:
                bad.append((r, c, ct))
        print(f"页面 {page:<26} 200 / {size:,} 字节 → 引用站内资源 {len(refs)} 个，"
              f"可取 {len(ok)}，POST端点(405) {len(method_only)}，失败 {len(bad)}")
        if method_only:
            print(f"    仅方法不匹配（路由已解析）: {', '.join(method_only)}")
        for r, c, ct in bad:
            print(f"    [FAIL] {r:<46} → {c} {ct[:30]}")
            total_bad += 1
        # 对照：同样资源在原服务是否也这样（区分"迁移引入"与"原本就有"）
        for r, c, _ in bad:
            oc, _, _, _ = get(None, OLD_BASE + r)
            print(f"    原服务对照 {r} → {oc}"
                  + ("（原服务同样失败 → 非迁移引入）" if oc != 200 else "（原服务正常 → 迁移缺口）"))
            if oc == 200:
                total_bad += 0
    print("=" * 78)
    print(f"资源回环结论：{'全部可取，无 404/500' if total_bad == 0 else f'{total_bad} 项待修'}")
    sys.exit(0 if total_bad == 0 else 1)
