#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
冒烟测试：合约下单步长（lotSz/minSz）三档验证
==============================================

背景（2026-09-01 实盘故障）：下单量取整长期硬编码 0.1 张，而各合约步长差异极大：
  XRP-USDT-SWAP  lotSz=0.01 minSz=0.01  → 折算出的 0.03 张被 round(…,1) 抹成 0，
                                          区间仓永不挂单（现象：仓位B纹丝不动）
  NEAR-USDT-SWAP lotSz=0.1  minSz=0.1   → 恰好等于硬编码值，唯一正常的币种
  POL-USDT-SWAP  lotSz=1    minSz=1     → 配置 0.5 张必被交易所拒（All operations failed）

本脚本用桩规格覆盖三档，验证：
  1. InstrumentSpecCache.steps()/quantize() 三档取整与最小下单量判定
  2. 挂单管理器 _q()/_in_pos() 与规格一致，且规格缺失时退回 0.1 张兜底
  3. 张数模式 _resolve_size：POL 0.5 张返回 0 并告警，NEAR/XRP 正常量化
  4. 平仓路径粉尘阈值按合约取：XRP 0.03 张不再被误清账本

运行：python crypto/task/_smoke_order_step.py
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from crypto.task.utils.instrument_spec import InstrumentSpecCache
from crypto.task.utils.position_order_manager import DualPositionOrderManager
from crypto.task.trend_range_trader import TrendRangeTrader

# 交易所实测规格（2026-09-01 拉取），三档步长
SPECS = {
    'XRP-USDT-SWAP': {'ct_val': 100.0, 'lot_sz': 0.01, 'min_sz': 0.01,
                      'max_lever': 75.0},
    'NEAR-USDT-SWAP': {'ct_val': 10.0, 'lot_sz': 0.1, 'min_sz': 0.1,
                       'max_lever': 50.0},
    'POL-USDT-SWAP': {'ct_val': 10.0, 'lot_sz': 1.0, 'min_sz': 1.0,
                      'max_lever': 50.0},
}

_fail = []


def check(name, got, want):
    ok = (abs(got - want) < 1e-9) if isinstance(want, float) else (got == want)
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: got={got} want={want}")
    if not ok:
        _fail.append(name)


def make_spec_cache():
    """桩规格缓存：绕过 __init__ 的 DB/网络依赖，直接注入实测规格。

    fetched_ts 置为当前时间，使 get_spec 命中新鲜缓存不走远程拉取。
    """
    sc = InstrumentSpecCache.__new__(InstrumentSpecCache)
    now = time.time()
    sc._mem = {k: dict(v, fetched_ts=now) for k, v in SPECS.items()}
    sc._lock = threading.Lock()
    sc._api = None
    sc.flag = '0'
    sc.ttl_seconds = 86400.0
    sc.cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '_smoke_spec_cache.json')
    return sc


def test_spec_steps():
    print("\n[1] InstrumentSpecCache.steps()/quantize() 三档")
    sc = make_spec_cache()
    check('XRP steps', sc.steps('XRP-USDT-SWAP'), (0.01, 0.01))
    check('NEAR steps', sc.steps('NEAR-USDT-SWAP'), (0.1, 0.1))
    check('POL steps', sc.steps('POL-USDT-SWAP'), (1.0, 1.0))
    # 规格缺失 → 兜底 0.1（沿用存量行为，不凭空放大敞口）
    check('未知合约 steps 兜底', sc.steps('NOPE-USDT-SWAP'), (0.1, 0.1))

    # XRP：0.5U×10x÷136.71 = 0.0366 张 → 向下取整到 0.03（旧代码 round→0.0 而失效）
    check('XRP quantize(0.0366)', sc.quantize('XRP-USDT-SWAP', 0.0366), 0.03)
    check('XRP quantize(0.009) 低于minSz', sc.quantize('XRP-USDT-SWAP', 0.009), 0.0)
    # 旧代码 round(0.073,1)=0.1 会把趋势仓量向上放大 37%，量化必须向下取整
    check('XRP quantize(0.073) 不放大', sc.quantize('XRP-USDT-SWAP', 0.073), 0.07)
    check('NEAR quantize(0.245)', sc.quantize('NEAR-USDT-SWAP', 0.245), 0.2)
    # POL：配置 0.5 张低于 minSz=1 → 返回 0，调用方须跳过下单
    check('POL quantize(0.5) 低于minSz', sc.quantize('POL-USDT-SWAP', 0.5), 0.0)
    check('POL quantize(2.7) 取整', sc.quantize('POL-USDT-SWAP', 2.7), 2.0)
    check('POL quantize(0.5,不强制minSz)',
          sc.quantize('POL-USDT-SWAP', 0.5, enforce_min_sz=False), 0.0)


