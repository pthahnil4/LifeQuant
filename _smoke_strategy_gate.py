# -*- coding: utf-8 -*-
"""策略计算锁自检：一把锁 / 两种降级 / 可重入 / 各铺锁点可导入（只读，不发信不下单）

为何单独留这个自检：「两种导入身份必须共用同一把锁」靠 builtins 传递，
只要有人把 strategy_gate 改成普通模块级变量就会默默失效（变成两把锁，
串行化是假象），而这种失效在业务代码里看不出来——只能这样直接对撞才能发现。

运行：python _smoke_strategy_gate.py     （全通过时 exit 0）
"""
import os
import sys
import threading
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

fails = []


def ck(cond, label, extra=''):
    print(('  [OK  ] ' if cond else '  [FAIL] ') + label + (f'  {extra}' if extra else ''))
    if not cond:
        fails.append(label)


print('== 1) 两种导入身份必须拿到同一把锁 ==')
import crypto.task.strategy_gate as g_pkg          # noqa: E402
sys.path.insert(0, ROOT + r'\crypto\task')
import strategy_gate as g_bare                     # noqa: E402
ck(g_pkg.PRO3_LOCK is g_bare.PRO3_LOCK, 'package vs bare: PRO3_LOCK 同一对象',
   f'{id(g_pkg.PRO3_LOCK)} / {id(g_bare.PRO3_LOCK)}')

print('\n== 2) 忙时两种降级方式各按契约 ==')


@g_pkg.pro3_locked(default_timeout=0.3, on_busy='none')
def returns_none(x=1):
    return {'v': x}


@g_pkg.pro3_locked(default_timeout=0.3, on_busy='raise')
def raises(x=1):
    return (x, x + 1)


box = {}
started = threading.Event()


def holder():
    with g_pkg.pro3_exclusive():
        started.set()
        box['hold'] = threading.Event()
        box['hold'].wait(6)


th = threading.Thread(target=holder, daemon=True)
th.start()
started.wait(3)
ck(g_pkg.pro3_locked_now(), '持锁期间 pro3_locked_now() 为真')

t0 = __import__('time').time()
ck(returns_none() is None, "on_busy='none' 忙时返回 None")
ck(raises.__name__ == 'raises', '装饰后保留函数名（functools.wraps）')
try:
    raises()
    ck(False, "on_busy='raise' 忙时应抛 StrategyBusy")
except g_bare.StrategyBusy as e:
    ck(True, "on_busy='raise' 忙时抛 StrategyBusy（跨身份也能捕获）", str(e)[:46])
except Exception as e:
    ck(False, '抛出的不是 StrategyBusy', repr(e))
spent = __import__('time').time() - t0
ck(0.25 < spent < 1.2, f'等待时长按 timeout 生效（{spent:.2f}s）')

box['hold'].set()
th.join(8)
ck(returns_none(7) == {'v': 7}, '锁释放后正常执行')
ck(raises(2) == (2, 3), "on_busy='raise' 空闲时原样返回元组")

print('\n== 3) 同线程嵌套不自锁（RLock） ==')
try:
    with g_pkg.pro3_exclusive(timeout=1):
        ck(returns_none(3) == {'v': 3}, '外层持锁时内层装饰函数仍可执行')
except Exception as e:
    ck(False, '嵌套应不抛异常', repr(e))


@g_pkg.pro3_locked(default_timeout=5.0, on_busy='raise')
def outer():
    return inner()


@g_pkg.pro3_locked(default_timeout=5.0, on_busy='raise')
def inner():
    return 'nested-ok'


ck(outer() == 'nested-ok', '装饰函数之间嵌套不死锁')

print('\n== 4) 各铺锁点模块可导入且已挂锁 ==')
for mod, attr in (('crypto.real_strategy_adapter', 'calculate_single_coin_data'),
                  ('crypto.real_strategy_adapter', 'calculate_dual_period_data'),
                  ('crypto.strategy_util', '_get_single_period_detail'),
                  ('crypto.strategy_util', '_get_dual_period_detail'),
                  ('crypto.task.strategy_adapter', 'DualPeriodStrategyAdapter'),
                  ('crypto.task.trend_compare', 'TrendCompareService')):
    try:
        m = __import__(mod, fromlist=['*'])
        target = getattr(m, attr)
        wrapped = getattr(target, '__wrapped__', None)
        if attr == 'DualPeriodStrategyAdapter':
            wrapped = getattr(target.analyze, '__wrapped__', None)
        elif attr == 'TrendCompareService':
            wrapped = getattr(target._replay, '__wrapped__', None)
        ck(wrapped is not None, f'{mod}.{attr} 已被 pro3_locked 包装',
           '有 __wrapped__' if wrapped else '缺 __wrapped__＝没铺到')
    except Exception as e:
        ck(False, f'{mod} 导入失败', repr(e)[:80])
        traceback.print_exc(limit=2)

print('\n== 5) 锁参数取值非法要当场报错（不能默默变成不锁） ==')
try:
    @g_pkg.pro3_locked(on_busy='ignore')
    def bad():
        return 1
    ck(False, '非法 on_busy 应在装饰时就失败')
except ValueError as e:
    ck(True, '非法 on_busy 立即 ValueError', str(e)[:44])

print('\n结果：' + (f'失败 {len(fails)} 项 -> {fails}' if fails else '全部通过'))
sys.exit(1 if fails else 0)
