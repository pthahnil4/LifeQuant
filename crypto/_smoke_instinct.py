#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘感语料管道冒烟测试（批次12 · P1）：corpus_builder + instinct_* 表

测试隔离策略（保护生产语料）：
  1. 先跑只读断言（建表齐全 / --verify 基线一致 / ctx-outcome 防泄漏）
  2. 快照 instinct_corpus 全表 → 清表 → 沙箱验证幂等 upsert 与映射口径
  3. finally 无条件恢复快照（含自增 id，用例成败都不留脏数据）

前置条件：CRYPTO_DB_URL 已配置（或 data/db_url.txt 存在）
用法：python crypto/_smoke_instinct.py
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import select, text  # noqa: E402

from crypto.database import session_scope, get_engine  # noqa: E402
from crypto.models import (  # noqa: E402
    InstinctCorpus, InstinctPrediction, TaskAnalysisRecord)
from crypto.analysis_record_repo import classify_move, _JUDGMENT_EXPECT  # noqa: E402
from crypto.instinct import corpus_builder as cb  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✅ {name}')
    else:
        FAIL += 1
        print(f'  ❌ {name} {detail}')


# =============================================================================
# 第 1 组：只读断言（不写库）
# =============================================================================
print('\n[组1] 建表与结构')
TABLES = ('instinct_corpus', 'instinct_wiki_rules',
          'instinct_predictions', 'instinct_embeddings')
with get_engine().connect() as conn:
    dbname = conn.execute(text('SELECT DATABASE()')).scalar()
    for t in TABLES:
        n = conn.execute(text(
            'SELECT COUNT(*) FROM information_schema.tables '
            'WHERE table_schema=:db AND table_name=:t'),
            {'db': dbname, 't': t}).scalar()
        check(f'表存在 {t}', bool(n))

print('\n[组2] 防泄漏结构断言')
_cols = set(InstinctCorpus.__table__.columns.keys())
_leak = {c for c in _cols if c.startswith(('outcome_', 'hit_', 'chg_', 'labeled'))}
_ctx = {c for c in _cols if c.startswith('ctx_') or c in
        ('short_period', 'long_period', 'ts', 'inst_id', 'source')}
check('ctx 列与 outcome 列不相交', not (_ctx & _leak), f'{_ctx & _leak}')
_probe = InstinctCorpus()
_leaked_out = {k for k in _probe.ctx_dict()
               if k.startswith(('outcome_', 'hit_', 'chg_', 'labeled'))}
check('ctx_dict() 给不出任何 outcome 字段', not _leaked_out, f'{_leaked_out}')
check('ctx_dict() 覆盖全部 ctx_* 列',
      {c for c in _cols if c.startswith('ctx_')} <= set(_probe.ctx_dict()))

print('\n[组3] 抽取口径（源表 → 样本）')
with session_scope() as s:
    samples = cb.build_samples(s)
    tar_rows = s.execute(
        select(TaskAnalysisRecord).where(
            TaskAnalysisRecord.user_judgment != '')).scalars().all()
by_ref = {m['source_ref']: m for m in samples}
_bad_keys = [set(m) - _cols for m in samples if not set(m) <= _cols]
check('builder 样本键均为表列（无野字段）', not _bad_keys, str(_bad_keys[:1]))
check('分析记录全覆盖', all(f'tar:{r.id}' in by_ref for r in tar_rows),
      f'{sum(1 for r in tar_rows if f"tar:{r.id}" not in by_ref)} 条缺失')
an = [m for m in samples if m['source'] == 'analysis_record']
check('分析记录 judgment 均归一三分类',
      all(m['judgment'] in _JUDGMENT_EXPECT for m in an))
check('labeled 与 outcome_far 一致',
      all(bool(m['labeled']) == bool(m['outcome_far']) for m in samples))
check('hit_near 与 classify_move 重算一致', all(
    m['hit_near'] is None or
    m['hit_near'] == (_JUDGMENT_EXPECT[m['judgment']] == m['outcome_near'])
    for m in an))
