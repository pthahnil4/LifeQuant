"""
OKX API 工具包
=============

提供OKX交易所API的工具方法和辅助函数

__init__.py - 包初始化文件
account_utils.py - 简化版账户查询工具
account_query_utils.py - 完整版账户查询工具
jumdata_kline_utils.py - JumData 加密货币K线/最新价格工具类
alicloud_market.py - 阿里云市场加密货币行情工具类（实时行情/历史K线）
cme_futures_utils.py - 阿里云市场 CME 外盘期货实时行情工具类（无K线接口）
"""

from .account_query_utils import (
    AccountQueryUtils,
    query_current_positions,
    query_positions_history, 
    query_asset_valuation
)
from .jumdata_kline_utils import (
    JumdataKlineClient,
    get_kline as jumdata_get_kline,
    get_latest_price as jumdata_get_latest_price,
)
from .alicloud_market import (
    AlicloudMarketClient,
    AlicloudMarketAPIError,
    get_realtime_quotes as alicloud_get_realtime_quotes,
    get_klines as alicloud_get_klines,
    KLINE_PERIODS as ALICLOUD_KLINE_PERIODS,
)
from .cme_futures_utils import (
    CmeFuturesClient,
    CmeFuturesError,
    CmeAuthError,
    CmeNoDataError,
    get_quote as cme_get_quote,
    get_quotes as cme_get_quotes,
    latest_price as cme_latest_price,
    SYMBOL_TABLE as CME_SYMBOL_TABLE,
)

__version__ = "1.0.0"
__author__ = "OKX API Utils"

__all__ = [
    'AccountQueryUtils',
    'query_current_positions',
    'query_positions_history',
    'query_asset_valuation',
    'JumdataKlineClient',
    'jumdata_get_kline',
    'jumdata_get_latest_price',
    'AlicloudMarketClient',
    'AlicloudMarketAPIError',
    'alicloud_get_realtime_quotes',
    'alicloud_get_klines',
    'ALICLOUD_KLINE_PERIODS',
    'CmeFuturesClient',
    'CmeFuturesError',
    'CmeAuthError',
    'CmeNoDataError',
    'cme_get_quote',
    'cme_get_quotes',
    'cme_latest_price',
    'CME_SYMBOL_TABLE',
]