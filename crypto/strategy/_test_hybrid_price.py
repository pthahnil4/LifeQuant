# type: ignore
# -*- coding: utf-8 -*-
"""
离线验证脚本：短周期信号算法可配置（diff/hybrid）+ 开/平仓价格参数
==========================================
1. hybrid_state_machine_step 单元测试
2. apply_price_types / apply_signal_algo 参数校验测试
3. 单周期 _run_strategy：不同价格配置下开/平仓价取值 + 两种信号算法验证
4. 双周期 _run_dual_strategy：限价单/平仓价取值 + 两种信号算法验证
5. strategy_util 归一化测试
"""
import sys
import os

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_DIR = os.path.dirname(os.path.abspath(__file__))
_CRYPTO = os.path.dirname(_DIR)
for p in (_DIR, _CRYPTO):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np
import pandas as pd

import pro3_singletimeframe as single
import pro3_dualtimeframe as dual

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ================================================================
# 1. 混合状态机单元测试
# ================================================================
print("\n=== 1. hybrid_state_machine_step 单元测试 ===")
step = single.hybrid_state_machine_step

# 场景1：常态 + 平滑差>0 + 上一根MACD主线>0 → 立即确认 rise
j, d, m = step("smoothed_histogram", None, "fall", histogram=1.0, smoothed_histogram=0.5, prev_macd=0.3)
check("平滑差>0 + prevMACD>0 → 立即rise", (j, m) == ("smoothed_histogram", "rise"), f"got {(j, d, m)}")

# 场景2：常态 + 平滑差>0 + 上一根MACD主线<=0 → 进入 waitRise 等待态，方向不变
j, d, m = step("smoothed_histogram", None, "fall", histogram=-0.2, smoothed_histogram=-0.5, prev_macd=-0.3)
check("平滑差>0 + prevMACD<0 → waitRise等待", (j, d, m) == ("histogram", "waitRise", "fall"), f"got {(j, d, m)}")

# 场景3：waitRise 态 + histogram 站上零轴 → 确认 rise，回常态
j, d, m = step("histogram", "waitRise", "fall", histogram=0.1, smoothed_histogram=-0.1, prev_macd=-0.1)
check("waitRise + histogram>0 → 确认rise", (j, m) == ("smoothed_histogram", "rise"), f"got {(j, d, m)}")

# 场景4：waitRise 态 + histogram 未站上零轴 → 维持 fall，仍在等待
j, d, m = step("histogram", "waitRise", "fall", histogram=-0.05, smoothed_histogram=-0.2, prev_macd=-0.1)
check("waitRise + histogram<=0 → 维持fall", (j, d, m) == ("histogram", "waitRise", "fall"), f"got {(j, d, m)}")

# 场景5：常态 + 平滑差<=0 + 上一根MACD主线<0 → 立即确认 fall
j, d, m = step("smoothed_histogram", None, "rise", histogram=-1.0, smoothed_histogram=-0.5, prev_macd=-0.3)
check("平滑差<0 + prevMACD<0 → 立即fall", (j, m) == ("smoothed_histogram", "fall"), f"got {(j, d, m)}")

# 场景6：常态 + 平滑差<=0 + 上一根MACD主线>=0 → 进入 waitFall
j, d, m = step("smoothed_histogram", None, "rise", histogram=0.2, smoothed_histogram=0.5, prev_macd=0.3)
check("平滑差<0 + prevMACD>0 → waitFall等待", (j, d, m) == ("histogram", "waitFall", "rise"), f"got {(j, d, m)}")

# 场景7：waitFall 态 + histogram 跌破零轴 → 确认 fall
j, d, m = step("histogram", "waitFall", "rise", histogram=-0.1, smoothed_histogram=0.1, prev_macd=0.1)
check("waitFall + histogram<0 → 确认fall", (j, m) == ("smoothed_histogram", "fall"), f"got {(j, d, m)}")

# 场景8：首根bar（prev_macd=None）+ 平滑差>0 → 不能立即确认，进等待态
j, d, m = step("smoothed_histogram", None, None, histogram=0.5, smoothed_histogram=0.2, prev_macd=None)
check("prev_macd=None → 进等待态不误判", (j, d, m) == ("histogram", "waitRise", None), f"got {(j, d, m)}")

