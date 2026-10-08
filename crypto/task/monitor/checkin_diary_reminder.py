#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日评级收尾提醒（checkin_diary_reminder）
==========================================
每天 23:30 一轮，只看一件事：当天「每日评级总控台」是否收尾完整——
若当天已提交打卡记录，但存在以下任一缺口，则发一封收尾提醒邮件：

  - 未填写自我评级（rating 为空）
  - 未关联日记（diary_entries 当天没有任何条目）

邮件正文含：今日三项指标摘要（热量/交易/学习，读提交时定格的快照列，
不再打 OKX 也不重算）+ 明确的缺口提示 + 直达打卡页与日记本的链接按钮。

【与 checkin_reminder 的区别】
- checkin_reminder：任务卡打卡「倒计时结束」提醒（每 5 分钟巡检计划树）；
- 本模块：一天一次的「评级/日记收尾」提醒（cron 23:30），二者互不影响。

【只读边界】
- 只读 daily_checkins 与 diary_entries 两张表，零写入、绝不触达 OKX；
- 判定基于提交时定格的快照值（trade_pct/cal_value/learn_hours），
  与页面刷新解耦：即便用户 23:30 后又改了源数据，提醒仍反映打卡当时口径。

配置存 kv_store key='checkin_diary_reminder_config'：
  {"enabled": true}
