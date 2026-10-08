#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打卡倒计时提醒（checkin_reminder）
==================================
每 5 分钟一轮，只做一件事：盯着「当前正在打的卡」最近一次打卡，
自那次打卡时刻起满 N 分钟（默认 60，即一小时的钟点窗）仍未产生新的
打卡记录时，发一封邮件告诉你「本小时时间已结束——可以短暂休息，
或继续下一次任务打卡」。

【为什么是轮询而不是一次性定时器】
- 进程重启 / 开关热更新后，已排的一次性 DateTrigger 任务会丢；
  轮询天然自愈，最迟只晚一个周期（5 分钟），与纪律邮件的节拍口径一致。
- 每个周期都从打卡数据现算，无后台常驻状态：state 只记「哪一格已提醒过」，
  新打卡一出现，候选格子自然变化，旧提醒自动作废（不会错发）。

【只读边界】
- 读计划走 plan_routes._load_summary_plans 窄投影（GET 链路同款，绝不写库），
  与既有「读路径不写库」红线一致（1205 锁超时教训见 plan_routes 注释）。
- 唯一的写入是 kv_store 里的去重状态键 checkin_reminder_state，
  不影响任何业务表。

配置存 kv_store key='checkin_reminder_config'：
  {"enabled": true, "minutes": 60}
