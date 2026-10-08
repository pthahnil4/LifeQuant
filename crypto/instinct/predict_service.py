# -*- coding: utf-8 -*-
"""
盘感影子预测服务（批次12 · P3，方案 D7 的回灌半边）
====================================================
两种运行形态：
  1) backfill 历史回灌：逐条遍历语料 analysis_record 样本，以"当时时刻"为锚
     （LOO 排除自身 + anchor 时间衰减 + anchor 时点已存在的 active 规则），
     跑 prompt→网关→校验全管线；结果直接用语料行既有 outcome 结算
     （只做事后统计，永不回流 prompt），落库行 model 标记来源。
  2) predict_now 实时入口（P3 上线给调度任务调用）：锚=现在、不排除自身、
     结算留给未来满窗口任务——本文件先交付接口，调度挂载与页面随 D6/D7 后续。

管线不变式（每条都进冒烟断言）：
  - prompt 渲染输入必须 sanitize 过；渲染产物再过 assert_no_future_leak；
  - cited_rule_ids / cited_corpus_ids 引用真实性 100%；
  - mock 网关与真实网关共用同一 parse/校验出口，model 列可区分（'mock'）。

CLI：
    python -m crypto.instinct.predict_service --backfill --mock
    python -m crypto.instinct.predict_service --backfill --mock --persist
"""

import argparse
import datetime
import json
import logging
import os
import sys
from typing import Dict, List, Optional

from sqlalchemy import select

from ..database import session_scope
from ..models import InstinctCorpus, InstinctPrediction, InstinctWikiRule
from ..analysis_record_repo import (
    _JUDGMENT_EXPECT, classify_move, review_windows_for,
    _fetch_kline_module, _review_price_at)
from . import retriever as rt
from . import prompts as pm
from . import llm_gateway as gw
from . import wiki_repo as wr
from . import corpus_builder as cb

logger = logging.getLogger(__name__)


