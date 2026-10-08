#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpsCenter 全站回归总入口（P1–P5 一次性体检）。

依次以子进程运行 _smoke_opscenter_p1..p5.py（各自离线、走 Flask test_client、不
import crypto），聚合每阶段返回码与结尾结论行，产出统一验收报告。任一阶段失败则整体
退出码非 0。用于快速确认"改界面不改逻辑、保交易不动"后全站仍自洽，对应安全红线：
可回滚 + 冒烟快速验证。

用法：
    python _smoke_opscenter_all.py            # 纯离线子进程回归（不依赖任何服务在跑）
    python _smoke_opscenter_all.py --ports    # 额外对已运行的 6002 做只读 HTTP 探活

--ports 只做 GET 探活，绝不 POST、绝不启动或重启任何进程；未启动 6002 时探活项会报错
但不影响离线回归结论。
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

PHASES = [
    ('P1', '_smoke_opscenter_p1.py', '核心页：仪表盘/健康/生命周期'),
    ('P2', '_smoke_opscenter_p2.py', '排障主力：内存治理/日志聚合'),
    ('P3', '_smoke_opscenter_p3.py', '运行态全景：调度/实盘只读'),
    ('P4', '_smoke_opscenter_p4.py', '运维工具箱：冒烟/诊断/配置(脱敏)'),
    ('P5', '_smoke_opscenter_p5.py', '告警中心：交易侧 + 系统侧'),
]


def _tail_line(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else ''


def run_phases():
    results = []
    for tag, fn, desc in PHASES:
        path = os.path.join(_HERE, fn)
        if not os.path.exists(path):
            results.append((tag, desc, 'MISSING', '找不到 %s' % fn))
            continue
        try:
            proc = subprocess.run(
                [sys.executable, path], cwd=_HERE,
                capture_output=True, text=True, timeout=120,
                encoding='utf-8', errors='replace')
            verdict = 'PASS' if proc.returncode == 0 else 'FAIL(%d)' % proc.returncode
            concl = _tail_line(proc.stdout or '') or _tail_line(proc.stderr or '')
        except Exception as e:
            verdict = 'ERROR'
            concl = repr(e)
        results.append((tag, desc, verdict, concl))
    return results


def probe_ports():
    """只对已运行的 6002 做只读 GET；不启动、不重启任何进程。"""
    from urllib.request import urlopen
    base = 'http://127.0.0.1:6002'
    urls = ['/', '/health', '/lifecycle', '/memory', '/logs', '/schedule',
            '/live', '/tools/smoke', '/tools/diag', '/tools/config',
            '/alerts', '/alerts/data']
    ok = True
    for u in urls:
        try:
            with urlopen(base + u, timeout=8) as r:
                st = r.status
        except Exception as e:
            st = 'ERR:%s' % e.__class__.__name__
            ok = False
        print('      GET %-16s -> %s' % (u, st))
    return ok


def main():
    mode_ports = '--ports' in sys.argv[1:]
    print('=' * 60)
    print('  OpsCenter 全站回归 · P1–P5（离线子进程，不 import crypto）')
    print('=' * 60)
    results = run_phases()
    passed = 0
    for tag, desc, verdict, concl in results:
        mark = '✅' if verdict == 'PASS' else '❌'
        if verdict == 'PASS':
            passed += 1
        print('  %s [%s] %-26s %s' % (mark, tag, desc, verdict))
        if concl:
            print('        └ %s' % concl)

    # 父进程自身也断言未 import crypto（子进程隔离，父进程更不应被污染）
    crypto_mods = sorted(x for x in sys.modules
                         if x == 'crypto' or x.startswith('crypto.'))
    print('-' * 60)
    print('  阶段通过：%d/%d    父进程 import crypto：%s'
          % (passed, len(PHASES), crypto_mods or '无（红线保持）'))
    all_ok = (passed == len(PHASES)) and not crypto_mods

    if mode_ports:
        print('-' * 60)
        print('  只读 HTTP 探活（需 6002 已运行；本命令不启动/不重启服务）：')
        port_ok = probe_ports()
        print('      HTTP 探活整体：%s' % ('全绿' if port_ok else '存在失败项'))
        all_ok = all_ok and port_ok

    print('=' * 60)
    if all_ok:
        print('🎉 OpsCenter 全站 P1–P5 回归通过，安全红线保持。')
        sys.exit(0)
    print('⚠️  存在未通过阶段，请查看上方明细。')
    sys.exit(1)


if __name__ == '__main__':
    main()
