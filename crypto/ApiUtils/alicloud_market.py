#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
阿里云市场 API 行情工具类（加密货币）
=====================================

数据来源：
- 网关：http://alirmgbdc.market.alicloudapi.com
- 认证：APPCODE 方式（请求头 Authorization: APPCODE xxx）

【当前可用状态】2026-09-28 复测：/query/comrms 与 /query/comkm 均返回 HTTP 403（空体），
而同一份 AppCode 在 jmjmhb（jumdata）网关仍正常，说明是本商品授权到期/额度用尽，
不是密钥错误。下列“实测数据深度”仅作历史参考；需要加密货币K线请改用
jumdata_kline_utils（已复测可用，1m 可追到 2017-08-17）。

提供接口：
1. get_realtime_quotes(symbols)  - 批量查询实时行情 /query/comrms，最多10个品种
2. get_klines(...)               - 单页历史K线 /query/comkm
3. iter_klines(...)              - 生成器，自动翻页把历史K线全部拉完
4. count_klines(...)             - 探测某品种+周期的K线总根数（倍增+二分，不下全量）
5. get_data_coverage(...)        - 一次性算出：总根数 + 最早一根时间 + 最新一根时间
6. probe_periods(symbol)         - 逐周期探测数据覆盖（用于评估数据深度）

K线接口参数说明（来自供应商文档 + 实测修正）：
- period：文档为 1M,3M,5M,10M,15M,30M,1H,2H,3H,4H,D,W,M；实测 3H 返回 HTTP 400 不可用
- pidx  ：页码，排序方向是"由当前向历史"，第1页最新
- psize ：每页最多500条
- withlast：是否包含最新一根动态K线，仅第1页有效
- 数据深度（实测，以 BTC 为例）：
  * 1分钟：名义"最近30天"，实际约 9 天 / 约 1.3 万根
  * 5分钟~4小时：名义"180天/6个月"，实际约 60 天 / 5M 约 1.7 万根
  * 日/周/月线：可一直追溯到币种上市时间（BTC 约 2013 年，日约 4836 根）

原始K线字段含义（服务端返回）：
- Tick：K线开盘时间戳（秒）
- D   ：K线开盘时间字符串
- O/H/L/C：开高低收
- V   ：成交量
- A   ：成交额

