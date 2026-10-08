#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监控台币种 1 分钟 K 线实时拉取 —— Flask 蓝图（/kline-live）
=====================================================
- GET  /kline-live                 页面（按币种进度看板 + 最新数据表格）
- GET  /kline-live/api/status      拉取进度 + 每币种每流最新数据 + 运行流水（纯内存/文件，不触网）
- GET  /kline-live/api/coins       币种清单（监控台币种 × 流类型 × ccxt 校验结论）
- GET  /kline-live/api/bars        某条流末段 N 根 K 线（只读 CSV 尾部）
- GET  /kline-live/api/meta        数据流与可调配置项元信息（供前端渲染）
- POST /kline-live/api/pull        手动触发一轮（后台线程，与定时轮共用同一把锁）
- POST /kline-live/api/backfill/start  后台连续全量回填（可按币种，数据地板 → 现在）
- POST /kline-live/api/backfill/stop   叫停回填（游标不丢，下次接着跑）
- GET  /kline-live/api/audit       按需精确体检（整扫台账，真实行数/缺口/重复）
- POST /kline-live/api/config      改配置（币种白名单/周期/启停），并重注册任务

口径：GET 全程零业务写入、零外部请求（status/bars 只读内存状态与本地 CSV，
audit 只扫本地文件）；发起 ccxt 网络请求的路径只有任务体本身（定时轮/
手动触发）与全量回填后台线程。POST 只改 kv 配置、调度注册与回填开关，不触碰任何交易接口。
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from . import kline_live_service as svc

kline_live_bp = Blueprint('kline_live_bp', __name__)
logger = logging.getLogger(__name__)


@kline_live_bp.route('/kline-live')
def kline_live_page():
    return render_template('kline_live.html', active_page='kline-live')


@kline_live_bp.route('/kline-live/api/status', methods=['GET'])
def api_status():
    try:
        return jsonify({'success': True, **svc.get_status()})
    except Exception as e:
        logger.exception('[KlineLive] 状态读取失败')
        return jsonify({'success': False, 'error': f'状态读取失败: {e}',
                        'code': 'status_error'}), 500


@kline_live_bp.route('/kline-live/api/coins', methods=['GET'])
def api_coins():
    """币种目录：监控台有哪些币、每个币的永续/现货在 ccxt 里到底有没有。"""
    try:
        return jsonify({'success': True, 'data': svc.coin_catalog()})
    except Exception as e:
        logger.exception('[KlineLive] 币种清单读取失败')
        return jsonify({'success': False, 'error': f'币种清单读取失败: {e}',
                        'code': 'coins_error'}), 500


@kline_live_bp.route('/kline-live/api/bars', methods=['GET'])
def api_bars():
    stream = (request.args.get('stream') or 'btc:swap').strip().lower()
    try:
        limit = int(request.args.get('limit', 60))
    except (TypeError, ValueError):
        limit = 60
    try:
        return jsonify({'success': True, **svc.get_bars(stream, limit)})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e), 'code': 'invalid_param'}), 400
    except Exception as e:
        logger.exception('[KlineLive] K 线读取失败')
        return jsonify({'success': False, 'error': f'K 线读取失败: {e}',
                        'code': 'bars_error'}), 500


@kline_live_bp.route('/kline-live/api/meta', methods=['GET'])
def api_meta():
    """数据流清单 + 币种清单 + 配置项范围（前端渲染表头与配置控件用）。"""
    cfg = svc.load_config()
    catalog = svc.coin_catalog(cfg)
    return jsonify({
        'success': True,
        'streams': [{'key': v['key'], 'label': v['label'], 'sub': v['sub'],
                     'symbol': v['symbol'], 'coin': v['coin'],
                     'stream_type': v['stream_type'], 'available': v['available'],
                     'market_note': v['market_note'],
                     'enabled': v['enabled']}
                    for v in catalog['streams']],
        'coins': catalog['coins'],
        'stream_types': catalog['stream_types'],
        'markets_checked': catalog['markets_checked'],
        'config': cfg,
        'limits': {'interval_seconds': [20, 600], 'since_days': [1, 36500],
                   'round_budget_sec': [5, 3000], 'request_interval_ms': [0, 5000],
                   'max_candles_per_stream': [0, 20000000]},
        'backfill_modes': [{'value': m,
                            'label': {'earliest': '全量（数据地板 → 现在）',
                                      'days': '仅最近 N 天'}[m]}
                           for m in svc.BACKFILL_MODES],
        'job_id': svc.JOB_ID,
    })


