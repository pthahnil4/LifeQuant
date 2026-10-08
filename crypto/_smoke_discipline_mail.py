#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析纪律邮件链路冒烟：缺口信 / 断档汇总 / 日报 / 策略行情表

【本脚本不写台账，也发不出信】
- 缺口与断档全部用假行（types.SimpleNamespace）直接喂渲染函数，分组用
  `_group_contiguous` 纯函数、追责判定用 `_still_accountable` 纯判定，都不碰库。
  曾经的做法是往 analysis_reminder_log 里造行再删掉：造行会与生产巡检互相
  干扰（实测一次冒烟留下 17 行假判定、并让生产调度器据此真发了两封断档信），
  删行又会把真实缺口一起删掉，下一轮以 notified=False 重建 → 招来第三封信。
- 发信出口 `_dispatch_email` 被换成落盘，产出 data/mail_preview/*.html；
  脚本里**没有真发分支**，也不接受任何"顺便发一下"的参数。
- 开跑前取一条带过期时间的巡检租约（只写 kv_store 状态、退出即撤），
  否则“台账零改动”这条断言会被生产巡检自己的正常写入干扰，变成假失败。
- 唯一的例外是 `--real-market`：它真的调一次策略引擎取行情（只读行情，
  不下单、不写库、不发信），用来验证邮件里的行情表与实盘同源。

运行：
    python -m crypto._smoke_discipline_mail              # 全本地渲染
    python -m crypto._smoke_discipline_mail --real-market # 另加一次真实取数
"""

import datetime
import json
import pathlib
import sys
import types

import os

os.environ.setdefault('CRYPTO_NO_BACKGROUND', '1')

from sqlalchemy import func, select

from .database import session_scope
from .models import AnalysisReminderLog
from . import discipline_repo as disc
from .task.monitor import analysis_discipline as ad
from .task.notification.message_notifier import MessageNotifier

FAILS = []
OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / 'data' / 'mail_preview'

TODAY = datetime.datetime.now().strftime('%Y-%m-%d')

# 假缺口槽全部落在生效时段内（08:00-24:00），否则渲染出来的信本身就是
# 在被追责口径过滤掉的时间上造假。凌晨那格是故意放的：用来验证它不会被
# 当成缺档（用户的原话：「那个时候我在睡觉」）。
SLEEP_SLOT = f'{TODAY} 03'
LONE_SLOT = f'{TODAY} 15'
RUN_SLOTS = [f'{TODAY} 08', f'{TODAY} 09', f'{TODAY} 10']
OK_SLOTS = [f'{TODAY} 11', f'{TODAY} 12']

# 台账基准（零写库证据）：跑完必须一行不多一行不少
LEDGER_BASE = {}
LEDGER_TOTAL = 0


def ck(cond, label, extra=''):
    tag = 'OK  ' if cond else 'FAIL'
    if not cond:
        FAILS.append(label)
    print(f'  [{tag}] {label}' + (f'  {extra}' if extra else ''))


def _title(t):
    print('\n' + '=' * 72)
    print(t)
    print('=' * 72)


def gap_row(slot, required=1, actual=0, missing='POL-USDT-SWAP、NEAR-USDT-SWAP'):
    """造一条与 AnalysisReminderLog 同字段的假台账行（不进数据库）"""
    return types.SimpleNamespace(
        hour_slot=slot, stat_date=slot[:10], required_count=required,
        actual_count=actual, missing_insts=missing, status=disc.ST_MISSING,
        notified=False, notified_at='', resolved_at='')


def sat_row(slot, status=disc.ST_SATISFIED, actual=1, required=1, missing=''):
    return types.SimpleNamespace(
        hour_slot=slot, stat_date=slot[:10], required_count=required,
        actual_count=actual, missing_insts=missing, status=status,
        notified=False, notified_at='', resolved_at='')


def install_capture():
    """把发信出口换成落盘。本文件里唯一的出口实现——不存在真发分支。"""
    captured = []

    def _cap(self, to_emails=None, subject='', html_content='', log_label='', **kw):
        captured.append({'label': log_label, 'subject': subject,
                         'to': list(to_emails or []), 'html': html_content})
        print(f'  [CAPTURED→本地] {log_label} | {subject[:52]}')
        return True

    MessageNotifier._dispatch_email = _cap
    return captured


def audit(label, item):
    """排版静态核对：结构完整 + 数据非空 + 无渲染残留"""
    html = item['html']
    ck('<div class="container">' in html and '<div class="footer">' in html,
       f'{label}：外层结构完整（container/footer）')
    ck('class="header"' in html and '<h2>' in html, f'{label}：标题头存在')
    ck('None' not in html, f'{label}：正文没有裸 None（字段全对上）')
    ck('nan' not in html.lower().replace('nanjing', ''), f'{label}：没有 nan')
    ck('{' not in html.split('<style>')[0], f'{label}：正文没有未替换的占位符')
    ck(f'{TODAY}' in html, f'{label}：带当日日期')
    links = [l for l in html.split('href="')[1:]]
    ck(bool(links) and all(l.startswith('http') for l in links),
       f'{label}：行动链接是绝对 URL（{len(links)} 个）',
       links[0].split('"')[0] if links else '无')
    # 站点是本机/内网地址时，必须同时给出可达性提示，别让人白点一次
    if ad._link_is_local():
        ck('同一网络' in html, f'{label}：内网地址已标注可达性提示')
    n = len(html)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fn = OUT_DIR / f'{label}.html'
    fn.write_text(html, encoding='utf-8')
    print(f'       {label}: {item["subject"]}')
    print(f'       落盘 → {fn.as_posix()}（{n} 字节，表格 {html.count("<table")} 个）')
    return n


def ledger_snapshot():
    global LEDGER_TOTAL
    with session_scope() as s:
        rows = s.execute(select(AnalysisReminderLog)).scalars().all()
        for r in rows:
            LEDGER_BASE[r.hour_slot] = (r.status, r.required_count, r.actual_count,
                                        r.notified)
        LEDGER_TOTAL = len(rows)
    print(f'  台账基准：{LEDGER_TOTAL} 行 / {len(LEDGER_BASE)} 个槽位'
          f'（本脚本一行都不该改）')


def ledger_untouched():
    with session_scope() as s:
        rows = s.execute(select(AnalysisReminderLog)).scalars().all()
    now = {r.hour_slot: (r.status, r.required_count, r.actual_count, r.notified)
           for r in rows}
    added = sorted(set(now) - set(LEDGER_BASE))
    removed = sorted(set(LEDGER_BASE) - set(now))
    changed = sorted(k for k in set(now) & set(LEDGER_BASE) if now[k] != LEDGER_BASE[k])
    ck(not added and not removed and not changed,
       '全程零写库：台账行数与内容一字未动',
       f'+{len(added)} -{len(removed)} ~{len(changed)}'
       + (f' 新增{added[:3]}' if added else '')
       + (f' 丢失{removed[:3]}' if removed else '')
       + (f' 改动{changed[:3]}' if changed else ''))
    if added or removed or changed:
        # 本脚本一行都不写库（只渲染），所以任何差集都是外人写的；最常见的
        # 外人就是生产巡检自己。租约能被读到与否取决于在线进程跑的是哪版
        # 代码，所以这里把归因直接指出来，不让人误以为验证发现了假数据。
        print('       ↑ 本脚本没有任何写库路径，差集只可能来自外部写入。')
        print('         若新行都是整点槽（如 "2026-09-08 17"），那是生产巡检写的真判定；')
        print('         它没理维护租约 = 在线进程还是租约之前的旧代码，重启后才会让路。')
    # 顺带按「行数」再对一次：上面是按 hour_slot 聚合比较，同槽重复行
    # （数据库里真允许的话）会被聚合成一条而漏网
    with session_scope() as s:
        total = s.execute(select(func.count()).select_from(AnalysisReminderLog)).scalar()
    ck(int(total or 0) == LEDGER_TOTAL, '台账总行数与基准一致',
       f'total={total} 基准={LEDGER_TOTAL}')


def acquire_lease() -> bool:
    """取一条巡检维护租约，返回是否真的取到（决定收尾要不要撤）。

    为何要取：生产调度器每 5 分钟跑一轮巡检，正常就会往 analysis_reminder_log
    写判定行。本脚本全程只读，但若不先把巡检暂停掉，收尾的「台账零改动」
    会被它的**真实写入**判成假失败——报错报到我头上，实际是它干的。

    租约写在 kv_store 状态里并带过期时间，所以即使本脚本被强杀、没走到
    finally，生产巡检也只会静默十几分钟，不会永远不提醒。
    """
    try:
        until = disc.begin_maintenance(reason='邮件冒烟 _smoke_discipline_mail')
    except Exception as e:
        print(f'  [WARN] 维护租约拿不到（{type(e).__name__}: {e}），'
              f'生产巡检可能照常写台账，下面「零写库」那条仅供参考')
        return False
    print(f'  巡检维护租约：至 {until}（退出即撤销，被强杀则到期自动失效）')
    return True


def release_lease(held: bool):
    if not held:
        return
    try:
        if disc.end_maintenance():
            print('  已撤销维护租约（生产巡检恢复判定）')
        ck(disc.maintenance_until() is None, '维护租约已清除')
    except Exception as e:
        print(f'  [WARN] 维护租约撤销失败，到期后自动失效: {e}')


# =============================================================================
# [1] 判定口径：睡觉时间不追责（用户投诉的直接回归）
# =============================================================================

def check_accountability(cfg):
    _title('[1] 追责口径（凌晨不该算缺档）')
    win = disc.active_window(cfg)
    ck(win == (8 * 60, 24 * 60), '生效时段解析为 08:00~23:59 共 16 格', str(win))
    ck(len(disc.day_slots(TODAY, cfg)) == 16, '当日生效槽数 = 16',
       str(disc.day_slots(TODAY, cfg))[:60] + ' …')
    ck(not disc.is_slot_active(SLEEP_SLOT, cfg), '03:00 不在生效时段（睡觉时间）')
    for s in [LONE_SLOT, *RUN_SLOTS, *OK_SLOTS]:
        ck(disc.is_slot_active(s, cfg), f'{s[-2:]}:00 在生效时段内')

    epoch = disc.epoch_dt()
    ck(ad._still_accountable(LONE_SLOT, cfg, epoch) is True, '时段内 + 晚于起点 → 追责')
    ck(ad._still_accountable(SLEEP_SLOT, cfg, epoch) is False, '凌晨格 → 不追责')
    past = '2000-01-01 09'
    ck(ad._still_accountable(past, cfg, epoch) is False, '生效起点之前的格 → 不追责')


# =============================================================================
# [2] 分组与合并策略（纯函数，不依赖数据库）
# =============================================================================

def check_grouping(cfg):
    _title('[2] 连续性分组（首缺单发、连续缺口合并一封）')
    rows = [gap_row(s) for s in [*RUN_SLOTS, LONE_SLOT, SLEEP_SLOT]]
    groups = ad._group_contiguous(rows)
    # 分组只看时间连续性（凌晨格排最前），把睡觉时间挡在提醒之外的是
    # _still_accountable，不是分组——两者分开测才能看出谁失效了
    sizes = [len(g) for g in groups]
    ck(sizes == [1, 3, 1], '5 个缺口按时间分成 03 / 08~10 / 15 三组', str(sizes))
    ck([g[0].hour_slot for g in groups] == [SLEEP_SLOT, RUN_SLOTS[0], LONE_SLOT],
       '分组按时间升序，跨组不粘连')

    merge_after = max(1, int((cfg.get('email', {}) or {}).get('merge_after', 2) or 2))
    mails = [('digest' if len(g) >= merge_after else 'single') for g in groups]
    ck(mails == ['single', 'digest', 'single'],
       f'merge_after={merge_after} → 一封汇总 + 两封单发（不是一槽一封）', str(mails))

    # 假想事故复现：时段被撑成全天时，凌晨那格会混进追责集
    bad_cfg = dict(cfg)
    bad_cfg['active_hours'] = '00:00-23:59'
    kept = [r for r in rows if ad._still_accountable(r.hour_slot, bad_cfg,
                                                     disc.epoch_dt())]
    ck(any(r.hour_slot == SLEEP_SLOT for r in kept) is True,
       '（对照）把时段写成全天，凌晨就会被追责', '这正是投诉的成因')
    kept2 = [r for r in rows if ad._still_accountable(r.hour_slot, cfg,
                                                      disc.epoch_dt())]
    ck(all(r.hour_slot != SLEEP_SLOT for r in kept2),
       '现行口径下凌晨格被排除在提醒之外')

    # 源头清账不只看渲染：台账里也不该再留时段外的缺档行（只读校验）
    with session_scope() as s:
        miss = s.execute(select(AnalysisReminderLog.hour_slot).where(
            AnalysisReminderLog.status == disc.ST_MISSING)).scalars().all()
    bad = [h for h in miss if not disc.is_slot_active(h, cfg)]
    ck(not bad, '台账里没有时段外的缺档行（巡检已在源头清账）',
       f'共 {len(miss)} 行 missing' + (f' 越界: {bad[:5]}' if bad else ''))


# =============================================================================
# [3] 三类邮件渲染（假行直接喂）
# =============================================================================

def check_gap_mails(cfg, captured):
    _title('[3] 缺口信 + 断档汇总')
    strict = str(cfg.get('strict_mode') or 'strict')
    market = ad._market_table_html(lambda: _stub_market())
    notifier = MessageNotifier()
    admin = 'smoke-only@invalid'

    before = len(captured)
    ok = ad._send_single_gap(notifier, admin, gap_row(LONE_SLOT), strict, market)
    ck(ok is True, '单发缺口信渲染并投递到出口')
    ck(len(captured) == before + 1, '出口只被调用一次')
    single = captured[-1]
    ck(single['label'] == '分析缺口', 'log_label 正确', single['label'])
    ck(LONE_SLOT in single['subject'], '主题点明是哪一小时')
    n1 = audit('1_缺口信', single)
    ck('当前策略计算行情' in single['html'], '缺口信附了行情表')
    ck(single['to'] == [admin], '收件人取自参数（本脚本不会真发）')

    before = len(captured)
    ok = ad._send_gap_digest(notifier, admin, [gap_row(s) for s in RUN_SLOTS],
                             strict, market)
    ck(ok is True, '断档汇总渲染并投递')
    ck(len(captured) == before + 1, '三格缺口只出一封信')
    digest = captured[-1]
    ck(digest['label'] == '分析断档汇总', 'log_label 正确', digest['label'])
    # 明细行数得单独量：正文里还拼了一张行情表，整封 count('<tr')
    # 会把两张表混在一起，量出来既不像 4 也不像 7，容易误判成“信错了”
    detail = digest['html'].split('当前策略计算行情')[0]
    ck(detail.count('<tr') == len(RUN_SLOTS) + 1,
       '汇总明细表 = 3 行 + 1 行表头', f"tr={detail.count('<tr')}")
    ck('合并为一封提醒' in digest['html'], '汇总说明为什么合并（防轰炸）')
    n2 = audit('2_断档汇总', digest)
    ck('当前策略计算行情' in digest['html'], '汇总信也附了行情表')
    ck(n1 > 800 and n2 > 1200, '两封正文都不是空壳', f'{n1} / {n2} 字节')


def check_daily(cfg, captured):
    _title('[4] 纪律日报（真实取数路径，只读）')
    with session_scope() as s:
        board = disc.build_board(s, days=1, cfg=cfg)
        digest = disc.gap_digest(s, days=7, cfg=cfg)
    s_today = board.get('summary', {}) or {}
    judged = int(s_today.get('ok', 0) or 0) + int(s_today.get('missing', 0) or 0)
    ck(judged > 0, '本次有已判定槽（护栏不会误拦）',
       json.dumps(s_today, ensure_ascii=False))
    before = len(captured)
    ok = ad._send_daily_report(TODAY, board, digest or {})
    ck(ok is True, '日报渲染并投递')
    ck(len(captured) == before + 1, '日报走的是同一个出口')
    item = captured[-1]
    audit('3_纪律日报', item)
    for kw in ('合规率', '连续全合规', '事后补记率', '有分析支撑', '无分析支撑', '差值'):
        ck(kw in item['html'], f'日报含指标「{kw}」')


# =============================================================================
# [5] 措辞与数据诚实
# =============================================================================

def check_wording(captured):
    _title('[5] 措辞与空样本语义（数据诚实）')
    # 三种闸门模式必须各说各话：写死「已被拦住」在 soft/off 下就是谎报
    for mode, expect, forbid in (
        ('strict', '已被闸门拦住', '不会被拦'),
        ('soft', '不会被拦', '已被闸门拦住'),
        ('off', '只做提醒', '已被闸门拦住'),
    ):
        phrase = ad._gate_phrase(mode)
        ck(expect in phrase, f'{mode} 模式缺口信措辞准确', phrase[:38])
        ck(forbid not in phrase, f'{mode} 模式不含越界表述', forbid)
        tail = ad._digest_tail(mode)
        ck(bool(tail) and ('等着你的分析' in tail) == (mode == 'strict'),
           f'{mode} 模式汇总结尾措辞随模式', tail[:24])

    # 无样本时不能给「0.0%（0/0 条）」：读起来像很自律，实际是没数据
    empty = {
        'summary': {'rate': 0, 'ok': 0, 'missing': 0, 'exempt': 0, 'satisfied_later': 0},
        'backfill': {'rate': 0, 'backfill': 0, 'total': 0},
        'attribution': {'rate_diff': None, 'with_analysis': {}, 'without_analysis': {}},
        'streak': {'current': 0, 'best': 0},
    }
    before = len(captured)
    ck(ad._send_daily_report(TODAY, empty, {}) is True, '零样本日报仍可渲染')
    html = captured[before]['html']
    ck('暂无样本' in html, '补记率无样本时显示「暂无样本」')
    ck('0.0%（0/0 条）' not in html, '不再把空白渲染成 0.0%')
    ck('样本不足' in html, '归因无样本时显示「样本不足」（与上面口径一致）')
    ck(TODAY in html, '零样本下仍标注日期')
    audit('4_零样本日报', captured[before])


# =============================================================================
# [6] 策略行情表
# =============================================================================

def _stub_market():
    """假行情数据：形状与 market_snapshot 的返回一致，用于测渲染与降级。

    默认不真调引擎：冒烟不该为了排版把策略通道占住几十秒（那是实盘在用的
    通道）。要看真数据用 --real-market。
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    items = [
        {'instId': 'POL-USDT-SWAP', 'ts': ts, 'price': 0.09639,
         'short_period': '15m', 'long_period': '4H', 'short_dir': 'long',
         'long_dir': 'short', 'long_dir_prev': 'short', 'atr_pct': 0.38,
         'boll_upper': 0.096712, 'boll_middle': 0.095825, 'boll_lower': 0.094937},
        {'instId': 'NEAR-USDT-SWAP', 'ts': ts, 'price': 2.334,
         'short_period': '15m', 'long_period': '4H', 'short_dir': 'long',
         'long_dir': 'long', 'long_dir_prev': 'short', 'atr_pct': 0.81,
         'boll_upper': 2.3443, 'boll_middle': 2.30065, 'boll_lower': 2.256996},
    ]
    return items, ['ARB-USDT-SWAP'], ''


def check_market_table(captured, real=False):
    _title('[6] 策略行情表（邮件里直接给行情）')
    items, failed, note = _stub_market()
    html = ad._market_table_html(lambda: (items, failed, note))
    for col in ('币种', '现价', '短周期', '长周期', '长周期上时段', 'ATR%', 'BOLL 上/中/下'):
        ck(col in html, f'表头含「{col}」')
    ck(html.count('<tr') == len(items) + 1, '行数 = 币种数 + 表头',
       f"tr={html.count('<tr')}")
    ck('0.096390' in html, '小价币保留 6 位小数（不能被抹成 0.0000）')
    ck('2.3340' in html, '个位价格 4 位小数')
    ck('上涨' in html and '下跌' in html, '方向用中文而不是 long/short')
    ck('ARB-USDT-SWAP' in html and '另有 1 个币种本轮未取到' in html,
       '部分失败如实列出币名')
    ck('本轮没取到' not in html, '有数据时不出现降级文案')
    ck('不是建议' in html, '标明只是参考')

    deg = ad._market_table_html(lambda: ([], ['X-USDT-SWAP'], '策略计算通道被占用'))
    ck('策略行情本轮没取到' in deg, '全失败 → 一句实话而不是空表')
    ck('<table' not in deg, '没数据就不铺表')
    ck('通道被占用' in deg, '把原因一起给用户')
    ck('照常记录' in deg, '说明不影响用户自己补记录')

    err = ad._market_table_html(lambda: ([], [], '行情取数异常：连接被重置'))
    ck('连接被重置' in err, '异常原因也如实转达（不吞栈）')

    if real:
        print('\n  --real-market：真的走一遍引擎取数（只读行情，不发信不下单）')
        try:
            from .task.strategy_adapter import market_snapshot
        except ImportError:
            sys.path.insert(0, os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'task'))
            from strategy_adapter import market_snapshot
        curs = disc.tracked_currencies()
        r_items, r_failed, r_note = market_snapshot(curs)
        ck(bool(curs), '交易配置能读到币种', f'{len(curs)} 个')
        ck(len(r_items) > 0, '至少取到一个币的实盘行情',
           f'{len(r_items)}/{len(curs)} failed={r_failed} note={r_note!r}')
        ck(all(str(i.get('instId')) and i.get('ts') for i in r_items),
           '真实每行都有币种与取数时间')
        r_html = ad._market_table_html(lambda: (r_items, r_failed, r_note))
        ck(r_html.count('<tr') == len(r_items) + 1, '真实数据行数一致')
        ck('None' not in r_html and 'nan' not in r_html.lower(),
           '真实数据没有裸 None / nan')
        ad_admin = ad._admin_email()
        ok = ad._send_gap_digest(MessageNotifier(), 'smoke-only@invalid',
                                 [gap_row(s) for s in RUN_SLOTS],
                                 'strict', r_html)
        ck(ok is True, '带真实行情的汇总信渲染成功')
        audit('5_断档汇总·真实行情', captured[-1])
        if ad_admin:
            print('  注：真实收件人已配置，但本轮仍然只落盘，没有发信')
    else:
        print('  （要看真实行情加 --real-market，本脚本任何模式都不会发信）')


def main():
    argv = sys.argv[1:]
    real = '--real-market' in argv

    _title('[0] 准备：接管发信出口 + 台账基准快照')
    cfg = disc.load_config()
    ck(str(cfg.get('active_hours')) == disc.DEFAULT_ACTIVE_HOURS,
       '生效时段配置为默认 08:00-24:00', str(cfg.get('active_hours')))
    ck(int(cfg.get('required_count', 0) or 0) >= 1, '每小时要求条数合法',
       str(cfg.get('required_count')))
    captured = install_capture()
    print('  模式：只渲染落盘（脚本内不存在真发实现）')
    held = acquire_lease()
    ledger_snapshot()

    try:
        check_accountability(cfg)
        check_grouping(cfg)
        check_gap_mails(cfg, captured)
        check_daily(cfg, captured)
        check_wording(captured)
        check_market_table(captured, real=real)
        _title('[7] 渲染结果')
        print(f'  共渲染 {len(captured)} 封 → {OUT_DIR.as_posix()}/')
        print('  用浏览器打开即可核对排版')
    finally:
        ledger_untouched()
        release_lease(held)

    _title('邮件链路冒烟结果')
    if FAILS:
        print(f'  失败 {len(FAILS)} 项：')
        for f in FAILS:
            print('   -', f)
        raise SystemExit(1)
    print('  全部通过 ✓')


if __name__ == '__main__':
    main()
