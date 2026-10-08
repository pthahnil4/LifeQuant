#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导航数据迁移：个人管理补图标 + 补挂日记本入口
================================================
背景：导航配置以整份 JSON 快照存于 kv_store 的 navigation_config:default。
该快照在「日记本 / 个人管理图标」进入 DEFAULT_ITEMS 之前就已保存（revision>=1），
而 ensure_default_items 只补位置缺失的项、不会覆盖已有图标，故运行库两者都没有。

本脚本走 navigation_repo.save_full 的原子写入路径（锁内校验结构+revision、
提交后失效缓存），对**已持久化的那份配置**做两处幂等修正：
  1) group-personal 若无图标 → 补 builtin 'plan'（沿用旗舰子项「任务计划」图标，
     与 group-trade=task / group-market=trend 的既有惯例一致）；
  2) 若无 link-diary → 追加到 group-personal 末尾（图标 builtin 'diary'）。

已是目标状态则不写（幂等，可重复执行）。只动这两处，其余项原样保留。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crypto import navigation_repo as nav_repo        # noqa: E402
from crypto import navigation_service as ns           # noqa: E402

GROUP_PERSONAL = 'group-personal'
DIARY_ICON = 'plan'          # 个人管理分组图标（须在 _ICON_BUILTIN_KEYS 白名单内）
DIARY_ITEM = {
    'id': 'link-diary', 'type': 'link', 'parent_id': GROUP_PERSONAL,
    'name': '日记本', 'url': '/diary',
    'icon': {'type': 'builtin', 'value': 'diary'},
    'visible': True, 'target': '_self', 'builtin_key': 'diary',
}


def main():
    state = nav_repo.read_config()
    if state['status'] != 'ok':
        print(f'读取失败：{state}')
        sys.exit(1)

    cfg = state['config']
    items = [dict(i) for i in cfg['items']]          # 深拷贝一层即可（icon 为嵌套 dict，单独处理）
    source = state.get('source')
    changed = []

    # 1) 补个人管理图标（仅当当前无图标时）
    for it in items:
        if it['id'] == GROUP_PERSONAL:
            if not it.get('icon'):
                it['icon'] = {'type': 'builtin', 'value': DIARY_ICON}
                changed.append(f'{GROUP_PERSONAL} 图标 → builtin:{DIARY_ICON}')
            else:
                print(f'{GROUP_PERSONAL} 已有图标 {it["icon"]}，不覆盖')
            break
    else:
        print(f'警告：未找到 {GROUP_PERSONAL}，跳过图标修正')

    # 2) 补日记本入口（仅当不存在时，追加到个人管理末尾）
    if not any(i['id'] == DIARY_ITEM['id'] for i in items):
        gp_orders = [i.get('order', 0) for i in items if i.get('parent_id') == GROUP_PERSONAL]
        new_item = dict(DIARY_ITEM)
        new_item['order'] = (max(gp_orders) + 1) if gp_orders else 0
        items.append(new_item)
        changed.append(f"追加 {new_item['id']}（{new_item['name']}）到 {GROUP_PERSONAL} order={new_item['order']}")
    else:
        print('link-diary 已存在，不重复添加')

    if not changed:
        print(f'配置已是目标状态（source={source}, revision={cfg.get("revision")}），无需写入。')
        return

    payload = {'items': items, 'settings': cfg.get('settings') or ns.default_settings()}
    result = nav_repo.save_full(cfg.get('revision', 0), 'nav_fix_diary_icon', payload)
    saved = result['config']
    print('写入完成：')
    for c in changed:
        print('  -', c)
    print(f"  source={source} → revision={saved.get('revision')}；warnings={result.get('warnings') or '无'}")

    # 校验落库结果
    verify = nav_repo.read_config()
    vitems = {i['id']: i for i in verify['config']['items']}
    gp = vitems.get(GROUP_PERSONAL, {})
    dy = vitems.get(DIARY_ITEM['id'])
    print('复核：group-personal.icon =', gp.get('icon'))
    print('复核：link-diary =', '存在' if dy else '缺失', (dy or {}).get('icon'), '/ 父=', (dy or {}).get('parent_id'))


if __name__ == '__main__':
    main()
