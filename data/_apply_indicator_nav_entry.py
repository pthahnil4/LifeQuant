#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性幂等迁移：向已落库的导航配置快照补插「指标导航」入口（行情分组下）。

背景：导航配置以整份 JSON 快照存于 kv_store 键 navigation_config:default，
navigation_service.DEFAULT_ITEMS 的增改对已保存过的快照无效（历史上已踩坑四次），
必须走 read_config → 补项 → save_full(base_revision) 原子落库。

用法（在项目根目录）：
    python data/_apply_indicator_nav_entry.py           # dry-run，只打印将做的变更
    python data/_apply_indicator_nav_entry.py --apply   # 真正写库（revision +1）
幂等：快照中已存在 builtin_key='indicator-nav' 或 url='/indicator-nav' 的项时零写入。
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

APPLY = '--apply' in sys.argv[1:]

NEW_ITEM = {
    'id': 'link-indicator-nav', 'type': 'link', 'parent_id': 'group-market',
    'name': '指标导航', 'url': '/indicator-nav',
    'icon': {'type': 'builtin', 'value': 'trend'},   # 在 _ICON_BUILTIN_KEYS 白名单内，
    'order': 2, 'visible': True, 'target': '_self',  # 且 navigation.js ICONS 已有同名键
    'builtin_key': 'indicator-nav',
}


def main():
    from crypto import navigation_repo as repo

    state = repo.read_config()
    print(f"[probe] read_config status={state['status']}"
          + (f" source={state.get('source')}" if state['status'] == 'ok' else ''))

    if state['status'] == 'db_error':
        print('数据库不可达，终止（不做任何写入）。请检查 data/db_url.txt 配置。')
        return 2
    if state['status'] == 'corrupt':
        print(f"快照损坏（fingerprint={state['fingerprint'][:12]}…），本脚本不做修复，"
              '请到导航配置页走显式修复流程。')
        return 2

    cfg = state['config']
    items = cfg.get('items', [])
    revision = int(cfg.get('revision', 0))

    # 幂等探测：入口已存在（含用户自行改名/移动过的情况）则零写入
    for it in items:
        if it.get('builtin_key') == 'indicator-nav' or it.get('url') == '/indicator-nav':
            print(f"[skip] 快照中已存在指标导航入口（id={it.get('id')} name={it.get('name')} "
                  f"parent={it.get('parent_id')} visible={it.get('visible')}），无需迁移。")
            return 0

    # 行情分组探测：快照里若连 group-market 都被删了，挂到顶层而不是硬造分组
    parent_id = 'group-market'
    if not any(it.get('id') == 'group-market' for it in items):
        parent_id = None
        print('[warn] 快照中无 group-market 分组，入口将补插为顶层链接。')

    # order 排在同父级现有最大 order 之后，不打乱用户已调好的顺序
    siblings = [it for it in items if it.get('parent_id') == parent_id]
    max_order = max([int(it.get('order', 0)) for it in siblings], default=-1)
    new_item = dict(NEW_ITEM)
    new_item['parent_id'] = parent_id
    new_item['order'] = max_order + 1
    new_item['id'] = 'link-indicator-nav' if parent_id == 'group-market' \
        else f'link-indicator-nav-{uuid.uuid4().hex[:6]}'

    print(f"[plan] 在 parent={parent_id} 下补插: {new_item}")
    if not APPLY:
        print('[dry-run] 未写库。确认无误后加 --apply 执行。')
        return 0

    mutation_id = f'add-indicator-nav-{uuid.uuid4().hex[:8]}'
    result = repo.save_full(revision, mutation_id,
                            {'settings': cfg.get('settings'), 'items': items + [new_item]})
    saved = result['config']
    print(f"[done] 已落库：revision {revision} → {saved.get('revision')}，"
          f"items={len(saved.get('items', []))}，warnings={result.get('warnings')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
