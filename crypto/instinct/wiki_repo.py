# -*- coding: utf-8 -*-
"""
盘感 Wiki 规则卡仓库（批次12 · P2）
===================================
instinct_wiki_rules 的 CRUD + candidate→active 状态机 + 条件匹配。

规则生命周期（方案 §1.4）：
    蒸馏/种子/人工 → candidate（不生效）
    用户在页面/API 确认 → active（进 prompt）
    valid_until 过期 → 自动降级 candidate（refresh_expired）
    人工否决 → retired（同 rule_key 再蒸馏会复活为 candidate）

rule_key 幂等合并（沿用学习任务卡"同类顺延合并"经验）：
    同 key 再蒸馏 → 更新 statement/stat_basis、evidence_refs 追加、
    不新建重复卡；active 状态不因再蒸馏被覆盖回 candidate。

条件 DSL（condition_json，对 InstinctCorpus.ctx_dict() 求值）：
    {"sources":[..], "judgment":[..], "long_dir":[..]|"..", "short_dir":..,
     "dir_flipped":0|1, "atr_pctile_min":x, "atr_pctile_max":y, "inst_id":[..]}
    空条件 {} / NULL = 恒匹配。字段缺失即不匹配（fail-closed，防误注入）。
"""

import datetime
import json
import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

from ..database import session_scope
from ..models import InstinctWikiRule

logger = logging.getLogger(__name__)

DEFAULT_VALID_DAYS = 90   # 规则卡默认有效期（U5 参数，90 天自动降级复检）
_KINDS = ('scenario', 'prohibition', 'meta')


def _now_str() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _until_str(valid_days: int) -> str:
    return (datetime.datetime.now() + datetime.timedelta(days=valid_days)
            ).strftime('%Y-%m-%d %H:%M:%S')


# =============================================================================
# 写入
# =============================================================================

def upsert_rule(rule_key: str, statement: str, kind: str = 'scenario',
                condition: Optional[Dict] = None, stat_basis: str = '',
                evidence_refs: Optional[List[str]] = None,
                status: str = 'candidate', created_by: str = 'distiller',
                supersedes_id: Optional[int] = None,
                valid_days: int = DEFAULT_VALID_DAYS) -> Tuple[int, str]:
    """按 rule_key 幂等写入。返回 (rule_id, 'created'|'merged')。

    merged 语义：文本/统计/证据刷新合并，valid_until 顺延；
    已 active 的卡不因再蒸馏降级（人工确认结果受保护）。
    """
    assert kind in _KINDS, f'未知 kind: {kind}'
    assert status in ('candidate', 'active', 'retired'), f'未知 status: {status}'
    with session_scope() as s:
        row = s.execute(
            select(InstinctWikiRule).where(InstinctWikiRule.rule_key == rule_key)
        ).scalar_one_or_none()
        refs_new = list(evidence_refs or [])
        if row is None:
            row = InstinctWikiRule(
                rule_key=rule_key, statement=statement, kind=kind,
                condition_json=json.dumps(condition or {}, ensure_ascii=False),
                stat_basis=stat_basis,
                evidence_refs=json.dumps(refs_new, ensure_ascii=False),
                status=status, created_by=created_by, supersedes_id=supersedes_id,
                valid_until=_until_str(valid_days))
            s.add(row)
            s.flush()
            return row.id, 'created'
        # ---- 合并分支 ----
        row.statement = statement or row.statement
        row.kind = kind or row.kind
        if condition is not None:
            row.condition_json = json.dumps(condition, ensure_ascii=False)
        if stat_basis:
            row.stat_basis = stat_basis
        try:
            old_refs = json.loads(row.evidence_refs or '[]')
        except (ValueError, TypeError):
            old_refs = []
        merged = old_refs + [r for r in refs_new if r not in set(old_refs)]
        row.evidence_refs = json.dumps(merged[-50:], ensure_ascii=False)  # 封顶防膨胀
        row.valid_until = _until_str(valid_days)
        if status == 'active' and row.status != 'active':
            row.status = 'active'          # 人工/种子直接激活路径
        # retired 再蒸馏 → 复活 candidate；candidate/active 各自保持
        if row.status == 'retired':
            row.status = 'candidate'
        s.flush()
        return row.id, 'merged'


def activate_rule(rule_id: int, confirmed_by: str = 'user') -> Dict:
    """candidate → active（人工确认生效）。active 幂等；retired 拒绝（需重新蒸馏）。"""
    with session_scope() as s:
        row = s.get(InstinctWikiRule, rule_id)
        if row is None:
            raise KeyError(f'rule id={rule_id} 不存在')
        if row.status == 'retired':
            raise ValueError(f'retired 规则不允许直接激活（key={row.rule_key}），请重新蒸馏')
        row.status = 'active'
        row.valid_until = _until_str(DEFAULT_VALID_DAYS)
        row.created_by = confirmed_by if row.created_by == 'distiller' else row.created_by
        return row.to_dict()


