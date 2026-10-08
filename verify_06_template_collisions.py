# -*- coding: utf-8 -*-
"""
模板命名冲突与解析归属核查（futureStockTrade 合并迁移 Phase 6）
==============================================================

合并之后期货 / 股票 / K 线训练三个蓝图挂进同一个 Flask 应用，它们的
``template_folder`` 与宿主的 ``crypto/templates`` 会**一起**进入 Jinja 搜索路径。
同名模板因此存在两种故障形态：

1. 蓝图页面渲染到宿主的同名模板（或反之），拿到完全不同的变量与接口地址，
   表现为「页面 200 但整块数据空白 / 前端 fetch 404」——冒烟状态码全绿也可能命中，
   所以不能只看 HTTP 码。
2. 解析顺序随 Flask 版本 / 蓝图注册顺序变化，升级或调整注册顺序后悄悄换了文件。

本次核查已经实际命中一处：宿主 ``/star-market``（加密货币星标行情，650 行）与期货
``/futures/star-market``（期货星标品种行情，908 行）都用裸名 ``star_market.html``。

脚本做两件事：
- 静态：把仓库里所有 ``render_template`` / ``{% include %}`` / ``{% extends %}``
  引用的模板名，对照「同一文件名出现在多个模板目录」求交集，列出冲突清单。
- 实测：导入宿主 app（``CRYPTO_NO_BACKGROUND=1``，绝不拉起调度器），用
  ``app.jinja_env.get_template()`` 打出每个冲突名**当前实际解析到哪个文件**，
  从而判断是哪一侧被覆盖。

用法：``python verify_06_template_collisions.py``
退出码：0 = 无冲突；1 = 存在同名模板（需要命名收敛）；2 = 执行异常。
"""
import io
import os
import re
import sys

# Windows 控制台可能是 GBK，中文与 emoji 输出会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 必须在导入 crypto.app 之前设置：宿主 import 阶段会拉起调度器与预热线程
os.environ['CRYPTO_NO_BACKGROUND'] = '1'

APP_TEMPLATE_DIR = os.path.join(ROOT, 'crypto', 'templates')

# 蓝图模板目录：目录名 → (相对展示名)。新增蓝图时在此登记即可。
BLUEPRINT_TEMPLATE_DIRS = {
    'futures': os.path.join(ROOT, 'crypto', 'futures', 'templates'),
    'stocks': os.path.join(ROOT, 'crypto', 'stocks', 'templates'),
    'kline': os.path.join(ROOT, 'crypto', 'kline_training', 'templates'),
}

SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv',
             'futureStockTrade', 'data', 'doc', 'opscenter'}

# 演示 / 自检 / 一次性脚本：这些位置里的模板引用不参与「缺失」判定。
# 理由：demo 常自带模板目录或用假名演示 API 用法，本脚本自己的文档示例
# （render_template('a.html')）也属此类，报成缺失只会淹掉真信号。
SCRATCH_PATTERNS = (
    'verify_', '_smoke', 'demo', 'test_', 'tools' + os.sep,
    'opscenter_dashboard',
)

_SELF = os.path.basename(__file__)


def is_scratch(rel_path: str) -> bool:
    if rel_path == _SELF:
        return True
    low = rel_path.replace('\\', '/').lower()
    name = os.path.basename(low)
    if any(p in name for p in SCRATCH_PATTERNS if p.endswith(os.sep) is False):
        return True
    return any(p.rstrip('/') in low for p in SCRATCH_PATTERNS)

# render_template('a.html') / render_template("a.html")
RE_RENDER = re.compile(r"""render_template\(\s*(['"])([^'"]+)\1""")
# {% include 'a.html' %} / {% extends "a.html" %}，也覆盖 without context 等后缀
RE_JINJA = re.compile(
    r"""\{%-?\s*(?:include|extends|import|from)\s+(['"])([^'"]+)\1""")


def iter_source_files(suffixes):
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.endswith('.egg-info')]
        for fn in filenames:
            if fn.endswith(suffixes):
                yield os.path.join(dirpath, fn)


