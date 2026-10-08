#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：OKX 能力清单阅读页 /okx-capability
============================================
覆盖点：
1. 蓝图 + 模板可编译；页面 200
2. 文档渲染结构完整（表格/代码块/小节/图示数量与源文件一致，转义竖线已还原）
3. 工具矩阵来自 data/_okx_list_tools.json，三个总数自洽（178 CLI − 12 本地 = 166 = 官方声明）
4. 导航入口注册且能高亮当前页
5. 降级路径：markdown 库缺失时不报错，改出纯文本 + 告警条
6. 文档改动后缓存失效（mtime 变了要重渲染）
7. 窄屏可读性（浏览器实测 480px 暴露的四个真问题）：目录收成浮层不挡正文、
   目录高亮跟随不得劫持整页滚动、超长行内 code 不许顶破版面、
   顶部 chip 计数须与实际渲染一致（曾显示 18 代码块 / 14 小节，实为 9 / 15）

安全边界：全程不导入 crypto.app（会拉起调度器与 DB 预热），只挂本蓝图。
约定：收集全部失败再退出，不在首个失败处 sys.exit。
"""

import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from flask import Flask  # noqa: E402
import crypto.capability_routes as cr  # noqa: E402

failures = []
ran = [0]


def check(name, cond, detail=''):
    ran[0] += 1
    if not cond:
        failures.append(name)
    print(('[OK]  ' if cond else '[FAIL] ') + name + (('  ' + detail) if detail else ''))


app = Flask(__name__)
app.register_blueprint(cr.capability_bp)
client = app.test_client()

# ---- 0. 前置：源文件与证据文件都在 ---------------------------------------
DOC = os.path.join(os.path.dirname(_HERE), 'doc', 'OKX交易操作能力清单.md')
TOOLS = os.path.join(os.path.dirname(_HERE), 'data', '_okx_list_tools.json')
check('doc file exists', os.path.isfile(DOC), DOC)
check('evidence json exists', os.path.isfile(TOOLS), TOOLS)
src = open(DOC, encoding='utf-8').read() if os.path.isfile(DOC) else ''
n_sep = len(re.findall(r'^\|[\s:|-]+\|\s*$', src, re.M))
n_fence = len(re.findall(r'^```', src, re.M)) // 2
n_h2 = len(re.findall(r'^## (?!#)', src, re.M))

# ---- 1. 模板可编译 --------------------------------------------------------
try:
    app.jinja_env.get_template('okx_capability.html')
    check('template compiles', True)
except Exception as e:
    check('template compiles', False, str(e))
try:
    app.jinja_env.get_template('nav.html')
    check('nav.html compiles', True)
except Exception as e:
    check('nav.html compiles', False, str(e))
# nav.html 被每个页面 {% include %}，改它等于改全站；这里顺手把全部模板都编译一遍
_tdirs = os.path.join(_HERE, 'templates')
_bad = []
for _f in sorted(os.listdir(_tdirs)):
    if not _f.endswith('.html'):
        continue
    try:
        app.jinja_env.get_template(_f)
    except Exception as e:
        _bad.append('%s: %s' % (_f, e))
check('全站 %d 个模板均可编译' % len([f for f in os.listdir(_tdirs) if f.endswith('.html')]),
      not _bad, '; '.join(_bad[:3]))

# ---- 2. 页面 200 + 结构完整 ----------------------------------------------
r = client.get('/okx-capability')
check('GET /okx-capability -> 200', r.status_code == 200, 'status=%s' % r.status_code)
html = r.get_data(as_text=True)
check('utf-8 中文未乱码', '交易操作能力清单' in html)
check('表格数 == 源文件表头行数', html.count('class="cap-tbl"') == n_sep,
      '%d vs %d' % (html.count('class="cap-tbl"'), n_sep))
check('代码块数 == 源围栏数', html.count('class="cap-code"') == n_fence,
      '%d vs %d' % (html.count('class="cap-code"'), n_fence))
# 顶部 chip 是另一套表达式算出来的，不跟上面对齐就会出现"页面说有 18 个、实际只有 9 个"
_chip = re.search(r'<b>(\d+)</b> 节 / <b>(\d+)</b> 表 / <b>(\d+)</b> 代码块', html)
check('顶部 chip 三个计数与实际渲染一致',
      bool(_chip) and int(_chip.group(1)) == n_h2 + 1
      and int(_chip.group(2)) == n_sep and int(_chip.group(3)) == n_fence,
      'chip=%s vs 实=%d/%d/%d' % (_chip.groups() if _chip else None, n_h2 + 1, n_sep, n_fence))
check('小节数 == h2+1(概述)', html.count('<section class="cap-sec') == n_h2 + 1,
      '%d vs %d+1' % (html.count('<section class="cap-sec'), n_h2))
check('section 标签闭合', html.count('<section') == html.count('</section>'))
check('div 标签闭合', html.count('<div') == html.count('</div>'),
      '%d/%d' % (html.count('<div'), html.count('</div>')))
check('表格全部有滚动容器', html.count('<table>') == 0 and html.count('cap-scroll') >= n_sep)
check('转义竖线已还原', '\\|' not in html)
check('无未替换的模板变量', '{{' not in re.sub(r'<script id="cap-tools-data.*?</script>', '', html, flags=re.S))

# ---- 3. 图示注入 ----------------------------------------------------------
check('图示全部注入', html.count('class="cap-scenario"') == len(cr.SCENARIOS),
      '%d/%d' % (html.count('class="cap-scenario"'), len(cr.SCENARIOS)))
check('图示 svg 成对', html.count('<svg') == html.count('</svg>') == len(cr.SCENARIOS))
check('图示带 aria-label', html.count('aria-label=') >= len(cr.SCENARIOS))
check('冰山图示参数在位', 'szLimit' in html and 'pxVar' in html)

# ---- 4. 目录 / 锚点 -------------------------------------------------------
# 注意：section 上的 data-hid="cap-hN" 会被裸 id=" 正则误抓，锚点只从 h2/h3 标签本身取
h23_ids = re.findall(r'<h[23] class="cap-h cap-h[23]" id="(cap-h\d+)"', html)
toc_links = re.findall(r'<a href="#(cap-h\d+)" class="lv', html)
check('TOC 与 h2/h3 锚点一一对应', sorted(h23_ids) == sorted(toc_links),
      '%d ids vs %d links' % (len(h23_ids), len(toc_links)))
check('锚点 id 唯一', len(set(h23_ids)) == len(h23_ids))
# 桌面目录条目多到会被 ellipsis 截断（实测 34 条里 21 条），没 title 就查不回全称
check('目录条目都带 title',
      len(re.findall(r'<a href="#cap-h\d+" class="lv\d" data-t="[^"]*" title="[^"]*"', html)) == len(toc_links),
      '%d/%d' % (len(re.findall(r'<a href="#cap-h\d+" class="lv\d" data-t="[^"]*" title="[^"]*"', html)),
                 len(toc_links)))
check('TOC 含附录 B 小节', '附录 B' in html and 'B.1' in html)

# ---- 4b. 窄屏可读性（浏览器实测踩过的三个坑，全部锁死） -------------------
_tpl = open(os.path.join(_HERE, 'templates', 'okx_capability.html'), encoding='utf-8').read()
_js = open(os.path.join(_HERE, 'static', 'js', 'okx_capability.js'), encoding='utf-8').read()
check('窄屏目录开关已渲染', 'id="cap-toc-switch"' in html)
check('窄屏下目录默认收起', '.cap-toc-switch { display: flex; }' in _tpl
      and '.cap-toc, .cap-side-foot { display: none; }' in _tpl)
check('目录条目不许被 flex 压扁裁字', 'flex: 0 0 auto;' in _tpl)
check('行内 code 长标识符可断行', re.search(r'\.cap-doc :not\(pre\) > code \{[^}]*overflow-wrap: anywhere', _tpl) is not None)
check('正文段落长串可断行', re.search(r'\.cap-doc p, \.cap-doc li \{[^}]*overflow-wrap: anywhere', _tpl) is not None)
check('窄屏模块卡改两列起排', 'minmax(148px, 1fr)' in _tpl)
# 目录高亮跟随若用 scrollIntoView({block:'nearest'})，窄屏下 .cap-side 不再是滚动容器，
# 这句会改为滚 document，把用户刚读到的位置拽回去（实测： scrollTop 5000 立刻弹回 359）
check('目录跟随不劫持整页滚动', "block: 'nearest'" not in _js and 'keepTocVisible' in _js)
check('目录开关已接线', 'cap-toc-switch' in _js)
# 收起目录只走 setTocOpen 一个出口：绕开它改 class 就会留下「目录已收、箭头还朝上」的脏状态
check('目录开合状态单一出口', "toc.classList.remove('is-open')" not in _js and 'function setTocOpen' in _js)

# ---- 5. 工具矩阵（证据文件驱动） -----------------------------------------
t = cr._load_tools()
check('matrix parsed', t.get('ok') is True, str(t.get('error', '')))
check('对账等式成立 (CLI - 本地 == 官方 totalTools)',
      t.get('reconciles') is True and t['cliTotal'] - t['localTotal'] == t['totalTools'],
      '%s - %s vs %s' % (t.get('cliTotal'), t.get('localTotal'), t.get('totalTools')))
check('去重工具名 = 承载命令数 - 别名数',
      t['uniqToolTotal'] == t['mappedTotal'] - t['aliasTotal'],
      '%s vs %s-%s' % (t['uniqToolTotal'], t['mappedTotal'], t['aliasTotal']))
check('读写相加 = 命令总数', t['writeTotal'] + t['readTotal'] == t['cliTotal'])
check('页面内嵌 JSON 与解析结果一致',
      ('"cliTotal": %d' % t['cliTotal']) in html or ('"cliTotal":%d' % t['cliTotal']) in html)
for probe in ('spot_place_algo_order', 'swap_close_position', 'grid_create_order', 'dca_create_order'):
    check('矩阵含命令 %s' % probe, probe in html)
# 本地专属命令（无 MCP 工具）由 JS 渲染，这里验数据层计数自洽 + JS 里确有该文案
_local = sum(1 for m in t['modules'] for c in m['commands'] if c['tool'] == '-')
check('无 MCP 工具的命令数 == localTotal', _local == t['localTotal'], '%d vs %s' % (_local, t['localTotal']))
check('JS 会给本地命令打标', '仅 CLI 可用' in _js)
check('JS 被页面引用', 'okx_capability.js' in html)

r2 = client.get('/okx-capability/api/matrix')
check('GET /okx-capability/api/matrix -> 200', r2.status_code == 200)
check('matrix api 结构与页面同源', r2.get_json()['data']['version'] == t['version'])

# ---- 6. 导航入口（nav.html 含 url_for，必须在请求上下文里断言，故直接查页面）----
_nav = html[html.find('<nav'):html.find('</nav>')]
check('导航含 /okx-capability 入口', 'href="/okx-capability"' in _nav)
m = re.search(r'href="/okx-capability" class="([^"]*)"', _nav)
check('当前页高亮', bool(m) and 'active' in m.group(1), m.group(1) if m else '未找到链接')
m2 = re.search(r'href="/api-console" class="([^"]*)"', _nav)
check('其它入口不误高亮', bool(m2) and 'active' not in m2.group(1))
_links = re.findall(r'<a href="[^"]*" class="nav-link[ "]', _nav)   # 不能裸数 'class="nav-link'，会误吞容器 class="nav-links"
check('导航项总数 12（新增 1 项）', len(_links) == 12, str(len(_links)))

# ---- 7. 缓存：文档变化要重渲染（临时副本放项目内，避免跨盘符；不碰仓库文档时间戳）----
_tmp = os.path.join(os.path.dirname(_HERE), 'data', '_smoke_cap_tmp')
os.makedirs(_tmp, exist_ok=True)
_copy = os.path.join(_tmp, 'cap.md')
with open(_copy, 'w', encoding='utf-8') as f:
    f.write(src + '\n\n## 冒烟追加节\n\n正文。\n')
_real_path, _real_cache = cr._DOC_PATH, dict(cr._doc_cache)
cr._DOC_PATH = _copy
cr._doc_cache.clear()
v1 = cr._render_doc()
n1 = len(v1.get('body', ''))
time.sleep(1.1)                      # 确保 mtime_ns 一定变化
with open(_copy, 'a', encoding='utf-8') as f:
    f.write('\n再追加一行内容，长度可变。\n')
v2 = cr._render_doc()
check('文档变更后重新渲染（非缓存）', v2.get('ok') and len(v2.get('body', '')) > n1,
      '%d -> %d' % (n1, len(v2.get('body', ''))))
check('新增节进入目录', any('冒烟追加节' in t['text'] for t in v2.get('toc', [])))
v3 = cr._render_doc()
check('未变更时命中缓存', v3 is v2)
cr._DOC_PATH, cr._doc_cache = _real_path, _real_cache
cr._doc_cache.clear()

# ---- 8. 降级：markdown 库缺失 --------------------------------------------
_saved = cr._md
cr._md = None
cr._doc_cache.clear()
rd = client.get('/okx-capability')
hd = rd.get_data(as_text=True)
check('缺 markdown 库仍 200', rd.status_code == 200, 'status=%s' % rd.status_code)
check('降级为纯文本容器', 'cap-fallback' in hd)
check('降级有可见告警', '未安装 Markdown' in hd or '降级' in hd)
check('降级页仍带工具矩阵', 'cap-matrix' in hd and 'spot_place_algo_order' in hd)
cr._md = _saved
cr._doc_cache.clear()
check('恢复后正常渲染', client.get('/okx-capability').status_code == 200 and
      'cap-scenario' in client.get('/okx-capability').get_data(as_text=True))

# ---- 9. 证据文件缺失 ------------------------------------------------------
_savedf = cr._TOOLS_JSON
cr._TOOLS_JSON = os.path.join(_HERE, '_no_such_file.json')
cr._tools_cache.clear()
rm = client.get('/okx-capability')
check('证据文件缺失仍 200 且明确告知', rm.status_code == 200 and '工具矩阵不可用' in rm.get_data(as_text=True))
cr._TOOLS_JSON = _savedf
cr._tools_cache.clear()

# ---- 10. 展示用路径计算不能把页面搞挂（跨盘符 relpath 会抛 ValueError）----
check('_rel 跨盘符不抛异常', '/' in cr._rel('Z:\\elsewhere\\x.md'))
check('_rel 正常情况给相对路径', cr._rel(DOC).startswith('doc/'), cr._rel(DOC))

import shutil  # noqa: E402
shutil.rmtree(_tmp, ignore_errors=True)

print('\n' + ('ALL PASS  (%d checks)' % ran[0] if not failures
            else 'FAILED %d / %d: %s' % (len(failures), ran[0], ' | '.join(failures))))
sys.exit(1 if failures else 0)
