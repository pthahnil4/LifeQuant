# -*- coding: utf-8 -*-
"""
盘感语料管道（批次12 · P1）
================================
三个语料源统一压缩成 instinct_corpus 的"一行一样本"，幂等入库：

    analysis_record  task_analysis_records     判断 + 近/远窗口回填对错（核心）
    trade_slot       plan_slots × trade 卡      预测 + 事后实际涨跌 + 行情文本
    journal_review   journal_notes(review)      复盘四格 lesson（当前 0 条，管道先行）

防泄漏切分（本模块存在的核心理由）：
    ctx_* 列 ← 决策当时可见字段（唯一允许进入检索与 prompt 的数据面）
    outcome_*/hit_*/chg_* 列 ← 事后才知字段（只做统计，永不进 prompt）
    InstinctCorpus.ctx_dict() 是下游取数的唯一入口，物理上给不出 outcome。

计分口径：直接复用 crypto/analysis_record_repo.classify_move /
_JUDGMENT_EXPECT / _DIR_EXPECT 与窗口常量，杜绝两套实现漂移。

用法（项目根目录）：
    python -m crypto.instinct.corpus_builder --dry-run   # 打印每源样例，不落库
    python -m crypto.instinct.corpus_builder --apply     # 全量幂等入库
    python -m crypto.instinct.corpus_builder --verify    # 基线核对（P1 验收）
"""

import argparse
import bisect
import logging
import sys
from typing import Dict, List, Optional

from sqlalchemy import select

from ..database import session_scope
from ..models import (
    InstinctCorpus, TaskAnalysisRecord, PlanCard, PlanSlot, JournalNote)
from ..analysis_record_repo import (
    REVIEW_NEAR_MULT, REVIEW_FAR_MULT, HIT_ATR_K, HIT_FLOOR_PCT, HIT_FAR_SCALE,
    _JUDGMENT_EXPECT, _DIR_EXPECT, classify_move)

logger = logging.getLogger(__name__)

# 交易卡 prediction/actual 为中文三分类（前端 select 值），归一到语料口径
_PRED_MAP = {'涨': 'rise', '跌': 'fall', '横盘': 'watch'}
_ACT_MAP = {'涨': 'up', '跌': 'down', '横盘': 'flat'}

# ATR 分位数计算的最低样本数：低于该值分位无统计意义，记 -1（未知）
_PCTILE_MIN_N = 5

# P1 验收冻结基线（P0 探针 2026-09-17 实测，窗口截至该时点最后一条记录；
# 新记录持续产生，verify 只在冻结窗口内比对，窗口外差异属正常增量）
_P0_CUTOFF_TS = '2026-09-14 16:16:24'
_P0_FROZEN = {'user_near': 20.0, 'user_far': 33.3, 'strat_near': 26.7, 'strat_far': 53.3}


def _chg_pct(price, follow) -> float:
    """后续价相对快照价的涨跌幅%（无效数据返回 0）"""
    try:
        price = float(price or 0)
        follow = float(follow or 0)
        if price <= 0 or follow <= 0:
            return 0.0
        return round((follow / price - 1) * 100, 4)
    except (TypeError, ValueError):
        return 0.0


def _atr_pctile(values: List[float], v: float) -> float:
    """v 在该币 ATR 历史序列中的分位（0~1）；样本 <_PCTILE_MIN_N 返回 -1"""
    if not values or len(values) < _PCTILE_MIN_N or v is None:
        return -1.0
    sv = sorted(values)
    return round(bisect.bisect_left(sv, float(v)) / (len(sv) - 1), 4)


# =============================================================================
# 三源抽取 → 标准样本 dict（键与 InstinctCorpus 列同名）
# =============================================================================