作者：cryptoTrade
创建时间：2026-09-28
版本：v1.0
"""

import time
import logging
from typing import Dict, List, Optional, Any, Iterator, Tuple

try:            # 包内导入（Flask 侧以 crypto.ApiUtils.alicloud_market 加载）
    from .appcode import resolve_appcode
except ImportError:   # 脚本直跑：本目录已在 sys.path 上
    from appcode import resolve_appcode

import requests

logger = logging.getLogger(__name__)

# ===========================================================================
# 配置区域（供应商密钥，可直接在初始化时覆盖）
# ===========================================================================
ALICLOUD_HOST = 'http://alirmgbdc.market.alicloudapi.com'
# AppCode 属账号级凭据，明文不能留在源码里（本仓库是公开仓库）。
# 解析顺序：环境变量 ALICLOUD_APPCODE / JUMEI_APPCODE → 外置文件
# data/jumei_appcode.txt → 空串。凭据留档与配置方式见 crypto/ApiUtils/appcode.py。
ALICLOUD_APPCODE = resolve_appcode('ALICLOUD_APPCODE', 'JUMEI_APPCODE')

# 支持的K线周期（由小到大）。注：文档列出 3H 但实测服务端返回 HTTP 400，故默认不纳入
KLINE_PERIODS: List[str] = ['1M', '3M', '5M', '10M', '15M', '30M',
                            '1H', '2H', '4H', 'D', 'W', 'M']

# 周期 -> 相邻两根K线的间隔秒数（用于校验数据连续性）
PERIOD_INTERVAL_SECONDS: Dict[str, int] = {
    '1M': 60, '3M': 180, '5M': 300, '10M': 600, '15M': 900, '30M': 1800,
    '1H': 3600, '2H': 7200, '4H': 14400,
    'D': 86400, 'W': 604800, 'M': 2592000,  # 月线按30天近似，仅做参考
}

MAX_PAGE_SIZE = 500          # 服务端每页上限
DEFAULT_TIMEOUT = (10, 20)   # (连接超时, 读取超时)

# 各周期理论最大根数（作为二分/倍增搜索上限，需 >= 实际可用深度，否则会漏计尾页）
# 实测：日/周/月线实际可追溯到币种上市时间（BTC 约 2013 年，>10年），故 D/W/M 上限放宽
PERIOD_BAR_CEILING: Dict[str, int] = {
    '1M': 30 * 24 * 60,           # 43200（服务端窗口上限，实际约9天）
    '3M': 30 * 24 * 20,           # 14400
    '5M': 180 * 24 * 12,          # 51840（服务端窗口上限，实际约60天）
    '10M': 180 * 24 * 6,          # 25920
    '15M': 180 * 24 * 4,          # 17280
    '30M': 180 * 24 * 2,          # 8640
    '1H': 180 * 24,               # 4320
    '2H': 180 * 12,               # 2160
    '4H': 180 * 6,                # 1080
    'D': 16 * 366,                # 5856（留足闰年与超10年余量）
    'W': 16 * 53,                 # 848
    'M': 16 * 12,                 # 192
}


class AlicloudMarketAPIError(Exception):
    """行情接口业务异常（HTTP非200或Code非0）"""
    pass


class AlicloudMarketClient:
    """阿里云市场加密货币行情客户端（连接复用 + 重试 + 翻页）"""

    def __init__(self,
                 host: str = ALICLOUD_HOST,
                 appcode: Optional[str] = None,
                 timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
                 max_retries: int = 3,
                 retry_backoff: float = 1.0):
        """
        Args:
            host: 网关地址
            appcode: 阿里云市场 AppCode；不传时按「环境变量 → 外置凭据文件」解析
            timeout: (连接超时秒, 读取超时秒)
            max_retries: 网络失败重试次数
            retry_backoff: 重试退避基数（秒），第n次等待 backoff*n
        """
        self.host = host.rstrip('/')
        self.appcode = appcode or ALICLOUD_APPCODE or resolve_appcode(
            'ALICLOUD_APPCODE', 'JUMEI_APPCODE')
        if not self.appcode:
            # 空凭据去打接口只会收到 401，排查成本高；在这里就把话说清楚
            raise ValueError(
                '未配置阿里云 API 市场 AppCode：传 appcode 参数、设环境变量 '
                'ALICLOUD_APPCODE / JUMEI_APPCODE，或写入 data/jumei_appcode.txt')
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        # 复用同一个 Session（keep-alive），避免高频重建 TCP 连接
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # 内部请求封装
    # ------------------------------------------------------------------
    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        统一 GET 请求，带重试与业务码校验。

        Returns:
            服务端 JSON（形如 {"Code":0,"Msg":"","Obj":[...]}）

        Raises:
            AlicloudMarketAPIError: 重试耗尽、HTTP异常或业务Code非0
        """
        url = f'{self.host}{path}'
        headers = {'Authorization': f'APPCODE {self.appcode}'}
        last_err: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.get(url, params=params, headers=headers,
                                         timeout=self.timeout)
                if resp.status_code != 200:
                    # 4xx 一般是参数/额度问题，重试无意义，直接抛出
                    if 400 <= resp.status_code < 500:
                        raise AlicloudMarketAPIError(
                            f'HTTP {resp.status_code} {path} params={params} body={resp.text[:200]}')
                    last_err = AlicloudMarketAPIError(
                        f'HTTP {resp.status_code} {path} body={resp.text[:200]}')
                else:
                    payload = resp.json()
                    code = payload.get('Code')
                    if code not in (0, None):
                        # 例如翻页越界会返回 Code!=0，属于可预期的终止信号
                        raise AlicloudMarketAPIError(
                            f'业务错误 Code={code} Msg={payload.get("Msg")!r} '
                            f'path={path} params={params}')
                    return payload
            except AlicloudMarketAPIError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_err = exc
                logger.warning('请求 %s 第%d次失败: %s', path, attempt, exc)

            if attempt < self.max_retries:
                time.sleep(self.retry_backoff * attempt)

        raise AlicloudMarketAPIError(f'请求 {path} 重试{self.max_retries}次仍失败: {last_err}')

    # ------------------------------------------------------------------
    # 1. 实时行情
    # ------------------------------------------------------------------
    def get_realtime_quotes(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """
        批量查询实时行情（最多10个品种）。

        Args:
            symbols: 品种代码列表，如 ['BTC', 'LTC']

        Returns:
            行情字典列表，主要字段：
            S=代码, N=名称, P=最新价, O/H/L=今开/高/低, YC=昨收,
            V=成交量, A=成交额, Z=总市值, Z2=流通市值, ZF=涨跌幅(%),
            Tick=时间戳, Time=时间字符串, B1~B5/S1~S5=买卖五档
        """
        if not symbols:
            raise ValueError('symbols 不能为空')
        if len(symbols) > 10:
            raise ValueError('symbols 最多10个，当前传入%d个' % len(symbols))

        payload = self._request('/query/comrms', {'symbols': ','.join(symbols)})
        return payload.get('Obj') or []

    # ------------------------------------------------------------------
    # 2. 历史K线（单页）
    # ------------------------------------------------------------------
    def get_klines(self,
                   symbol: str,
                   period: str = 'D',
                   pidx: int = 1,
                   psize: int = MAX_PAGE_SIZE,
                   withlast: int = 0) -> List[Dict[str, Any]]:
        """
        查询单页历史K线。

        Args:
            symbol: 品种代码，如 BTC
            period: K线周期，见 KLINE_PERIODS
            pidx: 页码，从1开始；排序为"由当前向历史"，第1页是最新
            psize: 每页条数，1~500
            withlast: 1=包含最新一根动态K线（仅第1页有效）

        Returns:
            K线列表（时间倒序，第0条最新），每条含
            Tick/O/H/L/C/V/A/D 字段
        """
        if period not in KLINE_PERIODS:
            raise ValueError(f'period 仅支持 {KLINE_PERIODS}，当前 {period!r}')
        if not 1 <= psize <= MAX_PAGE_SIZE:
            raise ValueError(f'psize 需在 1~{MAX_PAGE_SIZE} 之间')
        if pidx < 1:
            raise ValueError('pidx 从 1 开始')

        payload = self._request('/query/comkm', {
            'symbol': symbol, 'period': period,
            'pidx': pidx, 'psize': psize, 'withlast': withlast,
        })
        return payload.get('Obj') or []

    def iter_klines(self,
                    symbol: str,
                    period: str = 'D',
                    max_pages: Optional[int] = None,
                    psize: int = MAX_PAGE_SIZE,
                    withlast: int = 0,
                    page_interval: float = 0.0) -> Iterator[Dict[str, Any]]:
        """
        生成器：自动翻页逐条产出K线（时间倒序，最新在前）。

        Args:
            max_pages: 最多翻多少页，None 表示翻到没有数据为止
            page_interval: 每页之间强制休眠秒数（规避限流）

        Yields:
            单条K线字典（额外补充 symbol / period 字段）
        """
        pidx = 1
        while max_pages is None or pidx <= max_pages:
            try:
                bars = self.get_klines(symbol=symbol, period=period, pidx=pidx,
                                       psize=psize,
                                       withlast=withlast if pidx == 1 else 0)
            except AlicloudMarketAPIError as exc:
                # 翻到历史尽头/越界时服务端会报错，视为正常终止
                logger.info('翻页结束于 pidx=%d：%s', pidx, exc)
                return
            if not bars:
                logger.info('翻页结束于 pidx=%d（空数据）', pidx)
                return

            for bar in bars:
                bar['symbol'] = symbol
                bar['period'] = period
                yield bar

            if len(bars) < psize:
                return
            pidx += 1
            if page_interval:
                time.sleep(page_interval)

    # ------------------------------------------------------------------
    # 3. 数据深度探测
    # ------------------------------------------------------------------
    def _probe_page(self, symbol: str, period: str, pidx: int,
                    psize: int = MAX_PAGE_SIZE) -> int:
        """返回某页实际条数；越界/报错/空数据统一返回 0（数据连续，0 即代表到头）"""
        try:
            return len(self.get_klines(symbol, period, pidx, psize))
        except AlicloudMarketAPIError:
            return 0

    def count_klines(self,
                     symbol: str,
                     period: str,
                     psize: int = MAX_PAGE_SIZE,
                     upper_bound: Optional[int] = None) -> Optional[int]:
        """
        探测某品种+周期的K线总根数（不下载全量数据）。

        数据是连续的：最后一个有数据的页要么是"非满页"(len<psize)，
        要么其下一页返回 0。用"倍增找上界 + 二分定位最后一个满页"即可。

        Args:
            upper_bound: 总根数搜索上限，默认取 PERIOD_BAR_CEILING[period]

        Returns:
            总根数，None 表示首页都取不到
        """
        if upper_bound is None:
            upper_bound = PERIOD_BAR_CEILING.get(period, 200000)

        first_n = self._probe_page(symbol, period, 1, psize)
        if first_n == 0:
            return None
        if first_n < psize:            # 首页就是非满页，数据全在首页
            return first_n

        # 倍增找到第一个"非满/空/越界"页 (len < psize)
        lo = 1                          # lo 页保证是满页
        hi = 2
        max_page = max(upper_bound // psize, 2)
        while hi <= max_page and self._probe_page(symbol, period, hi, psize) == psize:
            lo = hi
            hi *= 2
        hi = min(hi, max_page + 1)      # 越界保护，hi 为未知/非满边界

        # 二分定位最后一个满页 last_full（lo..hi 之间）
        last_full = lo
        l, r = lo + 1, hi
        while l <= r:
            mid = (l + r) // 2
            if self._probe_page(symbol, period, mid, psize) == psize:
                last_full = mid
                l = mid + 1
            else:
                r = mid - 1

        # 最后一个满页之后的那一页决定总根数
        tail_n = self._probe_page(symbol, period, last_full + 1, psize)
        return last_full * psize + tail_n

    def get_data_coverage(self,
                          symbol: str,
                          period: str,
                          psize: int = MAX_PAGE_SIZE,
                          upper_bound: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        一次性给出某品种+周期的数据覆盖情况。

        Args:
            symbol: 品种代码
            period: K线周期
            psize: 探测用的每页条数
            upper_bound: 总根数搜索上限

        Returns:
            {
              'symbol': 'BTC', 'period': 'D',
              'total_bars': 3650,            # 总根数（页边界近似）
              'latest_time': '2026-09-28',   # 最新一根K线时间
              'earliest_time': '2016-10-01', # 最早一根K线时间
              'earliest_ts': 1475280000,
              'years_covered': 10.0,         # 覆盖年数
              'intraday': False,             # 是否分钟/小时级别
            }
            取不到数据时返回 None
        """
        newest = self.get_klines(symbol, period, 1, 1, withlast=0)
        if not newest:
            return None

        total = self.count_klines(symbol, period, psize, upper_bound)
        if not total:
            return None

        last_page = (total + psize - 1) // psize
        tail = self.get_klines(symbol, period, last_page, psize)
        earliest = tail[-1] if tail else newest[-1]

        latest_ts = int(newest[0]['Tick'])
        earliest_ts = int(earliest['Tick'])
        span_days = max((latest_ts - earliest_ts) / 86400.0, 0.0)

        interval = PERIOD_INTERVAL_SECONDS.get(period)
        intraday = period not in ('D', 'W', 'M')
        # 按理论间隔推算的跨度（用于和服务端返回时间交叉校验）
        span_days_by_bars = (total - 1) * interval / 86400.0 if interval else None

        return {
            'symbol': symbol,
            'period': period,
            'total_bars': total,
            'latest_time': newest[0].get('D'),
            'latest_ts': latest_ts,
            'earliest_time': earliest.get('D'),
            'earliest_ts': earliest_ts,
            'years_covered': round(span_days / 365.0, 2),
            'span_days': round(span_days, 1),
            'span_days_by_interval': round(span_days_by_bars, 1)
            if span_days_by_bars is not None else None,
            'intraday': intraday,
        }

    def probe_periods(self,
                      symbol: str,
                      periods: Optional[List[str]] = None,
                      sleep: float = 0.5) -> List[Dict[str, Any]]:
        """
        对单个品种逐个周期探测数据覆盖（用于判断最小周期与最长历史）。

        Args:
            symbol: 品种代码
            periods: 需要探测的周期，默认全部 KLINE_PERIODS
            sleep: 每个周期之间的休眠秒数

        Returns:
            探测结果列表（顺序与 periods 一致），失败项含 'error' 字段
        """
        results: List[Dict[str, Any]] = []
        for period in (periods or KLINE_PERIODS):
            try:
                cov = self.get_data_coverage(symbol, period)
                results.append(cov or {'symbol': symbol, 'period': period,
                                       'error': 'no_data'})
            except Exception as exc:  # noqa: BLE001 - 探测脚本需要容错继续
                logger.exception('探测 %s %s 失败', symbol, period)
                results.append({'symbol': symbol, 'period': period,
                                'error': str(exc)})
            if sleep:
                time.sleep(sleep)
        return results


# ----------------------------------------------------------------------
# 便捷函数
# ----------------------------------------------------------------------
_default_client: Optional[AlicloudMarketClient] = None


def get_default_client() -> AlicloudMarketClient:
    """获取进程级共享客户端（复用连接）"""
    global _default_client
    if _default_client is None:
        _default_client = AlicloudMarketClient()
    return _default_client


def get_realtime_quotes(symbols: List[str]) -> List[Dict[str, Any]]:
    """批量查询实时行情（便捷函数）"""
    return get_default_client().get_realtime_quotes(symbols)


def get_klines(symbol: str, period: str = 'D', pidx: int = 1,
               psize: int = MAX_PAGE_SIZE, withlast: int = 0) -> List[Dict[str, Any]]:
    """查询单页历史K线（便捷函数）"""
    return get_default_client().get_klines(symbol, period, pidx, psize, withlast)


def to_pandas(bars: List[Dict[str, Any]]):
    """
    把K线列表转成 DataFrame（时间正序，索引为开盘时间）。

    Args:
        bars: get_klines / iter_klines 产出的K线字典列表

    Returns:
        pandas.DataFrame，列：open/high/low/close/volume/amount/ts/datetime
    """
    import pandas as pd

    df = pd.DataFrame(bars)
    if df.empty:
        return df
    df = df.rename(columns={'O': 'open', 'H': 'high', 'L': 'low', 'C': 'close',
                            'V': 'volume', 'A': 'amount', 'Tick': 'ts',
                            'D': 'datetime'})
    df['ts'] = pd.to_numeric(df['ts'], errors='coerce').astype('int64')
    df['datetime'] = pd.to_datetime(df['datetime'], errors='coerce')
    df = df.sort_values('ts').reset_index(drop=True)
    cols = ['ts', 'datetime', 'open', 'high', 'low', 'close', 'volume', 'amount']
    if 'symbol' in df.columns:
        cols.append('symbol')
    if 'period' in df.columns:
        cols.append('period')
    return df[cols]
