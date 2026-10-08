#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运维工具箱采集（P4 · ⑧⑨⑩）：全部**只读盘点**，绝不触碰交易链路。

三个页面共用本模块：
- ⑧ 冒烟运行器：解析 crypto/SMOKE_TESTS.md 索引 + 盘点磁盘上的 _smoke_*.py，展示分级、
  覆盖、落盘状态；**交易系统的冒烟一律不代跑**（多含写库/发信/调真实 OKX），唯一可一键
  运行的是运维站自身的自检冒烟（白名单 SELF_SMOKE_WHITELIST，不 import crypto）。
- ⑨ 诊断脚本：盘点 crypto/task/_diag_*.py，用 ast 取模块 docstring 作说明，列出即止。
- ⑩ 配置总览：读 config.json（非金属）+ 用 ast 字面量解析 api_config.py（脱敏，绝不 import
  /绝不回显密钥明文）+ 汇总访问闸门与环境开关（敏感项只报"是否设置"）。

红线：不 import crypto（连 api_config 也只 ast 解析文本）；不 exec；不 POST；子进程只跑白名单
自检脚本且无 shell。任何解析失败都优雅降级、绝不抛出拖垮页面。
"""
import ast
import os
import re
import subprocess
import sys
from datetime import datetime

from .. import config as C
from . import _http, _io


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _rel(path):
    """相对项目根的路径（用正斜杠，前端展示统一）。"""
    try:
        return os.path.relpath(path, C.PROJECT_ROOT).replace(os.sep, '/')
    except ValueError:
        return path


def _stat(path):
    """文件落盘信息；不存在则 exists=False（不抛）。"""
    try:
        st = os.stat(path)
        mt = datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M')
        return {'exists': True, 'size': st.st_size, 'mtime': mt}
    except OSError:
        return {'exists': False, 'size': 0, 'mtime': ''}


def _scan_scripts(prefix):
    """在约定目录里找 <prefix>*.py（不递归全仓），返回 {relpath: abspath}。"""
    found = {}
    for d in C.SCRIPT_SCAN_DIRS:
        base = os.path.join(C.PROJECT_ROOT, d) if d else C.PROJECT_ROOT
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for n in names:
            if n.startswith(prefix) and n.endswith('.py'):
                ap = os.path.join(base, n)
                if os.path.isfile(ap):
                    found[_rel(ap)] = ap
    return found


# ==========================================================================
# ⑧ 冒烟运行器（只读索引 + 磁盘核对 + 自检白名单）
# ==========================================================================

_TIER_MAP = {  # 索引小节标题里的 emoji → (标签, 展示分级, 识别关键词)
    '🔒': ('纯离线', 'success', '离线'),
    '🛡️': ('写库+还原', 'info', '还原'),
    '⚠️': ('写库+租约', 'warning', '租约'),
    '🌐': ('连真实 API', 'danger', '真实'),
}
_PATH_RE = re.compile(r'`([^`]+?\.py)`')


def _parse_index():
    """解析 SMOKE_TESTS.md 的分组表，返回按出现顺序的 [{emoji,label,level,rows:[...]}]。

    仅当小节标题的 emoji 与其**关键词**同时命中才算分级，避免把 "🔒 脚本的四类假失败"
    这类同 emoji 的说明小节误当成分级表。
    """
    lines = _io.read_text_lines(C.SMOKE_INDEX_MD, max_lines=None,
                                max_bytes=2 * 1024 * 1024)
    tiers, cur = [], None
    for ln in lines:
        if ln.startswith('## '):
            cur = None
            for key, (label, level, kw) in _TIER_MAP.items():
                if key in ln[:6] and kw in ln:
                    cur = {'emoji': key, 'label': label, 'level': level, 'rows': []}
                    tiers.append(cur)
                    break
            continue
        if not ln.startswith('|') or cur is None:
            continue
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        if not cells or cells[0] in ('脚本', '') or set(cells[0]) <= set('-: '):
            continue
        m = _PATH_RE.search(cells[0])
        if not m:
            continue
        rel = m.group(1).strip()
        cur['rows'].append({
            'relpath': rel,
            'name': os.path.basename(rel),
            'coverage': cells[1] if len(cells) > 1 else '',
            'rerun': cells[2] if len(cells) > 2 else '',
            'note': cells[3] if len(cells) > 3 else '',
        })
    return tiers, bool(lines)


def _build_smoke():
    tiers, index_present = _parse_index()
    indexed_rel = set()
    disk = _scan_scripts('_smoke_')

    for t in tiers:
        for r in t['rows']:
            ap = os.path.normpath(os.path.join(C.PROJECT_ROOT, r['relpath']))
            r.update(_stat(ap))
            indexed_rel.add(r['relpath'])

    # 磁盘上存在、但索引里没登记（漂移，便于发现"新加了冒烟没写进 SMOKE_TESTS.md"）
    # 运维站自身自检冒烟（_smoke_opscenter_*）本就不属于交易索引，单列在下方，不算漂移。
    unindexed = []
    for rel, ap in sorted(disk.items()):
        if rel in indexed_rel:
            continue
        if os.path.basename(rel).startswith('_smoke_opscenter'):
            continue
        item = {'relpath': rel, 'name': os.path.basename(rel)}
        item.update(_stat(ap))
        unindexed.append(item)

    # 运维站自身可一键运行的自检冒烟
    self_smokes = []
    for key, fname in sorted(C.SELF_SMOKE_WHITELIST.items()):
        ap = os.path.join(C.PROJECT_ROOT, fname)
        item = {'key': key, 'relpath': fname, 'name': fname,
                'label': '运维站 P%s 渲染自检' % key.upper()[1:]}
        item.update(_stat(ap))
        self_smokes.append(item)

    indexed_count = sum(len(t['rows']) for t in tiers)
    runnable = sum(1 for s in self_smokes if s['exists'])
    return {
        'checked_at': _now(),
        'index_present': index_present,
        'index_path': _rel(C.SMOKE_INDEX_MD),
        'tiers': tiers,
        'unindexed': unindexed,
        'self_smokes': self_smokes,
        'totals': {
            'indexed': indexed_count,
            'on_disk': len(disk),
            'unindexed': len(unindexed),
            'runnable': runnable,
        },
    }


@_io.cached('smoke', C.TOOLS_TTL_SEC)
def get_smoke():
    return _build_smoke()


def run_self_smoke(which):
    """一键运行白名单内的运维站自检冒烟（子进程、无 shell、硬超时）。

    只接受 C.SELF_SMOKE_WHITELIST 的键；返回 {ok, kind, returncode, tail, elapsed_ms, label}。
    自检脚本不 import crypto、只用 test_client，故无交易副作用。任何异常都兜住不抛。
    """
    fname = C.SELF_SMOKE_WHITELIST.get(str(which))
    if not fname:
        return {'ok': False, 'kind': 'denied', 'returncode': None,
                'tail': '该脚本不在可运行白名单内（仅运维站自身 P1/P2/P3 自检冒烟可运行）',
                'elapsed_ms': 0, 'label': str(which)}
    path = os.path.join(C.PROJECT_ROOT, fname)
    if not os.path.isfile(path):
        return {'ok': False, 'kind': 'error', 'returncode': None,
                'tail': '脚本不存在：%s' % fname, 'elapsed_ms': 0, 'label': fname}

    import time
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, path],                     # 固定 argv，绝不 shell=True
            cwd=C.PROJECT_ROOT, capture_output=True, text=True,
            timeout=C.SMOKE_RUN_TIMEOUT_SEC, encoding='utf-8', errors='replace',
        )
        elapsed = int((time.time() - t0) * 1000)
        blob = (proc.stdout or '') + ('\n' + proc.stderr if proc.stderr else '')
        tail = _tail_lines(blob, 40)
        ok = (proc.returncode == 0)
        return {'ok': ok, 'kind': 'ok' if ok else 'fail', 'returncode': proc.returncode,
                'tail': tail, 'elapsed_ms': elapsed, 'label': fname}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'kind': 'timeout', 'returncode': None,
                'tail': '运行超过 %ds 未返回，已放弃等待（自检脚本正常应在数秒内完成）'
                        % C.SMOKE_RUN_TIMEOUT_SEC,
                'elapsed_ms': int((time.time() - t0) * 1000), 'label': fname}
    except Exception as e:                              # 兜底，绝不让蓝图崩溃
        return {'ok': False, 'kind': 'error', 'returncode': None,
                'tail': '运行失败：%s' % e.__class__.__name__,
                'elapsed_ms': int((time.time() - t0) * 1000), 'label': fname}


# ==========================================================================
# ⑨ 诊断脚本清单（只列不跑）
# ==========================================================================

def _module_docstring(path, max_lines=60):
    """用 ast 取模块 docstring（不执行文件）；失败回落为首个三引号块内的文本行。"""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            src = f.read()
    except OSError:
        return '', 0
    total = src.count('\n') + 1
    try:
        doc = ast.get_docstring(ast.parse(src)) or ''
    except SyntaxError:
        doc = ''
    if not doc:  # 无 docstring 时抓开头的 # 注释块
        head = []
        for ln in src.splitlines()[:max_lines]:
            s = ln.strip()
            if s.startswith('#') and 'coding' not in s and 'python' not in s.lower():
                head.append(s.lstrip('# ').rstrip())
            elif head and not s:
                break
        doc = ' '.join(head)
    doc = re.sub(r'\s+', ' ', doc).strip()
    return doc[:240], total


def _diag_safety(text):
    low = text.lower()
    if any(k in low for k in ('okx', '真实', '交易所', 'get_order', 'get_account', 'get_instruments',
                              '余额', '挂单', 'margin', '可用')):
        return {'level': 'warning', 'label': '连真实 API/DB · 手动'}
    if any(k in text for k in ('只读', '纯读', '不写', '临时目录', '不 kill', '不改状态')):
        return {'level': 'info', 'label': '只读 · 手动'}
    return {'level': 'neutral', 'label': '只列清单 · 手动'}


def _build_diag():
    disk = _scan_scripts('_diag_')
    scripts = []
    for rel, ap in sorted(disk.items()):
        doc, total = _module_docstring(ap)
        item = {'relpath': rel, 'name': os.path.basename(ap), 'desc': doc,
                'lines': total, 'cmd': 'python %s' % rel}
        item.update(_stat(ap))
        item['safety'] = _diag_safety(doc)
        scripts.append(item)
    return {'checked_at': _now(), 'scripts': scripts,
            'count': len(scripts),
            'index_hint': _rel(os.path.join('crypto', 'SMOKE_TESTS.md'))}


@_io.cached('diag', C.TOOLS_TTL_SEC)
def get_diag():
    return _build_diag()


# ==========================================================================
# ⑩ 配置总览（脱敏）
# ==========================================================================

def _secret_state(val):
    if not val or str(val).strip() in C.OKX_PLACEHOLDER_VALUES:
        return {'configured': False, 'label': '未配置'}
    return {'configured': True, 'label': '已配置 · %d 位' % len(str(val))}


def _mask_key(val):
    v = str(val or '').strip()
    if not v or v in C.OKX_PLACEHOLDER_VALUES:
        return '未配置'
    if len(v) <= 8:
        return v[:2] + '***'
    return '%s***%s · %d' % (v[:4], v[-2:], len(v))


def _parse_api_config():
    """ast.literal_eval 解析 api_config.py 的 ACCOUNTS/DEFAULT_ACCOUNT（不 import、不 exec）。"""
    out = {'parse_ok': False, 'message': '', 'default': '', 'accounts': []}
    try:
        with open(C.API_CONFIG_PY, encoding='utf-8', errors='replace') as f:
            tree = ast.parse(f.read())
    except OSError:
        out['message'] = '配置文件不存在（跳过）'
        return out
    except SyntaxError:
        out['message'] = '配置文件语法异常（跳过，不影响其它页）'
        return out

    accounts, default = None, ''
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            try:
                if tgt.id == 'ACCOUNTS':
                    accounts = ast.literal_eval(node.value)
                elif tgt.id == 'DEFAULT_ACCOUNT':
                    default = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    if not isinstance(accounts, dict):
        out['message'] = '未能解析 ACCOUNTS（结构非字面量？）'
        return out

    parsed = []
    for key, acct in accounts.items():
        if not isinstance(acct, dict):
            continue
        flag = str(acct.get('flag', '')).strip()
        env_txt = '实盘' if flag == '0' else ('模拟盘' if flag == '1' else '未知')
        env_level = 'warning' if flag == '0' else ('success' if flag == '1' else 'neutral')
        parsed.append({
            'key': key,
            'name': str(acct.get('name') or key),
            'is_default': key == default,
            'env': env_txt, 'env_level': env_level, 'flag': flag,
            'api_mask': _mask_key(acct.get('api_key')),
            'api_configured': not (str(acct.get('api_key') or '').strip() in C.OKX_PLACEHOLDER_VALUES),
            'secret': _secret_state(acct.get('secret_key')),
            'passphrase': _secret_state(acct.get('passphrase')),
        })
    parsed.sort(key=lambda a: (not a['is_default'], a['key']))
    out.update({
        'parse_ok': True, 'message': '', 'default': str(default or ''),
        'accounts': parsed,
        'live_count': sum(1 for a in parsed if a['flag'] == '0'),
        'demo_count': sum(1 for a in parsed if a['flag'] == '1'),
        'unconfigured_count': sum(1 for a in parsed if not a['api_configured']),
    })
    return out


def _read_coins():
    out = {'present': False, 'all_coins': [], 'starred': [], 'floating': [],
           'default_selected': [], 'counts': {}}
    try:
        import json
        with open(C.CONFIG_JSON, encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out

    def _lst(k):
        v = data.get(k) or []
        return [str(x) for x in v] if isinstance(v, list) else []

    allc = _lst('all_coins')
    out.update({
        'present': True,
        'all_coins': allc, 'starred': _lst('starred_coins'),
        'floating': _lst('floating_coins'), 'default_selected': _lst('default_selected'),
    })
    out['counts'] = {
        'all': len(allc),
        'starred': len(_lst('starred_coins')),
        'floating': len(_lst('floating_coins')),
        'default': len(_lst('default_selected')),
    }
    return out


# 环境开关注册：敏感项只报"是否设置"，普通项可显值
_ENV_FLAGS = [
    ('CRYPTO_WEB_PORT', False, '交易系统 Web 监听端口（缺省 7777）'),
    ('CRYPTO_NO_BACKGROUND', False, '置 1 时不拉起后台线程（冒烟/调试常用）'),
    ('CRYPTO_PLAN_DATA_DIR', False, '外置数据目录（口令/状态文件落盘处）'),
    ('CRYPTO_LIFECYCLE_DIR', False, '生命周期台账目录'),
    ('CRYPTO_DB_WARM_TRACE', False, 'DB 预热失败是否打印堆栈'),
    ('OPSCENTER_TRADING_API', False, '运维站访问交易 Web 的地址'),
    ('OPSCENTER_API_TIMEOUT', False, '只读 GET 超时秒数'),
    ('OPSCENTER_FAKE', False, '演示占位开关'),
    ('CRYPTO_WEB_TOKEN', True, 'Web 访问口令（敏感·仅报是否设置）'),
    ('CRYPTO_DB_URL', True, '数据库连接串（敏感·仅报是否设置）'),
]


def _build_config():
    okx = _parse_api_config()
    token = _http.read_token()
    source = 'env' if os.environ.get(C.WEB_TOKEN_ENV) else ('file' if token else 'none')
    return {
        'checked_at': _now(),
        'okx': okx,
        'webgate': {
            'token_configured': bool(token),
            'source': source,
            'token_file': _rel(C.WEB_TOKEN_FILE),
        },
        'coins': _read_coins(),
        'paths': {
            'trading_api': C.TRADING_API_BASE,
            'log_dir': C.LOG_DIR,
            'data_dir': _DATA_DIR_disp(),
            'api_config': _rel(C.API_CONFIG_PY),
            'opscenter_port': os.environ.get('OPSCENTER_PORT') or '6002（默认）',
        },
        'env_flags': _env_flags(),
        'safety': '本页只显存在性与脱敏片段，绝不回显 api_key/secret/passphrase/token/DB 串明文；'
                  '配置经 ast 字面量解析读取，不 import、不执行任何 crypto 模块。',
    }


def _DATA_DIR_disp():
    return _rel(C._DATA_DIR) if C._DATA_DIR else '(未设)'


def _env_flags():
    rows = []
    for name, secret, meaning in _ENV_FLAGS:
        present = bool((os.environ.get(name) or '').strip())
        val = (os.environ.get(name) or '').strip()
        rows.append({
            'name': name, 'present': present, 'secret': secret,
            'value': ('••••（值不显）' if (secret and present) else (val or '—')),
            'meaning': meaning,
        })
    return rows


@_io.cached('config_overview', C.TOOLS_TTL_SEC)
def get_config():
    return _build_config()


# --------------------------------------------------------------------------
def _tail_lines(blob, n):
    lines = [ln for ln in (blob or '').splitlines()]
    return '\n'.join(lines[-n:])
