# -*- coding: utf-8 -*-
"""学习任务卡「顺延合并」数据迁移：把第 3 张学习卡的打卡记录 / 小记 / 复盘 / 任务树并入第 2 张卡。

用法（项目根目录）：
  python data/merge_learn_card.py                     # 干跑：打印现状 + 合并试算，零写库
  python data/merge_learn_card.py --apply             # 执行：先落盘 JSON 备份，再单事务写库
  python data/merge_learn_card.py --verify            # 校验：重新读库，断言 filled/notes/孤儿关联
  python data/merge_learn_card.py --restore <备份文件> # 回滚：按备份整卡覆盖

字段口径（crypto/models.py: PlanCard / PlanSlot）：
  打卡内容 → plan_slots（主键 card_id + slot_index；record 拆列 content/duration_minutes/task_links/filled_at）
  小结     → plan_cards.notes（[{time,content}]）+ plan_cards.review（复盘总结文本）
  任务树   → plan_cards.tasks（JSON 整树；打卡靠 task_links.task_id 指向【本卡】节点）
  任务目标 → plan_cards.goal —— 本脚本不动（两张卡都不动）

为什么必须整树读写、不能只改 card_id（四个硬约束）：
  ① slot_index 是卡内主键：直接搬会与目标卡已有的 100 个格子撞 PK，必须重新编号；
  ② task_id 只在本卡任务树内解析：格子搬到卡2 后若不搬树，前端标签变「已删除任务」
     （task_plan.js:1042），任务进度与实际投入归因静默丢失；
  ③ 节点 completed_by_slot 记录「完成依据是哪一格」：格子重编号后不同步改，
     「已完成任务不能继续打卡」判定（_validate_task_links）与删打卡回退
     （_revert_slot_task_links）会指错格子、逻辑失效；
  ④ plan_repo.save_plans_data 是应用自身的差量写入路径（外键顺序 + 只写变化行），
     复用它比裸 SQL 更不容易把整树写歪。

安全性：默认干跑；--apply 前自动备份两张卡的全量数据；整个变更在一个 session_scope
事务内完成（异常自动 rollback，不会留下半合并状态）。运行期请勿同时在前端打卡。
"""

import argparse
import copy
import datetime
import json
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError, ValueError):
        pass

from crypto import plan_routes as pr            # noqa: E402  复用应用自身语义
from crypto.database import session_scope       # noqa: E402

BACKUP_DIR = os.path.join(ROOT, 'data', 'plan_backups')
FINISHED = ('completed', 'failed', 'abandoned')

# 卡片类型：'learn' / 'trade'。main() 里按 --card-type 覆盖；合并核心逻辑对本值无感，
# 只有"筛选同一类型的卡 / 校验定位卡"这两处依赖它。
_CTYPE = 'learn'


# =============================================================================
# 小工具
# =============================================================================

def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def learn_cards(plan):
    return sorted([c for c in plan.get('cards', []) if c.get('type') == _CTYPE],
                  key=lambda x: x.get('round', 0))


def pick_by_round(plan, round_no):
    for c in learn_cards(plan):
        if c.get('round') == round_no:
            return c
    return None


def filled_slots(card):
    return [s for s in card.get('slots', []) if s.get('filled')]


def slot_matrix(card):
    """{index: slot}，缺失下标补空格子，保证恒为 0..99 共 100 项"""
    m = {}
    for s in card.get('slots', []):
        idx = s.get('slot_index')
        if idx is not None:
            m[int(idx)] = s
    empty = pr._create_empty_slots(100)
    for i in range(100):
        m.setdefault(i, empty[i])
    return m


def alloc_indexes(matrix, count):
    """按用户规则分配新格子号：从「目标卡已用的最大 slot_index + 1」起升序取空格子；
    尾部不足再回头补前面的空洞。返回 None 表示放不下。
    """
    used = [i for i, s in matrix.items() if s.get('filled')]
    start = (max(used) + 1) if used else 0
    picked = [i for i in range(start, 100) if not matrix[i].get('filled')]
    if len(picked) < count:
        picked += [i for i in range(0, start) if not matrix[i].get('filled')]
    picked = sorted(set(picked))[:count]
    return picked if len(picked) == count else None


