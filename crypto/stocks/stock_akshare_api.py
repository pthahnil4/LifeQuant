#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 3 自动搬运（2026-10-08）。
# 原名: stocks/stock_akshare_api.py  →  现位置: crypto/stocks/stock_akshare_api.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底与根级 config 依赖已移除。
"""
统一的 akshare 股票数据接口工具类
=================================
提供统一接口封装 akshare 的 A股相关 API，包括：
  - A股日线历史数据 (stock_zh_a_daily，新浪数据源)
  - A股分钟K线数据 (stock_zh_a_minute，新浪数据源)
  - 日线备选数据 (stock_zh_a_hist_tx，腾讯数据源)
  - 股票代码 ↔ 中文名双向映射

所有方法返回 pandas DataFrame，列名统一为英文。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import datetime
import logging
import time
from typing import Optional, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)


class AkshareAPIError(Exception):
    """akshare 数据获取异常"""
    pass


class AkshareStockAPI:
    """统一的 akshare A 股数据接口工具类

    封装常用的 akshare 股票数据接口，提供一致的调用方式和列名。
    数据来源优先级：Sina > Tencent（备选）
    """

    # ----------------------------------------------------------------
    #  常见A股代码 ↔ 中文名映射表（沪深主板常见股）
    # ----------------------------------------------------------------
    COMMON_STOCKS: Dict[str, str] = {
        # 上证主板
        '600519': '贵州茅台', '600036': '招商银行', '601318': '中国平安',
        '600900': '长江电力', '600276': '恒瑞医药', '600887': '伊利股份',
        '600030': '中信证券', '600585': '海螺水泥', '600690': '海尔智家',
        '600104': '上汽集团', '600309': '万华化学', '600028': '中国石化',
        '600941': '中国移动', '600809': '山西汾酒', '600438': '通威股份',
        '601166': '兴业银行', '601328': '交通银行', '601398': '工商银行',
        '601288': '农业银行', '601939': '建设银行', '601857': '中国石油',
        '601988': '中国银行', '601012': '隆基绿能', '603259': '药明康德',
        '601899': '紫金矿业', '600031': '三一重工', '600000': '浦发银行',
        '601888': '中国中免', '600196': '复星医药', '600703': '三安光电',
        # 深证主板
        '000001': '平安银行', '000002': '万科A', '000333': '美的集团',
        '000858': '五粮液', '000651': '格力电器', '000725': '京东方A',
        '002415': '海康威视', '002714': '牧原股份', '002475': '立讯精密',
        '000568': '泸州老窖', '002304': '洋河股份', '000776': '广发证券',
        '000063': '中兴通讯', '002352': '顺丰控股', '300750': '宁德时代',
        '300059': '东方财富', '300124': '汇川技术', '300015': '爱尔眼科',
        '300274': '阳光电源', '300760': '迈瑞医疗', '300782': '卓胜微',
        '002230': '科大讯飞', '002371': '北方华创', '000100': 'TCL科技',
    }

    # 自动生成反向映射
    NAME_TO_CODE: Dict[str, str] = {v: k for k, v in COMMON_STOCKS.items()}

    # ----------------------------------------------------------------
    #  市场标识映射
    # ----------------------------------------------------------------
    MARKET_PREFIX: Dict[str, str] = {
        '6': 'sh',      # 上证 600/601/603/605
        '9': 'sh',      # 上证 900 (B股)
        '0': 'sz',      # 深证 000/001/002/003
        '2': 'sz',      # 深证 200/300 (创业板)
        '3': 'sz',      # 创业板 300/301
        '4': 'sh',      # 科创板 688 (实际是 sh)
        '8': 'sh',      # 科创板 789
    }

    # 分钟周期 → akshare period 参数映射
    MINUTE_PERIOD_MAP: Dict[str, str] = {
        '1m': '1', '5m': '5', '15m': '15', '30m': '30', '60m': '60',
        '1H': '60',  # 1小时 = 60分钟
    }
    # 分钟周期中文标签
    MINUTE_PERIOD_LABELS: Dict[str, str] = {
        '1m': '1分钟', '5m': '5分钟', '15m': '15分钟',
        '30m': '30分钟', '60m': '60分钟',
    }

    # 日线周期支持
    DAILY_PERIODS = ['daily', 'weekly', 'monthly']

    # ----------------------------------------------------------------
    #  市场板块常量
    # ----------------------------------------------------------------

    # 同花顺行业板块列表缓存
    _INDUSTRY_BOARD_CACHE: Optional[pd.DataFrame] = None
    _CONCEPT_BOARD_CACHE: Optional[pd.DataFrame] = None
    _CONCEPT_BOARD_CACHE_TIME: Optional[float] = None

    # 市场标识 → 中文名
    EXCHANGE_NAMES: Dict[str, str] = {
        'sh': '上海证券交易所',
        'sz': '深圳证券交易所',
        'bj': '北京证券交易所',
    }

    # ----------------------------------------------------------------
    #  辅助方法
    # ----------------------------------------------------------------

    @classmethod
    def _get_market_symbol(cls, code: str) -> str:
        """将纯数字代码转为带市场标识的完整代码

        Parameters
        ----------
        code : str
            股票代码，如 '600519', '000001', '300750'

        Returns
        -------
        str
            带市场前缀的代码，如 'sh600519', 'sz000001'
        """
        code = code.strip()
        first_digit = code[0]
        prefix = cls.MARKET_PREFIX.get(first_digit, 'sh')
        return f"{prefix}{code}"

    @classmethod
    def code_to_name(cls, code: str) -> str:
        """股票代码 → 中文名称"""
        return cls.COMMON_STOCKS.get(code.strip(), code)

    @classmethod
    def name_to_code(cls, name: str) -> str:
        """中文名称 → 股票代码"""
        return cls.NAME_TO_CODE.get(name, name)

    # ----------------------------------------------------------------
    #  日线数据接口
    # ----------------------------------------------------------------

    @classmethod
    def fetch_daily(cls, symbol: str,
                    start_date: Optional[str] = None,
                    end_date: Optional[str] = None,
                    adjust: str = 'qfq') -> pd.DataFrame:
        """获取A股日线历史数据

        优先使用新浪数据源 (stock_zh_a_daily)，若失败则回退到腾讯数据源。

        Parameters
        ----------
        symbol : str
            股票代码，支持多种格式：
            - 纯数字：'600519', '000001', '300750'
            - 带前缀：'sh600519', 'sz000001'
        start_date : str, optional
            起始日期 'YYYYMMDD' 或 'YYYY-MM-DD'，默认1年前
        end_date : str, optional
            结束日期 'YYYYMMDD' 或 'YYYY-MM-DD'，默认今日
        adjust : str
            复权类型：'qfq'(前复权), 'hfq'(后复权), ''(不复权)
            默认 'qfq'

        Returns
        -------
        pd.DataFrame
            columns: ['date', 'open', 'high', 'low', 'close', 'volume',
                      'amount', 'outstanding_share', 'turnover']
        """
        try:
            import akshare as ak
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")

        # 标准化代码格式
        full_symbol = cls._get_market_symbol(symbol)

        if start_date is None:
            start_date = (datetime.date.today() -
                          datetime.timedelta(days=365)).strftime('%Y%m%d')
        if end_date is None:
            end_date = datetime.date.today().strftime('%Y%m%d')

        # 统一日期格式为 YYYYMMDD
        start_clean = start_date.replace('-', '')
        end_clean = end_date.replace('-', '')

        # 尝试新浪数据源
        try:
            df = ak.stock_zh_a_daily(
                symbol=full_symbol,
                start_date=start_clean,
                end_date=end_clean,
                adjust=adjust,
            )
            if df is not None and not df.empty:
                df = cls._normalize_sina_daily(df)
                logger.info("[%s] 新浪日线获取成功: %d 条, %s ~ %s",
                            symbol, len(df), df['date'].min(), df['date'].max())
                return df
        except Exception as e:
            logger.warning("[%s] 新浪日线失败: %s，尝试腾讯数据源", symbol, e)

        # 回退到腾讯数据源
        try:
            df = ak.stock_zh_a_hist_tx(
                symbol=full_symbol,
                start_date=start_clean,
                end_date=end_clean,
                adjust=adjust,
            )
            if df is not None and not df.empty:
                df = cls._normalize_tx_daily(df)
                logger.info("[%s] 腾讯日线获取成功: %d 条, %s ~ %s",
                            symbol, len(df), df['date'].min(), df['date'].max())
                return df
        except Exception as e:
            logger.warning("[%s] 腾讯日线也失败: %s", symbol, e)

        raise AkshareAPIError(
            "股票 [%s] 所有数据源均无法获取日线数据" % symbol)

    # ----------------------------------------------------------------
    #  分钟K线数据接口
    # ----------------------------------------------------------------

    @classmethod
    def fetch_minute_kline(cls, symbol: str,
                           period: str = '5m',
                           start_date: Optional[str] = None,
                           end_date: Optional[str] = None,
                           adjust: str = 'qfq') -> pd.DataFrame:
        """获取A股分钟K线数据（新浪数据源）

        Parameters
        ----------
        symbol : str
            股票代码，支持多种格式
        period : str
            分钟周期：'1m', '5m', '15m', '30m', '60m'
        start_date : str, optional
            开始过滤日期 'YYYY-MM-DD HH:MM:SS' 或 'YYYY-MM-DD'
        end_date : str, optional
            结束过滤日期
        adjust : str
            复权类型：'qfq'(前复权), 'hfq'(后复权), ''(不复权)

        Returns
        -------
        pd.DataFrame
            columns: ['date', 'open', 'high', 'low', 'close',
                      'volume', 'amount']
        """
        try:
            import akshare as ak
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")

        ak_period = cls.MINUTE_PERIOD_MAP.get(period)
        if ak_period is None:
            raise ValueError(
                f"不支持的分钟周期: {period}，可选: {list(cls.MINUTE_PERIOD_MAP.keys())}")

        full_symbol = cls._get_market_symbol(symbol)

        try:
            df = ak.stock_zh_a_minute(
                symbol=full_symbol,
                period=ak_period,
                adjust=adjust,
            )
        except Exception as e:
            raise AkshareAPIError(
                "获取 [%s] %s分钟K线失败: %s" % (symbol, period, str(e)))

        if df is None or df.empty:
            raise AkshareAPIError("合约 [%s] %s分钟K线返回空数据" % (symbol, period))

        # 标准化列名
        df = cls._normalize_sina_minute(df)

        # 日期过滤（可选）
        if start_date:
            start_dt = pd.to_datetime(start_date)
            df = df[df['date'] >= start_dt].copy()
        if end_date:
            end_dt = pd.to_datetime(end_date) + pd.Timedelta(days=1)
            df = df[df['date'] < end_dt].copy()

        if df.empty:
            logger.warning("[%s] %s 分钟K线日期过滤后无数据", symbol, period)

        logger.info("[%s] 成功获取 %s 分钟K线: %d 条",
                    symbol, period, len(df))
        return df

    # ----------------------------------------------------------------
    #  周线/月线数据（通过日线 resample）
    # ----------------------------------------------------------------

    @classmethod
    def fetch_daily_resampled(cls, symbol: str,
                              period: str = 'weekly',
                              start_date: Optional[str] = None,
                              end_date: Optional[str] = None,
                              adjust: str = 'qfq') -> pd.DataFrame:
        """获取周线/月线数据（基于日线数据重采样）

        Parameters
        ----------
        symbol : str
            股票代码
        period : str
            'weekly' 或 'monthly'
        start_date, end_date, adjust : 同 fetch_daily

        Returns
        -------
        pd.DataFrame
        """
        if period not in ['weekly', 'monthly']:
            raise ValueError(f"不支持的周期: {period}，仅支持 weekly/monthly")

        df = cls.fetch_daily(symbol, start_date, end_date, adjust)
        if df.empty:
            return df

        df = df.set_index('date')
        rule = 'W' if period == 'weekly' else 'M'
        resampled = df.resample(rule).agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum',
            'amount': 'sum',
        }).dropna()
        resampled = resampled.reset_index()
        return resampled

    # ----------------------------------------------------------------
    #  实时行情（简易）
    # ----------------------------------------------------------------

    @classmethod
    def fetch_realtime(cls, symbol: str) -> Optional[Dict]:
        """获取股票实时行情快照（简易版）

        通过 stock_zh_a_spot_em 获取实时快照数据。
        注意：此接口依赖 East Money，可能因网络原因失败。

        Parameters
        ----------
        symbol : str
            股票代码

        Returns
        -------
        dict or None
            包含 price, open, high, low, volume, amount 等字段
        """
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return None
            # 查找匹配的股票
            code = symbol.strip()
            match = df[df['代码'] == code]
            if match.empty:
                return None
            row = match.iloc[0]
            return {
                "symbol": code,
                "name": row.get('名称', ''),
                "price": float(row.get('最新价', 0)),
                "open": float(row.get('今开', 0)),
                "high": float(row.get('最高', 0)),
                "low": float(row.get('最低', 0)),
                "close": float(row.get('昨收', 0)),
                "volume": float(row.get('成交量', 0)),
                "amount": float(row.get('成交额', 0)),
                "change_pct": float(row.get('涨跌幅', 0)),
            }
        except Exception as e:
            logger.warning("[%s] 获取实时行情失败: %s", symbol, e)
            return None

    # ----------------------------------------------------------------
    #  批量数据获取
    # ----------------------------------------------------------------

    @classmethod
    def fetch_all_daily(cls, symbols: List[str],
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None,
                        adjust: str = 'qfq') -> Dict[str, pd.DataFrame]:
        """批量获取多只股票的日线数据

        Parameters
        ----------
        symbols : list of str
            股票代码列表
        start_date, end_date, adjust : 同 fetch_daily

        Returns
        -------
        dict
            {symbol: dataframe} 的字典，失败的数据会有警告
        """
        results = {}
        for sym in symbols:
            try:
                df = cls.fetch_daily(sym, start_date, end_date, adjust)
                results[sym] = df
                logger.info("[%s] 获取成功: %d 条", sym, len(df))
            except (AkshareAPIError, Exception) as e:
                logger.warning("[%s] 获取失败: %s", sym, e)
        return results

    # ================================================================
    #  市场数据 — 股票列表
    # ================================================================

    @classmethod
    def get_all_stock_codes(cls) -> pd.DataFrame:
        """获取A股全市场股票代码和名称列表

        Returns
        -------
        pd.DataFrame
            columns: ['code', 'name']，共约 5500+ 只股票
        """
        try:
            import akshare as ak
            df = ak.stock_info_a_code_name()
            logger.info("全市场股票列表获取成功: %d 只", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取全市场股票列表失败: %s" % e)

    @classmethod
    def get_stock_list_by_market(cls, market: str = 'sh') -> pd.DataFrame:
        """获取指定交易所的股票列表

        Parameters
        ----------
        market : str
            'sh' 上交所, 'sz' 深交所, 'bj' 北交所

        Returns
        -------
        pd.DataFrame
            sh: ['证券代码', '证券简称', '证券全称', '公司简称', '公司全称', '上市日期']
            sz: ['板块', 'A股代码', 'A股简称', 'A股上市日期', 'A股总股本', 'A股流通股本', '所属行业']
            bj: ['证券代码', '证券简称', '总股本', '流通股本', '上市日期', '所属行业', '地区', '报告日期']
        """
        try:
            import akshare as ak
            if market == 'sh':
                df = ak.stock_info_sh_name_code()
            elif market == 'sz':
                df = ak.stock_info_sz_name_code()
            elif market == 'bj':
                df = ak.stock_info_bj_name_code()
            else:
                raise ValueError(f"不支持的市场: {market}，可选: sh/sz/bj")
            logger.info("%s 股票列表获取成功: %d 只",
                        cls.EXCHANGE_NAMES.get(market, market), len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取 %s 股票列表失败: %s" % (market, e))

    # ================================================================
    #  板块数据 — 行业/概念分类
    # ================================================================

    @classmethod
    def get_industry_board_ths(cls, use_cache: bool = True) -> pd.DataFrame:
        """获取同花顺行业板块分类（约 90 个行业）

        Parameters
        ----------
        use_cache : bool
            是否使用缓存（默认 True，缓存有效期 1 小时）

        Returns
        -------
        pd.DataFrame
            columns: ['name', 'code']，如 ['白酒', '881273']
        """
        if use_cache and cls._INDUSTRY_BOARD_CACHE is not None:
            return cls._INDUSTRY_BOARD_CACHE
        try:
            import akshare as ak
            df = ak.stock_board_industry_name_ths()
            cls._INDUSTRY_BOARD_CACHE = df
            logger.info("行业板块分类获取成功: %d 个行业", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取行业板块分类失败: %s" % e)

    @classmethod
    def get_concept_board_ths(cls, use_cache: bool = True) -> pd.DataFrame:
        """获取同花顺概念板块分类（约 375 个概念）

        Parameters
        ----------
        use_cache : bool
            是否使用缓存

        Returns
        -------
        pd.DataFrame
            columns: ['name', 'code']，如 ['人工智能', '309121']
        """
        if use_cache and cls._CONCEPT_BOARD_CACHE is not None:
            return cls._CONCEPT_BOARD_CACHE
        try:
            import akshare as ak
            df = ak.stock_board_concept_name_ths()
            cls._CONCEPT_BOARD_CACHE = df
            logger.info("概念板块分类获取成功: %d 个概念", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取概念板块分类失败: %s" % e)

    @classmethod
    def get_sector_spot(cls, indicator: str = '新浪行业') -> pd.DataFrame:
        """获取行业板块实时行情

        Parameters
        ----------
        indicator : str
            板块类型，如 '新浪行业', '证监会行业' 等

        Returns
        -------
        pd.DataFrame
            columns: ['label', '板块', '公司家数', '平均价格', '涨跌额', '涨跌幅',
                       '总成交量', '总成交额', ...]
        """
        try:
            import akshare as ak
            df = ak.stock_sector_spot(indicator=indicator)
            logger.info("行业板块行情获取成功: %d 个板块", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取行业板块行情失败: %s" % e)

    # ================================================================
    #  基础信息 — 个股资料
    # ================================================================

    @classmethod
    def get_stock_info(cls, symbol: str) -> Dict:
        """获取股票基本信息（东方财富）

        Parameters
        ----------
        symbol : str
            股票代码，如 '600519'

        Returns
        -------
        dict
            {'最新价', '股票代码', '股票简称', '总股本', '流通股',
             '总市值', '流通市值', '行业', '上市时间'}
        """
        try:
            import akshare as ak
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")

        last_error = None
        for attempt in range(2):  # 最多重试1次
            try:
                df = ak.stock_individual_info_em(symbol=symbol)
                result = {}
                for _, row in df.iterrows():
                    result[row['item']] = row['value']
                logger.info("[%s] 股票基本信息获取成功", symbol)
                return result
            except Exception as e:
                last_error = e
                if attempt == 0:
                    time.sleep(1.5)
                    continue

        # 降级：尝试通过公司资料构建部分信息
        logger.warning("[%s] 东方财富信息接口失败，尝试巨潮资讯降级: %s", symbol, last_error)
        try:
            profile = ak.stock_profile_cninfo(symbol=symbol)
            row = profile.iloc[0]
            # 从全市场列表获取股票简称
            all_stocks = ak.stock_info_a_code_name()
            name_match = all_stocks[all_stocks['code'] == symbol]
            short_name = name_match.iloc[0]['name'] if not name_match.empty else symbol
            result = {
                '股票代码': symbol,
                '股票简称': short_name,
                '公司名称': row.get('公司名称', ''),
                '所属行业': row.get('所属行业', ''),
                '上市时间': row.get('上市日期', ''),
                '法人代表': row.get('法人代表', ''),
                '注册资金': row.get('注册资金', ''),
                '主营业务': str(row.get('主营业务', ''))[:100],
            }
            logger.info("[%s] 降级获取基本信息成功", symbol)
            return result
        except Exception as fallback_err:
            raise AkshareAPIError(
                "获取 [%s] 基本信息失败(主: %s, 备: %s)" % (
                    symbol, last_error, fallback_err))

    @classmethod
    def get_company_profile(cls, symbol: str) -> pd.DataFrame:
        """获取公司详细资料（巨潮资讯）

        Parameters
        ----------
        symbol : str
            股票代码

        Returns
        -------
        pd.DataFrame
            包含公司名称、英文名称、曾用简称、所属行业、上市日期、
            法人代表、注册资金、成立日期、主营业务等 26 个字段
        """
        try:
            import akshare as ak
            df = ak.stock_profile_cninfo(symbol=symbol)
            logger.info("[%s] 公司详细资料获取成功", symbol)
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取 [%s] 公司资料失败: %s" % (symbol, e))

    # ================================================================
    #  分红送转 / 股本变动
    # ================================================================

    @classmethod
    def get_share_change(cls, symbol: str,
                         start_date: Optional[str] = None,
                         end_date: Optional[str] = None) -> pd.DataFrame:
        """获取历史股本变动/分红送转信息（巨潮资讯）

        Parameters
        ----------
        symbol : str
            股票代码
        start_date : str, optional
            起始日期 YYYYMMDD，默认 10 年前
        end_date : str, optional
            结束日期 YYYYMMDD，默认今日

        Returns
        -------
        pd.DataFrame
            包含总股本、流通股本、变动原因、变动日期、公告日期等 44 列
        """
        if start_date is None:
            start_date = (datetime.date.today() -
                          datetime.timedelta(days=3650)).strftime('%Y%m%d')
        if end_date is None:
            end_date = datetime.date.today().strftime('%Y%m%d')
        try:
            import akshare as ak
            df = ak.stock_share_change_cninfo(
                symbol=symbol, start_date=start_date, end_date=end_date)
            logger.info("[%s] 股本变动获取成功: %d 条", symbol, len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取 [%s] 股本变动失败: %s" % (symbol, e))

    # ================================================================
    #  财务指标数据
    # ================================================================

    @classmethod
    def get_financial_report(cls, date: Optional[str] = None) -> pd.DataFrame:
        """获取财务指标数据（东方财富）

        包含每股收益、营业收入、净利润、净资产收益率、
        每股经营现金流量、销售毛利率等关键财务指标。

        Parameters
        ----------
        date : str, optional
            财报截止日期 YYYYMMDD，如 '20251231'（年报）、'20250930'（三季报）
            默认最近一期

        Returns
        -------
        pd.DataFrame
            包含 16 列：序号、股票代码、股票简称、每股收益、营业总收入、
            营业总收入-同比增长、净利润、净利润-同比增长、每股净资产、
            净资产收益率、销售毛利率等
        """
        if date is None:
            date = datetime.date.today().strftime('%Y') + '1231'
        try:
            import akshare as ak
            df = ak.stock_yjbb_em(date=date)
            logger.info("财务指标获取成功: %d 条", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取财务指标失败: %s" % e)

    # ================================================================
    #  资金流向数据
    # ================================================================

    @classmethod
    def get_individual_fund_flow(cls, symbol: str,
                                 market: str = 'sh') -> pd.DataFrame:
        """获取个股资金流向历史数据（东方财富）

        包含主力净流入、超大单、大单、中单、小单的资金流向。

        Parameters
        ----------
        symbol : str
            股票代码
        market : str
            'sh' 上海, 'sz' 深圳（根据股票代码首字母判断：6→sh, 0/3→sz）

        Returns
        -------
        pd.DataFrame
            columns: ['日期', '收盘价', '涨跌幅', '主力净流入-净额',
                      '主力净流入-净占比', '超大单净流入-净额',
                      '超大单净流入-净占比', '大单净流入-净额',
                      '大单净流入-净占比', ...]
        """
        # 自动识别市场
        code = symbol.strip()
        if market is None or market not in ['sh', 'sz']:
            first_digit = code[0]
            market = 'sh' if first_digit in ('6', '9', '4', '8') else 'sz'
        try:
            import akshare as ak
            df = ak.stock_individual_fund_flow(stock=code, market=market)
            logger.info("[%s] 资金流向获取成功: %d 条", symbol, len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取 [%s] 资金流向失败: %s" % (symbol, e))

    # ================================================================
    #  龙虎榜数据
    # ================================================================

    @classmethod
    def get_lhb_detail(cls, start_date: str = None,
                       end_date: str = None) -> pd.DataFrame:
        """获取龙虎榜每日明细（东方财富）

        Parameters
        ----------
        start_date : str, optional
            开始日期 YYYYMMDD，默认 7 天前
        end_date : str, optional
            结束日期 YYYYMMDD，默认今日

        Returns
        -------
        pd.DataFrame
            columns: ['序号', '代码', '名称', '上榜日', '解读', '收盘价',
                      '涨跌幅', '龙虎榜净买额', '龙虎榜买入额', '龙虎榜卖出额',
                      '换手率', '流通市值', '上榜原因', '上榜后1日', ...]
        """
        if start_date is None:
            start_date = (datetime.date.today() -
                          datetime.timedelta(days=7)).strftime('%Y%m%d')
        if end_date is None:
            end_date = datetime.date.today().strftime('%Y%m%d')
        try:
            import akshare as ak
            df = ak.stock_lhb_detail_em(start_date=start_date,
                                        end_date=end_date)
            logger.info("龙虎榜明细获取成功: %d 条", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取龙虎榜明细失败: %s" % e)

    @classmethod
    def get_lhb_trader_detail(cls, start_date: str = None,
                               end_date: str = None) -> pd.DataFrame:
        """获取龙虎榜营业部排行（东方财富）

        Parameters
        ----------
        start_date : str, optional
            开始日期 YYYYMMDD
        end_date : str, optional
            结束日期 YYYYMMDD

        Returns
        -------
        pd.DataFrame
            营业部上榜排名详情
        """
        if start_date is None:
            start_date = (datetime.date.today() -
                          datetime.timedelta(days=7)).strftime('%Y%m%d')
        if end_date is None:
            end_date = datetime.date.today().strftime('%Y%m%d')
        try:
            import akshare as ak
            df = ak.stock_lhb_hyyyb_em(start_date=start_date,
                                        end_date=end_date)
            logger.info("龙虎榜营业部详情获取成功: %d 条", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取龙虎榜营业部详情失败: %s" % e)

    # ================================================================
    #  融资融券数据
    # ================================================================

    @classmethod
    def get_margin_account_statistics(cls) -> pd.DataFrame:
        """获取全市场融资融券账户统计

        Returns
        -------
        pd.DataFrame
            columns: ['日期', '融资余额', '融券余额', '融资买入额', '融券卖出额',
                      '证券公司数量', '营业部数量', '个人投资者数量', '机构投资者数量', ...]
        """
        try:
            import akshare as ak
            df = ak.stock_margin_account_info()
            logger.info("融资融券账户统计获取成功: %d 条", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取融资融券账户统计失败: %s" % e)

    @classmethod
    def get_margin_detail_szse(cls, date: str = None) -> pd.DataFrame:
        """获取深交所融资融券明细

        Parameters
        ----------
        date : str, optional
            日期 YYYYMMDD，默认今日

        Returns
        -------
        pd.DataFrame
            columns: ['证券代码', '证券简称', '融资买入额', '融资余额',
                      '融券卖出量', '融券余量', '融券余额', '融资融券余额']
        """
        if date is None:
            date = datetime.date.today().strftime('%Y%m%d')
        try:
            import akshare as ak
            df = ak.stock_margin_detail_szse(date=date)
            logger.info("深交所融资融券明细获取成功: %d 条", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取深交所融资融券明细失败: %s" % e)

    # ================================================================
    #  股东户数
    # ================================================================

    @classmethod
    def get_shareholder_count_detail(cls, symbol: str) -> pd.DataFrame:
        """获取个股股东户数变化历史

        Parameters
        ----------
        symbol : str
            股票代码

        Returns
        -------
        pd.DataFrame
            columns: ['股东户数统计截止日', '区间涨跌幅', '股东户数-本次',
                      '股东户数-上次', '股东户数-增减', '股东户数-增减比例',
                      '户均持股市值', '户均持股数量', '总市值', '总股本', '代码', '名称']
        """
        try:
            import akshare as ak
            df = ak.stock_zh_a_gdhs_detail_em(symbol=symbol)
            logger.info("[%s] 股东户数变化获取成功: %d 条", symbol, len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取 [%s] 股东户数失败: %s" % (symbol, e))

    @classmethod
    def get_shareholder_count_batch(cls, date: str = None) -> pd.DataFrame:
        """获取全市场股东户数快照（指定季度末）

        Parameters
        ----------
        date : str, optional
            季度末日期 YYYYMMDD，如 '20250331', '20251231'
            默认最近一季度

        Returns
        -------
        pd.DataFrame
            columns: ['代码', '名称', '最新价', '涨跌幅', '股东户数-本次',
                      '股东户数-上次', '股东户数-增减', '股东户数-增减比例', ...]
        """
        if date is None:
            today = datetime.date.today()
            month = today.month
            # 获取最近季度末
            if month < 4:
                date = '%s1231' % (today.year - 1)
            elif month < 7:
                date = '%s0331' % today.year
            elif month < 10:
                date = '%s0630' % today.year
            else:
                date = '%s0930' % today.year
        try:
            import akshare as ak
            df = ak.stock_zh_a_gdhs(symbol=date)
            logger.info("全市场股东户数获取成功: %d 条", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取全市场股东户数失败: %s" % e)

    # ================================================================
    #  机构研报
    # ================================================================

    @classmethod
    def get_research_reports(cls, symbol: str) -> pd.DataFrame:
        """获取个股机构研究报告（东方财富）

        Parameters
        ----------
        symbol : str
            股票代码

        Returns
        -------
        pd.DataFrame
            columns: ['序号', '股票代码', '股票简称', '报告名称', '东财评级',
                      '机构', '近一月个股研报数', '行业', '日期', '报告PDF链接']
        """
        try:
            import akshare as ak
            df = ak.stock_research_report_em(symbol=symbol)
            logger.info("[%s] 研报获取成功: %d 条", symbol, len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取 [%s] 研报失败: %s" % (symbol, e))

    # ================================================================
    #  业绩预告 / 业绩快报
    # ================================================================

    @classmethod
    def get_profit_forecast(cls, date: str = None) -> pd.DataFrame:
        """获取业绩预告数据（东方财富）

        Parameters
        ----------
        date : str, optional
            公告截止日期 YYYYMMDD，默认最近一期

        Returns
        -------
        pd.DataFrame
            columns: ['序号', '股票代码', '股票简称', '预测指标', '业绩变动',
                      '预测数值', '业绩变动幅度', '业绩变动原因', '预告类型',
                      '上年同期值', '公告日期']
        """
        if date is None:
            date = datetime.date.today().strftime('%Y') + '1231'
        try:
            import akshare as ak
            df = ak.stock_yjyg_em(date=date)
            logger.info("业绩预告获取成功: %d 条", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取业绩预告失败: %s" % e)

    @classmethod
    def get_profit_express(cls, date: str = None) -> pd.DataFrame:
        """获取业绩快报数据（东方财富）

        Parameters
        ----------
        date : str, optional
            公告截止日期 YYYYMMDD，默认最近一期

        Returns
        -------
        pd.DataFrame
            columns: ['序号', '股票代码', '股票简称', '每股收益', '营业收入',
                      '营业收入-同比增长', '净利润-净利润', '净利润-同比增长',
                      '每股净资产', '净资产收益率', '所处行业', '公告日期']
        """
        if date is None:
            date = datetime.date.today().strftime('%Y') + '1231'
        try:
            import akshare as ak
            df = ak.stock_yjkb_em(date=date)
            logger.info("业绩快报获取成功: %d 条", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取业绩快报失败: %s" % e)

    # ================================================================
    #  新股发行信息
    # ================================================================

    @classmethod
    def get_new_ipo_list(cls) -> pd.DataFrame:
        """获取新股发行一览（巨潮资讯）

        Returns
        -------
        pd.DataFrame
            columns: ['证劵代码', '证券简称', '上市日期', '申购日期', '发行价',
                      '总发行数量', '发行市盈率', '上网发行中签率',
                      '摇号结果公告日', '中签公告日', '中签缴款日',
                      '网上申购上限', '上网发行数量']
        """
        try:
            import akshare as ak
            df = ak.stock_new_ipo_cninfo()
            logger.info("新股发行信息获取成功: %d 条", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取新股发行信息失败: %s" % e)

    @classmethod
    def get_ipo_registration(cls, market: str = 'sh') -> pd.DataFrame:
        """获取IPO排队企业列表

        Parameters
        ----------
        market : str
            'sh' 上交所, 'sz' 深交所

        Returns
        -------
        pd.DataFrame
            包含企业名称、最新状态、注册地、行业、保荐机构等
        """
        try:
            import akshare as ak
            if market == 'sh':
                df = ak.stock_register_sh()
            elif market == 'sz':
                df = ak.stock_register_sz()
            else:
                raise ValueError(f"不支持: {market}，仅支持 sh/sz")
            logger.info("%s IPO排队列表获取成功: %d 条",
                        cls.EXCHANGE_NAMES.get(market, market), len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取IPO排队列表失败: %s" % e)

    # ----------------------------------------------------------------
    #  列名标准化
    # ----------------------------------------------------------------

    @staticmethod
    def _normalize_sina_daily(df: pd.DataFrame) -> pd.DataFrame:
        """标准化 stock_zh_a_daily 的列名为英文

        新浪返回列: ['date', 'open', 'high', 'low', 'close',
                      'volume', 'amount', 'outstanding_share', 'turnover']
        """
        col_map = {
            'date': 'date', 'open': 'open', 'high': 'high',
            'low': 'low', 'close': 'close', 'volume': 'volume',
            'amount': 'amount',
        }
        rename = {k: v for k, v in col_map.items() if k in df.columns}
        df = df.rename(columns=rename)

        # 确保 date 为 datetime
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date'])
            df = df.sort_values('date').reset_index(drop=True)

        # 数值列转换
        numeric_cols = ['open', 'high', 'low', 'close',
                        'volume', 'amount']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        return df

    @staticmethod
    def _normalize_tx_daily(df: pd.DataFrame) -> pd.DataFrame:
        """标准化 stock_zh_a_hist_tx 的列名为英文

        腾讯返回列: ['date', 'open', 'close', 'high', 'low', 'amount']
        注意：腾讯数据缺少 volume 字段，会通过 amount/avg_price 估算
        """
        col_map = {
            'date': 'date', 'open': 'open', 'close': 'close',
            'high': 'high', 'low': 'low', 'amount': 'amount',
        }
        rename = {k: v for k, v in col_map.items() if k in df.columns}
        df = df.rename(columns=rename)

        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date'])
            df = df.sort_values('date').reset_index(drop=True)

        # 数值列转换
        numeric_cols = ['open', 'high', 'low', 'close', 'amount']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 估算成交量（amount 单位为元，除以均价估算手数）
        if 'volume' not in df.columns and 'amount' in df.columns:
            avg_price = (df['open'] + df['high'] + df['low'] + df['close']) / 4
            df['volume'] = (df['amount'] / avg_price.replace(0, float('nan'))
                            ).round(0).fillna(0).astype(int)

        return df

    @staticmethod
    def _normalize_sina_minute(df: pd.DataFrame) -> pd.DataFrame:
        """标准化 stock_zh_a_minute 的列名为英文

        新浪分钟返回列: ['day', 'open', 'high', 'low', 'close',
                          'volume', 'amount']
        """
        col_map = {
            'day': 'date', 'open': 'open', 'high': 'high',
            'low': 'low', 'close': 'close', 'volume': 'volume',
            'amount': 'amount',
        }
        rename = {k: v for k, v in col_map.items() if k in df.columns}
        df = df.rename(columns=rename)

        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date'])
            df = df.sort_values('date').reset_index(drop=True)

        numeric_cols = ['open', 'high', 'low', 'close',
                        'volume', 'amount']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        return df


# ----------------------------------------------------------------
#  命令行快速测试
# ----------------------------------------------------------------
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(message)s'
    )

    api = AkshareStockAPI()
    print("=" * 70)
    print("  AkshareStockAPI - 统一股票数据接口 (扩展版)")
    print("=" * 70)

    # 1. 映射测试
    print("\n📋 映射测试:")
    for code in ['600519', '000001', '300750', '601318']:
        name = api.code_to_name(code)
        back_code = api.name_to_code(name)
        print("    %-6s → %-10s → %s" % (code, name, back_code))

    # 2. 日线数据测试
    for sym in ['600519', '000001']:
        try:
            df = api.fetch_daily(sym, '20250501', '20250520', adjust='qfq')
            print(f"\n📊 {sym}({api.code_to_name(sym)}) 日线:")
            for _, r in df.tail(3).iterrows():
                print("    %s  O:%.2f  H:%.2f  L:%.2f  C:%.2f  V:%d" %
                      (r['date'].strftime('%Y-%m-%d'),
                       r['open'], r['high'], r['low'], r['close'],
                       int(r['volume'])))
        except AkshareAPIError as e:
            print(f"\n    ❌ {sym}: {e}")

    # 3. 分钟K线测试
    try:
        df = api.fetch_minute_kline('600519', '5m', adjust='qfq')
        print(f"\n📊 600519 5分钟K线: {len(df)} 条")
        for _, r in df.tail(3).iterrows():
            print("    %s  O:%.2f  H:%.2f  L:%.2f  C:%.2f  V:%d" %
                  (r['date'].strftime('%Y-%m-%d %H:%M'),
                   r['open'], r['high'], r['low'], r['close'],
                   int(r['volume'])))
    except AkshareAPIError as e:
        print(f"\n    ❌ 分钟K线: {e}")

    # 4. 市场数据 — 股票列表
    print("\n" + "=" * 70)
    print("  📋 市场数据 — 股票列表")
    print("=" * 70)
    try:
        df = api.get_all_stock_codes()
        print(f"    A股全市场: {len(df)} 只股票")
        print(f"    示例: {df.head(3).to_string(index=False)}")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")
    for m in ['sh', 'sz', 'bj']:
        try:
            df = api.get_stock_list_by_market(m)
            print(f"    {api.EXCHANGE_NAMES.get(m, m)}: {len(df)} 只")
        except AkshareAPIError as e:
            print(f"    ❌ {m}: {e}")

    # 5. 板块数据
    print("\n" + "=" * 70)
    print("  📋 板块数据")
    print("=" * 70)
    try:
        df = api.get_industry_board_ths()
        print(f"    同花顺行业板块: {len(df)} 个")
        print(f"    前5: {df['name'].head(5).tolist()}")
    except AkshareAPIError as e:
        print(f"    ❌ 行业板块: {e}")
    try:
        df = api.get_concept_board_ths()
        print(f"    同花顺概念板块: {len(df)} 个")
        print(f"    前5: {df['name'].head(5).tolist()}")
    except AkshareAPIError as e:
        print(f"    ❌ 概念板块: {e}")
    try:
        df = api.get_sector_spot()
        print(f"    新浪行业板块行情: {len(df)} 个")
    except AkshareAPIError as e:
        print(f"    ❌ 板块行情: {e}")

    # 6. 基础信息
    print("\n" + "=" * 70)
    print("  📋 基础信息 — 贵州茅台")
    print("=" * 70)
    try:
        info = api.get_stock_info('600519')
        print("    股票基本信息:")
        for k, v in info.items():
            print(f"      {k}: {v}")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")
    try:
        df = api.get_company_profile('600519')
        row = df.iloc[0]
        print(f"    公司全称: {row.get('公司名称', '')}")
        print(f"    上市日期: {row.get('上市日期', '')}")
        print(f"    所属行业: {row.get('所属行业', '')}")
        print(f"    主营业务: {str(row.get('主营业务', ''))[:60]}...")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")

    # 7. 分红送转
    print("\n" + "=" * 70)
    print("  📋 分红送转 — 贵州茅台 (近3年)")
    print("=" * 70)
    try:
        df = api.get_share_change('600519',
                                   start_date='20230101',
                                   end_date='20260520')
        print(f"    获取到 {len(df)} 条股本变动记录")
        if not df.empty:
            cols = ['变动日期', '变动原因', '总股本', '已流通股份']
            avail = [c for c in cols if c in df.columns]
            print(f"    字段: {avail}")
            for _, r in df.tail(3).iterrows():
                print(f"      {r.get('变动日期', '')}  {r.get('变动原因', '')}")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")

    # 8. 财务指标
    print("\n" + "=" * 70)
    print("  📋 财务指标 (2025年报)")
    print("=" * 70)
    try:
        df = api.get_financial_report(date='20251231')
        print(f"    获取到 {len(df)} 条")
        cols = ['股票代码', '股票简称', '每股收益', '净利润-净利润', '净资产收益率']
        avail = [c for c in cols if c in df.columns]
        print(f"    前3条 (部分列):")
        for _, r in df.head(3).iterrows():
            vals = [str(r.get(c, ''))[:15] for c in avail]
            print(f"      {' | '.join(vals)}")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")

    # 9. 资金流向
    print("\n" + "=" * 70)
    print("  📋 资金流向 — 贵州茅台")
    print("=" * 70)
    try:
        df = api.get_individual_fund_flow('600519')
        print(f"    获取到 {len(df)} 条日资金流向")
        for _, r in df.tail(3).iterrows():
            print(f"    {r['日期']}  主力净流入:{r.get('主力净流入-净额', 0):>15,.0f}")
    except Exception as e:
        print(f"    ❌ {e}")

    # 10. 龙虎榜
    print("\n" + "=" * 70)
    print("  📋 龙虎榜 (近3天)")
    print("=" * 70)
    try:
        df = api.get_lhb_detail()
        print(f"    获取到 {len(df)} 条")
        if not df.empty:
            for _, r in df.head(3).iterrows():
                print(f"    {r.get('代码', '')} {r.get('名称', '')}  "
                      f"{r.get('上榜日', '')}  净买额:{r.get('龙虎榜净买额', 0):>12,.0f}")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")

    # 11. 股东户数
    print("\n" + "=" * 70)
    print("  📋 股东户数 — 贵州茅台")
    print("=" * 70)
    try:
        df = api.get_shareholder_count_detail('600519')
        print(f"    获取到 {len(df)} 条历史股东户数")
        for _, r in df.tail(3).iterrows():
            print(f"    {r.get('股东户数统计截止日', '')}  "
                  f"本次:{r.get('股东户数-本次', 0):>8,}  "
                  f"增减:{r.get('股东户数-增减', 0):>+8,}")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")

    # 12. 批量股东户数
    try:
        df = api.get_shareholder_count_batch()
        print(f"\n全市场最新股东户数: {len(df)} 只")
    except AkshareAPIError as e:
        print(f"    ❌ 批量股东户数: {e}")

    # 13. 机构研报
    print("\n" + "=" * 70)
    print("  📋 机构研报 — 贵州茅台")
    print("=" * 70)
    try:
        df = api.get_research_reports('600519')
        print(f"    获取到 {len(df)} 份研报")
        if not df.empty:
            for _, r in df.head(3).iterrows():
                print(f"    {r.get('日期', '')}  "
                      f"{str(r.get('机构', ''))[:15]}  "
                      f"{str(r.get('东财评级', ''))}")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")

    # 14. 业绩预告
    print("\n" + "=" * 70)
    print("  📋 业绩预告")
    print("=" * 70)
    try:
        df = api.get_profit_forecast(date='20251231')
        print(f"    获取到 {len(df)} 条")
        if not df.empty:
            for _, r in df.head(3).iterrows():
                print(f"    {r.get('股票简称', '')}  "
                      f"{r.get('预告类型', '')}  "
                      f"{str(r.get('业绩变动', ''))[:50]}")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")

    # 15. 业绩快报
    try:
        df = api.get_profit_express(date='20251231')
        print(f"\n业绩快报: {len(df)} 条")
    except AkshareAPIError as e:
        print(f"    ❌ 业绩快报: {e}")

    # 16. 新股发行
    print("\n" + "=" * 70)
    print("  📋 新股发行信息")
    print("=" * 70)
    try:
        df = api.get_new_ipo_list()
        print(f"    待上市新股: {len(df)} 只")
        if not df.empty:
            for _, r in df.head(3).iterrows():
                print(f"    {r.get('证劵代码', '')} {r.get('证券简称', '')}  "
                      f"发行价:{r.get('发行价', '')}  "
                      f"申购日:{r.get('申购日期', '')}")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")

    # 17. IPO排队
    try:
        df = api.get_ipo_registration('sh')
        print(f"\n上交所IPO排队: {len(df)} 家")
    except AkshareAPIError as e:
        print(f"    ❌ IPO排队: {e}")

    # 18. 融资融券
    print("\n" + "=" * 70)
    print("  📋 融资融券")
    print("=" * 70)
    try:
        df = api.get_margin_account_statistics()
        print(f"    全市场融资融券账户统计: {len(df)} 条")
        if not df.empty:
            last = df.iloc[-1]
            print(f"    最新: {last.get('日期', '')}  "
                  f"融资余额:{last.get('融资余额', 0):>8.1f}亿  "
                  f"融券余额:{last.get('融券余额', 0):>8.1f}亿")
    except AkshareAPIError as e:
        print(f"    ❌ {e}")

    print("\n" + "=" * 70)
    print("  全部测试完成！")
    print("=" * 70)
