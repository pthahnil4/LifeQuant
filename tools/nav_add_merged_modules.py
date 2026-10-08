# -*- coding: utf-8 -*-
"""
把合并进来的三个市场模块加进用户已保存的导航配置
================================================
背景（Phase 6 导航统一遗留的最后一步）：

宿主导航是**数据驱动**的，权威配置存在 MySQL 的 KV 表里。用户线上那份是
`source='db'` 的自定义配置（revision 6），里面没有 `/futures/`、`/stocks/`、
`/kline/` 这三项——`navigation_service.build_default_config()` 里新增的条目
**只会出现在「恢复默认」或全新安装**上，不会自动合并进已保存的配置。
所以模块虽然已经挂在 7777 上，用户的导航里却点不到。

本脚本做的事（幂等）：
1. 读当前权威配置；`source='default'`（从未保存过）时无需改动，直接退出。
2. 找到「多市场」分组 `group-market-multi`，没有就按默认配置的定义创建。
3. 逐个补齐四个入口（期货监控台 / 期货星标行情 / 股票监控台 / K线训练台），
   已存在同 URL 的条目就跳过，绝不重复添加、也不动用户已有的任何条目与排序。
4. 默认**只做 dry-run** 打印将要新增的条目；加 `--apply` 才真正落库
   （走 `repo.save_full()`，带 base_revision 乐观锁，冲突就中止不硬写）。

安全边界：只写 `navigation_config` 这一个 KV 键；不碰交易配置、不碰行情、
不发起任何 OKX 请求。回滚办法：管理页「恢复默认」，或用 dry-run 输出的
base_revision 之前的 revision 反写（脚本会打印新/旧 revision）。

用法：
    python -X utf8 tools/nav_add_merged_modules.py           # 看差异
    python -X utf8 tools/nav_add_merged_modules.py --apply    # 真正写入
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 导航读写只碰 KV 表，不该顺带拉起交易调度器与告警监控
os.environ.setdefault('CRYPTO_NO_BACKGROUND', '1')

from crypto import navigation_service as ns      # noqa: E402
from crypto import navigation_repo as repo       # noqa: E402

GROUP_ID = 'group-market-multi'


def _defaults_for(keys):
    """从内存默认配置里取这几条 builtin_key 的节点定义（口径与全站默认一致）。"""
    items = ns.build_default_config()['items']
    by_key = {n.get('builtin_key'): n for n in items if n.get('builtin_key')}
    out = []
    for k in keys:
        node = by_key.get(k)
        if not node:
            raise SystemExit(f'[FATAL] 默认配置里找不到 builtin_key={k}，'
                             f'说明 navigation_service 的清单变了，需人工核对')
        out.append(json.loads(json.dumps(node, ensure_ascii=False)))
    return out


def _existing_urls(items):
    return {str(n.get('url') or '') for n in items}


def _existing_ids(items):
    return {str(n.get('id') or '') for n in items}


def build_patch(config):
    """返回 (new_items, added, notes)；added 为空表示无需写入。"""
    items = [json.loads(json.dumps(n, ensure_ascii=False)) for n in config['items']]
    have_url = _existing_urls(items)
    have_id = _existing_ids(items)
    notes = []

    group = next((n for n in items if n.get('id') == GROUP_ID), None)
    if group is None:
        default_group = next((n for n in ns.build_default_config()['items']
                              if n.get('id') == GROUP_ID), None)
        if default_group is None:
            raise SystemExit('[FATAL] 默认配置里没有 group-market-multi 分组')
        group = json.loads(json.dumps(default_group, ensure_ascii=False))
        items.append(group)
        notes.append(f'补建「{group.get("name")}」分组（用户配置里原本没有）')

    wanted = _defaults_for(('futures', 'futures-star-market', 'stocks', 'kline'))
    # 新条目排在分组内已有条目之后，避免插队打乱用户习惯
    base_order = max([int(n.get('order') or 0) for n in items
                      if n.get('parent_id') == GROUP_ID] or [-1]) + 1

    added = []
    for offset, node in enumerate(wanted):
        if node['url'] in have_url:
            notes.append(f'跳过 {node["url"]}：已有同 URL 条目')
            continue
        if node['id'] in have_id:
            notes.append(f'跳过 {node["url"]}：id {node["id"]} 已被占用，'
                         f'需在导航管理页手工处理')
            continue
        node['parent_id'] = GROUP_ID
        node['order'] = base_order + offset
        node['visible'] = True
        items.append(node)
        added.append(node)

    return items, added, notes


def main():
    apply_change = '--apply' in sys.argv[1:]

    r = repo.read_config()
    if r['status'] != 'ok':
        print(f'[ABORT] 读不到权威配置：{r}')
        print('        DB 不可达时不要写入（否则等于把用户配置覆盖成默认）。')
        return 2
    if r['source'] == 'default':
        print('[SKIP] 当前是内存默认配置（从未保存过），默认里本就含这三项，无需写入。')
        return 0

    cfg = r['config']
    old_rev = int(cfg.get('revision', 0))
    print(f'当前权威配置：source=db revision={old_rev}，条目 {len(cfg["items"])} 个')

    new_items, added, notes = build_patch(cfg)
    for n in notes:
        print(f'  [note] {n}')
    if not added:
        print('[SKIP] 三个模块入口都已存在，无需改动。')
        return 0

    print(f'将新增 {len(added)} 条：')
    for n in added:
        print(f'  + {n["name"]:<10} {n["url"]}  (分组={GROUP_ID}, order={n["order"]})')

    if not apply_change:
        print('\n[dry-run] 未写入。确认无误后执行：'
              'python -X utf8 tools/nav_add_merged_modules.py --apply')
        return 0

    payload = {'items': new_items, 'settings': cfg.get('settings') or ns.default_settings()}
    try:
        saved = repo.save_full(old_rev, f'nav-add-merged-{old_rev + 1}', payload)
    except repo.RevisionConflict as e:
        print(f'[ABORT] revision 冲突（当前 {e}），说明期间有人在管理页改过导航，'
              f'请重新 dry-run 后再写入。')
        return 1
    except Exception as e:
        print(f'[ABORT] 写入失败：{e}')
        return 1

    new_rev = saved.get('config', {}).get('revision')
    print(f'[OK] 已写入：revision {old_rev} → {new_rev}')
    print('     导航改动即时生效（GET /api/navigation/config 的 ETag 会变），'
          '无需重启 7777。')
    print(f'     回滚：在导航管理页「恢复默认」，或用 revision={old_rev} 之前的备份反写。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
