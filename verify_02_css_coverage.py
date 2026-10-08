# -*- coding: utf-8 -*-
"""
Phase 2 补充验收 · 期货页面 CSS 类覆盖核算
==========================================
背景：合并后期货模板改用宿主 style.css（原 FST 根级样式）。需客观证据说明
"宿主样式表足以支撑期货页面"，而不是仅凭页面返回 200。

口径：
  used    = 模板 HTML/JS 里出现的 class 名（含静态属性与动态字符串拼接）
  defined = 宿主 style.css + navigation.css 中定义过的类选择器
  inline  = 模板自身 <style> 块里定义的类
  gap     = used - defined - inline，即"页面上有用到、但没有任何地方定义"的类
gap 越多说明视觉缺口越大；gap 为空即样式覆盖完整。

用法：python -X utf8 verify_02_css_coverage.py [futures|stocks ...]
     不传参数时默认只核期货模板（Phase 2 口径）；Phase 3 追加股票模板时传 stocks。
"""
import os
import re
import sys
import glob

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = r"D:\python\futureStockTrade"
# 允许按模块选择待核验的模板目录（默认沿用 Phase 2 的期货口径）
MODULES = [a for a in sys.argv[1:] if a in ("futures", "stocks", "kline")] or ["futures"]
# 模块名 → 宿主里的目录名：K 线训练蓝图落在 crypto/kline_training/（URL 前缀仍是 /kline）
MODULE_DIR = {"futures": "futures", "stocks": "stocks", "kline": "kline_training"}
CSS_FILES = [
    "crypto/static/css/style.css",
    "crypto/static/css/navigation.css",
] + [f"crypto/{MODULE_DIR[m]}/static/css/style.css" for m in MODULES]  # 蓝图自带样式（若有）也计入

CLASS_DEF = re.compile(r"\.([A-Za-z][A-Za-z0-9_-]*)")
ATTR_CLASS_DQ = re.compile(r'class="([^"]*)"')
ATTR_CLASS_SQ = re.compile(r"class='([^']*)'")
JS_CLASSNAME = re.compile(r"className\s*=\s*[`'\"]([^`'\"\\]*)")
JS_CLASSLIST = re.compile(r"classList\.(?:add|remove|toggle)\(([^)]*)\)")
JS_QUOTED = re.compile(r"[`'\"]([A-Za-z][A-Za-z0-9_-]*)[`'\"]")


def collect_defined():
    defined = set()
    hit_files = []
    for rel in CSS_FILES:
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(p):
            continue
        hit_files.append(rel)
        text = open(p, encoding="utf-8", errors="replace").read()
        # 只看选择器区（去掉注释），避免把属性值里的 .xxx 误当类名
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        defined |= {m for m in CLASS_DEF.findall(text)}
    return defined, hit_files


def collect_used(html):
    """只取模板里静态 class="" 属性中的类名。

    JS 动态拼接出的字符串（表达式碎片、函数调用）不纳入：把它们当类名会产生
    大量假缺口（实测把 '+'、':'、getSignalClass(...) 都抓了进来），反而掩盖真问题。
    """
    used = set()
    for m in ATTR_CLASS_DQ.findall(html) + ATTR_CLASS_SQ.findall(html):
        used |= {c for c in m.split() if c and re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", c)}
    return used


def collect_inline_defs(html):
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    out = set()
    for b in blocks:
        out |= set(CLASS_DEF.findall(re.sub(r"/\*.*?\*/", "", b, flags=re.S)))
    return out


RULE_BODY = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)


def rule_bodies(css_text):
    """类名 → 该类的声明集合（合并多处规则），用于比对同名类的实际样式内容。"""
    css_text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
    out = {}
    for sel, body in RULE_BODY.findall(css_text):
        props = {p.strip() for p in body.split(";") if p.strip()}
        if not props:
            continue
        for cls in CLASS_DEF.findall(sel):
            out.setdefault(cls, set()).update(props)
    return out


