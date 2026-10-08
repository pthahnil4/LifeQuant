#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
JumData 加密货币行情工具类
=========================

数据源：阿里云 API 市场（jumdata 提供的加密货币行情）
网关：https://jmjmhb.market.alicloudapi.com

封装接口：
1. 行情K线       POST /crypto/coin/kline
   - symbol    : 交易对，如 BTC/USDT
   - interval  : 1m/3m/5m/15m/30m/1h/2h/4h/8h/1d/3d/1w/1M
   - startTime : 开始时间，毫秒时间戳（可选）
   - endTime   : 结束时间，毫秒时间戳（可选）
   - limit     : 条数，默认 500，最大 1000
2. 最新价格      POST /crypto/coin/price
   - symbol    : 交易对

说明：
- 密钥 AppCode 不在源码里留明文：由构造函数参数、环境变量 JUMDATA_APPCODE /
  JUMEI_APPCODE、或外置凭据文件 data/jumei_appcode.txt 依次解析（见 appcode.py）。
- 依赖 httpx（requirements.txt 已包含）。

实测数据深度（2026-09-28，BTC/USDT，见 _probe_jumdata.py / _probe_jumdata_depth.py）：
- 周期粒度：1m~1M 全部可用，**最小周期 1m**
- 单次请求上限：1000 根（文档一致）
- 历史起点 = 该交易对上线日，且**1m 也是全历史**：
  * BTC/USDT 最早 2017-08-17（实测 1m 从 2017-08-17 07:21 UTC 起，连续无跳空）
  * ETH/USDT 2017-08，DOGE/USDT 2019-07，SOL/USDT 2020-08
- 单周期可得根数（BTC/USDT，按深度推算）：
  * 1m ≈ 479 万根（需翻页 ≈4794 次）
  * 1h ≈ 8.0 万根（翻页 ≈80 次）
  * 1d = 3330 根（实测翻页 5 次到底）
  * 1M = 110 根（单请求即覆盖全历史）
- endTime 语义：返回不晚于 endTime 的最近 limit 根（按 ot 升序）；
  endTime 早于该交易对首根K线时返回空数组，因此可用二分定位最早时间。

