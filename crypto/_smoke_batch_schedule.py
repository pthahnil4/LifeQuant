#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量趋势定时分析 + 强币邮件（batch_schedule）离线冒烟
======================================================
安全等级：🔒 纯离线 —— 不连 DB（kv 读取打桩 load_config / 伪 scheduler 模块）、
不碰真实批量引擎（sys.modules 注入假 crypto.batch_trend_updater）、
不真发信（_mail_tool/_resolve_to_email 打桩捕获）。覆盖：
1. parse_hours 各口径：默认 / 显式空串关闭 / 非法值回退 / 中文逗号
2. 晨间档判定 digest_slot_now（按触发时刻，不看完成时刻）
3. 强币筛选：双周期一致 + 阈值 + ADX 降序 + top_n + 空值/边界排除
4. 数据时间与邮件构造：正常表格 / 空结果通知 / 取数失败说明信
5. wait_batch_idle 限时等待（注入状态序列与假 sleep）
6. 任务体四态：成功 / 并发跳过 / 失败告警 / 晨间档链式发信（仅一次）
7. 注册层：cron 参数、batch_hours 空串 → 移除任务

运行：python -m crypto._smoke_batch_schedule
"""

import datetime
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from crypto.task import batch_schedule as bs  # noqa: E402

_PASS = []
_FAIL = []


def ck(cond, label, detail=''):
    (_PASS if cond else _FAIL).append(label)
    print(('  ✅ ' if cond else '  ❌ ') + label + (f' | {detail}' if detail else ''))


# =====================================================================
# 假批量引擎模块（拦截 batch_schedule 内部的懒加载 import）
# =====================================================================

class FakeAlreadyRunning(RuntimeError):
    pass


_state = {}


def _fake_progress():
    seq = _state.get('statuses') or ['completed']
    st = seq.pop(0) if len(seq) > 1 else seq[0]
    _state['status_seen'] = st
    return {'status': st, 'success': 20, 'coin_total': 20,
            'error': 0, 'elapsed_seconds': 12.3}


class FakeAnalyzer:
    def __init__(self, *a, **k):
        pass

    def run(self):
        _state['run_calls'] = _state.get('run_calls', 0) + 1
        mode = _state.get('analyzer_mode', 'success')
        if mode == 'busy':
            raise FakeAlreadyRunning('已有批量分析任务正在运行中，请等待完成后再启动')
        if mode == 'boom':
            raise RuntimeError('模拟分析引擎炸了')
        return {'total': 20, 'success': 20, 'error': 0}


def _fake_read_csv():
    if _state.get('read_error'):
        raise RuntimeError('模拟取数失败')
    return [dict(r) for r in _state.get('records', [])]


_fake_mod = types.ModuleType('crypto.batch_trend_updater')
_fake_mod.BatchTrendAnalyzer = FakeAnalyzer
_fake_mod.BatchAlreadyRunning = FakeAlreadyRunning
_fake_mod.get_progress = _fake_progress
_fake_mod.read_csv_for_display = _fake_read_csv
sys.modules['crypto.batch_trend_updater'] = _fake_mod

# =====================================================================
# 邮件层打桩（捕获而非发送）
# =====================================================================

_mails = []


class FakeMailTool:
    def send_html_email(self, to, subject, html, text_content=None):
        _mails.append({'to': list(to), 'subject': subject,
                       'html': html, 'text': text_content or ''})
        return _state.get('mail_ok', True)


bs._mail_tool = lambda: FakeMailTool()
bs._resolve_to_email = lambda cfg: 'tester@example.com'
bs.load_config = lambda: dict(bs.DEFAULT_CONFIG)

# =====================================================================
# 测试数据
# =====================================================================

def _rec(sym, t4, t1d, adx, atr, macd='0.1234', price='60000', ts='2026-09-22 08:00:00'):
    return {'币种': sym, '交易对': sym + '-USDT-SWAP', '名称': '测试' + sym,
            '4H_趋势': t4, '1D_趋势': t1d, 'ADX_4H': adx, 'ATR_4H': atr,
            'MACD_4H': macd, '4H_交易价格': price, '4H_交易时间': ts}


BASE_RECS = (
    [_rec('BTC', '上涨', '上涨', '35.2', '28.5', ts='2026-09-22 08:00:00'),
     _rec('ETH', '下跌', '下跌', '30.1', '25.0', ts='2026-09-22 08:00:00'),
     _rec('SOL', '上涨', '上涨', '26.0', '21.0'),
     # 噪声：不一致 / 观望 / 阈值边界不满足 / 空值 / 非法值
     _rec('XRP', '上涨', '下跌', '40.0', '30.0'),
     _rec('DOGE', '观望', '观望', '40.0', '30.0'),
     _rec('ADA', '上涨', '上涨', '25', '28.0'),      # ADX == 下限，严格大于排除
     _rec('AVAX', '上涨', '上涨', '40.0', '20'),     # ATR == 下限，排除
     _rec('DOT', '上涨', '上涨', '', '30.0'),        # ADX 空值，排除
     _rec('LINK', '上涨', '上涨', '--', '30.0'),     # ADX 非法，排除
     _rec('LTC', '上涨', '上涨', '33.0', '22.0')]
    + [_rec(f'P{i:02d}', '上涨', '上涨', f'{28 + i * 0.1:.1f}', '23.0') for i in range(12)]
)


def main():
    now8 = datetime.datetime(2026, 9, 22, 8, 5, 0)

    print('== 1. parse_hours / 晨间档判定 ==')
    ck(bs.parse_hours(None) == [0, 4, 8, 12, 16, 20], '未配置按默认 6 档', str(bs.parse_hours(None)))
    ck(bs.parse_hours('') == [], '显式空串 = 关闭')
    ck(bs.parse_hours('2，6, 10') == [2, 6, 10], '中文逗号/空格容错且排序去重')
    ck(bs.parse_hours('99') == [0, 4, 8, 12, 16, 20], '越界小时回退默认')
    ck(bs.parse_hours('abc') == [0, 4, 8, 12, 16, 20], '非法串回退默认（不因坏值让任务缺席）')
    cfg8 = dict(bs.DEFAULT_CONFIG)
    ck(bs.digest_slot_now(now8, cfg8) is True, '08:05 触发落在晨间档')
    ck(bs.digest_slot_now(now8.replace(hour=20), cfg8) is False, '20:05 档不发邮件')
    ck(bs.digest_slot_now(now8, dict(cfg8, digest_enabled=False)) is False, 'digest_enabled=False 关闭')

    print('== 2. 强币筛选 ==')
    sel = bs.select_strong_coins(BASE_RECS, 25, 20, 10)
    ck(len(sel) == 10, 'top_n 截断为 10', f'{len(sel)}')
    adxs = [r['_adx4h'] for r in sel]
    ck(adxs == sorted(adxs, reverse=True), '按 ADX_4H 降序', str(adxs[:3]))
    ck(sel[0]['币种'] == 'BTC' and sel[0]['_adx4h'] == 35.2, '榜首为 BTC(35.2)')
    names = {r['币种'] for r in sel}
    ck(not names & {'XRP', 'DOGE', 'ADA', 'AVAX', 'DOT', 'LINK'},
       '不一致/观望/边界值/空值全部排除')
    ck(all(str(r['4H_趋势']) == str(r['1D_趋势']) for r in sel), '入选者双周期趋势一致')

    print('== 3. 数据时间与邮件构造 ==')
    ck(bs.data_updated_at(BASE_RECS) == '2026-09-22 08:00:00', '数据时间=最新4H交易时间')
    ck(bs.data_updated_at([]) == '-', '空表数据时间为 -')
    subj, html, text = bs.build_digest_email(sel, cfg8, '2026-09-22 08:00:00', now=now8)
    ck(subj == '【每日趋势强币推荐】2026-09-22', '主题含日期')
    ck('BTC' in html and 'ADX_4H' in html and '当前价格' in html and '<table' in html,
       'HTML 含表头与入选币种')
    ck(html.count('<tr>') == 11, '表头+10 行数据', str(html.count('<tr>')))
    ck('ADX_4H > 25' in text and '数据更新时间' in text, '纯文本备用含条件说明')
    _s2, h2, t2 = bs.build_digest_email([], cfg8, '-', now=now8)
    ck('今日无符合条件的币种' in h2 and '今日无符合条件的币种' in t2, '空结果发明确通知而非静默')

    print('== 4. wait_batch_idle ==')
    _state['statuses'] = ['running', 'running', 'completed']
    slept = []
    ok = bs.wait_batch_idle(3600, poll_sec=30, sleep_fn=slept.append,
                            status_fn=lambda: _state['statuses'].pop(0) if len(_state['statuses']) > 1 else _state['statuses'][0])
    ck(ok is True and len(slept) == 2, '限时等待：跑完即返回（等 2 轮）', str(len(slept)))
    _state['statuses'] = ['running']
    ok = bs.wait_batch_idle(0, poll_sec=30, sleep_fn=lambda s: None,
                            status_fn=lambda: 'running')
    ck(ok is False, '超时仍在跑 → False（不无限挂住调度线程）')

    print('== 5. 任务体：成功 / 跳过 / 失败 / 晨间链式 ==')
    _state.update(analyzer_mode='success', records=BASE_RECS, read_error=False)
    _mails.clear()
    r = bs.run_batch_trend_job(now=now8.replace(hour=12), cfg=cfg8)
    ck(r == 'success' and _state.get('run_calls') == 1 and not _mails,
       '午间档成功：跑一轮引擎、不发邮件')

    _state['run_calls'] = 0
    _mails.clear()
    r = bs.run_batch_trend_job(now=now8, cfg=cfg8)
    ck(r == 'success' and len(_mails) == 1, '晨间档成功：分析后链式发信仅一次', f'{len(_mails)} 封')
    ck(_mails and _mails[0]['subject'] == '【每日趋势强币推荐】2026-09-22',
       '邮件主题正确', _mails[0]['subject'] if _mails else '')
    ck(_mails and _mails[0]['to'] == ['tester@example.com'], '收件人解析（打桩值）')

    _state.update(analyzer_mode='busy', run_calls=0, statuses=['completed'])
    _mails.clear()
    r = bs.run_batch_trend_job(now=now8, cfg=cfg8)
    ck(r == 'skipped_busy', '上一轮未结束（手动触发在跑）→ 跳过本轮不重跑')
    ck(len(_mails) == 1 and '【每日趋势强币推荐】' in _mails[0]['subject'],
       '跳过场景：等在跑批次结束后仍发当日邮件')

    _state.update(analyzer_mode='boom', run_calls=0)
    _mails.clear()
    r = bs.run_batch_trend_job(now=now8.replace(hour=4), cfg=cfg8)
    ck(r == 'failed' and len(_mails) == 1 and '执行失败' in _mails[0]['subject'],
       '非晨间档失败：只发告警信不发日报', _mails[0]['subject'] if _mails else '')

    _mails.clear()
    r = bs.run_batch_trend_job(now=now8, cfg=cfg8)
    ck(r == 'failed' and len(_mails) == 2,
       '晨间档失败：告警信 + 日报都发（数据时间如实呈现）', f'{len(_mails)} 封')

    print('== 6. 取数失败兜底 & 注册层 ==')
    _mails.clear()
    _state.update(read_error=True)
    ok = bs.send_digest_mail(cfg=cfg8)
    ck(ok is True and _mails and '数据读取失败' in _mails[0]['subject'],
       '取数异常不静默：发说明信区分「没机会」与「任务挂了」')
    _state['read_error'] = False

    class FakeSched:
        def __init__(self):
            self.registered, self.removed = [], []

        def register_job(self, func, trigger=None, job_id=None, job_name=None, **kw):
            self.registered.append({'job_id': job_id, 'trigger': trigger, 'kw': kw, 'name': job_name})
            return job_id

        def remove_job(self, jid):
            self.removed.append(jid)

    fake_sched_mod = types.ModuleType('crypto.task.scheduler')
    fs = FakeSched()
    fake_sched_mod.task_scheduler = fs
    real_sched_mod = sys.modules.get('crypto.task.scheduler')
    sys.modules['crypto.task.scheduler'] = fake_sched_mod
    try:
        job_id, note = bs.register_batch_jobs()
        ck(job_id == bs.BATCH_JOB_ID and fs.registered, '注册成功返回 job_id')
        kw = fs.registered[0]['kw']
        ck(fs.registered[0]['trigger'] == 'cron'
           and kw.get('hour') == '0,4,8,12,16,20' and kw.get('minute') == 5,
           'cron 参数：每日 6 档 :05', str(kw))
        bs.load_config = lambda: dict(bs.DEFAULT_CONFIG, batch_hours='')
        job_id, note = bs.register_batch_jobs()
        ck(job_id is None and fs.removed == [bs.BATCH_JOB_ID],
           'batch_hours 空串 → 移除任务（分析+邮件一起关）', note)
    finally:
        if real_sched_mod is not None:
            sys.modules['crypto.task.scheduler'] = real_sched_mod
        else:
            sys.modules.pop('crypto.task.scheduler', None)

    print()
    total = len(_PASS) + len(_FAIL)
    if _FAIL:
        print(f'❌ {len(_FAIL)}/{total} 项失败: ' + '; '.join(_FAIL))
        return 1
    print(f'🎉 全部 {total} 项批量趋势调度冒烟通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
