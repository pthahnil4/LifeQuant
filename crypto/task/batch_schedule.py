#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量趋势自动分析 + 每日强币邮件（batch_schedule）
==================================================
两个定时需求共用一个 cron 任务（一个任务体两种职责会互相牵制，但拆成两个
任务反而保证不了「邮件在 08:05 那轮分析结束后发送」的时序 —— 链式触发
天然满足，无需轮询等待竞态）：

任务一：每 4 小时自动批量趋势分析
    每天 00:05 / 04:05 / 08:05 / 12:05 / 16:05 / 20:05 触发，调用与手动
    入口 /api/batch/update 完全相同的底层引擎（BatchTrendAnalyzer）。
    并发防冲突是双层的：
      - APScheduler 任务级 max_instances=1（同任务两次触发不重叠）；
      - 引擎侧 _batch_run_lock（与手动触发互斥）：上一轮（无论手动还是
        定时）未结束时 _claim 抛 BatchAlreadyRunning → 本任务记日志跳过，
        绝不排队重跑。
    同步执行在调度线程内（BackgroundScheduler 默认 10 个 worker，占用
    一个不影响其他任务），因此能拿到精确的开始/结束时间与失败异常。

任务二：晨间窗口结束后的强币推荐邮件（仅一次）
    08 点档那轮分析收尾后（无论成功/失败/因手动批次在跑而跳过），从最新
    行情（read_csv_for_display，DB 优先）筛选：
      1. 4H_趋势 与 1D_趋势 同向且明确（原一致性规则）；
      2. ADX_4H > adx_min（默认 25）；
      3. ATR_4H > atr_min（默认 20，CSV 中该列即 ATR%，10倍杠杆口径）；
      4. 按 ADX_4H 降序取前 top_n（默认 10）。
    因跳过而等到的是手动批次在跑，这里会限时等其结束再取数（数据时间更
    新），超时则按现有最新数据发送（宁可数据旧一点，不断每日一份）。
    筛选为空也发「今日无符合条件的币种」，让收件人区分「没有机会」和
    「任务挂了」。分析失败另有独立告警邮件（失败≠无结果，两封各司其职）。

配置（kv_store key='batch_schedule_config'，改库后最迟 TTL 命中生效；
调度时刻在重启/重注册时生效）：
    {
      "batch_hours": "0,4,8,12,16,20",  # 空串 = 关闭任务一（连带不发邮件）
      "batch_minute": 5,
      "digest_enabled": true,           # 只关邮件不动分析
      "digest_hour": 8,                 # 在哪个钟点档结束后发
      "digest_wait_batch_sec": 3600,    # 发信前最多等多久的在跑批次
      "adx_min": 25, "atr_min": 20, "top_n": 10,
      "to_email": ""                    # 空 = email_config 管理员邮箱
    }
环境变量兜底（kv 读不到时生效）：CRYPTO_BATCH_HOURS / CRYPTO_BATCH_MINUTE。
更新配置可直接调本模块 update_batch_schedule_config({...})。

