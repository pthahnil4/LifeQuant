# -*- coding: utf-8 -*-
"""
盘感 Wiki 统计蒸馏（批次12 · P2，无需 LLM 的第一条蒸馏路）
==========================================================
从 instinct_corpus 聚合"判断 × 局面 × 事后结果"，达到显著性门槛才产出
candidate 规则卡（只出 candidate，active 必须人工确认——方案 §1.4 铁律）。

两条产出路径：
  1) 分桶统计路：按 (source, judgment, 长周期方向, 是否翻转) 分桶，
     桶内 n ≥ MIN_N(10) 且 |命中率 − 同源基线| ≥ MIN_GAP(15pp) 才出卡。
     基线取同源而非全局：analysis_record(33.3%) 与 trade_slot(88.9%)
     边际率差一截，混池比较会把"slot 本来就容易中"误蒸馏成规则。
  2) 种子元规则路：个人判断 vs 策略方向的四项命中率对比（P0 事实①的
     持续复算版），差距过门槛才生成 seed:* 卡（如"直觉与策略冲突时降权"）。

不显著就不出卡——报告里如实写"样本不足"，禁止把噪声蒸馏成"盘感"。
输出报告固定落盘 data/instinct_wiki_report.md（含混淆矩阵，供人工审阅）。

CLI：
    python -m crypto.instinct.wiki_distiller --report   # 只出报告不写库
    python -m crypto.instinct.wiki_distiller --apply    # 报告 + candidate 入库
    python -m crypto.instinct.wiki_distiller --list     # 列出现有规则卡
"""

import argparse
import datetime
import logging
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List

from sqlalchemy import select

from ..database import session_scope
from ..models import InstinctCorpus
from ..analysis_record_repo import _JUDGMENT_EXPECT, _DIR_EXPECT
from . import wiki_repo

logger = logging.getLogger(__name__)

MIN_N = 10           # 桶内最小样本数（U5 参数）
MIN_GAP_PP = 15.0    # 相对同源基线的最小偏离（U5 参数）

# 报告落盘：项目根 data/ 目录（与探针报告同处）
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORT_PATH = os.path.join(_ROOT, 'data', 'instinct_wiki_report.md')

_WIN_CN = {'near': '近窗口', 'far': '远窗口'}
_JCN = {'rise': '看涨', 'fall': '看跌', 'watch': '看横盘'}


def _sanitize_key(s: str) -> str:
    return re.sub(r'[^0-9a-zA-Z_\u4e00-\u9fff.-]+', '_', s)[:64]


def load_samples(session) -> List[InstinctCorpus]:
    return list(session.execute(
        select(InstinctCorpus)
        .where(InstinctCorpus.judgment != '',
               InstinctCorpus.labeled.is_(True))
    ).scalars().all())


def _hit(row: InstinctCorpus, win: str):
    return getattr(row, f'hit_{win}')


def _outcome(row: InstinctCorpus, win: str) -> str:
    return getattr(row, f'outcome_{win}') or ''


# =============================================================================
# 统计
# =============================================================================

def bucket_stats(rows: List[InstinctCorpus], win: str) -> Dict:
    """返回 {source: {'base': (h,t), 'buckets': {key: (h, t, refs)}}}。
    只统计该窗口已判定（hit 非空）的样本。"""
    out = {}
    for r in rows:
        if r.source not in ('analysis_record', 'trade_slot'):
            continue
        h = _hit(r, win)
        if h is None:
            continue
        src = out.setdefault(r.source, {'base': [0, 0], 'buckets': {}})
        src['base'][0] += int(bool(h))
        src['base'][1] += 1
        flip = int(bool(r.ctx_dir_flipped)) if r.source == 'analysis_record' else -1
        key = (r.judgment, r.ctx_long_dir or '-', flip)
        b = src['buckets'].setdefault(key, [0, 0, []])
        b[0] += int(bool(h))
        b[1] += 1
        b[2].append(r.source_ref)
    return out


def confusion(rows: List[InstinctCorpus], win: str) -> Dict:
    """source × judgment × outcome 计数矩阵（报告用，不产规则；分源防 slot 大样本主导）"""
    m = defaultdict(lambda: defaultdict(int))
    for r in rows:
        o = _outcome(r, win)
        if o:
            m[(r.source, r.judgment)][o] += 1
    return {k: dict(v) for k, v in m.items()}


def user_vs_strategy(rows: List[InstinctCorpus], win: str) -> Dict:
    """analysis_record 上 个人判断 vs 策略方向 的命中率与样本数（种子路输入）"""
    uh = ut = sh = st = 0
    for r in rows:
        if r.source != 'analysis_record':
            continue
        o = _outcome(r, win)
        if not o:
            continue
        if r.judgment in _JUDGMENT_EXPECT:
            ut += 1
            uh += (_JUDGMENT_EXPECT[r.judgment] == o)
        sdir = r.ctx_long_dir_prev or r.ctx_long_dir
        if sdir in _DIR_EXPECT:
            st += 1
            sh += (_DIR_EXPECT[sdir] == o)
    return {'user_h': uh, 'user_n': ut, 'strat_h': sh, 'strat_n': st,
            'user_rate': round(uh / ut * 100, 1) if ut else 0.0,
            'strat_rate': round(sh / st * 100, 1) if st else 0.0}


# =============================================================================
# 候选规则生成
# =============================================================================

def _bucket_label(source: str, key) -> str:
    judgment, long_dir, flip = key
    parts = [f"判断{_JCN.get(judgment, judgment)}"]
    if source == 'analysis_record':
        if long_dir != '-':
            parts.append(f"长周期看{'多' if long_dir == 'long' else '空'}")
        if flip >= 0:
            parts.append('方向刚翻转' if flip else '方向未翻转')
    return '＋'.join(parts)


def generate_candidates(rows: List[InstinctCorpus]) -> List[Dict]:
    """分桶路 + 种子路，返回未入库的候选规则 dict 列表（全部过显著性门槛）"""
    cands = []
    for win in ('near', 'far'):
        stats = bucket_stats(rows, win)
        for source, d in stats.items():
            bh, bt = d['base']
            if not bt:
                continue
            base_rate = bh / bt * 100
            for key, (h, t, refs) in sorted(d['buckets'].items()):
                rate = h / t * 100
                if t < MIN_N or abs(rate - base_rate) < MIN_GAP_PP:
                    continue
                judgment, long_dir, flip = key
                rk = f"stat:{_sanitize_key(source)}:{win}:{_sanitize_key(judgment)}" \
                     f":{long_dir if long_dir != '-' else 'na'}"
                if flip >= 0:
                    rk += f":f{flip}"
                gap = rate - base_rate
                cands.append({
                    'rule_key': rk[:64],
                    'statement': (
                        f"{_WIN_CN[win]}[{_bucket_label(source, key)}] 局面下历史命中率 "
                        f"{rate:.1f}%（同源基线 {base_rate:.1f}%，n={t}）——"
                        + ('明显占优，可优先考虑该判断' if gap > 0
                           else '明显偏弱，出现该直觉时先复核依据')),
                    'kind': 'scenario' if gap > 0 else 'prohibition',
                    'condition': {'sources': [source], 'judgment': [judgment]}
                                 | ({'long_dir': [long_dir], 'dir_flipped': flip}
                                    if source == 'analysis_record' and long_dir != '-' else {}),
                    'stat_basis': f"{source}/{win} 命中{h}/{t}={rate:.1f}% vs 基线{base_rate:.1f}%",
                    'evidence_refs': refs[:50],
                    'created_by': 'distiller',
                })
    # ---- 种子元规则路：个人 vs 策略（P0 事实①持续复算） ----
    for win in ('near', 'far'):
        uv = user_vs_strategy(rows, win)
        if uv['user_n'] >= MIN_N and uv['strat_n'] >= MIN_N:
            gap = uv['strat_rate'] - uv['user_rate']
            if gap >= MIN_GAP_PP:
                cands.append({
                    'rule_key': f'seed:user-vs-strategy-{win}',
                    'statement': (
                        f"{_WIN_CN[win]}个人判断命中率 {uv['user_rate']}%"
                        f"(n={uv['user_n']}) 显著低于策略方向 {uv['strat_rate']}%"
                        f"(n={uv['strat_n']})：直觉与该窗口策略方向冲突时，"
                        f"默认降权直觉、以策略为准，除非能写出具体反驳理由"),
                    'kind': 'meta',
                    'condition': {'sources': ['analysis_record']},
                    'stat_basis': (f"analysis_record/{win}: user {uv['user_h']}/{uv['user_n']}"
                                   f"={uv['user_rate']}% vs strat {uv['strat_h']}/{uv['strat_n']}"
                                   f"={uv['strat_rate']}%"),
                    'evidence_refs': [r.source_ref for r in rows
                                      if r.source == 'analysis_record'][:50],
                    'created_by': 'distiller',
                })
    return cands


# =============================================================================
# 报告
# =============================================================================

