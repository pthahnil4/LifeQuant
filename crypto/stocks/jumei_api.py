#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 3 自动搬运（2026-10-08）。
# 原名: stocks/jumei_api.py  →  现位置: crypto/stocks/jumei_api.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底与根级 config 依赖已移除。
"""
聚美智数 · 股票行情 API 工具类（阿里云市场网关，APPCODE 鉴权）
================================================================
直连 REST 通道：``https://jmgphqcxhs.market.alicloudapi.com``
（与 MCP 网关 ``mcpservergateway...`` 是两条独立链路；实测 MCP 数据面偶发整体 502，
 本类走直连 REST，稳定快速。落地一律用直连。）

商品：cmapi00067199【聚美智数】股票行情查询  供应商：杭州安那其科技
接口档案：doc/jumei_stock_api/ （README / endpoints / response_fields / limitations / samples）

设计
----
- :class:`AppCodeGatewayClient` —— 通用「APPCODE + POST form」传输基类，
  含信封解析与 5xx/网络退避重试。**期货等其它聚美产品可直接继承复用**（换 host 即可）。
- :class:`JumeiStockAPI` —— 股票产品，覆盖全部 14 个接口。

凭据统一来自根级 ``config.py``（环境变量 / .env 优先：JUMEI_API_HOST / JUMEI_APPCODE /
JUMEI_TIMEOUT），本文件不写死密钥。

关键限制（实测 + 官方文档双重确认）
----------------------------------
- ``a_kline`` / ``hs_kline``：入参**没有日期区间、没有翻页**，硬顶 **100 根**
  （pageSize 加到 5000、塞 pageNo/offset/start/end/date 全部无效），恒为最近 100 根。
- ``hs_mink``（沪深分钟K含均线）：**不受 100 限制**，实测单次可足额返回 1000~1800 根
  （60 分钟 ≈ 近 22 个月历史），pageSize≥2000 触发业务 code=502。
  ⚠️ 高频大 pageSize 连打会触发 mink 专属限流：之后任意大小都返回空 candle，需冷却数分钟。
- 三者都只到「当前」，无历史翻页；更早的历史仍须自建定时采集入库。
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


try:
    from crypto.stocks import gateway_config as config
    _HOST = getattr(config, 'JUMEI_API_HOST', 'https://jmgphqcxhs.market.alicloudapi.com')
    _APPCODE = getattr(config, 'JUMEI_APPCODE', '')
    _TIMEOUT = getattr(config, 'JUMEI_TIMEOUT', 15)
except Exception:  # config 不可用时回退环境变量，保持模块可独立导入
    _HOST = os.environ.get('JUMEI_API_HOST', 'https://jmgphqcxhs.market.alicloudapi.com')
    _APPCODE = os.environ.get('JUMEI_APPCODE', '')
    _TIMEOUT = int(os.environ.get('JUMEI_TIMEOUT', 15) or 15)

# 业务成功码
_OK_CODES = (200, '200', 0, '0')


class JumeiAPIError(Exception):
    """聚美接口调用异常（网络 / HTTP / 业务码）。"""

    def __init__(self, message, code=None, raw=None):
        super().__init__(message)
        self.code = code      # HTTP 状态码 或 业务 code
        self.raw = raw


class AppCodeGatewayClient:
    """阿里云市场 APPCODE 鉴权网关的通用 POST 表单客户端（传输层基类）。

    子类只需给出 ``HOST``（不同产品换 host / path）即可复用 post/重试/信封解析。
    期货等产品线可直接继承本类。
    """

    HOST = _HOST
    TIMEOUT = _TIMEOUT

    def __init__(self, appcode=None, host=None, timeout=None):
        self.appcode = appcode or _APPCODE
        self.host = (host or self.HOST).rstrip('/')
        self.timeout = timeout or self.TIMEOUT
        if not self.appcode:
            raise JumeiAPIError('缺少 AppCode：请在 .env 设置 JUMEI_APPCODE')

    # ----------------------------------------------------------------
    def post(self, path, body=None, retries=2, backoff=1.5):
        """发起带 APPCODE 鉴权的 POST 表单请求，返回解析后的业务 dict。

        - 4xx（鉴权/参数/配额）不重试，直接抛错；
        - 5xx / 网络错误 / 超时：指数退避重试 ``retries`` 次；
        - 业务码非成功（``code`` ∉ {200,0}）抛 :class:`JumeiAPIError`。
        """
        url = self.host + path
        data = urllib.parse.urlencode(body or {}).encode('utf-8')
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Authorization': 'APPCODE ' + self.appcode,
        }
        last_err = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                raw = resp.read().decode('utf-8', errors='replace')
                return self._parse_envelope(raw)
            except urllib.error.HTTPError as e:
                rb = self._read_err(e)
                last_err = JumeiAPIError('HTTP %s %s' % (e.code, e.reason), code=e.code, raw=rb[:500])
                if 400 <= e.code < 500:
                    raise last_err
            except Exception as e:  # URLError / timeout 等
                last_err = JumeiAPIError('%s: %s' % (type(e).__name__, e))
            if attempt < retries:
                logger.warning('[jumei] %s 第%d次重试（%s）', path, attempt + 1, last_err)
                time.sleep(backoff * (attempt + 1))
        raise last_err

    @staticmethod
    def _read_err(http_error):
        try:
            return http_error.read().decode('utf-8', errors='replace')
        except Exception:
            return ''

    @staticmethod
    def _parse_envelope(raw):
        """解析统一信封 {code,msg,taskNo,charge?,data}。非成功码抛错。"""
        if not raw:
            raise JumeiAPIError('返回为空', raw=raw)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise JumeiAPIError('返回非 JSON', raw=raw[:500])
        if isinstance(obj, dict):
            code = obj.get('code')
            if code is not None and code not in _OK_CODES:
                raise JumeiAPIError('业务错误 code=%s: %s' % (code, obj.get('msg', '')),
                                    code=code, raw=json.dumps(obj, ensure_ascii=False)[:500])
        return obj


class JumeiStockAPI(AppCodeGatewayClient):
    """聚美智数股票行情客户端（14 接口）。方法名对应 path，返回值均为解析后的 dict。"""

    # 接口注册表：标识 → (path, 说明)。便于遍历 / 文档化 / 通用调用。
    ENDPOINTS = {
        'a_rank':        ('/stock/a/rank',        'A股排行'),
        'a_f10_company': ('/stock/a/f10-company', 'A股F10公司信息'),
        'a_price':       ('/stock/a/price',       'A股报价(多只逗号分隔)'),
        'hs_mink':       ('/stock/hs/mink',       '沪深分钟K含均线'),
        'hs_tick':       ('/stock/hs/tick',       '沪深分时成交'),
        'hs_info':       ('/stock/hs/info',       '沪深股票信息'),
        'hs_rank':       ('/stock/hs/rank',       '沪深股票排行'),
        'hs_blocklist':  ('/stock/hs/blocklist',  '沪深板块成分股排行'),
        'hs_blockrank':  ('/stock/hs/blockrank',  '沪深板块排行'),
        'hs_overview':   ('/stock/hs/overview',   '沪深大盘涨跌数'),
        'a_extra':       ('/stock/a/extra',       'A股信息拓展(多只逗号分隔)'),
        'a_stop':        ('/stock/a/stop',        'A股停牌信息'),
        'a_kline':       ('/stock/a/kline',       'A股K线(symbol/type，最近≤100根)'),
        'hs_kline':      ('/stock/hs/kline',      '沪深K线(code/period/fuquan，最近≤100根)'),
    }

    # ---- 排行 / 行情 / 信息类 ----------------------------------------
    def a_rank(self, market='hs_a', sort='changeRate', asc='0', pageNo=1, pageSize=10):
        """A股排行。market: hs_a/hs_b/hs_bjs/kcb/cyb/hs。pageSize 最大 100。"""
        return self.post(self.ENDPOINTS['a_rank'][0], {
            'market': market, 'sort': sort, 'asc': asc,
            'pageNo': str(pageNo), 'pageSize': str(pageSize)})

    def hs_rank(self, type='1', sortField='px_change_rate', orderBy='desc',
                pageNo=1, pageSize=10):
        """沪深股票排行。type: 1全市场/2主板/3上证A/4深证A/5创业板/6中小板/7科创板。"""
        return self.post(self.ENDPOINTS['hs_rank'][0], {
            'type': type, 'sortField': sortField, 'orderBy': orderBy,
            'pageNo': str(pageNo), 'pageSize': str(pageSize)})

    def a_price(self, symbol):
        """A股报价。symbol 可逗号分隔多只，如 'sh000001,sz000002'。"""
        return self.post(self.ENDPOINTS['a_price'][0], {'symbol': symbol})

    def hs_info(self, code):
        """沪深股票信息。code 如 '000001.SZ'。"""
        return self.post(self.ENDPOINTS['hs_info'][0], {'code': code})

    def a_f10_company(self, symbol):
        """A股F10公司信息。symbol 为纯数字，如 '000002'。"""
        return self.post(self.ENDPOINTS['a_f10_company'][0], {'symbol': symbol})

    def a_extra(self, symbol):
        """A股信息拓展。symbol 如 'sh600519'，可逗号分隔。"""
        return self.post(self.ENDPOINTS['a_extra'][0], {'symbol': symbol})

    def a_stop(self):
        """A股停牌信息（无入参）。"""
        return self.post(self.ENDPOINTS['a_stop'][0], {})

    def hs_overview(self):
        """沪深大盘涨跌数（无入参）。"""
        return self.post(self.ENDPOINTS['hs_overview'][0], {})

    def hs_tick(self, code):
        """沪深分时成交。code 如 '000001.SZ'。"""
        return self.post(self.ENDPOINTS['hs_tick'][0], {'code': code})

    # ---- 板块类 ------------------------------------------------------
    def hs_blockrank(self, type='2', pageNo=1, pageSize=10):
        """沪深板块排行。type: 1地域/2行业/3概念。"""
        return self.post(self.ENDPOINTS['hs_blockrank'][0], {
            'type': type, 'pageNo': str(pageNo), 'pageSize': str(pageSize)})

    def hs_blocklist(self, blockCode, pageNo=1, pageSize=10):
        """沪深板块成分股排行。blockCode 从板块排行接口获取。"""
        return self.post(self.ENDPOINTS['hs_blocklist'][0], {
            'blockCode': blockCode, 'pageNo': str(pageNo), 'pageSize': str(pageSize)})

    # ---- K线类（均只有最近 ≤100 根，无日期/翻页）--------------------
    def a_kline(self, symbol, ktype='60', pageSize=100, ma=''):
        """A股K线（/stock/a/kline）。ktype: 1/5/15/30/60/120 分钟,240 日,1200 周,7200 月,86400 年。

        symbol 如 'sz000001'。返回 data.list[]（dict：day/open/high/low/close/volume/amount）。
        """
        body = {'symbol': symbol, 'type': str(ktype), 'pageSize': str(pageSize)}
        if ma:
            body['ma'] = ma
        return self.post(self.ENDPOINTS['a_kline'][0], body)

    def hs_kline(self, code, period='60', fuquan='1', pageSize=100):
        """沪深K线（/stock/hs/kline）。period: 1/5/15/30/60 分钟,101 日,102 周,103 月,104 季,105 半年,106 年。

        fuquan: 0 不复权/1 前复权/2 后复权。code 如 '000001.SZ'。
        返回 data.candle[]（数组：[ts秒,open,close,high,low,volume,amount,change,changeRate]）。
        """
        return self.post(self.ENDPOINTS['hs_kline'][0], {
            'code': code, 'period': str(period), 'fuquan': str(fuquan),
            'pageSize': str(pageSize)})

    def hs_mink(self, code, period='60', pageSize=1000):
        """沪深分钟K含均线（/stock/hs/mink）。period: 1/5/15/30/60/120。

        ★ 与 a_kline/hs_kline 不同：本接口**不受 100 根限制**，实测单次可返回
        1000~1800 根（60分钟≈近22个月历史）；pageSize≥2000 触发业务 code=502。
        ⚠️ 高频大 pageSize 连打会触发本接口专属限流（之后任意大小返回空 candle），
        批量回补时务必控制频率、失败冷却重试。仍只有「最近 N 根」，无历史翻页。
        """
        return self.post(self.ENDPOINTS['hs_mink'][0], {
            'code': code, 'period': str(period), 'pageSize': str(pageSize)})


# ====================================================================
#  自测入口
# ====================================================================
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    api = JumeiStockAPI()
    print('=' * 64)
    print('  聚美智数 REST 自测  host=%s' % api.host)
    print('  已登记接口 %d 个' % len(api.ENDPOINTS))
    print('=' * 64)

    def brief(name, fn):
        try:
            r = fn()
            data = r.get('data', {}) if isinstance(r, dict) else {}
            n = len(data.get('list') or data.get('candle') or []) or (1 if data else 0)
            print('  ✅ %-28s code=%s 数据量≈%d' % (name, r.get('code'), n))
        except JumeiAPIError as e:
            print('  ❌ %-28s %s (code=%s)' % (name, e, e.code))

    brief('a_rank 排行', lambda: api.a_rank(pageSize=3))
    brief('a_price 报价', lambda: api.a_price('sh000001,sz000001'))
    brief('hs_info 股票信息', lambda: api.hs_info('000001.SZ'))
    brief('a_f10_company F10', lambda: api.a_f10_company('000001'))
    brief('a_extra 信息拓展', lambda: api.a_extra('sz000001'))
    brief('a_stop 停牌', api.a_stop)
    brief('hs_overview 大盘涨跌数', api.hs_overview)
    brief('hs_blockrank 板块排行', lambda: api.hs_blockrank(type='2', pageSize=3))
    brief('hs_tick 分时成交', lambda: api.hs_tick('000001.SZ'))
    brief('a_kline A股K线', lambda: api.a_kline('sz000001', '60', 100))
    brief('hs_kline 沪深K线', lambda: api.hs_kline('000001.SZ', '60', '1', 100))
    brief('hs_mink 分钟K含均线', lambda: api.hs_mink('000001.SZ', '60', 50))
