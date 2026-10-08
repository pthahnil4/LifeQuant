# -*- coding: utf-8 -*-
"""
盘感模块 HTTP 路由（批次12 · P3，方案 D6）
==========================================
/instinct 页面 + JSON API。访问鉴权由全局 web_auth 闸门（app.py
before_request）统一覆盖，本模块不另设口令。

边界铁律：全部接口只读行情/只写 instinct_* 表，不 import 任何下单交易模块；
predict 是"影子预测"——只生成判断流水，永不触达委托链路。
"""

import datetime
import logging

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import delete, select

from .database import session_scope
from .models import InstinctCorpus, InstinctPrediction
from .analysis_record_repo import _JUDGMENT_EXPECT, _DIR_EXPECT
from .instinct import retriever as rt
from .instinct import wiki_repo as wr
from .instinct import llm_gateway as gw
from .instinct import predict_service as ps
from .instinct import corpus_builder as cb

logger = logging.getLogger(__name__)

instinct_bp = Blueprint('instinct_bp', __name__, url_prefix='/instinct')

_JCN = {'rise': '看涨', 'fall': '看跌', 'watch': '观望'}


def _err(msg: str, code: int = 400):
    return jsonify({'code': code, 'message': msg, 'data': None})


# =============================================================================
# 页面
# =============================================================================

@instinct_bp.route('/', methods=['GET'])
@instinct_bp.route('', methods=['GET'])
def page_instinct():
    return render_template('instinct.html', active_page='instinct')


@instinct_bp.route('/wiki', methods=['GET'])
def page_instinct_wiki():
    """Wiki 管理台：规则卡治理 / 预测审计 / 校准趋势 / 检索打分明细调试。"""
    return render_template('instinct_wiki.html', active_page='instinct')


# =============================================================================
# 行情快照 + 相似案例 + 命中规则（只读）
# =============================================================================

@instinct_bp.route('/api/currencies', methods=['GET'])
def api_currencies():
    """交易配置币种清单（页面下拉数据源，只读）。"""
    try:
        cfg = ps._load_trading_cfg()
        items = [c.get('instId') for c in cfg.get('currencies', [])
                 if str(c.get('instId') or '').strip()]
        return jsonify({'code': 200, 'message': 'success', 'data': {'items': items}})
    except Exception as e:  # noqa: BLE001
        return _err(f'交易配置不可读: {e}', 500)


@instinct_bp.route('/api/now', methods=['GET'])
def api_now():
    """当前局面一屏：实盘同源快照 + topK 相似历史 + active 规则命中。"""
    inst_id = (request.args.get('instId') or '').strip()
    if not inst_id:
        return _err('缺少 instId 参数')
    try:
        ctx = ps.build_live_ctx(inst_id)
    except (KeyError, RuntimeError, FileNotFoundError) as e:
        return _err(str(e), 404 if isinstance(e, KeyError) else 500)
    cases = rt.retrieve(ctx)
    # sanitize 会剥掉展示用的 score 键（ similarity 由 ctx 计算、不涉密），此处挂回供前端渲染
    clean = [dict(rt.sanitize_case_for_prompt(c), score=c.get('score')) for c in cases]
    rules = wr.match_rules(ctx)
    return jsonify({'code': 200, 'message': 'success', 'data': {
        'ctx': {k: v for k, v in ctx.items() if k.startswith(('ctx_', 'ts', 'inst_id', 'short_period', 'long_period'))},
        'cases': [{'score': c['score'], 'ts': c['ts'], 'inst_id': c['inst_id'],
                   'judgment_cn': _JCN.get(c.get('judgment', ''), '—'),
                   'long_dir': c.get('ctx_long_dir', ''), 'flipped': bool(c.get('ctx_dir_flipped')),
                   'text': (c.get('ctx_text') or c.get('decision_text') or '')[:60]}
                  for c in clean],
        'rules': [{'id': r['id'], 'rule_key': r['rule_key'], 'kind': r['kind'],
                   'statement': r['statement'], 'stat_basis': r['stat_basis']}
                  for r in rules],
        'llm_configured': gw.is_configured(),
    }})