_r = {r.id: r for r in tar_rows}
_ok = True
for m in an:
    r = _r[int(m['source_ref'].split(':')[1])]
    expect_near = classify_move(r.price, r.price_1h, float(r.atr_pct or 0), False) or ''
    if m['outcome_near'] != expect_near:
        _ok = False
        break
check('outcome_near 与源表 price_1h 重算一致', _ok)
flip_ok = all(
    m['ctx_dir_flipped'] == bool(m['ctx_long_dir_prev'] and m['ctx_long_dir']
                                 and m['ctx_long_dir_prev'] != m['ctx_long_dir'])
    for m in an)
check('ctx_dir_flipped 语义正确', flip_ok)
slot = [m for m in samples if m['source'] == 'trade_slot']
check('交易卡映射 rise/fall/watch 值域',
      all(m['judgment'] in ('rise', 'fall', 'watch', '') for m in slot))
with session_scope() as s2:
    samples2 = cb.build_samples(s2)
check('两次抽取结果一致（确定性）', samples == samples2)

print('\n[组4] --verify 基线三方核对（真实语料在库时）')
check('verify() PASS', cb.verify() is True)

# =============================================================================
# 第 2 组：沙箱写库（快照 → 清表 → 幂等验证 → finally 恢复）
# =============================================================================
print('\n[组5] 幂等 upsert（沙箱，快照-还原）')


def _snapshot():
    with session_scope() as s:
        rows = s.execute(select(InstinctCorpus)).scalars().all()
        return [{c.key: getattr(r, c.key) for c in InstinctCorpus.__table__.columns}
                for r in rows]


def _restore(snap):
    with session_scope() as s:
        s.execute(text('DELETE FROM instinct_corpus'))
        if snap:
            s.execute(InstinctCorpus.__table__.insert(), snap)


snap = _snapshot()
try:
    with session_scope() as s:
        s.execute(text('DELETE FROM instinct_corpus'))
    with session_scope() as s:
        fresh = cb.build_samples(s)
        ins, upd, unch = cb.upsert_samples(s, fresh)
    check('清表后全量入库 = 样本数', ins == len(fresh), f'{ins} != {len(fresh)}')
    with session_scope() as s:
        again = cb.build_samples(s)
        ins2, upd2, unch2 = cb.upsert_samples(s, again)
    check('幂等重跑：0 新增 0 更新', ins2 == 0 and upd2 == 0 and unch2 == len(fresh),
          f'ins={ins2} upd={upd2} unch={unch2}')
    # 篡改一行再 upsert：应被还原为源数据（更新分支生效）
    with session_scope() as s:
        row = s.execute(select(InstinctCorpus).where(
            InstinctCorpus.source == 'analysis_record')).scalars().first()
    if row is None:
        check('沙箱表存在 analysis_record 行（篡改测试前提）', False,
              '源表 task_analysis_records 无可用行——语料未入 analysis_record')
    else:
        with session_scope() as s:
            row = s.execute(select(InstinctCorpus).where(
                InstinctCorpus.source == 'analysis_record')).scalars().first()
            row.judgment = 'bogus'
        with session_scope() as s:
            _, upd3, _ = cb.upsert_samples(s, cb.build_samples(s))
        with session_scope() as s:
            row = s.execute(select(InstinctCorpus).where(
                InstinctCorpus.source == 'analysis_record')).scalars().first()
            check('篡改值被重跑还原（update 分支）',
                  row.judgment in _JUDGMENT_EXPECT and upd3 >= 1,
                  f'judgment={row.judgment} upd={upd3}')
    # 源新增记录 → 语料应新增（insert 分支，自造后当场清理）
    from crypto.models import TaskAnalysisRecord as TAR
    import datetime as _dt
    fake = TAR(ts=_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               inst_id='SMOKE-USDT-SWAP', price=1.0, short_period='15m',
               long_period='4H', short_dir='long', long_dir='long',
               long_dir_prev='long', atr_pct=0.5, user_judgment='rise',
               user_reason='smoke', hour_slot='', source='live')
    with session_scope() as s:
        s.add(fake)
    with session_scope() as s:
        ins4, _, _ = cb.upsert_samples(s, cb.build_samples(s))
    check('源新增记录触发语料 insert 分支', ins4 == 1, f'ins={ins4}')
    with session_scope() as s:
        s.execute(text("DELETE FROM instinct_corpus WHERE inst_id='SMOKE-USDT-SWAP'"))
        r2 = s.get(TAR, fake.id)
        if r2:
            s.delete(r2)
