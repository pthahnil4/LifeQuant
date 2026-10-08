#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程生命周期蓝图：启动归因时间线 + 自愈记录 + 只读 JSON API。"""
from flask import Blueprint, render_template, jsonify

from ..collectors.lifecycle import get_lifecycle

lifecycle_bp = Blueprint('lifecycle', __name__)


@lifecycle_bp.route('/')
def page():
    lc = get_lifecycle()
    return render_template('lifecycle.html', active_page='lifecycle', lc=lc)


@lifecycle_bp.route('/data')
def data():
    return jsonify({'code': 0, 'data': get_lifecycle()})