def _samples_from_analysis(session) -> List[dict]:
    """task_analysis_records：15 条带判断快照是本模块的权重样本源。

    近/远窗口 outcome 按记录自身 atr_pct 用 classify_move 三分类，
    与 compute_stats / P0 探针逐位同口径。
    """
    rows = session.execute(select(TaskAnalysisRecord)).scalars().all()
    atrs_by_inst: Dict[str, List[float]] = {}
    for r in rows:
        if r.atr_pct and float(r.atr_pct) > 0:
            atrs_by_inst.setdefault(r.inst_id, []).append(float(r.atr_pct))

    samples = []
    for r in rows:
        judgment = r.user_judgment if r.user_judgment in _JUDGMENT_EXPECT else ''
        atr = float(r.atr_pct or 0)
        long_dir_prev = r.long_dir_prev or ''
        outcome_near = classify_move(r.price, r.price_1h, atr, False) or ''
        outcome_far = classify_move(r.price, r.price_4h, atr, True) or ''
        hit_near = hit_far = None
        if judgment:
            if outcome_near:
                hit_near = _JUDGMENT_EXPECT[judgment] == outcome_near
            if outcome_far:
                hit_far = _JUDGMENT_EXPECT[judgment] == outcome_far
        samples.append({
            'source': 'analysis_record',
            'source_ref': f'tar:{r.id}',
            'ts': r.ts,
            'inst_id': r.inst_id,
            'short_period': r.short_period or '',
            'long_period': r.long_period or '',
            'ctx_short_dir': r.short_dir or '',
            'ctx_long_dir': r.long_dir or '',
            'ctx_long_dir_prev': long_dir_prev,
            'ctx_atr_pct': atr,
            'ctx_atr_pctile': _atr_pctile(atrs_by_inst.get(r.inst_id, []), atr),
            'ctx_dir_flipped': bool(long_dir_prev and r.long_dir
                                    and long_dir_prev != r.long_dir),
            'ctx_price': float(r.price or 0),
            'ctx_text': (r.user_reason or '').strip() or None,
            'judgment': judgment,
            'decision_text': None,
            'outcome_near': outcome_near,
            'outcome_far': outcome_far,
            'chg_near_pct': _chg_pct(r.price, r.price_1h),
            'chg_far_pct': _chg_pct(r.price, r.price_4h),
            'hit_near': hit_near,
            'hit_far': hit_far,
            'labeled': bool(outcome_far),
        })
    return samples


def _samples_from_trade_slots(session) -> List[dict]:
    """plan_slots × trade 卡：prediction(涨/跌/横盘) + 事后 actual 结算。

    打卡无行情数值上下文（方向/ATR 未记录），ctx_* 数值列留空位，
    主要贡献 judgment↔文本语料与全局倾向统计；actual 记入 outcome_far
    （结算时点与近/远窗口不严格对应，属粗粒度标签，检索与统计按
    source='trade_slot' 过滤时可整体排除，不污染分析记录基线）。
    """
    rows = session.execute(
        select(PlanSlot, PlanCard)
        .join(PlanCard, PlanSlot.card_id == PlanCard.id)
        .where(PlanCard.type == 'trade',
               PlanSlot.has_record.is_(True),
               PlanSlot.prediction != '')
    ).all()
    samples = []
    for slot, _card in rows:
        judgment = _PRED_MAP.get(slot.prediction, '')
        actual = _ACT_MAP.get(slot.actual or '', '')
        hit_far = None
        if judgment and actual:
            hit_far = _JUDGMENT_EXPECT[judgment] == actual
        elif slot.hit is not None:
            # actual 值域外（历史脏值）时退回打卡结算的 hit，不采信 actual
            hit_far = bool(slot.hit)
        samples.append({
            'source': 'trade_slot',
            'source_ref': f'slot:{slot.card_id}_{slot.slot_index}',
            'ts': slot.filled_at or '',
            'inst_id': '',
            'short_period': '',
            'long_period': '',
            'ctx_short_dir': '',
            'ctx_long_dir': '',
            'ctx_long_dir_prev': '',
            'ctx_atr_pct': 0.0,
            'ctx_atr_pctile': -1.0,
            'ctx_dir_flipped': False,
            'ctx_price': 0.0,
            'ctx_text': (slot.market_analysis or '').strip() or None,
            'judgment': judgment,
            'decision_text': (slot.action_advice or '').strip() or None,
            'outcome_near': '',
            'outcome_far': actual,
            'chg_near_pct': 0.0,
            'chg_far_pct': 0.0,
            'hit_near': None,
            'hit_far': hit_far,
            'labeled': bool(actual),
        })
    return samples


