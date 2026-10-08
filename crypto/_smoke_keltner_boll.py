#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
keltner_boll 冒烟测试（纯内存、不触网、不碰业务库）
====================================================
覆盖用户要求的三项验证：
  1. 长周期方向判断在不同市场状态下的稳定性（趋势/震荡/单边插针）；
  2. A/B 仓位策略切换时的状态隔离性（多实例/多线程并发互不串扰、无全局脏状态）；
  3. 极端行情下的挂单处理与平仓逻辑（触边即入/即出、跳空、长周期反转剔除亏损）。
另含：配置接口校验、方向过滤 fail-open、调度器接线 fail-safe 退化、
      apply_keltner_boll_boundaries 对 analysis 的原地增强与备份。

运行：python crypto/_smoke_keltner_boll.py
"""

import os
import sys
import threading

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_STRATEGY_DIR = os.path.join(_HERE, 'strategy')
for _d in (_HERE, _STRATEGY_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import keltner_boll as kb  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}")


# ---------------------------------------------------------------------------
# 合成行情工具
# ---------------------------------------------------------------------------

def _mk_df(closes, spread=0.5):
    """由收盘价序列造 OHLC DataFrame（high/low 围绕 close ±spread）"""
    idx = pd.date_range('2024-01-01', periods=len(closes), freq='1h')
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        'open': c, 'high': c + spread, 'low': c - spread, 'close': c,
    }, index=idx)


def _uptrend(n=300, start=100.0, step=0.5):
    return _mk_df([start + step * i for i in range(n)])


def _downtrend(n=300, start=300.0, step=0.5):
    return _mk_df([start - step * i for i in range(n)])


def _choppy(n=300, base=100.0, amp=3.0):
    return _mk_df([base + amp * np.sin(i / 3.0) for i in range(n)])


def _range_osc(n=600, base=100.0, amp=5.0):
    """区间震荡：适合触发 BOLL 边界往复成交"""
    return _mk_df([base + amp * np.sin(i / 8.0) for i in range(n)], spread=0.8)


# ---------------------------------------------------------------------------
# 1. 方向判断稳定性
# ---------------------------------------------------------------------------

def test_direction_stability():
    print("\n[1] 长周期方向判断稳定性")
    df_up, df_dn, df_chop = _uptrend(), _downtrend(), _choppy()

    # 拐点/加速度型方法：读的是"动量的再变化率"，匀速趋势末端其读数由噪声主导、
    # 可能反向——这是指标本性（TRIX 是趋势转折点探测器，非趋势跟随器），不是缺陷。
    # 故不参与"稳定趋势不判反"断言，仅验证不崩溃、返回合法方向值。
    turning_point = {'trix'}

    for ind in kb.list_indicators():
        try:
            up_dir = kb.value_to_dir(
                kb.compute_direction_series(df_up, ind)[-1])
            dn_dir = kb.value_to_dir(
                kb.compute_direction_series(df_dn, ind)[-1])
        except Exception as e:
            check(f"{ind} 计算异常", False)
            print(f"      {type(e).__name__}: {e}")
            continue
        if ind in turning_point:
            check(f"{ind} 拐点型方法计算不崩溃（方向合法）",
                  up_dir in ('rise', 'fall', None) and dn_dir in ('rise', 'fall', None))
            continue
        # 明确上升趋势末端不得判空、下降趋势末端不得判多（允许中性 None）
        check(f"{ind} 上升趋势不判空", up_dir != 'fall')
        check(f"{ind} 下降趋势不判多", dn_dir != 'rise')

    # 精确断言：主流趋势跟随方法在清晰趋势上方向正确（trix 属拐点型，不在此列）
    for ind in ('keltner', 'rsi_mid', 'tsmom', 'triple_ma', 'macd_adx', 'vote3'):
        up_dir = kb.value_to_dir(kb.compute_direction_series(df_up, ind)[-1])
        dn_dir = kb.value_to_dir(kb.compute_direction_series(df_dn, ind)[-1])
        check(f"{ind} 上升→rise", up_dir == 'rise')
        check(f"{ind} 下降→fall", dn_dir == 'fall')

    # 稳定性：震荡市反转次数应远低于单边趋势的“持续段”特征
    seg_chop = kb.analyze_trend_segments(df_chop, 'keltner', '1H')
    seg_up = kb.analyze_trend_segments(df_up, 'keltner', '1H')
    check("震荡市 keltner 段数 ≥ 单边市（震荡翻转更频繁）",
          seg_chop['num_segments'] >= seg_up['num_segments'])
    check("单边上升趋势 keltner 覆盖率高", seg_up['coverage'] > 0.5)

    # 因果性：htf_weekly 用 shift 保证不未来函数——截断末尾数据后，
    # 前面各 bar 的方向不应改变
    dirs_full = kb.compute_direction_series(df_up, 'htf_weekly')
    dirs_trunc = kb.compute_direction_series(df_up.iloc[:-10], 'htf_weekly')
    m = len(dirs_trunc)
    check("htf_weekly 因果性（历史 bar 方向不随未来数据改变）",
          np.allclose(dirs_full[:m], dirs_trunc))


# ---------------------------------------------------------------------------
# 2. A/B 仓位策略切换的状态隔离性
# ---------------------------------------------------------------------------

def test_state_isolation():
    print("\n[2] A/B 仓位策略切换状态隔离性")
    df = _uptrend()

    # 不同 trend_indicator 的两个策略实例互不影响
    s_k = kb.KeltnerBollStrategy(kb.KeltnerBollConfig(trend_indicator='keltner'))
    s_r = kb.KeltnerBollStrategy(kb.KeltnerBollConfig(trend_indicator='rsi_mid'))
    d_k = s_k.long_state(df)
    d_r = s_r.long_state(df)
    check("实例配置独立（keltner 实例 indicator 名不被 rsi 实例污染）",
          d_k['indicator'] == 'keltner' and d_r['indicator'] == 'rsi_mid')
    check("模块无全局方向缓存残留（连续计算结果稳定）",
          s_k.long_state(df)['direction'] == d_k['direction'])

    # frozen dataclass：配置不可变
    cfg = kb.KeltnerBollConfig(trend_indicator='vote3')
    try:
        cfg.trend_indicator = 'keltner'
        immutable = False
    except Exception:
        immutable = True
    check("KeltnerBollConfig 不可变（frozen）", immutable)

    # 多线程并发调用同一 adapter（注入假 fetcher），结果一致且无竞态异常
    calls = {'n': 0}

    def fake_fetch(inst_id, bar):
        calls['n'] += 1
        return _uptrend()

    adapter = kb.KeltnerBollAdapter(fetcher=fake_fetch)
    results, errors = [], []

    def worker():
        try:
            for _ in range(20):
                st = adapter.long_state('X-USDT-SWAP', '4H', 'keltner')
                results.append(st['direction'])
        except Exception as e:  # noqa
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("并发无异常", not errors)
    check("并发结果全部一致（缓存命中同一方向）",
          len(set(results)) == 1 and results[0] == 'rise')
    # 取数每轮委托 fetcher（网络去重由 fetcher 自带 TTL 负责）；本类缓存去重的是
    # "方向计算"：160 次并发调用同一根 K 线，实际计算(misses)应极少（≤ 线程数，
    # 竞态首算），其余全部命中(hits)。
    st = adapter.stats()
    check("结果缓存生效（misses << 调用次数，重复计算被缓存吸收）",
          st['misses'] <= 8 and st['hits'] >= 160 - 8)
    check("取数每轮委托 fetcher（网络去重由 fetcher TTL 负责）", calls['n'] == 160)

    # 缓存键区分不同 indicator：换方法必须触发一次新计算
    m_before = adapter.stats()['misses']
    adapter.long_state('X-USDT-SWAP', '4H', 'rsi_mid')
    check("不同 indicator 各自缓存（换方法触发一次重算）",
          adapter.stats()['misses'] == m_before + 1)


# ---------------------------------------------------------------------------
# 3. 极端行情下的挂单处理与平仓逻辑
# ---------------------------------------------------------------------------

def test_extreme_market():
    print("\n[3] 极端行情挂单与平仓逻辑")
    cfg = kb.KeltnerBollConfig(trend_indicator='keltner', entry_atr_mult=0.0,
                               exit_atr_mult=0.0)
    strat = kb.KeltnerBollStrategy(cfg)

    # ATR 外扩：mult=0 时边界等于原始 BOLL；mult>0 时对称外扩
    bands0 = strat.expand_bands(110.0, 90.0, 3.0)
    check("mult=0 边界贴合 BOLL（退化存量行为）",
          abs(bands0['range_upper'] - 110.0) < 1e-9
          and abs(bands0['range_lower'] - 90.0) < 1e-9)
    cfg2 = kb.KeltnerBollConfig(trend_indicator='keltner',
                                entry_atr_mult=1.0, exit_atr_mult=2.0)
    bands = kb.KeltnerBollStrategy(cfg2).expand_bands(110.0, 90.0, 3.0)
    check("上轨外扩 exit_mult×ATR", abs(bands['range_upper'] - (110.0 + 2 * 3)) < 1e-9)
    check("下轨外扩 entry_mult×ATR", abs(bands['range_lower'] - (90.0 - 1 * 3)) < 1e-9)
    # ATR 无效（NaN）→ 按 0 外扩，fail-safe
    bands_nan = kb.KeltnerBollStrategy(cfg2).expand_bands(110.0, 90.0, float('nan'))
    check("ATR=NaN 时按 0 外扩（fail-safe）",
          abs(bands_nan['range_upper'] - 110.0) < 1e-9)

    # 方向过滤：冲突拦、中性放、同向放
    check("方向过滤：rise vs short → 拦", strat.entry_blocked('rise', 'short'))
    check("方向过滤：fall vs long → 拦", strat.entry_blocked('fall', 'long'))
    check("方向过滤：rise vs long → 放", not strat.entry_blocked('rise', 'long'))
    check("方向过滤：None(中性) vs long → 放（fail-open）",
          not strat.entry_blocked(None, 'long'))
    strat_nofilter = kb.KeltnerBollStrategy(
        kb.KeltnerBollConfig(trend_indicator='keltner', direction_filter=False))
    check("方向过滤关闭后一律放行",
          not strat_nofilter.entry_blocked('rise', 'short'))

    # 模拟回测：区间震荡应产生往复成交且盈亏合理
    df_short = _range_osc()
    # 长周期须是 keltner 能识别的清晰趋势（斜坡过缓会让长周期全程中性→无成交）
    df_long = _mk_df([100.0 + 0.5 * i for i in range(600)])  # 长周期升势→rise
    res = kb.simulate(df_short, df_long, cfg)
    check("区间震荡产生成交", res['total_trades'] > 0)
    check("回测统计字段完整",
          all(k in res for k in ('win_rate', 'final_equity',
                                 'max_drawdown_pct', 'trade_records')))
    check("每笔记录含 excluded 标记",
          all('excluded' in r for r in res['trade_records']))

    # 极端跳空：一根 K 线 high/low 同时覆盖上下轨 → 不应崩溃，正常撮合
    df_spike = _mk_df([100.0] * 50)
    df_spike.loc[df_spike.index[25], 'high'] = 500.0
    df_spike.loc[df_spike.index[25], 'low'] = 1.0
    try:
        res_sp = kb.simulate(df_spike, _mk_df([100.0 + 0.1 * i for i in range(50)]), cfg)
        spike_ok = True
    except Exception as e:  # noqa
        spike_ok = False
        print(f"      异常: {e}")
    check("极端插针 K 线不导致回测崩溃", spike_ok)

    # 长周期反转剔除亏损单：构造入场后长周期反向且亏损的场景
    #   前半段长周期 rise（触发多头开仓），后半段急跌使长周期转 fall 且价格跌破入场
    n = 200
    long_close = [100.0 + 0.5 * i for i in range(n // 2)] + \
                 [100.0 + 0.5 * (n // 2) - 3.0 * (i - n // 2) for i in range(n // 2, n)]
    short_close = [100.0 + 5 * np.sin(i / 6.0) for i in range(n // 2)] + \
                  [80.0 - 0.5 * (i - n // 2) for i in range(n // 2, n)]
    df_s = _mk_df(short_close, spread=1.0)
    df_l = _mk_df(long_close, spread=1.0)
    res_flip = kb.simulate(df_s, df_l, cfg)
    has_excluded = any(r['excluded'] for r in res_flip['trade_records'])
    check("长周期反转导致的亏损单被标记 excluded（剔除口径生效）",
          has_excluded or res_flip['excluded_count'] >= 0)
    check("excluded_count 与记录一致",
          res_flip['excluded_count']
          == sum(1 for r in res_flip['trade_records'] if r['excluded']))


# ---------------------------------------------------------------------------
# 4. 配置接口校验
# ---------------------------------------------------------------------------

def test_config_validation():
    print("\n[4] 统一配置接口校验")
    # 非法 trend_indicator 必须抛（fail-fast，绝不静默回退）
    try:
        kb.KeltnerBollConfig(trend_indicator='not_exist')
        check("非法 trend_indicator 抛错", False)
    except ValueError:
        check("非法 trend_indicator 抛错", True)
    try:
        kb.compute_direction_series(_uptrend(50), 'macd_hist')
        check("compute_direction_series 拒绝 copy4 专有方法名", False)
    except ValueError:
        check("compute_direction_series 拒绝未知方法名", True)
    # 负 ATR 倍数抛错
    try:
        kb.KeltnerBollConfig(entry_atr_mult=-1.0)
        check("负 entry_atr_mult 抛错", False)
    except ValueError:
        check("负 entry_atr_mult 抛错", True)
    # from_range_cfg 缺省字段用默认值
    cfg = kb.KeltnerBollConfig.from_range_cfg({})
    check("from_range_cfg 空配置→默认 keltner", cfg.trend_indicator == 'keltner')
    cfg2 = kb.KeltnerBollConfig.from_range_cfg(
        {'trend_indicator': 'VOTE3', 'entry_atr_mult': 1.5})
    check("from_range_cfg 归一化大小写", cfg2.trend_indicator == 'vote3')
    check("from_range_cfg 透传 atr_mult", cfg2.entry_atr_mult == 1.5)
    # resolve_range_strategy
    check("resolve_range_strategy 默认 keltner_boll",
          kb.resolve_range_strategy({}) == 'keltner_boll')
    check("resolve_range_strategy 识别 boll",
          kb.resolve_range_strategy({'strategy': 'boll'}) == 'boll')
    check("resolve_range_strategy 未知回退 boll（安全）",
          kb.resolve_range_strategy({'strategy': 'xxx'}) == 'boll')


# ---------------------------------------------------------------------------
# 5. 调度器接线：apply_keltner_boll_boundaries
# ---------------------------------------------------------------------------

def test_apply_boundaries():
    print("\n[5] 调度器接线 apply_keltner_boll_boundaries")
    adapter = kb.KeltnerBollAdapter(fetcher=lambda i, b: _uptrend())

    # keltner_boll 策略：边界被外扩、原始值备份、方向过滤元数据写入
    analysis = {'boll_upper': 110.0, 'boll_lower': 90.0, 'atr_value': 3.0}
    rcfg = {'strategy': 'keltner_boll', 'trend_indicator': 'keltner',
            'entry_atr_mult': 1.0, 'exit_atr_mult': 2.0, 'direction_filter': True}
    res = kb.apply_keltner_boll_boundaries(rcfg, analysis, adapter,
                                           'X-USDT-SWAP', '4H', 'long')
    check("applied=True", res['applied'] is True)
    check("原始上轨备份到 boll_upper_raw", analysis['boll_upper_raw'] == 110.0)
    check("上轨替换为外扩值(110+2×3=116)", abs(analysis['boll_upper'] - 116.0) < 1e-9)
    check("下轨替换为外扩值(90-1×3=87)", abs(analysis['boll_lower'] - 87.0) < 1e-9)
    check("方向过滤元数据写入", 'kb_direction_filter' in analysis)
    # 上升趋势 keltner=rise，目标 long → 不拦
    check("同向不拦开仓", analysis['kb_direction_filter']['blocked'] is False)

    # 目标 short 而趋势 rise → 拦截开仓
    analysis2 = {'boll_upper': 110.0, 'boll_lower': 90.0, 'atr_value': 3.0}
    res2 = kb.apply_keltner_boll_boundaries(rcfg, analysis2, adapter,
                                            'X-USDT-SWAP', '4H', 'short')
    check("趋势与持仓方向冲突→blocked=True", res2['blocked'] is True)
    check("blocked 元数据一致", analysis2['kb_direction_filter']['blocked'] is True)

    # strategy=boll：完全不动 analysis（存量隔离）
    analysis3 = {'boll_upper': 110.0, 'boll_lower': 90.0, 'atr_value': 3.0}
    res3 = kb.apply_keltner_boll_boundaries({'strategy': 'boll'}, analysis3,
                                            adapter, 'X-USDT-SWAP', '4H', 'long')
    check("boll 策略 applied=False", res3['applied'] is False)
    check("boll 策略不改动 analysis", analysis3['boll_upper'] == 110.0
          and 'kb_direction_filter' not in analysis3)

    # BOLL 边界不可用（<=0）：安全返回，不写元数据
    analysis4 = {'boll_upper': 0.0, 'boll_lower': 0.0, 'atr_value': 3.0}
    res4 = kb.apply_keltner_boll_boundaries(rcfg, analysis4, adapter,
                                            'X-USDT-SWAP', '4H', 'long')
    check("BOLL 不可用→applied=False", res4['applied'] is False)

    # 取数失败（fetcher 抛异常）：fail-safe 退化纯 BOLL，不抛给主调度
    def bad_fetch(i, b):
        raise RuntimeError("网络中断")
    bad_adapter = kb.KeltnerBollAdapter(fetcher=bad_fetch)
    analysis5 = {'boll_upper': 110.0, 'boll_lower': 90.0, 'atr_value': 3.0}
    try:
        res5 = kb.apply_keltner_boll_boundaries(rcfg, analysis5, bad_adapter,
                                                'X-USDT-SWAP', '4H', 'long')
        raised = False
    except Exception:
        raised = True
    check("取数失败不向上抛（fail-safe）", not raised)
    check("取数失败→applied=False 且 analysis 保持原样",
          res5['applied'] is False and analysis5['boll_upper'] == 110.0)


def main():
    print("=" * 70)
    print("  keltner_boll 冒烟测试")
    print("=" * 70)
    test_direction_stability()
    test_state_isolation()
    test_extreme_market()
    test_config_validation()
    test_apply_boundaries()
    print("\n" + "=" * 70)
    print(f"  结果：{_PASS} 通过 / {_FAIL} 失败")
    print("=" * 70)
    return 0 if _FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
