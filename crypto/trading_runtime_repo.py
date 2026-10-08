#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实盘调度器「期望运行状态」 - 数据访问层
==========================================
背景：内存看门狗在严重超限时会主动 ``os._exit`` 交给外部进程管理器
（supervisord autorestart）拉起，而 ``register_default_jobs`` 一直是
「只注册、不启动」，所以每触发自愈一次，实盘交易就静默停摆一次，直到有人
去页面点「启动交易」；这段停摆期里持仓只有交易所侧兜底委托在管。

本层把「本来应该在跑」这件事记进 kv_store（key='trading_runtime'），进程
重启后按开关决定是否自动拉起：

    {
      "desired_running": true,            # 最后一次人工操作是「启动」且未被「停止」改回
      "account": "main",                  # 上次运行使用的账号标识
      "auto_resume": false,               # 自动拉起开关（默认关，需人工打开一次）
      "updated_at": "2026-09-08 22:00:00",
      "last_event": "manual_start"        # 最近一次状态变更来源（审计用）
    }

调用约定：
- 写：任何异常都只记日志、不抛出 —— 记账失败绝不能影响启停主流程；
- 读：DB 不可用/无数据时返回默认值，默认值语义 = 「不自动拉起」，
  即与改造前行为完全一致，不会因为本层故障做出自动下单的决定。
"""

import logging
import datetime

from .database import session_scope
from . import config_store_repo

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME = {
    'desired_running': False,
    'account': None,
    'auto_resume': False,
    'updated_at': '',
    'last_event': '',
}


def _now() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _merge(data) -> dict:
    """补齐缺失键（新增字段时旧库记录不会丢字段）"""
    out = dict(DEFAULT_RUNTIME)
    if isinstance(data, dict):
        out.update({k: v for k, v in data.items() if k in DEFAULT_RUNTIME})
    return out


def load_runtime() -> dict:
    """读取期望运行状态；读不到/异常一律回默认值（= 不自动拉起）"""
    try:
        with session_scope() as s:
            data = config_store_repo.load_json_config(
                s, config_store_repo.KEY_TRADING_RUNTIME)
    except Exception as e:
        logger.warning(f'[TradingRuntime] 读取期望运行状态失败，按默认值处理: {e}')
        return dict(DEFAULT_RUNTIME)
    return _merge(data)


def _write(runtime: dict) -> bool:
    """整份覆盖写入；失败只记日志（不影响调用方的启停动作）"""
    runtime['updated_at'] = _now()
    try:
        with session_scope() as s:
            config_store_repo.save_json_config(
                s, config_store_repo.KEY_TRADING_RUNTIME, runtime)
        return True
    except Exception as e:
        logger.error(f'[TradingRuntime] 写入期望运行状态失败（本次启停不受影响，'
                     f'但重启自愈判断会用旧值）: {e}')
        return False


def _update_runtime(changes: dict):
    """锁内读取并合并，避免启停与自动恢复开关读改写互相覆盖。"""
    result = _merge(changes)
    result['updated_at'] = _now()

    def transform(current):
        nonlocal result
        result = _merge(current)
        result.update(changes)
        result['updated_at'] = _now()
        return result

    try:
        with session_scope() as s:
            config_store_repo.patch_json_config(
                s, config_store_repo.KEY_TRADING_RUNTIME, transform)
        return result, True
    except Exception as e:
        logger.error('[TradingRuntime] 合并期望状态失败（启停不受影响）: %s', e)
        return result, False


def set_desired_running(running: bool, account=None, event: str = '') -> bool:
    """记录「用户希望的运行状态」：启动成功置 True、停止置 False。

    account 为 None 时保留原值（例如停止路径不再带账号信息）。
    """
    changes = {'desired_running': bool(running),
               'last_event': event or ('start' if running else 'stop')}
    if account is not None:
        changes['account'] = str(account) if account else None
    rt, saved = _update_runtime(changes)
    if running and not rt.get('account') and account is None:
        logger.warning('[TradingRuntime] 记录运行为 True 但没有账号信息，'
                       '重启自动拉起将跳过')
    return saved


def set_auto_resume(enabled: bool, event: str = 'auto_resume_toggle') -> dict:
    """打开/关闭「重启后自动拉起实盘」开关，返回最新状态"""
    rt, _saved = _update_runtime({'auto_resume': bool(enabled), 'last_event': event})
    return rt