def _open_lines(path):
    with io.open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read().splitlines()


def collect_references():
    """返回 {模板名: [(引用文件, 行号, 引用方式), ...]}。

    ``render_template`` 只会出现在 .py；``{% include %}`` / ``{% extends %}``
    出现在 .html（宿主 nav.html 就是这样被各模块模板引用的），两边都要扫。
    """
    refs = {}
    for path in iter_source_files(('.py', '.html', '.jinja', '.j2')):
        rel = os.path.relpath(path, ROOT)
        scratch = is_scratch(rel)
        try:
            for lineno, line in enumerate(_open_lines(path), 1):
                if path.endswith('.py'):
                    for m in RE_RENDER.finditer(line):
                        refs.setdefault(m.group(2), []).append(
                            (rel, lineno, 'render_template', scratch))
                for m in RE_JINJA.finditer(line):
                    refs.setdefault(m.group(2), []).append(
                        (rel, lineno, 'jinja 引用', scratch))
        except Exception as e:
            print(f'[WARN] 读取失败 {path}: {e}')
    return refs


def discover_template_dirs():
    """从**真实注册状态**取 Jinja 搜索目录，而不是靠人肉维护清单。

    ``app.template_folder`` + 每个已注册蓝图的 ``template_folder`` 都会进入同一条
    搜索链，所以冲突只可能发生在这些目录之间。用运行时注册结果 enumeration，
    新挂一个蓝图（比如以后 journal/expense 拆出独立模板目录）不需要改本脚本。

    导入失败时退回手工清单，保证静态部分仍可运行。
    """
    dirs = {}
    try:
        from crypto.app import app
    except Exception as e:
        print(f'[WARN] 导入宿主 app 失败，退回手工目录清单: {e}')
        dirs['宿主 crypto/templates'] = APP_TEMPLATE_DIR
        for bp, d in BLUEPRINT_TEMPLATE_DIRS.items():
            dirs[f'蓝图 {bp}'] = d
        return dirs, None

    dirs[f'宿主 {os.path.basename(app.root_path)}/templates'] = \
        os.path.join(app.root_path, app.template_folder or 'templates')
    for name, bp in app.blueprints.items():
        if getattr(bp, 'template_folder', None):
            dirs[f'蓝图 {name}'] = os.path.join(bp.root_path, bp.template_folder)
    return dirs, app


def build_index(dirs):
    """返回 {模板文件名: [所属模板目录, ...]}，只登记真实存在的文件。"""
    index = {}
    for label, d in dirs.items():
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(('.html', '.jinja', '.j2')):
                index.setdefault(fn, []).append(label)
    return index



def real_refs(refs_list):
    """过滤掉演示/自检脚本里的引用，只留生产代码的引用。"""
    return [r for r in refs_list if not r[3]]


def find_conflicts(index, refs):
    """同名模板出现在 ≥2 个目录 = 冲突；再区分「确实被生产代码引用」与「闲置文件」。"""
    out = []
    for fn, owners in sorted(index.items()):
        if len(owners) < 2:
            continue
        out.append({
            'name': fn,
            'owners': owners,
            'referenced': bool(real_refs(refs.get(fn, []))),
            'refs': refs.get(fn, []),
        })
    return out


def report_static(conflicts, index, refs):
    print(f'\n模板目录共 {len(index)} 个唯一文件名，代码引用名 '
          f'{len(refs)} 个')
    if not conflicts:
        print('[OK] 没有同名模板，不存在跨目录覆盖风险')
        return
    for c in conflicts:
        flag = '被生产代码引用（高危）' if c['referenced'] else '文件存在但无人引用（低危）'
        print(f'\n[冲突] {c["name"]}  —— {flag}')
        print(f'       出现于：{"、".join(c["owners"])}')
        for rel, lineno, kind, scratch in c['refs'][:6]:
            tag = '，仅演示/自检' if scratch else ''
            print(f'       引用：{rel}:{lineno} ({kind}{tag})')


