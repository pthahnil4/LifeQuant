#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析纪律 - Flask 蓝图（批次11）
==================================
所有 /plan/api/discipline/* 与 /plan/api/analysis-gate 路由：

- status            前端轮询唯一入口（当小时进度 + 今日合规 + streak + 打扰开关）
- config GET/POST   纪律配置读取/保存（保存后热重注册巡检任务）
- analysis-gate     打卡前闸门判定（返回合格性 + 该小时分析明细，供弹窗渲染状态条）
- board             看板：合规率曲线 / 24×7 断档热力 / streak / 补记率 / 归因对比
- check-now         手动跑一轮巡检（联调验证，仿 alert 的 run-now）
- exempt            一次性豁免 N 小时（出差/不盯盘的泄压阀，避免规则被整体关掉）
- backfill-preview  回溯分析取数：为过去的小时槽返回当时的真实价格（不写库）

路由挂在 /plan/api/* 下是因为闸门服务的是任务计划打卡；实现独立成蓝图，
避免 plan_routes.py（已 1800+ 行）继续膨胀。回溯分析的写入复用既有的
POST /api/task/analysis/records_batch（source 由服务端自动判为 backfill）。
"""

import logging
import datetime
from flask import Blueprint, jsonify, request

from .database import session_scope
from . import discipline_repo as disc
from . import analysis_record_repo as ana_repo

logger = logging.getLogger(__name__)

discipline_bp = Blueprint('discipline_bp', __name__)

_STRICT_MODES = ('strict', 'soft', 'off')


def _num(value, default, minimum=None, maximum=None):
    """数值字段容错转换：非法值回退默认，可选上下限夹紧（与 alert_routes 同款）"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    if minimum is not None:
        v = max(minimum, v)
    if maximum is not None:
        v = min(maximum, v)
    return v


def _parse_active_hours_str(raw, default):
    """生效时段校验：必须是 'HH:MM-HH:MM'（右端开区间，24:00=午夜）且可解析，否则回退默认"""
    s = str(raw or '').strip()
    if not s:
        return default
    return s if disc.parse_active_hours(s) is not None else default


def _valid_hhmm(s) -> bool:
    """空串（= 不发日报）或 'HH:MM' 视为合法"""
    if not s:
        return True
    if len(s) != 5 or s[2] != ':':
        return False
    try:
        h, m = int(s[:2]), int(s[3:])
    except ValueError:
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


def _parse_slot_list(raw, limit=31):
    """小时槽清单解析：支持数组或逗号/换行分隔字符串，归一去重限量"""
    if isinstance(raw, str):
        parts = raw.replace('\n', ',').split(',')
    elif isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = []
    seen, result = set(), []
    for p in parts:
        slot = disc.slot_from_str(p)
        if slot and slot not in seen:
            seen.add(slot)
            result.append(slot)
        if len(result) >= limit:
            break
    return sorted(result)


# =============================================================================
# 前端轮询状态 / 打卡闸门
# =============================================================================

@discipline_bp.route('/plan/api/discipline/status', methods=['GET'])
def api_discipline_status():
    """纪律状态（前端 60s 轮询唯一入口）：当小时进度 + 今日合规 + streak"""
    try:
        with session_scope() as s:
            data = disc.build_status(s)
        return jsonify({'code': 200, 'message': 'success', 'data': data})
    except Exception as e:
        # 状态接口失败不能影响页面：返回 enabled=False 让前端静默降级
        logger.error(f"[Discipline] 状态查询失败: {e}", exc_info=True)
        return jsonify({'code': 200, 'message': str(e),
                        'data': {'enabled': False, 'ok': True, 'banner': False}})


@discipline_bp.route('/plan/api/analysis-gate', methods=['GET'])
def api_analysis_gate():
    """打卡前闸门判定：?filled_at=YYYY-MM-DD HH:MM[:SS]（缺省=当前小时）

    返回 {allowed, hour_slot, required, actual, ok, mode, reason, records}，
    前端据此在打卡弹窗顶部渲染放行/拦截状态条。
    """
    try:
        filled_at = request.args.get('filled_at', '').strip()
        cfg = disc.load_config()
        target = disc.parse_dt(filled_at) if filled_at else datetime.datetime.now()
        with session_scope() as s:
            gate = disc.gate_check(s, target, cfg)
        ev = gate.get('evaluation') or {}
        # enabled 走总闸口径（业务 kv × /task 页任务开关）：任一层关→前端隐藏状态条
        return jsonify({'code': 200, 'message': 'success', 'data': {
            'enabled': disc.module_enabled(cfg),
            'allowed': bool(gate.get('allowed')),
            'mode': gate.get('mode'),
            'reason': gate.get('reason', ''),
            'hour_slot': gate.get('hour_slot', ''),
            'required': ev.get('required', 1),
            'actual': ev.get('actual', 0),
            'ok': bool(ev.get('ok')),
            'active': bool(ev.get('active')),
            'missing_coins': ev.get('missing_coins') or [],
            'covered': ev.get('covered') or [],
            'backfill_count': ev.get('backfill_count', 0),
            'records': [{
                'id': r['id'], 'ts': r['ts'], 'inst_id': r['inst_id'],
                'price': r.get('price'), 'user_judgment': r.get('user_judgment', ''),
                'user_reason': r.get('user_reason', ''), 'source': r.get('source', 'live'),
            } for r in (ev.get('records') or [])],
        }})
    except Exception as e:
        # 闸门接口异常同样 fail-open，与后端写入口径一致
        logger.error(f"[Discipline] 闸门判定失败: {e}", exc_info=True)
        return jsonify({'code': 200, 'message': str(e),
                        'data': {'enabled': False, 'allowed': True, 'ok': True}})


# =============================================================================
# 配置
# =============================================================================

@discipline_bp.route('/plan/api/discipline/config', methods=['GET'])
def api_discipline_config_get():
    """读取纪律配置（未保存过返回默认值）"""
    try:
        return jsonify({'code': 200, 'message': 'success', 'data': disc.load_config()})
    except Exception as e:
        logger.error(f"[Discipline] 读取配置失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'读取配置失败: {e}', 'data': None})


@discipline_bp.route('/plan/api/discipline/config', methods=['POST'])
def api_discipline_config_save():
    """保存纪律配置并热重注册巡检任务"""
    try:
        payload = request.get_json(silent=True) or {}
        cfg = disc.load_config()

        cfg['enabled'] = bool(payload.get('enabled', cfg.get('enabled')))
        cfg['required_count'] = int(_num(payload.get('required_count'),
                                         cfg.get('required_count', 1), 1, 50))
        cfg['require_cover_tracked'] = bool(payload.get(
            'require_cover_tracked', cfg.get('require_cover_tracked')))
        cfg['grace_minutes'] = int(_num(payload.get('grace_minutes'),
                                        cfg.get('grace_minutes', 15), 0, 180))
        # 生效时段不能静默回退：它直接决定「一天里有多少小时被追责」，
        # 以为自己填的值已生效、实际被换成了另一个，后果是整晚被拦。
        ah_in = str(payload.get('active_hours') or '').strip()
        ah = _parse_active_hours_str(ah_in, cfg.get('active_hours', disc.DEFAULT_ACTIVE_HOURS))
        note = (f'生效时段“{ah_in}”无法解析，已按“{ah}”保存'
                if ah_in and ah != ah_in else '')
        cfg['active_hours'] = ah

        mode = str(payload.get('strict_mode', cfg.get('strict_mode', 'strict'))).strip().lower()
        cfg['strict_mode'] = mode if mode in _STRICT_MODES else 'strict'

        email_in = payload.get('email') or {}
        email = cfg.setdefault('email', {})
        # 邮件打扰总开关：只停发信，不停台账判定与闸门（两者口径独立）
        email['enabled'] = bool(email_in.get('enabled', email.get('enabled', True)))
        email['on_gap'] = bool(email_in.get('on_gap', email.get('on_gap')))
        email['merge_after'] = int(_num(email_in.get('merge_after'),
                                        email.get('merge_after', 2), 1, 24))
        report = str(email_in.get('daily_report', email.get('daily_report', '23:00'))).strip()
        email['daily_report'] = report if _valid_hhmm(report) else '23:00'

        browser_in = payload.get('browser') or {}
        browser = cfg.setdefault('browser', {})
        for key in ('banner', 'sound', 'desktop_notify'):
            browser[key] = bool(browser_in.get(key, browser.get(key)))
        browser['poll_seconds'] = int(_num(browser_in.get('poll_seconds'),
                                           browser.get('poll_seconds', 60), 15, 600))
        browser['banner_after_minutes'] = int(_num(
            browser_in.get('banner_after_minutes'),
            browser.get('banner_after_minutes', 20), 0, 59))

        disc.save_config(cfg)

        # 热重注册巡检任务（开关/周期变化立即生效）
        try:
            from .task.monitor.analysis_discipline import register_discipline_job
            interval = register_discipline_job()
            msg = f'配置已保存，巡检任务已按 {interval // 60} 分钟周期重注册'
        except Exception as e:
            msg = f'配置已保存，但巡检任务重注册失败: {e}'
        if note:
            msg = f'{note}；{msg}'
            logger.warning(f'[Discipline] {note}')
        return jsonify({'code': 200, 'message': msg, 'data': cfg})
    except Exception as e:
        logger.error(f"[Discipline] 保存配置失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'保存配置失败: {e}', 'data': None})


# =============================================================================
# 豁免 / 手动巡检
# =============================================================================

@discipline_bp.route('/plan/api/discipline/exempt', methods=['POST'])
def api_discipline_exempt():
    """一次性豁免：?hours=N（0 = 取消豁免）

    豁免期内所有槽不追责、不提醒、闸门放行。存在的意义是给出泄压阀——
    有豁免可用，才不会因为几天特殊情况而把整个功能关掉。
    """
    try:
        payload = request.get_json(silent=True) or {}
        hours = _num(payload.get('hours', request.args.get('hours')), 0, 0, 24 * 14)
        until = disc.set_exempt(hours)
        msg = f'已豁免至 {until}' if until else '豁免已取消'
        return jsonify({'code': 200, 'message': msg, 'data': {'exempt_until': until}})
    except Exception as e:
        logger.error(f"[Discipline] 设置豁免失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'设置豁免失败: {e}', 'data': None})


@discipline_bp.route('/plan/api/discipline/check-now', methods=['POST'])
def api_discipline_check_now():
    """手动触发一轮巡检（同步执行，返回本轮摘要，供联调验证）"""
    try:
        from .task.monitor.analysis_discipline import run_discipline_check, get_last_run
        run_discipline_check()
        return jsonify({'code': 200, 'message': '巡检完成', 'data': get_last_run()})
    except Exception as e:
        logger.error(f"[Discipline] 手动巡检失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'巡检失败: {e}', 'data': None})


# =============================================================================
# 看板
# =============================================================================

@discipline_bp.route('/plan/api/discipline/board', methods=['GET'])
def api_discipline_board():
    """反懈怠看板：?days=30（1~180）"""
    try:
        days = int(_num(request.args.get('days'), 30, 1, 180))
        with session_scope() as s:
            data = disc.build_board(s, days=days)
            data['digest'] = disc.gap_digest(s, days=min(days, 7))
        return jsonify({'code': 200, 'message': 'success', 'data': data})
    except Exception as e:
        logger.error(f"[Discipline] 看板查询失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'看板查询失败: {e}', 'data': None})


# =============================================================================
# 回溯分析取数（不写库；写入复用 /api/task/analysis/records_batch）
# =============================================================================

@discipline_bp.route('/plan/api/discipline/backfill-preview', methods=['GET'])
def api_discipline_backfill_preview():
    """回溯分析预览：?slots=2026-09-01 20,2026-09-01 21（最多 31 个）

    为每个历史小时槽 × 每个跟踪币种返回【当时的真实价格】（1H K 线上一根
    已收线 bar 的 close），前端渲染成可编辑判断矩阵后走既有 records_batch 落库，
    source 由服务端自动判为 backfill。

    只拉 K 线、只读库，绝不调用 DualPeriodStrategyAdapter.analyze()：
    历史时刻的策略方向无法在不引入未来数据的前提下诚实重建，
    因此回溯记录的方向字段留空，只保留价格与用户自己的判断。
    """
    try:
        slots = _parse_slot_list(request.args.get('slots', ''), limit=31)
        if not slots:
            return jsonify({'code': 400, 'message': '缺少有效的小时槽参数 slots', 'data': None})

        inst_ids = disc.tracked_inst_ids()
        if not inst_ids:
            return jsonify({'code': 400, 'message': '交易配置中无跟踪币种', 'data': None})

        # 每个时点取 slot 起点：上一根 1H bar 的 close 恰好是 HH:00 的价格
        targets = [disc.slot_start(s) for s in slots]
        ts_of = {s: t.strftime('%Y-%m-%d %H:%M:%S') for s, t in zip(slots, targets)}

        prices, errors = {}, []
        for inst_id in inst_ids:
            got = ana_repo.historical_prices(inst_id, targets, bar='1H')
            prices[inst_id] = got
            if not any(v for v in got.values()):
                errors.append({'instId': inst_id,
                               'message': '历史K线不可用（超出可回溯深度或拉取失败）'})

        items = []
        for slot in slots:
            key = ts_of[slot]
            row_items = []
            for inst_id in inst_ids:
                price = (prices.get(inst_id) or {}).get(key)
                if not price:
                    continue
                row_items.append({
                    'instId': inst_id,
                    'ts': key,
                    'price': price,
                    'short_period': '1H',
                    'long_period': '',
                    'short_dir': '',
                    'long_dir': '',
                    'long_dir_prev': '',
                    'atr_pct': 0,
                    'user_judgment': 'watch',
                    'user_reason': f'回溯补记（{slot}:00）',
                })
            items.append({'hour_slot': slot, 'items': row_items,
                          'missing': [i for i in inst_ids
                                      if not (prices.get(i) or {}).get(key)]})

        ok_count = sum(len(x['items']) for x in items)
        return jsonify({'code': 200,
                        'message': f'回溯取数完成：{ok_count} 条可用（{len(inst_ids)} 币种 × {len(slots)} 小时）',
                        'data': {'slots': items, 'inst_ids': inst_ids, 'errors': errors}})
    except Exception as e:
        logger.error(f"[Discipline] 回溯预览失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'回溯预览失败: {e}', 'data': None})
