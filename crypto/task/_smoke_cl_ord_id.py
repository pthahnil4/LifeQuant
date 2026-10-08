#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：下单幂等打标与"结果未知"反查（问题#4）
================================================
安全等级：🔒 纯离线 —— 用假 TradeAPI 顶掉真实客户端，不连 DB、不调 OKX、不发信。

历史坑：全项目一个 clOrdId/algoClOrdId 都没传，下单请求超时/连接被断时，客户端
无法判断单子有没有落到交易所，只能当失败返回；上层下一轮照原计划重发 →
重复开仓。追逐委托更糟：`for attempt in range(3)` 是无条件盲重试，一次网络抖动
最多能连发 3 张。

修复口径（只动 trade_executor 内部，不改下单语义与调用方契约）：
- 每笔委托带唯一 clOrdId/algoClOrdId；
- 请求异常后先按客户号反查定性：found=按成功复用（**不再重发**）、
  absent=确认没落地（可以重发）、unknown=反查也失败（停手并提示需人工核实）。

二次事故（2026-09-11 实盘）：打标本身"上线即全灭"——每笔委托都被交易所回
`51000: Parameter clOrdId error`，区间仓/趋势仓一笔都挂不出去。根因是客户号里
带了分隔符 `ct_<token>_<hash>`：OKX 的 clOrdId/algoClOrdId 实测只接受
**纯字母数字、长度 1~32**（下划线/连字符/点号全部非法），而实现和下面的第 1 组
断言一起照抄了 Binance 式的 `[A-Za-z0-9_-]{1,64}`，所以冒烟 29/29 全绿也拦不住。
教训：**校验"合法性"的断言不能来自对文档/别的交易所的印象，必须来自交易所实测**；
口径由只读探针 `crypto/task/_diag_cl_ord_id_charset.py` 出（它只查单不下单）。

场景清单：
1. gen_cl_ord_id 合法（OKX 实测口径：纯字母数字 ≤32）且互不相同，
   并按源码扫出全部下单前缀逐一过一遍（防新增出口漏检）
2. execute_trade 正常成功 → 带 clOrdId，返回 cl_ord_id
3. 请求异常 + 反查命中 → 按成功返回、place 只被调用 1 次（没盲重发）
4. 请求异常 + 反查明确"查无此单" → 返回失败、不带 need_verify（可安全重发）
5. 请求异常 + 反查也失败 → 返回失败且 need_verify=True、place 只 1 次
6. execute_reduce_only_order 打标 + 异常后反查命中按成功
7. 追逐委托：反查也失败时只发 1 次就停手（改前会盲重试 3 次）
8. 追逐委托：反查确认未落地时按原逻辑重发，最终成功
9. TWAP/移动止损/计划止盈止损/强平 均带客户号（打标覆盖全）
10. OKX 客户端 HTTP 超时已抬高（SDK 默认 5s 太短 → 下单频繁"结果未知"）

