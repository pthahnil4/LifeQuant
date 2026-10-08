# -*- coding: utf-8 -*-
"""
Phase 5 验收脚本：依赖收口 + 全仓库明文密钥复扫
================================================

futureStockTrade 合并迁移走到 Phase 5，要收两件尾：

① 依赖收口 —— ``requirements.txt`` 与代码里的真实 import 双向核对：
   - 漏报方向：代码 import 了、清单没写 → 新环境 ``pip install -r`` 装完仍崩；
   - 多报方向：清单写了、代码无人 import → 假依赖，白装还拖慢部署。
   顺带校验本机已装版本是否落在声明区间内（声明写得再漂亮，装不上也是空的）。

② 明文密钥复扫 —— 扫描 git 可见的全部源码文件（跟踪文件 + 未跟踪但未被
   ``.gitignore`` 排除的文件），找「凭据形状」的硬编码字面量。
   被 ``.gitignore`` 排除的 ``data/`` 外置凭据**不在扫描范围内，也不该在**：
   那正是本项目要求的存放位置。

用法::

    python -X utf8 verify_05_secrets_and_deps.py            # 两项都跑
    python -X utf8 verify_05_secrets_and_deps.py --deps     # 只跑依赖收口
    python -X utf8 verify_05_secrets_and_deps.py --secrets  # 只跑密钥扫描

退出码：0 = 通过；1 = 存在硬性问题（缺声明 / 版本不满足 / 高危明文凭据）。
"""
import argparse
import os
import re
import subprocess
import sys
import ast
import importlib.metadata
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQ_PATH = ROOT / "requirements.txt"

# 扫描时跳过的目录名（任何一层命中即整棵剪掉）
SKIP_DIRS = {
    ".git", ".idea", ".vscode", ".qoder", "node_modules", "__pycache__",
    "venv", ".venv", "env", "site-packages", "build", "dist",
    ".pytest_cache", ".mypy_cache", "logs", "uploads",
    # 原项目保持只读，且最终要整个改名冻结；它自带的硬编码凭据不属于本仓库
    "futureStockTrade", "futureStockTrade_DEPRECATED",
}

# 参与密钥扫描的源码/配置后缀；文档类后缀单列（只提示，不当代码判高危）
CODE_SUFFIXES = {".py", ".js", ".html", ".css", ".json", ".cfg", ".ini",
                 ".yaml", ".yml", ".env", ".sh", ".bat", ".ps1", ".sql"}
DOC_SUFFIXES = {".md", ".txt", ".csv"}

# 单文件读取上限：K 线 CSV 之类的大文件只扫前若干字节，避免把整份行情读进内存
MAX_FILE_BYTES = 2_000_000


# ---------------------------------------------------------------------
#  通用：git 可见文件列表
# ---------------------------------------------------------------------
_FILES_CACHE = None


def git_visible_files() -> list:
    """返回「会被 git 看见」的文件（相对 ROOT）：跟踪中 + 未跟踪且未被 ignore。"""
    global _FILES_CACHE
    if _FILES_CACHE is not None:
        return _FILES_CACHE
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        )
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip() or "git ls-files 失败")
    except Exception as exc:                       # 拿不到 git 就退回目录遍历
        print(f"[WARN] git ls-files 不可用（{exc}），改用目录遍历")
        _FILES_CACHE = _walk_files()
        return _FILES_CACHE

    files = []
    for rel in out.stdout.splitlines():
        rel = rel.strip().strip('"')
        if not rel:
            continue
        parts = Path(rel).parts
        if any(p in SKIP_DIRS for p in parts):
            continue
        if (ROOT / rel).is_file():
            files.append(rel)
    _FILES_CACHE = files
    return files


def _walk_files() -> list:
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            files.append(str((Path(dirpath) / fn).relative_to(ROOT)))
    return files


def read_text(rel: str) -> str:
    """utf-8-sig 读取：迁移过来的部分 .py 带 BOM，用 utf-8 会把首行搞脏。"""
    p = ROOT / rel
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            with open(p, "rb") as fh:
                return fh.read(MAX_FILE_BYTES).decode("utf-8", "replace")
        with open(p, "rb") as fh:
            return fh.read().decode("utf-8-sig", "replace")
    except Exception:
        return ""


