# -*- coding: utf-8 -*-
"""从 instinct_corpus 反推重建被删除的 task_analysis_records（批次12 数据事故应急）

背景：2026-09-17 发现 task_analysis_records 被 HTTP 删除端点清空（15 条带判断
快照全失）。instinct_corpus 的 15 行 source='analysis_record' 保留了每行的
source_ref(tar:id) 与全部 ctx 字段，可反推重建：

  - 直接列：ts/inst_id/periods/directions/atr_pct/price/user_judgment/user_reason
  - price_1h/price_4h：由 chg_near/far_pct 反推 price*(1+chg/100)（4位小数精度，
    classify_move 重算逐位一致；outcome 为空的行保持 None 不伪造）
  - 不可恢复：ts_1h/ts_4h/hour_slot 置空（仅展示列，不参与任何统计口径）

用法：python data/_rebuild_tar_from_corpus.py          # 干跑核对
      python data/_rebuild_tar_from_corpus.py --apply  # 写库
安全：只插不回（若官方备份可用，可再按 id 删除本脚本产物换回原行）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import select  # noqa: E402
from crypto.database import session_scope  # noqa: E402
from crypto.models import InstinctCorpus, TaskAnalysisRecord  # noqa: E402

APPLY = '--apply' in sys.argv


def _price_from_chg(base_price, chg_pct, outcome):
    """由涨跌幅反推结算价；outcome 空 = 当时未回填，保持 None（不伪造数据）"""
    if not outcome:
        return None
    try:
        return round(float(base_price) * (1 + float(chg_pct or 0) / 100.0), 8)
    except (TypeError, ValueError):
        return None


def main():
    with session_scope() as s:
        existing = {r.id for r in s.execute(select(TaskAnalysisRecord)).scalars()}
        rows = s.execute(select(InstinctCorpus).where(
            InstinctCorpus.source == 'analysis_record')
            .order_by(InstinctCorpus.ts)).scalars().all()
        plan = []
        for r in rows:
            try:
                rid = int(str(r.source_ref).split(':', 1)[1])
            except (IndexError, ValueError):
                print(f'[SKIP] source_ref 异常: {r.source_ref}')
                continue
            if rid in existing:
                print(f'[SKIP] id={rid} 已存在（不覆盖）')
                continue
            plan.append(dict(
                id=rid, ts=r.ts, inst_id=r.inst_id,
                price=r.ctx_price,
                short_period=r.short_period or '', long_period=r.long_period or '',
                short_dir=r.ctx_short_dir or '', long_dir=r.ctx_long_dir or '',
                long_dir_prev=r.ctx_long_dir_prev or '',
                atr_pct=r.ctx_atr_pct,
                user_judgment=r.judgment or '', user_reason=r.ctx_text or '',
                hour_slot='', source='live',
                price_1h=_price_from_chg(r.ctx_price, r.chg_near_pct, r.outcome_near),
                ts_1h=None,
                price_4h=_price_from_chg(r.ctx_price, r.chg_far_pct, r.outcome_far),
                ts_4h=None))
        print(f'[PLAN] 重建 {len(plan)} 行（现存 tar {len(existing)} 行）')
        for p in plan:
            print(f"  id={p['id']} {p['ts']} {p['inst_id']} jd={p['user_judgment']} "
                  f"p1h={p['price_1h']} p4h={p['price_4h']}")

        # 口径自检：重建行过 classify_move 重算，与 corpus outcome 逐位比对
        from crypto.analysis_record_repo import classify_move
        mism = 0
        for p in plan:
            atr = float(p['atr_pct'] or 0)
            near = classify_move(p['price'], p['price_1h'], atr, False) or ''
            far = classify_move(p['price'], p['price_4h'], atr, True) or ''
            src = next(r for r in rows if str(r.source_ref) == f"tar:{p['id']}")
            if near != src.outcome_near or far != src.outcome_far:
                mism += 1
                print(f"  [MISMATCH] id={p['id']} 重算({near},{far}) != corpus"
                      f"({src.outcome_near},{src.outcome_far})")
        print(f'[CHECK] outcome 重算不一致行数 = {mism}')
        if mism:
            print('[ABORT] 存在口径漂移，拒绝写库')
            raise SystemExit(2)

        if not APPLY:
            print('[DRY-RUN] 未写库；确认无误后加 --apply')
            raise SystemExit(0)
        for p in plan:
            s.add(TaskAnalysisRecord(**p))
        print(f'[APPLIED] 已插入 {len(plan)} 行')

    # 写库后跑官方核对：verify() 三方核对（源表全量 vs P0 窗口 vs 语料表）
    from crypto.instinct import corpus_builder as cb
    ok = cb.verify()
    print(f'[VERIFY] corpus_builder.verify() = {ok}')
    raise SystemExit(0 if ok else 3)


if __name__ == '__main__':
    main()
