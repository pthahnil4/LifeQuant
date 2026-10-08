#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 2 自动搬运（2026-10-08）。
# 原名: futures/futures_blueprint.py  →  现位置: crypto/futures/routes.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底已移除。
"""
期货模块 Flask 蓝图
==================
定义所有期货相关的页面路由和 API 接口。
URL 前缀为 /futures/。
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import traceback

from flask import (Blueprint, jsonify, render_template, request,
                   current_app, Response)

# ====================================================================
#  创建蓝图
# ====================================================================
futures_bp = Blueprint(
    'futures',
    __name__,
    template_folder='templates',  # 相对于 futures/ 目录
    static_folder='static',
)


# ====================================================================
#  页面路由
# ====================================================================

@futures_bp.route('/')
def futures_index():
    """期货监控台主页"""
    return render_template('futures_index.html', active_page='futures')


@futures_bp.route('/detail/<symbol>')
def futures_detail(symbol):
    """期货品种策略详情页（回测 + 交易记录）"""
    return render_template('futures_detail.html',
                           symbol=symbol, active_page='futures')


@futures_bp.route('/multi/<symbol>')
def futures_multi_period(symbol):
    """期货品种多周期方向总览页"""
    return render_template('futures_multi_period.html',
                           symbol=symbol, active_page='futures')


@futures_bp.route('/star-market')
def star_market_page():
    """星标品种行情页面

    模板名必须带 ``futures_`` 前缀：合并后蓝图模板目录与宿主
    ``crypto/templates`` 同在 Jinja 搜索路径里，裸名 ``star_market.html`` 会被
    宿主的加密货币星标行情页遮蔽（宿主目录优先解析），页面会 200 却渲染错文件、
    取到完全不同的字段与接口。核查脚本见根目录 verify_06_template_collisions.py。
    """
    return render_template('futures_star_market.html', active_page='star-market')


# ====================================================================
#  API 路由 — 配置
# ====================================================================

@futures_bp.route('/api/config', methods=['GET'])
def api_get_config():
    """获取全部品种 + 自选品种配置"""
    try:
        from crypto.futures.futures_adapter import (get_futures_categories,
                                     get_selected_futures)
        categories = get_futures_categories()
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
                "selected": get_selected_futures(),
            }
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/config', methods=['POST'])
def api_save_config():
    """保存自选品种"""
    try:
        data = request.get_json()
        selected = data.get('selected', [])
        if not selected:
            return jsonify({
                "code": 400,
                "message": "selected 参数不能为空",
                "data": None,
            })

        from crypto.futures.futures_adapter import set_selected_futures, get_selected_futures
        set_selected_futures(selected)

        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "selected": get_selected_futures(),
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

@futures_bp.route('/api/strategy/data', methods=['GET'])
def api_strategy_data():
    """获取自选品种批量策略数据（监控台用）"""
    try:
        from crypto.futures.futures_adapter import calculate_futures_strategy_data
        data = calculate_futures_strategy_data()
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


@futures_bp.route('/api/strategy/single/<symbol>', methods=['GET'])
def api_strategy_single(symbol):
    """获取单个品种策略数据"""
    try:
        from crypto.futures.futures_adapter import calculate_futures_single
        data = calculate_futures_single(symbol)
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


@futures_bp.route('/api/strategy/detail/<symbol>', methods=['GET'])
def api_strategy_detail(symbol):
    """获取品种完整策略回测详情"""
    try:
        bar = request.args.get('bar', '1H')
        lookback = request.args.get('lookback', '1Y')
        from crypto.futures.futures_adapter import get_futures_strategy_detail
        data = get_futures_strategy_detail(symbol, bar=bar, lookback=lookback)
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


@futures_bp.route('/api/strategy/multi/<symbol>', methods=['GET'])
def api_strategy_multi(symbol):
    """获取品种多周期方向数据"""
    try:
        periods_param = request.args.get(
            'periods', '5m,15m,30m,1H,4H,1D,1W')
        periods = [p.strip() for p in periods_param.split(',') if p.strip()]

        from crypto.futures.futures_adapter import calculate_futures_multi_period
        data = calculate_futures_multi_period(symbol, periods)
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


# ====================================================================
#  API 路由 — BOLL 限价双周期策略
# ====================================================================

@futures_bp.route('/api/boll/<symbol>', methods=['GET'])
def api_boll_strategy_detail(symbol):
    """获取品种 BOLL 限价双周期策略回测详情"""
    try:
        short_bar = request.args.get('short_bar', '1H')
        long_bar = request.args.get('long_bar', '1D')
        lookback = request.args.get('lookback', '1Y')
        from crypto.futures.futures_adapter import get_futures_boll_strategy_detail
        data = get_futures_boll_strategy_detail(
            symbol, short_bar=short_bar,
            long_bar=long_bar, lookback=lookback)
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


# ====================================================================
#  API 路由 — 双周期 Pro3 策略
# ====================================================================

@futures_bp.route('/api/dualpro3/<symbol>', methods=['GET'])
def api_dual_pro3_strategy_detail(symbol):
    """获取品种双周期 Pro3 策略回测详情

    短周期交易信号 + 长周期方向过滤，只在方向一致时执行交易。
    """
    try:
        short_bar = request.args.get('short_bar', '1H')
        long_bar = request.args.get('long_bar', '1D')
        lookback = request.args.get('lookback', '1Y')
        from crypto.futures.futures_adapter import get_futures_dual_pro3_strategy_detail
        data = get_futures_dual_pro3_strategy_detail(
            symbol, short_bar=short_bar,
            long_bar=long_bar, lookback=lookback)
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


# ====================================================================
#  API 路由 — 批量多周期趋势分析
# ====================================================================

@futures_bp.route('/api/batch/trend/start', methods=['POST'])
def api_batch_trend_start():
    """启动批量趋势分析（后台线程）"""
    try:
        from crypto.futures.batch_trend_analysis import BatchTrendAnalyzer, get_progress

        # 检查是否有任务正在运行
        progress = get_progress()
        if progress['status'] == 'running':
            return jsonify({
                "code": 400,
                "message": "已有批量分析任务正在运行中，请等待完成后再启动",
                "data": progress,
            })

        analyzer = BatchTrendAnalyzer()
        analyzer.run_in_background()

        return jsonify({
            "code": 200,
            "message": "批量趋势分析已启动",
            "data": get_progress(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/batch/trend/status', methods=['GET'])
def api_batch_trend_status():
    """获取批量分析进度"""
    try:
        from crypto.futures.batch_trend_analysis import get_progress
        return jsonify({
            "code": 200,
            "message": "success",
            "data": get_progress(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/csv/data', methods=['GET'])
def api_csv_data():
    """获取完整 CSV 数据（用于表格展示）"""
    try:
        from crypto.futures.batch_trend_analysis import read_csv_for_display
        records = read_csv_for_display()

        # 统计
        up_count = sum(1 for r in records if r.get('1D_趋势') == '上涨')
        down_count = sum(1 for r in records if r.get('1D_趋势') == '下跌')
        wait_count = len(records) - up_count - down_count

        # 一致性统计 (1H/1D)
        consistent = [r for r in records
                      if r.get('1H_趋势') in ('上涨', '下跌')
                      and r.get('1H_趋势') == r.get('1D_趋势')]

        # 周线统计
        w_up = sum(1 for r in records if r.get('1W_趋势') == '上涨')
        w_down = sum(1 for r in records if r.get('1W_趋势') == '下跌')

        # 三周期一致性
        triple_consistent = [r for r in records
                             if r.get('1H_趋势') in ('上涨', '下跌')
                             and r.get('1D_趋势') in ('上涨', '下跌')
                             and r.get('1W_趋势') in ('上涨', '下跌')
                             and r.get('1H_趋势') == r.get('1D_趋势') == r.get('1W_趋势')]

        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "records": records,
                "total": len(records),
                "statistics": {
                    "up_count": up_count,
                    "down_count": down_count,
                    "wait_count": wait_count,
                    "consistent_count": len(consistent),
                    "w_up_count": w_up,
                    "w_down_count": w_down,
                    "triple_consistent_count": len(triple_consistent),
                },
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/csv/filtered', methods=['GET'])
def api_csv_filtered():
    """获取筛选后的一致性品种数据（1H 和 1D 趋势相同）"""
    try:
        from crypto.futures.batch_trend_analysis import get_filtered_records
        records = get_filtered_records()
        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "records": records,
                "total": len(records),
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


# ====================================================================
#  API 路由 — 星标品种行情
# ====================================================================

@futures_bp.route('/api/star-market/data', methods=['GET'])
def api_star_market_data():
    """读取 star品种行情.csv 数据供前端表格展示"""
    try:
        from crypto.futures.star_market import read_star_csv_for_display
        records = read_star_csv_for_display()
        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "records": records,
                "total": len(records),
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/star-market/sync', methods=['POST'])
def api_star_market_sync():
    """同步星标品种配置到 star品种行情.csv（保持行顺序）"""
    try:
        from crypto.futures.star_market import sync_starred_to_csv, read_star_csv_for_display
        count = sync_starred_to_csv()
        records = read_star_csv_for_display()
        return jsonify({
            "code": 200,
            "message": "同步完成，当前 %d 个星标品种" % count,
            "data": {
                "records": records,
                "total": len(records),
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/star-market/refresh-trend', methods=['POST'])
def api_star_market_refresh_trend():
    """启动后台刷新指定周期的趋势数据"""
    try:
        from crypto.futures.star_market import (refresh_trend_in_background,
                                  get_refresh_progress, _progress)

        # 防重入：检查是否已有刷新任务在运行
        progress = get_refresh_progress()
        if progress['status'] == 'running':
            return jsonify({
                "code": 400,
                "message": "已有刷新任务正在运行中，请等待完成",
                "data": progress,
            })

        data = request.get_json() or {}
        bar = data.get('bar', '1H')

        # 重置进度并启动后台线程
        _progress.reset()
        refresh_trend_in_background(bar)

        return jsonify({
            "code": 200,
            "message": "%s 趋势刷新已启动" % bar,
            "data": get_refresh_progress(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/star-market/refresh-all', methods=['POST'])
def api_star_market_refresh_all():
    """启动后台刷新全部周期（15m/1H/1D/1W）的趋势和行情数据"""
    try:
        from crypto.futures.star_market import (refresh_all_in_background,
                                  get_refresh_progress, _progress)

        # 防重入
        progress = get_refresh_progress()
        if progress['status'] == 'running':
            return jsonify({
                "code": 400,
                "message": "已有刷新任务正在运行中，请等待完成",
                "data": progress,
            })

        _progress.reset()
        refresh_all_in_background()

        return jsonify({
            "code": 200,
            "message": "全周期行情刷新已启动",
            "data": get_refresh_progress(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/star-market/refresh-status', methods=['GET'])
def api_star_market_refresh_status():
    """查询趋势刷新进度"""
    try:
        from crypto.futures.star_market import get_refresh_progress
        return jsonify({
            "code": 200,
            "message": "success",
            "data": get_refresh_progress(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/star-market/reorder', methods=['POST'])
def api_star_market_reorder():
    """拖拽排序后持久化新顺序"""
    try:
        from crypto.futures.star_market import reorder_csv, read_star_csv_for_display
        data = request.get_json() or {}
        order = data.get('order', [])
        if not order:
            return jsonify({
                "code": 400,
                "message": "order 参数不能为空",
                "data": None,
            })

        success = reorder_csv(order)
        if success:
            records = read_star_csv_for_display()
            return jsonify({
                "code": 200,
                "message": "排序已保存",
                "data": {
                    "records": records,
                    "total": len(records),
                },
            })
        else:
            return jsonify({
                "code": 500,
                "message": "排序保存失败，CSV 为空",
                "data": None,
            })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


# ====================================================================
#  API 路由 — 星标配置读写
# ====================================================================

@futures_bp.route('/api/starred', methods=['GET'])
def api_get_starred():
    """获取当前星标品种列表"""
    try:
        from crypto.futures.futures_adapter import get_starred_futures
        return jsonify({
            "code": 200,
            "message": "success",
            "data": {"starred": get_starred_futures()},
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/starred', methods=['POST'])
def api_save_starred():
    """保存星标品种列表"""
    try:
        from crypto.futures.futures_adapter import set_starred_futures, get_starred_futures
        data = request.get_json() or {}
        starred = data.get('starred', [])
        set_starred_futures(starred)
        return jsonify({
            "code": 200,
            "message": "success",
            "data": {"starred": get_starred_futures()},
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


# ====================================================================
#  API 路由 — 数据导出
# ====================================================================

@futures_bp.route('/api/star-market/export/csv', methods=['GET'])
def api_star_market_export_csv():
    """导出星标行情数据为 CSV 文件（UTF-8 BOM，中文不乱码）"""
    try:
        import datetime
        from crypto.futures.star_market import export_star_csv
        data = export_star_csv()
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = '星标品种行情_%s.csv' % ts
        return Response(
            data,
            mimetype='text/csv; charset=utf-8',
            headers={
                'Content-Disposition':
                    "attachment; filename*=UTF-8''" + filename,
                'Content-Length': len(data),
            },
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })


@futures_bp.route('/api/star-market/export/excel', methods=['GET'])
def api_star_market_export_excel():
    """导出星标行情数据为 Excel 文件（.xlsx）"""
    try:
        import datetime
        from crypto.futures.star_market import export_star_excel
        data = export_star_excel()
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = '星标品种行情_%s.xlsx' % ts
        return Response(
            data,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={
                'Content-Disposition':
                    "attachment; filename*=UTF-8''" + filename,
                'Content-Length': len(data),
            },
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None,
        })