# 一致性：单/双周期模块共享同一函数
check("单/双周期共享同一状态机函数", dual.hybrid_state_machine_step is single.hybrid_state_machine_step)

# ================================================================
# 2. apply_price_types 参数校验
# ================================================================
print("\n=== 2. apply_price_types 参数校验 ===")
single.apply_price_types(None, None)
check("默认配置 开仓=close", single.ENTRY_PRICE_TYPE == 'close', single.ENTRY_PRICE_TYPE)
check("默认配置 平仓=open", single.EXIT_PRICE_TYPE == 'open', single.EXIT_PRICE_TYPE)

single.apply_price_types('CLOSE ', ' Open')
check("大小写/空格容错 开仓=close", single.ENTRY_PRICE_TYPE == 'close', single.ENTRY_PRICE_TYPE)
check("大小写/空格容错 平仓=open", single.EXIT_PRICE_TYPE == 'open', single.EXIT_PRICE_TYPE)

try:
    single.apply_price_types('high', None)
    check("非法开仓类型应抛异常", False)
except ValueError:
    check("非法开仓类型应抛异常", True)

try:
    dual.apply_price_types(None, 'zero')  # 双周期不支持 zero
    check("双周期非法平仓类型应抛异常", False)
except ValueError:
    check("双周期非法平仓类型应抛异常", True)

# 还原默认
single.apply_price_types('open', 'close')
dual.apply_price_types('open', 'close')

# ================================================================
# 2b. apply_signal_algo 参数校验
# ================================================================
print("\n=== 2b. apply_signal_algo 参数校验 ===")
check("单周期默认算法=diff（平滑差）", single.SHORT_SIGNAL_ALGO == 'diff', single.SHORT_SIGNAL_ALGO)
check("双周期默认算法=diff（平滑差）", dual.SHORT_SIGNAL_ALGO == 'diff', dual.SHORT_SIGNAL_ALGO)

single.apply_signal_algo(' HYBRID ')
check("大小写/空格容错 → hybrid", single.SHORT_SIGNAL_ALGO == 'hybrid', single.SHORT_SIGNAL_ALGO)
single.apply_signal_algo(None)
check("None 保持不变", single.SHORT_SIGNAL_ALGO == 'hybrid', single.SHORT_SIGNAL_ALGO)

try:
    single.apply_signal_algo('macd')
    check("非法算法应抛异常", False)
except ValueError:
    check("非法算法应抛异常", True)

try:
    dual.apply_signal_algo('smooth')
    check("双周期非法算法应抛异常", False)
except ValueError:
    check("双周期非法算法应抛异常", True)

# 还原默认
single.apply_signal_algo('diff')
dual.apply_signal_algo('diff')

# ================================================================
# 3. 合成K线数据
# ================================================================
def make_df(n=300, seed=42, period=18.0, amp=12.0):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 100 + amp * np.sin(t / period) + np.cumsum(rng.normal(0, 0.4, n)) * 0.5
    open_ = close * 1.001  # open 恒 ≠ close，且两集合数值不相交
    # high/low 带宽放宽到 ±2%：限价单在下一根 bar 判定成交，需覆盖上一根的挂单价
    high = np.maximum(open_, close) * 1.02
    low = np.minimum(open_, close) * 0.98
    df = pd.DataFrame({
        'open': open_, 'high': high, 'low': low, 'close': close,
        'volume': np.full(n, 1000.0),
    }, index=pd.date_range('2026-01-01', periods=n, freq='h'))
    df.index.name = 'datetime'
    return single.calculate_adx(df)


def locate_row(df, ts):
    """backtrader 的 datetime 可能有微秒误差，按最近索引匹配"""
    idx = df.index.get_indexer([pd.Timestamp(ts)], method='nearest')[0]
    return df.iloc[idx]


def near(a, b, tol=1e-8):
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))


# 关闭打印、跳过期末强平
for mod in (single, dual):
    mod.PRINT_MARKET = 0
    mod.PRINT_TRADE_OPS = 0
    mod.PRINT_TRADE_RECORDS = 0
    mod.PRINT_ORIGINAL_OUTPUT = 0
    mod.FAST_MODE = True

# ================================================================
# 4. 单周期价格取值验证
# ================================================================
print("\n=== 3. 单周期 _run_strategy 价格取值验证 ===")

