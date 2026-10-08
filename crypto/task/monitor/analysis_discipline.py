#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析纪律巡检引擎（批次11）
==============================
每 5 分钟一轮，只做三件事：

1. 判定：把「已完结且过宽限期」的小时槽逐个判合格，幂等 upsert 到
   analysis_reminder_log（hour_slot 唯一键），并处理 missing → satisfied_later
   的回补迁移（先缺后补记）。
2. 打扰：新产生的缺口按策略发邮件——首个缺口即时单发，其后连续缺口合并成
   一封断档汇总（防轰炸）；已 notified 的缺口绝不重发。
3. 日报：每天到点发一封「分析纪律日报」（今日合格/缺口/补记率 + 最长断档提示）。

【关于调策略引擎的约束】巡检不代生成分析记录，但缺口提醒信会附一份「当前策略
行情表」（让人看一眼就该不该动手，而不是只能空着手去补记录）。两条红线：
1. 取行情必须走 DualPeriodStrategyAdapter.analyze()，它整体在策略计算全局锁
   （strategy_gate）下执行；引擎的模块级全局（FAST_MODE / PRINT_*）是共享
   可变状态，中途被别的线程恢复会让实盘在错误时点走强平分支。
2. 取回来的行情**只进邮件正文，绝不写进 task_analysis_records**：代生成的
   记录里没有用户自己的判断，会把命中率统计彻底稀释成噪声。
拿不到锁或取价失败就降级成「行情本轮没取到」，提醒照发。
提醒的目的是让人动手，不是替人动手。

【失败取向】台账落库失败只记日志，不阻断巡检主循环；邮件失败不影响台账。
配置存 kv_store key='analysis_discipline_config'，由 discipline_routes 保存后
调 register_discipline_job 热重注册。
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
    from crypto import discipline_repo as disc
except ImportError:
    session_scope = None
    disc = None

logger = logging.getLogger(__name__)
task_log = get_task_logger()

# 调度任务 ID（与 scheduler 注册/热重注册口径一致）
DISCIPLINE_JOB_ID = 'analysis_discipline'

# 巡检周期（秒）：5 分钟一轮。宽限期默认 15 分钟，缺口最迟在槽结束后
# 15~20 分钟内被判定并打扰，节拍足够紧又不会让邮件在整点齐发。
CHECK_INTERVAL_SECONDS = 300

# 分析记录页直达链接（缺口邮件里的行动入口）
ANALYSIS_PAGE_PATH = '/analysis'

# Web 服务端口（app.py 里 app.run 用的那个）：自动探测站点地址时拼上去
DEFAULT_WEB_PORT = 7777

_LAST_RUN = {
    'finished_at': None,
    'duration_ms': 0,
    'judged': 0,          # 本轮判定的槽数
    'satisfied': 0,       # 合格（含补记补齐）
    'missing': 0,         # 本轮新确认的缺口
    'resolved': 0,        # 本轮由 missing 迁移为 satisfied_later 的槽数
    'exempt': 0,
    'email_sent': 0,
    'email_ok': None,
    'daily_report': False,
    'maintenance': '',    # 非空表示本轮因维护暂停而跳过（值=截止时刻）
    'error': None,
}


def get_last_run() -> dict:
    """最近一轮巡检摘要（副本），供 check-now 接口与状态展示"""
    return dict(_LAST_RUN)


def _admin_email() -> str:
    """收件人：优先包内相对导入，回退 task 目录绝对导入（与既有模块两种跑法都兼容）"""
    try:
        from ..config.email_config import get_admin_email
        return get_admin_email()
    except Exception:
        try:
            from config.email_config import get_admin_email
            return get_admin_email()
        except Exception as e:
            task_log.warning(f'[Discipline] 读取管理员邮箱失败: {e}')
            return ''


def _server_base_url() -> str:
    """邮件里直达链接的站点前缀，务必给出绝对地址。

    之前只读一个项目里从没设置过的环境变量，取不到就退回相对路径 /analysis。
    邮件客户端没有 base URL 的概念，相对路径点出去会跳到邮件域的 404——
    而「提醒 → 一键进去补分析」正是这套邮件的全部价值，死链等于功能没做。
    三级回退：部署时显式指定 → 探测本机可路由地址 → 回环地址。
    """
    import os
    import socket

    env = str(os.environ.get('CRYPTO_WEB_BASE_URL', '') or '').strip().rstrip('/')
    if env:
        return env
    port = str(os.environ.get('CRYPTO_WEB_PORT', '') or '').strip() or str(DEFAULT_WEB_PORT)
    try:
        # UDP connect 并不真的发包，只是让内核按路由表选出默认出口网卡的地址，
        # 所以没有外网路由时同样能拿到局域网 IP；再失败就退回回环。
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.settimeout(0.2)
            sk.connect(('8.8.8.8', 80))
            ip = sk.getsockname()[0]
        if ip and not ip.startswith('127.'):
            return f'http://{ip}:{port}'
    except OSError:
        pass
    return f'http://127.0.0.1:{port}'