def walk_nodes(nodes):
    for n in nodes or []:
        yield n
        yield from walk_nodes(n.get('children'))


def count_links(card):
    return sum(len((s.get('record') or {}).get('task_links') or []) for s in filled_slots(card))


def nodes_with_basis(card):
    """任务树里带「完成依据格子」的节点数（这类节点必须跟着打卡一起搬）"""
    return sum(1 for n in walk_nodes(card.get('tasks') or []) if n.get('completed_by_slot') is not None)


def inspect_card(plan, card, tag, limit=100):
    """只读明细审计：逐格打卡 + 逐条小记，用于合并前后对账"""
    print(f'  [{tag}] round={card.get("round")} 「{card.get("title")}」 状态={card.get("status")}')
    print(f'      settlement={json.dumps(card.get("settlement"), ensure_ascii=False)}')
    used = sorted(int(s.get("slot_index")) for s in filled_slots(card))
    free = [i for i in range(100) if i not in set(used)]
    print(f'      已用格子 {len(used)} 个：'
          + (f'{used[0]}..{used[-1]}（下一个可用起点={used[-1] + 1}）' if used else '无')
          + f'；空格子 {len(free)} 个：{free[:12]}' + ('…' if len(free) > 12 else ''))
    for s in sorted(filled_slots(card), key=lambda x: (x.get('filled_at') or '', x.get('slot_index') or 0))[:limit]:
        rec = s.get('record') or {}
        content = str(rec.get('content') or '').replace('\n', ' ')
        print(f'        #{s.get("slot_index"):<3} {s.get("filled_at")}  {rec.get("duration_minutes")}分钟  '
              f'关联{len(rec.get("task_links") or [])}  {content[:34]}')
    notes = card.get('notes') or []
    print(f'      小记 {len(notes)} 条：')
    for n in notes[:limit]:
        print(f'        {n.get("time")}  {str(n.get("content") or "").replace(chr(10), " ")[:60]}')
    print(f'      任务树：')
    for n in walk_nodes(card.get('tasks') or []):
        print(f'        {n.get("id")}  [{n.get("status")}]  预估={n.get("estimated_minutes")}  '
              f'完成依据格子={n.get("completed_by_slot")}  {n.get("title")}')


def print_chain(plan, tag):
    """串行链快照：各学习卡 round/状态/格数（合并前后对照，防串行解锁被写歪）"""
    print(f'  [{tag}] ' + ' | '.join(
        f'r{c.get("round")} {c.get("status")} {pr._calc_total_hours(c.get("slots", []))}h'
        for c in learn_cards(plan)))


def describe(plan, card, tag):
    ri = pr._calc_card_reward(plan, card)
    tp = pr._calc_task_progress(card)
    return (f'  {tag} round={card.get("round")}  「{card.get("title")}」 id={card["id"]}\n'
            f'      状态={card.get("status")}  已结算={"是" if card.get("settlement") else "否"}  '
            f'打卡={ri["filled_count"]}/100h  在场={ri["presence_days"]}天  预计奖励={ri["final_reward"]}\n'
            f'      小记={len(card.get("notes") or [])}条  复盘={len((card.get("review") or "").strip())}字  '
            f'任务节点={len(list(walk_nodes(card.get("tasks") or [])))}个  打卡关联={count_links(card)}处  '
            f'任务进度={tp["done_count"]}/{tp["total_count"]}({tp["pct"]}%) 待补预估={tp["pending_estimate"]}\n'
            f'      目标={card.get("goal")}')


# =============================================================================
# 任务树搬迁（含 task_id 重映射 + completed_by_slot 重编号）
# =============================================================================

