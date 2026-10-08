#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CME 外盘期货行情工具类
======================

数据源：阿里云 API 市场「CME期货行情」供应商
网关：https://jmcmeqh.market.alicloudapi.com
认证：请求头 Authorization: APPCODE xxx（与 JumData / alirmgbdc 同一份 AppCode）

封装接口：
1. 期货最新行情  POST /futures/cme-price   （表单参数只有一个 symbol）
   - symbol : 期货品种代码，如 HG（COMEX 铜）、GC（黄金）、CL（WTI 原油）
   - 返回：当月主力合约的实时快照（最新价/买卖价/当日高低/成交量/持仓量/涨跌幅）
   - 本工具类额外提供：字段归一化 float/int、昨收推算、合约月份可读化、
     UTC epoch 换算、批量抓取与不可用品种容错

【重要能力边界（2026-09-28 实测，见 _probe_cme_capability.py）】
- 该数据源**只有实时快照接口，没有K线/历史行情接口**：
  * /futures/cme-kline、/futures/cme-history、/futures/kline、
    /futures/cme-minutes、/futures/cme-list 等猜测路径一律 HTTP 404；
  * 在 /futures/cme-price 上附加 type/period/klineType/limit/startTime
    等参数，返回体完全不变（仍是单条快照），说明参数被服务端忽略；
  * 合约月份字段 month（如 2612=2026年12月）由服务端自动选主力，
    传 HG2612 / HG00Y 这类带月份的代码返回 code=201「查无数据」。
  => 想做外盘期货K线回测，本源只能承担"实时打点"角色：自己按秒/分钟
     落库累积成K线；历史K线请走支持K线的源（加密货币可用
     jumdata_kline_utils.get_kline / alicloud_market.get_klines）。
- 响应语义与 JumData 相同：HTTP 状态码不等于业务结果，成功时 HTTP 200、
  失败时 HTTP 400，权威判据是响应体里的 success / code 字段。

密钥：源码内不留明文。默认取常量 ``DEFAULT_APPCODE``，该常量由
      环境变量 CME_APPCODE / JUMEI_APPCODE 或外置凭据文件
      data/jumei_appcode.txt 解析而来（见同目录 appcode.py）。

品种代码可用性（2026-09-28 逐个实测，共测 39 个常见代码）：
- 可用 17 个：见 SYMBOL_TABLE / SYMBOLS_AVAILABLE
- 不可用（返回 code=201 查无数据）：RTY、6E/6J/6B/6A/6C/6S/6N（外汇）、
  LE/HE（畜牧）、BTC/ETH（CME 币类）、QM、RB、PL/PA、ZM/ZO（粕油）、
  以及 ICE 系 KC/SB/CT/CC
- 美债类（ZT/ZF/ZN/ZB）报价用单引号作小数点（如 "101'61" = 101.61），
  已由 _to_float 统一处理；自洽校验：昨收 = 最新价 - 涨跌额 恰好等于当日最高
- 只返回当月主力合约（month 字段服务端自选），无法指定具体合约月份
- updateTime 为北京时间（UTC+8），实测滞后本机时间 15秒~3分钟（服务端快照缓存），
  不适合做高频对时，仅适合做行情快照留存