@instinct_bp.route('/api/predict', methods=['POST'])
def api_predict():
    """手动触发一次实时影子预测（落 pending 行，等窗口结算）。"""
    body = request.get_json(silent=True) or {}
    inst_id = str(body.get('instId') or request.args.get('instId') or '').strip()
    if not inst_id:
        return _err('缺少 instId')
    if not gw.is_configured():
        return _err('LLM 未配置（环境变量 CRYPTO_LLM_BASE_URL/API_KEY/MODEL），'
                    '影子预测暂不可用；可用 CLI --backfill --mock 验证管线', 503)
    try:
        res = ps.predict_now(inst_id, persist=True)
    except Exception as e:  # noqa: BLE001
        logger.exception('[instinct] predict 失败')
        return _err(f'预测失败: {e}', 500)
    if not res.get('ok'):
        return _err(res.get('error', '未知失败'), 502)
    p = res['pred']
    return jsonify({'code': 200, 'message': 'success', 'data': {
        'prediction_id': res.get('prediction_id'), 'ts': datetime.datetime.now().isoformat(timespec='seconds'),
        'judgment': p['judgment'], 'judgment_cn': _JCN.get(p['judgment'], p['judgment']),
        'confidence': p['confidence'], 'rationale': p['rationale'],
        'meta_cognition': p['meta_cognition'], 'model': res['model'],
        'latency_ms': res['latency_ms'], 'n_cases': res['n_cases'], 'n_rules': res['n_rules'],
    }})


# =============================================================================
# 规则卡管理（candidate→active 由人确认，这是全模块唯一的"人治"入口）
# =============================================================================

@instinct_bp.route('/api/rules', methods=['GET'])
def api_rules():
    status = (request.args.get('status') or '').strip() or None
    rows = wr.list_rules(status=status)
    counts = {}
    for r in wr.list_rules():
        counts[r['status']] = counts.get(r['status'], 0) + 1
    return jsonify({'code': 200, 'message': 'success',
                    'data': {'rules': rows, 'counts': counts,
                             'llm_configured': gw.is_configured()}})


@instinct_bp.route('/api/expire', methods=['POST'])
def api_expire():
    """valid_until 过期的 active 卡降级回 candidate（与整点任务同一入口的手动版）。"""
    try:
        return jsonify({'code': 200, 'message': 'success',
                        'data': {'degraded': wr.refresh_expired()}})
    except Exception as e:  # noqa: BLE001
        logger.exception('[instinct] 过期检查失败')
        return _err(f'过期检查失败: {e}', 500)


@instinct_bp.route('/api/rules/<int:rid>/confirm', methods=['POST'])
def api_rule_confirm(rid):
    """candidate → active（人工确认生效）。"""
    try:
        return jsonify({'code': 200, 'message': 'success',
                        'data': wr.activate_rule(rid, confirmed_by='user')})
    except KeyError:
        return _err(f'规则 {rid} 不存在', 404)
    except ValueError as e:
        return _err(str(e))


@instinct_bp.route('/api/rules/<int:rid>/retire', methods=['POST'])
def api_rule_retire(rid):
    """否决/下线规则（附原因写进证据链）。"""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({'code': 200, 'message': 'success',
                        'data': wr.retire_rule(rid, reason=str(body.get('reason') or ''))})
    except KeyError:
        return _err(f'规则 {rid} 不存在', 404)


# =============================================================================
# 影子预测流水 + A/B 统计（D9 后端；样本不足如实标注，不给结论性文案）
# =============================================================================

