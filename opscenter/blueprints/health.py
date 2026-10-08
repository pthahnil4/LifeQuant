#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统健康检查蓝图：服务存活/内存水位/磁盘空间 + 只读 JSON API。"""
from flask import Blueprint, render_template, jsonify

from .. import config as C
from ..charts import line_chart
from ..collectors.health import get_health

health_bp = Blueprint('health', __name__)


@health_bp.route('/')
def page():
    h = get_health()
    rss_vals = [p['rss'] for p in h['memory_series']]
    chart = line_chart(rss_vals, height=200, threshold=C.RSS_KILL_MB,
                       threshold_label='危险线 %dMB' % C.RSS_KILL_MB)
    return render_template('health.html', active_page='health', h=h, chart=chart)


@health_bp.route('/data')
def data():
    return jsonify({'code': 0, 'data': get_health()})
