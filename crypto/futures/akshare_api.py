#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 1 自动搬运（2026-10-08）。
# 原名: futures/akshare_api.py  →  现位置: crypto/futures/akshare_api.py
# 导入方式已由裸模块名/sys.path 兜底改为显式包路径，避免与宿主同名模块互相遮蔽。
"""
统一的 akshare 期货数据接口工具类
==================================
提供统一接口封装 akshare 的期货相关 API，包括：
  - 主力连续合约日线 (futures_main_sina)
  - 指定合约日线 (futures_zh_daily_sina)
  - 实时行情快照 (futures_zh_realtime)
  - 主力合约列表 (futures_display_main_sina)
  - 品种命名表 (futures_symbol_mark)
  - 分钟K线 (futures_zh_minute_sina)
  - 代码与中文名双向映射

所有方法返回 pandas DataFrame，列名统一为英文。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import datetime
import logging
from typing import Optional, Dict

import pandas as pd

logger = logging.getLogger(__name__)


class AkshareAPIError(Exception):
    """akshare 数据获取异常"""
    pass


class AkshareFuturesAPI:
    """统一的 akshare 期货数据接口工具类

    封装所有常用的 akshare 期货数据接口，提供一致的调用方式和列名。
    """

    # ----------------------------------------------------------------
    #  代码 ↔ 中文名映射表（用于 futures_zh_realtime 等接口）
    # ----------------------------------------------------------------
    CODE_TO_NAME: Dict[str, str] = {
        # 能源化工
        'FG': '玻璃', 'SA': '纯碱', 'V': 'PVC', 'SC': '原油',
        'MA': '郑醇', 'UR': '尿素', 'BU': '沥青', 'RU': '橡胶',
        'FU': '燃油', 'SP': '纸浆', 'EG': '乙二醇', 'TA': 'PTA',
        'EB': '苯乙烯', 'BR': '丁二烯橡胶', 'PG': '液化石油气',
        'I': '铁矿石', 'PP': 'PP', 'LU': '低硫燃料油', 'NR': '20号胶',
        'PF': '短纤', 'L': '塑料',
        # 黑色金属
        'JM': '焦煤', 'RB': '螺纹钢', 'HC': '热轧卷板',
        'SM': '锰硅', 'SS': '不锈钢', 'SF': '硅铁', 'J': '焦炭',
        # 贵金属
        'AU': '黄金', 'AG': '白银',
        # 有色金属
        'LC': '碳酸锂', 'AO': '氧化铝', 'CU': '沪铜', 'SI': '工业硅',
        'SN': '沪锡', 'NI': '沪镍', 'AL': '沪铝', 'ZN': '沪锌',
        'PB': '沪铅', 'BC': '国际铜',
        # 农产品
        'LH': '生猪', 'CF': '棉花', 'JD': '鸡蛋', 'M': '豆粕',
        'SR': '白糖', 'C': '玉米', 'P': '棕榈', 'RM': '菜粕',
        'AP': '鲜苹果', 'A': '豆一', 'CJ': '红枣', 'Y': '豆油',
        'OI': '菜油', 'B': '豆二', 'CS': '玉米淀粉', 'PK': '花生',
        'CY': '棉纱', 'RS': '菜籽', 'RR': '粳米',
        # 金融板块
        'IM': '中证1000股指期货', 'IC': '中证500指数期货',
        'IF': '沪深300指数期货', 'IH': '上证50指数期货',
        'T': '10年期国债期货', 'TF': '5年期国债期货',
        'TS': '2年期国债期货',
    }

    # 自动生成反向映射
    NAME_TO_CODE: Dict[str, str] = {v: k for k, v in CODE_TO_NAME.items()}

    # ----------------------------------------------------------------
    #  映射查询
    # ----------------------------------------------------------------

    @classmethod
    def code_to_name(cls, code: str) -> str:
        """期货字母代码 → 中文品种名（用于实时行情接口）"""
        return cls.CODE_TO_NAME.get(code.upper(), code)

    @classmethod
    def name_to_code(cls, name: str) -> str:
        """中文品种名 → 字母代码"""
        return cls.NAME_TO_CODE.get(name, name)

    # ----------------------------------------------------------------
    #  元数据接口
    # ----------------------------------------------------------------

    @staticmethod
    def get_main_contracts() -> pd.DataFrame:
        """获取新浪财经-主力连续合约品种一览表

        Returns
        -------
        pd.DataFrame
            columns: ['symbol', 'exchange', 'name']
        """
        try:
            import akshare as ak
            df = ak.futures_display_main_sina()
            logger.info("获取主力合约列表成功，共 %d 个品种", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取主力合约列表失败: %s" % str(e))

    @staticmethod
    def get_symbol_mark() -> pd.DataFrame:
        """获取品种命名表

        Returns
        -------
        pd.DataFrame
            columns: ['exchange', 'symbol', 'mark']
        """
        try:
            import akshare as ak
            df = ak.futures_symbol_mark()
            logger.info("获取品种命名表成功，共 %d 个品种", len(df))
            return df
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")
        except Exception as e:
            raise AkshareAPIError("获取品种命名表失败: %s" % str(e))

    # ----------------------------------------------------------------
    #  日线数据接口
    # ----------------------------------------------------------------

    @staticmethod
    def fetch_daily_main(symbol: str,
                         start_date: Optional[str] = None,
                         end_date: Optional[str] = None) -> pd.DataFrame:
        """获取主力连续合约日线数据

        Parameters
        ----------
        symbol : str
            品种代码，如 'RB'（会自动补 '0' 后缀）或 'RB0'。
        start_date : str, optional
            起始日期 'YYYYMMDD'，默认 '20230101'。
        end_date : str, optional
            结束日期 'YYYYMMDD'，默认今日。

        Returns
        -------
        pd.DataFrame
            columns: ['date', 'open', 'high', 'low', 'close',
                      'volume', 'open_interest', 'settle']
        """
        try:
            import akshare as ak
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")

        # 自动补 '0' 后缀（主力连续标识）
        symbol_main = symbol if symbol.endswith('0') else symbol + '0'

        if start_date is None:
            start_date = '20230101'
        if end_date is None:
            end_date = datetime.date.today().strftime('%Y%m%d')

        try:
            df = ak.futures_main_sina(
                symbol=symbol_main,
                start_date=start_date,
                end_date=end_date
            )
        except Exception as e:
            raise AkshareAPIError(
                "获取主力合约 [%s] 数据失败: %s" % (symbol_main, str(e)))

        if df is None or df.empty:
            raise AkshareAPIError(
                "品种 [%s] 返回数据为空，可能已退市或无数据" % symbol_main)

        # 中文列名 → 英文列名
        return AkshareFuturesAPI._normalize_main_sina(df)

    @staticmethod
    def fetch_daily_contract(symbol: str) -> pd.DataFrame:
        """获取指定具体合约日线数据

        Parameters
        ----------
        symbol : str
            完整合约代码，如 'IF2506', 'RB2510'。

        Returns
        -------
        pd.DataFrame
            columns: ['date', 'open', 'high', 'low', 'close',
                      'volume', 'open_interest', 'settle']
        """
        try:
            import akshare as ak
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")

        try:
            df = ak.futures_zh_daily_sina(symbol=symbol)
        except Exception as e:
            raise AkshareAPIError(
                "获取合约 [%s] 日线数据失败: %s" % (symbol, str(e)))

        if df is None or df.empty:
            raise AkshareAPIError("合约 [%s] 返回数据为空" % symbol)

        return AkshareFuturesAPI._normalize_zh_daily(df)

    # ----------------------------------------------------------------
    #  实时行情接口
    # ----------------------------------------------------------------

    @staticmethod
    def fetch_realtime(variety: str) -> pd.DataFrame:
        """获取品种所有合约的实时行情快照

        Parameters
        ----------
        variety : str
            品种名，支持字母代码（如 'RB'）或中文名（如 '螺纹钢'）。
            自动匹配映射表。

        Returns
        -------
        pd.DataFrame
            columns: ['symbol', 'exchange', 'name', 'trade', 'open',
                      'high', 'low', 'close', 'volume', 'position',
                      'changepercent', ...]
        """
        try:
            import akshare as ak
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")

        # 自动转中文名（如果传入的是字母代码）
        symbol = AkshareFuturesAPI.code_to_name(variety)

        try:
            df = ak.futures_zh_realtime(symbol=symbol)
        except Exception as e:
            raise AkshareAPIError(
                "获取 [%s](%s) 实时行情失败: %s" % (variety, symbol, str(e)))

        if df is None or df.empty:
            raise AkshareAPIError(
                "品种 [%s] 实时行情返回空数据" % variety)

        return df

    # ----------------------------------------------------------------
    #  分钟K线接口
    # ----------------------------------------------------------------

    @staticmethod
    def fetch_minute_kline(symbol: str, period: str = '60') -> pd.DataFrame:
        """获取指定合约分钟K线数据

        Parameters
        ----------
        symbol : str
            具体合约代码，如 'IF2506', 'RB2508'。
            注意：不支持主力连续代码（如 'IF0'）。
        period : str
            分钟周期：'1', '5', '15', '30', '60' 等。

        Returns
        -------
        pd.DataFrame
            columns: ['date', 'open', 'high', 'low', 'close',
                      'volume', 'hold']
        """
        try:
            import akshare as ak
        except ImportError:
            raise AkshareAPIError("请先安装 akshare: pip install akshare")

        try:
            df = ak.futures_zh_minute_sina(symbol=symbol, period=period)
        except Exception as e:
            raise AkshareAPIError(
                "获取 [%s] %s分钟K线失败: %s" % (symbol, period, str(e)))

        if df is None or df.empty:
            raise AkshareAPIError(
                "合约 [%s] %s分钟K线返回空数据" % (symbol, period))

        # 统一列名
        return AkshareFuturesAPI._normalize_minute_kline(df)

    # ----------------------------------------------------------------
    #  批量数据获取
    # ----------------------------------------------------------------

    @classmethod
    def fetch_all_daily_main(cls, symbols, start_date=None, end_date=None):
        """批量获取多个品种的主力连续日线数据

        Parameters
        ----------
        symbols : list of str
            品种代码列表，如 ['RB', 'IF', 'M']。

        Returns
        -------
        dict[str, pd.DataFrame]
            {symbol: dataframe} 的字典。
        """
        results = {}
        for sym in symbols:
            try:
                df = cls.fetch_daily_main(sym, start_date, end_date)
                results[sym] = df
                logger.info("[%s] 获取成功: %d 条", sym, len(df))
            except AkshareAPIError as e:
                logger.warning("[%s] 获取失败: %s", sym, e)
        return results

    @classmethod
    def fetch_all_realtime_from_csv(cls, csv_path: str) -> pd.DataFrame:
        """从 CSV 批量获取所有品种实时行情

        Parameters
        ----------
        csv_path : str
            CSV 文件路径，必须包含 '代码' 列。

        Returns
        -------
        pd.DataFrame
            合并后的实时行情数据，包含 '品种' 和 '种类' 分类信息。
        """
        df_csv = pd.read_csv(csv_path, encoding='utf-8')
        df_csv = df_csv.dropna(subset=['代码']).reset_index(drop=True)
        df_csv['代码'] = df_csv['代码'].astype(str).str.strip()

        rows = []
        for _, row in df_csv.iterrows():
            code = row['代码']
            try:
                df_rt = cls.fetch_realtime(code)
                if df_rt is not None and not df_rt.empty:
                    df_rt['品种'] = row['品种']
                    df_rt['种类'] = row['种类']
                    df_rt['字母代码'] = code
                    rows.append(df_rt)
            except AkshareAPIError:
                logger.warning("[%s] 实时行情获取失败，跳过", code)

        if not rows:
            return pd.DataFrame()

        return pd.concat(rows, ignore_index=True)

    # ----------------------------------------------------------------
    #  列名标准化
    # ----------------------------------------------------------------

    @staticmethod
    def _normalize_main_sina(df: pd.DataFrame) -> pd.DataFrame:
        """标准化 futures_main_sina 的列名为英文"""
        col_map = {
            '日期': 'date', '开盘价': 'open', '最高价': 'high',
            '最低价': 'low', '收盘价': 'close', '成交量': 'volume',
            '持仓量': 'open_interest', '动态结算价': 'settle',
        }
        # 只映射存在的列
        rename = {k: v for k, v in col_map.items() if k in df.columns}
        df = df.rename(columns=rename)

        # 确保 date 列存在
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date'])
            df = df.sort_values('date').reset_index(drop=True)

        # 数值列类型转换
        numeric_cols = ['open', 'high', 'low', 'close',
                        'volume', 'open_interest', 'settle']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        return df

    @staticmethod
    def _normalize_zh_daily(df: pd.DataFrame) -> pd.DataFrame:
        """标准化 futures_zh_daily_sina 的列名为英文"""
        col_map = {
            'date': 'date', 'open': 'open', 'high': 'high',
            'low': 'low', 'close': 'close', 'volume': 'volume',
            'hold': 'open_interest',
        }
        rename = {k: v for k, v in col_map.items() if k in df.columns}
        df = df.rename(columns=rename)

        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date'])
            df = df.sort_values('date').reset_index(drop=True)

        numeric_cols = ['open', 'high', 'low', 'close',
                        'volume', 'open_interest']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        return df

    @staticmethod
    def _normalize_minute_kline(df: pd.DataFrame) -> pd.DataFrame:
        """标准化 futures_zh_minute_sina 的列名为英文"""
        col_map = {
            'datetime': 'date', 'open': 'open', 'high': 'high',
            'low': 'low', 'close': 'close', 'volume': 'volume',
            'hold': 'hold',
        }
        rename = {k: v for k, v in col_map.items() if k in df.columns}
        df = df.rename(columns=rename)

        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date'])
            df = df.sort_values('date').reset_index(drop=True)

        numeric_cols = ['open', 'high', 'low', 'close',
                        'volume', 'hold']
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

    api = AkshareFuturesAPI()
    print("=" * 70)
    print("  AkshareFuturesAPI - 统一期货数据接口")
    print("=" * 70)

    # 1. 代码 → 中文名 映射测试
    print("\n📋 映射测试:")
    for code in ['RB', 'IF', 'AU', 'I', 'M', 'SC']:
        name = api.code_to_name(code)
        back_code = api.name_to_code(name)
        print("    %-4s → %-12s → %s" % (code, name, back_code))

    # 2. 主力合约列表
    try:
        df = api.get_main_contracts()
        print("\n📋 主力合约列表: %d 个品种" % len(df))
        for _, r in df.head(5).iterrows():
            print("    %-8s %-6s %s" % (r['symbol'], r['exchange'], r['name']))
    except AkshareAPIError as e:
        print("    ❌ %s" % e)

    # 3. 日线数据测试
    try:
        df = api.fetch_daily_main('IF0', '20250501', '20250515')
        print("\n📊 IF 主力连续 (5月):")
        for _, r in df.tail(3).iterrows():
            print("    %s  O:%.1f  H:%.1f  L:%.1f  C:%.1f  V:%d" %
                  (r['date'].strftime('%Y-%m-%d'),
                   r['open'], r['high'], r['low'], r['close'],
                   int(r['volume'])))
    except AkshareAPIError as e:
        print("    ❌ %s" % e)

    print("\n" + "=" * 70)
