"""
Phase 1 验收脚本（A/B 双跑）
============================
验收目标两条：
  1. 差异审计：迁移后的文件与源文件逐行比对，确认"变更行"只允许出现在
     导入语句、sys.path 兜底块、来源说明注释三类，任何策略逻辑行都不许变。
  2. 数值 A/B：用同一份确定性合成 OHLC 数据，分别喂给
     原路径（futureStockTrade/strategies/pro3_strategy.py）与
     新路径（cryptoTrade/crypto/market_strategy/pro3_strategy.py），
     逐元素比对输出，确认行为完全一致（不只是"能 import"）。

用法：python -X utf8 verify_01_strategy_layer.py
"""
import os
import sys
import importlib.util

SRC = r"D:\python\futureStockTrade"
DST = r"D:\python\cryptoTrade"

PAIRS = [
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

def _strip_non_logic(tree):
    """归一化 AST：删除所有 import 语句与 docstring，只留逻辑结构。

    返回 (归一化树, 被删掉的 import 文本集合, docstring 文本列表)。
    """
    import ast as _ast
    removed_imports = []
    docstrings = []

    def pop_doc(body):
        if body and isinstance(body[0], _ast.Expr) and \
           isinstance(getattr(body[0], "value", None), _ast.Constant) and \
           isinstance(body[0].value.value, str):
            docstrings.append(body[0].value.value)
            body.pop(0)

    pop_doc(tree.body)
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)):
            pop_doc(node.body)

    # 用 Pass 替换所有 import 语句（含函数体内的惰性导入）
    class _DropImports(_ast.NodeTransformer):
        def visit_Import(self, node):
            removed_imports.append(_ast.unparse(node))
            new = _ast.Pass()
            return _ast.copy_location(new, node)

        def visit_ImportFrom(self, node):
            mod = node.module or ""
            lvl = "." * node.level
            names = ", ".join(
                (a.name + (" as " + a.asname if a.asname else "")) for a in node.names)
            removed_imports.append(f"{lvl}{mod} :: {names}")
            new = _ast.Pass()
            return _ast.copy_location(new, node)

    normalized = _DropImports().visit(tree)
    _ast.fix_missing_locations(normalized)

    # 第二步：剥离"import 路径引导"代码（sys.path 拼接、_project_root 推导、
    # 以及因删除兜底导入而变空的 try/except ImportError 壳）。
    # 迁移把这类兜底整体去掉了，它不属于策略逻辑，故两侧都归一化掉。
    BOOTSTRAP_TOKENS = ("sys.path", "_project_root", "_STRATEGY_DIR", "_PROJECT_ROOT")

    def _is_bootstrap(node):
        if isinstance(node, (_ast.Assign, _ast.If, _ast.Expr)):
            try:
                src = _ast.unparse(node)
            except Exception:
                return False
            return any(t in src for t in BOOTSTRAP_TOKENS)
        return False

    def _normalize(node):
        """自底向上清理：去 Pass → 去兜底语句 → 空 try 塌缩为 Pass。"""
        for field in ("body", "orelse", "finalbody"):
            children = getattr(node, field, None)
            if isinstance(children, list):
                kept = []
                for st in children:
                    _normalize(st)
                    if isinstance(st, _ast.Pass):
                        continue
                    if _is_bootstrap(st):
                        continue
                    kept.append(st)
                setattr(node, field, kept)
        for handler in getattr(node, "handlers", []) or []:
            _normalize(handler)
        for extra in ("target", "iter"):
            pass
        if isinstance(node, _ast.Try):
            empty = (not node.body and not node.orelse and not node.finalbody and
                     all(not h.body for h in node.handlers))
            if empty:
                node._collapse_to_pass = True
        return node

    _normalize(normalized)

    def _prune_pass(node):
        """塌缩标记生效后，再清一遍残留 Pass（父级先于子级访问）。"""
        for field in ("body", "orelse", "finalbody"):
            children = getattr(node, field, None)
            if not isinstance(children, list):
                continue
            kept = []
            for st in children:
                if isinstance(st, _ast.Try) and getattr(st, "_collapse_to_pass", False):
                    continue
                _prune_pass(st)
                kept.append(st)
            setattr(node, field, kept)
        return node

    _prune_pass(normalized)
    _ast.fix_missing_locations(normalized)
    return normalized, removed_imports, docstrings


