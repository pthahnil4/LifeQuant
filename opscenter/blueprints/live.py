#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘只读监控蓝图（P3 · ⑦）：只读聚合交易系统账户/持仓/委托 + 只读 JSON API。

本蓝图与所有 P3 采集一样：只发 GET、只读展示，绝不提供下单/平仓/启停入口。
"""
from flask import Blueprint, render_template, jsonify

from ..collectors.live import get_live

live_bp = Blueprint('live', __name__)


@live_bp.route('/')
def page():
    return render_template('live.html', active_page='live', v=get_live())


@live_bp.route('/data')
def data():
    return jsonify({'code': 0, 'data': get_live()})