def _link_is_local() -> bool:
    """站点地址是否为本机/私网地址（决定要不要给可达性提示）"""
    from urllib.parse import urlparse
    host = urlparse(_server_base_url()).hostname or ''
    if host in ('', 'localhost'):
        return True
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        return bool(ip.is_loopback or ip.is_private)
    except ValueError:
        return False


# =============================================================================
# 待判定槽集合
# =============================================================================

def _pending_slots(cfg: dict, now: datetime.datetime) -> list:
    """本轮需要判定的小时槽（升序）：已完结 + 过宽限期 + 在生效时段内。

    只回看最近 LOOKBACK_SLOTS 个槽：幂等、有界，巡检永远不会全表扫历史。
    另受「纪律生效起点」约束：上线之前就开始的槽不判定、不写台账，
    避免功能首轮把历史空白全刷成缺档（详见 disc.ensure_epoch 注释）。
    """
    grace = max(0, int(cfg.get('grace_minutes', 15) or 0))
    cutoff = now - datetime.timedelta(minutes=grace)
    epoch = disc.epoch_dt()
    slots = []
    for back in range(disc.LOOKBACK_SLOTS, 0, -1):
        slot_dt = now - datetime.timedelta(hours=back)
        slot = disc.hour_slot_of(slot_dt)
        # 槽终点必须已过宽限期，否则还在"可以补记"的窗口内，不判缺
        if disc.slot_end(slot) > cutoff:
            continue
        if not disc.is_slot_active(slot, cfg):
            continue
        # 起点之前开始的槽：功能当时还不存在，无从追责
        if epoch is not None and disc.slot_start(slot) < epoch:
            continue
        slots.append(slot)
    return slots


def _is_exempt(slot: str) -> bool:
    """该槽是否落在一次性豁免窗口内（槽起点早于豁免截止即豁免）"""
    until = disc.exempt_until()
    if until is None:
        return False
    try:
        return disc.slot_start(slot) < until
    except ValueError:
        return False


# =============================================================================
# 台账 upsert（幂等）
# =============================================================================

def _upsert_slot(session, slot: str, cfg: dict) -> dict:
    """判定单个槽并幂等写台账，返回 {'row', 'status', 'is_new_gap'}。

    迁移规则：
      无台账行     → 按判定结果新建（satisfied / missing / exempt）
      missing      → 现在合格了则转 satisfied_later 并记 resolved_at（先缺后补记）
      satisfied_later / satisfied / exempt → 保持不动（不反复churn）
    """
    from crypto.models import AnalysisReminderLog
    from sqlalchemy import select

    exempt = _is_exempt(slot)
    ev = disc.evaluate_slot(session, slot, cfg)
    ok = True if exempt else ev['ok']
    status = disc.ST_EXEMPT if exempt else (disc.ST_SATISFIED if ok else disc.ST_MISSING)

    row = session.execute(
        select(AnalysisReminderLog).where(AnalysisReminderLog.hour_slot == slot)
    ).scalars().first()

    is_new_gap = False
    resolved = False
    if row is None:
        row = AnalysisReminderLog(
            hour_slot=slot,
            stat_date=slot[:10],
            required_count=int(ev.get('required', 1)),
            actual_count=int(ev.get('actual', 0)),
            missing_insts=_dump_missing(ev),
            status=status,
        )
        session.add(row)
        is_new_gap = (status == disc.ST_MISSING)
    else:
        row.actual_count = int(ev.get('actual', 0))
        row.required_count = int(ev.get('required', 1))
        if row.status == disc.ST_MISSING and status != disc.ST_MISSING:
            # 先缺后补记：如实记为 satisfied_later，看板据此算补记率
            row.status = disc.ST_SATISFIED_LATER
            row.resolved_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            row.missing_insts = _dump_missing(ev)
            resolved = True
        elif row.status == disc.ST_EXEMPT and status != disc.ST_EXEMPT:
            # 豁免被取消：回落为真实判定结果
            row.status = status
    session.flush()
    return {'row': row, 'status': row.status, 'is_new_gap': is_new_gap,
            'resolved': resolved, 'evaluation': ev}


def _dump_missing(ev: dict) -> str:
    """未覆盖币种清单序列化（require_cover_tracked 关闭时为空数组文本）"""
    import json
    try:
        return json.dumps(ev.get('missing_coins') or [], ensure_ascii=False)
    except (TypeError, ValueError):
        return '[]'


