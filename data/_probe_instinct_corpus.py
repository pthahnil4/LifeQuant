# -*- coding: utf-8 -*-
"""盘感语料质量体检探针（RAG+LLM Wiki 模拟盘感 · P0 阶段交付物）

只读探针，量化四大语料源的"可检索性"，回答三个问题：
  1. task_analysis_records 里有多少【带用户判断+已回填对错】的有效样本？
  2. 复盘四格 / 交易卡打卡 / 成交流水能补充多少结构化 lesson 与标注？
  3. 用 analyze_record_repo 同口径（ATR 中性带三分类）算出用户历史命中率基线，
     作为后续 LLM 影子预测 A/B 的对照组。

口径对齐说明：
  - 涨跌三分类复用 crypto/analysis_record_repo.py 的 classify_move 逻辑
    （θ = max(HIT_FLOOR_PCT, HIT_ATR_K × √窗口比 × atr_pct)），常量直接 import，
    避免两套实现漂移。
  - plan_slots 命中率归因用 analysis_ids 是否为空区分「有/无分析支撑」。

用法：python data/_probe_instinct_corpus.py
输出：stdout 报告 + 落盘 data/instinct_corpus_report.md
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# Windows 控制台默认 GBK，报告含 emoji/符号，统一切 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pymysql

from crypto.analysis_record_repo import (
    HIT_ATR_K, HIT_FLOOR_PCT, HIT_FAR_SCALE,
    _JUDGMENT_EXPECT, _DIR_EXPECT, classify_move)

with open(os.path.join(ROOT, 'data', 'db_url.txt'), encoding='utf-8') as f:
    URL = f.read().strip()

body = URL.split('://', 1)[1]
auth, rest = body.split('@', 1)
hostport, db = rest.split('/', 1)
HOST, PORT = hostport.split(':')[0], int(hostport.split(':')[1])
USER, PWD = auth.split(':', 1)

LINES = []


def out(s=''):
    print(s)
    LINES.append(s)


def pct(n, d):
    return f'{n / d * 100:.1f}%' if d else 'n/a'


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))
    return sorted_vals[i]


conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PWD,
                       database=db.split('?')[0], charset='utf8mb4',
                       connect_timeout=10, read_timeout=120,
                       cursorclass=pymysql.cursors.DictCursor)

out('# 盘感语料质量体检报告（RAG + LLM Wiki 模拟盘感 · P0）')
out(f'\n目标库：{USER}@{HOST}:{PORT}/{db.split("?")[0]}')

with conn.cursor() as cur:
    # =====================================================================
    # 1. task_analysis_records —— 核心盘感情景记忆
    # =====================================================================
    cur.execute("""
        SELECT COUNT(*) n,
               SUM(user_judgment <> '') n_jud,
               SUM(user_judgment <> '' AND price_1h > 0) n_jud_near,
               SUM(user_judgment <> '' AND price_4h > 0) n_jud_far,
               SUM(user_judgment <> '' AND user_reason IS NOT NULL
                   AND CHAR_LENGTH(user_reason) > 4) n_reason,
               SUM(source = 'live') n_live,
               MIN(ts) ts_min, MAX(ts) ts_max,
               COUNT(DISTINCT inst_id) n_inst
        FROM task_analysis_records""")
    a = cur.fetchone()
    total, n_jud = int(a['n']), int(a['n_jud'] or 0)
    out('\n## 1. 实盘分析记录 task_analysis_records')
    out(f"- 总记录 {total} 条 | 币种 {int(a['n_inst'])} 个 | 时间跨度 {a['ts_min']} ~ {a['ts_max']}")
    out(f"- 带个人判断: {n_jud} ({pct(n_jud, total)})")
    out(f"- 判断+近窗口已回填(可计分): {int(a['n_jud_near'] or 0)} ({pct(int(a['n_jud_near'] or 0), total)})")
    out(f"- 判断+远窗口已回填(可计分): {int(a['n_jud_far'] or 0)} ({pct(int(a['n_jud_far'] or 0), total)})")
    out(f"- 判断+原因文本(向量语料): {int(a['n_reason'] or 0)} ({pct(int(a['n_reason'] or 0), total)})")
    out(f"- source=live(非事后补记): {int(a['n_live'] or 0)} ({pct(int(a['n_live'] or 0), total)})")

    # 命中率基线（与 compute_stats 完全同口径）
    cur.execute("""
        SELECT inst_id, short_period, price, price_1h, price_4h, atr_pct,
               user_judgment, long_dir, long_dir_prev
        FROM task_analysis_records WHERE user_judgment <> ''""")
    rows = cur.fetchall()
    base = {k: {'near': [0, 0], 'far': [0, 0]} for k in ('user', 'strategy')}
    atrs = []
    inst_labeled = {}
    for r in rows:
        atr = float(r['atr_pct'] or 0)
        if atr > 0:
            atrs.append(atr)
        labeled_any = False
        for win, col, is_far in (('near', 'price_1h', False), ('far', 'price_4h', True)):
            if not r[col] or float(r[col]) <= 0:
                continue
            actual = classify_move(r['price'], float(r[col]), atr, is_far)
            if actual is None:
                continue
            labeled_any = True
            if r['user_judgment'] in _JUDGMENT_EXPECT:
                b = base['user'][win]
                b[1] += 1
                b[0] += (_JUDGMENT_EXPECT[r['user_judgment']] == actual)
            sdir = r['long_dir_prev'] or r['long_dir'] or ''
            if sdir in _DIR_EXPECT:
                b = base['strategy'][win]
                b[1] += 1
                b[0] += (_DIR_EXPECT[sdir] == actual)
        if labeled_any:
            inst_labeled[r['inst_id']] = inst_labeled.get(r['inst_id'], 0) + 1
    out('\n### 1.1 命中率基线（LLM 影子预测 A/B 对照组，compute_stats 同口径）')
    for side, cn in (('user', '个人判断'), ('strategy', '策略方向')):
        for win, wl in (('near', '近窗口'), ('far', '远窗口')):
            h, t = base[side][win]
            out(f"- {cn}·{wl}: {h}/{t} = {pct(h, t)}")
    atrs.sort()
    out('\n### 1.2 ATR% 分布（数值特征分位化的依据）')
    if atrs:
        out(f"- p10={percentile(atrs, .1):.2f} p50={percentile(atrs, .5):.2f} "
            f"p90={percentile(atrs, .9):.2f}（n={len(atrs)}）")
    out('\n### 1.3 带标注样本 Top 币种（检索覆盖度）')
    for inst, n in sorted(inst_labeled.items(), key=lambda x: -x[1])[:10]:
        out(f'- {inst}: {n} 条')

    # =====================================================================
    # 2. journal_notes —— 语义记忆（复盘 lesson）
    # =====================================================================
    cur.execute("""
        SELECT COUNT(*) n,
               SUM(type = 'review') n_review,
               SUM(type = 'review' AND review_lesson IS NOT NULL
                   AND CHAR_LENGTH(review_lesson) > 4) n_lesson,
               SUM(type = 'review' AND distilled = 1) n_distilled
        FROM journal_notes""")
    j = cur.fetchone()
    out('\n## 2. 随笔/复盘 journal_notes')
    out(f"- 总条目 {int(j['n'])} | 复盘 {int(j['n_review'] or 0)} | "
        f"lesson 有效 {int(j['n_lesson'] or 0)} | 已蒸馏 {int(j['n_distilled'] or 0)}")
    cur.execute("""
        SELECT t.tag_name, COUNT(*) n FROM note_tags t
        JOIN journal_notes jn ON jn.id = t.note_id
        WHERE t.tag_name IN ('交易心得','策略思考','复盘','教训','经验')
        GROUP BY t.tag_name ORDER BY n DESC""")
    for r in cur.fetchall():
        out(f"- 标签[{r['tag_name']}]: {int(r['n'])} 条")

    # =====================================================================
    # 3. plan_slots 交易卡 —— 判断→动作配对 + 有/无分析支撑归因
    # =====================================================================
    cur.execute("""
        SELECT COUNT(*) n,
               SUM(s.has_record = 1 AND s.prediction <> '') n_pred,
               SUM(s.hit IS NOT NULL) n_hit_labeled,
               SUM(s.hit IS NOT NULL AND s.analysis_ids <> '') n_with_ana,
               SUM(s.hit IS NOT NULL AND s.analysis_ids <> '' AND s.hit = 1) n_with_ana_hit,
               SUM(s.hit IS NOT NULL AND s.analysis_ids = '') n_no_ana,
               SUM(s.hit IS NOT NULL AND s.analysis_ids = '' AND s.hit = 1) n_no_ana_hit,
               SUM(s.market_analysis IS NOT NULL AND CHAR_LENGTH(s.market_analysis) > 4) n_mkt_txt
        FROM plan_slots s JOIN plan_cards c ON c.id = s.card_id AND c.type = 'trade'""")
    p = cur.fetchone()
    out('\n## 3. 交易卡打卡 plan_slots(type=trade)')
    out(f"- 格子总数 {int(p['n'])} | 有预测 {int(p['n_pred'] or 0)} | "
        f"已结算(hit非空) {int(p['n_hit_labeled'] or 0)} | 有行情分析文本 {int(p['n_mkt_txt'] or 0)}")
    for label, n, h in (('有分析支撑', p['n_with_ana'], p['n_with_ana_hit']),
                        ('无分析支撑', p['n_no_ana'], p['n_no_ana_hit'])):
        out(f'- {label}: {int(h or 0)}/{int(n or 0)} = {pct(int(h or 0), int(n or 0))}')

    # =====================================================================
    # 4. trade_journal —— 实际成交流水
    # =====================================================================
    cur.execute("""
        SELECT COUNT(*) n, MIN(ts) ts_min, MAX(ts) ts_max,
               COUNT(DISTINCT inst_id) n_inst,
               SUM(reason IS NOT NULL AND reason <> '' AND reason <> 'signal') n_reason
        FROM trade_journal""")
    t = cur.fetchone()
    out('\n## 4. 成交流水 trade_journal')
    if int(t['n'] or 0):
        out(f"- 共 {int(t['n'])} 笔 | 币种 {int(t['n_inst'])} | {t['ts_min']} ~ {t['ts_max']} "
            f"| 带非默认 reason {int(t['n_reason'] or 0)}")
    else:
        out('- 表为空或不存在（不影响 P1，流水为增强语料）')

conn.close()

# =====================================================================
# 体检结论
# =====================================================================
n_far = int(a['n_jud_far'] or 0)
out('\n## 结论与门槛判定')
out(f"- 情景记忆(分析记录带判断+远窗口结果): {n_far} 条 → "
    + ('✅ 达到 P1 语料入库最低线(≥30)' if n_far >= 30 else '⚠️ 不足 30 条，P3 影子预测需推迟，先积累'))
out(f"- 语义记忆(lesson 有效): {int(j['n_lesson'] or 0)} 条 → "
    + ('✅ 可启动 Wiki 蒸馏' if int(j['n_lesson'] or 0) >= 5 else '⚠️ lesson 过少，Wiki 层先人工维护种子规则'))
out(f"- 交易卡已结算: {int(p['n_hit_labeled'] or 0)} 条 → "
    + ('✅ 可参与判断→动作偏差分析' if int(p['n_hit_labeled'] or 0) >= 10 else '⚠️ 暂不参与，仅作展示'))
top_inst = sorted(inst_labeled.values(), reverse=True)[:1]
if top_inst and top_inst[0] >= 15:
    out(f"- 单币种最大带标注样本 {top_inst[0]} 条 → 首轮影子预测建议只跑该币种")
else:
    out('- 单币种样本 <15 条 → 检索需跨币种共享特征空间（同周期+方向组合泛化）')

dst = os.path.join(ROOT, 'data', 'instinct_corpus_report.md')
with open(dst, 'w', encoding='utf-8') as f:
    f.write('\n'.join(LINES) + '\n')
print(f'\n[已落盘] {dst}')