def merge_task_trees(dst, src, idx_map, warns):
    """把来源卡任务树整体挂到目标卡根层；返回 {旧task_id: 新task_id}"""
    dst_tree = dst.get('tasks') or []
    src_tree = src.get('tasks') or []
    if not src_tree:
        return {}
    dst_ids = {n.get('id') for n in walk_nodes(dst_tree)}
    dst_titles = {(n.get('title') or '').strip() for n in walk_nodes(dst_tree)}
    id_map = {}
    lost_done_basis = []

    def _clone(node):
        new = copy.deepcopy(node)
        old_id = new.get('id') or ''
        new_id = old_id
        if not new_id or new_id in dst_ids:              # ID 撞车 / 缺 ID → 换发新 ID
            new_id = 'task_' + uuid.uuid4().hex[:10]
        new['id'] = new_id
        dst_ids.add(new_id)
        id_map[old_id] = new_id
        cbs = new.get('completed_by_slot')
        if new.get('status') == 'done' and cbs is not None:
            if int(cbs) in idx_map:                      # 完成依据格子已搬到目标卡 → 跟着重编号
                new['completed_by_slot'] = idx_map[int(cbs)]
            else:                                        # 依据格子没搬 → 清掉，避免指向空格子
                new.pop('completed_by_slot', None)
                lost_done_basis.append(new.get('title') or new_id)
        new['children'] = [_clone(ch) for ch in (new.get('children') or [])]
        return new

    for root in src_tree:
        title = (root.get('title') or '').strip()
        if title and title in dst_titles:
            warns.append(f'目标卡已有同名任务「{title}」：脚本未自动去重，请到「🌳 任务」Tab 手工确认'
                         f'（在页面删掉多余一侧，关联的打卡记录会保留并显示「已删除任务」）')
        dst_tree.append(_clone(root))
    dst['tasks'] = dst_tree
    src['tasks'] = []
    pr._rollup_tasks(dst['tasks'])                      # 容器节点预估/状态重新上卷
    if lost_done_basis:
        warns.append('以下已完成任务的「完成依据格子」未搬入目标卡，已清除 completed_by_slot：'
                     + '、'.join(lost_done_basis))
    return id_map


def rewrite_links(card, id_map):
    """把搬进来的打卡记录里的 task_id 换成目标卡树中的新 ID"""
    if not id_map:
        return 0
    hit = 0
    for s in filled_slots(card):
        for link in (s.get('record') or {}).get('task_links') or []:
            old = link.get('task_id')
            if old in id_map and id_map[old] != old:
                link['task_id'] = id_map[old]
                hit += 1
    return hit


# =============================================================================
# 合并主体（纯内存操作，由调用方负责落库/回滚）
# =============================================================================

