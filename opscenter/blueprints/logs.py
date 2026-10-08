#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志聚合蓝图（P2）：多源合并 + 级别/关键词检索 + tail 增量翻页 + 只读 API。"""
from flask import Blueprint, render_template, jsonify, request

from .. import config as C
from ..collectors import logs as L

logs_bp = Blueprint('logs', __name__)


@logs_bp.route('/')
def page():
    ov = L.overview()
    initial = L.query(limit=C.LOG_DEFAULT_LIMIT)
    return render_template('logs.html', active_page='logs', ov=ov, initial=initial)


@logs_bp.route('/data')
def data():
    res = L.query(
        level=request.args.get('level', ''),
        q=request.args.get('q', ''),
        source=request.args.get('source', ''),
        coin=request.args.get('coin', ''),
        limit=request.args.get('limit', C.LOG_DEFAULT_LIMIT),
        before=request.args.get('before'),
    )
    return jsonify({'code': 0, 'data': res})


@logs_bp.route('/meta')
def meta():
    return jsonify({'code': 0, 'data': L.overview()})
