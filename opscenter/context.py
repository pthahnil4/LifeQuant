#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jinja 全局上下文：顶栏健康灯、数据源路径等，所有页面可用。"""
from . import config as C


def inject_globals(app):
    @app.context_processor
    def _globals():
        # 顶栏健康灯：进程存活 + 综合评分（采集层已带 TTL 缓存，且异常兜底为 neutral）
        try:
            from .collectors.health import get_health
            h = get_health()
            pill = {
                'alive': h['process']['alive'],
                'level': 'success' if (h['process']['alive'] and h['score'] >= 80)
                         else ('warning' if h['process']['alive'] else 'danger'),
                'score': h['score'],
                'text': '系统正常' if h['process']['alive'] else '交易进程离线',
            }
        except Exception:      # 采集异常绝不让页面崩溃，退化为"未知"
            pill = {'alive': False, 'level': 'neutral', 'score': 0, 'text': '状态未知'}
        return {
            'topbar_pill': pill,
            'app_name': 'OpsCenter',
            'app_stage': 'P5 · 全站完成（含告警中心：交易侧 + 系统侧统一告警流）',
            'log_dir': C.LOG_DIR,
            'trading_api': C.TRADING_API_BASE,
            'trading_display': (C.TRADING_API_BASE
                                .replace('http://', '').replace('https://', '')),
        }