@kline_live_bp.route('/kline-live/api/pull', methods=['POST'])
def api_pull():
    """手动拉一轮：立即返回，进度由 /api/status 轮询反映。"""
    try:
        return jsonify({'success': True, 'code': 202,
                        'message': '已提交一轮手动拉取',
                        'data': svc.trigger_pull_async()})
    except Exception as e:
        logger.exception('[KlineLive] 手动拉取启动失败')
        return jsonify({'success': False, 'error': f'手动拉取启动失败: {e}',
                        'code': 'pull_error'}), 500


@kline_live_bp.route('/kline-live/api/backfill/start', methods=['POST'])
def api_backfill_start():
    """启动全量回填（后台线程，从交易所数据地板一路推到今天），可只指定部分币种。"""
    body = request.get_json(silent=True) or {}
    keys = body.get('streams')
    if keys is not None and not isinstance(keys, list):
        return jsonify({'success': False, 'error': 'streams 需为数组',
                        'code': 'invalid_param'}), 400
    coins = body.get('coins')
    if coins is not None and not isinstance(coins, (list, str)):
        return jsonify({'success': False, 'error': 'coins 需为数组或逗号分隔字符串',
                        'code': 'invalid_param'}), 400
    try:
        budget = body.get('budget_sec')
        budget = int(budget) if budget else None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'budget_sec 需为整数',
                        'code': 'invalid_param'}), 400
    try:
        data = svc.start_full_backfill(list(keys) if keys else None, budget,
                                       coins=coins or None)
        if not data.get('started'):
            return jsonify({'success': True, 'code': 202, 'data': data,
                            'message': data.get('message')})
        scope = ('、'.join(data.get('coins') or []))[:120] or '全部可用币种'
        skip_note = (f'，已跳过 {len(data["skipped"])} 条无数据源的流'
                     if data.get('skipped') else '')
        return jsonify({'success': True, 'code': 202, 'data': data,
                        'message': f'全量回填已启动（{scope}，地板 → 现在）{skip_note}'})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e), 'code': 'invalid_param'}), 400
    except Exception as e:
        logger.exception('[KlineLive] 全量回填启动失败')
        return jsonify({'success': False, 'error': f'全量回填启动失败: {e}',
                        'code': 'backfill_error'}), 500


@kline_live_bp.route('/kline-live/api/backfill/stop', methods=['POST'])
def api_backfill_stop():
    """叫停回填（游标不丢，下次启动从原地接着跑）；可只停指定流或指定币种。"""
    body = request.get_json(silent=True) or {}
    key = (body.get('stream') or '').strip().lower() or None
    coins = body.get('coins')
    if coins is not None and not isinstance(coins, (list, str)):
        return jsonify({'success': False, 'error': 'coins 需为数组或逗号分隔字符串',
                        'code': 'invalid_param'}), 400
    try:
        return jsonify({'success': True,
                        'data': svc.stop_full_backfill(key, coins=coins or None),
                        'message': '已发出停止信号，当前批跑完就停（游标保留）'})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e), 'code': 'invalid_param'}), 400


@kline_live_bp.route('/kline-live/api/audit', methods=['GET'])
def api_audit():
    """精确体检：整扫一份台账（百万行约十几秒，只在按需调用时跑）。"""
    stream = (request.args.get('stream') or 'btc:swap').strip().lower()
    try:
        return jsonify({'success': True, 'data': svc.audit_stream(stream)})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e), 'code': 'invalid_param'}), 400
    except Exception as e:
        logger.exception('[KlineLive] 台账体检失败')
        return jsonify({'success': False, 'error': f'台账体检失败: {e}',
                        'code': 'audit_error'}), 500


@kline_live_bp.route('/kline-live/api/config', methods=['POST'])
def api_config():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not body:
        return jsonify({'success': False, 'error': '请求体需为包含配置字段的 JSON 对象',
                        'code': 'invalid_param'}), 400
    if 'coins' in body and not isinstance(body['coins'], (list, str)):
        return jsonify({'success': False, 'error': 'coins 需为币种数组或逗号分隔字符串',
                        'code': 'invalid_param'}), 400
    if 'streams' in body and not isinstance(body['streams'], list):
        return jsonify({'success': False, 'error': 'streams 需为流类型数组（swap/spot）',
                        'code': 'invalid_param'}), 400
    try:
        cfg = svc.update_live_config(body)
        return jsonify({'success': True, 'config': cfg,
                        'message': '配置已保存并重注册任务（周期变更即时生效）'})
    except Exception as e:
        logger.exception('[KlineLive] 配置保存失败')
        return jsonify({'success': False, 'error': f'配置保存失败: {e}',
                        'code': 'config_error'}), 500
