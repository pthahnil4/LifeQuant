#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指标导航 —— Flask 蓝图（可视化回测页）
==========================================
- /indicator-nav              页面（长周期趋势方法对比回测）
- /indicator-nav/api/meta     指标清单与周期档位（纯内存，不触网）
- /indicator-nav/api/backtest 单次回测（GET，只读：零 DB 写入、零交易接口）

数据与计算全部在 indicator_nav_service：CSV 优先，缺 CSV 时回退 OKX 公共
行情（无密钥、限速、带缓存）。异常映射：参数错 400，数据/依赖故障 502。
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from . import indicator_nav_service as svc

indicator_nav_bp = Blueprint('indicator_nav_bp', __name__)
logger = logging.getLogger(__name__)


@indicator_nav_bp.route('/indicator-nav')
def indicator_nav_page():
    """指标导航页"""
    return render_template('indicator_nav.html', active_page='indicator-nav')


@indicator_nav_bp.route('/indicator-nav/api/meta', methods=['GET'])
def api_meta():
    """指标注册表 + 周期档位（下拉菜单数据源，不触网不读库）"""
    return jsonify({
        'success': True,
        'indicators': svc.list_indicators(),
        'short_bar_options': svc.SHORT_BAR_OPTIONS,
        'long_bar_options': svc.LONG_BAR_OPTIONS,
    })


@indicator_nav_bp.route('/indicator-nav/api/compare', methods=['GET'])
def api_compare():
    """指标横向对比：全部注册表方法在同一长周期数据上的趋势段统计。"""
    args = request.args
    try:
        result = svc.compare_indicators(symbol=args.get('symbol', 'near'),
                                        long_bar=args.get('long_bar', '4H'))
        return jsonify({'success': True, **result})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e), 'code': 'invalid_param'}), 400
    except Exception as e:
        logger.exception('[IndicatorNav] 指标对比失败')
        return jsonify({'success': False, 'error': f'指标对比失败: {e}', 'code': 'compare_error'}), 502


@indicator_nav_bp.route('/indicator-nav/api/backtest', methods=['GET'])
def api_backtest():
    """单次回测：query 参数 symbol/short_bar/long_bar/indicator/leverage_times/
    entry_atr_mult/exit_atr_mult/chart_bars/view，全部可选（有默认值）。
    无论 view 为何，响应同时携带 chart（短周期）与 chart_long（长周期）两份载荷。"""
    args = request.args
    try:
        result = svc.run_backtest(
            symbol=args.get('symbol', 'near'),
            short_bar=args.get('short_bar', '15m'),
            long_bar=args.get('long_bar', '4H'),
            indicator=args.get('indicator', 'keltner'),
            leverage_times=args.get('leverage_times', 10.0),
            entry_atr_mult=args.get('entry_atr_mult', 0.0),
            exit_atr_mult=args.get('exit_atr_mult', 0.0),
            chart_bars=args.get('chart_bars', svc._CHART_MAX_BARS),
            view=args.get('view', 'short'),
        )
        return jsonify({'success': True, **result})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e), 'code': 'invalid_param'}), 400
    except Exception as e:
        logger.exception('[IndicatorNav] 回测失败')
        return jsonify({'success': False, 'error': f'回测失败: {e}', 'code': 'backtest_error'}), 502
