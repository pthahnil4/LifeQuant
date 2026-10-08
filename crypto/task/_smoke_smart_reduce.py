#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
智能减仓（smart_reduce 全局强制）冒烟测试
=================================
覆盖点：
1. 配置块缺失即全局启用（代码缺省：enabled=True + 默认白名单覆盖全部场景），永不全平
2. classify_reason：'智能减仓' 分类优先于 '反转'/'止损'/'止盈'
3. _calc_keep_contracts：目标保证金折算 / minSz 兜底（取大） / 杠杆 clamp / 规格缺失
4. _close_bucket_smart 分支：显式关闭降级全平、白名单收窄降级、盈亏查询失败
   仍减仓、盈利也减仓、持仓低于保留目标跳过、减仓量不足minSz跳过、
   规格缺失跳过、分批定量请求被「持仓−保留量」封顶
5. _record_residual / _remove_residual（内存态）
6. _smart_keep_qty 残留达标识别（启用/关闭/规格缺失）

全部使用内存 mock，不调用交易所 API、不写数据库。

曾踩过的坑：旧版第 1 项直接读 task/config/config_trend_range.json 断言
smart_reduce 块内容，但策略配置已迁 MySQL kv_store（本地 JSON 只是兜底快照，
内容随版本演进），拿可变的外部数据当断言依据必然假失败。
现在改成验证代码层保证的缺省行为。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crypto.task.trend_range_trader import TrendRangeTrader, BUCKET_RANGE, BUCKET_TREND
from crypto.task.utils.trade_journal import classify_reason

_passed = []


def ok(name, cond, detail=''):
    if not cond:
        print(f"[FAIL] {name} {detail}")
        sys.exit(1)
    _passed.append(name)
    print(f"[ OK ] {name} {detail}")


class FakeSpecCache:
    """可控规格缓存：GPS ctVal=10 lotSz=1 minSz=1；NEAR ctVal=10 lotSz=0.1 minSz=0.1

    方法集与 utils/instrument_spec 保持一致（get_spec / steps / quantize /
    usd_to_contracts）——生产代码里 _steps()/_q() 走的是 steps/quantize，桩缺这
    两个方法时会被 except 吞成兜底 (0.1, 0.1)，GPS 的“减 0.5 张不足 minSz=1”
    会被当成可下单，测试反而测不到真实分支。
    """
    SPECS = {
        'GPS-USDT-SWAP': {'ct_val': 10.0, 'lot_sz': 1.0, 'min_sz': 1.0,
                          'max_lever': 20.0},
        'NEAR-USDT-SWAP': {'ct_val': 10.0, 'lot_sz': 0.1, 'min_sz': 0.1,
                           'max_lever': 75.0},
    }

    def get_spec(self, inst_id, refresh=False):
        return self.SPECS.get(inst_id)

    def steps(self, inst_id):
        spec = self.get_spec(inst_id)
        if not spec:
            return 0.1, 0.1
        lot = float(spec['lot_sz']) or 0.1
        min_sz = float(spec['min_sz']) or lot
        return lot, min_sz

    def quantize(self, inst_id, amount, enforce_min_sz=True):
        import math
        amount = float(amount or 0)
        if amount <= 0:
            return 0.0
        lot, min_sz = self.steps(inst_id)
        out = round(math.floor(amount / lot + 1e-9) * lot, 4)
        if enforce_min_sz and out + 1e-12 < min_sz:
            return 0.0
        return out

    def usd_to_contracts(self, inst_id, usd, price, leverage=1, enforce_min_sz=True):
        import math
        spec = self.get_spec(inst_id)
        if not spec or usd <= 0 or price <= 0:
            return 0.0
        lev = float(leverage or 0) or 1.0
        lots = math.floor(usd * lev / (spec['ct_val'] * price) / spec['lot_sz'] + 1e-9)
        c = round(lots * spec['lot_sz'], 4)
        if enforce_min_sz and c + 1e-12 < spec['min_sz']:
            return 0.0
        return c


