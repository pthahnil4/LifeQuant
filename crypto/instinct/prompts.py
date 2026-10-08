# -*- coding: utf-8 -*-
"""
盘感提示词渲染（批次12 · P3，方案 §1.4 四段式）
================================================
SYSTEM(画像+纪律) → WIKI(active 规则卡) → CASES(topK 相似案例) → NOW(当前快照)

防泄漏执行线（v1 裁决，比 §1.4 原稿更保守）：
  - 案例只给"当时可见信息 + 当时的判断"，**不给事后 actual/hit 注记**——
    邻居命中标签在检索统计门证明判别力之前（当前 n=15 无判别力）注入，
    只会诱导模型复读多数类标签，属有害噪声；
  - 输入含 outcome_/hit_/chg_/labeled/price_1h/price_4h 键 → strict 模式直接
    抛 ValueError（fail-closed，宁可渲染失败也不静默剥键）；
  - 渲染产物附带 forbidden_values（各案例 outcome 值的字符串化集合），
    供 assert_no_future_leak 做最终文本扫描。

输出 JSON schema（llm_gateway.parse_prediction 校验）：
  {judgment: rise|watch|fall, confidence: 0~1, rationale: ≤120字,
   cited_rule_ids: [], cited_corpus_ids: [], meta_cognition: str}
"""

import logging
from typing import Dict, List, Tuple

from .retriever import _FORBIDDEN_PREFIX

logger = logging.getLogger(__name__)

_DIR_CN = {'long': '做多', 'short': '做空', '': '—'}
_JCN = {'rise': '看涨', 'fall': '看跌', 'watch': '看横盘/观望', '': '—'}

SYSTEM_PERSONA = (
    "你是加密货币交易员本人的盘感模拟器（影子模式，不执行任何交易）。\n"
    "你的任务：给定与历史决策时刻同构的行情上下文，输出他本人可能的方向判断，"
    "并在其个人历史弱势局面下给出元认知校准。\n"
    "纪律：\n"
    "1) 只依据提供的『当时可见信息』推理，禁止臆测任何事后结果；\n"
    "2) 输出严格 JSON（字段见末尾要求），judgment ∈ {rise, fall, watch}；\n"
    "3) 结论必须引用案例编号(cases)或规则编号(rules)，无充分依据时 "
    "judgment=watch 且 confidence≤0.4；\n"
    "4) 若 WIKI 段存在元规则提示你该局面个人历史弱于策略，"
    "meta_cognition 字段必须写明冲突与取舍。"
)


def _is_leaky_key(k: str) -> bool:
    return str(k).startswith(_FORBIDDEN_PREFIX) or str(k).startswith('price_1') \
        or str(k).startswith('price_4')


def _strict_scan(tag: str, d: Dict):
    bad = [k for k in d if _is_leaky_key(k)]
    if bad:
        raise ValueError(f'prompt 渲染输入含事后字段（fail-closed）: {tag} -> {bad}')


def _fmt_ctx_line(tag: str, ctx: Dict) -> str:
    """快照 → 单行同构描述（案例与 NOW 段共用，保证模型看到的字段一致）"""
    pct = ctx.get('ctx_atr_pctile', -1)
    pct_s = '未知' if pct is None or pct < 0 else f'{float(pct):.2f}'
    return (f"{tag} 时间={ctx.get('ts', '?')} 币种={ctx.get('inst_id') or '—'} "
            f"周期={ctx.get('short_period') or '—'}/{ctx.get('long_period') or '—'} "
            f"短期方向={_DIR_CN.get(ctx.get('ctx_short_dir', ''), '—')} "
            f"长期方向={_DIR_CN.get(ctx.get('ctx_long_dir', ''), '—')}"
            f"({'刚翻转' if ctx.get('ctx_dir_flipped') else '未翻转'}) "
            f"ATR%={ctx.get('ctx_atr_pct', 0):.2f} ATR分位={pct_s} "
            f"价格={ctx.get('ctx_price', 0) or '—'}")


def render_prompt(ctx: Dict, cases: List[Dict], rules: List[Dict],
                  profile: str = '', strict: bool = True) -> Tuple[str, List[str]]:
    """渲染四段式 user prompt。返回 (prompt_text, forbidden_values)。

    ctx/cases 必须是 retriever.sanitize_case_for_prompt() 之后的形态；
    strict=True 时任何事后键直接抛 ValueError（冒烟负例断言依赖此行为）。
    """
    if strict:
        _strict_scan('ctx', ctx)
        for i, c in enumerate(cases):
            _strict_scan(f'cases[{i}]', c)
    # 收集禁值（供最终文本扫描）：不注入 prompt，只登记"答案是什么"
    forbidden: List[str] = []
    for src in [ctx] + cases:
        for k in list(src.keys()):
            if _is_leaky_key(k):
                v = src[k]
                if v not in (None, '', -1, 0.0):
                    forbidden.append(str(v))

    L: List[str] = [SYSTEM_PERSONA, '']
    L.append('## WIKI 段 · 生效规则卡（判断期知识，编号引用）')
    if rules:
        for i, r in enumerate(rules, 1):
            basis = f"（依据：{r['stat_basis']}）" if r.get('stat_basis') else ''
            L.append(f"[R{i}] ({r.get('rule_key', '')}, {r.get('kind', '')}) "
                     f"{r.get('statement', '')}{basis}")
    else:
        L.append('（当前局面无命中规则）')
    L.append('')
    L.append('## CASES 段 · 相似历史局面（仅当时可见信息 + 当时判断）')
    if cases:
        for i, c in enumerate(cases, 1):
            L.append(_fmt_ctx_line(f"[C{i}]", c))
            extra = f"    当时判断：{_JCN.get(c.get('judgment', ''), '—')}"
            text = (c.get('ctx_text') or c.get('decision_text') or '').strip()
            if text:
                extra += f'；原话：{text[:80]}'
            L.append(extra)
    else:
        L.append('（无相似案例）')
    L.append('')
    L.append('## NOW 段 · 当前待判断快照')
    L.append(_fmt_ctx_line('[NOW]', ctx))
    if (ctx.get('ctx_text') or '').strip():
        L.append(f"    当时的思考记录：{ctx['ctx_text'].strip()[:120]}")
    if profile:
        L.append('')
        L.append(f'## 画像段 · 个人历史统计（compute_stats 口径）\n{profile}')
    L.append('')
    L.append('## 输出要求（仅输出 JSON，无其他文本）')
    L.append('{"judgment": "rise|fall|watch", "confidence": 0.0, '
             '"rationale": "≤120字", "cited_rule_ids": [], '
             '"cited_corpus_ids": [], '
             '"meta_cognition": "该局面个人 vs 策略强弱与取舍；无冲突可留空"}')
    L.append('（cited_* 填纯整数数组：引用某条时只填其编号数字，不要把 C/R 标签文字填进来）')
    return '\n'.join(L), forbidden


def build_profile(stats: Dict) -> str:
    """compute_stats/蒸馏口径的个人画像行（判断期知识：仅统计历史命中率结构）"""
    return (f"近窗口：个人 {stats.get('user_near', '—')}% vs 策略 {stats.get('strat_near', '—')}%；"
            f"远窗口：个人 {stats.get('user_far', '—')}% vs 策略 {stats.get('strat_far', '—')}%"
            f"（历史快照统计，来自语料库回算）")
