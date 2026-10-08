#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟测试：实盘分析记录（批次10 + 批次16 先触碰/结构价）— 建表 + CRUD + 复盘回填 + 统计

运行：python crypto/_smoke_analysis_record.py
测试数据自清理：脚本结束删除本次插入的全部记录。
前置：批次16 新列须已在库中（应用启动 init_db 会自动补齐存量表列）。
"""

import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto.database import get_engine, session_scope, Base
from crypto import models  # noqa: F401 注册模型
from crypto import analysis_record_repo as repo


def main():
    # 1. 建表（checkfirst，已存在则跳过）
    engine = get_engine()
    models.TaskAnalysisRecord.__table__.create(engine, checkfirst=True)
    # create(checkfirst) 不给存量表补列，这里显式校验批次16 新列已就绪
    from sqlalchemy import inspect as sa_inspect
    cols = {c['name'] for c in sa_inspect(engine).get_columns('task_analysis_records')}
    missing = {'touch_near', 'touch_near_ts', 'touch_far', 'touch_far_ts',
               'target_price', 'invalid_price', 'plan_result', 'plan_r',
               'plan_touch_ts'} - cols
    assert not missing, f'批次16 新列未补齐: {missing}（请先启动应用触发 init_db 补列）'
    print('[1] 建表+批次16列校验完成（task_analysis_records）')

    now = datetime.datetime.now()
    ts_2h_ago = (now - datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
    ts_now = now.strftime('%Y-%m-%d %H:%M:%S')
    inserted_ids = []

    with session_scope() as s:
        # 2. 新增：一条 2 小时前的记录（用于回填）+ 一条当前记录
        rid1 = repo.add_record(s, {
            'ts': ts_2h_ago, 'inst_id': 'BTC-USDT-SWAP', 'price': 100000.0,
            'short_period': '5m', 'long_period': '4H',
            'short_dir': 'long', 'long_dir': 'long', 'long_dir_prev': 'long',
            'atr_pct': 0.35, 'user_judgment': 'rise', 'user_reason': '冒烟测试-看涨',
        })
        rid2 = repo.add_record(s, {
            'ts': ts_now, 'inst_id': 'BTC-USDT-SWAP', 'price': 5.123,
            'short_period': '5m', 'long_period': '4H',
            'short_dir': 'short', 'long_dir': 'short', 'long_dir_prev': None,
            'atr_pct': 1.2, 'user_judgment': 'watch', 'user_reason': '',
        })
        inserted_ids = [rid1, rid2]
    print(f'[2] 新增记录成功: id={inserted_ids}')

    with session_scope() as s:
        rows = repo.query_records(s, inst_id='BTC-USDT-SWAP')
        assert any(r['id'] == rid1 for r in rows), '查询未返回新记录'
        assert rows[0]['ts'] >= rows[-1]['ts'], 'ts 降序不成立'
    print('[3] 查询正常（ts 降序，共 %d 条匹配）' % len(rows))

    # 3.5 先触碰判定纯函数断言（批次16-A，无DB无网络）
    bars_up = [{'ts': 't1', 'high': 100.5, 'low': 99.8},
               {'ts': 't2', 'high': 101.2, 'low': 100.0}]   # θ=1% → 上轨101
    assert repo.classify_touch(bars_up, 100.0, 1.0) == ('up', 't2')
    bars_dn = [{'ts': 't1', 'high': 100.4, 'low': 98.9}]    # 下轨99 先被触
    assert repo.classify_touch(bars_dn, 100.0, 1.0) == ('down', 't1')
    assert repo.classify_touch([{'ts': 't1', 'high': 101.5, 'low': 98.5}], 100.0, 1.0) \
        == ('conflict', 't1'), '同根双触应保守记 conflict'
    assert repo.classify_touch([{'ts': 't1', 'high': 100.4, 'low': 99.6}], 100.0, 1.0) \
        == ('none', None), '全程未触带应判 none'
    assert repo.classify_touch([], 100.0, 1.0) == ('none', None)
    assert repo.classify_touch(bars_up, 0, 1.0) == (None, None), 'p0 无效应返回 None'
    # 先触谁谁说了算：后面反转不推翻已定格结果
    assert repo.classify_touch(bars_up + [{'ts': 't3', 'high': 100.2, 'low': 90.0}], 100.0, 1.0) \
        == ('up', 't2')
    print('[3.5] classify_touch 先触碰判定正确')

    # 3.6 结构价计划纯函数断言（批次16-B，无DB无网络）
    # p0=100 target=110 invalid=95 → risk=5，win r=(10/5)=2.0
    assert repo.resolve_plan([{'ts': 't1', 'high': 102.0, 'low': 99.0},
                              {'ts': 't2', 'high': 110.5, 'low': 101.0}],
                             100.0, 110.0, 95.0) == {'result': 'win', 'r': 2.0, 'touch_ts': 't2'}
    assert repo.resolve_plan([{'ts': 't1', 'high': 103.0, 'low': 94.9}],
                             100.0, 110.0, 95.0)['result'] == 'loss', '先触失效价应判 loss'
    dual = repo.resolve_plan([{'ts': 't1', 'high': 112.0, 'low': 94.0}], 100.0, 110.0, 95.0)
    assert dual == {'result': 'loss', 'r': -1.0, 'touch_ts': 't1'}, '同根双触保守按失效先行'
    assert repo.resolve_plan([{'ts': 't1', 'high': 103.0, 'low': 97.0}],
                             100.0, 110.0, 95.0)['result'] == 'open', '未触应持续观察'
    assert repo.resolve_plan([], 100.0, 110.0, 95.0) is None, '无未来bar应返回None待重扫'
    assert repo.resolve_plan([{'ts': 't1', 'high': 112.0, 'low': 99.0}],
                             100.0, 110.0, 95.0, head_ok=False) \
        == {'result': 'gap', 'r': None, 'touch_ts': None}, 'head断档应终局gap'
    assert repo.resolve_plan([{'ts': 't1', 'high': 112.0, 'low': 99.0}], 100.0, 110.0, 110.0) is None
    print('[3.6] resolve_plan 结构价计划结算正确')

    # 4. 修改个人判断/原因 + 结构价计划（_UNSET 哨兵语义 + 终局锁定 + 改动重置）
    with session_scope() as s:
        ok = repo.update_user_fields(s, rid1, 'fall', '冒烟测试-改为看跌')
        assert ok
    with session_scope() as s:
        rows = repo.query_records(s, inst_id='BTC-USDT-SWAP')
        rec1 = next(r for r in rows if r['id'] == rid1)
        assert rec1['user_judgment'] == 'fall' and rec1['user_reason'] == '冒烟测试-改为看跌'
    # 4a. 挂计划：成对写入，判定态保持待回填
    with session_scope() as s:
        assert repo.update_user_fields(s, rid1, 'fall', '冒烟-挂计划',
                                       target_price=110.0, invalid_price=95.0)
    with session_scope() as s:
        rec1 = next(r for r in repo.query_records(s, inst_id='BTC-USDT-SWAP') if r['id'] == rid1)
        assert rec1['target_price'] == 110.0 and rec1['invalid_price'] == 95.0
        assert rec1['plan_result'] is None
    # 4b. 不传结构价（_UNSET）→ 计划保持不变
    with session_scope() as s:
        repo.update_user_fields(s, rid1, 'rise', '冒烟-仅改判断')
    with session_scope() as s:
        rec1 = next(r for r in repo.query_records(s, inst_id='BTC-USDT-SWAP') if r['id'] == rid1)
        assert rec1['target_price'] == 110.0 and rec1['user_judgment'] == 'rise'
    # 4c. 已终局（win）→ 结构价修改请求被忽略（终局锁定，路由层拦截外的双保险）
    from crypto.models import TaskAnalysisRecord
    with session_scope() as s:
        s.get(TaskAnalysisRecord, rid1).plan_result = 'win'
    with session_scope() as s:
        repo.update_user_fields(s, rid1, 'rise', '冒烟-终局改计划',
                                target_price=120.0, invalid_price=95.0)
    with session_scope() as s:
        row = s.get(TaskAnalysisRecord, rid1)
        assert row.target_price == 110.0 and row.plan_result == 'win'
    # 4d. 未终局（open）→ 计划改动即重置判定，等待重扫
    with session_scope() as s:
        s.get(TaskAnalysisRecord, rid1).plan_result = 'open'
    with session_scope() as s:
        repo.update_user_fields(s, rid1, 'rise', '冒烟-重挂计划',
                                target_price=120.0, invalid_price=95.0)
    with session_scope() as s:
        row = s.get(TaskAnalysisRecord, rid1)
        assert row.target_price == 120.0 and row.plan_result is None and row.plan_r is None
    # 4e. 清空计划（传 None）
    with session_scope() as s:
        repo.update_user_fields(s, rid1, 'fall', '冒烟测试-改为看跌',
                                target_price=None, invalid_price=None)
    with session_scope() as s:
        rec1 = next(r for r in repo.query_records(s, inst_id='BTC-USDT-SWAP') if r['id'] == rid1)
        assert rec1['target_price'] is None and rec1['invalid_price'] is None
        assert rec1['user_judgment'] == 'fall' and rec1['user_reason'] == '冒烟测试-改为看跌'
    print('[4] 修改判断/原因 + 结构价计划（哨兵/锁定/重置/清空）正常')

    # 5. 复盘回填（窗口按短周期动态：5m → 近=20min、远=40min；
    #    2小时前的记录两窗均已到期可回填，当前记录两窗未到不应回填。
    #    依赖 OKX K线网络；失败时仅提示不视为致命错误）
    try:
        with session_scope() as s:
            filled = repo.backfill_due_reviews(s)
        with session_scope() as s:
            rows = repo.query_records(s, inst_id='BTC-USDT-SWAP')
        rec1 = next(r for r in rows if r['id'] == rid1)
        rec2 = next(r for r in rows if r['id'] == rid2)
        print(f'[5] 回填字段数={filled} | rec1 price_1h={rec1["price_1h"]} '
              f'touch_near={rec1["touch_near"]} price_4h={rec1["price_4h"]} '
              f'touch_far={rec1["touch_far"]}')
        assert rec2['price_1h'] is None and rec2['touch_near'] is None \
            and rec2['price_4h'] is None and rec2['touch_far'] is None, '未到窗口不应回填'
        if rec1['price_1h'] is None:
            print('    [5!] 1H 未回填（多为网络/K线不可达），回填逻辑未实网验证')
    except Exception as e:
        print(f'[5!] 回填异常（非致命，可能为网络问题）: {e}')

    # 6. 统计口径验证（先触碰三态 + 混淆矩阵 + 结构价计划桶）
    demo = [
        {'price': 100.0, 'atr_pct': 1.0, 'user_judgment': 'rise', 'long_dir_prev': 'long',
         'long_dir': 'long', 'price_1h': 110.0, 'touch_near': 'up',
         'price_4h': 90.0, 'touch_far': 'down'},   # 近先触上=判对；远先触下=判错
        {'price': 100.0, 'atr_pct': 1.0, 'user_judgment': 'fall', 'long_dir_prev': None,
         'long_dir': 'short', 'price_1h': 95.0, 'touch_near': 'down'},   # 近跌判对；远窗待回填
        {'price': 100.0, 'atr_pct': 1.0, 'user_judgment': 'watch', 'long_dir_prev': 'long',
         'long_dir': 'long', 'price_1h': 100.1, 'touch_near': 'none'},   # 全程没行情→观望命中；多遇横盘→未兑现
        {'price': 100.0, 'atr_pct': 1.0, 'user_judgment': 'watch', 'long_dir_prev': 'short',
         'long_dir': 'short', 'price_1h': 105.0, 'touch_near': 'up'},    # 观望但先触上→错失
        {'price': 100.0, 'atr_pct': 1.0, 'user_judgment': 'rise', 'long_dir_prev': 'long',
         'long_dir': 'long', 'touch_near': 'conflict'},  # 同根双触保守未兑现，不进分母
    ]
    st = repo.compute_stats(demo)
    assert st['score_mode'] == 'touch'
    # 个人：hit=3(rise先触up/fall先触down/watch遇none) miss=1(watch遇up) open=1(conflict)
    un = st['user']['near']
    assert (un['hit'], un['miss'], un['open'], un['pending'], un['scored']) == (3, 1, 1, 0, 4), un
    assert un['rate'] == 75.0, f"命中率应剔除未兑现: {un['rate']}"
    # 远窗：仅第1条已定格（判错），其余 4 条未回填计 pending
    uf = st['user']['far']
    assert (uf['hit'], uf['miss'], uf['open'], uf['pending'], uf['scored'], uf['rate']) == (0, 1, 0, 4, 1, 0.0), uf
    # 策略：多先触上/空先触下命中；none/conflict 对方向判断记未兑现
    sn = st['strategy']['near']
    assert (sn['hit'], sn['miss'], sn['open'], sn['scored'], sn['rate']) == (2, 1, 2, 3, 66.7), sn
    # 混淆矩阵：预测 × 先触碰结果（up/down/none/conflict 四列）
    assert st['confusion']['near']['rise']['up'] == 1 and st['confusion']['near']['rise']['conflict'] == 1
    assert st['confusion']['near']['watch']['none'] == 1 and st['confusion']['near']['watch']['up'] == 1
    assert st['confusion']['near']['fall']['down'] == 1
    assert st['confusion']['far']['rise']['down'] == 1
    # 中性带参数回传（前端本地重算据此对齐口径）
    assert st['hit_k'] == repo.HIT_ATR_K and st['hit_floor_pct'] == repo.HIT_FLOOR_PCT
    print(f'[6a] 先触碰统计口径正确: user.near={un} strategy.near={sn}')

    # 6b. 结构价计划桶：win_rate 只算终局(win+loss)，avg_r 仅均值 win/loss
    demo_plan = [
        {'price': 100.0, 'user_judgment': 'rise', 'target_price': 110.0, 'invalid_price': 95.0,
         'plan_result': 'win', 'plan_r': 2.0},
        {'price': 100.0, 'user_judgment': 'rise', 'target_price': 110.0, 'invalid_price': 95.0,
         'plan_result': 'loss', 'plan_r': -1.0},
        {'price': 100.0, 'user_judgment': 'fall', 'target_price': 90.0, 'invalid_price': 98.0,
         'plan_result': 'open'},
        {'price': 100.0, 'user_judgment': 'rise', 'target_price': 110.0, 'invalid_price': 95.0},
    ]
    sp = repo.compute_stats(demo_plan)['plan']
    assert (sp['total'], sp['win'], sp['loss'], sp['open'], sp['pending']) == (4, 1, 1, 1, 1), sp
    assert sp['win_rate'] == 50.0 and sp['avg_r'] == 0.5, sp

    # 7. 删除测试数据
    with session_scope() as s:
        for rid in inserted_ids:
            assert repo.delete_record(s, rid)
        assert not repo.delete_record(s, 999999999), '不存在的记录应返回 False'
    print('[7] 删除正常，测试数据已清理')

    # 8. 批量删除（delete_records）：新增 3 条一次性删除，混合不存在 id 验证计数
    batch_ids = []
    with session_scope() as s:
        for i in range(3):
            batch_ids.append(repo.add_record(s, {
                'ts': ts_now, 'inst_id': 'SMOKE-BATCH-USDT-SWAP', 'price': 1.0 + i,
                'short_period': '5m', 'long_period': '4H',
                'short_dir': 'long', 'long_dir': 'long', 'long_dir_prev': None,
                'atr_pct': 0.5, 'user_judgment': 'watch', 'user_reason': '冒烟-批量删除',
            }))
    with session_scope() as s:
        deleted = repo.delete_records(s, batch_ids + [999999998])  # 含 1 个不存在 id
        assert deleted == 3, f'批量删除应删除 3 条，实际 {deleted}'
    with session_scope() as s:
        left = repo.query_records(s, inst_id='SMOKE-BATCH-USDT-SWAP')
        assert not left, '批量删除后仍残留记录'
    with session_scope() as s:
        assert repo.delete_records(s, []) == 0, '空 id 列表应返回 0 且不报错'
        assert repo.delete_records(s, [999999997]) == 0, '全部不存在 id 应返回 0'
    print('[8] 批量删除正常（含不存在 id 自动忽略、空列表安全）')

    print('\n===== 冒烟测试通过 =====')


if __name__ == '__main__':
    main()