def _samples_from_reviews(session) -> List[dict]:
    """journal_notes(type=review)：lesson 非空才入库（judgment 留空，纯语义样本）"""
    rows = session.execute(
        select(JournalNote).where(JournalNote.type == 'review')
    ).scalars().all()
    samples = []
    for n in rows:
        lesson = (n.review_lesson or '').strip()
        if len(lesson) <= 4:
            continue
        samples.append({
            'source': 'journal_review',
            'source_ref': f'note:{n.id}',
            'ts': n.created_at.strftime('%Y-%m-%d %H:%M:%S') if n.created_at else '',
            'inst_id': '',
            'short_period': '',
            'long_period': '',
            'ctx_short_dir': '', 'ctx_long_dir': '', 'ctx_long_dir_prev': '',
            'ctx_atr_pct': 0.0, 'ctx_atr_pctile': -1.0,
            'ctx_dir_flipped': False, 'ctx_price': 0.0,
            'ctx_text': lesson,
            'judgment': '',
            'decision_text': (n.review_decision or '').strip() or None,
            'outcome_near': '', 'outcome_far': '',
            'chg_near_pct': 0.0, 'chg_far_pct': 0.0,
            'hit_near': None, 'hit_far': None,
            'labeled': False,
        })
    return samples


def build_samples(session) -> List[dict]:
    """三源全量抽取（单源失败不拖垮整体，记 warning 返回已得部分）"""
    samples: List[dict] = []
    for fn in (_samples_from_analysis, _samples_from_trade_slots, _samples_from_reviews):
        try:
            samples.extend(fn(session))
        except Exception as e:  # noqa: BLE001
            logger.warning('[InstinctCorpus] 源抽取失败 %s: %s', fn.__name__, e)
    return samples


# =============================================================================
# 幂等入库
# =============================================================================

_MUTABLE = tuple(k for k in InstinctCorpus.__table__.columns.keys()
                 if k not in ('id', 'source', 'source_ref',
                              'created_at', 'updated_at'))


def upsert_samples(session, samples: List[dict]):
    """按 (source, source_ref) 幂等 upsert，返回 (inserted, updated, unchanged)"""
    refs = {(s['source'], s['source_ref']): s for s in samples}
    existing = {
        (r.source, r.source_ref): r
        for r in session.execute(
            select(InstinctCorpus).where(
                InstinctCorpus.source.in_(
                    {src for src, _ in refs} or {'__none__'}))
        ).scalars()
    }
    inserted = updated = unchanged = 0
    for key, s in refs.items():
        row = existing.get(key)
        if row is None:
            session.add(InstinctCorpus(**s))
            inserted += 1
            continue
        diffs = {c: s[c] for c in _MUTABLE if getattr(row, c) != s[c]}
        if diffs:
            for c, v in diffs.items():
                setattr(row, c, v)
            updated += 1
        else:
            unchanged += 1
    return inserted, updated, unchanged


# =============================================================================
# P1 验收：基线核对
# =============================================================================

def _rates_from_rows(rows) -> Dict[str, float]:
    """对 (judgment, ctx_long_dir_prev, ctx_long_dir, outcome_near, outcome_far)
    样本行计算四项命中率（保留 1 位小数，与 P0 探针/前端口径一致）"""
    acc = {k: [0, 0] for k in _P0_FROZEN}
    for judgment, dir_prev, dir_cur, o_near, o_far in rows:
        sdir = dir_prev or dir_cur or ''
        for win, outcome in (('near', o_near), ('far', o_far)):
            if judgment in _JUDGMENT_EXPECT and outcome:
                b = acc[f'user_{win}']
                b[1] += 1
                b[0] += (_JUDGMENT_EXPECT[judgment] == outcome)
            if sdir in _DIR_EXPECT and outcome:
                b = acc[f'strat_{win}']
                b[1] += 1
                b[0] += (_DIR_EXPECT[sdir] == outcome)
    return {k: round(h / t * 100, 1) if t else 0.0 for k, (h, t) in acc.items()}