class FakeTrader(TrendRangeTrader):
    """绕过 __init__ 的重型初始化，只注入测试所需依赖"""

    def __init__(self, smart_cfg=None, realized_pnl=-10.0, positions_ok=True,
                 held=30.0, avg_px=0.012):
        self.spec_cache = FakeSpecCache()
        self._cfg = {'global_settings': {'smart_reduce': smart_cfg or {}}}
        self.calls = []
        self._realized_pnl = realized_pnl
        self._positions_ok = positions_ok
        self._held = held
        self._avg_px = avg_px
        self._residuals_mem = {}

        class _PosMgr:
            def __init__(self, outer):
                self.o = outer

            def get_position(self, inst_id, bucket, direction):
                return self.o._held, self.o._avg_px

        class _Executor:
            def __init__(self, outer):
                self.o = outer

            def try_get_positions_by_mode(self, inst_id):
                if not self.o._positions_ok:
                    return None
                key = 'cross'
                return {key: [{'realizedPnl': str(self.o._realized_pnl)}],
                        'isolated': [{'realizedPnl': str(self.o._realized_pnl)}]}

        self.pos_mgr = _PosMgr(self)
        self.trade_executor = _Executor(self)

    def _load_config(self):
        return self._cfg

    def _close_bucket(self, inst_id, bucket, direction, reason='', run_id='',
                      short_period='', long_period='', price=0.0,
                      amount=None, cancel_orders=True):
        self.calls.append({'amount': amount, 'reason': reason})
        return True

    # 残留记录改为内存态，避免写真实 DB
    def _load_residuals(self):
        return dict(self._residuals_mem)

    def _save_residuals(self, residuals):
        self._residuals_mem = dict(residuals)


