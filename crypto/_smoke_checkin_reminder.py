#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打卡倒计时提醒冒烟：纯函数判定（到点/未到期/去重/防轰炸）→ 配置读写回环

只做读取与 kv 配置回环（测完还原原值），不发真实邮件、不写业务表。
kv 回环会在库内写 checkin_reminder_config/state 两个键，因此必须走隔离测试库
守卫（CRYPTO_TEST_DB_URL），绝不允许在业务库上跑。
运行：python -m crypto._smoke_checkin_reminder
"""

import datetime
import os
import sys

# Windows 终端默认 GBK，强制 UTF-8 输出避免中文/emoji 乱码报错
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crypto.test_isolation import (  # noqa: E402
    require_isolated_test_db, ensure_test_schema, TestDbNotConfigured)

try:
    _TEST_DB_URL = require_isolated_test_db()
except TestDbNotConfigured as e:
    print(f'❌ {e}')
    sys.exit(2)

from crypto.database import session_scope  # noqa: E402
from crypto import config_store_repo as cs  # noqa: E402
from crypto.task.monitor import checkin_reminder as cr  # noqa: E402

_created = ensure_test_schema()
print(f"[Smoke] 目标数据库（隔离测试库）: {_TEST_DB_URL.split('@')[-1]}"
      + (f'（补建 {len(_created)} 张缺失表）' if _created else ''))

FAILS = []
STATS = {'pass': 0}
NOW = datetime.datetime(2026, 9, 26, 11, 30, 0)


def chk(name, cond, extra=''):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f'  <- {extra}' if extra else ''))
    if cond:
        STATS['pass'] += 1
    else:
        FAILS.append(name)


def _mkdata(filled_at, status='in_progress', card_type='learn', slot_index=5):
    """构造一张带单个已打卡格子的计划树（字段与 summary 投影一致）"""
    return {'plans': [{
        'id': 'p1', 'type': 'learn', 'name': '测试计划',
        'daily_rule': {}, 'cards': [{
            'id': 'c1', 'type': card_type, 'status': status, 'round': 1,
            'title': 'R1 学习卡', 'slots': [
                {'slot_index': slot_index, 'filled': True, 'filled_at': filled_at,
                 'record': {'duration_minutes': 60}},
            ],
        }],
    }]}


CFG_ON = {'enabled': True, 'minutes': 60}


def check_pure():
    print('\n== 1. 纯函数判定 _evaluate ==')
    # 10:30 打卡，11:30 整 = 到点，应发
    r = cr._evaluate(_mkdata('2026-09-26 10:30:00'), CFG_ON, {}, NOW)
    chk('满 60 分钟到点可发', r.get('due') and not r.get('already'), r)
    chk('候选指纹含格子与时刻', r.get('key') == 'p1|c1|5|2026-09-26 10:30:00', r.get('key'))
    chk('邮件要素齐备（上次打卡/结束时刻）',
        r.get('filled_at') == '2026-09-26 10:30:00'
        and r.get('due_at') == '2026-09-26 11:30:00' and r.get('minutes') == 60, r)

    # 10:45 打卡，11:30 时倒计时未结束 → 不发
    r = cr._evaluate(_mkdata('2026-09-26 10:45:00'), CFG_ON, {}, NOW)
    chk('未到期不发信', r.get('due') is False and r.get('due_at') == '2026-09-26 11:45:00', r)

    # 同一格已提醒过，且没有新打卡 → 不重发（防轰炸）
    key = 'p1|c1|5|2026-09-26 10:30:00'
    r = cr._evaluate(_mkdata('2026-09-26 10:30:00'), CFG_ON,
                     {'last_reminded_key': key}, NOW)
    chk('同格已提醒不重发', r.get('due') and r.get('already'), r)

    # 提醒过后又打了新格（11:00）→ 新候选到 12:00 才发；11:30 时未到点
    newer = _mkdata('2026-09-26 11:00:00', slot_index=6)
    r = cr._evaluate(newer, CFG_ON, {'last_reminded_key': key}, NOW)
    chk('新打卡重开倒计时（未到期）', r.get('due') is False, r)

    # 12:05 时新格到期 → 与旧 key 不同，可再发
    r = cr._evaluate(newer, CFG_ON, {'last_reminded_key': key},
                     NOW + datetime.timedelta(minutes=35))
    chk('新格到期后可再提醒', r.get('due') and not r.get('already')
        and r.get('key').endswith('6|2026-09-26 11:00:00'), r)

    # 卡片状态门槛：非 in_progress（已结算/未解锁）不参与
    r = cr._evaluate(_mkdata('2026-09-26 10:30:00', status='completed'), CFG_ON, {}, NOW)
    chk('已结算卡不提醒', r.get('skip') == 'no_candidate', r)

    # 配置门槛
    chk('总开关关闭不发', cr._evaluate(_mkdata('2026-09-26 10:30:00'),
        {'enabled': False, 'minutes': 60}, {}, NOW).get('skip') == 'disabled')
    chk('minutes<=0 视为关闭', cr._evaluate(_mkdata('2026-09-26 10:30:00'),
        {'enabled': True, 'minutes': 0}, {}, NOW).get('skip') == 'countdown_off')

    # 时间边界：未来补录 / 超过 48h 的旧打卡都不作候选
    chk('未来时间打卡忽略', cr._evaluate(_mkdata('2026-09-26 23:00:00'),
        CFG_ON, {}, NOW).get('skip') == 'no_candidate')
    chk('超回溯窗旧打卡忽略', cr._evaluate(_mkdata('2026-09-20 10:30:00'),
        CFG_ON, {}, NOW).get('skip') == 'no_candidate')

    # 多格取最新时刻；HH:MM 无秒格式同样可解析
    d = _mkdata('2026-09-26 10:30:00')
    d['plans'][0]['cards'][0]['slots'].append(
        {'slot_index': 6, 'filled': True, 'filled_at': '2026-09-26 10:40', 'record': None})
    r = cr._evaluate(d, {'enabled': True, 'minutes': 5}, {}, NOW)
    chk('多格取最新打卡起倒计时', r.get('filled_at') == '2026-09-26 10:40:00', r)

    # 自定义时长：30 分钟窗，11:00 打卡到 11:30 正好到点
    r = cr._evaluate(_mkdata('2026-09-26 11:00:00'),
                     {'enabled': True, 'minutes': 30}, {}, NOW)
    chk('minutes 配置生效', r.get('due') and not r.get('already'), r)


def check_config_roundtrip():
    print('\n== 2. kv 配置/状态读写回环（测完还原） ==')
    with session_scope() as s:
        original = cs.load_json_config(s, cr.KEY_CHECKIN_CONFIG)
    try:
        with session_scope() as s:
            cs.save_json_config(s, cr.KEY_CHECKIN_CONFIG, {'enabled': True, 'minutes': 45})
        cfg = cr.load_config()
        chk('配置保存后读回 minutes=45', cfg.get('minutes') == 45 and cfg.get('enabled'), cfg)

        with session_scope() as s:
            cs.save_json_config(s, cr.KEY_CHECKIN_CONFIG, {'enabled': False, 'minutes': 45})
        chk('enabled=false 读回', cr.load_config().get('enabled') is False)

        # 状态回环（巡检去重用的 last_reminded_key）
        state = cr._load_state()
        chk('状态读为 dict', isinstance(state, dict))
        cr._save_state({'last_reminded_key': 'smoke|test', 'reminded_at': '2026-09-26 11:30:00'})
        chk('状态写后读回', cr._load_state().get('last_reminded_key') == 'smoke|test')
    finally:
        with session_scope() as s:
            if original is None:
                cs.delete_json_config(s, cr.KEY_CHECKIN_CONFIG)
            else:
                cs.save_json_config(s, cr.KEY_CHECKIN_CONFIG, original)
        cr._save_state({})
        cs.invalidate_config_cache(cr.KEY_CHECKIN_CONFIG)
        restored = cr.load_config()
        chk('配置已还原（enabled 回到原值）',
            restored.get('enabled', True) == (original or {}).get('enabled', True), restored)
        chk('状态键已清空', cr._load_state().get('last_reminded_key') is None)


def check_runner(monkey=True):
    print('\n== 3. 巡检入口 run_checkin_reminder_check（打桩不发真邮件） ==')
    sent = []
    orig_send = cr._send_reminder
    orig_email = cr._admin_email
    if monkey:
        cr._send_reminder = lambda result: (sent.append(result), True)[1]
        cr._admin_email = lambda: 'smoke@example.com'
    try:
        # 到点 → 发信并记状态
        class _FakeSessionCtx:
            def __enter__(self): return None
            def __exit__(self, *a): return False

        def _fake_scope():
            return _FakeSessionCtx()

        orig_scope, orig_cs_load, orig_cs_save = cr.session_scope, cs.load_json_config, cs.save_json_config
        fake_tree = _mkdata((NOW - datetime.timedelta(minutes=90)).strftime('%Y-%m-%d %H:%M:%S'))
        state_box = {}

        import crypto.plan_routes as plan_routes
        orig_summary = plan_routes._load_summary_plans
        try:
            cr.session_scope = _fake_scope
            cs.load_json_config = lambda s, k: dict(state_box.get(k) or {})
            def _save(s, k, v):
                state_box[k] = dict(v)
            cs.save_json_config = _save
            plan_routes._load_summary_plans = lambda s: fake_tree

            cr.run_checkin_reminder_check()
            chk('到点触发一次发送', len(sent) == 1, sent)
            chk('状态已记 last_reminded_key',
                str(state_box.get(cr.KEY_CHECKIN_STATE, {}).get('last_reminded_key', '')).startswith('p1|c1|'),
                state_box)

            # 同格再次巡检 → 不重发
            cr.run_checkin_reminder_check()
            chk('重复巡检不重发（防轰炸）', len(sent) == 1, sent)

            # 出现新打卡（用真实墙钟，与巡检入口同源）→ 倒计时重开、未到期不发
            state_box[cr.KEY_CHECKIN_STATE] = {}
            fake_tree['plans'][0]['cards'][0]['slots'].append(
                {'slot_index': 6, 'filled': True,
                 'filled_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 'record': None})
            cr.run_checkin_reminder_check()
            chk('新打卡未到期不发', len(sent) == 1, [s.get('key') for s in sent])
        finally:
            cr.session_scope = orig_scope
            cs.load_json_config = orig_cs_load
            cs.save_json_config = orig_cs_save
            plan_routes._load_summary_plans = orig_summary
    finally:
        cr._send_reminder = orig_send
        cr._admin_email = orig_email


if __name__ == '__main__':
    check_pure()
    check_config_roundtrip()
    check_runner()
    total = len(FAILS)
    print('\n' + (f'❌ 失败: {"；".join(FAILS)}' if FAILS else '✅ 全部通过'))
    print(f'通过 {STATS["pass"]} / 失败 {total}')
    sys.exit(1 if FAILS else 0)