@instinct_bp.route('/api/predictions', methods=['GET'])
def api_predictions():
    limit = min(int(request.args.get('limit') or 30), 200)
    with session_scope() as s:
        rows = s.execute(
            select(InstinctPrediction)
            .order_by(InstinctPrediction.id.desc()).limit(limit)
        ).scalars().all()
        items = [{
            'id': r.id, 'ts': r.ts, 'inst_id': r.inst_id, 'model': r.model,
            'judgment': r.judgment, 'judgment_cn': _JCN.get(r.judgment, r.judgment),
            'confidence': r.confidence, 'rationale': (r.rationale or '')[:240],
            'status': r.status, 'price_at_pred': r.price_at_pred,
            'actual_near': r.actual_near, 'actual_far': r.actual_far,
            'hit_near': r.hit_near, 'hit_far': r.hit_far,
            'cited_rule_ids': r.cited_rule_ids, 'cited_corpus_ids': r.cited_corpus_ids,
            'user_trusted': r.user_trusted, 'user_agree': r.user_agree,
            'latency_ms': r.latency_ms,
        } for r in rows]
    return jsonify({'code': 200, 'message': 'success', 'data': {'items': items}})


@instinct_bp.route('/api/predictions/<int:pid>/feedback', methods=['POST'])
def api_feedback(pid):
    """你对这条影子预测的态度：可信吗 / 与你判断一致吗（元认知校准数据源）。"""
    body = request.get_json(silent=True) or {}
    with session_scope() as s:
        r = s.get(InstinctPrediction, pid)
        if r is None:
            return _err(f'预测 {pid} 不存在', 404)
        for col in ('user_trusted', 'user_agree'):
            if col in body and body[col] is not None:
                setattr(r, col, bool(body[col]))
        return jsonify({'code': 200, 'message': 'success', 'data': {
            'id': r.id, 'user_trusted': r.user_trusted, 'user_agree': r.user_agree}})


@instinct_bp.route('/api/predictions/delete', methods=['POST'])
def api_predictions_delete():
    """批量删除影子预测流水（仅动 instinct_predictions 表；不影响情景语料/规则卡）。"""
    body = request.get_json(silent=True) or {}
    raw = body.get('ids') or []
    if not isinstance(raw, list):
        return _err('ids 必须为整数数组')
    try:
        ids = sorted({int(x) for x in raw})
    except (TypeError, ValueError):
        return _err('ids 必须为整数数组')
    if not ids:
        return _err('未指定要删除的预测 id')
    if len(ids) > 1000:
        return _err('单次删除上限 1000 条')
    try:
        with session_scope() as s:
            deleted = s.execute(
                delete(InstinctPrediction).where(InstinctPrediction.id.in_(ids))
            ).rowcount
        return jsonify({'code': 200, 'message': 'success', 'data': {'deleted': deleted}})
    except Exception as e:  # noqa: BLE001
        logger.exception('[instinct] 删除预测失败')
        return _err(f'删除失败: {e}', 500)


@instinct_bp.route('/api/predictions/clear', methods=['POST'])
def api_predictions_clear():
    """清空全部影子预测流水（须 confirm=true 显式确认；只清预测，不动语料/规则）。"""
    body = request.get_json(silent=True) or {}
    if not body.get('confirm'):
        return _err('需显式确认 (confirm=true) 才能清空全部预测流水')
    try:
        with session_scope() as s:
            cleared = s.execute(delete(InstinctPrediction)).rowcount
        return jsonify({'code': 200, 'message': 'success', 'data': {'cleared': cleared}})
    except Exception as e:  # noqa: BLE001
        logger.exception('[instinct] 清空预测失败')
        return _err(f'清空失败: {e}', 500)


@instinct_bp.route('/api/settle', methods=['POST'])
def api_settle():
    """手动触发满窗口结算（整点任务之外的即时补齐入口）。"""
    try:
        return jsonify({'code': 200, 'message': 'success',
                        'data': {'filled': ps.settle_pending()}})
    except Exception as e:  # noqa: BLE001
        logger.exception('[instinct] 结算失败')
        return _err(f'结算失败: {e}', 500)