def _parse_ts(s: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.strptime(str(s)[:19], '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def _rules_at(anchor: datetime.datetime, ctx: Dict, session) -> List[Dict]:
    """anchor 时点已存在、未过期、且 condition 命中当前 ctx 的 active 规则
    （避免用"未来规则"评历史题）"""
    rows = session.execute(
        select(InstinctWikiRule).where(InstinctWikiRule.status == 'active')
    ).scalars().all()
    a_str = anchor.strftime('%Y-%m-%d %H:%M:%S')
    out = []
    for r in rows:
        created = r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else ''
        if created > a_str:
            continue                      # anchor 之后才被蒸馏出来
        if r.valid_until and r.valid_until < a_str:
            continue                      # anchor 时已过期
        try:
            cond = json.loads(r.condition_json or '{}')
        except (ValueError, TypeError):
            cond = {}
        if cond and not wr._cond_hit_one(cond, ctx):
            continue                      # 条件不命中当前局面
        out.append(r.to_dict())
    return out


def _collect_forbidden(orig: Dict) -> List[str]:
    """从含 outcome 的原始行收集"答案值"（数值类），供 prompt 文本扫描。
    只取数值与价格串：up/down/flat 之类单词太泛会误伤，数值才是泄漏主通道。"""
    vals = []
    for k in ('chg_near_pct', 'chg_far_pct', 'price_1h', 'price_4h'):
        v = orig.get(k)
        if v not in (None, '', 0, 0.0):
            vals.append(f'{float(v):.2f}')
    return vals


def predict_from_ctx(ctx: Dict, anchor: Optional[datetime.datetime],
                     exclude_self_ref: str, sources=('analysis_record',),
                     use_mock: bool = True, session=None) -> Dict:
    """单样本全管线：检索→规则→渲染→泄漏扫描→网关→引用校验。

    返回 {ok, pred, meta_cognition, prompt_len, model, latency_ms, cases,
          rules, error}；任何一步失败都不落库、不抛到调用方之外。
    """
    own = session is None
    s = session
    try:
        if own:
            s = session_scope().__enter__()
        anchor = anchor or datetime.datetime.now()
        cases = rt.retrieve(ctx, exclude_self_ref=exclude_self_ref,
                            anchor=anchor, sources=sources, session=s)
        case_ids = [c['id'] for c in cases]
        ctx_only = rt.sanitize_case_for_prompt(ctx)
        clean_cases = [rt.sanitize_case_for_prompt(c) for c in cases]
        rules = _rules_at(anchor, ctx, s)
        profile = pm.build_profile(cb._P0_FROZEN)
        prompt_text, _ = pm.render_prompt(ctx_only, clean_cases, rules, profile=profile)
        # 泄漏扫描：用"原始未 sanitize"行里的答案值做负例检查
        forbidden = _collect_forbidden(ctx)
        for c in cases:
            forbidden += _collect_forbidden(c)
        rt.assert_no_future_leak(prompt_text, forbidden)
        if use_mock:
            pred, latency, model = gw.mock_chat(prompt_text, clean_cases, rules)
        else:
            pred, latency, model = gw.chat(prompt_text)
        gw.assert_citations_real(pred, n_rules=len(rules), n_cases=len(cases))
        # 编号 → 真实 DB id（落库审计用）
        real_rule_ids = [rules[i - 1]['id'] for i in pred['cited_rule_ids']]
        real_corpus_ids = [case_ids[i - 1] for i in pred['cited_corpus_ids']]
        return {'ok': True, 'pred': pred, 'prompt_len': len(prompt_text),
                'model': model, 'latency_ms': latency,
                'n_cases': len(cases), 'n_rules': len(rules),
                'real_rule_ids': real_rule_ids, 'real_corpus_ids': real_corpus_ids,
                'corpus_ref': ctx.get('source_ref', ''), 'error': ''}
    except (gw.ResponseInvalidError, gw.GatewayConfigError,
            AssertionError, ValueError) as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    except Exception as e:  # noqa: BLE001 DB/网络等底层异常统一收口
        logger.exception('[instinct] predict_from_ctx 意外失败')
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    finally:
        if own and s is not None:
            s.__exit__(None, None, None)


def _settle_row(pred: Dict, orig: Dict) -> Dict:
    """回灌题的答案本就躺在语料行里：即时结算（仅统计，绝不再进 prompt）"""
    j = pred['pred']['judgment'] if isinstance(pred.get('pred'), dict) else ''
    out = {}
    for win in ('near', 'far'):
        o = orig.get(f'outcome_{win}') or ''
        out[f'actual_{win}'] = o
        out[f'hit_{win}'] = (_JUDGMENT_EXPECT.get(j, '?') == o) if (o and j) else None
    return out


def backfill(limit: Optional[int] = None, persist: bool = False,
             use_mock: bool = True) -> Dict:
    """遍历语料 analysis_record 样本回灌影子预测。persist 时写 instinct_predictions
    （status=scored，model 标记 mock/真模型；A/B 统计侧应 where model!='mock'）。"""
    stats = {'total': 0, 'ok': 0, 'fail': 0, 'leak_blocked': 0, 'other_fail': 0,
             'persisted': 0, 'by_judgment': {}}
    with session_scope() as s:
        rows = s.execute(
            select(InstinctCorpus)
            .where(InstinctCorpus.source == 'analysis_record',
                   InstinctCorpus.judgment != '')
            .order_by(InstinctCorpus.id.asc())
            .limit(limit or 1000)
        ).scalars().all()
        samples = [rt._row_ctx(r) for r in rows]
    for c in samples:
        stats['total'] += 1
        res = predict_from_ctx(c, anchor=_parse_ts(c['ts']),
                               exclude_self_ref=c['source_ref'],
                               use_mock=use_mock)
        if res.get('ok'):
            stats['ok'] += 1
            j = res['pred']['judgment']
            stats['by_judgment'][j] = stats['by_judgment'].get(j, 0) + 1
            if persist:
                settle = _settle_row(res, c)
                with session_scope() as s2:
                    s2.add(InstinctPrediction(
                        ts=c['ts'], inst_id=c['inst_id'],
                        ctx_snapshot_json=json.dumps(
                            {'anchor_corpus_ref': c['source_ref'],
                             'ctx': rt.sanitize_case_for_prompt(c)},
                            ensure_ascii=False)[:60000],
                        judgment=j, confidence=res['pred']['confidence'],
                        rationale=res['pred']['rationale']
                                  + (f" ｜meta: {res['pred']['meta_cognition']}"
                                     if res['pred']['meta_cognition'] else ''),
                        cited_rule_ids=','.join(map(str, res['real_rule_ids']))[:255],
                        cited_corpus_ids=','.join(map(str, res['real_corpus_ids']))[:255],
                        retrieved_refs_json=json.dumps(
                            {'case_ids': res['real_corpus_ids'],
                             'n_rules': res['n_rules']})[:255],
                        model=res['model'], latency_ms=res['latency_ms'],
                        price_at_pred=float(c.get('ctx_price') or 0),
                        status='scored',
                        actual_near=settle['actual_near'], actual_far=settle['actual_far'],
                        hit_near=settle['hit_near'], hit_far=settle['hit_far']))
                stats['persisted'] += 1
        else:
            stats['fail'] += 1
            if 'leak' in res.get('error', '').lower():
                stats['leak_blocked'] += 1
            else:
                stats['other_fail'] += 1
            print(f"  [FAIL] {c['source_ref']}: {res['error']}")
    print(f"\n[BACKFILL] 总 {stats['total']} | 成功 {stats['ok']} "
          f"| 失败 {stats['fail']}（其中泄漏拦截 {stats['leak_blocked']}）| "
          f"落库 {stats['persisted']}{'（persist）' if persist else '（未落库）'}")
    print(f"  判断分布: {stats['by_judgment']}")
    if stats['total'] and stats['fail'] == 0:
        print('  ✅ 解析失败率 0%（D5 管线验收；真实网关接入后需重跑）')
    return stats


# =============================================================================
# 实时影子预测（D7：整点批任务 + 满窗口惰性结算）
# =============================================================================

_TRADING_CFG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'task', 'config', 'config_trend_range.json')


def _load_trading_cfg() -> Dict:
    """交易配置读取（与 app._load_trading_config 同源顺序：DB 全局 key → 本地文件）"""
    try:
        from .. import config_store_repo
        cfg = config_store_repo.load_strategy_config_cached()
        if cfg:
            return cfg
    except Exception:
        pass
    with open(_TRADING_CFG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def build_live_ctx(inst_id: str) -> Dict:
    """用实盘同源的双周期引擎构造"当前时刻"决策快照（与语料 ctx 同构）。

    只读行情分析，不触碰任何交易执行接口——instinct 包边界不变。
    """
    cfg = _load_trading_cfg()
    cur = next((c for c in cfg.get('currencies', [])
                if c.get('instId') == inst_id), None)
    if not cur:
        raise KeyError(f'交易配置中不存在币种 {inst_id}')
    short_period = cur.get('short_period', '5m')
    long_period = cur.get('long_period', '4H')
    rcfg = cur.get('range_position') or {}
    from ..task.strategy_adapter import DualPeriodStrategyAdapter
    analysis = DualPeriodStrategyAdapter().analyze(
        inst_id, short_period, long_period,
        boll_period=int(rcfg.get('boll_period', 20) or 20),
        boll_dev=float(rcfg.get('boll_dev', 2.0) or 2.0),
        signal_algo=cur.get('signal_algo'),
        entry_price_type=cur.get('entry_price_type', 'close'),
        exit_price_type=cur.get('exit_price_type', 'open'))
    if not analysis:
        raise RuntimeError(f'{inst_id} 策略分析返回空，快照不可用')
    atr = float(analysis.get('atr_percentage') or 0)
    # ATR 分位：该币语料历史序列（与 builder 同一 _atr_pctile 口径）
    with session_scope() as s:
        hist = [float(v) for v in s.execute(
            select(InstinctCorpus.ctx_atr_pct)
            .where(InstinctCorpus.inst_id == inst_id,
                   InstinctCorpus.ctx_atr_pct > 0)
        ).scalars().all()]
    long_dir = analysis.get('long_direction') or ''
    long_prev = analysis.get('long_prev_direction') or ''
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return {
        'source': 'live_snapshot', 'source_ref': f'live:{inst_id}:{now_str}',
        'ts': now_str, 'inst_id': inst_id,
        'short_period': short_period, 'long_period': long_period,
        'ctx_short_dir': analysis.get('direction') or '',
        'ctx_long_dir': long_dir, 'ctx_long_dir_prev': long_prev,
        'ctx_atr_pct': round(atr, 4),
        'ctx_atr_pctile': cb._atr_pctile(hist, atr),
        'ctx_dir_flipped': bool(long_prev and long_dir and long_prev != long_dir),
        'ctx_price': float(analysis.get('last_price') or 0),
        'ctx_text': '', 'judgment': '',
    }


def predict_now(inst_id: str, persist: bool = True) -> Dict:
    """实时入口：当前快照 → 全管线 → 落 pending 行（结算交给 settle_pending）。

    未配置 LLM 时抛 GatewayConfigError（由调用方/批任务收口，不假成功）。
    """
    ctx = build_live_ctx(inst_id)
    res = predict_from_ctx(ctx, anchor=None, exclude_self_ref='', use_mock=False)
    if not res.get('ok') or not persist:
        return res
    with session_scope() as s:
        row = InstinctPrediction(
            ts=ctx['ts'], inst_id=inst_id,
            ctx_snapshot_json=json.dumps(
                {'ctx': rt.sanitize_case_for_prompt(ctx), 'mode': 'live'},
                ensure_ascii=False)[:60000],
            judgment=res['pred']['judgment'],
            confidence=res['pred']['confidence'],
            rationale=res['pred']['rationale']
                      + (f" ｜meta: {res['pred']['meta_cognition']}"
                         if res['pred']['meta_cognition'] else ''),
            cited_rule_ids=','.join(map(str, res['real_rule_ids']))[:255],
            cited_corpus_ids=','.join(map(str, res['real_corpus_ids']))[:255],
            retrieved_refs_json=json.dumps(
                {'case_ids': res['real_corpus_ids'],
                 'n_rules': res['n_rules']})[:255],
            model=res['model'], latency_ms=res['latency_ms'],
            price_at_pred=ctx['ctx_price'], status='pending')
        s.add(row)
        s.flush()
        res['prediction_id'] = row.id
    return res


def settle_pending(limit: int = 100) -> int:
    """满窗口惰性结算：pending 预测行到窗后补近/远 follow 价并判 hit。

    口径与 task_analysis_records 复盘完全一致（review_windows_for 动态窗口 +
    classify_move + _JUDGMENT_EXPECT）；bar 未收线静默跳过，下轮重试。
    近远两窗都结算完才置 scored。
    """
    now = datetime.datetime.now()
    settled = 0
    single_mod = None
    with session_scope() as s:
        rows = s.execute(
            select(InstinctPrediction)
            .where(InstinctPrediction.status == 'pending')
            .order_by(InstinctPrediction.ts.asc())
            .limit(limit)
        ).scalars().all()
        for r in rows:
            try:
                snap = json.loads(r.ctx_snapshot_json or '{}')
                ctx = snap.get('ctx') or {}
                t0 = _parse_ts(r.ts)
                if t0 is None:
                    continue
                atr = float(ctx.get('ctx_atr_pct') or 0)
                windows = review_windows_for(ctx.get('short_period') or '')
                if single_mod is None:
                    single_mod = _fetch_kline_module()
                done_all = True
                for (win, bar, offset, price_col) in (
                        ('near', windows[0][2], windows[0][3], 'price_near'),
                        ('far', windows[1][2], windows[1][3], 'price_far')):
                    if getattr(r, price_col) is not None:
                        continue
                    target = t0 + datetime.timedelta(minutes=offset)
                    if now < target:
                        done_all = False
                        continue
                    price, _bar_ts = _review_price_at(single_mod, r.inst_id, bar, target)
                    if price is None:
                        done_all = False
                        continue
                    outcome = classify_move(r.price_at_pred, price, atr,
                                            win == 'far') or ''
                    setattr(r, price_col, price)
                    setattr(r, f'actual_{win}', outcome)
                    setattr(r, f'hit_{win}',
                           _JUDGMENT_EXPECT.get(r.judgment, '?') == outcome
                           if (outcome and r.judgment) else None)
                    settled += 1
                if done_all and r.price_near is not None and r.price_far is not None:
                    r.status = 'scored'
            except Exception as e:  # noqa: BLE001 单行失败不影响批次
                logger.warning('[instinct] 结算失败 id=%s: %s', r.id, e)
    return settled


def run_instinct_hourly() -> Dict:
    """整点批任务入口（scheduler 挂载）：串行逐币影子预测 + 惰性结算 + 规则过期。

    未配置 LLM → 直接 skip（设计上绝不假成功）；单币失败记录后继续下一币。
    """
    if not gw.is_configured():
        return {'skipped': 'LLM 未配置（CRYPTO_LLM_*），本轮影子预测跳过'}
    result = {'ok': [], 'fail': [], 'settled_fields': 0, 'expired_rules': 0}
    try:
        result['settled_fields'] = settle_pending()
    except Exception as e:  # noqa: BLE001
        logger.warning('[instinct] 结算批次失败: %s', e)
    try:
        cfg = _load_trading_cfg()
        for cur in cfg.get('currencies', []):
            inst_id = str(cur.get('instId') or '').strip()
            if not inst_id:
                continue
            try:   # 批量串行：规避 DualPeriodStrategyAdapter 全局状态竞争
                r = predict_now(inst_id)
                (result['ok'] if r.get('ok') else result['fail']).append(
                    {'inst': inst_id, **({k: r[k] for k in ('prediction_id', 'model')
                                          if k in r} or {}),
                     **({} if r.get('ok') else {'err': r.get('error', '')})})
            except Exception as e:  # noqa: BLE001
                result['fail'].append({'inst': inst_id, 'err': str(e)})
    except Exception as e:  # noqa: BLE001
        logger.warning('[instinct] 交易配置不可读，本轮跳过: %s', e)
    try:
        result['expired_rules'] = wr.refresh_expired()
    except Exception as e:  # noqa: BLE001
        logger.warning('[instinct] 规则过期检查失败: %s', e)
    logger.info('[instinct] 整点影子预测: ok=%d fail=%d 结算字段=%d',
                len(result['ok']), len(result['fail']), result['settled_fields'])
    return result


JOB_PREDICT_ID = 'instinct_shadow_predict'
JOB_SETTLE_ID = 'instinct_settle'


def register_instinct_jobs():
    """注册影子预测整点任务 + 10 分钟结算任务。

    双闸门：CRYPTO_INSTINCT_AUTO=0 显式关闭；未配 LLM key 不注册
    （注册了也是整天空转，不如干净缺席任务列表）。
    返回 (job_id|None, 说明)，模式同 memory_daily_reset。
    """
    if str(os.environ.get('CRYPTO_INSTINCT_AUTO', '1')).lower() in ('0', 'false', 'off'):
        return None, '已关闭（CRYPTO_INSTINCT_AUTO=0）'
    if not gw.is_configured():
        return None, 'LLM 未配置（CRYPTO_LLM_*），任务未注册'
    try:
        from ..task.scheduler import task_scheduler
    except Exception as e:  # noqa: BLE001
        return None, f'调度器不可用: {e}'
    task_scheduler.register_job(
        run_instinct_hourly, trigger='cron', minute=0,
        job_id=JOB_PREDICT_ID, job_name='盘感影子预测（整点·串行·仅记录不交易）')
    task_scheduler.register_job(
        settle_pending, trigger='interval', minutes=10,
        job_id=JOB_SETTLE_ID, job_name='盘感影子预测满窗结算')
    return JOB_PREDICT_ID, '已注册（整点预测 + 10 分钟结算）'


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    ap = argparse.ArgumentParser(description='盘感影子预测（批次12 P3）')
    ap.add_argument('--backfill', action='store_true', help='历史回灌干跑')
    ap.add_argument('--mock', action='store_true',
                    help='使用确定性 mock 网关（无 LLM key 时验证管线）')
    ap.add_argument('--persist', action='store_true', help='回灌结果写入 instinct_predictions')
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args()
    if args.backfill:
        if not args.mock and not gw.is_configured():
            print('[WARN] 未配置 CRYPTO_LLM_* 且未加 --mock：自动降级为 mock 干跑')
            args.mock = True
        stats = backfill(limit=args.limit, persist=args.persist, use_mock=args.mock)
        raise SystemExit(0 if stats['total'] and not stats['fail'] else 1)
    ap.print_help()


if __name__ == '__main__':
    main()
