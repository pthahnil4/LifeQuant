# -*- coding: utf-8 -*-
"""任务卡页面渲染验证用预览实例（仅本机回环、不注册鉴权闸门、不启动任何定时任务）

用途：主实例 7777 有口令闸门，浏览器自动化验证需要看真实 DOM。这里只挂
plan/discipline 两个只读相关蓝图，绑定 127.0.0.1:7799，页面行为与主实例一致，
但不会拉起交易调度器。验证结束直接关掉本进程即可。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402

from crypto.plan_routes import plan_bp  # noqa: E402
from crypto.discipline_routes import discipline_bp  # noqa: E402

app = Flask(__name__,
            template_folder=os.path.join(ROOT, 'crypto', 'templates'),
            static_folder=os.path.join(ROOT, 'crypto', 'static'))
app.register_blueprint(plan_bp)
app.register_blueprint(discipline_bp)

# ---- 只读闸门 ------------------------------------------------------------------
# 本实例只用来看 DOM，plan_bp 里的写接口（fill-slot / update-card / settle-round …）
# 全都连着远端正式库，一旦自动化脚本手滑点了「勾选/保存」就会污染真实数据。
# 这里直接把非只读请求挡在路由之前：验证脚本的"零写库"从行为约定变成物理不可能。
# 唯一放行的是 /plan/api/account-balance——它只是用 POST 传参，服务端仅调 OKX 查询接口。
_READ_ONLY_POSTS = {'/plan/api/account-balance'}


@app.before_request
def _block_writes():
    from flask import request, jsonify
    if request.method in ('GET', 'HEAD', 'OPTIONS') or request.path in _READ_ONLY_POSTS:
        return None
    return jsonify({'code': 403, 'message': '预览实例只读，写接口已禁用', 'data': None}), 403


@app.route('/')
def _home():
    return '<a href="/plan">/plan</a>'


@app.route('/plan')
def _plan_page():
    # 正式页面路由定义在 crypto/app.py（不在蓝图内），这里等价复刻一份供 DOM 验证
    from flask import render_template
    return render_template('task_plan.html', active_page='plan')

if __name__ == '__main__':
    print('[preview] http://127.0.0.1:7799/plan', flush=True)
    app.run(host='127.0.0.1', port=7799, debug=False, threaded=True, use_reloader=False)