@instinct_bp.route('/api/stats', methods=['GET'])
def api_stats():
    """三列 A/B：你本人 / 策略方向 / LLM 影子（mock 一律排除）。"""
    with session_scope() as s:
        rows = [rt._row_ctx(r) for r in s.execute(
            select(InstinctCorpus)).scalars().all()]
        preds = s.execute(
            select(InstinctPrediction.judgment, InstinctPrediction.hit_near,
                   InstinctPrediction.hit_far, InstinctPrediction.status,
                   InstinctPrediction.user_trusted, InstinctPrediction.user_agree)
            .where(InstinctPrediction.model != 'mock')
        ).all()
    # 三列命中率
    tar = [r for r in rows if r['source'] == 'analysis_record']
    def _rate(seq):
        seq = [v for v in seq if v is not None]
        return {'n': len(seq), 'rate': round(sum(seq) / len(seq) * 100, 1) if seq else None}
    user = {'near': [], 'far': []}
    strat = {'near': [], 'far': []}
    for r in tar:
        if r.get('labeled') and r.get('judgment') in _JUDGMENT_EXPECT:
            user['near'].append(_JUDGMENT_EXPECT[r['judgment']] == r.get('outcome_near'))
            user['far'].append(_JUDGMENT_EXPECT[r['judgment']] == r.get('outcome_far'))
        sdir = r.get('ctx_long_dir_prev') or r.get('ctx_long_dir') or ''
        if r.get('labeled') and sdir in _DIR_EXPECT:
            strat['near'].append(_DIR_EXPECT[sdir] == r.get('outcome_near'))
            strat['far'].append(_DIR_EXPECT[sdir] == r.get('outcome_far'))
    scored = [p for p in preds if p.status == 'scored']
    llm = {'near': [bool(p.hit_near) for p in scored if p.hit_near is not None],
           'far': [bool(p.hit_far) for p in scored if p.hit_far is not None]}
    n_llm = len(scored)
    return jsonify({'code': 200, 'message': 'success', 'data': {
        'user': {k: _rate(v) for k, v in user.items()},
        'strategy': {k: _rate(v) for k, v in strat.items()},
        'llm': {k: _rate(v) for k, v in llm.items()},
        'llm_pending': len([p for p in preds if p.status == 'pending']),
        'insufficient': n_llm < 60,
        'note': ('LLM 样本不足（<60），仅供参考，不构成结论' if n_llm < 60
                 else '达到观察样本量，结论仍以人工复核为准'),
        'frozen_p0': cb._P0_FROZEN,
    }})


# =============================================================================
# Wiki 管理台专属：检索调试 / 校准趋势 / 蒸馏触发（/instinct/wiki 页对接）
# =============================================================================