缺省=开启、60 分钟；页面 /task 的任务开关（job_id='checkin_reminder'）
与 config.json 文件急停统一参与治理（见 scheduler.register_job 守卫）。
"""

import datetime
import logging

# 双模式导入：Flask 包内（crypto.task.monitor）/ 独立脚本（task 目录在 sys.path）
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

# 调度任务 ID（与 scheduler 注册/SWITCHABLE_JOB_LABELS 口径一致）
CHECKIN_JOB_ID = 'checkin_reminder'

# 巡检周期（秒）：5 分钟一轮，提醒最迟在倒计时结束后 5 分钟内到达
CHECK_INTERVAL_SECONDS = 300

# kv 配置键与去重状态键
KEY_CHECKIN_CONFIG = 'checkin_reminder_config'
KEY_CHECKIN_STATE = 'checkin_reminder_state'

# 倒计时默认分钟数（一个打卡小时窗）；可被配置 minutes 覆盖，<=0 视为关闭
DEFAULT_COUNTDOWN_MINUTES = 60

# 候选打卡的最大回溯窗（小时）：超过 48h 没动静说明已长时间离场，
# 不再用一封迟到的邮件"倒打一耙"；新的一次打卡会立即重开倒计时。
MAX_LOOKBACK_HOURS = 48

# 任务卡页面直达链接（邮件里的行动入口）
PLAN_PAGE_PATH = '/plan'

_LAST_RUN = {
    'finished_at': None,
    'candidate_key': '',
    'due_at': '',
    'reminded': False,
    'email_ok': None,
    'error': None,
}


def get_last_run() -> dict:
    """最近一轮巡检摘要（副本），供状态展示与排障"""
    return dict(_LAST_RUN)


def load_config() -> dict:
    """读提醒配置（TTL 缓存）：{"enabled": bool, "minutes": int}，缺省=开 60 分钟"""
    cfg = {}
    if cs is not None:
        try:
            cfg = cs.load_json_config_cached(KEY_CHECKIN_CONFIG) or {}
        except Exception as e:
            task_log.warning(f'[CheckinReminder] 配置读取失败，按默认值处理: {e}')
            cfg = {}
    try:
        minutes = int(cfg.get('minutes', DEFAULT_COUNTDOWN_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_COUNTDOWN_MINUTES
    return {'enabled': bool(cfg.get('enabled', True)), 'minutes': minutes}


def _parse_dt(text):
    """打卡时间解析（YYYY-MM-DD HH:MM[:SS]）；非法返回 None。与 plan 侧口径一致"""
    text = str(text or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _find_latest_filled(data: dict, now: datetime.datetime):
    """扫描全部 in_progress 卡的已打卡格子，返回 (plan, card, slot, dt) 中
    打卡时刻最新的一个；无有效候选返回 None。

    - 只认 in_progress 卡（结算/未解锁的卡没有"下一步打卡"可言）；
    - 未来时间的补录直接忽略（不可用 now 之后的时刻起倒计时）；
    - 超过回溯窗的旧打卡忽略（长时间离场不发迟到的催促）。
    """
    best = None
    floor = now - datetime.timedelta(hours=MAX_LOOKBACK_HOURS)
    for plan in (data or {}).get('plans', []) or []:
        for card in plan.get('cards', []) or []:
            if card.get('status') != 'in_progress':
                continue
            for slot in card.get('slots', []) or []:
                if not slot.get('filled'):
                    continue
                dt = _parse_dt(slot.get('filled_at'))
                if dt is None or dt > now or dt < floor:
                    continue
                if best is None or dt > best[3]:
                    best = (plan, card, slot, dt)
    return best


def _candidate_key(plan, card, slot, dt):
    """格子指纹：计划/卡/格索引 + 打卡时刻。同一格内容不变则 key 不变（去重），
    改期或新打卡都会让 key 变化，旧提醒自动失效。"""
    return f"{plan.get('id')}|{card.get('id')}|{slot.get('slot_index')}|{dt:%Y-%m-%d %H:%M:%S}"


def _evaluate(data: dict, cfg: dict, state: dict, now: datetime.datetime) -> dict:
    """纯函数核心：本轮该不该发信。返回：
      {'skip': 原因}                     —— 整体不发（关闭/无候选/超回溯窗）
      {'due': False, ...}                —— 倒计时未结束
      {'due': True, 'already': True}     —— 本格已提醒过，且没有新打卡 → 不重发
      {'due': True, 'already': False, 'key', 'plan', 'card', 'slot', 'filled_at',
       'due_at', 'minutes'}              —— 到点，可发
    不做任何 I/O，可离线断言。
    """
    if not cfg.get('enabled', True):
        return {'skip': 'disabled'}
    found = _find_latest_filled(data, now)
    if found is None:
        return {'skip': 'no_candidate'}
    plan, card, slot, dt = found
    # 显式区分"未配置"与"配置为 0"：minutes<=0 是用户主动关闭倒计时，
    # 不能被 `or 默认值` 吞掉重新变回 60
    raw = cfg.get('minutes')
    if raw is None:
        raw = DEFAULT_COUNTDOWN_MINUTES
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = DEFAULT_COUNTDOWN_MINUTES
    if minutes <= 0:
        return {'skip': 'countdown_off'}
    due_at = dt + datetime.timedelta(minutes=minutes)
    if now < due_at:
        return {'due': False, 'due_at': due_at.strftime('%Y-%m-%d %H:%M:%S')}
    key = _candidate_key(plan, card, slot, dt)
    last_key = str((state or {}).get('last_reminded_key') or '')
    if last_key == key:
        return {'due': True, 'already': True}
    return {
        'due': True, 'already': False, 'key': key,
        'plan': plan, 'card': card, 'slot': slot,
        'filled_at': dt.strftime('%Y-%m-%d %H:%M:%S'),
        'due_at': due_at.strftime('%Y-%m-%d %H:%M:%S'),
        'minutes': minutes,
    }


def _load_state():
    with session_scope() as s:
        state = cs.load_json_config(s, KEY_CHECKIN_STATE)
    return state if isinstance(state, dict) else {}


def _save_state(state: dict):
    with session_scope() as s:
        cs.save_json_config(s, KEY_CHECKIN_STATE, state)


def _admin_email() -> str:
    """收件人：优先包内相对导入，回退 task 目录绝对导入（与纪律模块同款两种跑法）"""
    try:
        from ..config.email_config import get_admin_email
        return get_admin_email()
    except Exception:
        try:
            from config.email_config import get_admin_email
            return get_admin_email()
        except Exception as e:
            task_log.warning(f'[CheckinReminder] 读取管理员邮箱失败: {e}')
            return ''


def _plan_page_url() -> str:
    """邮件里直达链接的站点前缀（与 analysis_discipline 同款三级回退）"""
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


def _send_reminder(result: dict) -> bool:
    """发送"本小时已结束"提醒邮件；失败只记日志，不抛出"""
    admin = _admin_email()
    if not admin:
        task_log.warning('[CheckinReminder] 未配置收件邮箱，提醒跳过')
        return False
    card = result['card']
    plan = result['plan']
    card_type = '交易卡' if card.get('type') == 'trade' else '学习卡'
    subject = f"⏰ 打卡倒计时结束 · {result['filled_at'][11:16]} 起的 {result['minutes']} 分钟已到"
    body = f"""
    <p>你上一次任务打卡是在 <b>{result['filled_at']}</b>，
    距今已满 <b>{result['minutes']} 分钟</b>，这一小时的窗口时间已结束。</p>
    <p style="font-size:15px"><b>时间已结束，可以短暂休息，或者继续任务打卡。</b></p>
    <div class="info-group">
        <div class="info-row"><span class="info-label">所属计划</span><span class="info-value">{plan.get('name') or plan.get('id') or ''}</span></div>
        <div class="info-row"><span class="info-label">当前任务卡</span><span class="info-value">{card.get('title') or card.get('id')}（{card_type}）</span></div>
        <div class="info-row"><span class="info-label">上次打卡</span><span class="info-value">{result['filled_at']}</span></div>
        <div class="info-row"><span class="info-label">倒计时结束</span><span class="info-value">{result['due_at']}</span></div>
    </div>
    <p style="margin-top:14px"><a href="{_plan_page_url()}{PLAN_PAGE_PATH}" style="display:inline-block;padding:10px 18px;
       background:#0b5ed7;color:#fff;border-radius:8px;text-decoration:none;
       font-weight:bold">🗓 打开任务卡，继续打卡</a></p>
    <p class="timestamp">判定时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    """
    html = EmailTemplates._wrap_html(subject, body, header_color='#0b7285')
    try:
        return MessageNotifier()._dispatch_email(
            to_emails=[admin], subject=subject, html_content=html,
            log_label='打卡倒计时提醒')
    except Exception as e:
        task_log.error(f'[CheckinReminder] 提醒邮件发送异常: {e}')
        return False


def run_checkin_reminder_check():
    """巡检入口（调度器每 5 分钟调一次）：读计划树 → 纯函数判定 → 到点发信并记账。

    任何一步失败只记日志不抛出；先发信后记状态——发送失败不写
    last_reminded_key，下一个周期会自动重试，不会静默吞掉提醒。
    """
    global _LAST_RUN
    now = datetime.datetime.now()
    try:
        if session_scope is None or cs is None:
            raise RuntimeError('database/config_store_repo 不可用')
        # 懒导入：plan_routes 依赖 Flask，仅在轮询真正运行时才需要
        from crypto import plan_routes
        with session_scope() as s:
            data = plan_routes._load_summary_plans(s)
        cfg = load_config()
        state = _load_state()
        result = _evaluate(data, cfg, state, now)

        if result.get('skip'):
            _LAST_RUN = {'finished_at': now.strftime('%Y-%m-%d %H:%M:%S'),
                         'candidate_key': '', 'due_at': '', 'reminded': False,
                         'email_ok': None, 'error': None,
                         'skip': result['skip']}
            return
        if not result.get('due'):
            _LAST_RUN = {'finished_at': now.strftime('%Y-%m-%d %H:%M:%S'),
                         'candidate_key': '', 'due_at': result.get('due_at', ''),
                         'reminded': False, 'email_ok': None, 'error': None}
            return
        if result.get('already'):
            # 本格已提醒过且没有新打卡：不重发（防轰炸），等下一次打卡重开倒计时
            return

        ok = _send_reminder(result)
        _LAST_RUN = {'finished_at': now.strftime('%Y-%m-%d %H:%M:%S'),
                     'candidate_key': result['key'], 'due_at': result['due_at'],
                     'reminded': bool(ok), 'email_ok': ok, 'error': None}
        if ok:
            _save_state({'last_reminded_key': result['key'],
                         'reminded_at': now.strftime('%Y-%m-%d %H:%M:%S')})
            task_log.info(f"[CheckinReminder] 倒计时结束提醒已发送（上次打卡 "
                          f"{result['filled_at']}，{result['minutes']} 分钟窗）")
        else:
            task_log.warning('[CheckinReminder] 提醒邮件发送失败，下个周期重试')
    except Exception as e:
        logger.error(f'[CheckinReminder] 巡检异常: {e}', exc_info=True)
        _LAST_RUN = {'finished_at': now.strftime('%Y-%m-%d %H:%M:%S'),
                     'candidate_key': '', 'due_at': '', 'reminded': False,
                     'email_ok': None, 'error': str(e)}


# =============================================================================
# 调度注册（scheduler.register_default_jobs 与配置保存共用）
# =============================================================================

def register_checkin_reminder_job():
    """注册打卡倒计时提醒任务，返回 (job_id 或 None, 说明文本)。

    开关关闭时 register_job 自身会拦下注册（统一 task_switches 守卫）；
    配置 enabled=false 时显式移除已注册任务，支持热更新。
    """
    from ..scheduler import task_scheduler

    cfg = load_config()
    if not cfg.get('enabled', True):
        task_scheduler.remove_job(CHECKIN_JOB_ID)
        return None, '配置已关闭（checkin_reminder_config.enabled=false）'
    job_id = task_scheduler.register_job(
        run_checkin_reminder_check, trigger='interval',
        seconds=CHECK_INTERVAL_SECONDS,
        job_id=CHECKIN_JOB_ID,
        job_name='打卡倒计时提醒（距上次打卡满 N 分钟未续打则邮件提醒休息/继续）')
    note = f'每 {CHECK_INTERVAL_SECONDS}s 巡检 / 倒计时 {cfg.get("minutes")} 分钟'
    return job_id, note
