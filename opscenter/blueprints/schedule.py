#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调度可视化蓝图（P3 · ⑥）：只读展示交易系统任务调度态 + 只读 JSON API。"""
from flask import Blueprint, render_template, jsonify

from ..collectors.schedule import get_schedule

schedule_bp = Blueprint('schedule', __name__)


@schedule_bp.route('/')
def page():
    return render_template('schedule.html', active_page='schedule', s=get_schedule())


@schedule_bp.route('/data')
def data():
    return jsonify({'code': 0, 'data': get_schedule()})