finally:
    _restore(snap)

after = _snapshot()
check('快照恢复完整', len(after) == len(snap) and
      {r['source_ref'] for r in after} == {r['source_ref'] for r in snap})

with session_scope() as s:
    check('恢复后 verify 仍 PASS', cb.verify() is True)

# =============================================================================
# 第 3 组：P2 检索（只读断言）
# =============================================================================
print('\n[组6] retriever 打分与防泄漏出口')
import datetime as _dt  # noqa: E402
from crypto.instinct import retriever as rt  # noqa: E402

_a = {'ctx_short_dir': 'long', 'ctx_long_dir': 'long', 'ctx_dir_flipped': 0}
check('方向全对+翻转一致=1.0', rt.dir_pattern_sim(_a, dict(_a)) == 1.0)
check('方向全对+翻转不一致=0.85',
      rt.dir_pattern_sim(_a, {**_a, 'ctx_dir_flipped': 1}) == 0.85)
check('仅长对+翻转一致=0.6',
      rt.dir_pattern_sim(_a, {**_a, 'ctx_short_dir': 'short'}) == 0.6)
check('一侧方向全缺=0（trade_slot 不硬凑相似）',
      rt.dir_pattern_sim(_a, {'ctx_short_dir': '', 'ctx_long_dir': ''}) == 0.0)
check('ATR 分位未知→中性 0.5',
      rt.atr_proximity({'ctx_atr_pctile': -1}, {'ctx_atr_pctile': 0.9}) == rt.ATR_NEUTRAL)
check('ATR 分位 0 vs 1 → 0',
      rt.atr_proximity({'ctx_atr_pctile': 0.0}, {'ctx_atr_pctile': 1.0}) == 0.0)
check('周期一致 1 / 同数量级 0.5 / 无关 0',
      rt.period_match({'short_period': '15m'}, {'short_period': '15m'}) == 1.0
      and rt.period_match({'short_period': '15m'}, {'short_period': '30m'}) == 0.5
      and rt.period_match({'short_period': '15m'}, {'short_period': '4H'}) == 0.0)
_anchor = _dt.datetime(2026, 9, 1, 12, 0, 0)
_d45 = rt.time_decay({'ts': '2026-07-18 12:00:00'}, {}, now=_anchor)
check('time_decay τ=45d → e^-1', abs(_d45 - 0.3679) < 0.001, f'{_d45}')
check('time_decay 解析失败 → 0', rt.time_decay({'ts': 'bogus'}, {}, now=_anchor) == 0.0)
_boost = rt.case_score({**_a, 'inst_id': 'X-USDT-SWAP', 'ts': '2026-09-01 12:00:00'},
                       {**_a, 'inst_id': 'X-USDT-SWAP', 'ctx_atr_pctile': -1,
                        'short_period': '15m', 'ts': '2026-09-01 12:00:00'},
                       anchor=_anchor)
_nob = rt.case_score({**_a, 'inst_id': 'Y-USDT-SWAP', 'ts': '2026-09-01 12:00:00'},
                     {**_a, 'inst_id': 'X-USDT-SWAP', 'ctx_atr_pctile': -1,
                      'short_period': '15m', 'ts': '2026-09-01 12:00:00'},
                     anchor=_anchor)
check('同币种总分 ×3', abs(_boost - _nob * rt.SAME_INST_BOOST) < 1e-3,
      f'{_boost} vs {_nob}')
with session_scope() as s:
    _tar_id = s.execute(select(InstinctCorpus.id).where(
        InstinctCorpus.source == 'analysis_record')).scalar()
_n1 = rt.retrieve_for_record(_tar_id)
_n2 = rt.retrieve_for_record(_tar_id)
check('检索 topK 非空且确定性', bool(_n1) and
      [x['source_ref'] for x in _n1] == [x['source_ref'] for x in _n2])
check('检索结果按 score 降序', all(_n1[i]['score'] >= _n1[i + 1]['score']
                                  for i in range(len(_n1) - 1)))