作者：ApiUtils
"""

import os
import time
from typing import Dict, List, Optional, Any

import httpx

try:            # 包内导入（Flask 侧以 crypto.ApiUtils.jumdata_kline_utils 加载）
    from .appcode import resolve_appcode
except ImportError:   # 脚本直跑：本目录已在 sys.path 上
    from appcode import resolve_appcode

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------
HOST = "https://jmjmhb.market.alicloudapi.com"
KLINE_PATH = "/crypto/coin/kline"
PRICE_PATH = "/crypto/coin/price"

# 默认 AppCode：账号级凭据，明文不入库。解析顺序「环境变量 JUMDATA_APPCODE /
# JUMEI_APPCODE → 外置文件 data/jumei_appcode.txt → 空串」，详见 appcode.py
DEFAULT_APPCODE = resolve_appcode("JUMDATA_APPCODE", "JUMEI_APPCODE")

# 单次请求最大条数（接口文档：默认500，最大1000）
MAX_LIMIT = 1000

# 支持的K线周期（文档登记值），按粒度从小到大排列
INTERVALS = ["1m", "3m", "5m", "15m", "30m",
             "1h", "2h", "4h", "8h", "1d", "3d", "1w", "1M"]

# 每个周期对应的毫秒数（用于向前翻页定位）
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
    "1M": 2_592_000_000,  # 约30天
}

# HTTP 超时（连接/读取给足余量，弱网友好）
_TIMEOUT = httpx.Timeout(20.0, connect=15.0, read=20.0, write=15.0, pool=15.0)


class JumdataKlineClient:
    """JumData 加密货币行情客户端。"""

    def __init__(self, appcode: Optional[str] = None, max_retries: int = 3):
        self.appcode = (appcode or DEFAULT_APPCODE
                        or resolve_appcode("JUMDATA_APPCODE", "JUMEI_APPCODE"))
        if not self.appcode:
            raise ValueError(
                "未配置阿里云 API 市场 AppCode：传 appcode 参数、设环境变量 "
                "JUMDATA_APPCODE / JUMEI_APPCODE，或写入 data/jumei_appcode.txt")
        self.max_retries = max_retries
        # 复用同一长连接，避免高频重建连接
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Authorization": "APPCODE " + self.appcode,
        }

    # ------------------------------ 内部工具 ------------------------------
    def _post(self, path: str, bodys: Dict[str, Any]) -> Dict[str, Any]:
        """
        带重试的 POST 表单请求，返回解析后的 JSON dict。

        重要：该网关即使请求成功，HTTP 状态码也固定返回 400（后端把业务结果
        放在 JSON 里：success/code 才是权威判据）。因此这里以响应体 JSON 的
        success/code 字段判断成败，只有鉴权失败(401)或解析失败才视为错误。
        """
        url = HOST + path
        data = {k: v for k, v in bodys.items() if v is not None}
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.post(url, data=data, headers=self._headers)
                # 鉴权失败：网关直接 401，无业务体
                if resp.status_code == 401:
                    raise PermissionError("AppCode 鉴权失败（HTTP 401），请检查密钥")
                try:
                    payload = resp.json()
                except ValueError:
                    raise RuntimeError(f"响应非JSON（HTTP {resp.status_code}）：{resp.text[:300]}")
                # 业务层判断：success=true 或 code=200 视为成功
                if payload.get("success") is True or payload.get("code") == 200:
                    return payload
                raise RuntimeError(f"接口返回错误：{payload.get('msg') or payload}")
            except PermissionError:
                raise  # 鉴权错误无需重试
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(min(1 + attempt, 3))  # 1s、2s、3s 递增
        raise RuntimeError(f"请求失败（重试{self.max_retries}次）：{last_err}")

    @staticmethod
    def _extract_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从响应中取出K线数组，兼容 data 为 list 或 data.list/data.klines 等结构。"""
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", payload)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("list", "klines", "items", "rows", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    @staticmethod
    def _bar_ts(bar: Dict[str, Any]) -> Optional[int]:
        """取单根K线的时间戳（毫秒），兼容常见字段名。"""
        # ot=开盘时间, ct=收盘时间（均为毫秒时间戳）
        for key in ("ot", "ct", "time", "timestamp", "ts", "openTime", "open_time", "date"):
            v = bar.get(key)
            if v is None:
                continue
            try:
                iv = int(v)
                # 秒级时间戳补齐为毫秒
                return iv * 1000 if iv < 1e11 else iv
            except (TypeError, ValueError):
                continue
        return None

    # ------------------------------ 对外接口 ------------------------------
    def get_kline(self, symbol: str, interval: str,
                  start_time: Optional[int] = None, end_time: Optional[int] = None,
                  limit: int = MAX_LIMIT) -> List[Dict[str, Any]]:
        """获取K线，返回原始K线对象数组。"""
        payload = self.get_kline_raw(symbol, interval, start_time, end_time, limit)
        return self._extract_items(payload)

    def get_kline_raw(self, symbol: str, interval: str,
                      start_time: Optional[int] = None, end_time: Optional[int] = None,
                      limit: int = MAX_LIMIT) -> Dict[str, Any]:
        """获取K线，返回完整 JSON（便于查看接口元信息）。"""
        bodys = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_time,
            "endTime": end_time,
            "limit": min(int(limit), MAX_LIMIT),
        }
        return self._post(KLINE_PATH, bodys)

    def get_latest_price(self, symbol: str) -> Dict[str, Any]:
        """获取某交易对最新价格（返回完整 JSON）。"""
        return self._post(PRICE_PATH, {"symbol": symbol})

    def fetch_history(self, symbol: str, interval: str,
                      max_bars: int = 100_000, max_pages: int = 200,
                      verbose: bool = False) -> Dict[str, Any]:
        """
        从当前时间向前翻页，尽量拉取全部可得历史K线。

        返回统计信息：
          {
            "count": 总根数,
            "earliest_ts": 最早时间戳(ms),
            "earliest_date": 最早日期字符串,
            "latest_ts": 最新时间戳(ms),
            "pages": 翻页次数,
            "bars": 合并后的K线数组（按时间去重排序）,
          }
        用于回答“最早能取到哪年 / 一共有多少根”。
        """
        seen: Dict[int, Dict[str, Any]] = {}
        end_time: Optional[int] = None
        pages = 0
        while pages < max_pages:
            pages += 1
            bars = self.get_kline(symbol, interval, end_time=end_time, limit=MAX_LIMIT)
            if not bars:
                break
            new_cnt = 0
            min_ts: Optional[int] = None
            for b in bars:
                ts = self._bar_ts(b)
                if ts is None:
                    continue
                if ts not in seen:
                    seen[ts] = b
                    new_cnt += 1
                if min_ts is None or ts < min_ts:
                    min_ts = ts
            if verbose:
                print(f"[page {pages}] got={len(bars)} new={new_cnt} "
                      f"min_ts={min_ts} ({self._fmt(min_ts)})")
            # 本批无新数据或拿不到时间戳，说明已到尽头
            if new_cnt == 0 or min_ts is None:
                break
            if len(seen) >= max_bars:
                break
            # 下一批严格早于当前最早时间
            end_time = min_ts - 1

        ordered_ts = sorted(seen.keys())
        earliest = ordered_ts[0] if ordered_ts else None
        latest = ordered_ts[-1] if ordered_ts else None
        return {
            "count": len(ordered_ts),
            "earliest_ts": earliest,
            "earliest_date": self._fmt(earliest),
            "latest_ts": latest,
            "latest_date": self._fmt(latest),
            "pages": pages,
            "bars": [seen[t] for t in ordered_ts],
        }

    @staticmethod
    def _fmt(ts_ms: Optional[int]) -> str:
        if not ts_ms:
            return "N/A"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_ms / 1000)) + " UTC"

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# 模块级便捷函数
# ---------------------------------------------------------------------------
def get_kline(symbol: str, interval: str, **kwargs) -> List[Dict[str, Any]]:
    with JumdataKlineClient() as client:
        return client.get_kline(symbol, interval, **kwargs)


def get_latest_price(symbol: str) -> Dict[str, Any]:
    with JumdataKlineClient() as client:
        return client.get_latest_price(symbol)
