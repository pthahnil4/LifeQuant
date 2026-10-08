# -*- coding: utf-8 -*-
"""OKX 交易操作能力清单 —— 文档阅读页蓝图

职责
----
把 `doc/OKX交易操作能力清单.md` 渲染成一个可检索、可跳转、带图示的网页，
并把 `data/_okx_list_tools.json`（okx list-tools 的官方 schema 快照）做成
可筛选的工具矩阵面板，保证"网页内容 == 证据文件"。

【唯一内容源】文档本身。本模块只做"渲染 + 结构增强 + 图示注入"，
不复制表格数据，避免文档与页面两处维护互相打架。

【渲染失败要能用】markdown 库缺失 / 文档缺失时不抛异常打断进程，
降级为纯文本 + 顶部告警条，页面照常可访问。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from flask import Blueprint, jsonify, render_template

from .data_paths import resolve_data_dir
from .capability_diagrams import SCENARIOS, is_write_command, scenario_html

logger = logging.getLogger(__name__)

capability_bp = Blueprint('capability_bp', __name__)

# crypto/ 的上一级即项目根
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOC_PATH = os.path.join(_ROOT, 'doc', 'OKX交易操作能力清单.md')
_TOOLS_JSON = os.path.join(resolve_data_dir(), '_okx_list_tools.json')

try:
    import markdown as _md
except ImportError:  # 部署环境未装 Markdown 包时降级，不影响其余路由
    _md = None

_DOC_EXT = ['tables', 'fenced_code', 'sane_lists']

# 按 (mtime, size) 缓存渲染结果；文档不改就不重复解析
_doc_cache: dict = {}
_tools_cache: dict = {}


# =============================================================================
# 文档渲染
# =============================================================================

def _strip_tags(s: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', s)).strip()


def _rel(path: str) -> str:
    """相对项目根的路径，仅用于页面展示。跨盘符时 relpath 会抛 ValueError，
    而这个函数挂在页面渲染主路径上，绝不能因为"显示个路径"就把整页搞挂。"""
    try:
        return os.path.relpath(path, _ROOT).replace('\\', '/')
    except (ValueError, OSError):
        return str(path).replace('\\', '/')


# 代码块内容已被 HTML 转义，判语言前先把实体占位掉
_ENT_RE = re.compile(r'&[#a-zA-Z0-9]{1,8};')


def _classify_code(raw: str) -> str:
    """围栏代码块大多没有语言声明，按内容猜一个，用于配色与高亮规则。"""
    body = _ENT_RE.sub(' ', raw).lstrip()
    if body[:1] in ('{', '['):
        return 'json'
    if re.match(r'^(?:(?:okx|pip|npm|npx|python|powershell|pwsh|cd|ls|cat|curl|git|bash|sh)\b|[#$>])', body):
        return 'bash'
    if re.match(r'^(import |from |def |class |print\()', body):
        return 'python'
    if '+--' in raw or '|===' in raw:
        return 'ascii'
    return 'text'


_KW_PY = r'\b(def|return|import|from|if|else|elif|for|while|try|except|with|as|class|None|True|False|and|or|not|in|lambda)\b'


def _highlight(text: str, lang: str) -> str:
    """极简高亮。入参已是 HTML 转义后的实体串，只能在安全边界内包 span，不得引入新标签。"""
    if lang == 'json':
        out = re.sub(r'(&quot;.*?&quot;)(\s*:)', r'<span class="ck-key">\1</span>\2', text)
        out = re.sub(r'(:\s)(&quot;.*?&quot;)', r'\1<span class="ck-str">\2</span>', out)
        out = re.sub(r'\b(true|false|null)\b', r'<span class="ck-kw">\1</span>', out)
        out = re.sub(r'(?<![\w"&:>+-])(\d+(?:\.\d+)?)(?![\w"])', r'<span class="ck-num">\1</span>', out)
        return out
    if lang in ('bash', 'python'):
        kw = _KW_PY if lang == 'python' else None
        lines = []
        for ln in text.split('\n'):
            if ln.strip().startswith('#'):
                lines.append('<span class="ck-cmt">%s</span>' % ln)
                continue
            s = re.sub(r'(&quot;[^&]*?&quot;)', r'<span class="ck-str">\1</span>', ln)
            if lang == 'bash':
                s = re.sub(r'(\s--?[A-Za-z][\w-]*)', r'<span class="ck-flag">\1</span>', s)
                s = re.sub(r'^(&gt;\s*)?\b(okx|npm|pip|python|curl)\b',
                           r'\1<span class="ck-cmd">\2</span>', s)
            else:
                s = re.sub(kw, r'<span class="ck-kw">\1</span>', s)
            lines.append(s)
        return '\n'.join(lines)
    return text


def _post_process(html: str):
    """markdown → 阅读页 HTML：分类代码块 / 包表格 / 引述改提示条 / 标题加锚点 + 注入图示 / 按 h2 分节。"""
    # 表格单元格里转义的 \| 解析后仍带反斜杠，视觉上多余，统一还原
    html = html.replace('\\|', '|')

    # ---- 代码块：加语言类 + 复制按钮
    def _code(m):
        attrs, body = m.group(1) or '', m.group(2)
        lang = 'json' if 'language-json' in attrs else _classify_code(body)
        return ('<div class="cap-code" data-lang="{lang}">'
                '<button class="cap-code-copy" type="button" title="复制这段命令">复制</button>'
                '<pre class="cap-pre cap-pre-{lang}"><code>{body}</code></pre>'
                '<span class="cap-lang-tag">{lang}</span></div>').format(
            lang=lang, body=_highlight(body, lang))

    html = re.sub(r'<pre><code([^>]*)>(.*?)</code></pre>', _code, html, flags=re.S)

    # ---- 表格：外层滚动容器（窄屏不撑破版面）
    html = html.replace('<table>', '<div class="cap-scroll"><table class="cap-tbl">')
    html = html.replace('</table>', '</table></div>')

    # ---- 引用块 → 语义提示条（按起始 emoji 分派配色）
    def _quote(m):
        inner = m.group(1)
        head = _strip_tags(inner)[:8]
        if '🔴' in head or '⚠' in head:
            cls = 'warn'
        elif '💡' in head:
            cls = 'tip'
        elif '📌' in head:
            cls = 'rule'
        else:
            cls = 'note'
        return '<div class="cap-quote cap-quote-%s">%s</div>' % (cls, inner)

    html = re.sub(r'<blockquote>(.*?)</blockquote>', _quote, html, flags=re.S)

    # ---- 标题：分配锚点 id、收集目录、按标题文本命中即注入图示（一图或图集）
    toc = []
    diag_hits = set()
    out, pos = [], 0
    for i, m in enumerate(re.finditer(r'<h([1-4])>(.*?)</h\1>', html, flags=re.S), 1):
        level, raw = int(m.group(1)), m.group(2)
        sid = 'cap-h%d' % i
        label = _strip_tags(raw)
        if level >= 2:
            toc.append({'level': level, 'id': sid, 'text': label})
        out.append(html[pos:m.start()])
        out.append('<h%d class="cap-h cap-h%d" id="%s">%s</h%d>' % (level, level, sid, raw, level))
        # 同一标题可挂多张图（如 §2 下四种策略委托时序图并排成图集）；rank 控制并排次序
        group = [k for k, sc in enumerate(SCENARIOS) if k not in diag_hits and sc['match'] in label]
        group.sort(key=lambda k: (SCENARIOS[k].get('rank', 0), k))
        if group:
            out.append('<div class="cap-scenario-row">' +
                       ''.join(scenario_html(SCENARIOS[k]) for k in group) + '</div>')
            diag_hits.update(group)
        pos = m.end()
    out.append(html[pos:])
    html = ''.join(out)

    # ---- 按 h2 切分为可折叠小节（搜索过滤与折叠都依赖这个结构）
    sec_re = r'<h2 class="cap-h cap-h2" id="(cap-h\d+)">(.*?)</h2>'
    parts = re.split(r'(?=<h2 class="cap-h cap-h2" id="cap-h)', html)
    secs, idx = [], 0
    for p in parts:
        if not p:
            continue
        if p.startswith('<h2 class="cap-h cap-h2"'):
            idx += 1
            hm = re.match(sec_re, p, flags=re.S)
            secs.append('<section class="cap-sec" id="sec-%d" data-title="%s" data-hid="%s">'
                        '<button class="cap-sec-toggle" type="button" aria-label="折叠本节">▾</button>'
                        '%s</section>' % (
                            idx, _strip_tags(hm.group(2)) if hm else '', hm.group(1) if hm else '', p))
        else:
            secs.append('<section class="cap-sec cap-sec-lead" id="sec-0" data-title="概述">' + p + '</section>')
    return ''.join(secs), toc, sorted(diag_hits), len(secs)


def _render_doc():
    """带缓存的文档渲染。返回 dict 供模板直接使用。"""
    try:
        st = os.stat(_DOC_PATH)
        key = (st.st_mtime_ns, st.st_size)
    except OSError as e:
        return {'ok': False, 'error': '文档读取失败：%s' % e, 'path': _DOC_PATH}
    if _doc_cache.get('key') == key:
        return _doc_cache['val']

    try:
        with open(_DOC_PATH, encoding='utf-8') as f:
            src = f.read()
    except OSError as e:
        return {'ok': False, 'error': '文档读取失败：%s' % e, 'path': _DOC_PATH}

    meta = {
        'path': _rel(_DOC_PATH),
        'size': st.st_size,
        'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime)),
        'lines': src.count('\n') + 1,
    }
    if _md is None:
        val = {'ok': False, 'degrade': True, 'error': '服务端未安装 Markdown 渲染库（pip install Markdown），已降级为纯文本。',
               'text': src, 'meta': meta, 'toc': []}
    else:
        try:
            body = _md.markdown(src, extensions=_DOC_EXT)
            body, toc, diag, n_sec = _post_process(body)
        except Exception as exc:  # 渲染异常不应拖垮整页
            logger.exception('[capability] 文档渲染异常')
            val = {'ok': False, 'degrade': True, 'error': '渲染异常，已降级为纯文本：%s' % exc,
                   'text': src, 'meta': meta, 'toc': []}
        else:
            val = {'ok': True, 'body': body, 'toc': toc, 'meta': meta,
                   'diagrams': len(diag), 'tables': body.count('class="cap-tbl"'),
                   # 必须带 class=" 精确匹配：光数 'cap-code' 会把 cap-code-copy / cap-code-xxx 一起数进去（实测翻倍）
                   'codes': body.count('class="cap-code"'),
                   # 小节数直接数 section 标签，含「概述」lead 节 —— 与页面上可折叠的卡片数一致
                   'sections': n_sec}
    _doc_cache.clear()
    _doc_cache.update({'key': key, 'val': val})
    return val


# =============================================================================
# 证据文件 → 工具矩阵
# =============================================================================

def _load_tools():
    try:
        st = os.stat(_TOOLS_JSON)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {'ok': False, 'error': '证据文件缺失：%s' % _rel(_TOOLS_JSON),
                'modules': []}
    if _tools_cache.get('key') == key:
        return _tools_cache['val']

    try:
        with open(_TOOLS_JSON, encoding='utf-8-sig') as f:
            raw = json.load(f)
    except Exception as exc:
        logger.warning('[capability] 工具清单解析失败: %s', exc)
        return {'ok': False, 'error': '证据文件解析失败：%s' % exc, 'modules': []}

    mods, tw, ttot, tlocal = [], 0, 0, 0
    tool_names, empty = set(), []
    for m in raw.get('modules') or []:
        cmds = m.get('commands') or []
        if not cmds:
            empty.append({'name': m.get('name') or '-',
                          'desc': (m.get('description') or '').strip()[:60]})
            continue
        rows, local = [], 0
        for c in cmds:
            path = (c.get('path') or '').strip()
            shown = re.sub(r'\[<instId>\]\s*', '', path).replace('okx ', '', 1).strip()
            tool = c.get('toolName') or ''
            req = [p.get('name') for p in (c.get('parameters') or []) if p.get('required')]
            if not tool:
                local += 1
            else:
                tool_names.add(tool)
            rows.append({'cli': shown, 'tool': tool or '-',
                         'kind': 'W' if is_write_command(path) else 'R',
                         'desc': (c.get('description') or '').strip()[:110],
                         'req': req[:14]})
        w = sum(1 for r in rows if r['kind'] == 'W')
        tw += w
        ttot += len(rows)
        tlocal += local
        mods.append({'name': m.get('name') or '-', 'total': len(rows), 'write': w,
                     'read': len(rows) - w, 'local': local,
                     'tools': len({r['tool'] for r in rows if r['tool'] != '-'}),
                     'wpct': round(w * 100.0 / len(rows), 1),
                     'rpct': round((len(rows) - w) * 100.0 / len(rows), 1),
                     'commands': rows})

    declared = raw.get('totalTools')
    mapped = ttot - tlocal           # 有 MCP 工具承载的命令数，官方 totalTools 就是这个口径
    uniq = len(tool_names)           # 去重后的工具名数，小于 mapped（同一工具多个 CLI 别名）

    json_st = None
    try:
        json_st = os.stat(_TOOLS_JSON).st_mtime
    except OSError:
        pass
    val = {'ok': True, 'version': raw.get('version'), 'generatedAt': raw.get('generatedAt'),
           'totalTools': declared, 'cliTotal': ttot, 'parsedTotal': ttot,
           'localTotal': tlocal, 'mappedTotal': mapped, 'uniqToolTotal': uniq,
           'aliasTotal': mapped - uniq, 'moduleTotal': len(raw.get('modules') or []),
           'reconciles': mapped == declared,
           'writeTotal': tw, 'readTotal': ttot - tw, 'modules': mods, 'emptyModules': empty,
           'source': _rel(_TOOLS_JSON),
           'snapshotMtime': time.strftime('%Y-%m-%d', time.localtime(json_st)) if json_st else ''}
    _tools_cache.clear()
    _tools_cache.update({'key': key, 'val': val})
    return val


# =============================================================================
# 路由
# =============================================================================

@capability_bp.route('/okx-capability')
def capability_page():
    """OKX 交易操作能力清单（文档阅读页 + 工具矩阵）"""
    doc = _render_doc()
    tools = _load_tools()
    # 证据快照早于文档 → 提示重新抓取，避免"网页说的比实装的旧"
    stale = bool(doc.get('ok') and tools.get('ok') and tools.get('snapshotMtime')
                 and tools['snapshotMtime'] < doc['meta']['mtime'][:10])
    return render_template('okx_capability.html', active_page='okx-capability',
                           doc=doc, tools=tools, stale=stale)


@capability_bp.route('/okx-capability/api/matrix')
def capability_matrix_api():
    """工具矩阵原始数据（供脚本/调试复用，与页面同一份解析逻辑）"""
    return jsonify({'code': 200, 'data': _load_tools()})