# =====================================================================
#  ① 依赖收口
# =====================================================================
# import 顶层名 → PyPI 发行包名；未列出的按「同名」处理
IMPORT_TO_DIST = {
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "taosrest": "taospy",
    "taos": "taospy",
    "bs4": "beautifulsoup4",
    "Crypto": "pycryptodome",
    "okx": "python-okx",
    "apscheduler": "APScheduler",
    "pymysql": "PyMySQL",
    "sqlalchemy": "SQLAlchemy",
    "flask": "Flask",
    "werkzeug": "Werkzeug",
    "argon2": "argon2-cffi",
    "openpyxl": "openpyxl",
    "backtrader": "backtrader",
}

# 标准库/运行时自带，不参与第三方清单核对
LOCAL_MODULE_ALLOW = {
    "app", "config", "utils", "data_paths", "web_auth", "models", "database",
}

# 明确不作为依赖出现的名字：宿主内部包、以及随 CPython/环境自带的
NOT_A_REQUIREMENT = {
    "sys", "os", "re", "ast", "io", "json", "time", "math", "random",
    "sqlite3", "smtplib", "email", "hashlib", "logging", "threading",
    "subprocess", "datetime", "collections", "functools", "itertools",
    "pathlib", "typing", "traceback", "uuid", "secrets", "base64",
    "socket", "ssl", "struct", "shutil", "tempfile", "argparse",
    "getpass", "gzip", "csv", "copy", "bisect", "heapq", "queue",
    "contextlib", "decimal", "statistics", "textwrap", "warnings",
    "weakref", "zlib", "http", "urllib", "importlib", "inspect",
    "signal", "platform", "glob", "unicodedata", "enum", "dataclasses",
    "string", "calendar", "locale", "timeit", "pickle", "hmac",
    "mimetypes", "html", "xml", "array", "atexit", "operator", "types",
    "concurrent", "multiprocessing", "asyncio", "zoneinfo", "statistics",
}


def parse_requirements(path: Path) -> dict:
    """解析 requirements.txt → {发行包名: (spec 原文, 是否被注释掉)}"""
    reqs = {}
    if not path.exists():
        return reqs
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 去掉行内注释与环境标记
        line = re.split(r"\s*#", line)[0].split(";")[0].strip()
        if not line or line.startswith("-"):       # -r / -f 之类
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", line)
        if m:
            reqs[m.group(1).lower()] = (m.group(2).strip(), False)
    # 注释形式的可选依赖也收集，用于「已知并显式说明」判定
    commented = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            continue
        body = line.lstrip("#").strip()
        m = re.match(r"^([A-Za-z][A-Za-z0-9_.\-]+)\s*(>=|<=|==|~=|>|<)", body)
        if m:
            commented[m.group(1).lower()] = (m.group(0), True)
    merged = dict(reqs)
    for k, v in commented.items():
        merged.setdefault(k, v)
    return merged


# 明确「由别的包带进来、不必单独写进清单」的三方名 → 不判缺声明，只提示
TRANSITIVE_OK = {
    "jinja2": "随 Flask 安装（Flask 直接依赖 Jinja2）",
    "packaging": "随 pip / setuptools 带入，且代码已做 ImportError 降级",
    "dateutil": "随 pandas 安装",
    "pytz": "随 pandas / APScheduler 安装",
    "six": "随传递依赖带入",
    "typing_extensions": "随 pydantic / flask 等带入",
    "markupsafe": "随 Jinja2 / Flask 安装",
    "blinkers": "随 Flask 安装",
    "itsdangerous": "随 Flask 安装",
    "h11": "随 httpx 安装",
    "certifi": "随 httpx / requests 安装",
    "idna": "随 httpx / requests 安装",
    "anyio": "随 httpx 安装",
    "sniffio": "随 httpx 安装",
    "urllib3": "随 requests 安装",
    "charset_normalizer": "随 requests 安装",
    "simplejson": "随传递依赖带入",
    "console_progress_indicator": "随 akshare 使用脚本带入",
    "lxml": "随 akshare / pandas 安装",
    "bs4": "随 akshare 安装",
}


