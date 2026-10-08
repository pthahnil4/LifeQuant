#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 3 自动搬运（2026-10-08）。
# 原名: stocks/stocks_blueprint.py  →  现位置: crypto/stocks/routes.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底与根级 config 依赖已移除。
"""
股票模块 Flask 蓝图
==================
定义所有股票相关的页面路由和 API 接口。
URL 前缀为 /stocks/。

支持两种策略模式：
  - Pro3 策略（默认）：MACD平滑 + ADX自适应，含完整回测和交易记录
  - 传统指标：MACD + RSI + MA + BOLL，基础信号分析

通过 ?strategy=traditional 参数切换到传统指标模式。
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import traceback

from flask import (Blueprint, jsonify, render_template, request,
                   current_app)

# ====================================================================
#  创建蓝图
# ====================================================================
stocks_bp = Blueprint(
    'stocks',
    __name__,
    template_folder='templates',  # 相对于 stocks/ 目录
    static_folder='static',
)


# ====================================================================
#  页面路由
# ====================================================================

@stocks_bp.route('/')
def stocks_index():
    """股票监控台主页"""
    return render_template('stocks_index.html', active_page='stocks')


@stocks_bp.route('/detail/<code>')
def stocks_detail(code):
    """股票策略详情页（回测 + 交易记录）"""
    return render_template('stocks_detail.html',
                           code=code, active_page='stocks')


@stocks_bp.route('/multi/<code>')
def stocks_multi_period(code):
    """股票多周期方向总览页"""
    return render_template('stocks_multi_period.html',
                           code=code, active_page='stocks')


# ====================================================================
#  API 路由 — 配置
# ====================================================================

@stocks_bp.route('/api/config', methods=['GET'])
def api_get_config():
    """获取全部股票 + 自选股配置"""
    try:
        from crypto.stocks.stock_adapter import (get_stock_categories,
                                   get_selected_stocks)
        categories = get_stock_categories()
        # 转换为前端友好的格式
        category_list = []
        for cat_name, products in categories.items():
            category_list.append({
                "name": cat_name,
                "products": products,
            })

        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "categories": category_list,
                "selected": get_selected_stocks(),
            }
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@stocks_bp.route('/api/config', methods=['POST'])
def api_save_config():
    """保存自选股"""
    try:
        data = request.get_json()
        selected = data.get('selected', [])
        if not selected:
            return jsonify({
                "code": 400,
                "message": "selected 参数不能为空",
                "data": None,
            })

        from crypto.stocks.stock_adapter import set_selected_stocks, get_selected_stocks
        set_selected_stocks(selected)

        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "selected": get_selected_stocks(),
            }
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


# ====================================================================
#  API 路由 — 策略数据
# ====================================================================

def _get_strategy_mode():
    """从请求参数中获取策略模式，默认 pro3"""
    return request.args.get('strategy', 'pro3')


def _calculate_stock_pro3_batch():
    """使用 Pro3 策略批量获取自选股数据（监控台用）"""
    from crypto.stocks.stock_adapter import (
        get_selected_stocks, get_all_stocks, get_stock_code_name_map,
        calculate_stock_pro3_single,
    )
    selected = get_selected_stocks()
    if not selected:
        all_stocks = get_all_stocks()
        selected = all_stocks[:6]

    results = []
    up_count = 0
    down_count = 0
    wait_count = 0
    error_count = 0

    for code in selected:
        try:
            data = calculate_stock_pro3_single(code, long_only=False)
            flag = data['analysis']['modify_flag']
            if flag == 'rise':
                up_count += 1
            elif flag == 'fall':
                down_count += 1
            else:
                wait_count += 1
            results.append(data)
        except Exception as e:
            error_count += 1
            name_map = get_stock_code_name_map()
            display_name = name_map.get(code, code)
            results.append({
                "symbol": code,
                "display_name": display_name,
                "price": 0,
                "indicators": {
                    "macd": {"histogram": 0, "dif": 0, "dea": 0, "smoothed": 0},
                    "adx": {"adx": 0, "plus_di": 0, "minus_di": 0},
                    "adaptive_weight": 0,
                },
                "analysis": {
                    "overall_market": "数据获取失败",
                    "action_signal": "--",
                    "modify_flag": "wait",
                },
                "error": str(e),
            })

    total = len(selected) if len(selected) > 0 else 1
    if up_count > total * 0.6:
        overall_market = (f"股票池整体偏多 "
                          f"(看多:{up_count} 看空:{down_count} "
                          f"观望:{wait_count} 错误:{error_count})")
    elif down_count > total * 0.6:
        overall_market = (f"股票池整体偏空 "
                          f"(看多:{up_count} 看空:{down_count} "
                          f"观望:{wait_count} 错误:{error_count})")
    else:
        overall_market = (f"股票池走势分化/震荡 "
                          f"(看多:{up_count} 看空:{down_count} "
                          f"观望:{wait_count} 错误:{error_count})")

    return {
        "overall_market": overall_market,
        "products": results,
    }


@stocks_bp.route('/api/strategy/data', methods=['GET'])
def api_strategy_data():
    """获取自选股批量策略数据（监控台用）

    Query params:
        strategy: 'pro3'（默认）或 'traditional'
    """
    try:
        mode = _get_strategy_mode()
        if mode == 'traditional':
            from crypto.stocks.stock_adapter import calculate_stock_strategy_data
            data = calculate_stock_strategy_data()
        else:
            data = _calculate_stock_pro3_batch()

        return jsonify({
            "code": 200,
            "message": "success",
            "data": data,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@stocks_bp.route('/api/strategy/single/<code>', methods=['GET'])
def api_strategy_single(code):
    """获取单只股票策略数据

    Query params:
        strategy: 'pro3'（默认）或 'traditional'
    """
    try:
        mode = _get_strategy_mode()
        if mode == 'traditional':
            from crypto.stocks.stock_adapter import calculate_stock_single
            data = calculate_stock_single(code)
        else:
            from crypto.stocks.stock_adapter import calculate_stock_pro3_single
            data = calculate_stock_pro3_single(code, long_only=False)
        return jsonify({
            "code": 200,
            "message": "success",
            "data": data,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@stocks_bp.route('/api/strategy/detail/<code>', methods=['GET'])
def api_strategy_detail(code):
    """获取股票完整策略回测详情

    Query params:
        bar: K线周期，如 '1H', 'daily', 'weekly'（默认 'daily'）
        lookback: 回看范围，如 '30D', '3M', '6M', '1Y', '3Y', '5Y'（默认 '1Y'）
        strategy: 'pro3'（默认）或 'traditional'
    """
    try:
        bar = request.args.get('bar', 'daily')
        lookback = request.args.get('lookback', '1Y')
        mode = _get_strategy_mode()
        if mode == 'traditional':
            from crypto.stocks.stock_adapter import get_stock_strategy_detail
            data = get_stock_strategy_detail(code, bar=bar, lookback=lookback)
        else:
            from crypto.stocks.stock_adapter import get_stock_pro3_strategy_detail
            data = get_stock_pro3_strategy_detail(code, bar=bar, lookback=lookback, long_only=False)
        return jsonify({
            "code": 200,
            "message": "success",
            "data": data,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@stocks_bp.route('/api/strategy/multi/<code>', methods=['GET'])
def api_strategy_multi(code):
    """获取股票多周期方向数据

    Query params:
        periods: 周期列表，逗号分隔，如 '5m,15m,1H,6H,1D,1W'（默认 '5m,15m,30m,1H,6H,1D,1W'）
        strategy: 'pro3'（默认）或 'traditional'
    """
    try:
        periods_param = request.args.get(
            'periods', '5m,15m,30m,1H,4H,1D')
        periods = [p.strip() for p in periods_param.split(',') if p.strip()]
        mode = _get_strategy_mode()
        if mode == 'traditional':
            from crypto.stocks.stock_adapter import calculate_stock_multi_period
            data = calculate_stock_multi_period(code, periods)
        else:
            from crypto.stocks.stock_adapter import calculate_stock_pro3_multi_period
            data = calculate_stock_pro3_multi_period(code, periods, long_only=False)
        return jsonify({
            "code": 200,
            "message": "success",
            "data": data,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })
