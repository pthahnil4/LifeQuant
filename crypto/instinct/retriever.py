# -*- coding: utf-8 -*-
"""
盘感相似行情检索（批次12 · P2）
================================
hybrid 召回：SQL 粗筛 → Python 精排。不用纯文本向量的原因见方案 §1.3：
"行情相似"的主体是数值+类别上下文，中文自由文本只是辅助（v2 加向量路）。

打分公式（v1，权重与 doc/RAG_LLM_Wiki模拟盘感落地方案.md §1.3 对齐）：
    score = 3.0×dir_pattern_sim + 2.0×atr_proximity + 1.0×period_match
          + 1.5×time_decay(τ=45d) + 2.0×text_cosine(v2 恒 0)
    同币种样本总分 ×3.0；跨币种补位保证任意快照 topK 非空。

防泄漏：检索"相似度"只消费 ctx_* 字段（now_ctx 由调用方给）；
候选行携带 outcome/hit 仅用于结果展示注记与 leave-one-out 统计自检，
prompt 渲染（P3）必须经 sanitize_case_for_prompt() 过滤。

自检 CLI（P2 验收）：
    python -m crypto.instinct.retriever --self-check
    逐条 labeled 决策样本：排除自身 → topK 邻居多数 hit 与自身 hit 一致率，
    对比随机基线（全局 hit 边际率），要求 ≥ 基线 + 10pp。
"""

import argparse
import datetime
import logging
import sys
from typing import Dict, List, Optional

from sqlalchemy import select

from ..database import session_scope
from ..models import InstinctCorpus
from ..analysis_record_repo import period_to_minutes

logger = logging.getLogger(__name__)

# ---- v1 默认参数（U5 确认的口径，改这里即可全局生效） ----------------------
TOP_K = 6
DAYS_BACK = 120          # SQL 粗筛时间窗
TAU_DAYS = 45.0          # 时间衰减常数
W_DIR, W_ATR, W_PERIOD, W_DECAY, W_TEXT = 3.0, 2.0, 1.0, 1.5, 2.0
SAME_INST_BOOST = 3.0
ATR_NEUTRAL = 0.5        # 分位未知时的中性邻近度（不加分不减分）

# 检索候选：带归一判断的决策样本（journal_review 纯语义样本 judgment='' 不入池）
_CAND_SOURCES = ('analysis_record', 'trade_slot')

# prompt 渲染禁字段前缀（sanitize 断言用；与 instinct_corpus 表列名口径一致）
_FORBIDDEN_PREFIX = ('outcome_', 'hit_', 'chg_', 'labeled', 'price_1h', 'price_4h')


# =============================================================================
# 打分原语
# =============================================================================

def dir_pattern_sim(a: Dict, b: Dict) -> float:
    """方向组合相似度：全对1.0 / 长短对(翻转一致0.6否则0.45) / 仅长对0.3 / 其余0。
    任一侧方向信息全缺（如 trade_slot 数值上下文为空）→ 0，不硬凑相似。"""
    if not (a.get('ctx_short_dir') or a.get('ctx_long_dir')):
        return 0.0
    if not (b.get('ctx_short_dir') or b.get('ctx_long_dir')):
        return 0.0
    if (a.get('ctx_short_dir') == b.get('ctx_short_dir')
            and a.get('ctx_long_dir') == b.get('ctx_long_dir')):
        flip_eq = a.get('ctx_dir_flipped') == b.get('ctx_dir_flipped')
        return 1.0 if flip_eq else 0.85
    if a.get('ctx_long_dir') == b.get('ctx_long_dir'):
        return 0.6 if (a.get('ctx_dir_flipped') == b.get('ctx_dir_flipped')) else 0.45
    if a.get('ctx_short_dir') == b.get('ctx_short_dir'):
        return 0.15
    return 0.0


def atr_proximity(a: Dict, b: Dict) -> float:
    """ATR 分位空间邻近度（跨币种可比）；任一分位未知取中性值"""
    pa, pb = a.get('ctx_atr_pctile', -1), b.get('ctx_atr_pctile', -1)
    if pa is None or pb is None or pa < 0 or pb < 0:
        return ATR_NEUTRAL
    return max(0.0, 1.0 - abs(float(pa) - float(pb)))


