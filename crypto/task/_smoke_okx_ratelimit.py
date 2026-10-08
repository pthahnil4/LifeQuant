#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：只读接口限频 / 退避（问题#8）
======================================
安全等级：🔒 纯离线 —— 全部用假函数顶掉真实 SDK 调用，不连 DB、不调 OKX、不发信。

历史坑：全项目对 OKX 的调用是「想到就调」，既没节流也没对 50011 的退避。实盘
调度是常驻多线程进程（交易主循环 + 告警 + 行情扫描 + 前端轮询）共用一个 API key，
一旦某个接口打出 50011，客户端按「查询失败」处理 → 下一轮更勤地查，形成自我
放大；最坏情况是持仓查不到被当成无持仓。

修复口径：
- 新增 utils/okx_ratelimit.py：按接口分组令牌桶（先等令牌，等不到才抛
  RateLimited）+ 命中限频/网络抖动时指数退避重试；
- 只包**只读**接口；下单/撤单/改单/设杠杆一律不自动重试（结果未知只能靠
  clOrdId 反查定性，见 _smoke_cl_ord_id.py）。

场景清单：
1. 总开关 OFF → 完全直通（不节流、不重试，行为与改前一致）
2. 正常成功 → 响应原样透传，计数 granted 累加
3. 令牌桶本身：容量耗尽即拒绝，等待后可取到
4. 回 50011 两次后成功 → 共调用 3 次（退避重试生效）
5. 持续 50011 → 重试耗尽后仍把原始响应交回调用方（不抛异常）
6. 传输层异常（httpx 类）→ 退避后成功
7. 非网络异常（代码 bug）→ 立即抛出，一次都不重试
8. retry_on_error=False → 异常不重试，但 50011 仍然退避（供已有重试循环的调用方用）
9. 等不到令牌 → 抛 RateLimited 且带分组名
10. 环境变量覆盖速率/容量；非法值回落默认且不抛
11. 判定函数：哪些响应/异常算限频
12. 接线核对：6 个文件的只读调用全部走限频出口，且下单类调用一个都没被包进去
13. 与 #4 交叉回归：gen_cl_ord_id 打标结构没被限频改动破坏

何时重跑：改 utils/okx_ratelimit.py，或改 trade_executor / api_routes /
market_scanner / star_market / batch_trend_updater / instrument_spec 里 OKX 接口
调用方式（尤其新增只读接口或给下单加包层时）。
"""
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    sys.path.insert(0, _p)
sys.path.insert(0, os.path.join(_HERE, 'utils'))

from utils import okx_ratelimit as rl  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


class Counter:
    """假 SDK 方法：按预设脚本依次吐出响应或异常，并累计调用次数"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def scenario(**env):
    """设置速率类环境变量并清空桶/计数（桶在首次使用时才读环境变量）"""
    for k in ('OKX_RL_MARKET_TICKER', 'OKX_RL_BALANCE', 'OKX_RL_POSITIONS',
              'OKX_RL_LEVERAGE', 'OKX_RL_ORDER_QUERY', 'OKX_RL_ALGO_QUERY',
              'OKX_RL_INSTRUMENTS', 'OKX_RL_MARKET_CANDLES', 'OKX_RL_ODD'):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = str(v)
    os.environ['OKX_RATELIMIT_ENABLED'] = '1'
    rl.reset_state()


class _FakeHttpxTimeout(Exception):
    """伪 httpx 异常（下面把 __module__ 改写为 httpx.*）"""


_FakeHttpxTimeout.__module__ = 'httpx._exceptions'


def httpx_like(msg='ReadTimeout'):
    """构造一个“看起来像 httpx 传输层异常”的实例（不真的依赖 httpx）。

    okx_ratelimit 是按 `type(exc).__module__` 前缀判传输层异常的，
    所以这里把伪类的 __module__ 改写成 httpx.* 才能命中判定。
    """
    return _FakeHttpxTimeout(msg)


print('\n=== 1. 总开关 OFF → 完全直通 ===')
scenario()
os.environ['OKX_RATELIMIT_ENABLED'] = '0'
c = Counter([{'code': '50011', 'msg': 'Too Many Requests'}])
r = rl.limited('market_ticker', c, instId='BTC-USDT-SWAP')
check('1a 直通时不重试（50011 也只调 1 次）', c.calls == 1, f"calls={c.calls}")
check('1b 响应原样返回', r.get('code') == '50011')
c2 = Counter([Exception('boom')])
try:
    rl.limited('market_ticker', c2)
    check('1c 直通时异常照抛', False)
