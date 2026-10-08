#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时任务统一开关（kv task_switches + config.json 急停）离线冒烟
======================================================
安全等级：🔒 纯离线 —— 不连 DB（kv 开关层打桩 load_switch_state，不碰
config_store/database）、不碰真 config.json（开关文件路径打桩到临时目录）、
不真发信。覆盖：
1. task_switch 文件层语义：缺省=开 / 模块级 false / 全局 enabled=false / 非法任务名
2. fail-open：文件缺失 / JSON 写坏 / 无 task_switches 块 → 一律按开处理
3. mtime 缓存：改文件免重启即可读到新值
4. kv 持久开关层：全局/模块/缺省三态，及"文件只能单向关"的合并口径
5. 分析纪律：email.enabled=false → 发信判定关；任务开关关 → register 不注册（返回 0）
6. 每日归零：开关关 → register_daily_reset_job 不注册（环境变量开关行为不变）
7. 模块总闸 module_enabled：业务 kv enabled × 任务开关任一层关→静默
   （打卡闸门/网页倒计时随 /task 页开关一并关闭）

运行：python -m crypto._smoke_task_switch
"""

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from crypto.task import scheduler as sched  # noqa: E402

_PASS = []
_FAIL = []


def ck(cond, label, detail=''):
    (_PASS if cond else _FAIL).append(label)
    print(('  ✅ ' if cond else '  ❌ ') + label + (f' | {detail}' if detail else ''))


def _write_switches(tmpdir, payload):
    """把开关文件写到临时目录并让 task_switch 读它（None=删掉文件）"""
    path = os.path.join(tmpdir, 'config.json')
    if payload is None:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    sched._TASK_SWITCHES_FILE = path
    sched._task_switch_cache['sig'] = None
    sched._task_switch_cache['data'] = None


def main():
    print('== 0. kv 开关层打桩（不碰数据库）==')
    # 离线把持久开关层扁成空 dict（= 全开），各用例再逐个替换
    _real_store_loader = sched.load_switch_state
    _store_state = {'data': {}}
    sched.load_switch_state = lambda: dict(_store_state['data'])

    print('== 1. task_switch 文件层基本语义 ==')
    with tempfile.TemporaryDirectory() as tmp:
        sched._TASK_SWITCHES_FILE_BACKUP = sched._TASK_SWITCHES_FILE
        try:
            _write_switches(tmp, {})
            ck(sched.task_switch('analysis_discipline') is True, '无 task_switches 块 → 开')
            _write_switches(tmp, {'task_switches': {'analysis_discipline': False}})
            ck(sched.task_switch('analysis_discipline') is False, '模块级 false → 关')
            ck(sched.task_switch('daily_memory_reset') is True, '未配置的模块不受影响 → 开')
            _write_switches(tmp, {'task_switches': {'enabled': False}})
            ck(sched.task_switch('analysis_discipline') is False, '全局 enabled=false → 一切关')
            ck(sched.task_switch('whatever') is False, '全局关时任意任务都关')
            _write_switches(tmp, {'task_switches': {'enabled': True,
                                                    'daily_memory_reset': False}})
            ck(sched.task_switch('daily_memory_reset') is False, '全局开 + 模块关 → 关')
            ck(sched.task_switch('analysis_discipline') is True, '全局开 + 模块缺省 → 开')
            _write_switches(tmp, {'task_switches': {'bad key!': False}})
            ck(sched.task_switch('bad key!') is True, '非法任务名 → 忽略不报错（fail-open）')
            _write_switches(tmp, {'task_switches': 'not-a-dict'})
            ck(sched.task_switch('analysis_discipline') is True, '块不是 dict → 开')

            print('== 2. fail-open：文件异常一律按开 ==')
            _write_switches(tmp, None)
            ck(sched.task_switch('analysis_discipline') is True, '文件不存在 → 开')
            _write_switches(tmp, {'task_switches': {'analysis_discipline': False}})
            with open(sched._TASK_SWITCHES_FILE, 'w', encoding='utf-8') as f:
                f.write('{ broken json')
            sched._task_switch_cache['sig'] = None
            ck(sched.task_switch('analysis_discipline') is True, 'JSON 写坏 → 开（不沿用坏值）')

            print('== 3. mtime 缓存：改文件免重启生效 ==')
            _write_switches(tmp, {'task_switches': {'analysis_discipline': True}})
            ck(sched.task_switch('analysis_discipline') is True, '先读到开')
            _write_switches(tmp, {'task_switches': {'analysis_discipline': False}})
            ck(sched.task_switch('analysis_discipline') is False, '改文件后立即读到关')

            print('== 4. 分析纪律：邮件总开关 + 任务开关接线 ==')
            _store_state['data'] = {}
            from crypto.task.monitor import analysis_discipline as ad
            _write_switches(tmp, {'task_switches': {'analysis_discipline': True}})
            ck(ad._email_send_ok({}, {'enabled': False}) is False, 'kv email.enabled=false → 不发信')
            ck(ad._email_send_ok({}, {'enabled': True}) is True, 'kv 开 + 文件开 → 发信')
            ck(ad._email_send_ok({}, {}) is True, '存量配置无 enabled 键 → 默认发信（向后兼容）')
            _write_switches(tmp, {'task_switches': {'analysis_discipline': False}})
            ck(ad._email_send_ok({}, {'enabled': True}) is False,
               'kv 开但文件关 → 不发信（config.json 一键急停）')

            # register_discipline_job：文件关闭时不注册、返回 0（不碰真 DB：
            # 打桩 load_config 返回开启的引擎配置，只验文件闸那一条分支）
            import types
            fake_disc = types.SimpleNamespace(
                load_config=lambda: {'enabled': True})
            real_disc = ad.disc
            real_ts = sched.task_scheduler
            registered = {'jobs': [], 'removed': []}

            class FakeSched:
                def register_job(self, func, **kw):
                    registered['jobs'].append(kw.get('job_id'))

                def remove_job(self, job_id):
                    registered['removed'].append(job_id)

            ad.disc = fake_disc
            sched.task_scheduler = FakeSched()
            try:
                _write_switches(tmp, {'task_switches': {'analysis_discipline': False}})
                ck(ad.register_discipline_job() == 0
                   and not registered['jobs']
                   and registered['removed'] == ['analysis_discipline'],
                   '文件关 → 巡检不注册且移除旧任务，返回 0')
                _write_switches(tmp, {'task_switches': {'analysis_discipline': True}})
                ck(ad.register_discipline_job() == ad.CHECK_INTERVAL_SECONDS
                   and registered['jobs'] == ['analysis_discipline'],
                   '文件开 + 引擎开 → 正常注册')
            finally:
                ad.disc = real_disc
                sched.task_scheduler = real_ts

            print('== 5. 每日归零：task_switches 独立分支 ==')
            _store_state['data'] = {}
            from crypto.task import memory_daily_reset as mdr
            calls = {'register': [], 'remove': []}

            class FakeSched2:
                def register_job(self, func, **kw):
                    calls['register'].append(kw.get('job_id'))

                def remove_job(self, job_id):
                    calls['remove'].append(job_id)

            real_ts2 = sched.task_scheduler
            sched.task_scheduler = FakeSched2()
            try:
                _write_switches(tmp, {'task_switches': {'daily_memory_reset': False}})
                jid, note = mdr.register_daily_reset_job()
                ck(jid is None and not calls['register'] and 'task_switches' in note,
                   '文件关 → 每日归零不注册', note)
                _write_switches(tmp, {})
                jid, note = mdr.register_daily_reset_job()
                ck(jid == mdr.DAILY_JOB_ID and calls['register'] == [mdr.DAILY_JOB_ID],
                   '缺省（无块）→ 照常注册', note)
            finally:
                sched.task_scheduler = real_ts2

            print('== 6. kv 持久开关层与文件层合并 ==')
            _write_switches(tmp, {})
            _store_state['data'] = {'jobs': {'analysis_discipline': False}}
            ck(sched.task_switch('analysis_discipline') is False, 'kv 模块关 → 关')
            ck(sched.task_switch('alert_monitor') is True, 'kv 未登记的其它任务不受影响')
            _store_state['data'] = {'enabled': False, 'jobs': {'analysis_discipline': True}}
            ck(sched.task_switch('analysis_discipline') is False,
               'kv 全局关压过模块开（与页面行置灰口径一致）')
            _store_state['data'] = {'jobs': {'daily_memory_reset': True}}
            _write_switches(tmp, {'task_switches': {'daily_memory_reset': False}})
            ck(sched.task_switch('daily_memory_reset') is False,
               '文件 false 压过 kv true（急停只能单向关）')
            _write_switches(tmp, {'task_switches': {'daily_memory_reset': True}})
            ck(sched.task_switch('daily_memory_reset') is True, '两层都开 → 开')
            _store_state['data'] = {}

            print('== 7. 分析纪律模块总闸 module_enabled（业务 kv × 任务开关）==')
            from crypto import discipline_repo as disc_repo
            on = {'enabled': True}
            ck(disc_repo.module_enabled(on) is True, '两层缺省 → 开')
            _store_state['data'] = {'jobs': {'analysis_discipline': False}}
            ck(disc_repo.module_enabled(on) is False,
               '/task 页关巡检任务 → 总闸关（闸门/网页提醒随之静默）')
            _store_state['data'] = {'enabled': False, 'jobs': {'analysis_discipline': True}}
            ck(disc_repo.module_enabled(on) is False, 'kv 全局任务开关关 → 总闸关')
            _store_state['data'] = {}
            _write_switches(tmp, {'task_switches': {'analysis_discipline': False}})
            ck(disc_repo.module_enabled(on) is False, '文件急停 → 总闸关（压过缺省开）')
            _write_switches(tmp, {})
            ck(disc_repo.module_enabled({'enabled': False}) is False, '业务 kv 引擎开关关 → 总闸关')
            ck(disc_repo.module_enabled(on) is True, '全部恢复 → 总闸开')
        finally:
            sched.load_switch_state = _real_store_loader
            sched._TASK_SWITCHES_FILE = sched._TASK_SWITCHES_FILE_BACKUP

    print(f'\n结果：{len(_PASS)} 通过 / {len(_FAIL)} 失败')
    if _FAIL:
        for f in _FAIL:
            print(f'  ❌ {f}')
        sys.exit(1)


if __name__ == '__main__':
    main()