def main():
    base = dict(inst_id='GPS-USDT-SWAP', bucket=BUCKET_RANGE, direction='long',
                reason='长周期反转(long→short)', run_id='R1',
                short_period='5m', long_period='4H', price=0.012, leverage=10)

    # 1. 配置块缺失＝全局强制减仓默认启用（不读外部配置文件，只验代码缺省）
    #    GPS held=200，1U×10x÷0.12U/张 → keep=83 张 → 只减 117 张，绝不全平
    t = FakeTrader(smart_cfg=None, held=200.0)
    ok('H1a 配置块缺失也全局减仓',
       t._close_bucket_smart(scene='reversal', **base) is True
       and t.calls[-1]['amount'] == 117.0, f"calls={t.calls}")
    #    未配 scenes 时默认白名单覆盖全部平仓触发场景
    t = FakeTrader(smart_cfg={}, held=200.0)
    for sc in ('reversal', 'night_force', 'reverse_guard',
               'stop_loss', 'take_profit', 'range_risk'):
        ok(f'H1b 默认白名单含 {sc}',
           t._close_bucket_smart(scene=sc, **base) is True
           and t.calls[-1]['amount'] == 117.0, f"calls={t.calls}")

    # 2. classify_reason 优先级
    ok('H2a 智能减仓分类',
       classify_reason('长周期反转(long→short)·智能减仓') == 'smart_reduce')
    ok('H2b 反转分类不受影响', classify_reason('长周期反转(long→short)') == 'reverse')
    ok('H2c 睡眠强平+智能减仓',
       classify_reason('长周期反转(long→short)睡眠时段强平·智能减仓') == 'smart_reduce')
    ok('H2d 止盈分类不受影响', classify_reason('六类止盈') == 'tp')

    # 3. _calc_keep_contracts
    t = FakeTrader()
    # GPS: 0.5U×10x ÷ (10×0.012=0.12U/张) = 41.67 → floor 41 张（≥minSz=1）
    keep, min_sz = t._calc_keep_contracts('GPS-USDT-SWAP', 0.012, 10, 0.5)
    ok('H3a 常规折算', keep == 41.0 and min_sz == 1.0, f'keep={keep}')
    # NEAR: 0.5U×10x ÷ (10×1.98=19.8U/张) = 0.25 → < minSz 0.1? 0.25>0.1 → 0.2(取整到0.1)
    keep, min_sz = t._calc_keep_contracts('NEAR-USDT-SWAP', 1.98, 10, 0.5)
    ok('H3b 小数张数步长', keep == 0.2 and min_sz == 0.1, f'keep={keep}')
    # NEAR 高价: 0.5U×1x ÷ 198U/张 = 0.0025 → < minSz → 按 minSz=0.1 保留
    keep, _ = t._calc_keep_contracts('NEAR-USDT-SWAP', 19.8, 1, 0.5)
    ok('H3c minSz兜底', keep == 0.1, f'keep={keep}')
    # 杠杆 clamp：配置100x > GPS max_lever 20x → 按 20x 折算
    keep, _ = t._calc_keep_contracts('GPS-USDT-SWAP', 0.012, 100, 0.5)
    ok('H3d 杠杆clamp', keep == 83.0, f'keep={keep}')  # 0.5×20÷0.12=83.33→83
    ok('H3e 规格缺失', t._calc_keep_contracts('XXX-USDT-SWAP', 1.0, 10, 0.5) is None)
    ok('H3f 价格缺失', t._calc_keep_contracts('GPS-USDT-SWAP', 0, 10, 0.5) is None)

    # 4. _close_bucket_smart 分支（base 已在第 1 项定义）

    # 4a 显式关闭（紧急闸）→ 原逻辑全平（amount=None）
    t = FakeTrader(smart_cfg={'enabled': False})
    ok('H4a 显式关闭降级全平',
       t._close_bucket_smart(scene='reversal', **base) is True
       and t.calls[-1]['amount'] is None)

    # 4b 白名单显式收窄 → 名单外原逻辑全平
    t = FakeTrader(smart_cfg={'enabled': True, 'scenes': ['reversal']})
    ok('H4b 白名单外降级全平',
       t._close_bucket_smart(scene='night_force', **base) is True
       and t.calls[-1]['amount'] is None)

    # 4c realizedPnl 查询失败 → 不再降级全平，照常减仓（盈亏仅作快照上下文）
    t = FakeTrader(smart_cfg={'enabled': True, 'scenes': ['reversal'],
                              'target_margin_usd': 0.5},
                   positions_ok=False, held=200.0)
    r = t._close_bucket_smart(scene='reversal', **base)
    ok('H4c 盈亏查询失败仍减仓',
       r is True and t.calls[-1]['amount'] == 159.0
       and '智能减仓' in t.calls[-1]['reason'], f"calls={t.calls}")
    rec = t._residuals_mem.get('GPS-USDT-SWAP|range|long')
    ok('H4c2 查询失败盈亏记为0', rec and rec['realized_pnl_at_reduce'] == 0.0)

    # 4d realizedPnl ≥ 0（盈利）→ 同样不全平，只减仓（全局强制核心变更）
    t = FakeTrader(smart_cfg={'enabled': True, 'scenes': ['reversal'],
                              'target_margin_usd': 0.5},
                   realized_pnl=+1.5, held=100.0)
    r = t._close_bucket_smart(scene='reversal', **base)
    ok('H4d 盈利也强制减仓',
       r is True and t.calls[-1]['amount'] == 59.0
       and '智能减仓' in t.calls[-1]['reason'])
    rec = t._residuals_mem.get('GPS-USDT-SWAP|range|long')
    ok('H4d2 盈利减仓残留登记盈亏上下文',
       rec and rec['keep_amount'] == 41 and rec['reduce_amount'] == 59
       and rec['realized_pnl_at_reduce'] == 1.5 and rec['scene'] == 'reversal')

    # 4e 持仓低于保留目标（默认1U → keep=83 > held=30）→ 跳过达标不下单
    t = FakeTrader(smart_cfg={'enabled': True, 'scenes': ['reversal']},
                   realized_pnl=-5.0, held=30.0)
    ok('H4e 持仓低于保留目标跳过',
       t._close_bucket_smart(scene='reversal', **base) is True
       and len(t.calls) == 0)

    # 4f 亏损 → 正常减仓：held=100 → keep=41 → 减59张，reason 带智能减仓
    t = FakeTrader(smart_cfg={'enabled': True, 'scenes': ['reversal'],
                              'target_margin_usd': 0.5},
                   realized_pnl=-5.0, held=100.0)
    r = t._close_bucket_smart(scene='reversal', **base)
    last = t.calls[-1]
    ok('H4f 亏损减仓定量',
       r is True and last['amount'] == 59.0 and '智能减仓' in last['reason'],
       f"amount={last['amount']}")
    rec = t._residuals_mem.get('GPS-USDT-SWAP|range|long')
    ok('H4g 残留记录登记',
       rec and rec['keep_amount'] == 41 and rec['reduce_amount'] == 59
       and rec['realized_pnl_at_reduce'] == -5.0 and rec['scene'] == 'reversal')

    # 4h 减仓量不足 minSz → 跳过视为达标：held=41.5 keep=41 → 减0.5 < minSz=1
    #    （靠桩的 steps/quantize 真实报出 GPS lotSz=1，走到 _q() 抹平为 0 的分支）
    t = FakeTrader(smart_cfg={'enabled': True, 'scenes': ['reversal'],
                              'target_margin_usd': 0.5},
                   realized_pnl=-5.0, held=41.5)
    ok('H4h 减仓量不足minSz跳过',
       t._close_bucket_smart(scene='reversal', **base) is True
       and len(t.calls) == 0, f"calls={t.calls}")

    # 4i 规格缺失 → 跳过减仓返回 False（下轮重试，绝不被动全平）
    t = FakeTrader(smart_cfg={'enabled': True, 'scenes': ['reversal']},
                   realized_pnl=-5.0)
    r = t._close_bucket_smart(
        inst_id='XXX-USDT-SWAP', bucket=BUCKET_RANGE, direction='long',
        reason='反转', run_id='R1', price=1.0, leverage=10, scene='reversal')
    ok('H4i 规格缺失跳过不关仓', r is False and len(t.calls) == 0)

    # 4j 持仓为空 → 返回 False
    t = FakeTrader(smart_cfg={'enabled': True, 'scenes': ['reversal']},
                   realized_pnl=-5.0, held=0.0)
    ok('H4j 空仓返回False',
       t._close_bucket_smart(scene='reversal', **base) is False)

    # 4k 分批止盈定量请求被「持仓−保留量」封顶（末档 close_ratio=1.0 也不清零）
    #    held=200，target=0.5 → keep=41，cap=159：请求200被封顶为159；请求50原样执行
    t = FakeTrader(smart_cfg={'enabled': True, 'scenes': ['take_profit'],
                              'target_margin_usd': 0.5}, held=200.0)
    ok('H4k1 末档全量请求被封顶',
       t._close_bucket_smart(scene='take_profit', amount=200.0,
                             cancel_orders=False, **base) is True
       and t.calls[-1]['amount'] == 159.0, f"calls={t.calls}")
    ok('H4k2 分批中段定量原样执行',
       t._close_bucket_smart(scene='take_profit', amount=50.0,
                             cancel_orders=False, **base) is True
       and t.calls[-1]['amount'] == 50.0, f"calls={t.calls}")

    # 5. 残留记录删除
    t = FakeTrader()
    t._residuals_mem = {
        'GPS-USDT-SWAP|range|long': {}, 'GPS-USDT-SWAP|trend|long': {},
        'NEAR-USDT-SWAP|range|short': {}}
    t._remove_residual('GPS-USDT-SWAP', BUCKET_RANGE, 'long')
    ok('H5a 单条删除', len(t._residuals_mem) == 2
       and 'GPS-USDT-SWAP|range|long' not in t._residuals_mem)
    t._remove_residual('GPS-USDT-SWAP')
    ok('H5b 整币删除', list(t._residuals_mem.keys()) == ['NEAR-USDT-SWAP|range|short'])

    # 6. _smart_keep_qty（止损/止盈/区间风险退出的“已达保留目标”识别）
    t = FakeTrader(smart_cfg=None)
    ok('H6a 缺省启用返回保留量',
       t._smart_keep_qty('GPS-USDT-SWAP', 0.012, 10) == 83.0)
    t = FakeTrader(smart_cfg={'enabled': False})
    ok('H6b 关闭返回0不做达标判定', t._smart_keep_qty('GPS-USDT-SWAP', 0.012, 10) == 0.0)
    t = FakeTrader(smart_cfg=None)
    ok('H6c 规格缺失返回0', t._smart_keep_qty('XXX-USDT-SWAP', 1.0, 10) == 0.0)

    print(f"\n全部通过：{len(_passed)} 项")


if __name__ == '__main__':
    main()