check('LOO 排除自身', all(x['source_ref'] != f'tar:{_tar_id}' for x in _n1))
_p1 = rt.sanitize_case_for_prompt(_n1[0])
_dirty = [k for k in _p1 if str(k).startswith(('outcome_', 'hit_', 'chg_', 'labeled', '_'))]
check('sanitize 后无 outcome/内部字段', not _dirty, str(_dirty))
try:
    rt.assert_no_future_leak('prompt: 后续涨幅 12.3%', ['12.3'])
    check('泄漏闸门能拦截未来值', False, '未抛异常')
except AssertionError:
    check('泄漏闸门能拦截未来值', True)
rt.assert_no_future_leak('prompt: 仅含 ctx 字段', ['12.3'])
check('泄漏闸门不误伤干净文本', True)

# =============================================================================
# 第 4 组：P2 Wiki 规则卡（快照-还原 instinct_wiki_rules）
# =============================================================================
print('\n[组7] wiki_repo 状态机与条件匹配')
from crypto.instinct import wiki_repo as wr  # noqa: E402
from crypto.models import InstinctWikiRule  # noqa: E402


def _snap_rules():
    with session_scope() as s:
        rows = s.execute(select(InstinctWikiRule)).scalars().all()
        return [{c.key: getattr(r, c.key) for c in InstinctWikiRule.__table__.columns}
                for r in rows]


def _restore_rules(snap):
    with session_scope() as s:
        s.execute(text('DELETE FROM instinct_wiki_rules'))
        if snap:
            s.execute(InstinctWikiRule.__table__.insert(), snap)


_rsnap = _snap_rules()
try:
    rid, act = wr.upsert_rule('smoke:p2-key', '冒烟规则v1', kind='scenario',
                              condition={'sources': ['analysis_record'], 'judgment': ['rise']},
                              stat_basis='smoke n=1', evidence_refs=['tar:1'])
    check('新建 candidate 规则', act == 'created' and rid > 0)
    rid2, act2 = wr.upsert_rule('smoke:p2-key', '冒烟规则v2', kind='scenario',
                                evidence_refs=['tar:1', 'tar:2'])
    check('同 key 再蒸馏=merged 不重复建卡',
          act2 == 'merged' and rid2 == rid and len(_snap_rules()) == len(_rsnap) + 1)
    with session_scope() as s:
        _row = s.get(InstinctWikiRule, rid)
        check('merge 更新文本且证据去重追加',
              _row.statement == '冒烟规则v2' and '"tar:2"' in _row.evidence_refs)
    wr.activate_rule(rid)
    check('candidate→active', [r for r in wr.list_rules('active') if r['id'] == rid][0]
          ['status'] == 'active')
    try:
        wr.retire_rule(rid, reason='smoke')
        check('active→retired', [r for r in wr.list_rules() if r['id'] == rid][0]['status'] == 'retired')
    except Exception as e:  # noqa: BLE001
        check('active→retired', False, str(e))
    try:
        wr.activate_rule(rid)
        check('retired 拒绝直接激活', False, '未抛异常')
    except ValueError:
        check('retired 拒绝直接激活', True)
    _, act3 = wr.upsert_rule('smoke:p2-key', '冒烟规则v3')
    check('retired 再蒸馏复活为 candidate',
          act3 == 'merged' and [r for r in wr.list_rules() if r['id'] == rid][0]['status'] == 'candidate')
    wr.activate_rule(rid)  # match_rules 只消费 active：先激活再验证条件命中
    _ctx_hit = {'source': 'analysis_record', 'judgment': 'rise', 'inst_id': '',
                'ctx_long_dir': 'long', 'ctx_short_dir': 'long',
                'ctx_dir_flipped': 0, 'ctx_atr_pctile': -1}
    _ctx_miss = dict(_ctx_hit, source='trade_slot')
    _hit_ids = {r['id'] for r in wr.match_rules(_ctx_hit)}
    check('条件命中（source+judgment 匹配）', rid in _hit_ids)
    check('条件不命中（source 不符，fail-closed）', rid not in {r['id'] for r in wr.match_rules(_ctx_miss)})
    with session_scope() as s:
        s.get(InstinctWikiRule, rid).valid_until = '2020-01-01 00:00:00'
        s.get(InstinctWikiRule, rid).status = 'active'
    check('过期自动降级 ≥1 条', wr.refresh_expired() >= 1)
    check('降级后为 candidate', [r for r in wr.list_rules() if r['id'] == rid][0]['status'] == 'candidate')
    _line = wr.rule_to_prompt_line({'rule_key': 'smoke:p2-key', 'kind': 'meta',
                                    'statement': 'X', 'stat_basis': 'n=1'})
    check('prompt 行含规则标记且无样本级数值', _line.startswith('[规则|') and 'tar:' not in _line)
    # 生产种子卡存在性（--apply 已跑过）
    check('生产存在 seed:user-vs-strategy-far 候选卡',
          any(r['rule_key'] == 'seed:user-vs-strategy-far' for r in wr.list_rules()))