任务总开关走统一的 task_switches（页面 /task 或 config.json 急停，
job_id='batch_trend_auto'），与其他后台任务同一套治理。
"""

import datetime
import os
import time

# 双模式导入：Flask 包内（crypto.task.*）/ 独立脚本（task 目录在 sys.path）
try:
    from ..utils.logger import get_task_logger
except ImportError:  # pragma: no cover
    from utils.logger import get_task_logger

task_log = get_task_logger()

BATCH_JOB_ID = 'batch_trend_auto'
KV_CONFIG_KEY = 'batch_schedule_config'

DEFAULT_CONFIG = {
    'batch_hours': '0,4,8,12,16,20',
    'batch_minute': 5,
    'digest_enabled': True,
    'digest_hour': 8,
    'digest_wait_batch_sec': 3600,
    'adx_min': 25.0,
    'atr_min': 20.0,
    'top_n': 10,
    'to_email': '',
}


# =============================================================================
# 配置读取（kv 优先，环境变量兜底，默认值保底；任何坏值都退回默认不炸任务）
# =============================================================================

def _num(value, default, lo=None, hi=None):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default
    if hi is not None and n > hi:
        return default
    return n


def load_config() -> dict:
    """合并出本次运行的有效配置：defaults < env < kv_store。绝不抛异常。"""
    cfg = dict(DEFAULT_CONFIG)
    env_hours = str(os.environ.get('CRYPTO_BATCH_HOURS', '')).strip()
    if env_hours:
        cfg['batch_hours'] = env_hours
    env_minute = str(os.environ.get('CRYPTO_BATCH_MINUTE', '')).strip()
    if env_minute:
        cfg['batch_minute'] = _num(env_minute, cfg['batch_minute'], 0, 59)
    try:
        try:
            from .. import config_store_repo as cs
        except ImportError:  # pragma: no cover
            import config_store_repo as cs
        stored = cs.load_json_config_cached(KV_CONFIG_KEY)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
    except Exception as e:
        task_log.warning(f'[批量趋势调度] 读取 kv 配置失败，按环境/默认值执行: {e}')
    cfg['batch_minute'] = int(_num(cfg.get('batch_minute'), 5, 0, 59))
    cfg['digest_hour'] = int(_num(cfg.get('digest_hour'), 8, 0, 23))
    cfg['digest_enabled'] = bool(cfg.get('digest_enabled', True))
    cfg['adx_min'] = _num(cfg.get('adx_min'), 25.0, 0, 100)
    cfg['atr_min'] = _num(cfg.get('atr_min'), 20.0, 0, 1000)
    cfg['top_n'] = int(_num(cfg.get('top_n'), 10, 1, 100))
    cfg['digest_wait_batch_sec'] = _num(cfg.get('digest_wait_batch_sec'), 3600, 0, 4 * 3600)
    cfg['to_email'] = str(cfg.get('to_email') or '').strip()
    return cfg


def update_batch_schedule_config(patch: dict) -> dict:
    """合并写入 kv 配置（供管理脚本/接口调用）；返回保存后的整份配置。"""
    try:
        from ..database import session_scope
        from .. import config_store_repo as cs
    except ImportError:  # pragma: no cover
        from database import session_scope
        import config_store_repo as cs
    clean = {k: v for k, v in (patch or {}).items() if k in DEFAULT_CONFIG}
    with session_scope() as s:
        current = cs.load_json_config(s, KV_CONFIG_KEY) or {}
        current.update(clean)
        cs.save_json_config(s, KV_CONFIG_KEY, current)
    try:
        cs.invalidate_config_cache(KV_CONFIG_KEY)
    except Exception:
        pass
    return current


def parse_hours(text) -> list:
    """'0,4,8,12,16,20' → [0,4,8,12,16,20]；坏值退回默认档；
    显式空串 = 用户主动关闭（返回 []）；None = 未配置，按默认档。"""
    if text is None:
        return parse_hours(DEFAULT_CONFIG['batch_hours'])
    raw = str(text).strip()
    if raw == '':
        return []
    try:
        hours = sorted({int(t) for t in raw.replace('，', ',').split(',') if t.strip() != ''})
    except ValueError:
        task_log.warning(f'[批量趋势调度] batch_hours={raw!r} 非法，按默认 0,4,8,12,16,20 执行')
        return list(parse_hours(DEFAULT_CONFIG['batch_hours']))
    if any(h < 0 or h > 23 for h in hours):
        task_log.warning(f'[批量趋势调度] batch_hours={raw!r} 含越界小时，按默认执行')
        return list(parse_hours(DEFAULT_CONFIG['batch_hours']))
    return hours


def cron_kwargs(cfg=None) -> dict:
    """供 register_job 用的 cron 参数（hour 逗号串原生支持）"""
    cfg = cfg or load_config()
    hours = parse_hours(cfg.get('batch_hours'))
    return {'hour': ','.join(str(h) for h in hours), 'minute': int(cfg['batch_minute'])}


def digest_slot_now(now, cfg) -> bool:
    """本轮触发是否落在「该发晨间邮件」的钟点档（用任务触发时刻判断，
    不用完成时刻 —— 20:05 那轮哪怕跑到次日 00:30 也不算晨间档）。"""
    return bool(cfg.get('digest_enabled')) and now.hour == int(cfg['digest_hour'])


# =============================================================================
# 强币筛选（纯函数，离线冒烟覆盖）
# =============================================================================

def _to_float(value):
    """CSV 单元格 → float；空/--/非数字一律 None（该币被阈值条件排除）"""
    if value is None:
        return None
    text = str(value).strip().replace('%', '')
    if text in ('', '--', 'nan', 'None'):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def select_strong_coins(records, adx_min=25.0, atr_min=20.0, top_n=10) -> list:
    """双周期趋势一致 + ADX/ATR 阈值 + 按 ADX_4H 降序取前 N。

    空值/无效数据口径：趋势必须明确为 上涨/下跌；ADX_4H、ATR_4H 任一缺失
    或非法即不满足阈值，直接排除（宁缺毋滥，不给脏数据发推荐）。
    """
    picked = []
    for r in records or []:
        trend_4h = str(r.get('4H_趋势', '') or '').strip()
        trend_1d = str(r.get('1D_趋势', '') or '').strip()
        if trend_4h not in ('上涨', '下跌') or trend_4h != trend_1d:
            continue
        adx = _to_float(r.get('ADX_4H'))
        atr = _to_float(r.get('ATR_4H'))
        if adx is None or adx <= adx_min:
            continue
        if atr is None or atr <= atr_min:
            continue
        picked.append((adx, atr, r))
    # ADX 降序；同分时 ATR 降序，保证顺序稳定可复现
    picked.sort(key=lambda t: (-t[0], -t[1]))
    return [dict(r, _adx4h=adx, _atr4h=atr) for adx, atr, r in picked[:int(top_n)]]


def data_updated_at(records) -> str:
    """数据时间 = 全表最新的 4H_交易时间（K线口径）；取不到返回 '-'"""
    latest = ''
    for r in records or []:
        ts = str(r.get('4H_交易时间', '') or '').strip()
        if ts > latest:
            latest = ts
    return latest or '-'


# =============================================================================
# 邮件内容构造与发送
# =============================================================================

_DIGEST_CSS = """
    body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; margin: 0; padding: 0; background-color: #f6f6f6; }
    .container { max-width: 760px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); overflow: hidden; }
    .header { background-color: #1976d2; padding: 20px; text-align: center; color: white; }
    .header h2 { margin: 0; font-size: 22px; }
    .content { padding: 24px 20px; line-height: 1.6; color: #333; }
    table { border-collapse: collapse; width: 100%; font-size: 13px; }
    th, td { border: 1px solid #e0e0e0; padding: 7px 9px; text-align: center; }
    th { background-color: #e8f0fe; color: #1a73e8; }
    .up { color: #388e3c; font-weight: bold; }
    .down { color: #d32f2f; font-weight: bold; }
    .notes { margin-top: 18px; color: #666; font-size: 12px; }
    .footer { background-color: #f9f9f9; padding: 15px; text-align: center; font-size: 12px; color: #999; border-top: 1px solid #eee; }
"""


def _trend_cell(text):
    cls = 'up' if text == '上涨' else ('down' if text == '下跌' else '')
    return f'<span class="{cls}">{text or "--"}</span>' if cls else (text or '--')


def build_digest_email(coins, cfg, data_time, now=None):
    """返回 (subject, html, text)。coins 为 select_strong_coins 的输出。"""
    now = now or datetime.datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    subject = f'【每日趋势强币推荐】{date_str}'
    notes = (
        '筛选条件说明：<br>'
        '&nbsp;&nbsp;- 双周期趋势一致（4H与1D同向且明确）<br>'
        f'&nbsp;&nbsp;- ADX_4H &gt; {cfg["adx_min"]:g}（趋势强度较高）<br>'
        f'&nbsp;&nbsp;- ATR_4H% &gt; {cfg["atr_min"]:g}%（波动性充足）<br>'
        f'&nbsp;&nbsp;- 按 ADX_4H 降序取前 {int(cfg["top_n"])} 名<br>'
        f'&nbsp;&nbsp;- 数据更新时间：{data_time}'
    )
    notes_text = (
        '筛选条件说明：\n'
        '- 双周期趋势一致（4H与1D同向且明确）\n'
        f'- ADX_4H > {cfg["adx_min"]:g}（趋势强度较高）\n'
        f'- ATR_4H% > {cfg["atr_min"]:g}%（波动性充足）\n'
        f'- 按 ADX_4H 降序取前 {int(cfg["top_n"])} 名\n'
        f'- 数据更新时间：{data_time}'
    )
    if coins:
        rows_html, rows_text = [], []
        for i, r in enumerate(coins, 1):
            name = str(r.get('币种', '') or '')
            pair = str(r.get('交易对', '') or name)
            macd = _to_float(r.get('MACD_4H'))
            macd_s = f'{macd:.4f}' if macd is not None else '--'
            price = str(r.get('4H_交易价格', '') or '--').strip() or '--'
            t4 = str(r.get('4H_趋势', '') or '').strip()
            t1 = str(r.get('1D_趋势', '') or '').strip()
            rows_html.append(
                '<tr>'
                f'<td>{i}</td>'
                f'<td><strong>{pair}</strong></td>'
                f'<td>{name}</td>'
                f'<td>{_trend_cell(t4)}</td>'
                f'<td>{_trend_cell(t1)}</td>'
                f'<td>{r["_adx4h"]:.2f}</td>'
                f'<td>{r["_atr4h"]:.2f}%</td>'
                f'<td>{macd_s}</td>'
                f'<td>{price}</td>'
                '</tr>')
            rows_text.append(
                f'{i:>2}. {pair:<16} 4H:{t4:<3} 1D:{t1:<3} '
                f'ADX:{r["_adx4h"]:>6.2f}  ATR%:{r["_atr4h"]:>6.2f}  '
                f'MACD:{macd_s:>9}  价格:{price}')
        table_html = (
            '<p>以下是今日趋势最强的 ' + str(len(coins)) + ' 个币种（按ADX_4H降序）：</p>'
            '<table><tr><th>排名</th><th>交易对</th><th>币种</th><th>4H趋势</th><th>1D趋势</th>'
            '<th>ADX_4H</th><th>ATR_4H%</th><th>MACD_4H</th><th>当前价格</th></tr>'
            + ''.join(rows_html) + '</table>')
        table_text = f'以下是今日趋势最强的 {len(coins)} 个币种（按ADX_4H降序）：\n' + '\n'.join(rows_text)
    else:
        table_html = ('<p style="font-size:15px;color:#666;">'
                      '今日无符合条件的币种（市场可能处于震荡期，趋势信号不足）。</p>')
        table_text = '今日无符合条件的币种（市场可能处于震荡期，趋势信号不足）。'

    html = (f'<html><head><style>{_DIGEST_CSS}</style></head><body>'
            '<div class="container">'
            '<div class="header"><h2>📈 每日趋势强币推荐</h2></div>'
            f'<div class="content">{table_html}<div class="notes">{notes}</div></div>'
            '<div class="footer"><p>本邮件由系统自动发送，仅供参考，不构成投资建议</p>'
            f'<p>发送时间：{now.strftime("%Y-%m-%d %H:%M:%S")}</p></div>'
            '</div></body></html>')
    text = (f'{subject}\n\n{table_text}\n\n{notes_text}\n\n'
            '（本邮件由系统自动发送，仅供参考，不构成投资建议）')
    return subject, html, text


def _resolve_to_email(cfg):
    if cfg.get('to_email'):
        return cfg['to_email']
    try:
        from .config.email_config import get_admin_email
    except ImportError:  # pragma: no cover - 包外裸模块身份时的兼容分支
        from config.email_config import get_admin_email
    return get_admin_email()


def _mail_tool():
    try:
        from .notification.email_tool import EmailTool
    except ImportError:  # pragma: no cover
        from notification.email_tool import EmailTool
    return EmailTool()


def send_digest_mail(cfg=None, now=None) -> bool:
    """读最新行情 → 筛选 → 发晨间强币邮件（含空结果通知）。返回是否发出。"""
    cfg = cfg or load_config()
    now = now or datetime.datetime.now()
    try:
        try:
            from ..batch_trend_updater import read_csv_for_display
        except ImportError:  # pragma: no cover
            from batch_trend_updater import read_csv_for_display
        records = read_csv_for_display()
    except Exception as e:
        # 取数失败也不能静默：发一封说明信（与「无结果」区分开）
        task_log.error(f'[强币邮件] 读取行情数据失败: {e}')
        return _send_mail(cfg, '【每日趋势强币推荐】数据读取失败',
                          f'今日强币推荐未能生成：读取最新行情失败。\n错误: {e}',
                          f'数据读取失败，请查看日志。错误: {e}')
    coins = select_strong_coins(records, cfg['adx_min'], cfg['atr_min'], cfg['top_n'])
    data_time = data_updated_at(records)
    subject, html, text = build_digest_email(coins, cfg, data_time, now=now)
    ok = _send_mail(cfg, subject, html, text)
    task_log.info(f'[强币邮件] {"发送成功" if ok else "发送失败"}: '
                  f'{subject}（入选 {len(coins)} 个，数据时间 {data_time}）')
    return ok


def _send_mail(cfg, subject, html, text) -> bool:
    """统一发信出口；SMTP 异常只记日志返回 False，绝不让调度线程炸掉。"""
    try:
        ok = _mail_tool().send_html_email([_resolve_to_email(cfg)], subject, html,
                                          text_content=text)
        if not ok:
            task_log.error(f'[批量趋势调度] 邮件发送失败（SMTP 未受理）: {subject}')
        return bool(ok)
    except Exception as e:
        task_log.error(f'[批量趋势调度] 邮件发送异常: {subject} -> {e}')
        return False


def send_failure_alert(task_name, error_text):
    """任务失败告警邮件；发不出去只落日志（告警失败不该引发告警的告警）"""
    try:
        try:
            from .notification.email_tool import EmailTemplates
        except ImportError:  # pragma: no cover
            from notification.email_tool import EmailTemplates
        cfg = load_config()
        html = EmailTemplates.task_error(task_name, str(error_text)[:2000])
        ok = _send_mail(cfg, f'⚠️ {task_name} 执行失败', html,
                        f'{task_name} 执行失败: {error_text}')
        task_log.info(f'[批量趋势调度] 失败告警邮件{"已发出" if ok else "发出失败"}: {task_name}')
    except Exception as e:
        task_log.error(f'[批量趋势调度] 失败告警邮件异常: {e}')


# =============================================================================
# 等待在跑批次收尾（跳过场景下邮件仍尽量用新数据；限时，超时照发）
# =============================================================================

def wait_batch_idle(max_wait_sec, poll_sec=30, sleep_fn=time.sleep, status_fn=None) -> bool:
    """轮询批量分析状态直到非 running。True=已空闲，False=超时仍在跑。"""
    if status_fn is None:
        def status_fn():
            try:
                from ..batch_trend_updater import get_progress
            except ImportError:  # pragma: no cover
                from batch_trend_updater import get_progress
            return get_progress().get('status', 'idle')
    waited = 0.0
    while status_fn() == 'running':
        if waited >= max_wait_sec:
            return False
        sleep_fn(poll_sec)
        waited += poll_sec
    return True


# =============================================================================
# 任务体（cron 入口）
# =============================================================================

def run_batch_trend_job(now=None, cfg=None) -> str:
    """每 4 小时的批量趋势分析；晨间档收尾后链式发送强币邮件。

    返回本轮处置字符串（供冒烟断言/日志）：
      success / skipped_busy / failed / failed:<异常> ...
    """
    now = now or datetime.datetime.now()
    cfg = cfg or load_config()
    fired_at = now.strftime('%Y-%m-%d %H:%M:%S')
    slot_digest = digest_slot_now(now, cfg)
    task_log.info(f'[批量趋势调度] {fired_at} 自动批量分析开始'
                  + ('（晨间档，结束后将推送强币邮件）' if slot_digest else ''))

    skipped = False
    outcome = 'success'
    try:
        try:
            from ..batch_trend_updater import BatchTrendAnalyzer, BatchAlreadyRunning, get_progress
        except ImportError:  # pragma: no cover
            from batch_trend_updater import BatchTrendAnalyzer, BatchAlreadyRunning, get_progress
        try:
            BatchTrendAnalyzer().run()   # 与手动入口同一引擎、同一把任务锁
            p = get_progress()
            task_log.info(
                f'[批量趋势调度] 分析完成 @ {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
                f'：成功 {p.get("success")}/{p.get("coin_total")}，失败 {p.get("error")}，'
                f'耗时 {p.get("elapsed_seconds")}s')
        except BatchAlreadyRunning as exc:
            # 上一轮（手动或定时）未结束：跳过本轮，不排队重跑（需求口径）
            skipped = True
            outcome = 'skipped_busy'
            task_log.warning(f'[批量趋势调度] 跳过本轮：{exc}')
    except Exception as exc:
        outcome = 'failed'
        task_log.error(f'[批量趋势调度] 批量分析失败: {exc}', exc_info=True)
        send_failure_alert('批量多周期趋势分析（定时任务）', exc)

    if slot_digest:
        if skipped:
            # 跳过的原因是「有批次在跑」（多为手动）：限时等它跑完，邮件尽量用新数据
            idle = wait_batch_idle(cfg['digest_wait_batch_sec'])
            if not idle:
                task_log.warning('[强币邮件] 等待在跑批次超时，按当前最新数据发送')
        elif outcome == 'failed':
            # 本轮分析失败（数据可能旧一天）：照常发信，正文数据时间如实呈现，
            # 失败本身已有上面的告警邮件负责
            task_log.warning('[强币邮件] 本轮分析失败，仍按现有最新数据发送每日邮件')
        try:
            send_digest_mail(cfg=cfg)
        except Exception as e:
            task_log.error(f'[强币邮件] 发送流程异常: {e}', exc_info=True)

    return outcome


# =============================================================================
# 调度注册（scheduler.register_default_jobs 共用）
# =============================================================================

def register_batch_jobs():
    """注册批量趋势自动分析任务；返回 (job_id | None, 说明)。

    batch_hours 配成空串 = 整体关闭（分析 + 邮件同一任务体，一起停）。
    页面/文件级 task_switches 开关由 register_job 统一拦截，无需在此重复。
    """
    try:
        from .scheduler import task_scheduler
    except Exception as e:  # pragma: no cover
        task_log.warning(f'[批量趋势调度] 调度器不可用，任务未注册: {e}')
        return None, '调度器不可用'

    cfg = load_config()
    hours = parse_hours(cfg.get('batch_hours'))
    if not hours:
        # 显式空串（kv/env 给了 batch_hours=''）= 用户主动关闭
        task_scheduler.remove_job(BATCH_JOB_ID)
        task_log.info('[批量趋势调度] batch_hours 为空，任务已关闭')
        return None, '已关闭（batch_hours 为空）'

    minutes = int(cfg['batch_minute'])
    desc = '每日 ' + '、'.join(f'{h:02d}:{minutes:02d}' for h in hours)
    digest_note = (f'；{int(cfg["digest_hour"]):02d}点档结束后推送强币邮件'
                   if cfg['digest_enabled'] else '；强币邮件已关闭')
    job_id = task_scheduler.register_job(
        run_batch_trend_job, trigger='cron',
        job_id=BATCH_JOB_ID,
        job_name=f'批量趋势自动分析（{desc}{digest_note}）',
        **cron_kwargs(cfg))
    return job_id, f'{desc}{digest_note}'
