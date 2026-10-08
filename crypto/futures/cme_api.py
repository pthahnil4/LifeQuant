#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 3 自动搬运（2026-10-08）。
# 原名: futures/cme_api.py  →  现位置: crypto/futures/cme_api.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底与根级 config 依赖已移除。
"""
聚美智数 · CME 期货行情 API 工具类（阿里云市场网关，APPCODE 鉴权）
================================================================
直连 REST 通道：``https://jmcmeqh.market.alicloudapi.com``

商品：cmapi00075202【聚美智数】CME期货行情查询
     （CBOT 期货价格 / 芝加哥商品交易所 / COMEX 贵金属 / NYMEX 原油）
供应商：杭州安那其科技有限公司
接口档案：D:\\python\\TradingView\\金融API包\\cmapi00075202_聚美智数CME期货行情查询CBO

设计
----
- 复用 :class:`stocks.jumei_api.AppCodeGatewayClient`（通用「APPCODE + POST form」
  传输基类，含信封解析与 5xx/网络退避重试），本产品线只换 ``HOST`` + 补品种表。
- :class:`JumeiCMEFuturesAPI` —— CME 期货客户端，覆盖唯一接口 ``/futures/cme-price``，
  并提供行情规范化、品种目录、批量拉取等便利方法。

凭据统一来自根级 ``config.py``（环境变量 / .env 优先：JUMEI_CME_API_HOST /
JUMEI_CME_APPCODE / JUMEI_CME_TIMEOUT），本文件不写死密钥。

关键限制（官方文档确认）
------------------------
- 仅 1 个接口、POST 表单，入参只有 ``symbol``（期货品种代码）。
- 返回的是**主力连续合约的单条快照**（最新价/买卖/高低/涨跌/量/持仓/更新时间），
  没有 K 线、没有日期区间、没有翻页。要历史序列须自建定时采集入库。
- ``volume`` / ``position`` 原始值带千分位逗号（如 ``"76,214"``），规范化时去除。
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import logging
import os
import sys

logger = logging.getLogger(__name__)


from crypto.stocks.jumei_api import AppCodeGatewayClient, JumeiAPIError  # noqa: E402

try:
    from crypto.stocks import gateway_config as config
    _HOST = getattr(config, 'JUMEI_CME_API_HOST', 'https://jmcmeqh.market.alicloudapi.com')
    _APPCODE = getattr(config, 'JUMEI_CME_APPCODE', '')
    _TIMEOUT = getattr(config, 'JUMEI_CME_TIMEOUT', 15)
except Exception:  # config 不可用时回退环境变量，保持模块可独立导入
    _HOST = os.environ.get(
        'JUMEI_CME_API_HOST', 'https://jmcmeqh.market.alicloudapi.com')
    _APPCODE = (os.environ.get('JUMEI_CME_APPCODE')
                or os.environ.get('JUMEI_APPCODE') or '')
    _TIMEOUT = int(os.environ.get('JUMEI_CME_TIMEOUT')
                   or os.environ.get('JUMEI_TIMEOUT') or 15)


# --------------------------------------------------------------------
#  品种代码表：code -> (中文名, 交易所, 分类)
#  来源：接口文档「期货品种代码表」，涵盖金属/股指/能源/农产品/外汇/利率。
# --------------------------------------------------------------------
CME_SYMBOLS = {
    # 金属 (COMEX)
    'GC':   ('黄金期货', 'COMEX', '金属'),
    'MGC':  ('微型黄金期货', 'COMEX', '金属'),
    'SI':   ('白银期货', 'COMEX', '金属'),
    'HG':   ('铜期货', 'COMEX', '金属'),
    'ALI':  ('铝期货', 'COMEX', '金属'),
    'LTC':  ('碳酸锂', 'COMEX', '金属'),
    'LTH':  ('氢氧化锂', 'COMEX', '金属'),
    # 股指 (CME)
    'ES':   ('E-迷你标普500期货', 'CME', '股指'),
    'MES':  ('微型E-迷你标普500期货', 'CME', '股指'),
    'NQ':   ('E-迷你纳斯达克100指数期货', 'CME', '股指'),
    'MNQ':  ('微型E-迷你纳斯达克100指数期货', 'CME', '股指'),
    'YM':   ('E-迷你道琼斯指数期货', 'CME', '股指'),
    'NKD':  ('日经225指数（美元）期货', 'CME', '股指'),
    'NKY':  ('日经225指数（日元）期货', 'CME', '股指'),
    # 能源 (NYMEX)
    'CL':   ('WTI原油期货', 'NYMEX', '能源'),
    'MCL':  ('微型WTI原油期货', 'NYMEX', '能源'),
    'NG':   ('天然气期货', 'NYMEX', '能源'),
    'HO':   ('燃油期货', 'NYMEX', '能源'),
    # 农产品 (CBOT)
    'ZC':   ('玉米期货', 'CBOT', '农产品'),
    'MZC':  ('微型玉米期货', 'CBOT', '农产品'),
    'ZS':   ('大豆期货', 'CBOT', '农产品'),
    'MZS':  ('微型大豆期货', 'CBOT', '农产品'),
    'ZL':   ('豆油期货', 'CBOT', '农产品'),
    'MZL':  ('微型豆油期货', 'CBOT', '农产品'),
    'ZW':   ('软红冬麦期货', 'CBOT', '农产品'),
    'MZW':  ('微型软红冬麦期货', 'CBOT', '农产品'),
    'LN':   ('瘦猪肉期货', 'CBOT', '农产品'),
    # 外汇 (CME)
    'CNH':  ('美元/离岸人民币期货', 'CME', '外汇'),
    'EC':   ('欧元期货', 'CME', '外汇'),
    'BP':   ('英镑期货', 'CME', '外汇'),
    'AD':   ('澳元期货', 'CME', '外汇'),
    'JY':   ('日元期货', 'CME', '外汇'),
    # 利率 (CBOT)
    'ZT':   ('2年期美国国债期货', 'CBOT', '利率'),
    'ZF':   ('5年期美国国债期货', 'CBOT', '利率'),
    'ZN':   ('10年期美国国债期货', 'CBOT', '利率'),
    'ZB':   ('美国长期国债期货', 'CBOT', '利率'),
    'TN':   ('超长10年期美国国债期货', 'CBOT', '利率'),
    'SR1':  ('1个月SOFR期货', 'CBOT', '利率'),
    'SR3':  ('3个月SOFR期货', 'CBOT', '利率'),
    '2YY':  ('2年期收益率期货', 'CBOT', '利率'),
    '5YY':  ('5年期收益率期货', 'CBOT', '利率'),
    '10YY': ('10年期收益率期货', 'CBOT', '利率'),
    '30YY': ('30年期收益率期货', 'CBOT', '利率'),
}

# 接口注册表：标识 -> (path, 说明)
ENDPOINTS = {
    'cme_price': ('/futures/cme-price', 'CME期货行情快照(symbol)'),
}


def _to_float(val):
    """安全转 float；空/非法返回 None。

    兼容三种数据源格式：
    - 普通十进制（含千分位逗号、百分号），如 ``"4689.4"`` / ``"76,214"``；
    - CME 国债类「点'32分」报价（Treasury/point-32nds），如 ``"106'14"``
      表示 106 + 14/32 = 106.4375；三段式如 ``"106'145"`` 末位为 32 分的十分位
      （即 14.5/32），对应 ZN 等合约的半个 32 分最小变动价位。
    """
    if val is None:
        return None
    # 统一各种撇号为 ASCII 单引号，去除千分位逗号与百分号/空白
    s = (str(val).replace('’', "'").replace('′', "'")
         .replace(',', '').replace('%', '').strip())
    if s == '':
        return None
    # 国债「点'32分」记法：' 前为整数点，' 后为 32 分数（末位可为十分位小数）
    if "'" in s:
        try:
            points, frac = s.split("'", 1)
            points = float(points) if points not in ('', '.') else 0.0
            frac = frac.strip()
            if not frac.isdigit():
                return None
            # 两位→纯 32 分；三位及以上→末位是 32 分的十分位小数
            if len(frac) <= 2:
                thirty_seconds = float(frac)
            else:
                thirty_seconds = float(frac[:-1] + '.' + frac[-1])
            return points + thirty_seconds / 32.0
        except (ValueError, IndexError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(val):
    """安全转 int；空/非法返回 None。去除千分位逗号。"""
    f = _to_float(val)
    return int(f) if f is not None else None


class JumeiCMEFuturesAPI(AppCodeGatewayClient):
    """聚美智数 CME 期货行情客户端（接口 /futures/cme-price）。

    继承通用 APPCODE 传输基类，仅覆盖 ``HOST``/``TIMEOUT`` 默认值。
    """

    HOST = _HOST
    TIMEOUT = _TIMEOUT

    def __init__(self, appcode=None, host=None, timeout=None):
        # 显式给出 CME 专属默认，保留基类 appcode 校验逻辑
        super().__init__(
            appcode=appcode or _APPCODE,
            host=host or self.HOST,
            timeout=timeout or self.TIMEOUT,
        )

    # ----------------------------------------------------------------
    #  原始接口
    # ----------------------------------------------------------------
    def cme_price(self, symbol):
        """CME 期货行情快照（原始返回 dict）。

        :param symbol: 期货品种代码，如 ``GC``/``CL``/``ES``，详见 :data:`CME_SYMBOLS`。
        :returns: ``{code, msg, taskNo, data:{symbol, month, price, ...}}``。
        """
        symbol = (symbol or '').strip().upper()
        if not symbol:
            raise JumeiAPIError('期货品种代码不能为空')
        return self.post(ENDPOINTS['cme_price'][0], {'symbol': symbol})

    # ----------------------------------------------------------------
    #  规范化行情
    # ----------------------------------------------------------------
    def get_quote(self, symbol):
        """拉取并规范化单只期货行情，返回扁平 dict（数值已转 float/int）。

        数字字段（price/bid/ask/high/low/change/changeRate）转 float，
        volume/position 去逗号转 int；并附品种中文名/交易所/分类。
        出错时抛 :class:`JumeiAPIError`。
        """
        resp = self.cme_price(symbol)
        data = resp.get('data') or {}
        code = data.get('symbol') or (symbol or '').strip().upper()
        meta = CME_SYMBOLS.get(code)
        return {
            'symbol': code,
            'name': meta[0] if meta else None,
            'exchange': meta[1] if meta else None,
            'category': meta[2] if meta else None,
            'month': data.get('month'),
            'price': _to_float(data.get('price')),
            'bid': _to_float(data.get('bid')),
            'ask': _to_float(data.get('ask')),
            'high': _to_float(data.get('high')),
            'low': _to_float(data.get('low')),
            'change': _to_float(data.get('change')),
            'changeRate': _to_float(data.get('changeRate')),
            'volume': _to_int(data.get('volume')),
            'position': _to_int(data.get('position')),
            'updateTime': data.get('updateTime'),
            'taskNo': resp.get('taskNo'),
        }

    def get_quotes(self, symbols):
        """批量拉取多只行情。

        :param symbols: 品种代码可迭代对象（如 ``['GC', 'CL', 'ES']``）。
        :returns: dict，``symbol -> 规范化行情 | {'error': 描述}``（单只失败不影响其它）。
        """
        result = {}
        for sym in symbols:
            key = (sym or '').strip().upper()
            if not key:
                continue
            try:
                result[key] = self.get_quote(key)
            except JumeiAPIError as e:
                logger.warning('[cme] %s 拉取失败：%s', key, e)
                result[key] = {'error': str(e)}
        return result

    # ----------------------------------------------------------------
    #  品种目录（离线，不发请求）
    # ----------------------------------------------------------------
    @staticmethod
    def list_symbols(category=None, exchange=None):
        """列出品种代码；可按分类（如 '金属'）或交易所（如 'COMEX'）过滤。"""
        out = []
        for code, (name, exch, cat) in CME_SYMBOLS.items():
            if category and cat != category:
                continue
            if exchange and exch != exchange:
                continue
            out.append({'symbol': code, 'name': name,
                        'exchange': exch, 'category': cat})
        return out


# ====================================================================
#  自测入口
# ====================================================================
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    print('=' * 68)
    print('  聚美智数 CME 期货行情 REST 自测  host=%s' % (_HOST,))
    print('  已登记品种 %d 个，接口 %d 个' % (len(CME_SYMBOLS), len(ENDPOINTS)))
    print('=' * 68)

    try:
        api = JumeiCMEFuturesAPI()
    except JumeiAPIError as e:
        print('  ❌ 初始化失败：%s' % e)
        sys.exit(1)

    def show(sym):
        try:
            q = api.get_quote(sym)
            print('  ✅ %-5s %-22s 价=%-10s 涨跌=%-8s 高=%-9s 低=%-9s '
                  '量=%-10s 持仓=%-10s @%s'
                  % (q['symbol'], q['name'] or '-', q['price'], q['change'],
                     q['high'], q['low'], q['volume'], q['position'],
                     q['updateTime']))
        except JumeiAPIError as e:
            print('  ❌ %-5s %s (code=%s)' % (sym, e, e.code))

    # 逐品种连通性测试（金属/股指/能源/农产品/外汇/利率各取一只）
    for s in ('GC', 'SI', 'HG', 'ES', 'NQ', 'CL', 'NG', 'ZC', 'ZS',
              'CNH', 'EC', 'ZN'):
        show(s)

    # 批量接口演示
    print('-' * 68)
    batch = api.get_quotes(['GC', 'CL', 'ES'])
    print('  批量 get_quotes(GC/CL/ES) ->',
          {k: (v.get('price') if 'error' not in v else v['error'])
           for k, v in batch.items()})

    # 参数错误演示（空 symbol，本地校验不发请求）
    print('-' * 68)
    try:
        api.cme_price('')
    except JumeiAPIError as e:
        print('  ✅ 空代码本地校验生效：%s' % e)