def report_runtime(conflicts, app, dirs):
    """实测解析归属：谁赢、谁被覆盖，只有导入真实 app 才能确定。"""
    print('\n' + '=' * 78)
    print('② 实测核查：冲突名在当前注册顺序下实际解析到哪个文件')
    print('=' * 78)
    if app is None:
        print('[SKIP] app 未导入，无法实测解析归属（仅静态结论）')
        return 0
    if not conflicts:
        print('[SKIP] 无冲突，不必解析')
        return 0

    bad = 0
    for c in conflicts:
        try:
            t = app.jinja_env.get_template(c['name'])
            resolved = os.path.normpath(getattr(t, 'filename', '') or '')
        except Exception as e:
            print(f'\n[ERROR] 模板 {c["name"]} 解析失败: {e}')
            bad += 1
            continue
        print(f'\n模板 {c["name"]} → {rel_or_abs(resolved)}')
        winners = []
        for label, d in dirs.items():
            cand = os.path.normpath(os.path.join(d, c['name']))
            if os.path.isfile(cand) and cand == resolved:
                winners.append(label)
        losers = [o for o in c['owners'] if o not in winners]
        if winners:
            print(f'       实际使用：{"、".join(winners)}')
        if losers and c['referenced']:
            print(f'       被遮蔽  ：{"、".join(losers)}  ← 该侧页面渲染的不是自己的模板')
            bad += 1
    return bad


def rel_or_abs(p):
    if not p:
        return '(未知)'
    try:
        return os.path.relpath(p, ROOT)
    except Exception:
        return p


def main():
    refs = collect_references()
    dirs, app = discover_template_dirs()
    index = build_index(dirs)
    print('=' * 78)
    print('① 静态核查：同名模板出现在多个搜索目录')
    print('=' * 78)
    print('Jinja 搜索目录（取自运行时注册状态）：')
    for label, d in dirs.items():
        mark = '' if os.path.isdir(d) else '  (不存在，跳过)'
        print(f'  - {label}: {rel_or_abs(d)}{mark}')
    conflicts = find_conflicts(index, refs)
    report_static(conflicts, index, refs)
    bad = report_runtime(conflicts, app, dirs)

    # 顺带列出「生产代码引用了、但搜索目录里都没有」的名字：合并漏拷模板会在这里暴露
    print('\n' + '=' * 78)
    print('③ 引用完整性：生产代码引用了、但搜索目录里都没有的模板')
    print('=' * 78)
    missing = sorted(n for n in refs
                     if n.endswith(('.html', '.jinja', '.j2'))
                     and n not in index and real_refs(refs[n]))
    scratch_missing = sorted(n for n in refs
                             if n.endswith(('.html', '.jinja', '.j2'))
                             and n not in index and not real_refs(refs[n]))
    if missing:
        for n in missing:
            print(f'[缺失] {n}')
            for rel, lineno, kind, _s in real_refs(refs[n])[:3]:
                print(f'       ← {rel}:{lineno} ({kind})')
    else:
        print('[OK] 生产代码引用的模板文件都存在')
    if scratch_missing:
        print(f'[提示] 另有 {len(scratch_missing)} 个名字只出现在演示/自检脚本里，'
              f'不参与判定：{", ".join(scratch_missing)}')

    print('\n' + '=' * 78)
    if conflicts:
        print(f'结论：发现 {len(conflicts)} 组同名模板，'
              f'其中 {bad} 组确认存在跨目录遮蔽 → 需要命名收敛（退出码 1）')
        print('      收敛口径：蓝图自有模板一律带模块前缀（futures_ / stocks_ / kline_ / pk_），'
              '与宿主 crypto/templates 天然不重名。')
        return 1
    if missing:
        print(f'结论：命名无冲突，但有 {len(missing)} 个被引用却缺失的模板（退出码 1）')
        return 1
    print('结论：模板命名无冲突，引用完整')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as _e:
        print(f'[FATAL] 核查脚本异常: {_e}')
        import traceback
        traceback.print_exc()
        sys.exit(2)