def merge(plan, dst, src, opts, warns):
    """返回统计 dict；告警追加进 warns。只在内存整树上操作，由调用方负责落库/回滚"""
    matrix = slot_matrix(dst)
    src_slots = sorted(filled_slots(src), key=lambda s: (s.get('filled_at') or '', s.get('slot_index') or 0))
    n_src = len(src_slots)
    n_dst = len(filled_slots(dst))

    if n_dst + n_src > 100:
        raise SystemExit(f'❌ 容量不足：目标卡已用 {n_dst} 格，来源卡 {n_src} 格，合计 > 100 格。'
                         f'请减少来源卡记录，或把多出的部分顺延到再下一张卡。')

    # 撤销来源卡的历史结算（空壳不该再持有奖金凭证），并如实告知
    if src.get('settlement'):
        st = src['settlement']
        warns.append(f'来源卡原有结算记录（final_reward={st.get("final_reward")} 元，'
                     f'settled_at={st.get("settled_at")}）已清除——它已不再是一张完成卡；'
                     f'看板「已获奖金」会相应减少')
        src['settlement'] = None

    # 目标卡已结算：合并会让 settlement 与真实格数不一致（看板已获奖金 = Σ settlement）
    if dst.get('settlement') or dst.get('status') in FINISHED:
        if not opts.revert_settle:
            raise SystemExit('❌ 目标卡已结束/已结算，直接合并会让结算数据与实际格数不一致。\n'
                             '   确认要撤销该卡结算（回到进行中）请加 --revert-settle')
        warns.append(f'目标卡原结算已撤销（原 final_reward='
                     f'{(dst.get("settlement") or {}).get("final_reward")} 元），状态回到进行中')
        dst['settlement'] = None
        dst['status'] = 'pending'   # 必须复位，否则 _recompute_serial_chain 仍视其为已结束、把当前卡给空的来源卡

    # ① 打卡格子：重编号后写入目标卡
    new_idx = alloc_indexes(matrix, n_src)
    if new_idx is None:
        raise SystemExit('❌ 目标卡空格子分配失败（理论不该发生，请检查该卡 slots 数据）')
    idx_map = {}
    for slot, target in zip(src_slots, new_idx):
        old = int(slot.get('slot_index'))
        idx_map[old] = target
        matrix[target] = {
            'slot_index': target,
            'filled': True,
            'filled_at': slot.get('filled_at') or '',
            'record': copy.deepcopy(slot.get('record')),
        }
    dst['slots'] = [matrix[i] for i in sorted(matrix)]

    # ② 任务树：只在「打卡关联真的存在」时才整棵搬过去（否则会把两张卡的目标混在一张卡里，
    #    污染目标卡的任务进度分母与提前通关判定）；搬完重映射 task_links 的节点 ID
    src_links, src_basis = count_links(src), nodes_with_basis(src)
    mode = opts.move_tasks
    if mode == 'no' and (src_links or src_basis):
        raise SystemExit(f'❌ 来源卡有 {src_links} 处打卡关联 / {src_basis} 个带完成依据的任务节点，'
                         f'--move-tasks no 会把它们变成孤儿关联（前端显示「已删除任务」）。'
                         f'请改用 --move-tasks auto 或直接合并任务树')
    move_tree = (mode == 'yes') or (mode == 'auto' and bool(src_links or src_basis))
    id_map = merge_task_trees(dst, src, idx_map, warns) if move_tree else {}
    if move_tree:
        rewrite_links(dst, id_map)
    elif src.get('tasks'):
        src_tasks = src.get('tasks')
        warns.append('来源卡任务树未搬移（该卡没有任何打卡关联），原样留在来源卡：'
                     + ' / '.join(str(n.get('title')) for n in walk_nodes(src_tasks)))

    # ③ 小记：直接追加到目标卡 notes 末尾（用户规则，不重排）
    moved_notes = copy.deepcopy(src.get('notes') or [])
    dst['notes'] = list(dst.get('notes') or []) + moved_notes
    src['notes'] = []

    # ④ 复盘小结：有内容才并（两张卡都要留档时按来源标注拼接）
    moved_review = (src.get('review') or '').strip()
    if moved_review:
        base = (dst.get('review') or '').strip()
        dst['review'] = (base + '\n\n———（并入第' + str(src.get('round')) + '张卡复盘）———\n\n'
                         + moved_review) if base else moved_review
        src['review'] = ''

    # ⑤ 来源卡清空留壳：id/round/title/goal/reward 全保留（用户规则：目标不动、可只读查看）
    src['slots'] = pr._create_empty_slots(100)
    if move_tree:
        src['milestones'] = []      # 旧归档列：任务树已并入目标卡，一并清掉避免重复展示
        src['todos'] = []
    src['start_time'] = ''
    src['end_time'] = ''
    src['status'] = 'pending'

    # ⑥ 串行链归位：第一张未结束卡 = 进行中，其余锁定
    pr._recompute_serial_chain(plan)
    if dst.get('status') != 'in_progress':
        active = next((c for c in learn_cards(plan) if c.get('status') == 'in_progress'), None)
        warns.append(f'串行链重算后目标卡状态为 {dst.get("status")}（不是 in_progress）—— 当前可打卡的卡是 '
                     + (f'round={active.get("round")}「{active.get("title")}」' if active else '（无）')
                     + '，请先处理那张卡')

    # ⑦ 满 100 格时按需显式结算（直接写库不会触发 fill-slot 路径的自动通关）
    settled = False
    if opts.settle_if_full and pr._calc_total_hours(dst['slots']) >= 100 \
            and dst.get('status') == 'in_progress':
        pr._settle_card(plan, dst)
        settled = True
        pr._recompute_serial_chain(plan)

    dst['updated_at'] = _now()
    src['updated_at'] = _now()
    return {
        'moved': n_src, 'idx_map': idx_map, 'dst_before': n_dst,
        'notes': len(moved_notes), 'review': bool(moved_review),
        'task_nodes': len(id_map), 'tree_moved': move_tree, 'settled': settled,
    }


