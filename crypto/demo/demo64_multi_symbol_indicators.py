#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
多币种技术指标批量计算模块
========================

基于demo53的成熟指标计算，扩展支持多币种批量处理
功能：
1. 从InfluxDB读取K线数据
2. 计算多种技术指标（MA、MACD、BOLL、RSI、SAR、KDJ）
3. 批量存储指标到InfluxDB
4. 支持去重和增量计算

作者：AI Assistant
创建时间：2025年1月
基于：demo53_InfluxDB技术指标计算.py
"""

import sys
import os
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
import time

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入InfluxDB客户端
try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
    from influxdb_client.client.query_api import QueryApi
    from influxdb_client.client.delete_api import DeleteApi
    print("✅ InfluxDB 2.x 客户端库导入成功")
except ImportError as e:
    print("❌ InfluxDB客户端库导入失败")
    print("请安装: pip install influxdb-client")
    sys.exit(1)

# 导入配置
from influxdb_config import InfluxDBConfig

# 支持的币种列表
SUPPORTED_SYMBOLS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP", "ADA-USDT-SWAP", "LTC-USDT-SWAP", "NEAR-USDT-SWAP", "TRX-USDT-SWAP",
    "BCH-USDT-SWAP", "DOT-USDT-SWAP", "UNI-USDT-SWAP", "LINK-USDT-SWAP", "TRUMP-USDT-SWAP"
]

# 支持的时间周期
SUPPORTED_PERIODS = ["5m", "15m", "1H", "4H", "1D", "1W"]

class MultiSymbolIndicatorCalculator:
    """多币种技术指标计算器"""
    
    def __init__(self):
        print("🔧 初始化多币种技术指标计算器...")
        
        # InfluxDB配置
        self.config = InfluxDBConfig()
        self.client = None
        self.write_api = None
        self.query_api = None
        self.delete_api = None
        
        # 指标计算参数
        self.ma_12_period = 12
        self.ma_26_period = 26
        self.macd_fast_period = 12
        self.macd_slow_period = 26
        self.macd_signal_period = 9
        self.rsi_period = 14
        self.boll_period = 20
        self.boll_std_multiplier = 2
        
        # 处理统计
        self.total_processed = 0
        self.total_written = 0
        self.total_skipped = 0
        
        print("✅ 多币种技术指标计算器初始化完成")
    
    def connect_influxdb(self) -> bool:
        """连接到InfluxDB"""
        try:
            print("🔗 正在连接InfluxDB...")
            client_config = self.config.get_client_config()
            self.client = InfluxDBClient(**client_config)
            
            # 测试连接
            health = self.client.health()
            print(f"✅ InfluxDB连接成功 - 状态: {health.status}")
            
            # 初始化API
            self.query_api = self.client.query_api()
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
            self.delete_api = DeleteApi(self.client)
            
            return True
            
        except Exception as e:
            print(f"❌ InfluxDB连接失败: {e}")
            return False
    
    def get_kline_table_name(self, symbol: str) -> str:
        """根据币种生成K线表名"""
        clean_symbol = symbol.replace('-', '_').lower()
        return f"{clean_symbol}_kline"
    
    def get_indicator_table_name(self, symbol: str) -> str:
        """根据币种生成指标表名"""
        clean_symbol = symbol.replace('-', '_').lower()
        return f"{clean_symbol}_indicators"
    
    def fetch_kline_data(self, symbol: str, period: str, limit: int = 1000) -> Optional[pd.DataFrame]:
        """从InfluxDB获取K线数据"""
        try:
            kline_table = self.get_kline_table_name(symbol)
            
            query = f'''
            from(bucket: "{self.config.DEFAULT_BUCKETS[0]}")
                |> range(start: -30d)
                |> filter(fn: (r) => r._measurement == "{kline_table}")
                |> filter(fn: (r) => r.period == "{period}")
                |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"])
                |> limit(n: {limit})
            '''
            
            result = self.query_api.query(org=self.config.ORG, query=query)
            
            # 转换为pandas DataFrame
            data_list = []
            for table in result:
                for record in table.records:
                    data_list.append({
                        'timestamp': record.get_time(),
                        'open': float(record.values.get('open', 0)),
                        'high': float(record.values.get('high', 0)),
                        'low': float(record.values.get('low', 0)),
                        'close': float(record.values.get('close', 0)),
                        'volume': float(record.values.get('volume', 0)),
                        'vol_ccy': float(record.values.get('vol_ccy', 0)),
                        'confirm': int(record.values.get('confirm', 0))
                    })
            
            if not data_list:
                print(f"   ⚠️ 无K线数据: {symbol} {period}")
                return None
            
            df = pd.DataFrame(data_list)
            df = df.sort_values('timestamp').reset_index(drop=True)
            
            print(f"   📊 获取K线数据: {len(df)} 条")
            return df
            
        except Exception as e:
            print(f"❌ 获取K线数据失败 {symbol} {period}: {e}")
            return None
    
    def calculate_ma(self, df: pd.DataFrame, period: int) -> pd.Series:
        """计算移动平均线"""
        return df['close'].rolling(window=period).mean()
    
    def calculate_ema(self, df: pd.DataFrame, period: int) -> pd.Series:
        """计算指数移动平均线"""
        return df['close'].ewm(span=period).mean()
    
    def calculate_macd(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """计算MACD指标"""
        ema_fast = self.calculate_ema(df, self.macd_fast_period)
        ema_slow = self.calculate_ema(df, self.macd_slow_period)
        
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=self.macd_signal_period).mean()
        macd = (dif - dea) * 2
        
        return dif, dea, macd
    
    def calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """计算RSI指标"""
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def calculate_boll(self, df: pd.DataFrame, period: int = 20, std_multiplier: float = 2) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """计算布林带指标"""
        middle = df['close'].rolling(window=period).mean()
        std = df['close'].rolling(window=period).std()
        upper = middle + (std * std_multiplier)
        lower = middle - (std * std_multiplier)
        return upper, middle, lower
    
    def calculate_sar(self, df: pd.DataFrame, step: float = 0.02, maximum: float = 0.2) -> pd.Series:
        """计算抛物线SAR指标"""
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        
        sar = np.zeros(len(df))
        trend = np.zeros(len(df))
        acc = np.zeros(len(df))
        extreme = np.zeros(len(df))
        
        # 初始化
        sar[0] = low[0]
        trend[0] = 1  # 1 for up, -1 for down
        acc[0] = step
        extreme[0] = high[0]
        
        for i in range(1, len(df)):
            if trend[i-1] == 1:  # Uptrend
                sar[i] = sar[i-1] + acc[i-1] * (extreme[i-1] - sar[i-1])
                
                if low[i] <= sar[i]:
                    trend[i] = -1
                    sar[i] = extreme[i-1]
                    acc[i] = step
                    extreme[i] = low[i]
                else:
                    trend[i] = 1
                    if high[i] > extreme[i-1]:
                        extreme[i] = high[i]
                        acc[i] = min(acc[i-1] + step, maximum)
                    else:
                        extreme[i] = extreme[i-1]
                        acc[i] = acc[i-1]
            else:  # Downtrend
                sar[i] = sar[i-1] + acc[i-1] * (extreme[i-1] - sar[i-1])
                
                if high[i] >= sar[i]:
                    trend[i] = 1
                    sar[i] = extreme[i-1]
                    acc[i] = step
                    extreme[i] = high[i]
                else:
                    trend[i] = -1
                    if low[i] < extreme[i-1]:
                        extreme[i] = low[i]
                        acc[i] = min(acc[i-1] + step, maximum)
                    else:
                        extreme[i] = extreme[i-1]
                        acc[i] = acc[i-1]
        
        return pd.Series(sar, index=df.index)
    
    def calculate_kdj(self, df: pd.DataFrame, period: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """计算KDJ指标"""
        low_min = df['low'].rolling(window=period).min()
        high_max = df['high'].rolling(window=period).max()
        
        rsv = 100 * (df['close'] - low_min) / (high_max - low_min)
        
        k = pd.Series(np.zeros(len(df)), index=df.index)
        d = pd.Series(np.zeros(len(df)), index=df.index)
        j = pd.Series(np.zeros(len(df)), index=df.index)
        
        k.iloc[0] = 50
        d.iloc[0] = 50
        
        for i in range(1, len(df)):
            k.iloc[i] = (2/3) * k.iloc[i-1] + (1/3) * rsv.iloc[i]
            d.iloc[i] = (2/3) * d.iloc[i-1] + (1/3) * k.iloc[i]
            j.iloc[i] = 3 * k.iloc[i] - 2 * d.iloc[i]
        
        return k, d, j
    
    def calculate_all_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算所有技术指标"""
        try:
            # 计算各种指标
            df['ma12'] = self.calculate_ma(df, self.ma_12_period)
            df['ma26'] = self.calculate_ma(df, self.ma_26_period)
            
            dif, dea, macd = self.calculate_macd(df)
            df['dif'] = dif
            df['dea'] = dea
            df['macd'] = macd
            
            df['rsi'] = self.calculate_rsi(df, self.rsi_period)
            
            boll_upper, boll_middle, boll_lower = self.calculate_boll(df, self.boll_period, self.boll_std_multiplier)
            df['boll_upper'] = boll_upper
            df['boll_middle'] = boll_middle
            df['boll_lower'] = boll_lower
            
            df['sar'] = self.calculate_sar(df)
            
            kdj_k, kdj_d, kdj_j = self.calculate_kdj(df)
            df['kdj_k'] = kdj_k
            df['kdj_d'] = kdj_d
            df['kdj_j'] = kdj_j
            
            # 计算一些辅助字段（简化版本）
            df['direction'] = np.where(df['close'] > df['close'].shift(1), 1, -1)
            df['current_profit'] = 0.0  # 简化设为0
            df['zero1'] = np.where(df['macd'] > 0, 1, 0)  # MACD信号
            df['zero2'] = np.where(df['dif'] > df['dea'], 1, 0)  # DIF>DEA信号
            df['deal_flag'] = 0  # 简化设为0
            df['strategy_flag'] = 0  # 简化设为0
            
            return df
            
        except Exception as e:
            print(f"❌ 计算指标失败: {e}")
            import traceback
            traceback.print_exc()
            return df
    
    def delete_existing_indicators(self, symbol: str, period: str, start_time: datetime, end_time: datetime):
        """删除已存在的指标数据"""
        try:
            indicator_table = self.get_indicator_table_name(symbol)
            
            predicate = f'_measurement="{indicator_table}" AND symbol="{symbol}" AND period="{period}"'
            
            self.delete_api.delete(
                start=start_time,
                stop=end_time,
                predicate=predicate,
                bucket=self.config.DEFAULT_BUCKETS[0],
                org=self.config.ORG
            )
            
            print(f"   🗑️  清理已存在指标数据")
            
        except Exception as e:
            print(f"❌ 删除已存在指标失败: {e}")
    
    def store_indicators(self, symbol: str, period: str, df: pd.DataFrame) -> Tuple[int, int]:
        """存储指标数据到InfluxDB"""
        try:
            indicator_table = self.get_indicator_table_name(symbol)
            
            # 过滤掉NaN值的数据
            df_clean = df.dropna()
            
            if df_clean.empty:
                print(f"   ⚠️ 无有效指标数据")
                return 0, 0
            
            # 删除已存在的数据
            start_time = df_clean['timestamp'].min()
            end_time = df_clean['timestamp'].max()
            self.delete_existing_indicators(symbol, period, start_time, end_time)
            
            points = []
            
            # 指标字段列表
            indicator_fields = [
                'ma12', 'ma26', 'dif', 'dea', 'macd', 'rsi', 
                'boll_upper', 'boll_middle', 'boll_lower', 'sar',
                'kdj_k', 'kdj_d', 'kdj_j', 'direction', 'current_profit',
                'zero1', 'zero2', 'deal_flag', 'strategy_flag'
            ]
            
            for _, row in df_clean.iterrows():
                point = Point(indicator_table) \
                    .tag("symbol", symbol) \
                    .tag("period", period) \
                    .time(row['timestamp'])
                
                # 添加所有指标字段
                for field in indicator_fields:
                    if field in row and pd.notna(row[field]):
                        point = point.field(field, float(row[field]))
                
                points.append(point)
            
            # 批量写入
            if points:
                write_start = time.time()
                self.write_api.write(bucket=self.config.DEFAULT_BUCKETS[0], org=self.config.ORG, record=points)
                write_time = time.time() - write_start
                
                print(f"   ✅ 写入指标: {len(points)} 条 (耗时{write_time:.2f}s)")
                return len(points), 0
            else:
                print(f"   ⚠️ 无指标数据写入")
                return 0, 0
                
        except Exception as e:
            print(f"❌ 存储指标失败: {e}")
            import traceback
            traceback.print_exc()
            return 0, 0
    
    def process_single_symbol_period(self, symbol: str, period: str) -> Dict[str, Any]:
        """处理单个币种和周期的指标计算"""
        print(f"\n🔄 计算 {symbol} - {period}")
        start = time.time()
        
        result = {
            'symbol': symbol,
            'period': period,
            'success': False,
            'kline_count': 0,
            'indicators_written': 0,
            'indicators_skipped': 0,
            'duration': 0,
            'error': None
        }
        
        try:
            # 获取K线数据
            df = self.fetch_kline_data(symbol, period)
            
            if df is None or df.empty:
                result['error'] = 'K线数据为空'
                return result
            
            result['kline_count'] = len(df)
            
            # 计算指标
            print(f"   📊 开始计算指标...")
            df = self.calculate_all_indicators(df)
            
            # 存储指标
            written, skipped = self.store_indicators(symbol, period, df)
            result['indicators_written'] = written
            result['indicators_skipped'] = skipped
            result['success'] = True
            
            # 更新总计
            self.total_processed += len(df)
            self.total_written += written
            self.total_skipped += skipped
            
        except Exception as e:
            result['error'] = str(e)
            print(f"❌ 计算失败: {e}")
        
        result['duration'] = time.time() - start
        return result
    
    def batch_calculate_indicators(self, symbols: List[str], periods: List[str]) -> Dict[str, Any]:
        """批量计算技术指标"""
        if not self.connect_influxdb():
            return {'success': False, 'error': 'InfluxDB连接失败'}
        
        print(f"\n🚀 开始批量计算技术指标")
        print(f"📈 币种数量: {len(symbols)}")
        print(f"⏰ 周期数量: {len(periods)}")
        
        # 生成任务列表
        tasks = []
        for symbol in symbols:
            for period in periods:
                tasks.append((symbol, period))
        
        total_tasks = len(tasks)
        print(f"📋 总任务数: {total_tasks}")
        
        # 重置统计
        self.total_processed = 0
        self.total_written = 0
        self.total_skipped = 0
        
        # 执行计算任务
        results = []
        success_count = 0
        failed_count = 0
        
        for i, (symbol, period) in enumerate(tasks, 1):
            print(f"[{i}/{total_tasks}]", end="")
            
            result = self.process_single_symbol_period(symbol, period)
            results.append(result)
            
            if result['success']:
                success_count += 1
            else:
                failed_count += 1
        
        # 汇总结果
        total_duration = sum(r['duration'] for r in results)
        
        summary = {
            'success': True,
            'total_tasks': total_tasks,
            'success_count': success_count,
            'failed_count': failed_count,
            'total_processed': self.total_processed,
            'total_written': self.total_written,
            'total_skipped': self.total_skipped,
            'total_duration': total_duration,
            'results': results
        }
        
        # 显示汇总
        print(f"\n{'='*60}")
        print(f"📊 批量指标计算完成统计:")
        print(f"   总任务数: {total_tasks}")
        print(f"   成功数: {success_count}")
        print(f"   失败数: {failed_count}")
        print(f"   处理数据: {self.total_processed:,} 条")
        print(f"   写入指标: {self.total_written:,} 条")
        print(f"   跳过指标: {self.total_skipped:,} 条")
        print(f"   总耗时: {total_duration:.2f} 秒")
        print(f"{'='*60}")
        
        return summary
    
    def close(self):
        """关闭连接"""
        if self.client:
            self.client.close()
            print("🔒 InfluxDB连接已关闭")

def main():
    """主函数 - 用于测试"""
    calculator = MultiSymbolIndicatorCalculator()
    
    try:
        # 测试计算单个币种指标
        test_symbols = ["BTC-USDT-SWAP"]
        test_periods = ["5m", "15m"]
        
        result = calculator.batch_calculate_indicators(test_symbols, test_periods)
        
        if result['success']:
            print("🎉 测试完成")
        else:
            print(f"❌ 测试失败: {result.get('error')}")
    
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断")
    except Exception as e:
        print(f"❌ 测试异常: {e}")
    finally:
        calculator.close()

if __name__ == "__main__":
    main()
