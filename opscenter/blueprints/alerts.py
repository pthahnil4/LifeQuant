#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警中心蓝图（P5）：统一告警流（交易侧行情/盈亏 + 系统侧内存/自愈）+ 只读 API。

全部只读：交易侧经 GET 调 alert_routes，系统侧读本地文件；不提供存配置/手动检测入口。
"""
from flask import Blueprint, render_template, jsonify

from ..collectors.alerts import get_alerts

alerts_bp = Blueprint('alerts', __name__)


@alerts_bp.route('/')
def page():
    return render_template('alerts.html', active_page='alerts', a=get_alerts())


@alerts_bp.route('/data')
def data():
    return jsonify({'code': 0, 'data': get_alerts()})
