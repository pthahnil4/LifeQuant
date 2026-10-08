# -*- coding: utf-8 -*-
"""把学习计划与交易计划的 round=3 卡 start_time 统一重置为 2026-09-22。

目的：修正学习卡 r3 的罚款起算日（原 09-16 导致按缺勤计罚不合理），
并让两轨道第 3 轮都从 2026-09-22 起步（同时作为新的"10 天倒计时"起算锚点）。

护栏：仅改动 status=in_progress 且未结算的 round=3 卡；其余一律不碰。
规则向前生效，已结算历史卡不回溯。

用法（项目根目录）：
  python data/_set_r3_start.py            # 干跑：打印将改动的卡
  python data/_set_r3_start.py --apply    # 执行：备份两计划整树后写库
"""
import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto import plan_routes as pr
from crypto.database import session_scope

BACKUP_DIR = os.path.join(ROOT, 'data', 'plan_backups')
NEW_START = '2026-09-22 00:00:00'
PLANS = ('plan_learn_1000', 'plan_trade_1000')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default=NEW_START)
    ap.add_argument('--apply', action='store_true')
    opts = ap.parse_args()

    with session_scope() as session:
        data = pr._load_plans(session)
        hits = []
        for plan in data['plans']:
            if plan['id'] not in PLANS:
                continue
            for c in plan['cards']:
                if c.get('round') != 3:
                    continue
                tag = '%s r3(%s)' % (plan['id'], c.get('type'))
                if c.get('status') != 'in_progress' or c.get('settlement'):
                    print('跳过 %s：status=%s 结算=%s（非未结算 in_progress）' % (
                        tag, c.get('status'), '有' if c.get('settlement') else '无'))
                    continue
                print('将改 %s：start_time %r → %r' % (tag, c.get('start_time'), opts.start))
                hits.append((plan['id'], c['id'], c.get('start_time')))

        if not hits:
            print('没有需要改动的 r3 卡。')
            return
        if not opts.apply:
            print('\n（干跑）确认后加 --apply，共 %d 张卡待更新' % len(hits))
            return

        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        path = os.path.join(BACKUP_DIR, 'set_r3_start_%s.json' % ts)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'created_at': ts, 'plans': [p for p in data['plans'] if p['id'] in PLANS]},
                      f, ensure_ascii=False, indent=1)
        print('\n📦 整树备份：%s' % path)

        for plan in data['plans']:
            if plan['id'] not in PLANS:
                continue
            for c in plan['cards']:
                if c.get('round') == 3 and c.get('status') == 'in_progress' and not c.get('settlement'):
                    c['start_time'] = opts.start
                    c['updated_at'] = pr._now_str()
        pr._save_plans(session, data)
        print('✅ 已写库：%d 张 r3 卡 start_time → %s' % (len(hits), opts.start))
        print('回滚：用备份 %s 覆盖（可参照 merge_learn_card.py --restore 思路）' % path)


if __name__ == '__main__':
    main()
