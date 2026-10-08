# -*- coding: utf-8 -*-
"""把 r3 的 12 次打卡（纯时长）并入已完成的 r2，任务树整棵留在 r3。

目标（用户口径）：
  r2 = completed / 99h / 无任务树 / 无打卡关联   ← 吸收 r3 的 12 格 + 小记 + 复盘
  r3 = in_progress / 0h / 含全部任务树 / 无打卡关联  ← 清空打卡，断掉完成依据指针

与 merge_learn_card.py 的区别：不撤销 r2 结算、不搬任务树、不改任何卡状态；
打卡搬入时清空 record.task_links（时长与内容保留，任务归属解除）。
r3 树里带 completed_by_slot 的节点会被清掉该字段（其依据格子已移走），避免悬空指针。

安全性：默认干跑；--apply 前自动备份 r2/r3 整树。单事务写库，异常自动回滚。
运行期请勿同时在前端打卡。

用法（项目根目录）：
  python data/absorb_r3_slots_to_r2.py            # 干跑
  python data/absorb_r3_slots_to_r2.py --apply    # 执行
"""
import argparse
import copy
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'data'))

import merge_learn_card as mlc
from crypto import plan_routes as pr
from crypto.database import session_scope


def summarize(card):
    return 'r%s status=%s filled=%sh notes=%s review=%s字 树节点=%s 打卡关联=%s 完成依据节点=%s 结算=%s' % (
        card.get('round'), card.get('status'),
        pr._calc_total_hours(card.get('slots', [])), len(card.get('notes') or []),
        len((card.get('review') or '').strip()),
        len(list(mlc.walk_nodes(card.get('tasks') or []))),
        mlc.count_links(card), mlc.nodes_with_basis(card),
        '有' if card.get('settlement') else '无')


def do_absorb(r2, r3):
    """内存整树操作：r3 打卡(清关联)+小记+复盘 → r2；r3 清打卡与完成依据。返回统计。"""
    matrix = mlc.slot_matrix(r2)
    src_slots = sorted(mlc.filled_slots(r3),
                       key=lambda s: (s.get('filled_at') or '', s.get('slot_index') or 0))
    n_src = len(src_slots)
    n_dst = len(mlc.filled_slots(r2))
    if n_dst + n_src > 100:
        raise SystemExit('❌ r2 容量不足：%d + %d > 100 格' % (n_dst, n_src))

    new_idx = mlc.alloc_indexes(matrix, n_src)
    idx_map = {}
    moved_links = 0
    for slot, target in zip(src_slots, new_idx):
        idx_map[int(slot.get('slot_index'))] = target
        rec = copy.deepcopy(slot.get('record') or {})
        moved_links += len(rec.get('task_links') or [])
        rec['task_links'] = []          # 解除任务归属，仅留时长/内容/时间
        matrix[target] = {
            'slot_index': target, 'filled': True,
            'filled_at': slot.get('filled_at') or '', 'record': rec,
        }
    r2['slots'] = [matrix[i] for i in sorted(matrix)]

    moved_notes = copy.deepcopy(r3.get('notes') or [])
    r2['notes'] = list(r2.get('notes') or []) + moved_notes
    r3['notes'] = []

    moved_review = (r3.get('review') or '').strip()
    if moved_review:
        base = (r2.get('review') or '').strip()
        r2['review'] = (base + '\n\n———（并入第3张卡复盘）———\n\n' + moved_review) if base else moved_review
        r3['review'] = ''

    # r3：清空打卡，树原样保留但断掉指向已移走格子的完成依据
    r3['slots'] = pr._create_empty_slots(100)
    basis_cleared = 0
    for n in mlc.walk_nodes(r3.get('tasks') or []):
        if n.get('completed_by_slot') is not None:
            n.pop('completed_by_slot', None)
            basis_cleared += 1

    r2['updated_at'] = mlc._now()
    r3['updated_at'] = mlc._now()
    return {'moved': n_src, 'idx_map': idx_map, 'links_cleared': moved_links,
            'notes': len(moved_notes), 'review': bool(moved_review), 'basis_cleared': basis_cleared}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan', default='plan_learn_1000')
    ap.add_argument('--r2-round', type=int, default=2)
    ap.add_argument('--r3-round', type=int, default=3)
    ap.add_argument('--apply', action='store_true')
    opts = ap.parse_args()

    mlc._CTYPE = 'learn'
    with session_scope() as session:
        data = pr._load_plans(session)
        plan = next((p for p in data['plans'] if p['id'] == opts.plan), None)
        if not plan:
            raise SystemExit('计划不存在：%s' % opts.plan)
        r2 = mlc.pick_by_round(plan, opts.r2_round)
        r3 = mlc.pick_by_round(plan, opts.r3_round)
        if not r2 or not r3:
            raise SystemExit('找不到 round=%s/%s 的学习卡' % (opts.r2_round, opts.r3_round))

        print('=== 变更前 ===')
        print('  ' + summarize(r2))
        print('  ' + summarize(r3))

        # 护栏
        if r2.get('status') not in mlc.FINISHED:
            raise SystemExit('❌ r2 不是已完成状态（当前=%s），本工具前提是 r2 保持完成' % r2.get('status'))
        if r3.get('status') != 'in_progress':
            raise SystemExit('❌ r3 不是进行中（当前=%s）' % r3.get('status'))
        if not (r3.get('tasks') or []):
            raise SystemExit('❌ r3 没有任务树，无需保留——本工具目标就是让树留在 r3')
        if len(mlc.filled_slots(r3)) == 0:
            print('\nℹ️ r3 已无打卡可并（0 格）。只需处理残留？无需变更。')
            return

        if not opts.apply:
            trial = copy.deepcopy(plan)
            t2 = mlc.pick_by_round(trial, opts.r2_round)
            t3 = mlc.pick_by_round(trial, opts.r3_round)
            stat = do_absorb(t2, t3)
            print('\n--- 试算（未写库）---')
            print('  ' + summarize(t2))
            print('  ' + summarize(t3))
            print('  搬动打卡 %d 格（%s）/ 清空关联 %d 处 / 小记 %d 条 / 复盘%s / r3 断完成依据 %d 个' % (
                stat['moved'], ', '.join('%s→%s' % kv for kv in sorted(stat['idx_map'].items())),
                stat['links_cleared'], stat['notes'], '已并' if stat['review'] else '无',
                stat['basis_cleared']))
            print('\n（干跑）确认后加 --apply')
            return

        backup = mlc.write_backup(plan, r2, r3)
        print('\n📦 备份已写入：%s' % backup)
        stat = do_absorb(r2, r3)
        pr._save_plans(session, data)
        print('=== 变更后（已写库）===')
        print('  ' + summarize(r2))
        print('  ' + summarize(r3))
        print('  搬动打卡 %d 格 / 清空关联 %d 处 / 小记 %d 条 / r3 断完成依据 %d 个' % (
            stat['moved'], stat['links_cleared'], stat['notes'], stat['basis_cleared']))
        print('  守恒：r2 filled 应=87+%d=%d；r3 filled 应=0' % (stat['moved'], 87 + stat['moved']))
        print('\n回滚：python data/merge_learn_card.py --restore %s' % backup)


if __name__ == '__main__':
    main()
