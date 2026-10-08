#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性幂等迁移：把已保存导航快照里的「每日打卡」改名为「每日评级总控台」。

背景（同 _nav_add_checkin.py 的已知坑）：导航是整份 JSON 快照存 kv_store，
用户保存过自定义导航后（revision>=1），改代码 DEFAULT_ITEMS 的名字对运行页
面无效。日记本入口（link-diary）此前迁移已存在，本脚本只处理改名。

用法：
    python -B data/_nav_rename_checkin.py            # 只读 dry-run
    python -B data/_nav_rename_checkin.py --apply    # 实际写入
幂等：快照里 link-checkin 名字已是新名时直接退出不写。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from crypto import navigation_repo as nrepo
from crypto import navigation_service as ns

ITEM_ID = 'link-checkin'
NEW_NAME = '每日评级总控台'
APPLY = '--apply' in sys.argv


def main():
    res = nrepo.read_config()
    if res.get('status') != 'ok':
        print(f"✗ 读取导航配置失败: {res}")
        return 1
    cfg = res['config']
    items = cfg.get('items') or []

    if res.get('source') == 'default':
        print('· 当前用代码种子（从未保存自定义导航），DEFAULT_ITEMS 已是新名，'
              '重启即可见，无需迁移。')
        return 0
    target = next((i for i in items if i.get('id') == ITEM_ID), None)
    if target is None:
        print(f'✗ 快照里没有 {ITEM_ID}，请先到 /navigation-settings 添加打卡入口。')
        return 1
    old = target.get('name')
    if old == NEW_NAME:
        print(f'· {ITEM_ID} 已是「{NEW_NAME}」（revision={cfg.get("revision")}），幂等跳过。')
        return 0

    target['name'] = NEW_NAME
    payload = {'schema_version': cfg.get('schema_version') or ns.SCHEMA_VERSION,
               'items': items, 'settings': cfg.get('settings') or ns.default_settings()}
    print(f"· 将把「{old}」改名为「{NEW_NAME}」，落库前 revision={cfg.get('revision')}")
    if not APPLY:
        print('（dry-run，未写入；加 --apply 执行）')
        return 0
    out = nrepo.save_full(cfg.get('revision'), f'migrate-{ITEM_ID}-rename', payload)
    print(f"✓ 已写入，新 revision={out['config'].get('revision')}"
          + (f"，warnings={out.get('warnings')}" if out.get('warnings') else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
