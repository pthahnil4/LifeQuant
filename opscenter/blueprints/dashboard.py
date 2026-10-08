#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仪表盘蓝图：真实数据总览 + 只读 JSON API。"""
from flask import Blueprint, render_template, jsonify

from .. import config as C
from ..charts import line_chart
from ..collectors.health import get_health
from ..collectors.lifecycle import get_lifecycle

dashboard_bp = Blueprint('dashboard', __name__)

_RING_CIRC = 2 * 3.141592653589793 * 56   # 评分环半径 56 的周长


def _spark(values, w=100, h=28):
    """把序列缩放到 w×h 的 polyline 点串（用于磁贴迷你图）。"""
    vals = [v for v in values if isinstance(v, (int, float))]
    if len(vals) < 2:
        return ''
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = w * i / (n - 1)
        y = h - (h - 4) * (v - lo) / span - 2
        pts.append('%.1f,%.1f' % (x, y))
    return ' '.join(pts)


def _build_context():
    h = get_health()
    lc = get_lifecycle()
    rss_vals = [p['rss'] for p in h['memory_series']]
    chart = line_chart(rss_vals, threshold=C.RSS_KILL_MB,
                       threshold_label='危险线 %dMB' % C.RSS_KILL_MB)
    return {
        'h': h, 'lc': lc, 'chart': chart,
        'rss_spark': _spark(rss_vals),
        'ring_offset': round(_RING_CIRC * (1 - h['score'] / 100.0), 1),
        'ring_circ': round(_RING_CIRC, 1),
    }


@dashboard_bp.route('/')
def index():
    return render_template('dashboard.html', active_page='dashboard', **_build_context())


@dashboard_bp.route('/api/dashboard')
def api_dashboard():
    h = get_health()
    lc = get_lifecycle()
    return jsonify({
        'code': 0,
        'data': {
            'score': h['score'], 'process': h['process'],
            'rss_mb': h['rss_mb'], 'mem_usage_pct': h['mem_usage_pct'],
            'disk': h['disk'], 'issues': h['issues'][:8],
            'today_restarts': lc['today_restarts'], 'running_for': lc['running_for'],
        },
    })