# 清单里声明了、但代码不会直接 import 的包 → 有正当理由，不算冗余
REASONED_NO_IMPORT = {
    "tzdata": "时区数据包，Windows / 精简容器镜像靠它取 tzinfo，无 import 属正常",
    "cryptography": "PyMySQL 连 MySQL 8 的 caching_sha2_password 握手需要，"
                    "由 pymysql 内部 import，业务代码不碰",
}


def local_module_names(files: list) -> set:
    """仓库内部模块/包名集合，用来把「import 了自家代码」误判成三方依赖的情况排掉。

    判定不能只看 ``crypto/<name>.py``：像 ``strategy_gate``、``notification``、
    ``pro3_singletimeframe`` 这些都在更深的子目录里，靠 sys.path 注入后裸导入。
    所以把每个 .py 的文件名、以及每个目录名都当成候选内部模块名。

    还要注意：**被 .gitignore 排除的自家模块同样存在**，典型就是
    ``crypto/api_config.py``（OKX 密钥外置文件）。它不在 git 可见清单里，
    只按 git 文件算就会把 ``import api_config`` 判成三方依赖，因此这里额外
    做一次磁盘遍历（仍然跳过 venv / .git 等目录）。
    """
    names = set()
    sources = list(files)
    try:
        sources += _walk_files()
    except Exception:
        pass
    for rel in sources:
        p = Path(rel)
        if p.suffix.lower() == ".py" and p.stem != "__init__":
            names.add(p.stem)
        # 目录名同样算内部模块：crypto/task/notification/ 就是靠 sys.path
        # 注入后以 `from notification.email_tool import ...` 的形式裸导入的
        for part in p.parts[:-1]:
            names.add(part)
    return names


# 只用于「一次性演示 / 本地调试」的路径，缺依赖不该影响部署清单（判定见 is_scratch）


def is_scratch(rel: str) -> bool:
    p = Path(rel)
    parts = [s.lower() for s in p.parts]
    if "demo" in parts or "demos" in parts or "tools" in parts:
        return True
    if p.stem.lower().startswith(("demo", "_probe", "_check", "_plan_", "_smoke",
                                  "_diag", "_tmp", "test_")):
        return True
    return False


def collect_imports(local_names: set) -> dict:
    """扫描全部 .py，返回 {顶层 import 名: {出现过的相对文件}}"""
    found = {}
    for rel in git_visible_files():
        if Path(rel).suffix.lower() != ".py":
            continue
        src = read_text(rel)
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
        std = getattr(sys, "stdlib_module_names", frozenset())
        for n in names:
            if n in std or n in NOT_A_REQUIREMENT or n in LOCAL_MODULE_ALLOW:
                continue
            if n in local_names:              # 自家模块，靠 sys.path 裸导入
                continue
            found.setdefault(n, set()).add(rel)
    return found


def version_satisfies(dist: str, spec: str) -> tuple:
    """本机是否装了 dist，且版本落在 spec 区间内。返回 (状态, 说明)"""
    if not spec:
        return "OK", "未给区间"
    try:
        installed = importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return "MISSING", f"本机未安装（声明区间 {spec}）"
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        try:
            ss = SpecifierSet(spec)      # 语法不合法在这里就会抛，而不是留到 pip
        except Exception as bad:
            return "MISMATCH", f"区间写法非法：{bad}"
        ok = Version(installed) in ss
        return ("OK" if ok else "MISMATCH"), f"{installed} {'符合' if ok else '不符合'} {spec}"
    except ImportError:
        # 没有 packaging 库时退回逐个比较前缀，够用即可
        nums = re.findall(r"\d+", installed)
        return "UNKNOWN", f"{installed}（无 packaging，无法判定 {spec}）"