def audit_diff():
    """AST 级逻辑比对：import 与 docstring 之外的语法树必须完全一致。"""
    import ast
    print("=" * 72)
    print("验收 1 · AST 逻辑等价审计（剥离 import/docstring 后语法树必须一致）")
    bad_files = 0
    for rel_src, rel_dst in PAIRS:
        src_p = os.path.join(SRC, rel_src.replace("/", os.sep))
        dst_p = os.path.join(DST, rel_dst.replace("/", os.sep))
        t_src = ast.parse(open(src_p, encoding="utf-8").read())
        t_dst = ast.parse(open(dst_p, encoding="utf-8").read())
        n_src, imp_src, doc_src = _strip_non_logic(t_src)
        n_dst, imp_dst, doc_dst = _strip_non_logic(t_dst)
        same_logic = ast.dump(n_src) == ast.dump(n_dst)
        added = sorted(set(imp_dst) - set(imp_src))
        removed = sorted(set(imp_src) - set(imp_dst))
        doc_delta = "（docstring 有措辞更新）" if doc_src != doc_dst else ""
        mark = "OK " if same_logic else "!!!"
        if not same_logic:
            bad_files += 1
        print(f"  [{mark}] {rel_dst:<58} 逻辑等价={same_logic} "
              f"导入改写 {len(added)} 增/{len(removed)} 删{doc_delta}")
        if not same_logic:
            # 定位差异节点，便于人工排查
            a, b = ast.dump(n_src).split("), "), ast.dump(n_dst).split("), ")
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    print(f"        首个差异点 #{i}:\n          源: {x[:150]}\n          新: {y[:150]}")
                    break
        for r in removed:
            print(f"        - 移除导入: {r[:100]}")
        for ad in added:
            print(f"        + 新增导入: {ad[:100]}")
    print(f"  → 逻辑不等价文件数: {bad_files}  "
          f"({'合格：策略逻辑零改动' if bad_files == 0 else '不合格：存在逻辑改动'})")
    return bad_files == 0


def synthetic_df(n=400, seed=42):
    """确定性合成 OHLC：正弦趋势 + 伪随机噪声，不依赖外部随机源。"""
    import math
    import pandas as pd
    closes = []
    v = 100.0
    for i in range(n):
        v *= 1 + 0.004 * math.sin(i / 17.0) + 0.0015 * math.sin(i / 3.0) \
            + 0.0008 * math.cos(i / 7.0)
        closes.append(round(v, 4))
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        h = max(o, c) * 1.004
        l = min(o, c) * 0.996
        rows.append({"date": i, "open": o, "high": round(h, 4),
                     "low": round(l, 4), "close": c, "volume": 1000 + i})
    return pd.DataFrame(rows)


def numeric_ab():
    print("=" * 72)
    print("验收 2 · 数值 A/B（同一份数据，原路径 vs 新路径逐元素比对）")
    import numpy as np
    import pandas as pd

    # 原版本：SRC 入 path 后按包名 strategies 导入（宿主无同名包，不会互相遮蔽）
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    old_ps = importlib.import_module("strategies.pro3_strategy")
    new_ps = importlib.import_module("crypto.market_strategy.pro3_strategy")

    df = synthetic_df()
    cases = [
        ("期货版 long_only=False use_sar=True", dict(long_only=False, use_sar=True)),
        ("股票版 long_only=True  use_sar=False", dict(long_only=True, use_sar=False)),
        ("仅多+无SAR+默认参数", dict(long_only=True)),
    ]
    all_ok = True
    for label, kwargs in cases:
        ro = old_ps.run_pro3_strategy(df.copy(), **kwargs)
        rn = new_ps.run_pro3_strategy(df.copy(), **kwargs)
        ko, kn = set(ro.keys()), set(rn.keys())
        same_keys = ko == kn
        diffs = []
        for k in sorted(ko & kn):
            vo, vn = ro[k], rn[k]
            if isinstance(vo, (pd.Series, np.ndarray)):
                so, sn = pd.Series(vo), pd.Series(vn)
                if len(so) != len(sn) or not so.equals(sn):
                    try:
                        if not np.allclose(so.astype(float), sn.astype(float), rtol=0, atol=0,
                                           equal_nan=True):
                            diffs.append(k)
                    except Exception:
                        diffs.append(k)
            elif isinstance(vo, pd.DataFrame):
                if not vo.equals(vn):
                    diffs.append(k)
            elif vo != vn:
                diffs.append(k)
        status = "PASS" if same_keys and not diffs else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"  [{status}] {label:<34} 字段数={len(ko)} 一致={same_keys} "
              f"不一致字段={diffs if diffs else '无'}")
        if diffs:
            for k in diffs[:6]:
                vo, vn = ro[k], rn[k]
                print(f"       {k}: 原={str(vo)[:60]} 新={str(vn)[:60]}")
    print(f"  → 数值 A/B: {'完全一致' if all_ok else '存在差异'}")
    return all_ok


def host_still_imports():
    print("=" * 72)
    print("验收 3 · 宿主应用未被污染（crypto.app 仍可导入）")
    try:
        importlib.import_module("crypto.app")
        print("  [PASS] import crypto.app 成功")
        return True
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(DST)
    sys.path.insert(0, DST)
    r1 = audit_diff()
    r2 = numeric_ab()
    r3 = host_still_imports()
    print("=" * 72)
    print(f"Phase 1 验收结论：差异审计={'通过' if r1 else '未通过'} | "
          f"数值A/B={'通过' if r2 else '未通过'} | 宿主未污染={'通过' if r3 else '未通过'}")
    sys.exit(0 if (r1 and r2 and r3) else 1)