# =============================================================================
# 校验
# =============================================================================

def verify(plan, dst_round, src_round, expect_dst, expect_src):
    """读库后的完整性校验：格数、格子编号唯一、孤儿 task_links、完成依据指向"""
    fails, info = [], {}
    dst, src = pick_by_round(plan, dst_round), pick_by_round(plan, src_round)
    if not dst or not src:
        return ['找不到指定 round 的学习卡'], info
    for tag, card in (('dst', dst), ('src', src)):
        idxs = [s.get('slot_index') for s in card.get('slots', [])]
        if len(idxs) != len(set(idxs)):
            fails.append(f'{tag}(round={card.get("round")}) slot_index 存在重复')
        if len(idxs) != 100:
            fails.append(f'{tag}(round={card.get("round")}) 格子数为 {len(idxs)}，应为 100')
    d_n, s_n = pr._calc_total_hours(dst.get('slots', [])), pr._calc_total_hours(src.get('slots', []))
    info['dst_filled'], info['src_filled'] = d_n, s_n
    info['dst_notes'], info['src_notes'] = len(dst.get('notes') or []), len(src.get('notes') or [])
    if expect_dst is not None and d_n != expect_dst:
        fails.append(f'目标卡 filled_count={d_n}，期望 {expect_dst}')
    if expect_src is not None and s_n != expect_src:
        fails.append(f'来源卡 filled_count={s_n}，期望 {expect_src}')

    tree_ids = {n.get('id') for n in walk_nodes(dst.get('tasks') or [])}
    dst_by_idx = {int(s.get('slot_index')): s for s in (dst.get('slots') or [])
                  if s.get('slot_index') is not None}
    orphans = []
    for s in filled_slots(dst):
        for link in (s.get('record') or {}).get('task_links') or []:
            if link.get('task_id') not in tree_ids:
                orphans.append((s.get('slot_index'), link.get('task_id')))
    if orphans:
        fails.append(f'目标卡存在 {len(orphans)} 处孤儿任务关联（前端会显示「已删除任务」）：'
                     + '；'.join(f'格子{a}→{b}' for a, b in orphans[:5]))
    for n in walk_nodes(dst.get('tasks') or []):
        cbs = n.get('completed_by_slot')
        if n.get('status') != 'done' or cbs is None:
            continue
        slot = dst_by_idx.get(int(cbs))
        if slot is None or not slot.get('filled'):
            fails.append(f'任务「{n.get("title")}」标记完成，但完成依据格子 {cbs} 在目标卡里不是已勾选格子')
    src_links_left = count_links(src)
    if src_links_left:
        fails.append(f'来源卡仍残留 {src_links_left} 处打卡关联（打卡没搬干净）')
    info['src_tree_nodes'] = len(list(walk_nodes(src.get('tasks') or [])))
    info['dst_tree_nodes'] = len(list(walk_nodes(dst.get('tasks') or [])))
    return fails, info


# =============================================================================
# 入口
# =============================================================================

def load_plan_and_cards(session, opts):
    """读取整树 + 定位两张卡；找不到就退出（只读，不写库）"""
    data = pr._load_plans(session)
    plan = next((p for p in data.get('plans', []) if p['id'] == opts.plan), None)
    if not plan:
        raise SystemExit(f'计划不存在：{opts.plan}（现有：' +
                         '、'.join(p['id'] for p in data.get('plans', [])) + '）')
    dst, src = pick_by_round(plan, opts.dst_round), pick_by_round(plan, opts.src_round)
    if not dst or not src:
        raise SystemExit(f'找不到 round={opts.dst_round}/{opts.src_round} 的学习卡；现有 round='
                         + ','.join(str(c.get('round')) for c in learn_cards(plan)))
    if dst['id'] == src['id']:
        raise SystemExit('来源卡与目标卡是同一张')
    return data, plan, dst, src


