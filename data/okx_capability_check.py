# -*- coding: utf-8 -*-
"""OKX 能力清单页 —— 文档/图示/证据文件 一致性校验（stdout 全 ASCII，规避 GBK 控制台）

用法：
    python data/okx_capability_check.py            # 校验，有失败则 exit 1
    python data/okx_capability_check.py --dump     # 另存渲染结果到 data/_render_dump.html

校验三件事：
1. 文档能否被 python-markdown 正常渲染，且结构数量对得上（表格/代码块/小节）
2. capability_diagrams.SCENARIOS 的每条 match 是否仍能命中某个标题（改标题措辞会 MISS）
3. 证据文件 _okx_list_tools.json 能否解析，且解析出的命令数与 totalTools 声明一致
"""
import io
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

DOC = os.path.join(ROOT, 'doc', 'OKX交易操作能力清单.md')
TOOLS = os.path.join(ROOT, 'data', '_okx_list_tools.json')

import markdown  # noqa: E402
from crypto.capability_routes import _post_process, _load_tools  # noqa: E402
from crypto.capability_diagrams import SCENARIOS  # noqa: E402

fails = []
text = io.open(DOC, encoding='utf-8').read()

# ---- 1. 源文件表格列数自检（漏一个 | 会让整表错位，且 markdown 不报错）-----
block, bad = [], []
for ln, line in enumerate(text.split('\n'), 1):
    if line.startswith('|'):
        block.append((ln, line.count('|') - line.count('\\|')))
    else:
        if len(block) > 1 and len(set(w for _, w in block)) > 1:
            bad.append('table@L%d widths=%s' % (block[0][0], sorted(set(w for _, w in block))))
        block = []
fails += bad
src_tables = len(re.findall(r'^\|[\s:|-]+\|\s*$', text, re.M))
src_fences = len(re.findall(r'^```', text, re.M)) // 2

# ---- 2. 渲染 + 结构比对 ----------------------------------------------------
html, toc, diag, sec_n = _post_process(markdown.markdown(
    text, extensions=['tables', 'fenced_code', 'sane_lists']))

checks = [
    ('tables rendered == separator rows', html.count('cap-tbl'), src_tables),
    ('code blocks rendered == fences', html.count('class="cap-code"'), src_fences),
    ('sections == h2 + lead', html.count('<section'), len([t for t in toc if t['level'] == 2]) + 1),
    ('div balanced', html.count('<div'), html.count('</div>')),
    ('section balanced', html.count('<section'), html.count('</section>')),
    ('svg balanced', html.count('<svg'), html.count('</svg>')),
    ('literal backslash-pipe left', html.count('\\|'), 0),
    ('raw <table> without wrapper', len(re.findall(r'(?<!</div>)<table>', html)), 0),
    ('heading ids unique', len({t['id'] for t in toc}), len(toc)),
]
for name, got, want in checks:
    flag = 'OK  ' if got == want else 'FAIL'
    if got != want:
        fails.append('%s (got %s want %s)' % (name, got, want))
    print('[%s] %-34s %s' % (flag, name, got if got == want else '%s != %s' % (got, want)))

# ---- 3. 图示 match 是否仍能命中标题 ----------------------------------------
labels = [t['text'] for t in toc]
hit = 0
for sc in SCENARIOS:
    if any(sc['match'] in l for l in labels):
        hit += 1
    else:
        fails.append('diagram MISS: match=%r title=%r' % (sc['match'], sc['title'][:30]))
print('[%s] %-34s %d/%d' % ('OK  ' if hit == len(SCENARIOS) else 'FAIL', 'scenario matches hit', hit, len(SCENARIOS)))
print('[%s] %-34s %d' % ('OK  ' if html.count('cap-scenario">') == hit else 'FAIL',
                         'scenario blocks injected', html.count('cap-scenario">')))

# ---- 4. 证据文件解析 -------------------------------------------------------
tools = _load_tools()
if not tools.get('ok'):
    fails.append('tools matrix unavailable: %s' % tools.get('error'))
    print('[FAIL] %-34s %s' % ('tools json', tools.get('error')))
else:
    print('[INFO] kit v%s  cliCmds=%d (write %d / read %d)  localOnly=%d  mappedToTool=%d  '
          'uniqToolName=%d  aliases=%d  declaredTotalTools=%s  modules=%d(with cmds)/%d' % (
              tools['version'], tools['cliTotal'], tools['writeTotal'], tools['readTotal'],
              tools['localTotal'], tools['mappedTotal'], tools['uniqToolTotal'],
              tools['aliasTotal'], tools['totalTools'], len(tools['modules']), tools['moduleTotal']))
    # 官方 totalTools 的口径 = 「有 MCP 工具承载的 CLI 命令数」（不含 config/pilot/skill 本地命令）。
    # 这条等式成立才说明页面显示的三个总数是自洽的。
    if not tools.get('reconciles'):
        fails.append('totalTools drift: mapped cmds %d != declared %s'
                     % (tools['mappedTotal'], tools['totalTools']))
    else:
        print('[OK  ] declared totalTools == CLI commands backed by a tool')
    empty = [m['name'] for m in tools['modules'] if not m['commands']]
    if empty:
        fails.append('modules with no commands: %s' % empty)

# ---- 5. 文档更新时间 vs 证据快照 -------------------------------------------
doc_mt = os.path.getmtime(DOC)
if tools.get('ok') and os.path.exists(TOOLS) and os.path.getmtime(TOOLS) < doc_mt - 1:
    print('[WARN] evidence json is OLDER than the doc -> rerun data/okx_capability_dump.ps1')

if '--dump' in sys.argv:
    out = os.path.join(ROOT, 'data', '_render_dump.html')
    io.open(out, 'w', encoding='utf-8').write(html)
    print('dumped -> data/_render_dump.html (%d bytes)' % len(html.encode('utf-8')))

print('\n' + ('ALL PASS' if not fails else 'FAILURES:\n  ' + '\n  '.join(fails)))
sys.exit(1 if fails else 0)
