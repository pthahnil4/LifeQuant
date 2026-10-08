#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
多币种历史K线数据批量存储模块
==========================

基于demo52的成熟代码，扩展支持多币种批量处理
功能：
1. 支持15个币种的历史K线数据获取
2. 分页处理，突破API限制
3. 去重机制，避免重复写入
4. 批量存储到InfluxDB

作者：AI Assistant
创建时间：2025年1月
基于：demo52_InfluxDB存储K线数据.py
"""

import sys
import os
from datetime import datetime, timedelta, timezone
import json
import time
import pandas as pd
from typing import List, Dict, Any, Optional, Tuple

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入OKX API
from okx import MarketData

# 导入配置
from influxdb_config import InfluxDBConfig

# 导入InfluxDB客户端
try:
    from influxdb_client import InfluxDBClient, Point, DeleteApi
    from influxdb_client.client.write_api import SYNCHRONOUS
    from influxdb_client.client.query_api import QueryApi
    print("✅ InfluxDB 2.x 客户端库导入成功")
except ImportError as e:
    print("❌ InfluxDB客户端库导入失败")
    print("请安装: pip install influxdb-client")
    sys.exit(1)

# 支持的币种列表
SUPPORTED_SYMBOLS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP", "ADA-USDT-SWAP", "LTC-USDT-SWAP", "NEAR-USDT-SWAP", "TRX-USDT-SWAP",
    "BCH-USDT-SWAP", "DOT-USDT-SWAP", "UNI-USDT-SWAP", "LINK-USDT-SWAP", "TRUMP-USDT-SWAP"
]

# 支持的时间周期
SUPPORTED_PERIODS = ["5m", "15m", "1H", "4H", "1D", "1W"]

class MultiSymbolKlineStorage:
    """多币种K线数据存储器"""
    
    def __init__(self):
        print("🔧 初始化多币种K线数据存储器...")
        
        # InfluxDB配置
        self.config = InfluxDBConfig()
        self.client = None
        self.write_api = None
        self.query_api = None
        self.delete_api = None
        
        # OKX API配置
        self.market_api = MarketData.MarketAPI(flag="0")  # 实盘数据
        
        # 处理统计
        self.total_fetched = 0
        self.total_written = 0
        self.total_skipped = 0
        
        print("✅ 多币种K线数据存储器初始化完成")
    
    def connect_influxdb(self) -> bool:
        """连接到InfluxDB"""
        try:
            print("🔗 正在连接InfluxDB...")
            client_config = self.config.get_client_config()
            self.client = InfluxDBClient(**client_config)
            
            # 测试连接
            health = self.client.health()
            print(f"✅ InfluxDB连接成功 - 状态: {health.status}, 版本: {health.version}")
            
            # 初始化API
            self.query_api = self.client.query_api()
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
            self.delete_api = DeleteApi(self.client)
            
            return True
            
        except Exception as e:
            print(f"❌ InfluxDB连接失败: {e}")
            return False
    
    def get_table_name(self, symbol: str) -> str:
        """根据币种生成表名"""
        # 将 BTC-USDT-SWAP 转换为 btc_usdt_swap_kline
        clean_symbol = symbol.replace('-', '_').lower()
        return f"{clean_symbol}_kline"
    
    def get_api_symbol(self, symbol: str) -> str:
        """转换为API使用的币种格式"""
        # BTC-USDT-SWAP -> BTC-USDT (移除SWAP)
        if symbol.endswith('-SWAP'):
            return symbol.replace('-SWAP', '')
        return symbol
    
    def get_existing_timestamps(self, symbol: str, period: str, start_time: datetime, end_time: datetime) -> set:
        """获取已存在的时间戳，用于去重"""
        try:
            table_name = self.get_table_name(symbol)
            
            query = f'''
            from(bucket: "{self.config.DEFAULT_BUCKETS[0]}")
                |> range(start: {start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}, 
                         stop: {end_time.strftime('%Y-%m-%dT%H:%M:%SZ')})
                |> filter(fn: (r) => r._measurement == "{table_name}")
                |> filter(fn: (r) => r.period == "{period}")
                |> filter(fn: (r) => r._field == "close")
                |> keep(columns: ["_time"])
            '''
            
            result = self.query_api.query(org=self.config.ORG, query=query)
            
            timestamps = set()
            for table in result:
                for record in table.records:
                    timestamps.add(record.get_time())
            
            return timestamps
            
        except Exception as e:
            print(f"❌ 获取已存在时间戳失败: {e}")
            return set()
    
    def fetch_kline_data_paginated(self, symbol: str, period: str, start_time: datetime, end_time: datetime) -> List[Dict]:
        """分页获取K线数据"""
        try:
            api_symbol = self.get_api_symbol(symbol)
            print(f"📥 开始获取 {symbol} ({api_symbol}) {period} 周期K线数据")
            print(f"   时间范围: {start_time.date()} ~ {end_time.date()}")
            
            all_data = []
            current_end = end_time
            page_count = 0
            
            while current_end > start_time:
                page_count += 1
                
                # 优先尝试标记价格API（更稳定）
                result = self.market_api.get_mark_price_candlesticks(
                    instId=api_symbol,
                    bar=period,
                    limit="100",
                    before=str(int(current_end.timestamp() * 1000))
                )
                
                # 如果标记价格API失败，尝试指数K线API
                if 'code' in result and result['code'] != '0':
                    print(f"   ⚠️ 标记价格API失败，尝试指数K线API")
                    result = self.market_api.get_index_candlesticks(
                        instId=api_symbol,
                        bar=period,
                        limit="100",
                        before=str(int(current_end.timestamp() * 1000))
                    )
                
                # 如果两个API都返回成功但无数据，添加调试信息
                if 'data' not in result or not result['data']:
                    print(f"   🔍 调试信息:")
                    print(f"      API响应码: {result.get('code', 'N/A')}")
                    print(f"      API消息: {result.get('msg', 'N/A')}")
                    print(f"      请求参数: instId={api_symbol}, bar={period}, before={int(current_end.timestamp() * 1000)}")
                    print(f"      请求时间戳对应: {current_end}")
                    print(f"   ⚠️ 第{page_count}页无数据，停止分页")
                    break
                
                page_data = result['data']
                print(f"   📄 第{page_count}页: 获取{len(page_data)}条数据")
                
                # 过滤时间范围内的数据并转换格式
                filtered_data = []
                for kline in page_data:
                    timestamp = datetime.fromtimestamp(int(kline[0]) / 1000, tz=timezone.utc)
                    if start_time <= timestamp <= end_time:
                        filtered_data.append({
                            'timestamp': timestamp,
                            'open': float(kline[1]),
                            'high': float(kline[2]),
                            'low': float(kline[3]),
                            'close': float(kline[4]),
                            'volume': float(kline[5]) if len(kline) > 5 else 0.0,
                            'vol_ccy': float(kline[6]) if len(kline) > 6 else 0.0,
                            'confirm': int(kline[7]) if len(kline) > 7 else 1
                        })
                
                all_data.extend(filtered_data)
                
                # 更新下一页的结束时间
                if filtered_data:
                    current_end = min(item['timestamp'] for item in filtered_data) - timedelta(seconds=1)
                else:
                    break
                
                # 检查是否已经获取到开始时间之前的数据
                if filtered_data and min(item['timestamp'] for item in filtered_data) <= start_time:
                    break
                
                # 防止API过于频繁调用
                time.sleep(0.1)
            
            print(f"   ✅ 分页查询完成: 共获取 {len(all_data)} 条数据（{page_count} 页）")
            return all_data
            
        except Exception as e:
            print(f"❌ 获取K线数据失败: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def store_kline_data(self, symbol: str, period: str, kline_data: List[Dict], existing_timestamps: set) -> Tuple[int, int]:
        """存储K线数据到InfluxDB"""
        try:
            if not kline_data:
                return 0, 0
            
            table_name = self.get_table_name(symbol)
            points = []
            skipped_count = 0
            
            for data in kline_data:
                # 检查是否已存在
                if data['timestamp'] in existing_timestamps:
                    skipped_count += 1
                    continue
                
                # 创建Point对象
                point = Point(table_name) \
                    .tag("symbol", symbol) \
                    .tag("period", period) \
                    .field("open", data['open']) \
                    .field("high", data['high']) \
                    .field("low", data['low']) \
                    .field("close", data['close']) \
                    .field("volume", data['volume']) \
                    .field("vol_ccy", data['vol_ccy']) \
                    .field("confirm", data['confirm']) \
                    .time(data['timestamp'])
                
                points.append(point)
            
            # 批量写入
            if points:
                write_start = time.time()
                self.write_api.write(bucket=self.config.DEFAULT_BUCKETS[0], org=self.config.ORG, record=points)
                write_time = time.time() - write_start
                
                print(f"   ✅ 写入成功: {len(points)} 条 (耗时{write_time:.2f}s)")
                if skipped_count > 0:
                    print(f"   ⚡ 去重跳过: {skipped_count} 条")
                
                return len(points), skipped_count
            else:
                print(f"   ⚡ 全部跳过: {skipped_count} 条 (已存在)")
                return 0, skipped_count
                
        except Exception as e:
            print(f"❌ 存储数据失败: {e}")
            import traceback
            traceback.print_exc()
            return 0, 0
    
    def process_single_symbol_period(self, symbol: str, period: str, start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """处理单个币种和周期的数据"""
        print(f"\n🔄 处理 {symbol} - {period}")
        start = time.time()
        
        result = {
            'symbol': symbol,
            'period': period,
            'success': False,
            'fetched': 0,
            'written': 0,
            'skipped': 0,
            'duration': 0,
            'error': None
        }
        
        try:
            # 获取已存在的时间戳
            existing_timestamps = self.get_existing_timestamps(symbol, period, start_time, end_time)
            
            # 获取K线数据
            kline_data = self.fetch_kline_data_paginated(symbol, period, start_time, end_time)
            result['fetched'] = len(kline_data)
            
            if kline_data:
                # 存储数据
                written, skipped = self.store_kline_data(symbol, period, kline_data, existing_timestamps)
                result['written'] = written
                result['skipped'] = skipped
                result['success'] = True
                
                # 更新总计
                self.total_fetched += len(kline_data)
                self.total_written += written
                self.total_skipped += skipped
            else:
                print(f"   ⚠️ 无数据获取")
            
        except Exception as e:
            result['error'] = str(e)
            print(f"❌ 处理失败: {e}")
        
        result['duration'] = time.time() - start
        return result
    
    def batch_store_history_data(self, symbols: List[str], periods: List[str], 
                                start_time: Optional[datetime] = None, 
                                end_time: Optional[datetime] = None) -> Dict[str, Any]:
        """批量存储历史数据"""
        if not self.connect_influxdb():
            return {'success': False, 'error': 'InfluxDB连接失败'}
        
        # 设置默认时间范围
        if end_time is None:
            end_time = datetime.now(timezone.utc)
        if start_time is None:
            start_time = end_time - timedelta(days=30)
        
        # 调试：显示当前时间和设置的时间范围
        current_time = datetime.now(timezone.utc)
        print(f"🕐 当前UTC时间: {current_time}")
        print(f"📅 设置的时间范围: {start_time} ~ {end_time}")
        
        # 如果时间范围看起来不合理，使用更保守的范围
        if end_time > current_time + timedelta(hours=1):  # 如果结束时间超过当前时间1小时
            print("⚠️ 检测到时间范围异常，使用默认范围")
            end_time = current_time
            start_time = end_time - timedelta(days=7)  # 改为最近7天
            print(f"📅 调整后时间范围: {start_time} ~ {end_time}")
        
        print(f"\n🚀 开始批量存储历史K线数据")
        print(f"📈 币种数量: {len(symbols)}")
        print(f"⏰ 周期数量: {len(periods)}")
        print(f"📅 时间范围: {start_time.date()} ~ {end_time.date()}")
        
        # 生成任务列表
        tasks = []
        for symbol in symbols:
            for period in periods:
                tasks.append((symbol, period))
        
        total_tasks = len(tasks)
        print(f"📋 总任务数: {total_tasks}")
        
        # 重置统计
        self.total_fetched = 0
        self.total_written = 0
        self.total_skipped = 0
        
        # 执行存储任务
        results = []
        success_count = 0
        failed_count = 0
        
        for i, (symbol, period) in enumerate(tasks, 1):
            print(f"\n[{i}/{total_tasks}]", end="")
            
            result = self.process_single_symbol_period(symbol, period, start_time, end_time)
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
            'total_fetched': self.total_fetched,
            'total_written': self.total_written,
            'total_skipped': self.total_skipped,
            'total_duration': total_duration,
            'results': results
        }
        
        # 显示汇总
        print(f"\n{'='*60}")
        print(f"📊 批量存储完成统计:")
        print(f"   总任务数: {total_tasks}")
        print(f"   成功数: {success_count}")
        print(f"   失败数: {failed_count}")
        print(f"   获取数据: {self.total_fetched:,} 条")
        print(f"   写入数据: {self.total_written:,} 条")
        print(f"   跳过数据: {self.total_skipped:,} 条")
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
    storage = MultiSymbolKlineStorage()
    
    try:
        # 测试存储单个币种
        test_symbols = ["BTC-USDT-SWAP"]
        test_periods = ["5m", "15m"]
        
        result = storage.batch_store_history_data(
            symbols=test_symbols,
            periods=test_periods
        )
        
        if result['success']:
            print("🎉 测试完成")
        else:
            print(f"❌ 测试失败: {result.get('error')}")
    
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断")
    except Exception as e:
        print(f"❌ 测试异常: {e}")
    finally:
        storage.close()

if __name__ == "__main__":
    main()