def write_backup(plan, dst, src):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f'merge_learn_card_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
    payload = {'plan_id': plan['id'], 'created_at': _now(),
               'rounds': {dst['id']: dst.get('round'), src['id']: src.get('round')},
               'cards': {dst['id']: dst, src['id']: src}}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return path


def do_restore(path):
    with open(path, encoding='utf-8') as f:
        snap = json.load(f)
    with session_scope() as session:
        data = pr._load_plans(session)
        plan = next((p for p in data.get('plans', []) if p['id'] == snap['plan_id']), None)
        if not plan:
            raise SystemExit(f'备份中的计划不存在：{snap["plan_id"]}')
        hit = 0
        for i, card in enumerate(plan['cards']):
            saved = (snap.get('cards') or {}).get(card['id'])
            if saved is not None:
                plan['cards'][i] = copy.deepcopy(saved)
                hit += 1
        pr._recompute_serial_chain(plan)
        pr._save_plans(session, data)
    print(f'✅ 已按备份回滚 {hit} 张卡（备份时间 {snap.get("created_at")}），刷新 /plan 页面即生效')


def main():
    global _CTYPE
    ap = argparse.ArgumentParser(description='任务卡顺延合并（默认干跑，不写库）')
    ap.add_argument('--card-type', choices=('learn', 'trade'), default='learn',
                    help='合并哪条轨道的卡：learn=学习计划 / trade=交易计划')
    ap.add_argument('--plan', default=None,
                    help='计划 ID（缺省按 card-type 自动选 plan_learn_1000 / plan_trade_1000）')
    ap.add_argument('--dst-round', type=int, default=2, help='目标卡 round（并入这张），默认 2')
    ap.add_argument('--src-round', type=int, default=3, help='来源卡 round（搬空这张），默认 3')
    ap.add_argument('--apply', action='store_true', help='真正写库（缺省仅干跑）')
    ap.add_argument('--verify', action='store_true', help='只做校验，不改数据')
    ap.add_argument('--expect-dst', type=int, default=None, help='校验用：目标卡期望 filled_count')
    ap.add_argument('--expect-src', type=int, default=None, help='校验用：来源卡期望 filled_count')
    ap.add_argument('--revert-settle', action='store_true', help='目标卡已结算时允许撤销结算')
    ap.add_argument('--settle-if-full', action='store_true', help='合并后满 100 格则立即结算通关')
    ap.add_argument('--move-tasks', choices=('auto', 'yes', 'no'), default='auto',
                    help='来源卡任务树：auto=仅当存在打卡关联/完成依据时才并入（默认）；yes=强制并入；no=强制保留')
    ap.add_argument('--inspect', action='store_true', help='只读打印两张卡的逐格打卡与逐条小记（对账用）')
    ap.add_argument('--restore', default='', help='用指定备份 JSON 整卡回滚')
    opts = ap.parse_args()

    _CTYPE = opts.card_type
    if opts.plan is None:
        opts.plan = 'plan_learn_1000' if opts.card_type == 'learn' else 'plan_trade_1000'

    if opts.restore:
        do_restore(opts.restore)
        return

    if opts.verify:
        with session_scope() as session:
            _data, plan, dst, src = load_plan_and_cards(session, opts)
        fails, info = verify(plan, opts.dst_round, opts.src_round, opts.expect_dst, opts.expect_src)
        print(describe(plan, dst, '目标'))
        print(describe(plan, src, '来源'))
        print(f'  实测：目标卡打卡={info.get("dst_filled")} 小记={info.get("dst_notes")} 任务节点={info.get("dst_tree_nodes")}；'
              f'来源卡打卡={info.get("src_filled")} 小记={info.get("src_notes")} 任务节点={info.get("src_tree_nodes")}')
        if fails:
            print('\n❌ 校验未通过：')
            for x in fails:
                print('   -', x)
            sys.exit(1)
        print('\n✅ 校验通过：格数守恒、格子编号唯一、无孤儿任务关联、来源卡已清空')
        return

    with session_scope() as session:
        data, plan, dst, src = load_plan_and_cards(session, opts)
        dst_before, src_before = pr._calc_total_hours(dst.get('slots', [])), pr._calc_total_hours(src.get('slots', []))
        print('=== 合并前 ===')
        print(describe(plan, dst, f'目标(round={opts.dst_round})'))
        print(describe(plan, src, f'来源(round={opts.src_round})'))
        print_chain(plan, '链·前')
        if opts.inspect:
            print('\n=== 只读明细对账 ===')
            inspect_card(plan, dst, '目标')
            inspect_card(plan, src, '来源')
            return
        if src_before == 0 and not (src.get('notes') or []) and not (src.get('review') or '').strip():
            print('\nℹ️ 来源卡没有打卡、也没有小记/复盘，无需合并。')
            return
        if not opts.apply:
            trial = copy.deepcopy(plan)
            t_dst, t_src = pick_by_round(trial, opts.dst_round), pick_by_round(trial, opts.src_round)
            warns = []
            try:
                stat = merge(trial, t_dst, t_src, opts, warns)
            except SystemExit as e:
                print('\n--- 试算受阻（干跑不写库）---')
                print(str(e).strip())
                return
            print(f'\n--- 试算（未写库）---\n{describe(trial, t_dst, "目标")}\n{describe(trial, t_src, "来源")}')
            print(f'  将搬动：打卡 {stat["moved"]} 格（'
                  + ', '.join(f'{a}→{b}' for a, b in sorted(stat['idx_map'].items())[:23])
                  + ('…' if stat['moved'] > 23 else '') + '）')
            print(f'  任务树：{"并入目标卡" if stat["tree_moved"] else "保留在来源卡（无打卡关联）"}'
                  f'（映射节点 {stat["task_nodes"]} 个）/ 小记 {stat["notes"]} 条 / 复盘'
                  f'{"已并" if stat["review"] else "无"}'
                  f'{" / 满100格并结算" if stat["settled"] else ""}')
            for w in warns:
                print('  ⚠️ ' + w)
            print_chain(plan, '链·前')
            print_chain(trial, '链·后')
            print('  守恒检查：合并前 ' + str(dst_before + src_before) + ' 格 → 合并后 '
                  + str(pr._calc_total_hours(t_dst.get('slots', [])) + pr._calc_total_hours(t_src.get('slots', []))) + ' 格')
            print('\n（干跑结束）确认无误后加 --apply')
            return

        backup = write_backup(plan, dst, src)
        print(f'\n📦 备份已写入：{backup}')
        warns = []
        stat = merge(plan, dst, src, opts, warns)
        pr._save_plans(session, data)   # 单事务：异常时 session_scope 自动 rollback

        print('=== 合并后（已写库）===')
        print(describe(plan, dst, f'目标(round={opts.dst_round})'))
        print(describe(plan, src, f'来源(round={opts.src_round})'))
        print(f'  实际搬动打卡 {stat["moved"]} 格 / 任务树'
              f'{"已并入" if stat["tree_moved"] else "保留在来源卡"} / '
              f'小记 {stat["notes"]} 条 / 复盘{"已并" if stat["review"] else "无"}')
        print(f'  格子编号映射：' + ', '.join(f'{a}→{b}' for a, b in sorted(stat['idx_map'].items())))
        print(f'  守恒检查：{dst_before}+{src_before} = {dst_before + src_before} → 现 '
              f'{pr._calc_total_hours(dst.get("slots", []))} + {pr._calc_total_hours(src.get("slots", []))} = '
              f'{pr._calc_total_hours(dst.get("slots", [])) + pr._calc_total_hours(src.get("slots", []))}')
        for w in warns:
            print('  ⚠️ ' + w)
        print_chain(plan, '链·后')
        print(f'\n回滚命令：python data/merge_learn_card.py --restore {backup}')
    print('提示：应用每请求重读整树（无进程内缓存），刷新 /plan 页面即生效，无需重启')


if __name__ == '__main__':
    main()