finally:
    _restore_rules(_rsnap)
check('规则表快照恢复完整',
      {r['rule_key'] for r in _snap_rules()} == {r['rule_key'] for r in _rsnap})

# =============================================================================
# 第 5 组：P2 统计蒸馏（只读 + 确定性）
# =============================================================================
print('\n[组8] wiki_distiller 显著性门槛与确定性')
from crypto.instinct import wiki_distiller as wd  # noqa: E402

with session_scope() as s:
    _rows = wd.load_samples(s)
_c1 = wd.generate_candidates(_rows)
_c2 = wd.generate_candidates(_rows)
check('蒸馏确定性（两次候选一致）',
      [(c['rule_key'], c['stat_basis']) for c in _c1] ==
      [(c['rule_key'], c['stat_basis']) for c in _c2])
check('候选均含必备字段', all(
    {'rule_key', 'statement', 'kind', 'condition', 'stat_basis', 'evidence_refs'} <= set(c)
    for c in _c1))
_stat_bad = []
import re as _re  # noqa: E402
for c in _c1:
    if not c['rule_key'].startswith('stat:'):
        continue
    m = _re.search(r'命中(\d+)/(\d+)=([\d.]+)% vs 基线([\d.]+)%', c['stat_basis'])
    if not m:
        _stat_bad.append((c['rule_key'], 'basis 不可解析'))
        continue
    h, t, _r0, base = int(m.group(1)), int(m.group(2)), float(m.group(3)), float(m.group(4))
    if t < wd.MIN_N or abs(h / t * 100 - base) < wd.MIN_GAP_PP - 0.05:
        _stat_bad.append((c['rule_key'], f'n={t} gap={h / t * 100 - base:.1f}'))
check('stat:* 候选全部复算满足门槛（n≥10 且偏离≥15pp）', not _stat_bad, str(_stat_bad[:2]))
_uv = wd.user_vs_strategy(_rows, 'far')
check('user_vs_strategy 口径复算（n≥15 且命中率已产出）',
      _uv['user_n'] >= 15 and _uv['strat_n'] >= 15 and 0 < _uv['user_rate'] < 100,
      str(_uv))
check('种子卡与冻结基线方向一致（user_far < strat_far）',
      wd._P0_REF['user_far'] < wd._P0_REF['strat_far'] if hasattr(wd, '_P0_REF')
      else cb._P0_FROZEN['user_far'] < cb._P0_FROZEN['strat_far'])

# =============================================================================
# 第 6 组：P3 提示词渲染 / 网关校验 / 回灌管线（只读 + mock，零落库）
# =============================================================================
print('\n[组9] prompts 渲染与泄漏负例')
from crypto.instinct import prompts as pm  # noqa: E402
from crypto.instinct import llm_gateway as gw  # noqa: E402
from crypto.instinct import predict_service as ps  # noqa: E402

with session_scope() as s:
    _cor = s.execute(select(InstinctCorpus).where(
        InstinctCorpus.source == 'analysis_record',
        InstinctCorpus.judgment != '').order_by(InstinctCorpus.id)
    ).scalars().all()
    _rows3 = [rt._row_ctx(r) for r in _cor]
