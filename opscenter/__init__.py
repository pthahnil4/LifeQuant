#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpsCenter · 个人量化运维与监控中心（正式站，P5）
=================================================
独立 Flask 应用工厂。与交易系统物理隔离：

- 不 import crypto 任何模块（其 app/scheduler 导入即拉起后台线程）；
- 采集层只读交易系统落盘的产物（memory_history.jsonl / boot_ledger.jsonl 等）；
- 运行态全景（P3）以只读 HTTP 客户端调用交易系统 Web API（默认 127.0.0.1:7777），
  绝不 import、绝不 POST、绝不自行交易；
- 运维工具箱（P4）只读盘点冒烟/诊断脚本与配置（ast 解析 + 脱敏），交易脚本一律不代跑，
  仅运维站自身自检冒烟可一键运行（无副作用、不 import crypto）。

create_app() 内延迟导入蓝图与依赖，保证 `import opscenter` 零副作用。
"""
import os

from flask import Flask


def create_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'),
        static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'),
    )
    app.config['JSON_AS_ASCII'] = False
    # 允许 /health 与 /health/ 同义（蓝图 url_prefix + route('/') 规范名带尾斜杠，
    # 关闭严格斜杠可省去 308 重定向，导航直接命中）
    app.url_map.strict_slashes = False

    # 顶栏健康灯等全局上下文
    from .context import inject_globals
    inject_globals(app)

    # 注册路由蓝图（P1：仪表盘 / 健康检查 / 进程生命周期；P2：内存治理 / 日志聚合；
    # P3：调度可视化 / 实盘只读监控；P4：运维工具箱；P5：告警中心）
    from .blueprints.dashboard import dashboard_bp
    from .blueprints.health import health_bp
    from .blueprints.lifecycle import lifecycle_bp
    from .blueprints.memory import memory_bp
    from .blueprints.logs import logs_bp
    from .blueprints.schedule import schedule_bp
    from .blueprints.live import live_bp
    from .blueprints.tools import tools_bp
    from .blueprints.alerts import alerts_bp
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(health_bp, url_prefix='/health')
    app.register_blueprint(lifecycle_bp, url_prefix='/lifecycle')
    app.register_blueprint(memory_bp, url_prefix='/memory')
    app.register_blueprint(logs_bp, url_prefix='/logs')
    app.register_blueprint(schedule_bp, url_prefix='/schedule')
    app.register_blueprint(live_bp, url_prefix='/live')
    app.register_blueprint(tools_bp, url_prefix='/tools')
    app.register_blueprint(alerts_bp, url_prefix='/alerts')

    return app