def verify_single(entry_type, exit_type):
    single.apply_price_types(entry_type, exit_type)
    df = make_df()
    strat = single._run_strategy(df, is_dual_period=False)
    n_trades = len(strat.trades)
    check(f"[{entry_type}/{exit_type}] 产生了交易", n_trades >= 4, f"trades={n_trades}")
    ok_entry = ok_exit = True
    for i, (price, ts) in enumerate(zip(strat.trades, strat.trade_times)):
        row = locate_row(df, ts)
        if i % 2 == 0:  # 开仓
            if not near(price, row[entry_type]):
                ok_entry = False
                print(f"    开仓价不匹配 idx={i} price={price} {entry_type}={row[entry_type]} @{ts}")
        else:           # 平仓
            if not near(price, row[exit_type]):
                ok_exit = False
                print(f"    平仓价不匹配 idx={i} price={price} {exit_type}={row[exit_type]} @{ts}")
    check(f"[{entry_type}/{exit_type}] 所有开仓价均取 {entry_type}", ok_entry)
    check(f"[{entry_type}/{exit_type}] 所有平仓价均取 {exit_type}", ok_exit)
    return strat

s1 = verify_single('open', 'close')   # 新默认
s2 = verify_single('close', 'open')   # 旧配置反向
check("两种价格配置产生不同成交价", list(s1.trades) != list(s2.trades))

# 两种信号算法验证：
# diff 模式：MODIFY_FLAG 应严格等价于平滑差正负
single.apply_price_types('open', 'close')
single.apply_signal_algo('diff')
df_diff = make_df()
single._run_strategy(df_diff, is_dual_period=False)
sub = df_diff.dropna(subset=['MODIFY_FLAG', 'HIST_SMOOTH_DIFF'])
naive = np.where(sub['HIST_SMOOTH_DIFF'] > 0, 'rise', 'fall')
diff_cnt = int((sub['MODIFY_FLAG'].values != naive).sum())
check("diff模式：方向严格等于平滑差正负（无确认延迟）", diff_cnt == 0, f"diff={diff_cnt}")

# hybrid 模式：MODIFY_FLAG 与平滑差正负存在差异（确认延迟生效）
single.apply_signal_algo('hybrid')
df_hyb = make_df()
strat_chk = single._run_strategy(df_hyb, is_dual_period=False)
sub_h = df_hyb.dropna(subset=['MODIFY_FLAG', 'HIST_SMOOTH_DIFF'])
naive_h = np.where(sub_h['HIST_SMOOTH_DIFF'] > 0, 'rise', 'fall')
hyb_cnt = int((sub_h['MODIFY_FLAG'].values != naive_h).sum())
check("hybrid模式：方向与平滑差正负存在差异（确认延迟生效）", hyb_cnt > 0, f"diff={hyb_cnt}")
check("策略实例携带状态机状态字段", hasattr(strat_chk, 'justice_flag') and hasattr(strat_chk, 'dealjustice_flag'))
single.apply_signal_algo('diff')  # 还原默认

# ================================================================
# 5. 双周期价格取值验证
# ================================================================
print("\n=== 4. 双周期 _run_dual_strategy 价格取值验证 ===")

