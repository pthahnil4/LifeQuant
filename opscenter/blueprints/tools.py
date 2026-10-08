#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运维工具箱蓝图（P4）：冒烟运行器 / 诊断脚本 / 配置总览。

全部只读盘点；唯一有动作的是 /smoke/run/<key>，且严格限定运维站自身白名单自检冒烟
（不 import crypto、只用 test_client），交易系统的冒烟/诊断脚本只列清单一律不代跑。
"""
from flask import Blueprint, render_template, jsonify

from ..collectors import tools as T

tools_bp = Blueprint('tools', __name__)


@tools_bp.route('/smoke')
def smoke_page():
    return render_template('smoke.html', active_page='smoke', s=T.get_smoke())


@tools_bp.route('/smoke/data')
def smoke_data():
    return jsonify({'code': 0, 'data': T.get_smoke()})


@tools_bp.route('/smoke/run/<key>', methods=['GET', 'POST'])
def smoke_run(key):
    res = T.run_self_smoke(key)
    return jsonify({'code': 0 if res['ok'] else 1, 'data': res})


@tools_bp.route('/diag')
def diag_page():
    return render_template('diag.html', active_page='diag', d=T.get_diag())


@tools_bp.route('/diag/data')
def diag_data():
    return jsonify({'code': 0, 'data': T.get_diag()})


@tools_bp.route('/config')
def config_page():
    return render_template('config.html', active_page='config', c=T.get_config())


@tools_bp.route('/config/data')
def config_data():
    return jsonify({'code': 0, 'data': T.get_config()})