def verify() -> bool:
    """语料回算基线 vs 源表现算基线 vs P0 冻结值，三方核对。

    冻结比对限定在 P0 窗口（ts <= cutoff）内，此后新增记录属正常增量。
    """
    with session_scope() as s:
        src_rows = s.execute(
            select(TaskAnalysisRecord.user_judgment,
                   TaskAnalysisRecord.long_dir_prev,
                   TaskAnalysisRecord.long_dir,
                   TaskAnalysisRecord.price,
                   TaskAnalysisRecord.price_1h,
                   TaskAnalysisRecord.price_4h,
                   TaskAnalysisRecord.atr_pct,
                   TaskAnalysisRecord.ts)
            .where(TaskAnalysisRecord.user_judgment.in_(
                tuple(_JUDGMENT_EXPECT)))  # 与语料归一化同一过滤（脏 judgment 双侧同剔）
        ).all()
        expected = []
        for judgment, dprev, dcur, price, p1h, p4h, atr, ts in src_rows:
            a = float(atr or 0)
            expected.append((judgment, dprev or '', dcur or '',
                             classify_move(price, p1h, a, False) or '',
                             classify_move(price, p4h, a, True) or '',
                             ts))
        cor_rows = s.execute(
            select(InstinctCorpus.judgment, InstinctCorpus.ctx_long_dir_prev,
                   InstinctCorpus.ctx_long_dir, InstinctCorpus.outcome_near,
                   InstinctCorpus.outcome_far, InstinctCorpus.ts)
            .where(InstinctCorpus.source == 'analysis_record',
                   InstinctCorpus.judgment != '')
        ).all()
    rates_src = _rates_from_rows([tuple(e[:5]) for e in expected])
    rates_src_p0 = _rates_from_rows([tuple(e[:5]) for e in expected if e[5] <= _P0_CUTOFF_TS])
    rates_cor = _rates_from_rows([tuple(r[:5]) for r in cor_rows])
    print(f'口径常量: near×{REVIEW_NEAR_MULT} far×{REVIEW_FAR_MULT} '
          f'θ=max({HIT_FLOOR_PCT}, {HIT_ATR_K}×atr×[1|{round(HIT_FAR_SCALE, 3)}])')
    print(f'{"指标":<12}{"源表全量":>10}{"源表P0窗":>10}{"语料表":>10}{"P0冻结":>10}  判定')
    ok = True
    for k, v in _P0_FROZEN.items():
        line_ok = (rates_src[k] == rates_cor[k] == rates_src_p0[k] == v)
        ok = ok and line_ok
        print(f'{k:<14}{rates_src[k]:>9.1f}{rates_src_p0[k]:>10.1f}'
              f'{rates_cor[k]:>10.1f}{v:>9.1f}  {"✅" if line_ok else "❌"}')
    print('[VERIFY]', 'PASS' if ok else 'FAIL（语料与源表或冻结基线不一致）')
    return ok


# =============================================================================
# CLI
# =============================================================================

def _print_samples(samples: List[dict]):
    """每源打一条完整样例（ctx 与 outcome 分区展示，人工核对防泄漏切分）"""
    seen = set()
    for smp in samples:
        if smp['source'] in seen:
            continue
        seen.add(smp['source'])
        print(f"\n--- 样例 [{smp['source']}] {smp['source_ref']} ---")
        print('  当时可见(ctx):', {k: v for k, v in smp.items() if k.startswith('ctx_')
                                 or k in ('ts', 'inst_id', 'short_period', 'long_period')})
        print('  当时决策:', {'judgment': smp['judgment'],
                             'decision_text': (smp['decision_text'] or '')[:60]})
        print('  事后(outcome):', {k: v for k, v in smp.items()
                                  if k.startswith(('outcome_', 'hit_', 'chg_', 'labeled'))})
    missing = {'analysis_record', 'trade_slot', 'journal_review'} - seen
    if missing:
        print(f'\n[提示] 本次无样例的源: {", ".join(sorted(missing))}')


def main():
    ap = argparse.ArgumentParser(description='盘感语料管道（批次12 P1）')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true', help='打印每源样例，不落库')
    g.add_argument('--apply', action='store_true', help='全量幂等入库')
    g.add_argument('--verify', action='store_true', help='P1 验收：基线三方核对')
    args = ap.parse_args()
    # Windows 控制台默认 GBK，输出含符号字符时统一切 UTF-8
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    logging.basicConfig(level=logging.INFO, format='%(message)s')

    if args.verify:
        raise SystemExit(0 if verify() else 1)

    with session_scope() as s:
        samples = build_samples(s)
        if args.dry_run:
            by_src: Dict[str, int] = {}
            for m in samples:
                by_src[m['source']] = by_src.get(m['source'], 0) + 1
            print(f'样本总量 {len(samples)}：'
                  + ' | '.join(f'{k}={v}' for k, v in sorted(by_src.items())))
            _print_samples(samples)
            print('\n[dry-run] 未写库。核对 ctx/outcome 切分后执行 --apply')
            return
        ins, upd, unch = upsert_samples(s, samples)
    print(f'[apply] 样本 {len(samples)} 条：新增 {ins}，更新 {upd}，未变 {unch}')
    print('下一步：python -m crypto.instinct.corpus_builder --verify')


if __name__ == '__main__':
    main()
