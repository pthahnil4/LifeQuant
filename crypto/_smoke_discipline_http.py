#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析纪律（批次11）HTTP 层冒烟：用 Flask test_client 打通全部蓝图路由

不起服务、不占端口。写操作全部可还原：
- 配置改动测完还原原值
- fill-slot 把 required_count 抬到上限 50 让闸门必然拦截，因此只会拿到 403
- 豁免/soft 分支确实会写入一个真格子，测完立即 unfill-slot 撤销
- check-now 前先把邮件与日报静默，不往用户收件箱塞测试信
- 巡检会真写 analysis_reminder_log，且写的是 required_count=50 口径下的假判定，
  所以台账本身也在还原范围内（见 restore_ledger）
- 开跑前先拿巡检维护租约，让同一 DB 上的生产巡检本轮让路（见
  _preflight_shared_process），退出时无条件撤销

运行：python -m crypto._smoke_discipline_http
"""

import json
import os
import time

# 必须在导入任何 crypto 子模块之前设置：app.py 在导入期就会拉起调度器与
# 两个监控线程，那些线程会真发告警邮件、真调 OKX、真跑巡检（上一轮
# 冒烟就被后台线程向生产收件箱发了一封日报）。test_client 只想打接口。
os.environ['CRYPTO_NO_BACKGROUND'] = '1'

from sqlalchemy import select

from .app import app
from .database import session_scope
from .models import AnalysisReminderLog
from . import discipline_repo as disc
from . import config_store_repo

FAILS = []


def _title(t):
    print('\n' + '=' * 72)
    print(t)
    print('=' * 72)


def ck(cond, label, extra=''):
    tag = 'OK  ' if cond else 'FAIL'
    if not cond:
        FAILS.append(label)
    print(f'  [{tag}] {label}' + (f'  {extra}' if extra else ''))


def jget(client, path):
    r = client.get(path)
    try:
        return r.status_code, r.get_json()
    except Exception:
        return r.status_code, None


def jpost(client, path, body=None):
    r = client.post(path, json=body or {})
    try:
        return r.status_code, r.get_json()
    except Exception:
        return r.status_code, None


def biz_code(res):
    """本项目路由约定：HTTP 永远 200，业务结果放在响应体 code 里
    （plan_routes/discipline_routes 全部如此，前端 apiPost 也只看 res.code）。
    因此闸门拦截的断言必须盯 biz_code，盯 HTTP 状态会得到假失败。
    """
    return (res or {}).get('code')


# =============================================================================
# 配置还原护栏
# -----------------------------------------------------------------------------
# 冒烟中途断网最危险的后果不是测试失败，而是把 required_count=50 +
# active_hours='00:00-23:59' 这种「必定拦下所有交易打卡」的试验配置
# 留在生产库里（上一轮就真的在 [G] 的 finally 里撞上断网）。因此：
#   1) 写试验配置失败 → 重试，仍失败就跳过本项，不抛到外层
#   2) 还原比测试本身更顽强：重试 6 次，全失败则在退出前再兜一次
# =============================================================================

BASELINE_CONFIG = None
BASELINE_STATE = None
BASELINE_LEDGER = None   # {hour_slot: 字段快照}；巡检会往这张表写行
_DIRTY = {'v': False}

_LEDGER_FIELDS = ('stat_date', 'required_count', 'actual_count', 'missing_insts',
                  'status', 'notified', 'notified_at', 'resolved_at', 'created_at')


def _ledger_row(r):
    return {k: getattr(r, k) for k in _LEDGER_FIELDS}


def mark_dirty():
    """标记「已动过配置」，供退出兜底判断是否需要还原"""
    _DIRTY['v'] = True


def snapshot_baseline():
    """记下进入冒烟前的配置、运行时状态与台账，作为唯一还原基准"""
    global BASELINE_CONFIG, BASELINE_STATE, BASELINE_LEDGER
    with session_scope() as s:
        BASELINE_CONFIG = config_store_repo.load_json_config(s, disc.KEY_DISCIPLINE_CONFIG)
        BASELINE_STATE = config_store_repo.load_json_config(s, disc.KEY_DISCIPLINE_STATE)
        BASELINE_LEDGER = {r.hour_slot: _ledger_row(r) for r in
                           s.execute(select(AnalysisReminderLog)).scalars().all()}
    print('  基准配置:', json.dumps(BASELINE_CONFIG, ensure_ascii=False)[:160])
    print('  基准状态:', json.dumps(BASELINE_STATE, ensure_ascii=False)[:160])
    print(f'  基准台账: {len(BASELINE_LEDGER)} 行', sorted(BASELINE_LEDGER)[:6])


def write_cfg(cfg, what='试验配置') -> bool:
    """写配置（带重试）。返回是否写成功，失败不抛异常"""
    mark_dirty()
    for i in range(3):
        try:
            disc.save_config(cfg)
            return True
        except Exception as e:
            print(f'  [WARN] 写{what}失败（第 {i + 1}/3 次），3s 后重试: {e}')
            time.sleep(3)
    print(f'  [WARN] 写{what}连续失败，本项无法安全执行')
    return False


def restore_baseline(label='') -> bool:
    """把配置与豁免状态还原到基准（带重试）"""
    if BASELINE_CONFIG is None and BASELINE_STATE is None:
        return True
    last = None
    for i in range(6):
        try:
            with session_scope() as s:
                if BASELINE_CONFIG is None:
                    config_store_repo.delete_json_config(s, disc.KEY_DISCIPLINE_CONFIG)
                else:
                    config_store_repo.save_json_config(
                        s, disc.KEY_DISCIPLINE_CONFIG, BASELINE_CONFIG)
                if BASELINE_STATE is None:
                    config_store_repo.delete_json_config(s, disc.KEY_DISCIPLINE_STATE)
                else:
                    config_store_repo.save_json_config(
                        s, disc.KEY_DISCIPLINE_STATE, BASELINE_STATE)
            config_store_repo.invalidate_config_cache(disc.KEY_DISCIPLINE_CONFIG)
            config_store_repo.invalidate_config_cache(disc.KEY_DISCIPLINE_STATE)
            _DIRTY['v'] = False
            print(f'  已还原{label} →',
                  json.dumps(disc.load_config(), ensure_ascii=False)[:150])
            return True
        except Exception as e:
            last = e
            print(f'  [WARN] 还原配置失败（第 {i + 1}/6 次），5s 后重试: {e}')
            time.sleep(5)
    print('!' * 72)
    print('!! 配置还原失败：纪律配置可能停在试验值上。')
    print('!! required_count=50 / active_hours=00:00-23:59 会拦下所有交易打卡，')
    print('!! 请立即到【任务计划 → 分析纪律看板 → 纪律规则】重新保存一次，或执行：')
    print('!!   python -m crypto._smoke_discipline_http --reset')
    print(f'!! 最后错误: {last}')
    print('!' * 72)
    return False


def restore_ledger(label='') -> bool:
    """把 analysis_reminder_log 还原到基准快照。

    为什么台账也得还原：巡检与 check-now 是真写库的，而试验配置把
    required_count 抬到上限 50，于是写下来的是「每小时要填 50 条分析记录、
    你没填 → 判你缺档」这种从来没存在过的规则（实测一次冒烟留下 17 行，
    其中 8 行是凌晨 00~07 的睡觉时间）。它们会同时污染看板断档统计与缺口
    邮件——正是使用者投诉的那两样，不还原等于每跑一次冒烟就重现一次。
    """
    if BASELINE_LEDGER is None:
        return True
    last = None
    for i in range(6):
        try:
            deleted = 0
            with session_scope() as s:
                for r in s.execute(select(AnalysisReminderLog)).scalars().all():
                    base = BASELINE_LEDGER.get(r.hour_slot)
                    if base is None:
                        s.delete(r)              # 冒烟新建的槽：整行删掉
                        deleted += 1
                        continue
                    for k, v in base.items():    # 既有行：被改过的字段写回
                        setattr(r, k, v)
            print(f'  已还原台账{label} → 删除 {deleted} 行新建，基准 {len(BASELINE_LEDGER)} 行')
            return True
        except Exception as e:
            last = e
            print(f'  [WARN] 还原台账失败（第 {i + 1}/6 次），5s 后重试: {e}')
            time.sleep(5)
    print('!' * 72)
    print('!! 台账还原失败：analysis_reminder_log 里可能残留假缺档行。到【分析纪律看板】')
    print('!! 看最近小时，把 required_count 不是当前配置值的行删掉（或重跑一次本冒烟）。')
    print(f'!! 最后错误: {last}')
    print('!' * 72)
    return False


# =============================================================================
# 任务开关层（kv task_switches）基准护栏
# -----------------------------------------------------------------------------
# /task 页的任务开关已接入纪律闸门总闸（module_enabled）：用户若已把
# 「分析纪律巡检」关掉，闸门必然全部放行，"必须拦 403/巡检必须注册"等
# 老用例不再成立。因此冒烟期间强制把任务层的 analysis_discipline 与
# 全局开关置开，退出时无条件还原用户基准（含原本没这个键的情况）。
# =============================================================================

_SWITCH_BASE = {'v': None, 'dirty': False}


def _force_task_switches_on():
    from .task import scheduler as sched
    with session_scope() as s:
        base = config_store_repo.load_json_config(s, sched.KEY_TASK_SWITCHES)
    _SWITCH_BASE['v'] = base
    forced = dict(base or {})
    jobs = dict(forced.get('jobs') or {})
    jobs['analysis_discipline'] = True
    forced['jobs'] = jobs
    forced['enabled'] = True
    with session_scope() as s:
        config_store_repo.save_json_config(s, sched.KEY_TASK_SWITCHES, forced)
    config_store_repo.invalidate_config_cache(sched.KEY_TASK_SWITCHES)
    _SWITCH_BASE['dirty'] = True
    print('  task_switches 用户基准:', json.dumps(base, ensure_ascii=False)[:120])
    print('  冒烟期间强制打开 analysis_discipline 任务开关（测完还原）')


def _restore_task_switches(what=''):
    if not _SWITCH_BASE['dirty']:
        return
    from .task import scheduler as sched
    base = _SWITCH_BASE['v']
    for i in range(6):
        try:
            with session_scope() as s:
                if base is None:
                    config_store_repo.delete_json_config(s, sched.KEY_TASK_SWITCHES)
                else:
                    config_store_repo.save_json_config(s, sched.KEY_TASK_SWITCHES, base)
            config_store_repo.invalidate_config_cache(sched.KEY_TASK_SWITCHES)
            _SWITCH_BASE['dirty'] = False
            print(f'  task_switches 已还原用户基准{what}')
            return
        except Exception as e:
            print(f'  [WARN] task_switches 还原第 {i + 1} 次失败{what}: {e}')
            time.sleep(1)
    print('!! task_switches 多次还原失败：/task 页若发现分析纪律开关状态不对，'
          '请重新勾选或直接删除 kv_store 的 task_switches 键')


def _exit_guard():
    """退出兜底：任何一处 finally 因断网没还原成功，这里再试一次"""
    if _DIRTY['v']:
        print('\n[退出兜底] 检测到配置可能未还原，最后再试一次…')
        restore_baseline('（退出兜底）')
    _restore_task_switches('（退出兜底）')
    # 台账不受 _DIRTY 管：check-now 无论配置有没有改过都会写行，所以无条件跑
    restore_ledger('（退出兜底）')
    _release_lease()


def check_pages_and_static(client):
    _title('[A] 页面渲染与静态资源（nav.html 全站注入是否生效）')
    for path in ('/', '/plan', '/analysis', '/task', '/journal', '/calorie', '/alert'):
        st, body = None, None
        r = client.get(path)
        st = r.status_code
        body = r.get_data(as_text=True)
        ok = st == 200 and 'js/discipline.js' in body and 'js/analysis_quick.js' in body
        ck(ok, f'GET {path}', f'status={st} 注入脚本={"有" if ok else "缺"}')
    for js in ('/static/js/discipline.js', '/static/js/analysis_quick.js',
               '/static/js/discipline_board.js'):
        r = client.get(js)
        ck(r.status_code == 200 and len(r.get_data()) > 500, f'GET {js}',
           f'status={r.status_code} bytes={len(r.get_data())}')


def check_status_and_gate(client):
    _title('[B] status / analysis-gate 载荷契约')
    st, res = jget(client, '/plan/api/discipline/status')
    ck(st == 200 and res and res.get('code') == 200, 'GET discipline/status 返回 200')
    d = (res or {}).get('data') or {}
    for key in ('enabled', 'strict_mode', 'hour_slot', 'required', 'actual', 'ok',
                'elapsed_minutes', 'banner', 'today', 'streak', 'browser', 'records'):
        ck(key in d, f'status.data 含字段 {key}')
    ck(isinstance(d.get('today'), dict) and 'pending' in d['today'],
       'status.today 含 pending（台账空洞不计入分母）',
       json.dumps(d.get('today'), ensure_ascii=False))

    st, res = jget(client, '/plan/api/analysis-gate')
    ck(st == 200 and res and res.get('code') == 200, 'GET analysis-gate（无参=当前小时）')
    g = (res or {}).get('data') or {}
    for key in ('enabled', 'allowed', 'mode', 'hour_slot', 'required', 'actual',
                'ok', 'active', 'missing_coins', 'records'):
        ck(key in g, f'gate.data 含字段 {key}')
    print(f"       当前判定: allowed={g.get('allowed')} mode={g.get('mode')} "
          f"actual={g.get('actual')}/{g.get('required')}")

    # 时段外必须放行（08:00-23:00 之外不追责）
    st, res = jget(client, '/plan/api/analysis-gate?filled_at=2026-09-07 03:30')
    g2 = (res or {}).get('data') or {}
    ck(g2.get('mode') == 'inactive' and g2.get('allowed') is True,
       '凌晨 03:30 → inactive 且放行', f"mode={g2.get('mode')}")

    # 非法参数必须 fail-open，不得 500
    st, res = jget(client, '/plan/api/analysis-gate?filled_at=not-a-date')
    ck(st == 200 and (res or {}).get('code') == 200,
       '非法 filled_at 不炸（回退当前时间）', f'status={st}')


def check_config_roundtrip(client):
    _title('[C] 配置读写回环（含非法值夹紧）')
    mark_dirty()
    try:
        st, res = jget(client, '/plan/api/discipline/config')
        ck(st == 200 and res.get('code') == 200 and isinstance(res.get('data'), dict),
           'GET discipline/config')

        trial = {
            'enabled': True, 'required_count': 999, 'grace_minutes': 10000,
            'active_hours': '乱七八糟', 'strict_mode': '不存在的模式',
            'email': {'on_gap': False, 'merge_after': 999, 'daily_report': '99:99'},
            'browser': {'banner': False, 'sound': False, 'desktop_notify': False,
                        'poll_seconds': 1, 'banner_after_minutes': 999},
        }
        st, res = jpost(client, '/plan/api/discipline/config', trial)
        ck(st == 200 and res.get('code') == 200, 'POST discipline/config',
           f"message={res.get('message')}")
        c = res.get('data') or {}
        ck(c.get('required_count') == 50, 'required_count 夹紧到上限 50', str(c.get('required_count')))
        ck(c.get('grace_minutes') == 180, 'grace_minutes 夹紧到上限 180', str(c.get('grace_minutes')))
        # 非法生效时段的回退目标是「改动前的存量值」（路由保留现值），
        # 不能写死字面量：默认值已从 08:00-23:00 改成 08:00-24:00，
        # 写死会让这条断言在两种正确行为下各自误报一次。
        want_ah = ((BASELINE_CONFIG or {}).get('active_hours')
                   or disc.DEFAULT_ACTIVE_HOURS)
        ck(c.get('active_hours') == want_ah, '非法 active_hours 回退到现值/默认',
           c.get('active_hours'))
        # 回退必须看得见：生效时段决定一天里有多少小时被追责，
        # 静默换掉会让用户以为自己填的值已生效。
        ck('无法解析' in str(res.get('message') or ''),
           '非法生效时段在 message 里明示', str(res.get('message'))[:90])

        # 生效时段边界：夜里到底管不管，就是这几行口径，必须钉死
        ck(disc.parse_active_hours('08:00-24:00') == (480, 1440),
           "parse '08:00-24:00' → (480,1440)",
           str(disc.parse_active_hours('08:00-24:00')))
        w16 = {'active_hours': '08:00-24:00'}
        ck(all(disc.is_slot_active(f'2026-01-01 {h:02d}', w16) for h in range(8, 24)),
           '08:00-24:00 覆盖 08~23 共 16 格')
        ck(not any(disc.is_slot_active(f'2026-01-01 {h:02d}', w16) for h in range(0, 8)),
           '08:00-24:00 不含凌晨 00~07（睡觉时间不追责）')
        # 旧行为：写 24:00 被判非法 → 解析成 None → 对所有槽返回 True，
        # 追责面从 16 小时静默放大到 24 小时。手误应当缩小而不是放大追责面。
        bad = {'active_hours': '乱七八糟'}
        ck(not disc.is_slot_active('2026-01-01 03', bad),
           '非法时段回退默认而不是放开全天（关键护栏）')
        ck(disc.is_slot_active('2026-01-01 03', {'active_hours': ''}) is True,
           '显式留空仍 = 全天生效（保留原语义）')
        ck(c.get('strict_mode') == 'strict', '非法 strict_mode 回退 strict', c.get('strict_mode'))
        ck((c.get('email') or {}).get('daily_report') == '23:00', '非法日报时刻回退 23:00',
           str((c.get('email') or {}).get('daily_report')))
        ck((c.get('email') or {}).get('merge_after') == 24, 'merge_after 夹紧到 24')
        ck((c.get('browser') or {}).get('poll_seconds') == 15, 'poll_seconds 夹紧到下限 15')
        ck((c.get('browser') or {}).get('banner_after_minutes') == 59,
           'banner_after_minutes 夹紧到上限 59')

        # 关掉再打开：register_discipline_job 两条分支都要走通
        st, res = jpost(client, '/plan/api/discipline/config', {'enabled': False})
        ck(st == 200 and res.get('code') == 200, '关闭纪律（触发 remove_job 分支）',
           res.get('message'))
        st, res = jget(client, '/plan/api/discipline/status')
        ck(((res or {}).get('data') or {}).get('enabled') is False, '关闭后 status.enabled=False')
        st, res = jget(client, '/plan/api/analysis-gate')
        ck(((res or {}).get('data') or {}).get('mode') == 'disabled'
           and ((res or {}).get('data') or {}).get('allowed') is True,
           '关闭后闸门 mode=disabled 且放行')
    finally:
        restore_baseline()
        # 还原后再走一次 POST：既让缓存与巡检任务跟新配置对齐，
        # 也验「只传 enabled 的增量保存」不会把其它字段冲掉
        jpost(client, '/plan/api/discipline/config',
              {'enabled': bool((disc.load_config()).get('enabled', True))})


def check_task_switch_linkage(client):
    """[C2] 定时任务总开关→分析纪律全模块静默的联动契约

    /task 页关掉「分析纪律巡检」（kv task_switches）后，打卡前的 analysis-gate
    与全站轮询的 discipline/status 必须同步返回 disabled/enabled=False，
    前端才能摘掉倒计时横幅/角标（否则关了任务仍被打扰）。
    只动 task_switches 键，finally 里按基准原样还原（含删除新增键）。
    """
    _title('[C2] 任务开关 → 闸门/网页提醒静默联动')
    from .task import scheduler as sched
    with session_scope() as s:
        before = config_store_repo.load_json_config(s, sched.KEY_TASK_SWITCHES)
    try:
        sched.set_job_switch('analysis_discipline', False)
        d = (jget(client, '/plan/api/discipline/status')[1] or {}).get('data') or {}
        ck(d.get('enabled') is False, '页面开关关巡检 → status.enabled=False（角标/横幅静默）')
        g = (jget(client, '/plan/api/analysis-gate')[1] or {}).get('data') or {}
        ck(g.get('enabled') is False and g.get('mode') == 'disabled'
           and g.get('allowed') is True,
           '页面开关关巡检 → 闸门 disabled 直接放行（不做任何判定）',
           f"enabled={g.get('enabled')} mode={g.get('mode')}")

        sched.set_job_switch('analysis_discipline', True)
        sched.set_global_switch(False)
        d = (jget(client, '/plan/api/discipline/status')[1] or {}).get('data') or {}
        ck(d.get('enabled') is False, '全局任务开关关 → status 一并静默')

        sched.set_global_switch(True)
        # 任务开关层全开时，status 应跟随纪律引擎自身的 kv 开关（基准态可能本就关着，
        # 不能拿入口 status 当基准：入口值里混着 task_switches 的现状）
        want = bool(disc.load_config().get('enabled', True))
        d = (jget(client, '/plan/api/discipline/status')[1] or {}).get('data') or {}
        ck(d.get('enabled') == want, '任务开关全开 → status 跟随引擎开关',
           f'{d.get("enabled")} vs 引擎 {want}')

        # 文件级急停也须热反映到前台（mtime 缓存，免重启）
        real_file, real_cache = sched._TASK_SWITCHES_FILE, dict(sched._task_switch_cache)
        import tempfile
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fp = os.path.join(tmp, 'config.json')
                with open(fp, 'w', encoding='utf-8') as f:
                    json.dump({'task_switches': {'analysis_discipline': False}}, f)
                sched._TASK_SWITCHES_FILE = fp
                sched._task_switch_cache.update({'sig': None, 'data': None})
                d = (jget(client, '/plan/api/discipline/status')[1] or {}).get('data') or {}
                ck(d.get('enabled') is False, 'config.json 文件急停 → status 静默（免重启）')
        finally:
            sched._TASK_SWITCHES_FILE = real_file
            sched._task_switch_cache.update(real_cache)
    finally:
        restored = False
        for i in range(6):
            try:
                with session_scope() as s:
                    if before is None:
                        config_store_repo.delete_json_config(s, sched.KEY_TASK_SWITCHES)
                    else:
                        config_store_repo.save_json_config(s, sched.KEY_TASK_SWITCHES, before)
                config_store_repo.invalidate_config_cache(sched.KEY_TASK_SWITCHES)
                restored = True
                break
            except Exception as e:
                print(f'  [WARN] task_switches 还原第 {i + 1} 次失败: {e}')
                time.sleep(1)
        ck(restored, 'task_switches kv 已回到基准', json.dumps(before, ensure_ascii=False)[:80])


def check_board(client):
    _title('[D] 看板 board')
    for days in (7, 30, 90):
        st, res = jget(client, f'/plan/api/discipline/board?days={days}')
        d = (res or {}).get('data') or {}
        ok = (st == 200 and res.get('code') == 200 and d.get('days') == days
              and isinstance(d.get('daily'), list) and isinstance(d.get('digest'), dict)
              and isinstance(d.get('attribution'), dict)
              and len(d.get('heatmap') or []) == 7)
        ck(ok, f'board?days={days}',
           f"daily={len(d.get('daily') or [])} summary={json.dumps(d.get('summary'), ensure_ascii=False)}")
    st, res = jget(client, '/plan/api/discipline/board?days=99999')
    ck(((res or {}).get('data') or {}).get('days') == 180, 'days 越界夹紧到 180')
    d = (jget(client, '/plan/api/discipline/board?days=7')[1] or {}).get('data') or {}
    print('       归因:', json.dumps(d.get('attribution'), ensure_ascii=False))
    print('       补记:', json.dumps(d.get('backfill'), ensure_ascii=False))


def check_exempt(client):
    _title('[E] 豁免泄压阀')
    try:
        # 豁免判定在「生效时段」之后：时段外闸门直接 inactive 放行，根本走不到
        # exempt 分支。要验豁免就得先把生效时段撑到覆盖当前时刻，并抬高门槛
        # 让闸门先处于“真的会拦”的状态——否则后面的“放行”毫无信息量。
        cfg = disc.load_config()
        cfg.update({'enabled': True, 'strict_mode': 'strict',
                    'required_count': 50, 'active_hours': '00:00-23:59'})
        if not write_cfg(cfg, '[E] 试验配置'):
            print('  [SKIP] 无法写入试验配置（网络异常），跳过本项')
            return

        g = (jget(client, '/plan/api/analysis-gate')[1] or {}).get('data') or {}
        ck(g.get('mode') == 'strict_block' and g.get('allowed') is False,
           '豁免前闸门确实拦下', g.get('mode'))

        st, res = jpost(client, '/plan/api/discipline/exempt', {'hours': 3})
        until = ((res or {}).get('data') or {}).get('exempt_until')
        ck(biz_code(res) == 200 and bool(until), '豁免 3 小时', str(until))
        g = (jget(client, '/plan/api/analysis-gate')[1] or {}).get('data') or {}
        ck(g.get('mode') == 'exempt' and g.get('allowed') is True, '豁免期内闸门放行', g.get('mode'))
        stt = (jget(client, '/plan/api/discipline/status')[1] or {}).get('data') or {}
        ck(stt.get('exempt_until') == until, 'status 回带 exempt_until')
        ck(stt.get('banner') is False, '豁免期内不出横幅')

        st, res = jpost(client, '/plan/api/discipline/exempt', {'hours': 0})
        ck(not ((res or {}).get('data') or {}).get('exempt_until'), '取消豁免')
        g = (jget(client, '/plan/api/analysis-gate')[1] or {}).get('data') or {}
        ck(g.get('mode') == 'strict_block', '取消后闸门恢复拦截', g.get('mode'))
    finally:
        restore_baseline('[E]')


def _find_free_slot(client, card_type):
    """找一个真正可勾选的空格子。

    必须同时满足 status=='in_progress' 且未串行锁定：fill-slot 对其它状态
    一律返回 403「当前卡不可勾选」，那条 403 与分析纪律无关，
    拿它当闸门拦截的验证结果会把测试判成假通过。
    """
    _, res = jget(client, '/plan/api/list')
    for plan in (res or {}).get('data') or []:
        for card in (plan.get('stats') or {}).get('cards') or []:
            if card.get('type') != card_type:
                continue
            if card.get('status') != 'in_progress' or card.get('locked'):
                continue
            _, det = jget(client, '/plan/api/card-detail?plan_id='
                          f"{plan['id']}&card_id={card['id']}")
            c = (det or {}).get('data') or {}
            for i, sl in enumerate(c.get('slots') or []):
                if not sl.get('filled'):
                    return plan['id'], card['id'], i, c
    return None, None, None, None


def check_fill_slot_blocked(client):
    _title('[F] fill-slot 闸门硬阻断（required_count=50 上限，必定拦下且不写库）')
    plan_id, card_id, idx, card = _find_free_slot(client, 'trade')
    if not plan_id:
        print('  [SKIP] 没有可勾选（in_progress 且未锁定）的交易卡空格子，跳过本项')
        return
    print(f'  目标: plan={plan_id} card={card_id} slot_index={idx}（{card.get("title")}）')

    try:
        cfg = disc.load_config()
        cfg['required_count'] = 50          # 上限值，现实中不可能一小时做 50 条
        cfg['strict_mode'] = 'strict'
        cfg['enabled'] = True
        cfg['active_hours'] = '00:00-23:59'  # 保证测试时段一定生效
        if not write_cfg(cfg, '[F] 试验配置'):
            print('  [SKIP] 无法写入试验配置（网络异常），跳过本项')
            return

        payload = {
            'plan_id': plan_id, 'card_id': card_id, 'slot_index': idx,
            'record': {'prediction': '涨', 'duration_minutes': 60, 'actual': '',
                       'market_analysis': '冒烟测试', 'action_advice': '冒烟测试',
                       'account_balance': ''},
        }
        r = client.post('/plan/api/fill-slot', json=payload)
        res = r.get_json() or {}
        ck(biz_code(res) == 403, '当前小时缺分析 → 业务码 403',
           f'http={r.status_code} code={biz_code(res)}')
        ck((res.get('data') or {}).get('need_analysis') is True, '响应体带 need_analysis=True')
        print('       message:', str(res.get('message')).replace('\n', ' / '))

        # 关键：拦下就绝不能写库
        _, det = jget(client, f'/plan/api/card-detail?plan_id={plan_id}&card_id={card_id}')
        slots = ((det or {}).get('data') or {}).get('slots') or []
        ck(slots[idx].get('filled') is False, f'slot[{idx}] 仍未被填入（拦截未泄漏写入）')

        # 历史补录同样要拦
        payload['filled_at'] = '2026-09-01 10:00'
        r = client.post('/plan/api/fill-slot', json=payload)
        res = r.get_json() or {}
        ck(biz_code(res) == 403 and (res.get('data') or {}).get('is_history') is True,
           '历史补录缺分析 → 403 且 is_history=True')

        # 范围补录：全部被拦 → 403 + blocked_slots
        r = client.post('/plan/api/backfill-batch', json={
            'plan_id': plan_id, 'card_id': card_id,
            'entries': [{'filled_at': f'2026-09-0{d} 10:00',
                         'record': {'prediction': '涨', 'duration_minutes': 60}}
                        for d in (1, 2)]})
        res = r.get_json() or {}
        d = res.get('data') or {}
        ck(biz_code(res) == 403 and len(d.get('blocked_slots') or []) == 2,
           'backfill-batch 全拦 → 403 + blocked_slots 2 个',
           json.dumps(d.get('blocked_slots'), ensure_ascii=False))

        # 豁免后必须放行（泄压阀有效，不会因为规则太严而只能关功能）
        disc.set_exempt(2)
        payload.pop('filled_at', None)
        r = client.post('/plan/api/fill-slot', json=payload)
        res = r.get_json() or {}
        ck(biz_code(res) == 200, '豁免期内 fill-slot 放行', f'code={biz_code(res)} {res.get("message")}')
        if biz_code(res) == 200:
            _, det = jget(client, f'/plan/api/card-detail?plan_id={plan_id}&card_id={card_id}')
            slots = ((det or {}).get('data') or {}).get('slots') or []
            rec = slots[idx].get('record') or {}
            ck(slots[idx].get('filled') is True, '豁免期内确实写入成功')
            ck('analysis_hour' in rec and 'bypass_analysis' in rec and 'analysis_ids' in rec,
               '写入的记录带纪律关联三字段',
               f"hour={rec.get('analysis_hour')!r} ids={rec.get('analysis_ids')} "
               f"bypass={rec.get('bypass_analysis')}")
            ck(rec.get('bypass_analysis') is False,
               '豁免放行不算 bypass（豁免是正当休息，不是偷懒）',
               str(rec.get('bypass_analysis')))
            # 清理：撤销这次测试写入，保持数据原样
            r2 = client.post('/plan/api/unfill-slot', json={
                'plan_id': plan_id, 'card_id': card_id, 'slot_index': idx})
            ck(biz_code(r2.get_json()) == 200, '已撤销测试写入（unfill-slot）',
               str((r2.get_json() or {}).get('message')))
        disc.set_exempt(0)

        # soft 模式：放行但必须打 bypass 留痕
        cfg['required_count'] = 50
        cfg['strict_mode'] = 'soft'
        write_cfg(cfg, '[F] soft 配置')
        g = (jget(client, '/plan/api/analysis-gate')[1] or {}).get('data') or {}
        ck(g.get('mode') == 'soft_bypass' and g.get('allowed') is True,
           'soft 模式 → soft_bypass 且放行', g.get('mode'))
        r = client.post('/plan/api/fill-slot', json=payload)
        res = r.get_json() or {}
        if biz_code(res) == 200:
            _, det = jget(client, f'/plan/api/card-detail?plan_id={plan_id}&card_id={card_id}')
            slots = ((det or {}).get('data') or {}).get('slots') or []
            rec = slots[idx].get('record') or {}
            ck(rec.get('bypass_analysis') is True, 'soft 模式打卡被记为 bypass_analysis=True')
            r2 = client.post('/plan/api/unfill-slot', json={
                'plan_id': plan_id, 'card_id': card_id, 'slot_index': idx})
            ck(biz_code(r2.get_json()) == 200, '已撤销 soft 模式测试写入')
        else:
            ck(False, 'soft 模式应放行', f'code={biz_code(res)} {res.get("message")}')

        # off 模式：闸门完全不介入
        cfg['strict_mode'] = 'off'
        write_cfg(cfg, '[F] off 配置')
        g = (jget(client, '/plan/api/analysis-gate')[1] or {}).get('data') or {}
        ck(g.get('mode') == 'off' and g.get('allowed') is True, 'off 模式 → 闸门不介入')
    finally:
        restore_baseline('[F]')


def check_learn_card_unaffected(client):
    _title('[G] 学习卡不受纪律约束（只管交易打卡）')
    plan_id, card_id, idx, _card = _find_free_slot(client, 'learn')
    if not plan_id:
        print('  [SKIP] 没有可勾选（in_progress 且未锁定）的学习卡空格子')
        return
    try:
        cfg = disc.load_config()
        cfg.update({'required_count': 50, 'strict_mode': 'strict',
                    'enabled': True, 'active_hours': '00:00-23:59'})
        if not write_cfg(cfg, '[G] 试验配置'):
            print('  [SKIP] 无法写入试验配置（网络异常），跳过本项')
            return
        r = client.post('/plan/api/fill-slot', json={
            'plan_id': plan_id, 'card_id': card_id, 'slot_index': idx,
            'record': {'content': '冒烟测试学习内容', 'duration_minutes': 1}})
        res = r.get_json() or {}
        ck(biz_code(res) == 200, '纪律最严设置下学习卡仍可打卡',
           f'code={biz_code(res)} {res.get("message")}')
        if biz_code(res) == 200:
            r2 = client.post('/plan/api/unfill-slot', json={
                'plan_id': plan_id, 'card_id': card_id, 'slot_index': idx})
            ck(biz_code(r2.get_json()) == 200, '已撤销学习卡测试写入')
    finally:
        restore_baseline('[G]')


def check_backfill_preview(client):
    _title('[H] 回溯分析预览（只读取价，不写库）')
    from . import analysis_record_repo as ana_repo

    # 真拉 K 线要连 OKX：网络不通时每个币种 4 次重试能把冒烟拖到十几分钟
    # （上一轮就是在这里超时的）。本项验的是路由自身的契约——槽归一去重
    # 限量、方向字段必须留空、缺币种如实上报——不是交易所连通性，故 stub 取价。
    real_fn = ana_repo.historical_prices
    calls = []

    def fake_prices(inst_id, targets, bar='1H'):
        calls.append({'inst_id': inst_id, 'n': len(targets), 'bar': bar})
        keys = [t.strftime('%Y-%m-%d %H:%M:%S') for t in targets]
        if len(calls) == 2:            # 第 2 个币种模拟“历史K线不可用”
            return {k: None for k in keys}
        return {k: 100.0 + i for i, k in enumerate(keys)}

    ana_repo.historical_prices = fake_prices
    try:
        st, res = jget(client,
                       '/plan/api/discipline/backfill-preview?slots=2026-09-01 11,2026-09-01 10')
        d = (res or {}).get('data') or {}
        ck(biz_code(res) == 200, 'backfill-preview 返回 200', f'http={st}')
        print('       message:', res.get('message'))

        inst_ids = d.get('inst_ids') or []
        ck(len(inst_ids) > 0, '返回跟踪币种清单', json.dumps(inst_ids, ensure_ascii=False))
        ck(len(calls) == len(inst_ids) and all(c['n'] == 2 for c in calls),
           '每币种只拉一次K线（批量取价，不是每槽一拉）',
           json.dumps(calls, ensure_ascii=False))
        ck(all(c['bar'] == '1H' for c in calls), 'K线周期固定 1H')

        groups = d.get('slots') or []
        ck([g['hour_slot'] for g in groups] == ['2026-09-01 10', '2026-09-01 11'],
           '乱序入参 → 槽按时间升序返回',
           json.dumps([g['hour_slot'] for g in groups], ensure_ascii=False))

        grp = groups[0] if groups else {}
        items = grp.get('items') or []
        ck(len(items) == max(0, len(inst_ids) - 1),
           f"可用 {len(items)} 条（1 个币种无K线，共 {len(inst_ids)} 币种）")
        ck(len(grp.get('missing') or []) == 1, '缺币种如实上报',
           json.dumps(grp.get('missing'), ensure_ascii=False))
        ck(len(d.get('errors') or []) == 1, '不可用币种进 errors',
           json.dumps(d.get('errors'), ensure_ascii=False))
        want_ts = disc.slot_start('2026-09-01 10').strftime('%Y-%m-%d %H:%M:%S')
        for it in items:
            ck(it['short_dir'] == '' and it['long_dir'] == '' and it['long_dir_prev'] == '',
               f"{it['instId']} 回溯方向字段留空（不臆造历史方向）")
            ck(it['ts'] == want_ts, f"{it['instId']} 取价时点=槽起点", it['ts'])
            ck(it['user_judgment'] == 'watch' and '回溯补记' in it['user_reason'],
               f"{it['instId']} 默认判断=观望且原因标注回溯")

        st, res = jget(client, '/plan/api/discipline/backfill-preview?slots=')
        ck(biz_code(res) == 400, '空 slots → 业务码 400', f'http={st}')

        calls.clear()
        st, res = jget(client, '/plan/api/discipline/backfill-preview?slots=' +
                       ','.join([f'2026-08-0{day} {h:02d}' for day in (1, 2) for h in range(24)]))
        d = (res or {}).get('data') or {}
        n = len(d.get('slots') or [])
        ck(n == 31, f'48 个槽去重后限量到 31（实际 {n}）')
        ck(bool(calls) and all(c['n'] == 31 for c in calls),
           '先限量再取价（不白拉 48 个时点）', json.dumps([c['n'] for c in calls]))
    finally:
        ana_repo.historical_prices = real_fn


def check_scheduler_registration(client):
    _title('[I] 巡检任务注册与手动巡检')
    import inspect
    from .task import scheduler as sched_mod
    from .task.scheduler import task_scheduler
    from .task.monitor.analysis_discipline import (
        DISCIPLINE_JOB_ID, CHECK_INTERVAL_SECONDS, register_discipline_job)

    # CRYPTO_NO_BACKGROUND 下 register_default_jobs 不会自动跑，这里分两层验：
    #   1) 静态接线：生产启动路径确实调了 register_discipline_job
    #   2) 动态注册：只注册、绝不 start()，不让任何生产定时任务真跑起来
    src = inspect.getsource(sched_mod.register_default_jobs)
    ck('register_discipline_job' in src,
       'register_default_jobs 已接线分析纪律巡检（生产启动路径）')
    ck(task_scheduler.get_status()['running'] is False,
       '调度器未启动（冒烟不跑任何生产定时任务）')

    interval = register_discipline_job()
    ck(interval == CHECK_INTERVAL_SECONDS, 'register_discipline_job 返回巡检周期', f'{interval}s')
    jobs = {j['id']: j for j in task_scheduler.get_all_jobs()}
    ck(DISCIPLINE_JOB_ID in jobs, f'调度器含 {DISCIPLINE_JOB_ID}',
       json.dumps(jobs.get(DISCIPLINE_JOB_ID), ensure_ascii=False))
    ck(jobs.get(DISCIPLINE_JOB_ID, {}).get('trigger_kwargs', {}).get('seconds')
       == str(CHECK_INTERVAL_SECONDS), '巡检周期=300s（5分钟）',
       str(jobs.get(DISCIPLINE_JOB_ID, {}).get('trigger_kwargs')))

    # 关掉后必须把任务移除（否则用户关了开关巡检还在跑）
    try:
        off = disc.load_config()
        off['enabled'] = False
        if write_cfg(off, '[I] 关闭配置'):
            register_discipline_job()
            jobs = {j['id']: j for j in task_scheduler.get_all_jobs()}
            ck(DISCIPLINE_JOB_ID not in jobs, '关闭纪律后巡检任务已移除', str(list(jobs)))
        register_discipline_job()

        # 手动巡检会真发邮件，而日报发送后会把 daily_report_date 写进运行时
        # 状态（失败也写，当天不重试），冒烟绝不能把用户今天的真日报给吃掉。
        # 这里临时静默邮件与日报，跑完还原。
        cfg = disc.load_config()
        cfg.setdefault('email', {})
        cfg['email']['on_gap'] = False
        cfg['email']['daily_report'] = ''
        if not write_cfg(cfg, '[I] 静默邮件配置'):
            print('  [SKIP] 无法静默邮件，不跑 check-now（宁可不验，不可发测试信）')
            return

        st, res = jpost(client, '/plan/api/discipline/check-now', {})
        ck(biz_code(res) == 200, 'check-now 手动巡检', f'http={st}')
        run = (res or {}).get('data') or {}
        print('       本轮:', json.dumps(run, ensure_ascii=False))
        ck(run.get('email_sent') == 0, '冒烟期间未发出任何邮件', str(run.get('email_sent')))
        ck(run.get('daily_report') is False, '冒烟期间未触发日报', str(run.get('daily_report')))
        ck(not run.get('error'), '巡检无异常', str(run.get('error')))
        print(f"       判定 {run.get('judged')} 槽（合格 {run.get('satisfied')} / "
              f"缺口 {run.get('missing')} / 豁免 {run.get('exempt')}）")

        # epoch 护栏：台账里绝不能出现生效起点之前的槽。直接查库而不看
        # 本轮 judged：后者取决于跑测试的时刻（跨过整点就会有新槽被判定），
        # 拿它当断言会让脚本时间敏感。
        ep = disc.epoch_dt()
        with session_scope() as s:
            logged = s.execute(select(AnalysisReminderLog.hour_slot)).scalars().all()
        early = sorted({h for h in logged if ep and disc.slot_start(h) < ep})
        ck(not early, '台账里没有生效起点之前的槽（epoch 护栏）',
           f'epoch={ep} 台账 {len(logged)} 行' + (f' 越界: {early[:5]}' if early else ''))
    finally:
        restore_baseline('[I]')
        register_discipline_job()


def _preflight_shared_process() -> bool:
    """探测是否另有 app.py 实例在跑，并取得巡检维护租约。

    为何必须处理：冒烟会往共享 DB 写试验配置（required_count=50、active_hours 撑到
    全天），而同时在跑的生产调度器会读到它——实测因此把「每小时要填 50 条」
    和凌晨 00~07 的睡觉时间写进了真台账，并据此向生产收件箱发出了两封断档
    汇总信（16:21 与 17:07）。本进程里的 CRYPTO_NO_BACKGROUND 只能管住自己，
    管不住另一个进程。

    处理方式是让路而不是拒跑：写一条带过期时间的维护租约（巡检读到就本轮
    跳过），退出时无条件撤销。拒跑在实践中等于永远跑不了——生产服务平时就
    是开着的。

    残留窗口（如实记下）：若生产巡检刚好在租约写入前一刻开了一轮，它拿的是
    当时的真实配置，最多写真实判定、发一封本来就是该发的信，不会把假数字
    落库。
    """
    import socket

    port = int(os.environ.get('CRYPTO_WEB_PORT', '7777') or 7777)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(0.4)
        live = sk.connect_ex(('127.0.0.1', port)) == 0
    if live:
        print(f'  检测到 {port} 端口有实例在监听（很可能是生产 app.py）')
    try:
        until = disc.begin_maintenance(reason='HTTP 冒烟 _smoke_discipline_http')
    except Exception as e:
        print(f'  [FAIL] 无法写入维护租约（DB 不可用？）: {e}')
        if live:
            print('  生产巡检不会被本轮冒烟让路，拒跑以免再现“试验配置漏进生产”事故。')
            print('  请先停服务再跑，或确认 DB 已可用。')
            return False
        print('  拿不到租约，但没发现其它实例，继续跑（写台账失败会另外报）')
        return True
    print(f'  已取得巡检维护租约，至 {until}（退出时自动撤销）')
    return True


def _release_lease():
    """撤销维护租约，让生产巡检恢复判定"""
    try:
        if disc.end_maintenance():
            print('  已撤销巡检维护租约（生产巡检恢复判定）')
    except Exception as e:
        print(f'  [WARN] 维护租约撤销失败，将在到期后自动失效: {e}')


def main():
    app.config['TESTING'] = True
    c = app.test_client()

    _title('[0] 基准快照（所有试验配置的还原目标）')
    snapshot_baseline()
    _force_task_switches_on()

    sections = [
        check_pages_and_static,
        check_status_and_gate,
        check_config_roundtrip,
        check_task_switch_linkage,
        check_board,
        check_exempt,
        check_fill_slot_blocked,
        check_learn_card_unaffected,
        check_backfill_preview,
        check_scheduler_registration,
    ]
    for fn in sections:
        try:
            fn(c)
        except Exception as e:
            # 单段崩了不应该把后面的验证全带走（上一轮断网就只跑到 [G] 就死了）
            import traceback
            ck(False, f'{fn.__name__} 抛异常中断', f'{type(e).__name__}: {e}')
            traceback.print_exc()
        if _DIRTY['v']:
            # 每段结束都确认配置回到基准，试验值绝不过段
            restore_baseline(f'（{fn.__name__} 段末兜底）')

    _title('[J] 台账还原（冒烟不得留下假判定行）')
    ok = restore_ledger('（[J]）')
    with session_scope() as s:
        left = s.execute(select(AnalysisReminderLog.hour_slot)).scalars().all()
    ck(ok and sorted(left) == sorted(BASELINE_LEDGER or {}),
       '台账已回到基准（无冒烟残留）',
       f'现 {len(left)} 行 / 基准 {len(BASELINE_LEDGER or {})} 行')

    _release_lease()
    ck(disc.maintenance_until() is None, '维护租约已清除（巡检恢复判定）')
    _restore_task_switches('（主流程末）')

    _title('HTTP 冒烟结果')
    if FAILS:
        print(f'  共 {len(FAILS)} 项失败:')
        for f in FAILS:
            print('   -', f)
        raise SystemExit(1)
    print('  全部通过 ✅')


if __name__ == '__main__':
    import atexit
    import copy
    import sys

    atexit.register(_exit_guard)

    if '--reset' in sys.argv:
        # 急救入口：上一次冒烟断网导致还原失败时，把纪律配置写回安全默认值
        # （required_count=1 / 08:00-24:00 / strict）、清掉豁免与维护租约。
        _title('配置急救重置')
        disc.save_config(copy.deepcopy(disc.DEFAULT_DISCIPLINE_CONFIG))
        disc.set_exempt(0)
        disc.end_maintenance()
        print('  已写回默认配置:', json.dumps(disc.load_config(), ensure_ascii=False))
        raise SystemExit(0)

    _title('前置检查')
    if not _preflight_shared_process():
        raise SystemExit(2)

    main()