缺省=开启；页面 /task 的任务开关（job_id='checkin_diary_reminder'）与
config.json 文件急停统一参与治理（见 scheduler.register_job 守卫）。
"""

import datetime
import logging

try:
    from ..utils.logger import get_task_logger
    from ..notification.message_notifier import MessageNotifier
    from ..notification.email_tool import EmailTemplates
except ImportError:
    from utils.logger import get_task_logger
    from notification.message_notifier import MessageNotifier
    from notification.email_tool import EmailTemplates

try:
    from crypto.database import session_scope
    from crypto import config_store_repo as cs
except ImportError:
    session_scope = None
    cs = None

logger = logging.getLogger(__name__)
task_log = get_task_logger()

JOB_ID = 'checkin_diary_reminder'
KEY_CONFIG = 'checkin_diary_reminder_config'

# 每日触发时刻（本地 23:30）
RUN_HOUR, RUN_MINUTE = 23, 30

CHECKIN_PAGE_PATH = '/checkin'
DIARY_PAGE_PATH = '/diary'

_LAST_RUN = {
    'finished_at': None,
    'date': '',
    'need_remind': False,
    'missing_rating': False,
    'missing_diary': False,
    'email_ok': None,
    'error': None,
}


def get_last_run() -> dict:
    """最近一轮巡检摘要（副本），供状态展示与排障"""
    return dict(_LAST_RUN)


def load_config() -> dict:
    cfg = {}
    if cs is not None:
        try:
            cfg = cs.load_json_config_cached(KEY_CONFIG) or {}
        except Exception as e:
            task_log.warning(f'[CheckinDiaryReminder] 配置读取失败，按默认值处理: {e}')
            cfg = {}
    return {'enabled': bool(cfg.get('enabled', True))}


def _fmt(v, digits=1):
    if v is None:
        return '--'
    try:
        return ('{:.' + str(digits) + 'f}').format(float(v)).rstrip('0').rstrip('.') \
            if digits else str(int(float(v)))
    except (TypeError, ValueError):
        return '--'


def _site_base_url() -> str:
    """邮件直达链接的站点前缀（与 analysis_discipline / checkin_reminder 同款三级回退）"""
    import os
    import socket
    env = str(os.environ.get('CRYPTO_WEB_BASE_URL', '') or '').strip().rstrip('/')
    if env:
        return env
    port = str(os.environ.get('CRYPTO_WEB_PORT', '') or '').strip() or '7777'
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.settimeout(0.2)
            sk.connect(('8.8.8.8', 80))
            host = sk.getsockname()[0]
    except Exception:
        host = '127.0.0.1'
    return f'http://{host}:{port}'


def _diary_exists(session, day: str) -> bool:
    """当天是否已有日记条目（含隐私条目——只判存在性，不读正文）。"""
    from sqlalchemy import select, func
    from crypto.models import DiaryEntry
    cnt = session.execute(
        select(func.count()).select_from(DiaryEntry).where(DiaryEntry.date == day)
    ).scalar()
    return bool(cnt)


def _evaluate(row, has_diary: bool) -> dict:
    """纯函数判定：当天打卡记录 row（可能 None）+ 是否有日记 → 该不该提醒。
    返回 {'skip'} 或 {'need', 'missing_rating', 'missing_diary'}。不做 I/O。"""
    if row is None:
        return {'skip': 'not_checked_in'}
    missing_rating = not str(row.rating or '').strip()
    missing_diary = not has_diary
    if not missing_rating and not missing_diary:
        return {'skip': 'complete'}
    return {'need': True, 'missing_rating': missing_rating, 'missing_diary': missing_diary}


def _build_email(row, verdict: dict) -> bool:
    """发送收尾提醒邮件；失败只记日志不抛出。"""
    admin = _admin_email()
    if not admin:
        task_log.warning('[CheckinDiaryReminder] 未配置收件邮箱，提醒跳过')
        return False
    base = _site_base_url()
    date_str = row.date
    checks = []
    if verdict['missing_rating']:
        checks.append('自我评级（E~S）')
    if verdict['missing_diary']:
        checks.append('当日日记')
    miss_txt = '、'.join(checks)

    cal = row.to_dict()['calorie']
    tr = row.to_dict()['trade']
    ln = row.to_dict()['learn']

    def mark(passed):
        return '<span style="color:#2e7d32;">✔ 达标</span>' if passed \
            else '<span style="color:#c62828;">✘ 未达标</span>'

    subject = f"🌙 收尾提醒 · {date_str} 还差：{miss_txt}"
    body = f"""
    <p>你今天已经打卡，但有一项没收尾：<b style="color:#e65100;">{miss_txt}</b>。
    睡前补一下，明天回看会更清楚。</p>
    <div class="info-group">
        <div class="info-row"><span class="info-label">🔥 热量缺口</span>
            <span class="info-value">{_fmt(cal['value'], 0)} kcal · {mark(cal['passed'])}</span></div>
        <div class="info-row"><span class="info-label">📈 交易收益</span>
            <span class="info-value">{_fmt_signed(tr['pct'])} · {mark(tr['passed'])}</span></div>
        <div class="info-row"><span class="info-label">📚 学习</span>
            <span class="info-value">{_fmt(ln['hours'], 1)}h · 完成任务 {ln['task_done_count']} 个 · {mark(ln['passed'])}</span></div>
        <div class="info-row"><span class="info-label">⭐ 自我评级</span>
            <span class="info-value">{('已评 ' + row.rating) if row.rating else '<b style="color:#e65100;">待评级</b>'}</span></div>
        <div class="info-row"><span class="info-label">📔 当日日记</span>
            <span class="info-value">{'已写' if not verdict['missing_diary'] else '<b style="color:#e65100;">未写</b>'}</span></div>
    </div>
    <p style="margin-top:16px">
        <a href="{base}{CHECKIN_PAGE_PATH}?date={date_str}" style="display:inline-block;padding:10px 18px;
           background:#4356c0;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;margin-right:10px;">📋 去补评级</a>
        <a href="{base}{DIARY_PAGE_PATH}?date={date_str}&link=checkin:{date_str}" style="display:inline-block;padding:10px 18px;
           background:#0b7285;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;">✍️ 去写日记</a>
    </p>
    <p class="timestamp">检查时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    """
    html = EmailTemplates._wrap_html(subject, body, header_color='#5f3dc4')
    try:
        return MessageNotifier()._dispatch_email(
            to_emails=[admin], subject=subject, html_content=html,
            log_label='每日评级收尾提醒')
    except Exception as e:
        task_log.error(f'[CheckinDiaryReminder] 提醒邮件发送异常: {e}')
        return False


def _fmt_signed(v):
    if v is None:
        return '--%'
    try:
        n = float(v)
        return ('+' if n > 0 else '') + f'{n:.2f}%'
    except (TypeError, ValueError):
        return '--%'


def _admin_email() -> str:
    try:
        from ..config.email_config import get_admin_email
        return get_admin_email()
    except Exception:
        try:
            from config.email_config import get_admin_email
            return get_admin_email()
        except Exception as e:
            task_log.warning(f'[CheckinDiaryReminder] 读取管理员邮箱失败: {e}')
            return ''


def run_checkin_diary_reminder_check(now=None):
    """23:30 入口：读当天打卡记录 + 日记存在性 → 判定 → 需要则发信。

    任何一步失败只记日志不抛出。cron 一天一次，无需去重状态。
    """
    global _LAST_RUN
    now = now or datetime.datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    try:
        if session_scope is None or cs is None:
            raise RuntimeError('database/config_store_repo 不可用')
        cfg = load_config()
        if not cfg.get('enabled', True):
            _LAST_RUN = {'finished_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'date': date_str,
                         'need_remind': False, 'missing_rating': False,
                         'missing_diary': False, 'email_ok': None, 'error': None,
                         'skip': 'disabled'}
            return
        from crypto import checkin_repo as repo
        with session_scope() as s:
            row = repo.load_checkin(s, date_str)
            has_diary = _diary_exists(s, date_str)
        verdict = _evaluate(row, has_diary)

        if verdict.get('skip'):
            _LAST_RUN = {'finished_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'date': date_str,
                         'need_remind': False, 'missing_rating': False,
                         'missing_diary': False, 'email_ok': None, 'error': None,
                         'skip': verdict['skip']}
            return

        ok = _build_email(row, verdict)
        _LAST_RUN = {'finished_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'date': date_str,
                     'need_remind': True, 'missing_rating': verdict['missing_rating'],
                     'missing_diary': verdict['missing_diary'], 'email_ok': bool(ok), 'error': None}
        if ok:
            task_log.info(f"[CheckinDiaryReminder] 收尾提醒已发送（{date_str} 缺：" +
                          '、'.join(k for k, v in (('评级', verdict['missing_rating']),
                                                   ('日记', verdict['missing_diary'])) if v) + '）')
        else:
            task_log.warning('[CheckinDiaryReminder] 收尾提醒邮件发送失败')
    except Exception as e:
        logger.error(f'[CheckinDiaryReminder] 巡检异常: {e}', exc_info=True)
        _LAST_RUN = {'finished_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'date': date_str,
                     'need_remind': False, 'missing_rating': False,
                     'missing_diary': False, 'email_ok': None, 'error': str(e)}


# =============================================================================
# 调度注册（scheduler.register_default_jobs 与配置保存共用）
# =============================================================================

def register_checkin_diary_reminder_job():
    """注册每日评级收尾提醒任务（cron 23:30），返回 (job_id 或 None, 说明文本)。

    开关关闭时 register_job 自身会拦下注册；配置 enabled=false 时显式移除
    已注册任务，支持热更新。
    """
    from ..scheduler import task_scheduler

    cfg = load_config()
    if not cfg.get('enabled', True):
        task_scheduler.remove_job(JOB_ID)
        return None, '配置已关闭（checkin_diary_reminder_config.enabled=false）'
    job_id = task_scheduler.register_job(
        run_checkin_diary_reminder_check, trigger='cron',
        hour=RUN_HOUR, minute=RUN_MINUTE,
        job_id=JOB_ID,
        job_name='每日评级收尾提醒（23:30 检查当天打卡是否缺评级/缺日记并邮件提醒）')
    return job_id, f'每天 {RUN_HOUR:02d}:{RUN_MINUTE:02d} 检查'
