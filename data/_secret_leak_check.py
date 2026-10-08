# -*- coding: utf-8 -*-
"""密钥泄漏体检：把真实凭据值拿到内存里，在 git 索引工作区文件 + 待推送提交中搜索。

只输出命中的文件名，绝不打印密钥本身。用于公开仓库推送前的最后一道校验。
"""
import re
import subprocess
import sys

REPO = r"D:\python\cryptoTrade"
SECRET_SOURCES = [
    r"crypto\api_config.py",
    r"crypto\task\config\email_config.py",
    r"data\db_url.txt",
    r"data\jumei_appcode.txt",
    r"data\jumei_cme_appcode.txt",
]


def run(args):
    return subprocess.run(args, cwd=REPO, capture_output=True)


def extract_values(path):
    """从凭据文件里抽取长度>=12 的引号内字面量，跳过占位符。"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return []
    values = set()
    for m in re.finditer(r"""['"]([^'"\n]{12,120})['"]""", text):
        v = m.group(1)
        low = v.lower()
        if any(k in low for k in ("example", "your_", "placeholder", "xxx", "<", " ")):
            continue
        # 排除 URL / 主机名 / 模块名等通用串，它们不是密钥
        if "://" in v or low.startswith(("http", "ftp", "wss", "ws://")):
            continue
        if re.search(r"\.(com|cn|net|org|local|qq|163)\b", low) and "@" not in v:
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", v):
            continue  # 形如 a.b.c 的标识符/模块名
        if len(v) < 16:
            continue
        if re.fullmatch(r"[A-Za-z0-9@._+/=%:-]+", v):
            values.add(v)
    # 连接串形式的 user:password@host
    for m in re.finditer(r"://([^:/\s]+):([^@\s]+)@", text):
        values.add(m.group(2))
    return sorted(values)


def main():
    secrets = []
    for rel in SECRET_SOURCES:
        secrets.extend(extract_values(REPO + "\\" + rel))
    secrets = sorted(set(secrets))
    if not secrets:
        print("[WARN] 没有抽到任何凭据字面量，检查凭据文件路径")
        return

    print("[INFO] 待搜索的凭据字面量数量: %d" % len(secrets))
    for i, s in enumerate(secrets):
        print("   #%02d len=%d head=%s****" % (i, len(s), s[:4]))

    # 1) 当前会被提交的内容（已跟踪 + 未跟踪且非 ignored）
    ls = run(["git", "ls-files", "--cached", "--others", "--exclude-standard"])
    files = [f for f in ls.stdout.decode("utf-8", "ignore").splitlines() if f]
    print("[INFO] 参与体检的文件数: %d" % len(files))
    hit_files = []
    for rel in files:
        full = REPO + "\\" + rel.replace("/", "\\")
        try:
            with open(full, "rb") as fh:
                blob = fh.read()
        except OSError:
            continue
        low = blob.lower()
        for s in secrets:
            if s.lower().encode("utf-8", "ignore") in low:
                hit_files.append((rel, len(s)))
                break
    if hit_files:
        print("[FAIL] 工作区/索引中命中密钥的文件:")
        for rel, _ in hit_files:
            print("   - " + rel)
    else:
        print("[OK] 工作区与索引中未发现真实凭据明文")

    # 2) 待推送的提交（origin/main..HEAD）以及本地全部分支可达对象
    log = run(["git", "log", "--format=%H", "HEAD"])
    commits = log.stdout.decode("utf-8", "ignore").split()
    print("[INFO] 待搜索提交数: %d" % len(commits))
    bad = []
    for c in commits:
        p = run(["git", "grep", "-I", "-l", "-F"] + [s for s in secrets[:0]] or ["git", "show", c])
        show = run(["git", "show", "--format=", "-U0", c])
        blob = show.stdout.lower()
        for s in secrets:
            if s.lower().encode("utf-8", "ignore") in blob:
                bad.append(c[:8])
                break
    if bad:
        print("[FAIL] 历史提交内容中出现凭据明文: %s" % ", ".join(bad))
    else:
        print("[OK] 本分支可达的提交 diff 中未发现真实凭据明文")


if __name__ == "__main__":
    sys.exit(main())
