# -*- coding: utf-8 -*-
"""一次性暂存脚本：只把「合并模块接线」那段塞进 git 索引，不带入宿主未提交的 WIP。

背景：crypto/app.py 的工作区改动里混着两类内容——
  1) 宿主自己的在制功能（每日多维打卡 / 指标导航 / BTC 1分钟K线 / 导航管理）
  2) futureStockTrade 合并迁移 Phase 2~6 的蓝图注册与降级助手（_FST_MODULES 那块）
两者落在同一个 hunk，`git add crypto/app.py` 会把用户的在制代码一起提交。
本脚本按「HEAD 原文 + 仅第 2 类行」重建索引内容，用 hash-object/update-index
直接写入索引，工作区文件保持不动（用户 WIP 一行不少）。

用法：python -X utf8 data/_stage_fst_wiring.py          # 预览（不写索引）
      python -X utf8 data/_stage_fst_wiring.py --apply  # 写入索引
"""
import ast
import subprocess
import sys

FILE = 'crypto/app.py'
# 工作区里「合并模块接线」块的行范围（1 基，闭区间）：
# 起 = 公共助手注释块首行，止 = K 线训练 _register_fst_module(...) 调用行
BLOCK_START, BLOCK_END = 130, 269
ANCHOR = 'app.register_blueprint(instinct_bp)'   # HEAD 里的插入锚点（合并块就接在它后面）

MUST_HAVE = ('_FST_MODULES', '_register_fst_module', '_register_fst_fallback',
             'FST_ENABLE_FUTURES', 'FST_ENABLE_STOCKS', 'FST_ENABLE_KLINE',
             'kline_training', 'fst_unavailable.html')
MUST_NOT = ('checkin_bp', 'indicator_nav_bp', 'kline_live_bp', 'navigation_routes')


def run(args, data=None):
    p = subprocess.run(args, input=data, capture_output=True, check=True)
    return p.stdout


def main():
    apply = '--apply' in sys.argv

    head_lines = run(['git', 'show', f'HEAD:{FILE}']).splitlines(keepends=True)
    work_lines = open(FILE, 'rb').read().splitlines(keepends=True)

    anchors = [i for i, l in enumerate(head_lines) if l.rstrip() == ANCHOR.encode()]
    if len(anchors) != 1:
        raise SystemExit(f'[FAIL] HEAD 里锚点出现 {len(anchors)} 次，需人工确认插入位置')
    a = anchors[0]

    block = work_lines[BLOCK_START - 1:BLOCK_END]
    joined = b''.join(block).decode('utf-8')
    for k in MUST_HAVE:
        if k not in joined:
            raise SystemExit(f'[FAIL] 合并块内缺少 {k}，行范围取错了')
    for k in MUST_NOT:
        if k in joined:
            raise SystemExit(f'[FAIL] 合并块混入宿主 WIP：{k}')
    if '合并模块' not in joined or '_register_fst_module(app, \'kline\'' not in joined:
        raise SystemExit('[FAIL] 合并块首尾不完整')

    # blob 里的行尾以 HEAD 为准（本仓库 core.autocrlf 会把工作区 CRLF 存成 LF），
    # 否则索引内容会混着两种行尾。
    head_has_crlf = any(l.endswith(b'\r\n') for l in head_lines)
    if head_has_crlf != any(l.endswith(b'\r\n') for l in block):
        block = [l.replace(b'\r\n', b'\n') for l in block] if not head_has_crlf else \
                [l.replace(b'\n', b'\r\n') for l in [b.replace(b'\r\n', b'\n') for b in block]]
    eol = b'\r\n' if head_has_crlf else b'\n'
    staged = head_lines[:a + 1] + [eol] + block + head_lines[a + 1:]
    ast.parse(b''.join(staged).decode('utf-8'))

    print(f'[INFO] HEAD {len(head_lines)} 行 → 索引 {len(staged)} 行'
          f'（+{len(staged) - len(head_lines)}，其中合并块 {len(block)} 行）')
    print(f'[INFO] 取用工作区 行 {BLOCK_START}..{BLOCK_END}，EOL={"CRLF" if eol == b"\\r\\n" else "LF"}')
    print('[INFO] 语法自检通过')

    if not apply:
        print('[DRY-RUN] 未写入索引；加 --apply 生效')
        return

    sha = run(['git', 'hash-object', '-w', '--stdin', '--path', FILE],
              b''.join(staged)).decode().strip()
    run(['git', 'update-index', '--cacheinfo', f'100644,{sha},{FILE}'])
    print(f'[OK] 索引已写入 {FILE} @ {sha}')


if __name__ == '__main__':
    main()