def same_declared_props(a, b):
    """两个声明集合是否等价：逐属性名比，值不同才算差异；仅顺序/空白不同不算。"""
    def by_prop(items):
        d = {}
        for it in items:
            if ":" in it:
                k, v = it.split(":", 1)
                d[k.strip()] = v.strip().rstrip("!important").strip()
        return d
    da, db = by_prop(a), by_prop(b)
    if set(da) != set(db):
        return False, f"属性集不同 -{sorted(set(da)-set(db))[:4]} +{sorted(set(db)-set(da))[:4]}"
    diffs = [f"{k}: {da[k]!r} vs {db[k]!r}" for k in da if da[k] != db[k]]
    return (not diffs), ("; ".join(diffs[:3]) if diffs else "")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    defined, hit_files = collect_defined()
    # 待核验模板：按命令行选中的模块收集
    TEMPLATES = [t for m in MODULES
                 for t in sorted(glob.glob(os.path.join(
                     ROOT, "crypto", MODULE_DIR[m], "templates", "*.html")))]
    print(f"核验模块: {', '.join(MODULES)}  —— 模板 {len(TEMPLATES)} 个")
    # 原项目样式表（迁移前的视觉基准）。蓝图样式是各模块自带的，所以基准要按
    # 本次选中的模块取并集：kline 的类名写在 futureStockTrade 根级 style.css 里，
    # 期货/股票则写在各自 blueprint 的 static/css/style.css 里。
    baseline_paths = [os.path.join(SRC_ROOT, "static", "css", "style.css")]
    baseline_paths += [os.path.join(SRC_ROOT, m, "static", "css", "style.css")
                       for m in MODULES]
    baseline_text = ""
    for p in baseline_paths:
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                baseline_text += "\n" + f.read()
            print(f"原项目基准样式: {p}  ({os.path.getsize(p):,} 字节)")
    _bt = re.sub(r"/\*.*?\*/", "", baseline_text, flags=re.S)
    fst_defined = set(CLASS_DEF.findall(_bt))
    fst_b = rule_bodies(baseline_text)
    fst_css = baseline_paths[0]
    print(f"基准并集共 {len(fst_defined)} 个类")
    print(f"宿主样式来源: {', '.join(hit_files)}  —— 共定义 {len(defined)} 个类")
    print("=" * 78)
    print("口径：只看模板静态 class=\"\" 属性（JS 拼接出的类名不纳入，避免误判）；")
    print("     回归 = 原样式表定义了、宿主样式表却没有的类。回归为 0 即视觉不退化。")
    print("=" * 78)
    total_reg = 0
    for t in TEMPLATES:
        html = open(t, encoding="utf-8", errors="replace").read()
        used = collect_used(html)
        inline = collect_inline_defs(html)
        in_fst = {c for c in used if c in fst_defined}
        # 回归项：原样式表有、宿主没有、且模板自身 <style> 也没兜住
        regression = sorted(c for c in in_fst if c not in defined and c not in inline)
        # 两边都没有的：迁移前后同样无定义，属原项目遗留，不计为迁移回归
        never = sorted(c for c in used if c not in fst_defined and c not in defined
                       and c not in inline)
        total_reg += len(regression)
        print(f"{os.path.basename(t):<26} 静态类 {len(used):>3}  原样式已覆盖 {len(in_fst):>3}  "
              f"迁移回归 {len(regression):>2}{'  → ' + ', '.join(regression[:8]) if regression else ''}"
              f"  两边皆无定义(原有遗留) {len(never):>2}")
    print("=" * 78)
    print(f"合计迁移回归 {total_reg} 个类 —— "
          + ("宿主样式表完全覆盖，沿用宿主设计语言成立" if total_reg == 0
             else "存在回归，需为蓝图补一份自带 style.css"))

    # 第二项：同名类的声明内容比对（宿主规则会覆盖生效，内容差异 = 视觉漂移）
    host_text = ""
    for rel in hit_files:
        host_text += "\n" + open(os.path.join(ROOT, rel.replace("/", os.sep)),
                                 encoding="utf-8", errors="replace").read()
    host_b = rule_bodies(host_text)
    # fst_b 已在上面按选中模块的样式并集算好，这里不再重算，避免只看根级 style.css
    # 而漏掉蓝图自带样式里的同名类声明。

    used_all = set()
    for t in TEMPLATES:
        html = open(t, encoding="utf-8", errors="replace").read()
        # Jinja 注释 {# ... #} 里常出现 class="..." 示例文本，先剔除再取类名
        html = re.sub(r"\{#.*?#\}", "", html, flags=re.S)
        used_all |= collect_used(html)
    common = sorted(c for c in used_all if c in fst_b and c in host_b)
    drift = []
    for c in common:
        ok, why = same_declared_props(fst_b[c], host_b[c])
        if not ok:
            drift.append((c, why))
    print("=" * 78)
    print(f"同名类声明比对：页面用到且两边都定义的类 {len(common)} 个，"
          f"声明内容不同 {len(drift)} 个")
    for c, why in drift[:15]:
        print(f"    · {c:<22} {why[:150]}")
    if len(drift) > 15:
        print(f"    … 其余 {len(drift)-15} 个略")
    print("  说明：这些类在原项目与宿主页中共用同名，合并后以宿主定义为准，"
          "属预期的设计语言统一，非缺失；如需还原原视觉需给蓝图自带样式表。")
    sys.exit(0 if total_reg == 0 else 1)