作者：cryptoTrade
创建时间：2026-09-28
版本：v1.0
"""

import os
import re
import time
import calendar
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import httpx

try:            # 包内导入（Flask 侧以 crypto.ApiUtils.cme_futures_utils 加载）
    from .appcode import resolve_appcode
except ImportError:   # 脚本直跑：本目录已在 sys.path 上
    from appcode import resolve_appcode

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------
CME_HOST = "https://jmcmeqh.market.alicloudapi.com"
PRICE_PATH = "/futures/cme-price"

# 实测不存在的历史/K线路径（保留供探测脚本回归验证）
KLINE_GUESS_PATHS: List[str] = [
    "/futures/cme-kline",
    "/futures/cme-history",
    "/futures/cme-minutes",
    "/futures/kline",
    "/futures/cme-list",
]

# 默认 AppCode：账号级凭据，明文不入库。解析顺序「环境变量 CME_APPCODE /
# JUMEI_APPCODE → 外置文件 data/jumei_appcode.txt → 空串」，详见 appcode.py
DEFAULT_APPCODE = resolve_appcode("CME_APPCODE", "JUMEI_APPCODE")

# HTTP 超时
_TIMEOUT = httpx.Timeout(20.0, connect=15.0, read=20.0, write=15.0, pool=15.0)

# 业务码
CODE_OK = 200
CODE_NO_DATA = 201  # 查无数据（品种代码不存在/无行情）

# 供应商时间戳时区偏移（实测 updateTime 为北京时间）
SERVER_TZ_OFFSET_SECONDS = 8 * 3600

# 候选品种代码全表：代码 -> 说明。
# 供应商文档只写「详见品种代码表」但未提供该表，下表按 CME/CBOT/COMEX/NYMEX
# 通用代码整理，可用性已逐个实测（_probe_cme_capability.py）。
SYMBOL_DESC: Dict[str, str] = {
    # --- CME 股指 ---
    'ES': 'E-mini 标普500（CME 股指）',
    'NQ': 'E-mini 纳斯达克100（CME 股指）',
    'YM': 'E-mini 道琼斯指数（CME 股指）',
    'RTY': 'E-mini 罗素2000（CME 股指）',
    # --- CME 外汇 ---
    '6E': '欧元期货（CME 外汇）',
    '6J': '日元期货（CME 外汇）',
    '6B': '英镑期货（CME 外汇）',
    '6A': '澳元期货（CME 外汇）',
    '6C': '加元期货（CME 外汇）',
    '6S': '瑞郎期货（CME 外汇）',
    '6N': '纽元期货（CME 外汇）',
    # --- CME 利率（美债，报价用单引号作小数点）---
    'ZT': '2年期美国国债（CME 利率）',
    'ZF': '5年期美国国债（CME 利率）',
    'ZN': '10年期美国国债（CME 利率）',
    'ZB': '30年期美国国债（CME 利率）',
    # --- CME 畜牧/数字资产 ---
    'LE': '活牛（CME 畜牧）',
    'HE': '饲养牛（CME 畜牧）',
    'BTC': '比特币期货（CME 数字资产）',
    'ETH': '以太坊期货（CME 数字资产）',
    # --- COMEX 金属 ---
    'GC': '黄金（COMEX）',
    'MGC': '微型黄金（COMEX，报价与 GC 一致、量仓不同）',
    'SI': '白银（COMEX）',
    'HG': '铜（COMEX）',
    # --- NYMEX 能源/金属 ---
    'CL': 'WTI原油（NYMEX）',
    'QM': '微型WTI原油（NYMEX）',
    'NG': '天然气（NYMEX）',
    'HO': '取暖油（NYMEX）',
    'RB': 'RBOB汽油（NYMEX）',
    'PL': '铂（NYMEX）',
    'PA': '钯（NYMEX）',
    # --- CBOT 农产品 ---
    'ZC': '玉米（CBOT）',
    'ZS': '大豆（CBOT）',
    'ZW': '小麦（CBOT）',
    'ZM': '豆粕（CBOT）',
    'ZO': '豆油（CBOT）',
    # --- ICE 系（非四大交易所，预期不覆盖）---
    'KC': '咖啡（ICE）',
    'SB': '原糖（ICE）',
    'CT': '棉花（ICE）',
    'CC': '可可（ICE）',
}

# 实测可用（17 个）：接口正常返回当月主力合约快照
SYMBOLS_AVAILABLE: List[str] = [
    'ES', 'NQ', 'YM',
    'ZT', 'ZF', 'ZN', 'ZB',
    'GC', 'MGC', 'SI', 'HG',
    'CL', 'NG', 'HO',
    'ZC', 'ZS', 'ZW',
]

# 实测不可用（返回 code=201 查无数据），保留供回归验证、避免重复踩坑
SYMBOLS_UNAVAILABLE: List[str] = [
    'RTY',                                     # CME 罗素2000
    '6E', '6J', '6B', '6A', '6C', '6S', '6N',  # CME 外汇
    'LE', 'HE',                                # CME 畜牧
    'BTC', 'ETH',                              # CME 数字资产
    'QM', 'RB', 'PL', 'PA',                    # NYMEX 部分品种
    'ZM', 'ZO',                                # CBOT 豆粕/豆油
    'KC', 'SB', 'CT', 'CC',                    # ICE 系（本源不覆盖）
]

# 可用品种表：代码 -> 说明（业务侧优先用这个）
SYMBOL_TABLE: Dict[str, str] = {k: SYMBOL_DESC[k] for k in SYMBOLS_AVAILABLE}

# 探测脚本用的完整候选表
CANDIDATE_SYMBOLS: Dict[str, str] = SYMBOL_DESC


class CmeFuturesError(RuntimeError):
    """CME 行情接口通用异常（网络失败/业务失败）"""


class CmeAuthError(CmeFuturesError, PermissionError):
    """AppCode 鉴权失败（HTTP 401）"""


class CmeNoDataError(CmeFuturesError):
    """品种代码无数据（业务码 201 查无数据）"""

    def __init__(self, symbol: str, msg: str = '查无数据'):
        super().__init__(f'品种 {symbol!r} 查无数据（{msg}），'
                         f'可用代码见 CANDIDATE_SYMBOLS')
        self.symbol = symbol


def _to_float(value: Any) -> Optional[float]:
    """
    '6.77' / '14,984' / "101'61" / '' -> float / None

    注：美债类合约（ZT/ZF/ZN/ZB）报价用单引号当小数点，如 "101'61" 即 101.61；
    自洽校验：昨收(101'61+0.1=101.71) 恰好等于当日最高价 101'71，确认是小数点而非 32 分度记法。
    """
    if value is None:
        return None
    text = str(value).replace(',', '').replace('%', '').replace("'", '.').strip()
    if not text or text in {'-', '--', '.'}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> Optional[int]:
    num = _to_float(value)
    return int(num) if num is not None else None


class CmeFuturesClient:
    """CME 外盘期货实时行情客户端（连接复用 + 重试 + 字段归一化）。"""

    def __init__(self, appcode: Optional[str] = None, max_retries: int = 3,
                 retry_backoff: float = 1.0):
        """
        Args:
            appcode: 阿里云市场 AppCode，默认取常量/环境变量 CME_APPCODE
            max_retries: 网络异常重试次数（业务错误不重试）
            retry_backoff: 重试退避基数（秒），第 n 次等待 backoff * n
        """
        self.appcode = appcode or DEFAULT_APPCODE or resolve_appcode(
            "CME_APPCODE", "JUMEI_APPCODE")
        if not self.appcode:
            raise ValueError(
                "未配置阿里云 API 市场 AppCode：传 appcode 参数、设环境变量 "
                "CME_APPCODE / JUMEI_APPCODE，或写入 data/jumei_appcode.txt")
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        # 复用同一长连接，避免高频重建连接
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Authorization": "APPCODE " + self.appcode,
        }

    # ------------------------------ 内部请求 ------------------------------
    def _post(self, path: str, bodys: Dict[str, Any]) -> Dict[str, Any]:
        """
        POST 表单请求，返回解析后的 JSON dict（完整响应体）。

        注意：该网关成功返回 HTTP 200、业务失败返回 HTTP 400，但两种情况都
        带 JSON 体，因此以 success/code 为权威判据，不能只看状态码。
        """
        url = CME_HOST + path
        data = {k: v for k, v in bodys.items() if v is not None}
        last_err: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._client.post(url, data=data, headers=self._headers)
                if resp.status_code == 401:
                    raise CmeAuthError("AppCode 鉴权失败（HTTP 401），请检查密钥")
                try:
                    payload = resp.json()
                except ValueError:
                    raise CmeFuturesError(
                        f"响应非JSON（HTTP {resp.status_code}）：{resp.text[:300]}")
                if payload.get("success") is True or payload.get("code") == CODE_OK:
                    return payload
                # 业务失败（如 201 查无数据）：重试无意义，直接抛
                code = payload.get("code")
                msg = payload.get("msg") or "接口返回错误"
                if code == CODE_NO_DATA:
                    raise CmeNoDataError(str(data.get("symbol")), str(msg))
                raise CmeFuturesError(f"接口返回错误 code={code} msg={msg}")
            except (CmeAuthError, CmeNoDataError):
                raise
            except CmeFuturesError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络类异常才重试
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * attempt)
        raise CmeFuturesError(f"请求失败（重试{self.max_retries}次）：{last_err}")

    # ------------------------------ 对外接口 ------------------------------
    def get_quote_raw(self, symbol: str) -> Dict[str, Any]:
        """获取原始响应（含 data/msg/code/taskNo 外层结构）。"""
        return self._post(PRICE_PATH, {"symbol": symbol.upper().strip()})

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """
        获取单个品种当月主力合约实时快照（字段已归一化）。

        Args:
            symbol: 期货品种代码，如 'HG'、'GC'、'CL'（大小写不敏感）

        Returns:
            {
              'symbol': 'HG',            # 品种代码
              'name': '铜（COMEX）',      # 本地品种表说明，未知代码为 None
              'contract_month': '2612',  # 合约月份 YYMM（服务端自动选主力）
              'expiry': '2026-12',       # 合约月份可读形式
              'price': 6.62,             # 最新价（已转 float；美债报价的单引号当作小数点）
              'prev_close': 6.77,        # 昨收（最新价-涨跌额推算）
              'change': -0.15,           # 涨跌额
              'change_rate': -2.19,      # 涨跌幅（%）
              'high': 6.77, 'low': 6.60, # 今日最高/最低
              'bid': 6.62, 'ask': 6.62,  # 买价/卖价
              'volume': 14984,           # 成交量（已去千分位）
              'position': 172940,        # 持仓量
              'update_time': '2026-09-28 17:25:49.539',  # 供应商时间戳原文
              'update_ts': 1789...,      # 解析出的 epoch 秒（按北京时间-8h 还原）
              'raw': {...},              # 服务端原始字段
            }

        Raises:
            CmeNoDataError: 品种代码无数据
            CmeAuthError: 鉴权失败
            CmeFuturesError: 网络/其它业务错误
        """
        payload = self.get_quote_raw(symbol)
        data = payload.get("data") or {}
        if not data:
            raise CmeNoDataError(symbol)
        return self._normalize(symbol.upper().strip(), data)

    def latest_price(self, symbol: str) -> float:
        """只取最新价（数值）。"""
        price = self.get_quote(symbol).get("price")
        if price is None:
            raise CmeFuturesError(f"{symbol} 最新价为空")
        return price

    def get_quotes(self, symbols: Iterable[str], interval: float = 0.2,
                   skip_unavailable: bool = True) -> List[Dict[str, Any]]:
        """
        批量抓取多个品种行情（串行请求，逐个失败不中断）。

        Args:
            symbols: 品种代码可迭代对象
            interval: 相邻请求之间的休眠秒数（规避网关限流）
            skip_unavailable: True 时跳过「查无数据」的品种，False 时原样抛出

        Returns:
            归一化行情字典列表（仅成功的品种）
        """
        results: List[Dict[str, Any]] = []
        symbols = list(symbols)
        for idx, sym in enumerate(symbols):
            try:
                results.append(self.get_quote(sym))
            except CmeNoDataError:
                if not skip_unavailable:
                    raise
            if interval and idx < len(symbols) - 1:
                time.sleep(interval)
        return results

    # ------------------------------ 字段归一化 ------------------------------
    @staticmethod
    def _normalize(symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        quote: Dict[str, Any] = {
            'symbol': data.get('symbol') or symbol,
            'name': CANDIDATE_SYMBOLS.get(symbol) or CANDIDATE_SYMBOLS.get(
                data.get('symbol') or ''),
            'contract_month': data.get('month'),
            'expiry': None,
            'price': None,
            'prev_close': None,
            'change': None,
            'change_rate': None,
            'high': None,
            'low': None,
            'bid': None,
            'ask': None,
            'volume': None,
            'position': None,
            'update_time': data.get('updateTime'),
            'update_ts': None,
            'raw': data,
        }
        quote['price'] = _to_float(data.get('price'))
        quote['change'] = _to_float(data.get('change'))
        quote['change_rate'] = _to_float(data.get('changeRate'))
        quote['high'] = _to_float(data.get('high'))
        quote['low'] = _to_float(data.get('low'))
        quote['bid'] = _to_float(data.get('bid'))
        quote['ask'] = _to_float(data.get('ask'))
        quote['volume'] = _to_int(data.get('volume'))
        quote['position'] = _to_int(data.get('position'))

        # 昨收 = 最新价 - 涨跌额（服务端未直接给昨收）
        if quote['price'] is not None and quote['change'] is not None:
            quote['prev_close'] = round(quote['price'] - quote['change'], 10)

        # 合约月份 2612 -> 2026-12
        month = str(data.get('month') or '')
        if re.fullmatch(r'\d{4}', month):
            quote['expiry'] = f"20{month[:2]}-{month[2:]}"

        # updateTime 为供应商本地时间（实测为北京时间 UTC+8），换算成 UTC epoch 秒
        ts = CmeFuturesClient._parse_server_time(data.get('updateTime'))
        if ts is not None:
            quote['update_ts'] = ts
        return quote

    @staticmethod
    def _parse_server_time(text: Any) -> Optional[int]:
        """供应商时间字符串 -> UTC epoch 秒（按固定 +8 时区换算，不受本机时区影响）。"""
        if not text:
            return None
        raw = str(text).strip()
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                dt = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            return calendar.timegm(dt.utctimetuple()) - SERVER_TZ_OFFSET_SECONDS
        return None

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# 模块级便捷函数
# ---------------------------------------------------------------------------
def get_quote(symbol: str) -> Dict[str, Any]:
    """获取单个品种实时行情（便捷函数）。"""
    with CmeFuturesClient() as client:
        return client.get_quote(symbol)


def get_quotes(symbols: Iterable[str]) -> List[Dict[str, Any]]:
    """批量获取品种行情（便捷函数）。"""
    with CmeFuturesClient() as client:
        return client.get_quotes(symbols)


def latest_price(symbol: str) -> float:
    """获取单个品种最新价（便捷函数）。"""
    with CmeFuturesClient() as client:
        return client.latest_price(symbol)
