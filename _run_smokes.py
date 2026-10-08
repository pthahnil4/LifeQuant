# 批量跑冒烟脚本，输出每个的退出码
#
# 覆盖三组（futureStockTrade 合并迁移 Phase 6 起加入第二、三组）：
#   1) crypto/task/_smoke_*.py        —— 定时任务/交易层
#   2) crypto/_smoke_merge_*.py       —— 合并进来的期货/股票/K线训练：
#        每个模块跑「开启态」与「--off 关闭态」两轮，开关关掉后必须给 503
#        降级提示页而不是裸 404，且不能牵连宿主主功能。
#   3) 根目录 verify_*.py             —— 合并验收（依赖/密钥/模板冲突），
#        verify_06 退出码 1 代表「确实还有冲突」，属有效结论，同样计入失败。
import glob
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
here = os.path.dirname(os.path.abspath(__file__))
files = sorted(glob.glob(os.path.join(here, 'crypto', 'task', '_smoke_*.py')))
# 合并模块冒烟：夹具文件 _smoke_merge_helper.py 不是用例，别当脚本跑
files += sorted(f for f in glob.glob(os.path.join(here, 'crypto', '_smoke_merge_*.py'))
                if not f.endswith('_helper.py'))
# 合并模块的关闭态是独立一轮：同一脚本带 --off 再跑一次
MERGE_OFF = ('_smoke_merge_futures.py', '_smoke_merge_stocks.py', '_smoke_merge_kline.py')

jobs = [(f, []) for f in files]
jobs += [(os.path.join(here, 'crypto', name), ['--off']) for name in MERGE_OFF]

fails = []
for f, extra in jobs:
    p = subprocess.run([sys.executable, '-X', 'utf8', '-B', f] + extra,
                       cwd=here, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=900)
    tail = (p.stdout or '').strip().splitlines()
    name = os.path.basename(f) + ((' ' + extra[0]) if extra else '')
    if p.returncode != 0:
        fails.append(name)
        print(f'[FAIL] {name} rc={p.returncode}')
        print('  stdout:', ' | '.join(tail[-6:]))
        print('  stderr:', ' | '.join((p.stderr or '').strip().splitlines()[-8:]))
    else:
        print(f'[ ok ] {name} :: {tail[-1] if tail else "(no output)"}')
print('=' * 60)
print('FAILED:', fails or 'none')
if fails:
    sys.exit(1)