def build_report(rows: List[InstinctCorpus], cands: List[Dict], applied: List) -> str:
    L = [f'# 盘感 Wiki 统计蒸馏报告',
         f'',
         f'- 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}',
         f'- 语料样本：labeled 决策样本 {len(rows)} 条'
         f'（analysis_record {sum(1 for r in rows if r.source == "analysis_record")} / '
         f'trade_slot {sum(1 for r in rows if r.source == "trade_slot")}）',
         f'- 门槛：桶内 n≥{MIN_N} 且偏离同源基线≥{MIN_GAP_PP}pp 才出卡',
         f'- 显著候选：{len(cands)} 条 | 本次写库 {len(applied)} 条', '']
    for win in ('near', 'far'):
        uv = user_vs_strategy(rows, win)
        L.append(f'## {_WIN_CN[win]}：个人判断 vs 策略方向（analysis_record）')
        L.append('')
        L.append(f"- 个人：{uv['user_h']}/{uv['user_n']} = {uv['user_rate']}%"
                 f" | 策略：{uv['strat_h']}/{uv['strat_n']} = {uv['strat_rate']}%")
        L.append('')
        L.append(f'### {_WIN_CN[win]}混淆矩阵（源 × 判断 × 实际）')
        L.append('')
        L.append('| 源 | 判断 | up | down | flat | 合计 |')
        L.append('|---|---|---|---|---|---|')
        cm = confusion(rows, win)
        for src in ('analysis_record', 'trade_slot'):
            for j in ('rise', 'fall', 'watch'):
                row = cm.get((src, j), {})
                tot = sum(row.values())
                if not tot:
                    continue
                L.append(f"| {src} | {_JCN.get(j, j)} | {row.get('up', 0)} "
                         f"| {row.get('down', 0)} | {row.get('flat', 0)} | {tot} |")
        L.append('')
        L.append(f'### {_WIN_CN[win]}分桶（基线=同源全体，⚠=过门槛出卡）')
        L.append('')
        L.append('| 源 | 局面 | n | 命中率 | 同源基线 | 偏离 |')
        L.append('|---|---|---|---|---|---|')
        cand_keys = {c['rule_key'] for c in cands if c['rule_key'].startswith('stat:')}
        for source, d in sorted(bucket_stats(rows, win).items()):
            bh, bt = d['base']
            base_rate = bh / bt * 100 if bt else 0.0
            for key, (h, t, _refs) in sorted(d['buckets'].items()):
                rate = h / t * 100
                mark = '⚠ ' if (t >= MIN_N and abs(rate - base_rate) >= MIN_GAP_PP) else ''
                L.append(f'| {source} | {mark}{_bucket_label(source, key)} | {t} '
                         f'| {rate:.1f}% | {base_rate:.1f}% | {rate - base_rate:+.1f}pp |')
        L.append('')
    if cands:
        L.append('## 本次显著候选（candidate 状态，需人工确认才生效）')
        L.append('')
        for c in cands:
            L.append(f"- `{c['rule_key']}` [{c['kind']}] {c['statement']}")
    else:
        L.append('## 本次显著候选')
        L.append('')
        L.append('- （无）—— 样本量或偏离度未达门槛，宁缺毋滥；继续积累语料后复跑。')
    return '\n'.join(L) + '\n'


def run(apply: bool = False) -> Dict:
    with session_scope() as s:
        rows = load_samples(s)
    cands = generate_candidates(rows)
    applied = []
    if apply:
        for c in cands:
            rid, action = wiki_repo.upsert_rule(
                rule_key=c['rule_key'], statement=c['statement'], kind=c['kind'],
                condition=c['condition'], stat_basis=c['stat_basis'],
                evidence_refs=c['evidence_refs'], created_by=c['created_by'])
            applied.append((rid, c['rule_key'], action))
            print(f"  [{action}] id={rid} {c['rule_key']} ({c['kind']})")
    report = build_report(rows, cands, applied)
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f'[report] {REPORT_PATH}')
    print(f'样本 {len(rows)} 条 | 显著候选 {len(cands)} 条 | '
          f"写库 {len(applied)} 条（--apply 才写）")
    return {'samples': len(rows), 'candidates': cands, 'applied': applied,
            'report_path': REPORT_PATH}


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    ap = argparse.ArgumentParser(description='盘感 Wiki 统计蒸馏（批次12 P2）')
    ap.add_argument('--report', action='store_true', help='只出报告不写库')
    ap.add_argument('--apply', action='store_true', help='报告 + candidate 入库')
    ap.add_argument('--list', action='store_true', help='列出现有规则卡')
    args = ap.parse_args()
    if args.list:
        for r in wiki_repo.list_rules():
            print(f"id={r['id']:<3} [{r['status']:<9}] {r['rule_key']:<40} "
                  f"{r['kind']:<11} by={r['created_by']:<10} until={r['valid_until']}")
            print(f"    {r['statement'][:110]}")
        return
    if args.apply:
        run(apply=True)
        return
    run(apply=False)


if __name__ == '__main__':
    main()