except Exception as e:
    check('1c 直通时异常照抛', c2.calls == 1 and 'boom' in str(e), f"calls={c2.calls}")

print('\n=== 2. 正常成功 → 原样透传 + 计数 ===')
scenario(OKX_RL_MARKET_TICKER='50:50')
ok = {'code': '0', 'data': [{'last': '12345'}]}
c = Counter([ok])
r = rl.limited('market_ticker', c, instId='BTC-USDT-SWAP')
check('2a 调用 1 次', c.calls == 1)
check('2b 响应完全透传', r is ok)
check('2c granted 计数 +1', rl.get_stats()['market_ticker']['granted'] == 1,
      str(rl.get_stats()))

print('\n=== 3. 令牌桶本体：容量耗尽即拒绝 ===')
b = rl._Bucket(rate=2, capacity=1)
got1, _ = b.acquire(0)
got2, _ = b.acquire(0)
check('3a 容量 1 时第二次立刻取不到', got1 and not got2)
b2 = rl._Bucket(rate=20, capacity=1)
b2.acquire(0)
t0 = time.monotonic()
got3, waited = b2.acquire(1.0)
cost = time.monotonic() - t0
check('3b 等待补桶后可以取到', got3, f"waited={waited:.3f}s")
check('3c 等待确实耗时（真的在限速）', cost >= 0.02, f"cost={cost:.3f}s")

print('\n=== 4. 50011 退避后成功 ===')
scenario(OKX_RL_MARKET_TICKER='50:50')
c = Counter([{'code': '50011', 'msg': 'Too Many Requests'},
             {'code': '50011'},
             {'code': '0', 'data': []}])
r = rl.limited('market_ticker', c, base_delay=0.01)
check('4a 共尝试 3 次（1 + 2 次退避）', c.calls == 3, f"calls={c.calls}")
check('4b 最终返回成功响应', r.get('code') == '0')
check('4c throttled 计数 = 2', rl.get_stats()['market_ticker']['throttled'] == 2,
      str(rl.get_stats()['market_ticker']))

print('\n=== 5. 持续 50011 → 耗尽后交回调用方，不抛异常 ===')
scenario(OKX_RL_BALANCE='50:50')
c = Counter([{'code': '50011', 'msg': 'Too Many Requests'}])
r = rl.limited('balance', c, base_delay=0.01, retries=2)
check('5a 只试 1+2 次（不无限重试）', c.calls == 3, f"calls={c.calls}")
check('5b 原始限频响应交回上层自行降级', r.get('code') == '50011')

print('\n=== 6. 传输层异常 → 退避后成功 ===')
scenario(OKX_RL_POSITIONS='50:50')
c = Counter([httpx_like('read timed out'), {'code': '0', 'data': []}])
r = rl.limited('positions', c, base_delay=0.01)
check('6a 网络抖动被退避重试', c.calls == 2, f"calls={c.calls}")
check('6b 返回恢复后的响应', r.get('code') == '0')

print('\n=== 7. 非网络异常（代码 bug）→ 立即抛，一次都不重试 ===')
scenario(OKX_RL_ORDER_QUERY='50:50')
c = Counter([ValueError('本地参数拼错了')])
try:
    rl.limited('order_query', c, base_delay=0.01)
    check('7a 异常向上抛出', False)
except ValueError as e:
    check('7a 异常向上抛出', '本地参数拼错了' in str(e))
check('7b 不重试（bug 不该被退避掩盖）', c.calls == 1, f"calls={c.calls}")

print('\n=== 8. retry_on_error=False：已有重试循环的调用方专用 ===')
scenario(OKX_RL_ALGO_QUERY='50:50')
c = Counter([httpx_like('connect timeout'), {'code': '0'}])
try:
    rl.limited('algo_query', c, retry_on_error=False, base_delay=0.01)
    check('8a 传输层异常直接上抛（交给外层重试）', False)
except Exception:
    check('8a 传输层异常直接上抛（交给外层重试）', c.calls == 1, f"calls={c.calls}")