def run_discipline_check():
    """执行一轮巡检（调度器周期调用 / check-now 手动触发共用）"""
    global _LAST_RUN
    if session_scope is None or disc is None:
        task_log.warning('[Discipline] DB/纪律模块不可用，本轮巡检跳过')
        return

    started = datetime.datetime.now()
    run_id = started.strftime('D%Y%m%d-%H%M%S')
    judged = satisfied = missing = resolved = exempt = 0
    sent = 0
    email_ok = None
    report_done = False
    error = None

    try:
        cfg = disc.load_config()
        if not cfg.get('enabled'):
            task_log.info(f'[{run_id}] 分析纪律已关闭，本轮跳过')
            _LAST_RUN = {'finished_at': started.strftime('%Y-%m-%d %H:%M:%S'),
                         'duration_ms': 0, 'judged': 0, 'satisfied': 0, 'missing': 0,
                         'resolved': 0, 'exempt': 0, 'email_sent': 0, 'email_ok': None,
                         'daily_report': False, 'maintenance': '', 'error': None}
            return

        # 维护暂停：给需要临时改共享配置的验证（HTTP 冒烟）让路，避免生产
        # 调度器读到试验值并把假判定写进真台账、据此真发信（租约机制见
        # disc.begin_maintenance 注释）。不看台账也不发信，也不能静默失败：
        # 每轮留一条日志，不然“今天怎么没提醒”根本查不出来。
        mu = disc.maintenance_until()
        if mu is not None:
            until_str = mu.strftime('%Y-%m-%d %H:%M:%S')
            task_log.info(f'[{run_id}] 纪律巡检维护暂停中（至 {until_str}，'
                          f'{disc.maintenance_reason() or "未填写原因"}），'
                          f'本轮不判定、不发信')
            _LAST_RUN = {'finished_at': until_str,
                         'duration_ms': 0, 'judged': 0, 'satisfied': 0, 'missing': 0,
                         'resolved': 0, 'exempt': 0, 'email_sent': 0, 'email_ok': None,
                         'daily_report': False, 'maintenance': until_str, 'error': None}
            return

        # 首轮记下生效起点（已记录则为空操作），后续槽判定以此为下界
        epoch = disc.ensure_epoch()
        slots = _pending_slots(cfg, started)
        with session_scope() as s:
            for slot in slots:
                try:
                    r = _upsert_slot(s, slot, cfg)
                except Exception as e:
                    task_log.warning(f'[{run_id}] {slot} 台账写入失败，跳过: {e}')
                    continue
                judged += 1
                st = r['status']
                if st == disc.ST_EXEMPT:
                    exempt += 1
                elif st == disc.ST_MISSING:
                    missing += 1
                else:
                    satisfied += 1
                if r['resolved']:
                    resolved += 1

            # 口径变更清账：先删掉现行时段/起点下不该存在的缺档行，再看板与
            # 邮件两条路径才能拿到同一份干净数据（时段改窄后旧行不会再来烦人）
            _purge_unaccountable(s, cfg, run_id)

            # 缺口邮件：在同一事务内读取待通知行并回写 notified，避免重复发送
            email_cfg = cfg.get('email', {}) or {}
            if email_cfg.get('on_gap', True) and _email_send_ok(cfg, email_cfg):
                sent, email_ok = _notify_gaps(s, cfg, run_id)

        # 日报独立事务：即使台账写入失败也应按时发出
        report_at = str((cfg.get('email', {}) or {}).get('daily_report', '23:00') or '')
        if report_at:
            report_done = _maybe_send_daily_report(cfg, report_at, started, run_id)

        task_log.info(f'[{run_id}] 分析纪律巡检完成（生效起点 {epoch}）：判定 {judged} 槽 '
                      f'（合格 {satisfied} / 缺口 {missing} / 豁免 {exempt} / 补齐 {resolved}），'
                      f'邮件 {sent} 封')
    except Exception as e:
        error = str(e)
        task_log.error(f'[{run_id}] 分析纪律巡检异常: {e}')

    _LAST_RUN = {
        'finished_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'duration_ms': int((datetime.datetime.now() - started).total_seconds() * 1000),
        'judged': judged,
        'satisfied': satisfied,
        'missing': missing,
        'resolved': resolved,
        'exempt': exempt,
        'email_sent': sent,
        'email_ok': email_ok,
        'daily_report': report_done,
        'maintenance': '',
        'error': error,
    }


# =============================================================================
# 缺口邮件（首个缺口即时单发，其后连续缺口合并成一封断档汇总）
# =============================================================================

