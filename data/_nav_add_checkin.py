#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性幂等迁移：把「每日打卡」入口补进已保存在 kv_store 的导航快照。

背景（踩过两次坑的已知问题）：导航配置不是每次从代码种子 DEFAULT_ITEMS 读，
而是整份 JSON 快照存 kv_store 键 navigation_config:default。用户保存过自定义
导航后（revision>=1），批次15 新增的 link-checkin 种子项对运行页面完全无效。

修复口径（与 link-diary 迁移同款）：read_config 取当前 items+settings →
在 group-personal 组内、link-plan 之后插入 link-checkin（重复 URL 时按 order
选第一个高亮，插在 plan 前会抢「任务计划」的高亮）→ 组内 order 按新相对顺序
重排 0..n → navigation_repo.save_full 原子落库（锁内校验结构 + revision）。
严禁裸改 KV 行。

用法：
    python -B data/_nav_add_checkin.py            # 只读 dry-run，打印将做什么
    python -B data/_nav_add_checkin.py --apply    # 实际写入
幂等：快照里已有 link-checkin 时直接退出不写。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from crypto import navigation_repo as nrepo
from crypto import navigation_service as ns

ITEM_ID = 'link-checkin'
APPLY = '--apply' in sys.argv


def main():
    res = nrepo.read_config()
    if res.get('status') != 'ok':
        print(f"✗ 读取导航配置失败: {res}")
        return 1
    cfg = res['config']
    items = cfg.get('items') or []

    if res.get('source') == 'default':
        print('· 当前用的是代码种子（从未保存过自定义导航），DEFAULT_ITEMS 已含 '
              f'{ITEM_ID}，重启服务即可见，无需迁移。')
        return 0
    if any(i.get('id') == ITEM_ID for i in items):
        print(f'· 快照里已有 {ITEM_ID}（revision={cfg.get("revision")}），幂等跳过。')
        return 0

    seed = next(i for i in ns.build_default_config()['items'] if i.get('id') == ITEM_ID)
    new_item = copy.deepcopy(seed)

    # 插入位置：group-personal 组内、link-plan 之后（没有 link-plan 就组内末尾）
    same_group = [i for i in items if i.get('parent_id') == 'group-personal']
    anchor_idx = None
    for pos, i in enumerate(items):
        if i.get('id') == 'link-plan' and i.get('parent_id') == 'group-personal':
            anchor_idx = pos + 1
    if anchor_idx is None:
        if same_group:
            anchor_idx = max(items.index(i) for i in same_group) + 1
        else:
            print('✗ 快照里 group-personal 组一个子项都没有，拒绝猜测插入位置；'
                  '请到 /navigation-settings 手动添加。')
            return 1
    items.insert(anchor_idx, new_item)

    # 组内 order 按新的相对顺序重排 0..n，组外不动
    pos = 0
    for i in items:
        if i.get('parent_id') == 'group-personal':
            i['order'] = pos
            pos += 1

    payload = {'schema_version': cfg.get('schema_version') or ns.SCHEMA_VERSION,
               'items': items, 'settings': cfg.get('settings') or ns.default_settings()}

    title = new_item.get('name')
    print(f"· 将在 group-personal 组内第 {anchor_idx} 位插入「{title}」→ /checkin，"
          f"组内 {pos} 个成员 order 重排 0..{pos - 1}")
    print(f"· 落库前 revision={cfg.get('revision')}")
    if not APPLY:
        print('（dry-run，未写入；加 --apply 执行）')
        return 0

    out = nrepo.save_full(cfg.get('revision'), f'migrate-{ITEM_ID}-nav', payload)
    print(f"✓ 已写入，新 revision={out['config'].get('revision')}"
          + (f"，warnings={out.get('warnings')}" if out.get('warnings') else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