c2 = Counter([{'code': '50011'}, {'code': '0'}])
r = rl.limited('algo_query', c2, retry_on_error=False, base_delay=0.01)
check('8b 限频响应仍然退避重试', c2.calls == 2 and r.get('code') == '0',
      f"calls={c2.calls}")

print('\n=== 9. 等不到令牌 → RateLimited ===')
scenario(OKX_RL_INSTRUMENTS='0.2:1')
c = Counter([{'code': '0', 'data': []}])
r = rl.limited('instruments', c, base_delay=0.01)
check('9a 首个请求正常放行', c.calls == 1 and r.get('code') == '0')
try:
    rl.limited('instruments', c, acquire_timeout=0.1, base_delay=0.01)
    check('9b 补桶不及且超过等待上限时抛 RateLimited', False)
except rl.RateLimited as e:
    check('9b 补桶不及且超过等待上限时抛 RateLimited',
          e.group == 'instruments' and c.calls == 1, f"group={e.group}")
check('9c refused 计数 +1', rl.get_stats()['instruments']['refused'] == 1,
      str(rl.get_stats()['instruments']))

print('\n=== 10. 环境变量覆盖速率/容量 ===')
scenario(OKX_RL_LEVERAGE='7:14')
bucket, known = rl._bucket_for('leverage')
check('10a 速率被覆盖', known and abs(bucket.rate - 7.0) < 1e-9, f"rate={bucket.rate}")
check('10b 容量被覆盖', abs(bucket.capacity - 14.0) < 1e-9, f"cap={bucket.capacity}")
scenario(OKX_RL_LEVERAGE='不是数字')
bucket2, _ = rl._bucket_for('leverage')
check('10c 非法值回落默认且不抛异常', abs(bucket2.rate - rl.DEFAULT_RATES['leverage']) < 1e-9,
      f"rate={bucket2.rate}")
check('10d 未登记分组按保守 2/s', rl._bucket_for('odd')[1] is False
      and abs(rl._bucket_for('odd')[0].rate - 2.0) < 1e-9)

print('\n=== 11. 限频判定口径 ===')
check('11a code=50011 算限频', rl.is_throttle_response({'code': '50011'}))
check('11b 报文含 Too Many Requests 算限频',
      rl.is_throttle_response({'code': '1', 'msg': 'Too Many Requests'}))
check('11c 正常响应不算限频', not rl.is_throttle_response({'code': '0', 'data': []}))
check('11d 业务失败（非限频）不算限频',
      not rl.is_throttle_response({'code': '51008', 'msg': 'Available insufficient'}))
check('11e httpx 类异常判为可重试', rl.is_retryable_error(httpx_like()))
check('11f 超时措辞判为可重试', rl.is_retryable_error(Exception('Connection reset by peer')))
check('11g 本地逻辑错误不判为可重试', not rl.is_retryable_error(KeyError('instId')))

print('\n=== 12. 接线核对（静态扫描，不导入被测模块）===')
_ROOT = os.path.dirname(os.path.dirname(_HERE))          # 项目根 cryptoTrade
_TARGETS = {
    'crypto/task/utils/trade_executor.py': 'self._rl(',
    'crypto/task/utils/instrument_spec.py': '_rl(',
    'crypto/task/utils/okx_ratelimit.py': None,
    'crypto/market_scanner.py': '_rl(',
    'crypto/star_market.py': '_rl(',
    'crypto/batch_trend_updater.py': '_rl(',
    'crypto/api_routes.py': '_rl_call(',
    # 以下是 2026-09-10 补的清单盲区：当时只扫了 7 个文件就报了 41/41 全绿，
    # 而 alert_monitor / app.py 完全没接限频——“扫描清单不全”会让静态核对
    # 给出假的完备感，新增调用 OKX 的模块时必须往里加。
    'crypto/task/monitor/alert_monitor.py': '_rl(',
    'crypto/app.py': '_rl_read(',
    'crypto/plan_routes.py': '_rl_call(',
}
# 已登记的例外：(文件, 方法名) —— 不算“裸调用”，但必须在注释里写清理由。
_RAW_READ_ALLOW = {
    # plan_routes 走 api_routes._rl_call（传对象+方法名，不是直调），
    # 这里只剩“限频模块导入失败才走”的 else 直连兜底分支。
    # 代价：同文件同方法的真正裸调用也会被放过，新增其他接口仍会被卡。
    ('crypto/plan_routes.py', 'get_account_balance'): '已包好的 fallback 分支',
}
_CLIENT = (r'\b(?:self\.)?(?:market_api|trade_api|account_api|public_api|'
           r'funding_api|marketDataAPI|local_api|_mkt_api|_acct_api|'
           # 链式直调也要抓到：旧版只认变量名，正因如此没发现
           # `MarketData.MarketAPI(flag=flag).get_ticker(...)` 与
           # `get_account_api(account).get_positions(...)` 这两种裸调用。
           r'get_\w*api\([^()]*\)|'
           r'(?:\w+\.)?(?:MarketAPI|AccountAPI|TradeAPI|PublicAPI|FundingAPI)\([^()]*\))')