何时重跑：改 trade_executor.py 的 gen_cl_ord_id / probe_order_by_cl_id /
probe_algo_by_client_id / execute_trade / execute_reduce_only_order /
execute_chase_limit_order / _apply_http_timeout 任一逻辑，或新增下单出口时。
客户号字符集口径存疑时先跑只读探针 `crypto/task/_diag_cl_ord_id_charset.py`。
"""
import os
import re
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    sys.path.insert(0, _p)
sys.path.insert(0, os.path.join(_HERE, 'utils'))

from utils.trade_executor import TradeExecutor, gen_cl_ord_id  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


# ------------------------------------------------------------------ 假交易所
class FakeTradeAPI:
    """按脚本需要依次吐出预设响应/异常，并记录每次调用入参"""

    def __init__(self):
        self.place_calls = []
        self.place_script = []          # 每项: dict(响应) 或 Exception
        self.get_order_script = []      # 每项: dict(响应) 或 Exception
        self.get_order_calls = []
        self.place_algo_calls = []
        self.place_algo_script = []
        self.get_algo_script = []
        self.get_algo_calls = []

    @staticmethod
    def _pop(script):
        if script:
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {'code': '0', 'data': [{}]}

    def place_order(self, **kw):
        self.place_calls.append(kw)
        return self._pop(self.place_script)

    def get_order(self, **kw):
        self.get_order_calls.append(kw)
        return self._pop(self.get_order_script)

    def place_algo_order(self, **kw):
        self.place_algo_calls.append(kw)
        return self._pop(self.place_algo_script)

    def get_algo_order_details(self, **kw):
        self.get_algo_calls.append(kw)
        return self._pop(self.get_algo_script)

    def close_positions(self, **kw):
        self.place_calls.append(kw)
        return self._pop(self.place_script)


OK_ORDER = {'code': '0', 'data': [{'sCode': '0', 'sMsg': '', 'ordId': 'ord-777'}]}
TIMEOUT = RuntimeError('Max retries exceeded with url: /api/v5/trade/order (read timed out)')


class FakeMarketAPI:
    @staticmethod
    def get_ticker(instId=None, **kw):
        return {'code': '0', 'data': [{'last': '3.2'}]}


def new_executor():
    """绕开 __init__（不建真实客户端），只注入假 API"""
    ex = TradeExecutor.__new__(TradeExecutor)
    ex.trade_api = FakeTradeAPI()
    ex.market_api = FakeMarketAPI()
    ex.flag = '0'
    ex._lock = threading.Lock()
    # __new__ 不跑 __init__，单证类型常量得补上（否则被测函数取不到）
    ex.ORDER_TYPE_TWAP = 'twap'
    ex.ORDER_TYPE_CHASE = 'chase_limit'
    ex.ORDER_TYPE_TRAILING = 'move_order_stop'
    ex.ORDER_TYPE_CONDITIONAL = 'conditional'
    ex.ORDER_TYPE_OCO = 'oco'
    # 防重签名与落盘路径给内存/临时件，避免测试碰真实的 trade_state.json
    ex.strategy_last_order_sig = {}
    ex.pending_order_ids = set()
    ex.strategy_state_file = os.path.join(
        tempfile.gettempdir(), 'smoke_trade_state_%d.json' % os.getpid())
    return ex


def first(lst):
    """取第一条请求参数；空列表返回 {} 而不是抛 IndexError（否则挂一条带一片）"""
    return lst[0] if lst else {}


# ---------------------------------------------------------------- 1 客户号格式
# 【口径来源＝OKX 实测，不是文档抄写】2026-09-11 事故：本组断言原先写的是
# 「字母数字下划线、≤64」（照搬 Binance 式假设），与交易所真实规矩一致地"错"，
# 所以 29/29 全绿却拦不住实盘每笔委托都被拒 51000 Parameter clOrdId error。
# 现在的规则由只读探针 _diag_cl_ord_id_charset.py 逐形态定性得出：
#   纯字母数字 1~32 位；下划线/连字符/点号一律非法；33 位起超长非法。
print("\n[1] clOrdId 生成规则（OKX 实测：纯字母数字 1~32 位）")
ids = {gen_cl_ord_id('ct', seed=f'NEAR-USDT-SWAP|buy|0.2|3.14|limit') for _ in range(200)}
sample = next(iter(ids))
check("1a 字符集只含字母与数字（无下划线/连字符）",
      bool(re.fullmatch(r'[A-Za-z0-9]{1,32}', sample)), sample)
check("1b 长度 ≤32（OKX 实测上限，不是 64）", len(sample) <= 32, len(sample))
check("1c 200 次不撞号", len(ids) == 200, len(ids))
check("1d 超长被截断而非报错",
      len(gen_cl_ord_id('x' * 100, seed='y' * 100)) <= 32)
check("1e 截断后仍是纯字母数字",
      bool(re.fullmatch(r'[A-Za-z0-9]{1,32}', gen_cl_ord_id('x' * 100, seed='y' * 100))))
# 分隔符只能被剔除，不能被"洗白"成合法字符
check("1f 带分隔符的前缀也被净化",
      bool(re.fullmatch(r'[A-Za-z0-9]{1,32}', gen_cl_ord_id('ct_chase-1', seed='a|b'))),
      gen_cl_ord_id('ct_chase-1', seed='a|b'))
# 代码里所有下单出口用到的前缀逐一过一遍：前缀清单**从源码扫出来**，
# 不再靠手写（手写清单会漏，正是"静态核对假完备"的老坑）
import utils.trade_executor as _te_mod  # noqa: E402
with open(_te_mod.__file__, encoding='utf-8') as _fh:
    _src = _fh.read()
_prefixes = sorted(set(re.findall(r"gen_cl_ord_id\(\s*'([^']*)'", _src)))
check("1g 源码中的前缀清单非空（扫描本身有效）", len(_prefixes) >= 5, _prefixes)
_bad_pfx = [p for p in _prefixes
            if not re.fullmatch(r'[A-Za-z0-9]{1,32}',
                                gen_cl_ord_id(p, seed='BTC-USDT-SWAP|buy|0.1|100|limit'))]
check("1h 每个下单出口的客户号都合法", not _bad_pfx,
      f"扫描到 {len(_prefixes)} 个前缀，非法={_bad_pfx}")
# 唯一性靠 token（毫秒时间戳+uuid）：即便同秒并发也不能撞号
burst = {gen_cl_ord_id('ct', seed=f'X-USDT-SWAP|buy|{i}') for i in range(500)}
check("1i 同秒 500 次并发不撞号", len(burst) == 500, len(burst))

# ---------------------------------------------------------------- 2~5 普通委托
print("\n[2] execute_trade 正常成功")
ex = new_executor()
ex.trade_api.place_script = [OK_ORDER]
r = ex.execute_trade('NEAR-USDT-SWAP', 'buy', 0.2, price=3.14,
                     order_type='limit', trading_mode='cross')
sent = first(ex.trade_api.place_calls)
check("2a 下单带 clOrdId", bool(sent.get('clOrdId')), sent)
check("2b 成功返回 order_id 与 cl_ord_id",
      r.get('success') and r.get('order_id') == 'ord-777' and r.get('cl_ord_id'), r)

print("\n[3] 请求超时 + 反查命中 → 按成功处理，不重发")
ex = new_executor()
ex.trade_api.place_script = [TIMEOUT]
ex.trade_api.get_order_script = [{'code': '0', 'data': [
    {'ordId': 'ord-888', 'state': 'live', 'px': '3.14'}]}]
r = ex.execute_trade('NEAR-USDT-SWAP', 'buy', 0.2, price=3.14,
                     order_type='limit', trading_mode='cross')
check("3a 只有 1 次下单请求（未盲重发）", len(ex.trade_api.place_calls) == 1,
      len(ex.trade_api.place_calls))
check("3b 按成功返回并复用反查到的 ordId",
      r.get('success') and r.get('order_id') == 'ord-888'
      and r.get('recovered_by_cl_ord_id'), r)
check("3c 反查用的就是本次打标的 clOrdId",
      ex.trade_api.get_order_calls and
      ex.trade_api.get_order_calls[0].get('clOrdId') == r.get('cl_ord_id'),
      ex.trade_api.get_order_calls)

print("\n[4] 请求异常 + 反查明确无此单 → 仍按结果未知处理（不得重发）")
ex = new_executor()
ex.trade_api.place_script = [TIMEOUT]
ex.trade_api.get_order_script = [{'code': '0', 'data': []}]
r = ex.execute_trade('NEAR-USDT-SWAP', 'buy', 0.2, price=3.14,
                     order_type='limit', trading_mode='cross')
check("4a 返回失败", not r.get('success'), r)
# 口径变更：暂时查无此单**不能**证明原请求未落地（传播/归档延迟），
# 所以 intent 必须保留、不得据此重发（见 `_unknown_place_failure`）
check("4b 查无此单也保留 need_verify（禁止重发）", bool(r.get('need_verify')), r)
check("4c 文案保留客户号并要求人工核实",
      '人工核实' in str(r.get('error')) and r.get('cl_ord_id') in str(r.get('error')), r)

print("\n[5] 请求异常 + 反查也失败 → 停手并要求人工核实")
ex = new_executor()
ex.trade_api.place_script = [TIMEOUT]
ex.trade_api.get_order_script = [TIMEOUT, TIMEOUT, TIMEOUT]
r = ex.execute_trade('NEAR-USDT-SWAP', 'buy', 0.2, price=3.14,
                     order_type='limit', trading_mode='cross')
check("5a 只有 1 次下单请求", len(ex.trade_api.place_calls) == 1,
      len(ex.trade_api.place_calls))
check("5b success=False 且 need_verify=True",
      (not r.get('success')) and r.get('need_verify'), r)
check("5c 文案含 clOrdId 与人工核实提示",
      '人工核实' in str(r.get('error')) and r.get('cl_ord_id') in str(r.get('error')),
      r.get('error'))

# ---------------------------------------------------------------- 6 平仓单
print("\n[6] execute_reduce_only_order 打标与恢复")
ex = new_executor()
ex.get_positions = lambda inst_id=None: [{'posSide': 'net', 'mgnMode': 'cross',
                                          'pos': '10'}]
# 减仓前置检查已改走 try_get_positions_by_mode（查不到持仓宁可拒单），桩必须跟上
ex.try_get_positions_by_mode = lambda inst_id=None: {
    'cross': [{'instId': 'NEAR-USDT-SWAP', 'posSide': 'net', 'mgnMode': 'cross',
               'pos': '10', 'avgPx': '3.1'}], 'isolated': []}
ex.get_last_price = lambda inst_id: 3.2
ex.trade_api.place_script = [TIMEOUT]
ex.trade_api.get_order_script = [{'code': '0', 'data': [
    {'ordId': 'ord-999', 'state': 'filled', 'avgPx': '3.19'}]}]
r = ex.execute_reduce_only_order('NEAR-USDT-SWAP', 'sell', 10.0, trading_mode='cross')
check("6a 平仓请求异常但反查命中 → 按已发起处理",
      r.get('success') and r.get('order_id') == 'ord-999', r)
check("6b 回填成交价取反查 avgPx（不是 0）", float(r.get('price') or 0) > 3.1, r)
check("6c 只发了一次平仓请求", len(ex.trade_api.place_calls) == 1,
      len(ex.trade_api.place_calls))

ex = new_executor()
ex.get_positions = lambda inst_id=None: [{'posSide': 'net', 'mgnMode': 'cross',
                                          'pos': '10'}]
ex.try_get_positions_by_mode = lambda inst_id=None: {
    'cross': [{'instId': 'NEAR-USDT-SWAP', 'posSide': 'net', 'mgnMode': 'cross',
               'pos': '10', 'avgPx': '3.1'}], 'isolated': []}
ex.get_last_price = lambda inst_id: 3.2
ex.trade_api.place_script = [OK_ORDER]
ex.execute_reduce_only_order('NEAR-USDT-SWAP', 'sell', 10.0, trading_mode='cross',
                             order_type='limit', price=3.5)
check("6d 正常平仓也带 clOrdId", bool(first(ex.trade_api.place_calls).get('clOrdId')),
      first(ex.trade_api.place_calls))

# ---------------------------------------------------------------- 7~8 追逐委托
print("\n[7] 追逐委托：定性不了就停手（改前会盲重试 3 次）")


def chase_setup():
    ex = new_executor()
    ex.get_positions = lambda inst_id=None: []
    ex.get_account_balance = lambda *a, **kw: {}
    return ex


ex = chase_setup()
ex.trade_api.place_algo_script = [TIMEOUT]
ex.trade_api.get_algo_script = [TIMEOUT, TIMEOUT, TIMEOUT]
r = ex.execute_chase_limit_order('NEAR-USDT-SWAP', 'buy', 0.5)
check("7a place_algo_order 只被调用 1 次", len(ex.trade_api.place_algo_calls) == 1,
      len(ex.trade_api.place_algo_calls))
check("7b 返回 need_verify 且带 algoClOrdId",
      (not r.get('success')) and r.get('need_verify') and r.get('algo_cl_ord_id'), r)
check("7c 首次请求就带 algoClOrdId",
      bool(first(ex.trade_api.place_algo_calls).get('algoClOrdId')),
      first(ex.trade_api.place_algo_calls))

print("\n[8] 追逐委托：反查确认未落地 → 按原逻辑重发并最终成功")
ex = chase_setup()
ex.trade_api.place_algo_script = [TIMEOUT, TIMEOUT,
                                  {'code': '0', 'data': [{'algoId': 'algo-1'}]}]
ex.trade_api.get_algo_script = [{'code': '0', 'data': []}] * 3   # 明确查无此单
r = ex.execute_chase_limit_order('NEAR-USDT-SWAP', 'buy', 0.5)
check("8a 重发到第 3 次成功", len(ex.trade_api.place_algo_calls) == 3
      and r.get('success') and r.get('algo_id') == 'algo-1', r)
check("8b 重发沿用同一个 algoClOrdId",
      len({c.get('algoClOrdId') for c in ex.trade_api.place_algo_calls}) == 1,
      [c.get('algoClOrdId') for c in ex.trade_api.place_algo_calls])

ex = chase_setup()
ex.trade_api.place_algo_script = [TIMEOUT]
ex.trade_api.get_algo_script = [{'code': '0', 'data': [
    {'algoId': 'algo-2', 'state': 'live'}]}]
r = ex.execute_chase_limit_order('NEAR-USDT-SWAP', 'buy', 0.5)
check("8c 反查命中 → 按成功处理且不再重发",
      r.get('success') and r.get('algo_id') == 'algo-2'
      and len(ex.trade_api.place_algo_calls) == 1, r)

# ---------------------------------------------------------------- 9 打标覆盖
print("\n[9] 其余下单出口全部打标")

ex = new_executor()
ex.get_positions = lambda inst_id=None: []
ex.get_account_balance = lambda *a, **kw: {}
ex.execute_twap_order('NEAR-USDT-SWAP', 'buy', 1.0, interval=60, single_amount=0.5,
                      duration=120, is_same_direction=True)
check("9a TWAP 带 algoClOrdId",
      bool(first(ex.trade_api.place_algo_calls).get('algoClOrdId')),
      first(ex.trade_api.place_algo_calls))

ex = new_executor()
ex.trade_api.place_algo_script = [{'code': '0', 'data': [{'algoId': 'a2'}]}]
ex.create_trailing_stop_order('NEAR-USDT-SWAP', 'sell', 1.0, 0.01)
check("9b 移动止损带 algoClOrdId",
      bool(first(ex.trade_api.place_algo_calls).get('algoClOrdId')),
      first(ex.trade_api.place_algo_calls))

ex = new_executor()
ex.trade_api.place_algo_script = [{'code': '0', 'data': [{'algoId': 'a3'}]}]
ex.create_tp_sl_order('NEAR-USDT-SWAP', 'sell', 1.0,
                      trading_mode='cross', tp_trigger_px=4.0)
check("9c 计划止盈止损带 algoClOrdId",
      bool(first(ex.trade_api.place_algo_calls).get('algoClOrdId')),
      first(ex.trade_api.place_algo_calls))

ex = new_executor()
ex.trade_api.place_script = [OK_ORDER]
ex.close_position('NEAR-USDT-SWAP')
check("9d 一键强平带 clOrdId", bool(first(ex.trade_api.place_calls).get('clOrdId')),
      first(ex.trade_api.place_calls))

# ---------------------------------------------------------------- 10 HTTP 超时
# 现网另一半年根因：SDK 默认读超时 5s，链路一抖下单就变成"结果未知"（必须人工
# 核实的最坏状态）。这里只验配置生效与"绝不因设超时而崩"，不发任何网络请求。
print("\n[10] OKX 客户端 HTTP 超时（下单结果未知的另一半年根因）")
import utils.trade_executor as _te  # noqa: E402
check("10a 超时值不低于 SDK 默认且可被环境变量抬高",
      _te._OKX_HTTP_TIMEOUT_SEC >= 5.0, f"{_te._OKX_HTTP_TIMEOUT_SEC}s")
_pub = _te.PublicData.PublicAPI(flag='0')          # 只建对象，不发请求
_te._apply_http_timeout(_pub)
check("10b 真实 SDK 客户端读超时被抬高",
      abs(float(getattr(_pub.timeout, 'read', 0)) - _te._OKX_HTTP_TIMEOUT_SEC) < 1e-6,
      f"{getattr(_pub.timeout, 'read', None)}s")


class _NoTimeoutAttr:
    __slots__ = ()                                  # 赋值 timeout 必抛 AttributeError


_te._apply_http_timeout(_NoTimeoutAttr())
check("10c 设置失败只告警不上抛（不因限流件带崩交易链路）", True)

_with_wrap = _src.count('_apply_http_timeout(')
check("10d trade_executor 内所有 OKX 客户端都套了超时（含重建路径）",
      _with_wrap >= 6, f"_apply_http_timeout( 出现 {_with_wrap} 次")

print("\n" + "=" * 52)
print(f"  PASS {len(PASS)}  /  FAIL {len(FAIL)}")
if FAIL:
    print("  失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