_c0 = _rows3[0]
_clean = rt.sanitize_case_for_prompt(_c0)
_cases_clean = [rt.sanitize_case_for_prompt(c) for c in _rows3[1:4]]
_txt, _forb = pm.render_prompt(_clean, _cases_clean, [], profile='P')
check('四段结构齐全', all(seg in _txt for seg in
      ('## WIKI 段', '## CASES 段', '## NOW 段', '## 输出要求')))
check('clean 输入下 forbidden 为空', _forb == [])
try:
    pm.render_prompt(dict(_c0), [], [])   # _row_ctx 含 outcome_/hit_/chg_ 原始行
    check('输入含 outcome 键必须抛错（fail-closed）', False, '未抛异常')
except ValueError:
    check('输入含 outcome 键必须抛错（fail-closed）', True)
_t2, _f2 = pm.render_prompt(_clean, [dict(_c0)], [], strict=False)
_leaked = [v for v in _f2 if v in _t2]
check('strict=False 兜底：答案值仍不进文本', bool(_f2) and not _leaked, str(_f2[:3]))
rt.assert_no_future_leak(_t2, _f2)
check('泄漏闸门扫描通过', True)
check('规则行进 WIKI 段', 'R1' in pm.render_prompt(
    _clean, [], [{'id': 1, 'rule_key': 'k', 'kind': 'meta',
                  'statement': 'S', 'stat_basis': 'B'}])[0])

print('\n[组10] llm_gateway schema 校验')
_okraw = '{"judgment":"rise","confidence":0.7,"rationale":"r","cited_rule_ids":[1],"cited_corpus_ids":[2],"meta_cognition":""}'
check('合法 JSON 通过', gw.parse_prediction(_okraw)['judgment'] == 'rise')
check('围栏容忍', gw.parse_prediction('```json\n' + _okraw + '\n```')['confidence'] == 0.7)
# DeepSeek 实测会把 [C1]/[R2] 标签文字原样填回：归一化为整数，真实性仍靠越界检查兜底
_norm = gw.parse_prediction('{"judgment":"rise","confidence":0.7,"rationale":"r",'
                            '"cited_rule_ids":["R1","2"],"cited_corpus_ids":["C3",4," 5 "]}')
check('C/R 前缀与数字串引用归一为整数',
      _norm['cited_rule_ids'] == [1, 2] and _norm['cited_corpus_ids'] == [3, 4, 5])
for _nm, _badraw in (
        ('judgment 非法', '{"judgment":"moon","confidence":0.5,"rationale":"r"}'),
        ('confidence 越界', '{"judgment":"rise","confidence":1.5,"rationale":"r"}'),
        ('rationale 缺失', '{"judgment":"rise","confidence":0.5}'),
        ('cited 非整数组', '{"judgment":"rise","confidence":0.5,"rationale":"r","cited_corpus_ids":["a"]}'),
        ('cited 野标签', '{"judgment":"rise","confidence":0.5,"rationale":"r","cited_corpus_ids":["CX",""]}'),
        ('cited bool', '{"judgment":"rise","confidence":0.5,"rationale":"r","cited_corpus_ids":[true]}'),
        ('无 JSON', '模型胡言乱语')):
    try:
        gw.parse_prediction(_badraw)
        check(f'拒绝 {_nm}', False, '未抛异常')
    except gw.ResponseInvalidError:
        check(f'拒绝 {_nm}', True)
try:
    gw.assert_citations_real({'cited_rule_ids': [3], 'cited_corpus_ids': [9]}, 2, 6)
    check('越界引用被拒', False, '未抛异常')
except gw.ResponseInvalidError:
    check('越界引用被拒', True)
_m1, _, _mod = gw.mock_chat('p', _cases_clean, [])
_m2, _, _ = gw.mock_chat('p', _cases_clean, [])
check('mock 网关确定性', _m1 == _m2 and _mod == 'mock')

print('\n[组11] 回灌管线（真实语料 + mock，不落库）')
_res = ps.predict_from_ctx(_c0, anchor=ps._parse_ts(_c0['ts']),
                           exclude_self_ref=_c0['source_ref'])