_RAW_READ = re.compile(_CLIENT + r'\.(get_\w+)\(')
_RAW_WRITE = re.compile(_CLIENT +
                        r'\.(place_order|place_algo_order|cancel_order|cancel_algo_order|'
                        r'amend_order|set_leverage|close_positions)\('
                        # 传引用形式（写接口统一走 `_write(...)` 出口）：旧版只认
                        # `api.place_order(` 直调，重构后一个都扫不到，12b 会
                        # 变成假阴性（“根本没扫到”被当成“扫描有效”）
                        + r'|_write\(\s*(?:self\.)?' + _CLIENT + r'\.(place_order|'
                        r'place_algo_order|cancel_order|cancel_algo_order|'
                        r'amend_order|set_leverage|close_positions)')
_WRITE_IN_RL = re.compile(r"_rl(?:_call)?\(\s*(?:'[\w_]+',\s*)?(?:self\.)?\w*\.?"
                          r"(place_order|place_algo_order|cancel_order|cancel_algo_order|"
                          r"amend_order|set_leverage|close_positions)")

unwired, written_ok, bad_wrapped = [], 0, []
for rel, wrap in _TARGETS.items():
    if wrap is None:
        continue
    path = os.path.join(_ROOT, rel.replace('/', os.sep))
    src = open(path, encoding='utf-8').read()
    for m in _RAW_READ.finditer(src):
        if (rel, m.group(1)) in _RAW_READ_ALLOW:
            continue
        unwired.append(f"{rel}:{m.group(1)}")
    for m in _RAW_WRITE.finditer(src):
        written_ok += 1
    for m in _WRITE_IN_RL.finditer(src):
        bad_wrapped.append(f"{rel}:{m.group(1)}")

check('12a 只读接口调用全部已接限频出口（无裸调用残留）',
      not unwired, '; '.join(unwired[:6]))
check('12b 确实扫到了下单类调用（说明扫描有效）', written_ok >= 10, f"count={written_ok}")
check('12c 下单/撤单/设杠杆没有一个被包进限频重试', not bad_wrapped,
      '; '.join(bad_wrapped[:6]))

# trade_executor 的 _rl 必须真的拿到了限频实现（导入失败会静默直通，等于没修）
from utils import trade_executor as te  # noqa: E402
check('12d trade_executor 成功导入限频模块（不是静默直通）',
      te._rl_limited is not None, f"_rl_limited={te._rl_limited}")
check('12e TradeExecutor._rl 存在且默认走限频', hasattr(te.TradeExecutor, '_rl'))

print('\n=== 13. 幂等打标仍然生效（与 #4 交叉回归）===')
scenario(OKX_RL_ORDER_QUERY='50:50')
cid = te.gen_cl_ord_id('ct', seed='BTC-USDT-SWAP|buy|1')
# 口径＝OKX 实测（纯字母数字 1~32），不是照抄 Binance 的 [A-Za-z0-9_-]{1,64}；
# 详见 _smoke_cl_ord_id.py 第 1 组与 _diag_cl_ord_id_charset.py（2026-09-11 51000 事故）
check('13a 客户号仍满足 OKX 字符集/长度约束',
      re.fullmatch(r'[A-Za-z0-9]{1,32}', cid) is not None, cid)
check('13b 前缀与 seed 后缀结构未变',
      cid.startswith('ct') and re.fullmatch(r'ct[0-9a-f]{19,}[0-9a-f]{6}', cid) is not None, cid)

print(f"\n结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print('失败项：' + ', '.join(FAIL))
sys.exit(1 if FAIL else 0)