def _still_accountable(slot: str, cfg: dict, epoch) -> bool:
    """这条缺口行按「现行口径」是否仍应追责：在生效时段内、且不早于生效起点。

    台账行是当时写的，口径一改窄就会剩下一批“从来没被要求过”的缺口。
    不复核就直接发，等于拿系统自己的口径变化去算用户的账。
    """
    if not disc.is_slot_active(slot, cfg):
        return False
    if epoch is None:
        return True
    try:
        return disc.slot_start(slot) >= epoch
    except ValueError:
        return True


def _purge_unaccountable(session, cfg: dict, run_id: str) -> int:
    """删掉按现行口径根本不该存在的缺档行，返回删除数。

    为什么是删而不是标记：这类行是无效判定记录（当时读的是另一个时段/或
    当时还没这套规则），留着看板的断档热力图就会把用户睡觉的时间染红。实际
    发过这种事故：时段一度被撑成 00:00-23:59，巡检据此写出凌晨 00~07 共 8 行
    缺档并汇总发信。行删掉不会丢数据：这些槽仍在回看窗口内，口径合
    格后下一轮巡检会重新如实判定。
    """
    from crypto.models import AnalysisReminderLog
    from sqlalchemy import select

    epoch = disc.epoch_dt()
    rows = session.execute(
        select(AnalysisReminderLog)
        .where(AnalysisReminderLog.status == disc.ST_MISSING)
    ).scalars().all()
    doomed = [r for r in rows if not _still_accountable(r.hour_slot, cfg, epoch)]
    for r in doomed:
        session.delete(r)
    if doomed:
        slots = '、'.join(r.hour_slot for r in doomed[:6])
        task_log.info(f'[{run_id}] 清除不在当前生效时段/起点内的缺档行 {len(doomed)} 行：'
                      f'{slots}{" …" if len(doomed) > 6 else ""}')
    return len(doomed)


def _group_contiguous(rows):
    """按小时连续性把缺口行分组（不升序则先排）。

    抽成纯函数是为了它能拿假行直接测：上一轮验证要靠往真台账里造行才能跑
    分组，而造行就会与环境里正在跑的巡检互相干扰。
    """
    def key(r):
        try:
            return disc.slot_start(r.hour_slot)
        except ValueError:
            return datetime.datetime.max

    groups, cur = [], []
    for r in sorted(rows, key=key):
        if cur:
            try:
                prev_end = disc.slot_start(cur[-1].hour_slot) + datetime.timedelta(hours=1)
                contiguous = disc.slot_start(r.hour_slot) == prev_end
            except ValueError:
                contiguous = False
            if not contiguous:
                groups.append(cur)
                cur = []
        cur.append(r)
    if cur:
        groups.append(cur)
    return groups


def _notify_gaps(session, cfg: dict, run_id: str):
    """处理台账中尚未通知的缺口，返回 (发送封数, 是否成功)。

    策略：把待通知缺口按小时连续性分组——
      组内只有 1 个缺口            → 即时单发（第一声打扰要快）
      组内 >= merge_after 个缺口   → 合并为一封断档汇总（防轰炸）
    整组一起标记 notified，已通知的缺口永不重发。

    调用前必须先跑过 _purge_unaccountable；下面的复核只是防御（万一有人跳过
    清理直接调本函数，也不能拿不该追责的槽去发信）。
    """
    from crypto.models import AnalysisReminderLog
    from sqlalchemy import select

    rows = session.execute(
        select(AnalysisReminderLog)
        .where(AnalysisReminderLog.status == disc.ST_MISSING,
               AnalysisReminderLog.notified == False)   # noqa: E712
        .order_by(AnalysisReminderLog.hour_slot.asc())
    ).scalars().all()
    if not rows:
        return 0, None

    epoch = disc.epoch_dt()
    rows = [r for r in rows if _still_accountable(r.hour_slot, cfg, epoch)]
    if not rows:
        return 0, None

    merge_after = max(1, int((cfg.get('email', {}) or {}).get('merge_after', 2) or 2))
    groups = _group_contiguous(rows)

    notifier = MessageNotifier()
    admin = _admin_email()
    if not admin:
        task_log.warning(f'[{run_id}] 未配置收件邮箱，缺口邮件跳过')
        return 0, None

    sent = 0
    ok_all = None
    strict = str(cfg.get('strict_mode') or 'strict')
    # 行情只在“确实要发信”时取一次（一轮多封信共用同一份数据）；取价过
    # 程在本事务内，会多挂十几秒数据库事务——发信本身就说明用户已落后，
    # 且单币拿不到锁只等 8s、整批有 25s 预算，不会把事务无限撑下去。
    market_prov = _market_provider(run_id)
    for group in groups:
        if len(group) >= merge_after:
            ok = _send_gap_digest(notifier, admin, group, strict,
                                  _market_table_html(market_prov))
            subject_rows = group
        else:
            ok = _send_single_gap(notifier, admin, group[0], strict,
                                  _market_table_html(market_prov))
            subject_rows = group
        if ok:
            sent += 1
        ok_all = ok if ok_all is None else (ok_all and ok)
        if ok:
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for r in subject_rows:
                r.notified = True
                r.notified_at = now_str
            task_log.info(f'[{run_id}] 缺口邮件已发送：'
                          f'{group[0].hour_slot}~{group[-1].hour_slot}（{len(group)} 槽）')
        else:
            # 发送失败不标记 notified，下一轮自动重试
            task_log.warning(f'[{run_id}] 缺口邮件发送失败，下轮重试：{group[0].hour_slot}')
    session.flush()
    return sent, ok_all


