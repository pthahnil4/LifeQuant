# -*- coding: utf-8 -*-
"""OKX 能力清单页 —— 本地预览入口（开发用，不启调度器/不连库）

为什么单独起一个：`crypto/app.py` 会拉起实盘调度器、DB 预热，并且 web_auth
闸门配了口令后连回环也要过口令；只想看一个静态阅读页时代价太大。这里只挂
capability_bp，用真实模板与静态目录渲染。

    python data/_run_cap_preview.py            # 默认 127.0.0.1:7791
    $env:CAP_PREVIEW_PORT='7792'; python data/_run_cap_preview.py

已知噪音：页面全站共用的 nav.html 里 discipline.js 每 60s 轮询
/plan/api/discipline/status，本进程没挂那个蓝图 → 控制台会报 404。
这是预览进程的固有现象，不是页面缺陷（真 app 上该路由存在）。
导航条里除 /okx-capability 之外的链接在此预览下也会 404。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from flask import Flask  # noqa: E402
from crypto.capability_routes import capability_bp  # noqa: E402

app = Flask(__name__,
            template_folder=os.path.join(_ROOT, 'crypto', 'templates'),
            static_folder=os.path.join(_ROOT, 'crypto', 'static'))
app.register_blueprint(capability_bp)

if __name__ == '__main__':
    port = int(os.environ.get('CAP_PREVIEW_PORT') or 7791)
    print('preview -> http://127.0.0.1:%d/okx-capability' % port)
    app.run(host='127.0.0.1', port=port, debug=False)
