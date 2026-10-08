#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日内存主动归零重启（Daily Memory Reset）
==========================================
定位：把「被动涨到 kill 阈值才撞线重启」变成「每天低波动窗口主动重启一次」。

为什么需要它
------------
内存阈值（800MB）保持不变，而 CPython 没有 JVM 式压缩 GC：glibc 分配器棘轮 +
pymalloc arena 残留会让 RSS 随累计轮数单调上升（``memory_watchdog`` 里的
``tune_glibc_allocator`` / ``malloc_trim`` 只能压平一部分）。既然涨是必然的，
那就挑一个没人看盘、行情也淡的时刻自己清零，而不是等它涨到 800 撞线 ——
后者可能正好撞在剧烈波动的那一分钟。

三条硬约束（每条都有对应的跳过条件，另加一条「读不到水位就不做不可逆动作」的保护）
--------------------------------
1. **不能把实盘重启成一个静默停摆窗口**：如果「期望运行=True」但页面「重启自动
   拉起」开关还没打开，此刻重启等于亲手把实盘停掉且没人接手 → 直接跳过。
   （内存撞线时这没办法，只能发信求救；但计划内重启完全可以挑条件。）
2. **不能打断正在进行的策略计算**：pro3 引擎的参数放在模块级全局里，一轮
   ``analyze()`` 中途被杀虽然不会污染交易所侧（挂单在服务器外、账本与反向风控
   计时都持久化在 MySQL），但会把那一轮的分析记录、下单结果确认留在半路。
   所以撞到 ``PRO3_LOCK`` 被占用时先等（默认最多 5 分钟），等不到就让位给明天。
3. **水位低就别折腾**：距上次重启才两小时、RSS 只有 250MB 的进程，重启它没有任何
   收益，只是白白制造一个停摆窗口 → 低于 ``MIN_RSS_MB`` 直接跳过。

拉起链路复用同一条：本模块的 ``os._exit(1)`` 与内存看门狗走的是同一个外部管理器
（supervisord ``autorestart=true``）。现在撞 800MB 能自动重启，就说明这条链路是通的。

环境变量（可选）：
- CRYPTO_DAILY_RESET               设 '0' 关闭本功能（默认开）
- CRYPTO_DAILY_RESET_AT            执行时刻 HH:MM，默认 04:03（错开整点，避开整点轮次）
- CRYPTO_DAILY_RESET_MIN_RSS_MB    低于该水位不重启，默认 350
- CRYPTO_DAILY_RESET_WAIT_SEC      等策略引擎空闲的最长秒数，默认 300

