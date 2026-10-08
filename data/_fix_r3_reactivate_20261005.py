# -*- coding: utf-8 -*-
"""第3轮交易卡倒计时到期后再激活（dry-run 默认，--apply 才写库）
================================================================
背景：2026-10-05 14:12 第 3 轮卡 trade_8a460d28 被「10 天倒计时」到期自动
结算为 failed（起始 2026-09-25 12:00 已满 10 天，仅打卡 9 格）。用户裁定
恢复为进行中，并重置起始时间为当前时刻（重新给完整 10 天倒计时）。

变更：
  1. trade_8a460d28（第3轮）：status failed→in_progress，settlement 清空，
     start_time 2026-09-25 12:00:00 → 当前时间（倒计时重起 10 天）；
  2. trade_a15edbe6（第4轮）：status in_progress→pending，start_time/end_time 清空
     （前置校验其无打卡格子，有则中止不强改）。

安全措施：写库前备份两张卡全部行 + filled slots 到 data/plan_backups/；
前置校验精确匹配事故现场，不符即中止。

用法：
  python data/_fix_r3_reactivate_20261005.py           # dry-run
  python data/_fix_r3_reactivate_20261005.py --apply   # 写库
"""
import io
import json
import os
import sys
import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import create_engine, text

R3 = 'trade_8a460d28'   # 第3轮：倒计时到期被判死，要恢复
R4 = 'trade_a15edbe6'   # 第4轮：被到期结算提前解锁，要退回 pending
R3_OLD_START = '2026-09-25 12:00:00'

APPLY = '--apply' in sys.argv
url = open(os.path.join(ROOT, 'data', 'db_url.txt')).read().strip()
engine = create_engine(url)

CARD_COLS = ('id, plan_id, sort_order, type, round, title, status, start_time, '
             'end_time, settlement, updated_at')


def fetch_card(conn, cid):
    row = conn.execute(text(
        f"SELECT {CARD_COLS} FROM plan_cards WHERE id=:cid"), {'cid': cid}).mappings().first()
    return dict(row) if row else None


def fetch_filled_slots(conn, cid):
    rows = conn.execute(text(
        "SELECT card_id, slot_index, filled_at, duration_minutes, prediction, actual, "
        "account_balance FROM plan_slots WHERE card_id=:cid AND filled=1 "
        "ORDER BY slot_index"), {'cid': cid}).mappings().all()
    return [dict(r) for r in rows]


def abort(msg):
    print(f'❌ 中止：{msg}')
    sys.exit(2)


with engine.connect() as conn:
    r3 = fetch_card(conn, R3)
    r4 = fetch_card(conn, R4)
    if not r3 or not r4:
        abort(f'卡不存在 r3={bool(r3)} r4={bool(r4)}')
    if r3['plan_id'] != r4['plan_id']:
        abort('两张卡不在同一计划，脚本假设不成立')

    print('== 当前状态 ==')
    for c in (r3, r4):
        print(f"  round={c['round']} {c['id']} status={c['status']} "
              f"start_time={c['start_time']!r} settlement={'有' if c['settlement'] else '无'}")

    # 幂等：已恢复过直接退出
    if r3['status'] == 'in_progress' and not r3['settlement'] and r4['status'] == 'pending':
        print('✅ 已是恢复后的状态，无需处理（幂等退出）')
        sys.exit(0)

    # 前置校验：必须精确匹配"到期结算现场"，防止状态漂移后误改
    if r3['status'] != 'failed' or not r3['settlement']:
        abort(f"第3轮状态非预期（status={r3['status']}, settlement={'有' if r3['settlement'] else '无'}），"
              f"疑似已被其他操作修改，请人工确认")
    if r3['start_time'] != R3_OLD_START:
        abort(f'第3轮 start_time 非预期（{r3["start_time"]!r} != {R3_OLD_START!r}），请人工确认')
    if r4['status'] != 'in_progress':
        abort(f'第4轮状态非预期（{r4["status"]}），请人工确认')

    r3_slots = fetch_filled_slots(conn, R3)
    r4_slots = fetch_filled_slots(conn, R4)
    print(f'== 打卡格子 ==\n  第3轮 filled={len(r3_slots)} 个；第4轮 filled={len(r4_slots)} 个')
    if r4_slots:
        abort(f'第4轮已有 {len(r4_slots)} 个打卡格子（索引 {[s["slot_index"] for s in r4_slots]}），'
              f'需人工决定归属，不强改')

    # 结算内容留档打印（仅记录，不参与判定）
    try:
        st = json.loads(r3['settlement']) if isinstance(r3['settlement'], str) else r3['settlement']
        print(f"== 将被清空的 settlement ==\n  status={st.get('status')} "
              f"settled_at={st.get('settled_at')} growth_pct={st.get('growth_pct')}")
    except Exception:
        pass

    # 备份
    backup_dir = os.path.join(ROOT, 'data', 'plan_backups')
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(backup_dir, f'reactivate_r3_{ts}.json')
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump({'r3_card': r3, 'r4_card': r4,
                   'r3_filled_slots': r3_slots, 'r4_filled_slots': r4_slots},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f'== 备份 ==\n  已写入 {backup_path}')

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print('== 将执行的变更 ==')
    print(f"  1. {R3}: status failed→in_progress, settlement→NULL, "
          f"start_time {R3_OLD_START!r}→{now!r}（倒计时重起 10 天）, updated_at→{now}")
    print(f"  2. {R4}: status in_progress→pending, start_time→'', end_time→'', updated_at→{now}")

    if not APPLY:
        print('\n（dry-run 未写库；确认无误后加 --apply 执行）')
        sys.exit(0)

    with engine.begin() as wconn:
        wconn.execute(text(
            "UPDATE plan_cards SET status='in_progress', settlement=NULL, "
            "start_time=:now, end_time='', updated_at=:now "
            "WHERE id=:cid AND status='failed'"), {'now': now, 'cid': R3})
        wconn.execute(text(
            "UPDATE plan_cards SET status='pending', start_time='', end_time='', updated_at=:now "
            "WHERE id=:cid AND status='in_progress'"), {'now': now, 'cid': R4})

    # 回读验证
    with engine.connect() as conn:
        v3 = fetch_card(conn, R3)
        v4 = fetch_card(conn, R4)
        n3 = len(fetch_filled_slots(conn, R3))
    ok = (v3['status'] == 'in_progress' and not v3['settlement']
          and v3['start_time'] == now and n3 == len(r3_slots)
          and v4['status'] == 'pending' and not v4['start_time'])
    print('== 回读验证 ==')
    print(f"  r3: status={v3['status']} settlement={'有' if v3['settlement'] else '无'} "
          f"start={v3['start_time']!r} filled={n3}")
    print(f"  r4: status={v4['status']} start={v4['start_time']!r}")
    print('✅ 恢复成功' if ok else '❌ 回读校验未通过，请对照备份人工检查')
    sys.exit(0 if ok else 3)