def _gap_link() -> str:
    return f'{_server_base_url()}{ANALYSIS_PAGE_PATH}'


def _module_switch_on() -> bool:
    """任务总开关（task_switches.analysis_discipline：/task 页 kv 开关 + config.json 文件急停）；
    取不到/异常按开处理。

    这是分析纪律功能的总闸：关掉后后台巡检、台账判定、打卡闸门、
    网页倒计时横幅/角标全部静默（见 discipline_repo.module_enabled）。
    只想停邮件保留看板/闸门时，用配置里的 email.enabled，不要关这里。
    """
    try:
        from ..scheduler import task_switch
        return task_switch('analysis_discipline')
    except Exception as e:
        task_log.warning(f'[Discipline] 读取任务开关失败，按开启处理: {e}')
        return True


def _email_send_ok(cfg: dict, email_cfg: dict) -> bool:
    """邮件打扰总开关：kv 配置 email.enabled 与 config.json 同名开关，
    任一关闭即不发（config.json 侧是免登录的一键急停）。

    关闭时缺口行不标记 notified：台账照常判定，重新打开后积压的缺口
    按连续性分组补发/合并，不会静默吞掉，也不会逐小时轰炸。
    """
    if not email_cfg.get('enabled', True):
        return False
    return _module_switch_on()


def _link_hint() -> str:
    """自动探测出来的多半是内网地址，离开该网段就点不开。

    不写清楚的话，在外面点开失败会让人误以为系统坏了，反倒更不愿理这些提醒；
    如实标一句「需同网络」并说明如何换成公网地址，比默默给一个打不开的死链好。
    """
    if not _link_is_local():
        return ''
    return ('<p class="timestamp">链接指向本机/局域网地址，需在同一网络内才能打开；'
            '跨网使用请设 CRYPTO_WEB_BASE_URL 为公网地址。</p>')


def _td(text, extra=''):
    return f"<td style='padding:8px 10px;border-bottom:1px solid #eee{extra}'>{text}</td>"


def _gate_phrase(strict: str) -> str:
    """按实际闸门模式描述后果。

    写死「打卡已被闸门拦住」在 soft/off 下就是不实陈述——提醒信一旦
    有一处夸大，收件人就会开始怀疑其余所有数字。与归因栏「样本不足」
    同一个道理：宁可说得准，不要说得狠。
    """
    if strict == 'soft':
        return ('<p>当前是提醒模式：打卡不会被拦，但会留下「未分析直接打卡」的记号，'
                '看板里的 bypass 比例就是用它算的。</p>')
    if strict == 'off':
        return '<p>闸门目前不介入打卡，这封信只做提醒。</p>'
    return '<p>交易打卡已被闸门拦住——先分析，再打卡。</p>'


