#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内存治理蓝图（P2）：RSS/系统内存趋势 + 阈值告警 + 自愈事件 + 只读 JSON API。"""
from flask import Blueprint, render_template, jsonify

from .. import config as C
from ..charts import line_chart
from ..collectors.memory import get_memory

memory_bp = Blueprint('memory', __name__)


def _charts(m):
    rss_vals = [p['v'] for p in m['rss_series']]
    mem_vals = [p['mem'] for p in m['sys_series'] if p.get('mem') is not None]
    rss_chart = line_chart(rss_vals, height=240, thresholds=[
        {'value': C.RSS_WARN_MB, 'label': '关注 %dMB' % C.RSS_WARN_MB, 'color': 'warning'},
        {'value': C.RSS_KILL_MB, 'label': '危险 %dMB' % C.RSS_KILL_MB, 'color': 'danger'},
    ])
    mem_chart = line_chart(mem_vals, height=240, thresholds=[
        {'value': C.MEM_PCT_DANGER, 'label': 'OOM %d%%' % int(C.MEM_PCT_DANGER), 'color': 'danger'},
    ])
    return rss_chart, mem_chart


@memory_bp.route('/')
def page():
    m = get_memory()
    rss_chart, mem_chart = _charts(m)
    return render_template('memory.html', active_page='memory',
                           m=m, rss_chart=rss_chart, mem_chart=mem_chart)


@memory_bp.route('/data')
def data():
    return jsonify({'code': 0, 'data': get_memory()})
