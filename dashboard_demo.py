#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运维监控中心 · 独立演示站入口
================================

这是一个**完全独立**的 Flask 应用，用于展示"个人量化运维与监控中心"的
视觉原型与设计语言，供评估是否推进整体重构。

安全边界（重要）
----------------
- 本文件**不 import crypto 包的任何模块**，尤其不碰 crypto.app —— 后者在导入
  阶段会拉起实盘调度器、内存看门狗、系统监控线程（见 crypto/SMOKE_TESTS.md
  第 163 行的同款告诫）。演示站与交易系统物理隔离，零副作用。
- 演示阶段为**纯静态展示**，不接入任何真实数据接口，不下单、不查库、不发信。
- 默认只绑定 127.0.0.1（fail-safe：与项目 web_auth 的安全默认值口径一致）。
  如需从其它设备查看，显式设置环境变量 OPSDEMO_HOST=0.0.0.0 再启动。
- 独立端口 8889，与现有交易服务互不影响；停止本站直接 Ctrl+C 即可，
  不涉及交易进程的任何重启。

启动
----
    python dashboard_demo.py
然后浏览器访问终端提示的地址（默认 http://127.0.0.1:8889）。

目录结构
--------
    dashboard_demo.py              本入口
    opscenter_demo/
        templates/base.html        应用外壳（侧栏+顶栏+内容区+页脚）
        templates/dashboard.html   系统健康概览页
        static/css/tokens.css      设计令牌
        static/css/layout.css      布局层
        static/css/components.css  组件层
        static/js/app.js           前端交互
"""
import os
import sys

# Windows 无控制台/GBK 环境下 print 含 emoji 会抛 UnicodeEncodeError，统一转 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from flask import Flask, render_template

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.join(_HERE, 'opscenter_demo')

app = Flask(
    __name__,
    template_folder=os.path.join(_PKG, 'templates'),
    static_folder=os.path.join(_PKG, 'static'),
)

# 演示站端口（可用环境变量覆盖）
DEMO_PORT = int(os.environ.get('OPSDEMO_PORT') or 8889)


@app.route('/')
@app.route('/dashboard')
def dashboard():
    """系统健康概览仪表盘（演示入口页）"""
    return render_template('dashboard.html', active_page='dashboard', demo_port=DEMO_PORT)


if __name__ == '__main__':
    host = (os.environ.get('OPSDEMO_HOST') or '127.0.0.1').strip()
    print('=' * 60)
    print('  🛰️  OpsCenter 运维监控中心 · 独立演示站')
    print('=' * 60)
    print(f'  访问地址 : http://{host if host != "0.0.0.0" else "127.0.0.1"}:{DEMO_PORT}')
    print('  数据模式 : 纯静态样例（未接入真实接口）')
    print('  安全边界 : 不 import crypto、不碰交易进程、默认只绑本机')
    print('  停止方式 : Ctrl+C（不影响交易服务）')
    print('=' * 60)
    app.run(debug=True, host=host, port=DEMO_PORT, use_reloader=False)