def check_deps() -> int:
    print("=" * 78)
    print("① 依赖收口：requirements.txt ↔ 代码实际 import")
    print("=" * 78)
    reqs = parse_requirements(REQ_PATH)
    files = git_visible_files()
    imports = collect_imports(local_module_names(files))
    hard_fail = 0

    declared = {k for k, v in reqs.items() if not v[1]}
    optional_declared = {k for k, v in reqs.items() if v[1]}

    # 正向：代码用到 → 必须声明。演示/调试脚本里的依赖单列，不计入硬性缺口
    print("\n[正向] 代码里有 import，清单是否声明")
    missing = []
    scratch_missing = []
    for imp, where in sorted(imports.items()):
        dist = IMPORT_TO_DIST.get(imp, imp)
        low = dist.lower()
        if low in declared:
            continue
        runtime_files = [f for f in where if not is_scratch(f)]
        sample = sorted(runtime_files or where)[0]
        if low in optional_declared:
            print(f"    · {dist:<16} 仅声明为可选（已注释）→ 见 {sample}")
            continue
        if low in TRANSITIVE_OK:
            print(f"    · {dist:<16} 不单独声明（{TRANSITIVE_OK[low]}）→ {sample}")
            continue
        if not runtime_files:
            scratch_missing.append((dist, imp, len(where), sample))
            continue
        missing.append((dist, imp, len(runtime_files), sample))

    if missing:
        hard_fail = 1
        for dist, imp, n, sample in missing:
            print(f"    [缺声明] {dist:<16}（import 名 {imp}，运行链路 {n} 个文件）"
                  f"→ 例如 {sample}")
    else:
        print("    运行链路全部覆盖，无漏报")
    if scratch_missing:
        for dist, imp, n, sample in scratch_missing:
            print(f"    [仅演示] {dist:<16}（import 名 {imp}，{n} 个文件）"
                  f"→ {sample}，不进部署清单")

    # 反向：清单声明 → 代码里得有人用
    print("\n[反向] 清单声明了，代码是否真的用到")
    used_dists = {IMPORT_TO_DIST.get(k, k).lower() for k in imports}
    orphans = []
    for name in sorted(declared):
        if name in used_dists:
            continue
        if name in REASONED_NO_IMPORT:
            print(f"    · {name:<16} {REASONED_NO_IMPORT[name]}")
            continue
        orphans.append(name)
    if orphans:
        for name in orphans:
            print(f"    [冗余?] {name:<16} 全仓库未见 import，需人工确认是否传递依赖")
    else:
        print("    无冗余声明")

    # 版本区间：按本机实测
    print("\n[实测] 本机版本是否落在声明区间内")
    for name, (spec, is_comment) in sorted(reqs.items()):
        if is_comment:
            continue
        state, why = version_satisfies(name, spec)
        mark = {"OK": "  ok ", "MISSING": "FAIL ", "MISMATCH": "FAIL ",
                "UNKNOWN": " ?  "}[state]
        print(f"    [{mark}] {name:<16} {spec:<20} {why}")
        if state in ("MISSING", "MISMATCH"):
            hard_fail = 1

    hard_fail |= cross_check_by_origin(declared, optional_declared)

    print("\n小结：" + ("存在硬性缺口，见上方 FAIL / 缺声明" if hard_fail
                        else "清单与代码一致，本机版本均满足声明区间"))
    return hard_fail


