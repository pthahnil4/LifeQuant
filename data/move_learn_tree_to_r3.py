# -*- coding: utf-8 -*-
"""把学习任务卡 r2 的残余任务树并入 r3，并清空 r2 的任务树。

背景：先执行 merge_learn_card.py --restore 回滚了合并，回到
  r2=completed/87h/其3个原始占位节点、r3=in_progress/12h/27节点树。
本脚本只做"任务树搬迁"这一件事，不动 slots / notes / review / settlement / status。
r2 的 3 个原始节点均为 done、est=0、无子节点、无打卡关联、无完成依据，
搬走后不会产生孤儿关联。

用法（项目根目录）：
  python data/move_learn_tree_to_r3.py            # 干跑：打印将搬动的根节点
  python data/move_learn_tree_to_r3.py --apply    # 执行：先备份 r2/r3 再写库
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan', default='plan_learn_1000')
    ap.add_argument('--dst-round', type=int, default=3, help='接收任务树的卡（r3）')
    ap.add_argument('--src-round', type=int, default=2, help='交出任务树的卡（r2）')
    ap.add_argument('--apply', action='store_true')
    opts = ap.parse_args()

    mlc._CTYPE = 'learn'
    with session_scope() as session:
        data, plan, dst, src = mlc.load_plan_and_cards(session, opts)
        print('目标(收树) round=%s status=%s 现有根节点=%s 总节点=%s' % (
            dst.get('round'), dst.get('status'), len(dst.get('tasks') or []),
            len(list(mlc.walk_nodes(dst.get('tasks'))))))
        print('来源(交树) round=%s status=%s 根节点=%s 总节点=%s 打卡关联=%s 完成依据节点=%s' % (
            src.get('round'), src.get('status'), len(src.get('tasks') or []),
            len(list(mlc.walk_nodes(src.get('tasks')))),
            mlc.count_links(src), mlc.nodes_with_basis(src)))
        src_titles = [n.get('title') for n in (src.get('tasks') or [])]
        print('将并入的 r%s 根节点：%s' % (src.get('round'), src_titles))

        # 安全护栏：来源卡若有任何打卡关联/完成依据指向其树，本工具不做（那需要连格子一起搬）
        if mlc.count_links(src) or mlc.nodes_with_basis(src):
            raise SystemExit('❌ 来源卡任务树仍被其打卡记录引用（关联/完成依据非 0）——'
                             '不能只搬树，请先回滚合并。')
        if dst.get('status') != 'in_progress':
            raise SystemExit('❌ 目标卡不是 in_progress，当前状态=%s，请核对' % dst.get('status'))

        if not opts.apply:
            warns = []
            trial = copy.deepcopy(plan)
            t_dst = mlc.pick_by_round(trial, opts.dst_round)
            t_src = mlc.pick_by_round(trial, opts.src_round)
            id_map = mlc.merge_task_trees(t_dst, t_src, {}, warns)
            print('--- 试算（未写库）---')
            print('并入后 r%s 根节点=%s（原 %s + 移来 %s）；r%s 根节点将清空为 %s' % (
                t_dst.get('round'), len(t_dst.get('tasks') or []),
                len(dst.get('tasks') or []), len(src_titles),
                t_src.get('round'), len(t_src.get('tasks') or [])))
            for w in warns:
                print('  ⚠️ ' + w)
            print('（干跑）确认后加 --apply')
            return

        backup = mlc.write_backup(plan, dst, src)
        print('📦 备份已写入：%s' % backup)
        warns = []
        mlc.merge_task_trees(dst, src, {}, warns)   # r2 树并入 r3，src.tasks 自动清空
        dst['updated_at'] = mlc._now()
        src['updated_at'] = mlc._now()
        pr._save_plans(session, data)
        print('✅ 已写库：r%s 现有根节点=%s；r%s 根节点=%s（应=0）' % (
            dst.get('round'), len(dst.get('tasks') or []),
            src.get('round'), len(src.get('tasks') or [])))
        for w in warns:
            print('  ⚠️ ' + w)
        print('回滚：%s' % backup)


if __name__ == '__main__':
    main()
