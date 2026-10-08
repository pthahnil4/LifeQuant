# -*- coding: utf-8 -*-
"""一次性清除 analysis_reminder_log 里「现行口径不该存在」的缺档行。

巡检主循环已经内置同样的清理（analysis_discipline._purge_unaccountable），
本脚本只是给「不方便重启服务、或重启前先把看板擦干净」留的手动入口。

⚠ 若还有一个跑着旧代码的 app.py 在跑，别用这个脚本删：它每 5 分钟会把
凌晨槽重新判成缺档（旧 parse_active_hours 不认 24:00 → 等于全天追责），
新行 notified=False 会再触发一封断档汇总信。先重启再清。

判定与巡检共用 disc.is_slot_active / disc.epoch_dt，不会出现两套口径。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select
from crypto.database import session_scope
from crypto.models import AnalysisReminderLog
from crypto import discipline_repo as disc

with session_scope() as s:
    rows = s.execute(select(AnalysisReminderLog)
                     .order_by(AnalysisReminderLog.hour_slot.asc())).scalars().all()
    cfg = disc.load_config()
    ep = disc.epoch_dt()
    print(f'active_hours={cfg.get("active_hours")!r} required_count='
          f'{cfg.get("required_count")} epoch={ep}；台账 {len(rows)} 行')
    doomed, keep = [], []
    for r in rows:
        # 与 _notify_gaps 同口径：不在生效时段、或早于生效起点 → 无效判定
        accountable = disc.is_slot_active(r.hour_slot, cfg)
        if accountable and ep is not None:
            try:
                accountable = disc.slot_start(r.hour_slot) >= ep
            except ValueError:
                accountable = True
        (keep if accountable else doomed).append(r)
    print(f'待删 {len(doomed)} 行 / 保留 {len(keep)} 行')
    for r in doomed:
        print(f'  DEL {r.hour_slot} req={r.required_count} act={r.actual_count} '
              f'{r.status} created={r.created_at}')
        s.delete(r)
    for r in keep:
        print(f'  KEEP {r.hour_slot} req={r.required_count} act={r.actual_count} '
              f'{r.status} notified={int(bool(r.notified))}')
    s.flush()

with session_scope() as s:
    left = s.execute(select(AnalysisReminderLog.hour_slot)).scalars().all()
    print(f'AFTER 台账剩 {len(left)} 行: {sorted(left)}')