@instinct_bp.route('/api/search_test', methods=['POST'])
def api_search_test():
    """检索测试区：三种查询构造方式，返回 topK + 逐维打分明细。

    mode=live   {instId}                → 实盘同源实时快照（要拉行情，数秒）
    mode=manual {ctx:{short_dir,long_dir,flipped,atr_pctile,short_period,inst_id}}
    mode=corpus {corpus_id}             → 用某条语料的当时快照（LOO 排除自身）
    注意：返回行携带 outcome/hit 注记仅供人核对——该展示路径与 prompt
    渲染物理隔离（prompts.py 走 sanitize_case_for_prompt，此处不走）。
    """
    body = request.get_json(silent=True) or {}
    mode = str(body.get('mode') or 'manual').strip()
    top_k = min(int(body.get('top_k') or rt.TOP_K), 20)
    try:
        if mode == 'live':
            inst_id = str(body.get('instId') or '').strip()
            if not inst_id:
                return _err('mode=live 需要 instId')
            q = ps.build_live_ctx(inst_id)
            excl = ''
        elif mode == 'corpus':
            cid = int(body.get('corpus_id') or 0)
            with session_scope() as s:
                row = s.get(InstinctCorpus, cid)
                if row is None:
                    return _err(f'语料 id={cid} 不存在', 404)
                q = rt._row_ctx(row)
            excl = q['source_ref']
        else:
            c = body.get('ctx') or {}
            q = {'inst_id': str(c.get('inst_id') or ''),
                 'ctx_short_dir': str(c.get('short_dir') or ''),
                 'ctx_long_dir': str(c.get('long_dir') or ''),
                 'ctx_dir_flipped': bool(c.get('flipped')),
                 'ctx_atr_pctile': float(c.get('atr_pctile') or -1),
                 'short_period': str(c.get('short_period') or ''),
                 'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            excl = ''
    except (ValueError, KeyError, RuntimeError, FileNotFoundError) as e:
        return _err(f'查询快照构造失败: {e}')
    cases = rt.explain(q, top_k=top_k, exclude_self_ref=excl)
    return jsonify({'code': 200, 'message': 'success', 'data': {
        'query': {k: v for k, v in q.items() if not str(k).startswith('_')},
        'cases': cases}})


@instinct_bp.route('/api/trend', methods=['GET'])
def api_trend():
    """校准趋势：按时间序返回 人/策略/LLM 三列逐条命中的 0/1 事件流，
    累计命中率曲线由前端绘制（后端不做平滑，不藏样本）。"""
    with session_scope() as s:
        rows = [rt._row_ctx(r) for r in s.execute(
            select(InstinctCorpus)
            .where(InstinctCorpus.source == 'analysis_record',
                   InstinctCorpus.labeled.is_(True))
            .order_by(InstinctCorpus.ts.asc())).scalars().all()]
        preds = s.execute(
            select(InstinctPrediction.ts, InstinctPrediction.judgment,
                   InstinctPrediction.actual_near, InstinctPrediction.actual_far,
                   InstinctPrediction.status)
            .where(InstinctPrediction.model != 'mock',
                   InstinctPrediction.status == 'scored')
            .order_by(InstinctPrediction.ts.asc())
        ).all()
    events = []
    for r in rows:
        sdir = r.get('ctx_long_dir_prev') or r.get('ctx_long_dir') or ''
        ev = {'ts': r['ts']}
        if r.get('judgment') in _JUDGMENT_EXPECT:
            ev['user_near'] = int(_JUDGMENT_EXPECT[r['judgment']] == r.get('outcome_near'))
            ev['user_far'] = int(_JUDGMENT_EXPECT[r['judgment']] == r.get('outcome_far'))
        if sdir in _DIR_EXPECT:
            ev['strat_near'] = int(_DIR_EXPECT[sdir] == r.get('outcome_near'))
            ev['strat_far'] = int(_DIR_EXPECT[sdir] == r.get('outcome_far'))
        events.append(ev)
    llm_events = [{'ts': p.ts,
                   'llm_near': int(_JUDGMENT_EXPECT.get(p.judgment, '?') == p.actual_near),
                   'llm_far': int(_JUDGMENT_EXPECT.get(p.judgment, '?') == p.actual_far)}
                  for p in preds]
    return jsonify({'code': 200, 'message': 'success',
                    'data': {'events': events, 'llm_events': llm_events}})


@instinct_bp.route('/api/distill', methods=['POST'])
def api_distill():
    """蒸馏复算：apply=true 仅落/合并 candidate（激活仍须逐条人工确认）。"""
    from .instinct import wiki_distiller as wd
    body = request.get_json(silent=True) or {}
    apply_flag = bool(body.get('apply'))
    try:
        st = wd.run(apply=apply_flag)
    except Exception as e:  # noqa: BLE001
        logger.exception('[instinct] 蒸馏失败')
        return _err(f'蒸馏失败: {e}', 500)
    return jsonify({'code': 200, 'message': 'success', 'data': {
        'candidates': st.get('candidates', []), 'applied': st.get('applied', []),
        'n_samples': st.get('samples'), 'applied_mode': apply_flag,
        'report_path': 'data/instinct_wiki_report.md'}})
