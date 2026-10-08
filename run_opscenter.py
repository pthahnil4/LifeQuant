#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpsCenter 正式站 · 启动入口（P1）
==================================
独立运行的运维监控中心，与交易系统（crypto/，默认 Web 端口 7777）同级、物理隔离。

安全边界
--------
- 只 import opscenter 自身，绝不 import crypto.app / crypto.task.scheduler
  （它们导入即拉起调度器/看门狗/监控后台线程）。
- 采集层只读交易系统落盘产物（memory_history.jsonl / boot_ledger.jsonl 等），
  不写任何交易文件、不发邮件、不下单。
- 默认只绑 127.0.0.1（fail-safe）；远程查看需显式设 OPSCENTER_HOST=0.0.0.0。

启动
----
    python run_opscenter.py                 # 默认 http://127.0.0.1:6002
    $env:OPSCENTER_PORT='7000'; python run_opscenter.py    # 换端口（PowerShell）
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from opscenter import create_app

app = create_app()

if __name__ == '__main__':
    host = (os.environ.get('OPSCENTER_HOST') or '127.0.0.1').strip()
    port = int(os.environ.get('OPSCENTER_PORT') or 6002)
    from opscenter import config as C

    print('=' * 64)
    print('  🛰️  OpsCenter · 个人量化运维与监控中心（正式站 P5 · 全站完成）')
    print('=' * 64)
    print(f'  访问地址 : http://{host if host != "0.0.0.0" else "127.0.0.1"}:{port}')
    print(f'  数据源   : {C.LOG_DIR}  （只读）')
    print(f'  交易API  : {C.TRADING_API_BASE}  （运行态全景只读 HTTP GET 调用）')
    print('  安全边界 : 不 import crypto、只读产物/GET/盘点、默认仅本机、独立端口')
    print(f'  停止方式 : Ctrl+C（不影响交易系统 {C.TRADING_API_BASE}）')
    print('=' * 64)
    # 生产请换 waitress/gunicorn；此处开发预览
    app.run(debug=False, host=host, port=port, threaded=True)