def _money(x) -> str:
    """加密币价格跳几个量级，固定小数位会把 PENGU 这种小价币抹成 0.0000"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return '—'
    if abs(v) >= 1000:
        return f'{v:,.2f}'
    if abs(v) >= 1:
        return f'{v:,.4f}'
    return f'{v:.6f}'


def _market_provider(run_id: str):
    """返回一个“要渲染时才去取行情”的闭包。

    为什么不直接传行情数据：不发信的轮次根本不该取（每 5 分钟跑 5 次引擎
    计算是白烧），而一轮里多封信又只需取一次——取一次、复用、拿不到锁就
    降级，全靠这个惰性闭包实现。
    """
    cache = {}

    def get():
        if 'v' not in cache:
            try:
                try:
                    from ..strategy_adapter import market_snapshot
                except ImportError:
                    from strategy_adapter import market_snapshot
                cache['v'] = market_snapshot(disc.tracked_currencies())
            except Exception as e:
                task_log.warning(f'[{run_id}] 策略行情取数失败: {e}')
                cache['v'] = ([], [], f'行情取数异常：{e}')
        return cache['v']
    return get


def _market_table_html(provider) -> str:
    """「当前策略计算行情」表格；取不到时给一句实话而不是空表"""
    # 惰性导入：strategy_adapter 会拉起 backtrader/pandas，不能在模块顶层进
    # （否则应用启动就要多扛一个策略引擎，与其他快照路由的做法一致）
    try:
        from ..strategy_adapter import dir_cn
    except ImportError:
        from strategy_adapter import dir_cn

    items, failed, note = provider()
    if not items:
        return ('<p style="color:#888;font-size:13px">策略行情本轮没取到'
                + (f'（{note}）' if note else '')
                + '。不影响你照常记录——打开下面页面自己刷一次即可。</p>')
    trs = ''
    for it in items:
        boll = ' / '.join(_money(it.get(k)) for k in
                          ('boll_upper', 'boll_middle', 'boll_lower'))
        trs += ('<tr>' + _td(f"<b>{it.get('instId') or '—'}</b>")
                + _td(_money(it.get('price')))
                + _td(f"{dir_cn(it.get('short_dir'))}"
                      f"<span style='color:#999'>（{it.get('short_period') or ''}）</span>")
                + _td(f"{dir_cn(it.get('long_dir'))}"
                      f"<span style='color:#999'>（{it.get('long_period') or ''}）</span>")
                + _td(dir_cn(it.get('long_dir_prev')))
                + _td(f"{it.get('atr_pct') or 0:.2f}%")
                + _td(f"<span style='font-size:12px'>{boll}</span>")
                + '</tr>')
    tail = ''
    if failed:
        tail = (f'<p class="timestamp">另有 {len(failed)} 个币种本轮未取到'
                f'：{ "、".join(failed[:6]) }{" …" if len(failed) > 6 else ""}</p>')
    elif note:
        tail = f'<p class="timestamp">{note}</p>'
    return f"""
    <p style="margin-top:16px"><b>当前策略计算行情</b>
    <span style="color:#999;font-size:12px">（取数时间 {items[0].get('ts') or ''}，
    与实盘同源的双周期引擎；只是参考，不是建议）</span></p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background-color:#f5f5f5;color:#666">
            <th style="padding:8px 10px;text-align:left">币种</th>
            <th style="padding:8px 10px;text-align:left">现价</th>
            <th style="padding:8px 10px;text-align:left">短周期</th>
            <th style="padding:8px 10px;text-align:left">长周期</th>
            <th style="padding:8px 10px;text-align:left">长周期上时段</th>
            <th style="padding:8px 10px;text-align:left">ATR%</th>
            <th style="padding:8px 10px;text-align:left">BOLL 上/中/下</th>
        </tr>
        {trs}
    </table>
    {tail}
    """


def _send_single_gap(notifier, admin: str, row, strict: str = 'strict',
                     market_html: str = '') -> bool:
    """单个缺口：即时单发，正文直接给出行动入口 + 当前行情"""
    slot = row.hour_slot
    subject = f'📝 分析缺口 · {slot}:00 这一小时没有分析记录'
    body = f"""
    <p><b>{slot}:00 ~ {slot}:59</b> 这一小时没有留下分析记录
    （要求 {row.required_count} 条，实际 {row.actual_count} 条）。</p>
    {_gate_phrase(strict)}
    {market_html}
    <p style="margin-top:14px"><a href="{_gap_link()}" style="display:inline-block;padding:10px 18px;
       background:#0b5ed7;color:#fff;border-radius:8px;text-decoration:none;
       font-weight:bold">📸 打开分析记录页，一键批量快照</a></p>
    {_link_hint()}
    <p class="timestamp">判定时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    """
    html = EmailTemplates._wrap_html(subject, body, header_color='#e07b00')
    return notifier._dispatch_email(to_emails=[admin], subject=subject,
                                   html_content=html, log_label='分析缺口')


def _digest_tail(strict: str) -> str:
    if strict == 'off':
        return '闸门暂不介入，但这段时间的空白会如实记在看板上。'
    if strict == 'soft':
        return '闸门会记下每一次没有分析支撑的打卡。'
    return '但打卡闸门会一直等着你的分析。'


def _send_gap_digest(notifier, admin: str, rows, strict: str = 'strict',
                     market_html: str = '') -> bool:
    """连续缺口：合并为一封断档汇总（防轰炸）"""
    first, last = rows[0].hour_slot, rows[-1].hour_slot
    subject = f'📝 分析断档汇总 · {first}:00 起连续 {len(rows)} 小时未分析'
    trs = ''.join(
        '<tr>' + _td(f'<b>{r.hour_slot}:00</b>') + _td(f'{r.actual_count} / {r.required_count}')
        + _td(r.missing_insts or '—') + '</tr>'
        for r in rows)
    body = f"""
    <p>从 <b>{first}:00</b> 到 <b>{last}:59</b> 连续 <b>{len(rows)}</b> 个小时没有分析记录，
    合并为一封提醒（避免逐小时轰炸）。</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background-color:#f5f5f5;color:#666">
            <th style="padding:8px 10px;text-align:left">小时槽</th>
            <th style="padding:8px 10px;text-align:left">实际/要求</th>
            <th style="padding:8px 10px;text-align:left">未覆盖币种</th>
        </tr>
        {trs}
    </table>
    <p style="margin-top:14px">断档不罚款、不归零，如实呈现即可。
    回来永远被欢迎——{_digest_tail(strict)}</p>
    {market_html}
    <p style="margin-top:14px"><a href="{_gap_link()}" style="display:inline-block;padding:10px 18px;
       background:#0b5ed7;color:#fff;border-radius:8px;text-decoration:none;
       font-weight:bold">📸 补上回溯分析记录</a></p>
    {_link_hint()}
    <p class="timestamp">判定时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    """
    html = EmailTemplates._wrap_html(subject, body, header_color='#d32f2f')
    return notifier._dispatch_email(to_emails=[admin], subject=subject,
                                   html_content=html, log_label='分析断档汇总')


# =============================================================================
# 每日纪律日报
# =============================================================================

def _maybe_send_daily_report(cfg: dict, report_at: str, now: datetime.datetime,
                             run_id: str) -> bool:
    """到点发日报；用运行时状态里的日期做幂等，一天只发一封"""
    today = now.strftime('%Y-%m-%d')
    try:
        hh, mm = int(report_at[:2]), int(report_at[3:5])
    except (ValueError, IndexError):
        return False
    if now < now.replace(hour=hh, minute=mm, second=0, microsecond=0):
        return False

    state = disc.load_state()
    if str(state.get('daily_report_date') or '') == today:
        return False

    # 邮件总开关关闭：日报直接跳过，且不记 daily_report_date——
    # 没发出去就不算发过，重新打开后当晚仍可补发
    if not _email_send_ok(cfg, cfg.get('email', {}) or {}):
        task_log.info(f'[{run_id}] 邮件打扰已关闭，跳过纪律日报（不记已发日期）')
        return False

    try:
        with session_scope() as s:
            board = disc.build_board(s, days=1, cfg=cfg)
            digest = disc.gap_digest(s, days=7)
    except Exception as e:
        task_log.error(f'[{run_id}] 纪律日报生成失败: {e}')
        return False

    # 今天一个槽都没判定过（功能当天才启用、整天在生效时段外、或全天豁免）：
    # 这时 summary 是 ok=0/missing=0/rate=0.0，照发出去就是一封
    # 「今日合规率 0%」的信——拿系统的空白去制造愧疚，正是这套纪律
    # 最该避免的事。跳过且不记 daily_report_date：没发出去就不算发过。
    s_today = board.get('summary', {}) or {}
    judged = int(s_today.get('ok', 0) or 0) + int(s_today.get('missing', 0) or 0)
    if judged <= 0:
        task_log.info(f'[{run_id}] 今日无已判定小时槽，跳过纪律日报（不发空数据）')
        return False

    try:
        ok = _send_daily_report(today, board, digest)
    except Exception as e:
        task_log.error(f'[{run_id}] 纪律日报发送异常: {e}')
        return False

    # 无论发送成功与否都记日期：失败不重试到深夜反复打扰，次日自然恢复
    state['daily_report_date'] = today
    disc.save_state(state)
    if ok:
        task_log.info(f'[{run_id}] 分析纪律日报已发送（{today}）')
    return bool(ok)


def _send_daily_report(today: str, board: dict, digest: dict) -> bool:
    admin = _admin_email()
    if not admin:
        return False
    s = board.get('summary', {})
    bf = board.get('backfill', {})
    at = board.get('attribution', {})
    streak = board.get('streak', {})
    wa, wo = at.get('with_analysis', {}), at.get('without_analysis', {})

    subject = f'📊 分析纪律日报 · {today} 合规率 {s.get("rate", 0)}%'
    bf_txt = ('暂无样本' if not int(bf.get('total', 0) or 0)
              else f'{bf.get("rate", 0)}%（{bf.get("backfill", 0)}/{bf.get("total", 0)} 条）')

    def _rate_txt(g):
        """零样本不印 0.0%：那是「还不知道」，不是「命中率为零」。

        打卡命中要等记录满 1H/4H 后惰性回填 K线才算得出，当天填的记录
        两栏必定都是 0 —— 与同信「差值=样本不足」「事后补记率=暂无样本」
        保持一套标准，不能一处说不知道、另一处说你是零。
        """
        if not int((g or {}).get('total', 0) or 0):
            return '暂无样本'
        return f'{g.get("rate", 0)}%'

    diff_txt = '样本不足' if at.get('rate_diff') is None else f'{at["rate_diff"]:+.1f} 个百分点'
    top_hours = '、'.join(f'{h["hour"]:02d}:00（{h["count"]} 次）'
                          for h in (digest.get('top_hours') or [])) or '—'
    body = f"""
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:14px">
        <tr style="background-color:#f5f5f5;color:#666">
            <th style="padding:8px 10px;text-align:left">指标</th>
            <th style="padding:8px 10px;text-align:left">今日</th>
        </tr>
        <tr>{_td('合规率')}{_td(f'<b>{s.get("rate", 0)}%</b>')}</tr>
        <tr>{_td('合格 / 缺口 / 豁免')}{_td(f'{s.get("ok", 0)} / {s.get("missing", 0)} / {s.get("exempt", 0)}')}</tr>
        <tr>{_td('先缺后补记')}{_td(f'{s.get("satisfied_later", 0)} 个小时')}</tr>
        <tr>{_td('连续全合规')}{_td(f'当前 {streak.get("current", 0)} 天 · 历史最佳 {streak.get("best", 0)} 天')}</tr>
        <tr>{_td('事后补记率')}{_td(bf_txt)}</tr>
    </table>
    <p style="font-weight:bold;margin-bottom:6px">归因对比：有分析支撑 vs 无分析支撑的打卡命中率</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:14px">
        <tr style="background-color:#f5f5f5;color:#666">
            <th style="padding:8px 10px;text-align:left">分组</th>
            <th style="padding:8px 10px;text-align:left">命中率</th>
            <th style="padding:8px 10px;text-align:left">样本</th>
        </tr>
        <tr>{_td('✅ 有分析支撑')}{_td(f'<b>{_rate_txt(wa)}</b>')}{_td(f'{wa.get("total", 0)} 次打卡')}</tr>
        <tr>{_td('⚠️ 无分析支撑')}{_td(f'<b>{_rate_txt(wo)}</b>')}{_td(f'{wo.get("total", 0)} 次打卡')}</tr>
        <tr>{_td('差值')}{_td(f'<b>{diff_txt}</b>')}{_td('—')}</tr>
    </table>
    <p>近 7 天最长连续断档：<b>{digest.get('longest_gap') or '无'}</b><br>
       高发缺档时段：<b>{top_hours}</b></p>
    <p style="color:#666;font-size:12px">断档不罚款、不归零。数据诚实，情绪友好——
    看见自己在哪个时段最容易滑，本身就是最有效的改进。</p>
    <p><a href="{_gap_link()}" style="display:inline-block;padding:10px 18px;
       background:#0b5ed7;color:#fff;border-radius:8px;text-decoration:none;
       font-weight:bold">📝 打开分析记录</a></p>
    {_link_hint()}
    """
    html = EmailTemplates._wrap_html(subject, body, header_color='#1a7f42')
    try:
        return MessageNotifier()._dispatch_email(
            to_emails=[admin], subject=subject, html_content=html, log_label='分析纪律日报')
    except Exception as e:
        task_log.error(f'[Discipline] 日报发送异常: {e}')
        return False


# =============================================================================
# 调度注册（scheduler.register_default_jobs 与配置保存接口共用）
# =============================================================================

def register_discipline_job() -> int:
    """按当前配置注册/热重注册巡检任务，返回巡检周期（秒）。

    关闭纪律引擎时移除已注册任务。APScheduler replace_existing 保证幂等。
    """
    from ..scheduler import task_scheduler

    if disc is None:
        task_log.warning('[Discipline] 纪律模块不可用，巡检任务未注册')
        return CHECK_INTERVAL_SECONDS

    cfg = disc.load_config()
    interval = CHECK_INTERVAL_SECONDS
    if cfg.get('enabled') and not _module_switch_on():
        task_scheduler.remove_job(DISCIPLINE_JOB_ID)
        task_log.info('[Discipline] task_switches 已关闭分析纪律，'
                      '巡检任务未注册（台账/闸门/网页倒计时提醒随总闸一并静默）')
        return 0
    if cfg.get('enabled'):
        task_scheduler.register_job(
            run_discipline_check, trigger='interval', seconds=interval,
            job_id=DISCIPLINE_JOB_ID,
            job_name='分析纪律巡检（小时槽合格判定/缺口提醒/打卡闸门与倒计时提醒）')
    else:
        task_scheduler.remove_job(DISCIPLINE_JOB_ID)
    return interval