def period_match(a: Dict, b: Dict) -> float:
    """短周期一致性：完全一致=1，同数量级(±1倍)=0.5，其他/缺失=0"""
    pa, pb = a.get('short_period') or '', b.get('short_period') or ''
    if not pa or not pb:
        return 0.0
    if pa == pb:
        return 1.0
    ma, mb = period_to_minutes(pa), period_to_minutes(pb)
    if ma and mb and 0.5 <= ma / mb <= 2.0:
        return 0.5
    return 0.0


def time_decay(a: Dict, b: Dict, now: Optional[datetime.datetime] = None) -> float:
    """exp(-Δdays/τ)：以查询锚点 now 计（缺省用系统时间）；解析失败给 0"""
    anchor = now or datetime.datetime.now()
    ts = a.get('_ts_dt')
    if ts is None:
        ts = _parse_ts(a.get('ts'))
        if ts is not None:
            a['_ts_dt'] = ts
    if ts is None:
        return 0.0
    d = abs((anchor - ts).total_seconds()) / 86400.0
    return pow(2.718281828, -d / TAU_DAYS)


def _parse_ts(s):
    try:
        return datetime.datetime.strptime(str(s)[:19], '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def case_score(now_ctx: Dict, case: Dict, anchor: Optional[datetime.datetime] = None) -> float:
    """单案例得分（同币种 ×3 boost；查询无币种时不 boost）"""
    base = (W_DIR * dir_pattern_sim(now_ctx, case)
            + W_ATR * atr_proximity(now_ctx, case)
            + W_PERIOD * period_match(now_ctx, case)
            + W_DECAY * time_decay(case, now_ctx, anchor)
            + W_TEXT * 0.0)                          # v2: 文本向量余弦
    inst_q = (now_ctx.get('inst_id') or '').strip()
    if inst_q and case.get('inst_id') == inst_q:
        base *= SAME_INST_BOOST
    return round(base, 4)


# =============================================================================
# 检索
# =============================================================================

def _row_ctx(r: InstinctCorpus) -> Dict:
    d = r.ctx_dict()
    d.update({'id': r.id, 'source_ref': r.source_ref, 'judgment': r.judgment,
              'outcome_near': r.outcome_near, 'outcome_far': r.outcome_far,
              'hit_near': r.hit_near, 'hit_far': r.hit_far, 'labeled': bool(r.labeled),
              'decision_text': r.decision_text or ''})
    return d


def _query_cands(s, since: str, exclude_self_ref: str,
                 sources=_CAND_SOURCES) -> List[Dict]:
    rows = s.execute(
        select(InstinctCorpus)
        .where(InstinctCorpus.source.in_(sources),
               InstinctCorpus.judgment != '',
               InstinctCorpus.ts >= since)
        .order_by(InstinctCorpus.ts.desc())
    ).scalars().all()
    return [_row_ctx(r) for r in rows if r.source_ref != exclude_self_ref]


def retrieve(now_ctx: Dict, top_k: int = TOP_K, days_back: int = DAYS_BACK,
             exclude_self_ref: str = '', anchor: Optional[datetime.datetime] = None,
             session=None, sources=_CAND_SOURCES) -> List[Dict]:
    """SQL 粗筛（时间窗+决策样本源）→ Python 精排 topK。

    anchor：历史回灌时的"当时时刻"（P3 影子预测必备——时间衰减按真实决策
    时点算，否则历史样本会被"未来数据"支配）。缺省=现在。
    sources：候选池。数值检索只应使用带 ctx 的源（analysis_record）；
    trade_slot 无方向/ATR 上下文，仅按时间衰减参与，作纯语义兜底池。
    """
    since = ((anchor or datetime.datetime.now())
             - datetime.timedelta(days=days_back)).strftime('%Y-%m-%d %H:%M:%S')
    if session is not None:
        cands = _query_cands(session, since, exclude_self_ref, sources)
    else:
        with session_scope() as s:
            cands = _query_cands(s, since, exclude_self_ref, sources)
    # 时间窗内不足 topK 时放宽窗口（跨币种/跨期补位，保证非空）
    if len(cands) < top_k:
        with session_scope() as s2:
            extra = s2.execute(
                select(InstinctCorpus)
                .where(InstinctCorpus.source.in_(sources),
                       InstinctCorpus.judgment != '')
            ).scalars().all()
        seen = {c['source_ref'] for c in cands}
        cands += [_row_ctx(r) for r in extra if r.source_ref not in seen
                  and r.source_ref != exclude_self_ref]
    scored = [(case_score(now_ctx, c, anchor), c) for c in cands]
    scored.sort(key=lambda x: (-x[0], x[1]['ts']))
    return [dict(c, score=sc) for sc, c in scored[:top_k]]


def retrieve_for_record(rec_id: int, top_k: int = TOP_K,
                        exclude_self: bool = True) -> List[Dict]:
    """按 task_analysis_records.id 构造当时快照并检索（调试/自检/回灌入口）"""
    with session_scope() as s:
        r = s.get(InstinctCorpus, rec_id)
        if r is None:
            raise KeyError(f'corpus id={rec_id} 不存在')
        now = _row_ctx(r)
        anchor = _parse_ts(r.ts)
        return retrieve(now, top_k=top_k,
                        exclude_self_ref=r.source_ref if exclude_self else '',
                        anchor=anchor, session=s)


def explain(now_ctx: Dict, top_k: int = TOP_K,
            anchor: Optional[datetime.datetime] = None,
            exclude_self_ref: str = '') -> List[Dict]:
    """打分明细版检索（仅供人调试；结果永不进 prompt）。

    对 retrieve 的每个 topK 结果，用同一批纯函数复算子分项：
      breakdown = {dir, atr, period, decay 原始相似度(0~1),
                   各自加权分, base 基础分, same_inst_boost, final}
    复算而非改 retrieve 返回值——保持生产检索路径零变动。
    """
    cases = retrieve(now_ctx, top_k=top_k, exclude_self_ref=exclude_self_ref,
                     anchor=anchor)
    out = []
    q_inst = (now_ctx.get('inst_id') or '').strip()
    for c in cases:
        d, at, p, dc = (dir_pattern_sim(now_ctx, c), atr_proximity(now_ctx, c),
                        period_match(now_ctx, c), time_decay(c, now_ctx, anchor))
        base = (W_DIR * d + W_ATR * at + W_PERIOD * p + W_DECAY * dc + W_TEXT * 0.0)
        boosted = bool(q_inst) and c.get('inst_id') == q_inst
        out.append(dict(
            {k: v for k, v in c.items() if not str(k).startswith('_')},
            breakdown={'dir_sim': round(d, 4), 'atr_prox': round(at, 4),
                       'period': round(p, 4), 'decay': round(dc, 4),
                       'w_dir': round(W_DIR * d, 4), 'w_atr': round(W_ATR * at, 4),
                       'w_period': round(W_PERIOD * p, 4), 'w_decay': round(W_DECAY * dc, 4),
                       'base': round(base, 4),
                       'boost': SAME_INST_BOOST if boosted else 1.0,
                       'final': round(base * (SAME_INST_BOOST if boosted else 1.0), 4)}))
    return out


# =============================================================================
# 防泄漏出口（P3 prompt 渲染必须用这两个函数取数）
# =============================================================================

def sanitize_case_for_prompt(case: Dict) -> Dict:
    """把检索结果过滤成 prompt 可注入形态：ctx_* + judgment，
    outcome/hit/chg 一律剥离（结果注记如需展示，由展示层单独取并在
    prompt 中显式标注"事后已知"——P3 默认不注入）。"""
    leak = [k for k in case if str(k).startswith(_FORBIDDEN_PREFIX)]
    if leak:
        logger.debug('[InstinctRetriever] sanitize 剥离字段: %s', leak)
    return {k: v for k, v in case.items()
            if not str(k).startswith(_FORBIDDEN_PREFIX)
            and not str(k).startswith('_') and k != 'score'}


def assert_no_future_leak(prompt_text: str, known_values: List[str]):
    """负例断言：prompt 文本不得出现任何 outcome 数值串（P3 泄漏闸门）。
    known_values 传各案例的 chg/outcome 字符串化值。"""
    hits = [v for v in known_values if v and str(v) in prompt_text]
    if hits:
        raise AssertionError(f'prompt 泄漏未来信息值: {hits[:5]}')


# =============================================================================
# leave-one-out 自检 CLI（P2 验收）
# =============================================================================

def self_check(top_k: int = TOP_K, margin_pp: float = 10.0,
               stat_min_n: int = 30) -> bool:
    """leave-one-out 自检（P2 验收），查询与邻居池均限 analysis_record。

    为什么剔出 trade_slot：它们没有方向/ATR 数值上下文，只能靠时间衰减
    混进相似池，会把多数投票拉向自己的高边际命中率（88.9%），制造假信号
    ——首轮全池自检就实测到了这种污染（一致率与全池边际率完全相等）。

    两个口径：
      结构门（永远强制）：每条查询 topK 非空 + 排序确定性（重跑一致）；
      统计门（n ≥ stat_min_n 才出判定，否则仅报告不定分）：
        排除自身后 topK 邻居多数 hit 与自身 hit 一致率，
        对比"永远猜多数类"基线 max(p, 1-p)，要求 ≥ 基线 + margin_pp。
    """
    with session_scope() as s:
        rows = s.execute(
            select(InstinctCorpus)
            .where(InstinctCorpus.source == 'analysis_record',
                   InstinctCorpus.judgment != '',
                   InstinctCorpus.labeled.is_(True),
                   InstinctCorpus.hit_far.isnot(None))
        ).scalars().all()
    samples = [_row_ctx(r) for r in rows]
    ok = True
    if not samples:
        print('[FAIL] 无 labeled analysis_record 样本')
        return False
    pool = ('analysis_record',)
    hs = [1 if c['hit_far'] else 0 for c in samples]
    hit_rate = sum(hs) / len(hs) * 100
    naive_majority_base = max(hit_rate, 100 - hit_rate)

    # ---- 结构门 ----
    agree = tested = 0
    nonempty = True
    for c in samples:
        neigh = retrieve(c, top_k=top_k, exclude_self_ref=c['source_ref'],
                         anchor=_parse_ts(c['ts']), sources=pool)
        nonempty = nonempty and bool(neigh)
        nh = [n['hit_far'] for n in neigh if n.get('hit_far') is not None]
        if not nh:
            continue
        tested += 1
        majority = (sum(nh) / len(nh)) >= 0.5
        same = bool(c['hit_far']) == majority
        agree += same
        print(f"  {c['source_ref']:<10} hit={int(bool(c['hit_far']))} "
              f"邻居hit={[int(x) for x in nh]} → {'一致' if same else '不一致'}")
    # 确定性：抽 3 条重跑比对 source_ref 序列
    determ = True
    for c in samples[:3]:
        r1 = [x['source_ref'] for x in retrieve(c, top_k=top_k,
                                                exclude_self_ref=c['source_ref'],
                                                anchor=_parse_ts(c['ts']), sources=pool)]
        r2 = [x['source_ref'] for x in retrieve(c, top_k=top_k,
                                                exclude_self_ref=c['source_ref'],
                                                anchor=_parse_ts(c['ts']), sources=pool)]
        determ = determ and r1 == r2
    print(f'\n[结构门]')
    print(f"  {'✅' if nonempty else '❌'} 全部查询 topK 非空（n={len(samples)}）")
    print(f'  {"✅" if determ else "❌"} 排序确定性（重跑一致）')
    ok = ok and nonempty and determ

    # ---- 统计门 ----
    rate = agree / tested * 100 if tested else 0.0
    print(f'\n[统计门] 邻居多数一致率 {agree}/{tested} = {rate:.1f}%  | '
          f"hit 边际率 {hit_rate:.1f}% → 多数类基线 {naive_majority_base:.1f}%"
          f' → 门槛 {naive_majority_base + margin_pp:.1f}%')
    if len(samples) >= stat_min_n:
        stat_ok = rate >= naive_majority_base + margin_pp
        print(f"  {'✅' if stat_ok else '❌'} 统计门判定（n={len(samples)} ≥ {stat_min_n}）")
        ok = ok and stat_ok
    else:
        print(f'  ⏸ 样本 {len(samples)} < {stat_min_n}，统计门不出判定，'
              f'延后至语料达标时复跑（避免小样本假信号，P4 复检）')
    return ok


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    ap = argparse.ArgumentParser(description='盘感检索（批次12 P2）')
    ap.add_argument('--self-check', action='store_true', help='leave-one-out 自检')
    ap.add_argument('--top-k', type=int, default=TOP_K)
    ap.add_argument('--demo', type=int, metavar='CORPUS_ID',
                    help='打印指定语料行的相似邻居（排除自身）')
    args = ap.parse_args()
    if args.self_check:
        raise SystemExit(0 if self_check(args.top_k) else 1)
    if args.demo:
        for c in retrieve_for_record(args.demo):
            print(f"{c['score']:>8.3f}  {c['source_ref']:<28} {c['inst_id']:<16} "
                  f"judgment={c['judgment']:<5} hit_far={c['hit_far']} ts={c['ts']}")
        return
    ap.print_help()


if __name__ == '__main__':
    main()