config.json 统一开关（与上面的环境变量任一关闭即不注册，重启生效）：
- task_switches.daily_memory_reset  设 false 关闭本功能（缺省=开）
"""

import datetime
import os

# 双模式导入：Flask 包内（crypto.task.*）/ 独立脚本（task 目录在 sys.path）
try:
    from ..utils.logger import get_task_logger
except ImportError:  # pragma: no cover
    from utils.logger import get_task_logger

try:
    from .. import process_lifecycle as _lifecycle
except Exception:  # pragma: no cover - 包外裸模块身份时
    try:
        import process_lifecycle as _lifecycle
    except Exception:
        _lifecycle = None

try:
    from .strategy_gate import pro3_locked_now
except ImportError:  # pragma: no cover
    from strategy_gate import pro3_locked_now

task_log = get_task_logger()

DAILY_JOB_ID = 'daily_memory_reset'

ENABLED = str(os.environ.get('CRYPTO_DAILY_RESET', '')).strip().lower() not in ('0', 'false', 'off')
MIN_RSS_MB = float(os.environ.get('CRYPTO_DAILY_RESET_MIN_RSS_MB', 350) or 350)
WAIT_BUSY_SEC = float(os.environ.get('CRYPTO_DAILY_RESET_WAIT_SEC', 300) or 300)


def parse_reset_at(text=None):
    """解析 HH:MM 配置；写错就退回默认 04:03，绝不因为一个错别字让任务不注册"""
    raw = (text if text is not None else os.environ.get('CRYPTO_DAILY_RESET_AT', '')).strip()
    default = (4, 3)
    if not raw:
        return default
    try:
        hh, mm = raw.split(':', 1)
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(raw)
        return hh, mm
    except Exception:
        task_log.warning(f'[每日归零] CRYPTO_DAILY_RESET_AT={raw!r} 不是 HH:MM，按 04:03 执行')
        return default


def cron_kwargs():
    """供 register_job 用的 cron 参数"""
    hh, mm = parse_reset_at()
    return {'hour': hh, 'minute': mm}


# =============================================================================
# 判定（纯函数，便于离线冒烟：不做任何 IO，也不退出）
# =============================================================================

def evaluate_daily_reset(rss_mb, rt, busy=False):
    """返回 (should_restart: bool, why: str)。why 一律是人话，直接进日志/页面。"""
    if not ENABLED:
        return False, '每日主动归零已关闭（CRYPTO_DAILY_RESET=0）'
    if rss_mb is None:
        return False, '读不到本进程内存水位，不做不可逆的重启动作'
    if rt.get('desired_running') and not rt.get('auto_resume'):
        return False, ('实盘本应在跑，但「重启自动拉起」开关未打开 —— 此刻重启'
                       '等于把实盘静默停摆，跳过（请先在 /task 页面打开该开关）')
    if busy:
        return False, '策略计算仍在进行中（等空闲超时），本轮让位给明天'
    if rss_mb < MIN_RSS_MB:
        return False, (f'当前 RSS {rss_mb:.0f}MB 低于水位线 {MIN_RSS_MB:.0f}MB，'
                       f'内存没涨起来，没必要再重启一次')
    return True, (f'当前 RSS {rss_mb:.0f}MB ≥ 水位线 {MIN_RSS_MB:.0f}MB，'
                  f'主动归零重启（计划内动作，起来后由自愈逻辑直接接回实盘）')


# =============================================================================
# 任务体
# =============================================================================

def run_daily_memory_reset():
    """cron 任务体：判定通过则写退出标记并自杀，交给外部管理器拉起"""
    run_id = f"R{datetime.datetime.now().strftime('%H%M%S')} "
    if _lifecycle is None:
        task_log.warning(f'[每日归零] {run_id}process_lifecycle 不可用，跳过本次')
        return False, '归因层不可用，跳过'

    try:
        from ..memory_watchdog import get_process_rss_mb
    except ImportError:  # pragma: no cover
        from memory_watchdog import get_process_rss_mb

    rss_mb = None
    try:
        rss_mb = get_process_rss_mb()
    except Exception as e:
        task_log.warning(f'[每日归零] {run_id}读取 RSS 失败: {e}')

    try:
        from .scheduler import task_scheduler
        rt = task_scheduler._runtime_snapshot()
    except Exception as e:
        # 读不到期望运行状态时按「不重启」处理：宁可明天再清零，也不能赌
        task_log.warning(f'[每日归零] {run_id}读取实盘期望运行状态失败，跳过: {e}')
        return False, f'期望运行状态不可读，跳过: {e}'

    # 等策略引擎空闲：一轮 analyze 3~50s，整轮批量可能几分钟
    waited = 0.0
    step = 5.0
    busy = pro3_locked_now()
    while busy and waited < WAIT_BUSY_SEC:
        task_log.info(f'[每日归零] {run_id}策略引擎占用中，等 {step:g}s 后重试'
                      f'（已等 {waited:.0f}s / 上限 {WAIT_BUSY_SEC:.0f}s）')
        _sleep(step)
        waited += step
        busy = pro3_locked_now()

    should, why = evaluate_daily_reset(rss_mb, rt, busy=busy)
    if not should:
        task_log.info(f'[每日归零] {run_id}跳过：{why}')
        return False, why

    task_log.warning(f'[每日归零] {run_id}执行：{why}')
    # 计划内重启：不发邮件（天天发信一周就把告警看麻了），归因侧按 silent 处理
    _lifecycle.hard_exit(_lifecycle.REASON_DAILY_RESTART, {
        'rss_mb': round(rss_mb, 1) if rss_mb is not None else None,
        'trigger': 'daily_reset',
        'waited_busy_sec': round(waited, 1),
    })
    return True, why  # pragma: no cover - hard_exit 不返回


def _sleep(seconds):
    """单独抽出来，方便冒烟桩掉（真线程里它就是 time.sleep）"""
    import time
    time.sleep(seconds)


def register_daily_reset_job():
    """注册每日主动归零任务；返回 (job_id | None, 说明)"""
    try:
        from .scheduler import task_scheduler, task_switch
    except Exception as e:  # pragma: no cover
        task_log.warning(f'[每日归零] 调度器不可用，任务未注册: {e}')
        return None, '调度器不可用'

    if not ENABLED:
        task_scheduler.remove_job(DAILY_JOB_ID)
        task_log.info('[每日归零] 已关闭（CRYPTO_DAILY_RESET=0），任务未注册')
        return None, '已关闭'

    # config.json 统一任务开关：免环境变量也能停（只影响注册，任务体逻辑不变）
    if not task_switch('daily_memory_reset'):
        task_scheduler.remove_job(DAILY_JOB_ID)
        task_log.info('[每日归零] 已关闭（config.json task_switches.daily_memory_reset=false），'
                      '任务未注册')
        return None, '已关闭（task_switches）'

    hh, mm = parse_reset_at()
    task_scheduler.register_job(
        run_daily_memory_reset, trigger='cron',
        job_id=DAILY_JOB_ID,
        job_name=f'每日内存主动归零重启（{hh:02d}:{mm:02d}，水位≥{MIN_RSS_MB:.0f}MB 才动手）',
        **cron_kwargs())
    return DAILY_JOB_ID, f'{hh:02d}:{mm:02d}'