def retire_rule(rule_id: int, reason: str = '') -> Dict:
    """任意状态 → retired（人工否决/失效）。reason 追加进 evidence_refs 留痕。"""
    with session_scope() as s:
        row = s.get(InstinctWikiRule, rule_id)
        if row is None:
            raise KeyError(f'rule id={rule_id} 不存在')
        row.status = 'retired'
        if reason:
            try:
                refs = json.loads(row.evidence_refs or '[]')
            except (ValueError, TypeError):
                refs = []
            refs.append(f'retire:{reason}'[:200])
            row.evidence_refs = json.dumps(refs[-50:], ensure_ascii=False)
        return row.to_dict()


def refresh_expired(now: Optional[str] = None) -> int:
    """valid_until 过期的 active 卡自动降级 candidate（待人工复检续期或否决）。
    返回降级数量。P3 起由每日任务调用。"""
    now = now or _now_str()
    n = 0
    with session_scope() as s:
        rows = s.execute(
            select(InstinctWikiRule).where(InstinctWikiRule.status == 'active')
        ).scalars().all()
        for row in rows:
            if row.valid_until and row.valid_until < now:
                row.status = 'candidate'
                n += 1
                logger.info('[InstinctWiki] 规则过期降级: %s (%s)', row.rule_key, row.valid_until)
    return n


# =============================================================================
# 读取 / 匹配
# =============================================================================

def list_rules(status: Optional[str] = None) -> List[Dict]:
    with session_scope() as s:
        stmt = select(InstinctWikiRule).order_by(InstinctWikiRule.id.asc())
        if status:
            stmt = stmt.where(InstinctWikiRule.status == status)
        return [r.to_dict() for r in s.execute(stmt).scalars().all()]


def _cond_hit_one(cond: Dict, ctx: Dict) -> bool:
    """单条件对单快照求值。未知键一律不匹配（fail-closed）。"""
    for k, v in cond.items():
        if k == 'sources':
            if ctx.get('source') not in list(v):
                return False
        elif k in ('judgment', 'long_dir', 'short_dir', 'inst_id'):
            ctx_key = {'judgment': 'judgment', 'inst_id': 'inst_id',
                       'long_dir': 'ctx_long_dir', 'short_dir': 'ctx_short_dir'}[k]
            val = ctx.get(ctx_key, '')
            want = list(v) if isinstance(v, (list, tuple)) else [v]
            if not val or val not in want:
                return False
        elif k == 'dir_flipped':
            if ctx.get('ctx_dir_flipped') != int(v):
                return False
        elif k == 'atr_pctile_min':
            p = ctx.get('ctx_atr_pctile', -1)
            if p is None or p < 0 or p < float(v):
                return False
        elif k == 'atr_pctile_max':
            p = ctx.get('ctx_atr_pctile', -1)
            if p is None or p < 0 or p > float(v):
                return False
        else:
            logger.warning('[InstinctWiki] 未知条件键 %s → 整条规则不匹配', k)
            return False
    return True


def match_rules(ctx: Dict, status: str = 'active') -> List[Dict]:
    """快照 ctx（InstinctCorpus.ctx_dict() + judgment）命中的规则卡。
    prompt 注入方（P3）只应消费本函数返回值。"""
    out = []
    for r in list_rules(status=status):
        try:
            cond = json.loads(r.get('condition_json') or '{}')
        except (ValueError, TypeError):
            cond = {}
        if cond == {} or _cond_hit_one(cond, ctx):
            out.append(r)
    return out


def rule_to_prompt_line(r: Dict) -> str:
    """规则卡 → prompt 一行文本。只含判断期知识，不含任何样本级 outcome 数值。"""
    tag = {'scenario': '可做', 'prohibition': '勿做', 'meta': '元规则'}.get(r.get('kind'), '规则')
    basis = f"（依据：{r['stat_basis']}）" if r.get('stat_basis') else ''
    return f"[规则|{tag}|{r.get('rule_key', '')}] {r.get('statement', '')}{basis}"


def seed_from_ctx(record) -> Dict:
    """从检索/语料行提取条件匹配所需字段（ctx + judgment）。"""
    d = dict(record)
    return {k: d.get(k) for k in
            ('source', 'judgment', 'inst_id', 'ctx_long_dir', 'ctx_short_dir',
             'ctx_dir_flipped', 'ctx_atr_pctile')}
