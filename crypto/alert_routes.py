#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风险警报 - Flask 蓝图（异常行情与持仓盈亏监控报警）
====================================================
所有 /alert 和 /alert/api/* 路由：
- 页面渲染（alert.html）
- 监控配置读取/保存（kv_store key='alert_config'，保存后热重注册调度任务）
- 告警历史查询（alert_log 表）
- 手动触发一轮检测 / 任务状态查询

检测引擎与级别递进状态机见 crypto/task/monitor/alert_monitor.py。
"""

import logging
import os
import sys
from flask import Blueprint, jsonify, request, render_template

from .database import session_scope
from .models import AlertLog
from sqlalchemy import select

# =============================================================================
# 【关键】确保优先加载项目根目录的 api_config.py（与 plan_routes 同款守卫）
# 策略模块会向 sys.path 头部插入含另一份 api_config.py 的子目录，
# 若不加守卫，懒加载导入时可能取到错误密钥导致 OKX 签名失败。
# =============================================================================
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT in sys.path:
    sys.path.remove(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger(__name__)

alert_bp = Blueprint('alert_bp', __name__)

# =============================================================================
# 数据库初始化已收敛到 database.init_db() 进程内单例，由应用启动时后台
# 预热（warmup_async）完成，蓝图导入期不再同步建表，避免阻塞启动。
# =============================================================================


def _monitor():
    """延迟导入监控引擎（避免蓝图加载期拉起 OKX 客户端依赖）"""
    from .task.monitor import alert_monitor
    return alert_monitor


# =============================================================================
# 页面
# =============================================================================

@alert_bp.route('/alert')
def alert_page():
    """风险警报页面"""
    try:
        from .api_config import ACCOUNTS, DEFAULT_ACCOUNT
        accounts = [{'key': k, 'name': v.get('name', k),
                     'is_default': k == DEFAULT_ACCOUNT} for k, v in ACCOUNTS.items()]
    except Exception:
        accounts = []
    return render_template('alert.html', active_page='alert', accounts=accounts)


# =============================================================================
# 配置接口（kv_store key='alert_config'）
# =============================================================================

def _num(value, default, minimum=None, maximum=None):
    """数值字段容错转换：非法值回退默认，可选上下限夹紧"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    if minimum is not None:
        v = max(minimum, v)
    if maximum is not None:
        v = min(maximum, v)
    return v


def _parse_inst_ids(raw):
    """监控币种解析：支持数组或逗号/换行分隔字符串，去重去空"""
    if isinstance(raw, str):
        parts = raw.replace('\n', ',').split(',')
    elif isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = []
    seen, result = set(), []
    for p in parts:
        p = str(p).strip()
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return result


@alert_bp.route('/alert/api/config', methods=['GET'])
def get_alert_config():
    """读取当前监控配置（未保存过返回默认值）"""
    try:
        cfg = _monitor().load_alert_config()
        return jsonify({'code': 200, 'data': cfg})
    except Exception as e:
        logger.error(f"[Alert] 读取配置失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'读取配置失败: {e}'})


@alert_bp.route('/alert/api/config', methods=['POST'])
def save_alert_config():
    """保存监控配置并热重注册调度任务"""
    try:
        payload = request.get_json(silent=True) or {}
        mon = _monitor()
        cfg = mon.load_alert_config()

        cfg['enabled'] = bool(payload.get('enabled', cfg.get('enabled')))
        cfg['interval_seconds'] = int(_num(payload.get('interval_seconds'),
                                           cfg.get('interval_seconds', 300), 30, 86400))
        cfg['account'] = str(payload.get('account', cfg.get('account', '') or '')).strip()

        price_in = payload.get('price', {}) or {}
        price = cfg.setdefault('price', {})
        price['enabled'] = bool(price_in.get('enabled', price.get('enabled')))
        price['window_minutes'] = int(_num(price_in.get('window_minutes'),
                                           price.get('window_minutes', 15), 1, 99))
        price['warning_pct'] = _num(price_in.get('warning_pct'),
                                    price.get('warning_pct', 5.0), 0.1, 99)
        price['critical_pct'] = _num(price_in.get('critical_pct'),
                                     price.get('critical_pct', 8.0), 0.1, 99)
        if price['critical_pct'] < price['warning_pct']:
            return jsonify({'code': 400, 'message': '行情严重阈值不得小于预警阈值'})
        if 'inst_ids' in price_in:
            price['inst_ids'] = _parse_inst_ids(price_in.get('inst_ids'))

        pnl_in = payload.get('pnl', {}) or {}
        pnl = cfg.setdefault('pnl', {})
        pnl['enabled'] = bool(pnl_in.get('enabled', pnl.get('enabled')))
        pnl['warning_pct'] = _num(pnl_in.get('warning_pct'),
                                  pnl.get('warning_pct', -10.0), -99, -0.1)
        pnl['critical_pct'] = _num(pnl_in.get('critical_pct'),
                                   pnl.get('critical_pct', -20.0), -99, -0.1)
        if pnl['critical_pct'] > pnl['warning_pct']:
            return jsonify({'code': 400, 'message': '盈亏严重阈值（更深亏损）不得大于预警阈值'})
        # 盈利侧提醒（阈值全部由配置决定；正值，越大越"达标"）
        pnl['profit_enabled'] = bool(pnl_in.get('profit_enabled',
                                                 pnl.get('profit_enabled', True)))
        pnl['profit_warning_pct'] = _num(pnl_in.get('profit_warning_pct'),
                                          pnl.get('profit_warning_pct', 10.0), 0.1, 999)
        pnl['profit_critical_pct'] = _num(pnl_in.get('profit_critical_pct'),
                                           pnl.get('profit_critical_pct', 20.0), 0.1, 999)
        if pnl['profit_critical_pct'] < pnl['profit_warning_pct']:
            return jsonify({'code': 400, 'message': '盈利达标阈值不得小于盈利预警阈值'})

        liq_in = payload.get('liq', {}) or {}
        liq = cfg.setdefault('liq', {})
        liq['enabled'] = bool(liq_in.get('enabled', liq.get('enabled')))
        # 强平距离：越近越危险，严重阈值应 ≤ 预警阈值
        liq['warning_pct'] = _num(liq_in.get('warning_pct'),
                                    liq.get('warning_pct', 5.0), 0.1, 99)
        liq['critical_pct'] = _num(liq_in.get('critical_pct'),
                                     liq.get('critical_pct', 2.0), 0.1, 99)
        if liq['critical_pct'] > liq['warning_pct']:
            return jsonify({'code': 400, 'message': '强平距离严重阈值（更近）不得大于预警阈值'})

        acct_in = payload.get('acct', {}) or {}
        acct = cfg.setdefault('acct', {})
        acct['enabled'] = bool(acct_in.get('enabled', acct.get('enabled')))
        acct['margin_warning_pct'] = _num(acct_in.get('margin_warning_pct'),
                                           acct.get('margin_warning_pct', 200.0), 100, 100000)
        acct['margin_critical_pct'] = _num(acct_in.get('margin_critical_pct'),
                                            acct.get('margin_critical_pct', 120.0), 100, 100000)
        if acct['margin_critical_pct'] > acct['margin_warning_pct']:
            return jsonify({'code': 400, 'message': '保证金率严重阈值（更临近强平）不得大于预警阈值'})
        acct['min_avail_warning'] = _num(acct_in.get('min_avail_warning'),
                                          acct.get('min_avail_warning', 100.0), 0, 1e9)
        acct['min_avail_critical'] = _num(acct_in.get('min_avail_critical'),
                                           acct.get('min_avail_critical', 20.0), 0, 1e9)
        if acct['min_avail_critical'] > acct['min_avail_warning']:
            return jsonify({'code': 400, 'message': '可用余额严重阈值不得大于预警阈值'})

        lv_in = payload.get('liveness', {}) or {}
        lv = cfg.setdefault('liveness', {})
        lv['enabled'] = bool(lv_in.get('enabled', lv.get('enabled')))
        lv['grace_multiplier'] = _num(lv_in.get('grace_multiplier'),
                                        lv.get('grace_multiplier', 5), 1, 1000)
        lv['min_stale_seconds'] = _num(lv_in.get('min_stale_seconds'),
                                        lv.get('min_stale_seconds', 900), 60, 86400)
        lv['critical_stale_seconds'] = _num(lv_in.get('critical_stale_seconds'),
                                             lv.get('critical_stale_seconds', 2400), 60, 86400)
        if lv['critical_stale_seconds'] < lv['min_stale_seconds']:
            return jsonify({'code': 400, 'message': '调度轮停滞严重阈值不得小于预警下限'})

        mon.save_alert_config(cfg)

        # 热重注册调度任务（周期/开关变化立即生效）
        try:
            interval = mon.register_alert_job()
            msg = f'配置已保存，监控任务已按 {interval}s 周期重注册'
        except Exception as e:
            msg = f'配置已保存，但调度任务重注册失败: {e}'
        return jsonify({'code': 200, 'message': msg, 'data': cfg})
    except Exception as e:
        logger.error(f"[Alert] 保存配置失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'保存配置失败: {e}'})


# =============================================================================
# 告警历史（alert_log）
# =============================================================================

@alert_bp.route('/alert/api/history', methods=['GET'])
def alert_history():
    """告警历史查询：?limit=200&type=price|pnl|profit|liq|acct|liveness&level=&inst=（默认最近200条）"""
    try:
        limit = min(int(request.args.get('limit', 200)), 1000)
        f_type = request.args.get('type', '').strip()
        f_level = request.args.get('level', '').strip()
        f_inst = request.args.get('inst', '').strip()

        stmt = select(AlertLog).order_by(AlertLog.created_at.desc(), AlertLog.id.desc())
        if f_type in ('price', 'pnl', 'profit', 'liq', 'acct', 'liveness'):
            stmt = stmt.where(AlertLog.alert_type == f_type)
        if f_level in ('warning', 'critical', 'recover'):
            stmt = stmt.where(AlertLog.level == f_level)
        if f_inst:
            stmt = stmt.where(AlertLog.inst_id.contains(f_inst))

        with session_scope() as s:
            rows = s.execute(stmt.limit(limit)).scalars().all()
            data = [{
                'id': r.id,
                'type': r.alert_type,
                'inst_id': r.inst_id,
                'level': r.level,
                'metric_value': r.metric_value,
                'threshold': r.threshold,
                'message': r.message,
                'notify_sent': bool(r.notify_sent),
                'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else '',
            } for r in rows]
        return jsonify({'code': 200, 'data': data, 'total': len(data)})
    except Exception as e:
        logger.error(f"[Alert] 查询历史失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'查询历史失败: {e}'})


# =============================================================================
# 状态与手动触发
# =============================================================================

@alert_bp.route('/alert/api/status', methods=['GET'])
def alert_status():
    """监控任务状态：调度注册信息 + 最近一轮检测摘要"""
    try:
        from .task.scheduler import task_scheduler
        mon = _monitor()
        cfg = mon.load_alert_config()
        job = next((j for j in task_scheduler.get_all_jobs()
                    if j['id'] == mon.ALERT_JOB_ID), None)
        return jsonify({'code': 200, 'data': {
            'enabled': bool(cfg.get('enabled')),
            'interval_seconds': cfg.get('interval_seconds'),
            'job': job,
            'last_run': mon.get_last_run(),
        }})
    except Exception as e:
        logger.error(f"[Alert] 查询状态失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'查询状态失败: {e}'})


@alert_bp.route('/alert/api/run-now', methods=['POST'])
def alert_run_now():
    """手动触发一轮检测（同步执行，返回本轮摘要）"""
    try:
        mon = _monitor()
        mon.run_alert_check()
        return jsonify({'code': 200, 'message': '检测完成', 'data': mon.get_last_run()})
    except Exception as e:
        logger.error(f"[Alert] 手动检测失败: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': f'检测失败: {e}'})