def test_mgr_steps():
    print("\n[2] 挂单管理器 _q()/_in_pos()")
    mgr = DualPositionOrderManager.__new__(DualPositionOrderManager)
    mgr.spec_cache = make_spec_cache()
    check('XRP _q(0.0366)', mgr._q('XRP-USDT-SWAP', 0.0366), 0.03)
    check('POL _q(0.5)', mgr._q('POL-USDT-SWAP', 0.5), 0.0)
    check('NEAR _q(0.245)', mgr._q('NEAR-USDT-SWAP', 0.245), 0.2)
    check('_q(0)', mgr._q('XRP-USDT-SWAP', 0), 0.0)
    check('_in_pos(0.03) XRP真实小仓在场', mgr._in_pos(0.03), True)
    # 粉尘阈值已收窄为“只忽略浮点残差”：低于 minSz 的真实仓位不能从账本抹去
    # （抹去会被当成空仓触发重复开仓，比留着一张下不掉的单危险得多）
    check('_in_pos(0.001) 不足minSz的真实小仓仍算在场', mgr._in_pos(0.001), True)
    check('_in_pos(1e-12) 浮点残渣视为空仓', mgr._in_pos(1e-12), False)

    # 无规格来源（旧调用方/测试）→ 退回 0.1 张兜底，不抛异常
    mgr2 = DualPositionOrderManager.__new__(DualPositionOrderManager)
    mgr2.spec_cache = None
    check('无规格 _steps 兜底', mgr2._steps('XRP-USDT-SWAP'), (0.1, 0.1))
    check('无规格 _q(0.25)', mgr2._q('XRP-USDT-SWAP', 0.25), 0.2)


def _make_trader():
    tr = TrendRangeTrader.__new__(TrendRangeTrader)
    tr.spec_cache = make_spec_cache()
    tr._size_warned = set()
    return tr


def test_resolve_size():
    print("\n[3] _resolve_size 张数模式按合约校验")
    tr = _make_trader()
    ctx = {'rid': '', 'price': 0.0904, 'leverage': 10}
    # POL 区间仓实际配置：张数模式 0.5 张 < minSz=1 → 返回 0 且记一次告警
    check('POL contracts=0.5 → 0',
          tr._resolve_size('POL-USDT-SWAP',
                           {'size_mode': 'contracts', 'contracts': 0.5},
                           ctx, '区间仓'), 0.0)
    check('POL 告警已去重登记', 'POL-USDT-SWAP:区间仓:lot' in tr._size_warned, True)
    check('POL contracts=2.7 → 2',
          tr._resolve_size('POL-USDT-SWAP',
                           {'size_mode': 'contracts', 'contracts': 2.7},
                           ctx, '区间仓'), 2.0)
    check('POL 合法后告警登记清除',
          'POL-USDT-SWAP:区间仓:lot' in tr._size_warned, False)
    # XRP 张数模式 0.03 张合法（旧代码会被抹成 0）
    check('XRP contracts=0.03 → 0.03',
          tr._resolve_size('XRP-USDT-SWAP',
                           {'size_mode': 'contracts', 'contracts': 0.03},
                           ctx, '区间仓'), 0.03)
    # 金额模式：0.5U × 10x ÷ (100×1.3671) = 0.0366 → 0.03 张（XRP 区间仓真实配置）
    check('XRP usd=0.5/10x → 0.03',
          tr._resolve_size('XRP-USDT-SWAP',
                           {'size_mode': 'usd', 'notional_usd': 0.5},
                           {'rid': '', 'price': 1.3671, 'leverage': 10},
                           '区间仓'), 0.03)
    # NEAR 区间仓：0.5U × 10x ÷ (10×2.037) = 0.245 → 0.2 张（与实盘日志一致）
    check('NEAR usd=0.5/10x → 0.2',
          tr._resolve_size('NEAR-USDT-SWAP',
                           {'size_mode': 'usd', 'notional_usd': 0.5},
                           {'rid': '', 'price': 2.037, 'leverage': 10},
                           '区间仓'), 0.2)


def test_close_dust():
    print("\n[4] 平仓路径粉尘阈值按合约（幽灵持仓防护）")
    tr = _make_trader()
    check('XRP _q(0.03) 可平', tr._q('XRP-USDT-SWAP', 0.03), 0.03)
    check('XRP minSz', tr._steps('XRP-USDT-SWAP')[1], 0.01)
    # 旧代码 `0.01 < held < 0.1` 会把 XRP 真实 0.03 张判为粉尘并清零账本
    xrp_min = tr._steps('XRP-USDT-SWAP')[1]
    check('XRP 0.03张不落入粉尘分支',
          (tr.POS_DUST <= 0.03 < xrp_min), False)
    # POL 0.5 张确实无法下单平仓 → 必须走粉尘清零出口，否则清理死循环
    pol_min = tr._steps('POL-USDT-SWAP')[1]
    check('POL 0.5张落入粉尘清零分支',
          (tr.POS_DUST <= 0.5 < pol_min), True)
    check('POL _q(0.5) 不可下单', tr._q('POL-USDT-SWAP', 0.5), 0.0)
    # 反向持仓强平阈值：POL 上 0.05 张零头不该被当反向单反复强平失败
    check('POL 反向零头0.05 < minSz', 0.05 + 1e-9 < pol_min, True)


if __name__ == '__main__':
    test_spec_steps()
    test_mgr_steps()
    test_resolve_size()
    test_close_dust()
    print("\n" + "=" * 60)
    if _fail:
        print(f"[FAIL] {len(_fail)} 项未通过: {_fail}")
        sys.exit(1)
    print("[OK] 全部通过：三档步长(XRP 0.01 / NEAR 0.1 / POL 1)行为正确")