check('单样本全管线通过', _res.get('ok') is True, _res.get('error', ''))
check('prompt 非平凡长度', _res.get('prompt_len', 0) > 400)
check('LOO：引用真实存在（corpus ids ⊆ 语料 id 集）',
      set(_res.get('real_corpus_ids', [])) <= {r.id for r in _cor})
check('防未来规则：anchor 早于 active 规则创建时间 → n_rules=0',
      _res.get('n_rules') == 0, f"n_rules={_res.get('n_rules')}")
_st = ps.backfill(limit=5, persist=False, use_mock=True)
check('backfill 干跑 5/5 零失败',
      _st['total'] == 5 and _st['ok'] == 5 and _st['fail'] == 0 and _st['persisted'] == 0)
check('backfill 未 persist 不落库', True)
with session_scope() as s:
    _n_mock = len(s.execute(select(InstinctPrediction.id).where(
        InstinctPrediction.model == 'mock')).all())
check('库中无 mock 残留行', _n_mock == 0, f'{_n_mock} 行')

print('\n[组12] /instinct HTTP API（test_client · NO_BACKGROUND 防拉调度器）')
os.environ['CRYPTO_NO_BACKGROUND'] = '1'   # 导入 app 不拉起调度器/监控线程
from crypto import app as _app_mod          # noqa: E402
from crypto import web_auth as _wa          # noqa: E402
_client = _app_mod.app.test_client()
_tok = _wa.configured_token()
_H = {'Authorization': 'Bearer ' + _tok} if _tok else {}

_r = _client.get('/instinct/', headers=_H)
check('页面 200 且含标题特征', _r.status_code == 200 and '盘感模拟台' in _r.get_data(as_text=True),
      f'HTTP {_r.status_code}')
_j = _client.get('/instinct/api/currencies', headers=_H).get_json()
check('currencies code=200 且为数组', _j['code'] == 200 and isinstance(_j['data']['items'], list))
_j = _client.get('/instinct/api/rules', headers=_H).get_json()
check('rules code=200 且含状态计数', _j['code'] == 200 and isinstance(_j['data']['counts'], dict)
      and len(_j['data']['rules']) >= 1)
_j = _client.get('/instinct/api/stats', headers=_H).get_json()
_d = _j['data']
check('stats 三列结构 + insufficient 布尔', _j['code'] == 200
      and all(k in _d for k in ('user', 'strategy', 'llm', 'insufficient'))
      and isinstance(_d['insufficient'], bool))
check('stats 人/策略列与 P0 冻结基线一致（语料未增长时逐位对；增长后只验结构）',
      (_d['user']['near']['rate'] == _d['frozen_p0']['user_near']
       and _d['user']['far']['rate'] == _d['frozen_p0']['user_far']
       and _d['strategy']['near']['rate'] == _d['frozen_p0']['strat_near']
       and _d['strategy']['far']['rate'] == _d['frozen_p0']['strat_far'])
      if _d['user']['far']['n'] == 15 and _d['strategy']['far']['n'] == 15
      else (_d['user']['far']['n'] > 15 and _d['user']['far']['rate'] is not None),
      f"user={_d['user']} strat={_d['strategy']}")
_j = _client.get('/instinct/api/now', headers=_H).get_json()   # 不带 instId，不触网
check('now 缺参 code=400', _j['code'] == 400)
_j = _client.post('/instinct/api/predict', json={'instId': 'FAKE-USDT-SWAP'}, headers=_H).get_json()
if not gw.is_configured():
    check('无 key 时 predict 明确 503 提示（不假成功）', _j['code'] == 503 and 'CRYPTO_LLM' in _j['message'])
else:
    print('  ⏸ 已配置 LLM key，跳过无 key 断言')
_j = _client.post('/instinct/api/rules/999999/confirm', headers=_H).get_json()
check('不存在规则 confirm code=404', _j['code'] == 404)
_j = _client.post('/instinct/api/predictions/999999/feedback',
                  json={'user_trusted': True}, headers=_H).get_json()
check('不存在预测 feedback code=404', _j['code'] == 404)
_j = _client.post('/instinct/api/settle', json={}, headers=_H).get_json()
check('settle code=200 且 filled 为非负 int',
      _j['code'] == 200 and isinstance(_j['data']['filled'], int) and _j['data']['filled'] >= 0)