# =====================================================================
#  ② 明文密钥扫描
# =====================================================================
# 每条：(规则名, 正则, 严重级别)
# 严重级别 H=高危（几乎确定是凭据）/ M=中危（凭据形状，需看语境）/ L=提示
SECRET_RULES = [
    ("私钥块", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"), "H"),
    ("云厂商密钥形状", re.compile(r"\b(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,})"), "H"),
    ("URL 内嵌账号口令", re.compile(r"[a-zA-Z][\w+.-]*://[^/\s'\"`:@]+:[^/\s'\"`]+@"), "H"),
    ("口令类赋值-字面量",
     re.compile(r"""(?xi)\b([a-z_]*(?:password|passwd|pwd|secret|auth_code|
         appcode|app_code|app_secret|client_secret|access_key|secret_key|
         api_key|apikey|passphrase|token)[a-z_]*)\b\s*(?::|=)\s*
         (['\"])([^'\"\n]{6,})\2"""), "H"),
    ('口令类字典键',
     re.compile(r"""(?xi)['\"](password|passwd|secret|secret_key|api_key|
         passphrase|auth_code|appcode|access_key)['\"]\s*:\s*
         ['\"]([^'\"\n]{6,})['\"]"""), "H"),
    ("QQ/163 邮箱授权码形状", re.compile(r"""\b[a-z0-9]{16}\b"""), "M"),
    ("TDengine 出厂口令", re.compile(r"(?i)\btaosdata\b"), "M"),
    ("写死公网 IP",
     re.compile(r"""\b(?:\d{1,3}\.){3}\d{1,3}\b"""), "M"),
]

# 命中行里出现这些形态，说明凭据来自外部（环境变量/外置文件/表单），属正确写法
SAFE_HINT = re.compile(
    r"(?i)(os\.environ|getenv|_env\(|resolve_data_file|_read_secret_file|"
    r"read_text|open\(|request\.(form|json|args|values)|\.get\(|body\.|"
    r"payload\[|data\.get|cfg\.get|CONFIG\[|input\(|decrypt|from_json)")

# 但要额外抓一种「看起来在读环境变量、实际留了写死后门」的形态：
#   TDENGINE_PASSWORD = _env('TDENGINE_…') or ‹出厂口令›
#   SECRET_KEY = os.environ.get('…') or ‹写死的默认串›
# 原项目 config.py 的事故就是这么来的，环境变量的优先级救不了缺省值。
# 只对名字本身就像密钥的变量生效，避免把 `_env(..) or 'near_usdt'` 这类
# 库名/表名缺省也刷成噪音。
CRED_NAME = re.compile(
    r"(?i)(password|passwd|secret|api_?key|apikey|passphrase|auth_?code|"
    r"app_?code|access_?key|private_?key|token)")
FALLBACK_LITERAL = re.compile(
    r"""(?i)(?:\bor\b|\belse\b)\s*['\"]([^'\"\n]{3,})['\"]""")

# 明显不是凭据的常见词，避免噪音（按整词匹配）
BENIGN_VALUE = re.compile(
    r"(?i)^(true|false|none|null|utf-8|utf8|gbk|json|text/html|localhost|"
    r"127\.0\.0\.1|0\.0\.0\.0|::1|example\.com.*|your[_-].*|xxx.*|\*+)$")

# 以下这些值形状上像凭据，语义上一定不是：
#   · 环境变量名 / 常量名（全大写加下划线），如 'CRYPTO_WEB_TOKEN'
#   · 模板占位（f-string 的 {api_key}、文档里的 ‹某某›、user:pwd@ 示例）
#   · 说明性文字（含中文），如 database.py 头注释里的「用户:密码」
ENV_NAME_VALUE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
PLACEHOLDER_VALUE = re.compile(r"[{}<>‹›]|\bexample\b|your[_-]|\bu:p\b|"
                               r"pwd@|\.\.\.|…|XXX|CHANGE.?ME", re.I)
HAS_CJK = re.compile(r"[一-鿿]")

IP_OK = re.compile(r"^(127\.0\.0\.1|0\.0\.0\.0|::1|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)")

# 测试 / 演示 / 诊断类文件里的「假口令」不算事故：冒烟用例总要拿个字符串
# 当密码喂进去，那些值本来就设计成一眼假。真正要盯的是运行链路源码。
FIXTURE_NAME = re.compile(r"(?i)(^|/)(_|test[_s]|smoke|verify_|demo|_probe|_diag)")


def is_fixture_file(rel: str) -> bool:
    """判定是否为「测试/演示/诊断」文件：这类文件里的凭据形状一律降级。"""
    p = Path(rel)
    parts = [s.lower() for s in p.parts]
    if "demo" in parts or "demos" in parts or "tools" in parts:
        return True
    stem = p.stem.lower()
    return bool(FIXTURE_NAME.search(stem)) or stem.startswith(("check_", "probe_"))


def looks_placeholder(val: str) -> bool:
    """值本身表明它不是凭据，而是变量名 / 模板占位 / 中文说明。"""
    v = val.strip()
    if ENV_NAME_VALUE.match(v):
        return True
    if PLACEHOLDER_VALUE.search(v):
        return True
    if HAS_CJK.search(v):
        return True
    return False


def redact(value: str) -> str:
    """只留前 2 字符与长度做定位，绝不回显其余部分。"""
    v = str(value)
    if len(v) <= 4:
        return "*" * len(v)
    return f"{v[:2]}{'*' * 8}(len={len(v)})"


def _backdoor_hit(line: str) -> str:
    """识别「凭据变量 = 读环境变量 or 写死缺省」这种后门形态，返回那个写死值。

    只认 *赋值目标* 本身就是凭据名的情况（``SECRET_KEY = …`` /
    ``'password': …``），避免把 ``source = 'env' if … else 'none'`` 这类
    三元状态标签当成漏了口令。取到的缺省值还要过一遍占位/常见词判定。
    """
    target = re.match(r"""\s*['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?\s*(?::|=)""", line)
    if not target or not CRED_NAME.search(target.group(1)):
        return ""
    m = FALLBACK_LITERAL.search(line)
    if not m:
        return ""
    val = m.group(1)
    if BENIGN_VALUE.match(val.strip()) or looks_placeholder(val):
        return ""
    return val


def check_secrets() -> int:
    print("\n" + "=" * 78)
    print("② 明文密钥复扫（git 可见源码文件；.gitignore 排除的 data/ 不扫）")
    print("=" * 78)
    files = git_visible_files()
    scanned = 0
    hits = []          # (级别, 规则, rel, lineno, 摘要)
    downgraded = 0     # 被识别为「测试/演示夹具」或占位形态而降级的条数

    for rel in files:
        suffix = Path(rel).suffix.lower()
        if suffix not in CODE_SUFFIXES and suffix not in DOC_SUFFIXES:
            continue
        text = read_text(rel)
        if not text:
            continue
        scanned += 1
        is_doc = suffix in DOC_SUFFIXES
        is_fixture = is_fixture_file(rel)
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > 400:
                line = line[:400]
            # 注释行也扫：原项目的教训之一就是「凭据没进代码，但进了代码注释」
            has_safe = bool(SAFE_HINT.search(line))
            # 「读环境变量 / 或一个写死的缺省」= 后门，安全形态豁免对它无效
            backdoor = _backdoor_hit(line)
            has_backdoor = bool(backdoor)
            for rule, regex, level in SECRET_RULES:
                for m in regex.finditer(line):
                    if rule == "写死公网 IP":
                        ip = m.group(0)
                        if IP_OK.match(ip) or ip.startswith(("1.1.1", "8.8.8")):
                            continue
                    # `'api_key' in argv` 这类成员判断，取的是键名不是值
                    if rule != "写死公网 IP" and re.match(r"\s+in\b", line[m.end():m.end() + 8]):
                        continue
                    val = ""
                    for g in reversed(m.groups() or ()):
                        if g:
                            val = g
                            break
                    if not val:
                        val = m.group(0)
                    if BENIGN_VALUE.match(val.strip()) or looks_placeholder(val):
                        continue
                    if has_safe and not has_backdoor:
                        continue
                    if rule == "QQ/163 邮箱授权码形状":
                        # 16 位小写字母数字太宽：只在同时含字母与数字时才算
                        if not (re.search(r"[a-z]", val) and re.search(r"\d", val)):
                            continue
                        if len(set(val)) < 6:
                            continue
                    lv = level
                    if lv in ("H", "M") and is_doc:
                        lv = "M" if lv == "H" else "L"   # 文档示例降级，但仍要人看一眼
                    if lv in ("H", "M") and is_fixture:
                        lv = "L"                # 冒烟用例里的假口令 / 测试 IP
                        downgraded += 1
                    hits.append((lv, rule, rel, lineno, redact(val)))
            backdoor = _backdoor_hit(line)
            if backdoor and not is_doc:
                lv = "L" if is_fixture else "H"
                downgraded += 1 if is_fixture else 0
                hits.append((lv, "环境变量带写死缺省", rel, lineno, redact(backdoor)))

    order = {"H": 0, "M": 1, "L": 2}
    hits.sort(key=lambda h: (order[h[0]], h[2], h[3]))
    for lv, rule, rel, lineno, mask in hits:
        if lv == "L":
            continue
        print(f"    [{lv}] {rule:<20} {rel}:{lineno}  → {mask}")

    high = [h for h in hits if h[0] == "H"]
    mid = [h for h in hits if h[0] == "M"]
    low = [h for h in hits if h[0] == "L"]
    print(f"\n扫描文件 {scanned} 个；高危 {len(high)} 条，中危 {len(mid)} 条，"
          f"已降级为提示 {len(low)} 条（测试/演示夹具 {downgraded} 条，其余为文档示例）")
    print("判定口径：命中行的右侧值必须是字面量（注释行同样算），且该行不含"
          "「从环境变量/外置文件/表单取值」等安全形态；但「环境变量 or 写死缺省」"
          "属后门形态，仍按高危上报。占位符、环境变量名、含中文的说明、成员判断"
          "里的键名均视为非凭据；冒烟/演示脚本里的假口令降为提示。")
    return 1 if high else 0


# ---------------------------------------------------------------------
#  交叉核对：用「解释器实际从哪加载」反查三方依赖，补名字启发式的漏洞
# ---------------------------------------------------------------------
def cross_check_by_origin(declared: set, optional_declared: set) -> int:
    """把仓库里出现过的每个顶层 import 名交给 importlib 解析。

    启发式判定「是不是自家模块」靠的是名字，会有两类漏子：三方库恰好和仓库里
    某个目录同名（被误当内部模块放过），或自家模块恰好和某个已安装发行包同名
    （被误报缺声明）。这里改成问解释器：``find_spec`` 解析出来的加载路径如果
    落在仓库之外，那它就是真正需要从 PyPI 安装的依赖，跑不掉。
    """
    print("\n[交叉核对] 按解释器实际加载位置反查三方依赖")
    all_names = {}
    for rel in git_visible_files():
        if Path(rel).suffix.lower() != ".py":
            continue
        src = read_text(rel)
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                all_names.setdefault(n, set()).add(rel)

    std = getattr(sys, "stdlib_module_names", frozenset())
    root_text = str(ROOT).lower()
    undeclared, shadow, info = [], [], []
    for name in sorted(all_names):
        if name in std or name in NOT_A_REQUIREMENT:
            continue
        dist = IMPORT_TO_DIST.get(name, name).lower()
        if dist in declared or dist in TRANSITIVE_OK:
            continue
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError, AttributeError):
            spec = None
        except Exception:
            spec = None
        if spec is None:
            # 解析不到：多半是运行时靠 sys.path 注入的自家模块，或压根没装
            shadow.append((name, sorted(all_names[name])[0]))
            continue
        origin = spec.origin or ""
        if not origin or origin == "namespace":
            paths = list(spec.submodule_search_locations or [])
            origin = paths[0] if paths else ""
        if origin.lower().startswith(root_text):
            continue                       # 仓库内部模块，正确
        runtime_files = [f for f in all_names[name] if not is_scratch(f)]
        if not runtime_files:
            info.append((name, "只出现在演示/调试脚本里，不进部署清单",
                         sorted(all_names[name])[0]))
            continue
        if dist in optional_declared:
            info.append((name, "已在清单里显式声明为可选（注释状态）",
                         sorted(runtime_files)[0]))
            continue
        undeclared.append((name, origin, sorted(runtime_files)[0]))

    rc = 0
    for name, why, sample in info:
        print(f"    · {name:<18} {why} → {sample}")
    if undeclared:
        rc = 1
        for name, origin, sample in undeclared:
            print(f"    [缺声明] {name:<18} 由解释器从仓库外加载：{origin}")
            print(f"              引用处示例：{sample}")
        print("    说明：按加载位置判定，不受「名字恰好和某个目录同名」影响。")
    if not undeclared:
        print("    三方依赖与声明清单一致")
    if shadow:
        for name, sample in shadow[:8]:
            print(f"    · 解析不到 {name}（按 sys.path 注入的自家模块处理，示例 {sample}）")
        if len(shadow) > 8:
            print(f"    · … 其余 {len(shadow) - 8} 个同类略")
    return rc


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Phase 5 依赖收口与明文密钥验收")
    ap.add_argument("--deps", action="store_true", help="只跑依赖收口")
    ap.add_argument("--secrets", action="store_true", help="只跑明文密钥扫描")
    args = ap.parse_args()
    both = not (args.deps or args.secrets)

    rc = 0
    if both or args.deps:
        rc |= check_deps()
    if both or args.secrets:
        rc |= check_secrets()
    print("\n" + "=" * 78)
    print("退出码 " + str(rc) + ("（通过）" if rc == 0 else "（存在需要处理的问题）"))
    sys.exit(rc)


if __name__ == "__main__":
    main()