def verify_dual(entry_type, exit_type):
    dual.apply_price_types(entry_type, exit_type)
    # 短周期正弦波 → 信号频繁翻转，保证多次限价单成交
    df = make_df(n=500, seed=7, period=12.0, amp=15.0)
    df['LONG_DIRECTION'] = 'rise'  # 长周期恒多 → 短周期翻多开仓/翻空平仓
    strat = dual._run_dual_strategy(df, long_direction='rise')
    check(f"[{entry_type}/{exit_type}] 有限价单成交", strat.entry_orders_filled >= 2,
          f"filled={strat.entry_orders_filled}")
    # 开仓价必须来自 entry_type 价格集合（open/close 集合数值不相交）
    entry_vals = df[entry_type].values
    other_vals = df['close' if entry_type == 'open' else 'open'].values
    ok_entry = ok_exit = True
    open_i = 0
    for i, (price, ts) in enumerate(zip(strat.trades, strat.trade_times)):
        is_entry = (open_i < len(strat.trade_dirs)) and (i % 2 == 0)
        if i % 2 == 0:  # 开仓（限价单成交价=提交bar的 entry_type 价）
            in_entry_set = np.any(np.abs(entry_vals - float(price)) <= 1e-8 * max(1.0, abs(float(price))))
            in_other_set = np.any(np.abs(other_vals - float(price)) <= 1e-8 * max(1.0, abs(float(price))))
            if not in_entry_set or in_other_set:
                ok_entry = False
                print(f"    开仓价来源异常 idx={i} price={price}")
            # 成交bar必须覆盖限价
            row = locate_row(df, ts)
            if not (row['low'] - 1e-9 <= float(price) <= row['high'] + 1e-9):
                ok_entry = False
                print(f"    成交bar未覆盖限价 idx={i} price={price} low={row['low']} high={row['high']}")
            open_i += 1
        else:           # 平仓（市价 = 当前bar exit_type 价）
            row = locate_row(df, ts)
            if not near(price, row[exit_type]):
                ok_exit = False
                print(f"    平仓价不匹配 idx={i} price={price} {exit_type}={row[exit_type]} @{ts}")
    check(f"[{entry_type}/{exit_type}] 开仓限价均取 {entry_type} 且成交bar覆盖", ok_entry)
    check(f"[{entry_type}/{exit_type}] 平仓价均取 {exit_type}", ok_exit)
    return strat

d1 = verify_dual('open', 'close')
d2 = verify_dual('close', 'open')
check("双周期两种配置产生不同成交价", list(d1.trades) != list(d2.trades))

# 双周期两种信号算法验证：同一数据下 diff/hybrid 的 MODIFY_FLAG 序列应不同
dual.apply_price_types('open', 'close')
dual.apply_signal_algo('diff')
df_dd = make_df(n=500, seed=7, period=12.0, amp=15.0)
df_dd['LONG_DIRECTION'] = 'rise'
dual._run_dual_strategy(df_dd, long_direction='rise')
flags_diff = df_dd['MODIFY_FLAG'].fillna('_').tolist()

dual.apply_signal_algo('hybrid')
df_dh = make_df(n=500, seed=7, period=12.0, amp=15.0)
df_dh['LONG_DIRECTION'] = 'rise'
dual._run_dual_strategy(df_dh, long_direction='rise')
flags_hyb = df_dh['MODIFY_FLAG'].fillna('_').tolist()

check("双周期 diff/hybrid 短周期信号序列不同", flags_diff != flags_hyb)
sub_dd = df_dd.dropna(subset=['MODIFY_FLAG', 'HIST_SMOOTH_DIFF'])
naive_dd = np.where(sub_dd['HIST_SMOOTH_DIFF'] > 0, 'rise', 'fall')
check("双周期 diff模式：方向严格等于平滑差正负",
      int((sub_dd['MODIFY_FLAG'].values != naive_dd).sum()) == 0)
dual.apply_signal_algo('diff')  # 还原默认

# ================================================================
# 6. strategy_util 归一化测试
# ================================================================
print("\n=== 5. strategy_util 归一化 ===")
try:
    import strategy_util as su
    npt = su._normalize_price_type
    check("'OPEN ' → open", npt('OPEN ', 'close') == 'open')
    check("'xxx' → 回退默认", npt('xxx', 'close') == 'close')
    check("None → 回退默认", npt(None, 'open') == 'open')
    check("默认常量 开仓=close/平仓=open",
          su.DEFAULT_ENTRY_PRICE_TYPE == 'close' and su.DEFAULT_EXIT_PRICE_TYPE == 'open')
    nsa = su._normalize_signal_algo
    check("默认算法常量=diff", su.DEFAULT_SIGNAL_ALGO == 'diff')
    check("'HYBRID ' → hybrid", nsa('HYBRID ') == 'hybrid')
    check("'state_machine' 别名 → hybrid", nsa('state_machine') == 'hybrid')
    check("'smoothed_diff' 别名 → diff", nsa('smoothed_diff') == 'diff')
    check("非法值 → 回退 diff", nsa('xxx') == 'diff')
    check("None → 回退 diff", nsa(None) == 'diff')
except ImportError as e:
    print(f"  [SKIP] strategy_util 导入失败（可能依赖 Flask 环境）: {e}")

# ================================================================
# 汇总
# ================================================================
print("\n" + "=" * 50)
print(f"测试完成: PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