if _tok:
    check('配了口令时未带凭证访问被闸门拦截',
          _client.get('/instinct/api/rules').status_code in (401, 302))
else:
    print('  ⏸ 未配置访问口令，跳过 401 断言')
_jid, _note = ps.register_instinct_jobs()
if not gw.is_configured():
    check('无 key 时调度任务不注册（缺席优于空转）', _jid is None and '未配置' in _note)
else:
    check('有 key 时调度任务注册返回 job_id', _jid is not None)

print('\n[组13] /instinct/wiki 管理台（Wiki 治理页 + 检索调试 + 趋势 + 蒸馏端点）')
_r = _client.get('/instinct/wiki', headers=_H)
_html = _r.get_data(as_text=True)
check('wiki 页面 200 + 标题特征', _r.status_code == 200 and '盘感 Wiki 管理台' in _html)
check('样式隔离：引独立 instinct_wiki.css 且选择器带 iwk- 前缀',
      'css/instinct_wiki.css' in _html and '.iwk-' in _html)
check('四功能区块齐全', all(k in _html for k in ('规则卡治理', '预测审计', '校准仪表盘', '检索测试')))
_j = _client.post('/instinct/api/search_test', headers=_H, json={
    'mode': 'manual', 'top_k': 4,
    'ctx': {'short_dir': 'up', 'long_dir': 'down', 'flipped': False,
            'atr_pctile': 0.6, 'short_period': '5m', 'inst_id': 'INJ-USDT-SWAP'}}).get_json()
_cs = _j['data']['cases']
check('manual 检索非空 + breakdown 齐全', _j['code'] == 200 and len(_cs) > 0
      and all(set(c['breakdown']) >= {'dir_sim', 'atr_prox', 'period', 'decay', 'base', 'boost', 'final'} for c in _cs))
check('打分明细与生产检索总分逐条一致（final==score）',
      all(abs(c['score'] - c['breakdown']['final']) < 1e-6 for c in _cs))
_j = _client.post('/instinct/api/search_test', headers=_H, json={'mode': 'corpus', 'corpus_id': 999999}).get_json()
check('corpus 模式不存在 id → code=404', _j['code'] == 404)
with session_scope() as _s:
    _cid = _s.execute(select(InstinctCorpus.id).where(
        InstinctCorpus.source == 'analysis_record',
        InstinctCorpus.judgment != '').limit(1)).scalars().first()
_j = _client.post('/instinct/api/search_test', headers=_H, json={'mode': 'corpus', 'corpus_id': _cid}).get_json()
check('corpus 模式 LOO：结果不含查询自身',
      _j['code'] == 200 and all(c['id'] != _cid for c in _j['data']['cases']))
_j = _client.post('/instinct/api/search_test', headers=_H, json={'mode': 'live'}).get_json()
check('live 模式缺 instId → 400（不触网）', _j['code'] == 400)
_j = _client.get('/instinct/api/trend', headers=_H).get_json()
_ev = _j['data']['events']
check('trend 事件流覆盖全部 labeled 语料且含人/策略命中键',
      _j['code'] == 200 and len(_ev) >= 15
      and any('user_far' in e for e in _ev) and any('strat_far' in e for e in _ev)
      and 'llm_events' in _j['data'])
_n_before = len(wr.list_rules())
_j = _client.post('/instinct/api/distill', headers=_H, json={'apply': False}).get_json()
check('distill 干跑出候选且不写库', _j['code'] == 200 and _j['data']['n_samples'] >= 62
      and len(wr.list_rules()) == _n_before)
_j = _client.post('/instinct/api/expire', headers=_H, json={}).get_json()
check('expire 端点 200（degraded 为 int）', _j['code'] == 200 and isinstance(_j['data']['degraded'], int))
_j = _client.get('/instinct/api/rules', headers=_H).get_json()
check('rules 响应携带 llm_configured 布尔', isinstance(_j['data'].get('llm_configured'), bool))

print(f'\n[RESULT] PASS={PASS} FAIL={FAIL}')
sys.exit(1 if FAIL else 0)
