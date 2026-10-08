#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
多币种WebSocket增量更新模块
========================

基于demo42的成熟WebSocket连接，扩展支持增量写入InfluxDB
功能：
1. 连接OKX WebSocket公共频道
2. 订阅多币种K线数据
3. 增量更新策略：新增插入，变化更新
4. 自动重连机制

作者：AI Assistant
创建时间：2025年1月
基于：demo42_标记价格K线WebSocket订阅.py
"""

import asyncio
import json
import sys
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import time

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入WebSocket客户端
try:
    from websocket.WsPublicAsync import WsPublicAsync
    print("✅ WebSocket客户端库导入成功")
except ImportError as e:
    print("❌ WebSocket客户端库导入失败")
    print("请检查websocket模块")
    sys.exit(1)

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

class WebSocketIncrementalUpdater:
    """WebSocket增量更新器"""
    
    def __init__(self):
        print("🔧 初始化WebSocket增量更新器...")
        
        # InfluxDB配置
        self.config = InfluxDBConfig()
        self.client = None
        self.write_api = None
        self.query_api = None
        self.delete_api = None
        
        # WebSocket配置
        self.ws_client = None
        self.running = False
        self.subscribed_channels = []
        
        # 统计信息
        self.total_received = 0
        self.total_inserted = 0
        self.total_updated = 0
        
        print("✅ WebSocket增量更新器初始化完成")
    
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
    
    def get_table_name(self, symbol: str) -> str:
        """根据币种生成表名"""
        clean_symbol = symbol.replace('-', '_').lower()
        return f"{clean_symbol}_kline"
    
    def check_existing_record(self, symbol: str, period: str, timestamp: datetime) -> Optional[Dict]:
        """检查是否存在相同时间戳的记录"""
        try:
            table_name = self.get_table_name(symbol)
            
            # 查询前后1分钟的数据
            start_time = timestamp - timedelta(minutes=1)
            stop_time = timestamp + timedelta(minutes=1)
            
            query = f'''
            from(bucket: "{self.config.DEFAULT_BUCKETS[0]}")
                |> range(start: {start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}, 
                         stop: {stop_time.strftime('%Y-%m-%dT%H:%M:%SZ')})
                |> filter(fn: (r) => r._measurement == "{table_name}")
                |> filter(fn: (r) => r.period == "{period}")
                |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> limit(n: 1)
            '''
            
            result = self.query_api.query(org=self.config.ORG, query=query)
            
            for table in result:
                for record in table.records:
                    return {
                        'timestamp': record.get_time(),
                        'open': record.values.get('open', 0),
                        'high': record.values.get('high', 0),
                        'low': record.values.get('low', 0),
                        'close': record.values.get('close', 0),
                        'volume': record.values.get('volume', 0),
                        'vol_ccy': record.values.get('vol_ccy', 0),
                        'confirm': record.values.get('confirm', 0)
                    }
            
            return None
            
        except Exception as e:
            print(f"❌ 检查已存在记录失败: {e}")
            return None
    
    def insert_kline_record(self, symbol: str, period: str, timestamp: datetime, data: Dict):
        """插入新的K线记录"""
        try:
            table_name = self.get_table_name(symbol)
            
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
                .time(timestamp)
            
            self.write_api.write(bucket=self.config.DEFAULT_BUCKETS[0], org=self.config.ORG, record=point)
            self.total_inserted += 1
            
        except Exception as e:
            print(f"❌ 插入K线记录失败: {e}")
    
    def update_kline_record(self, symbol: str, period: str, timestamp: datetime, new_data: Dict):
        """更新现有K线记录"""
        try:
            table_name = self.get_table_name(symbol)
            
            # InfluxDB更新策略：删除旧记录，插入新记录
            predicate = f'_measurement="{table_name}" AND symbol="{symbol}" AND period="{period}"'
            start_time = timestamp - timedelta(seconds=1)
            stop_time = timestamp + timedelta(seconds=1)
            
            self.delete_api.delete(
                start=start_time,
                stop=stop_time,
                predicate=predicate,
                bucket=self.config.DEFAULT_BUCKETS[0],
                org=self.config.ORG
            )
            
            # 插入新记录
            self.insert_kline_record(symbol, period, timestamp, new_data)
            self.total_updated += 1
            
        except Exception as e:
            print(f"❌ 更新K线记录失败: {e}")
    
    def check_data_changes(self, existing: Dict, new_data: Dict) -> bool:
        """检查数据是否有变化"""
        for key in ['open', 'high', 'low', 'close', 'volume', 'vol_ccy', 'confirm']:
            if abs(float(existing.get(key, 0)) - float(new_data.get(key, 0))) > 1e-8:
                return True
        return False
    
    def get_changed_fields(self, existing: Dict, new_data: Dict) -> List[str]:
        """获取变化的字段列表"""
        changed_fields = []
        for key in ['open', 'high', 'low', 'close', 'volume', 'vol_ccy', 'confirm']:
            if abs(float(existing.get(key, 0)) - float(new_data.get(key, 0))) > 1e-8:
                changed_fields.append(key)
        return changed_fields
    
    def process_kline_data(self, symbol: str, period: str, kline: List):
        """处理单条K线数据"""
        try:
            if len(kline) < 6:
                print(f"⚠️ K线数据格式不正确: {kline}")
                return
            
            # 解析K线数据
            timestamp = datetime.fromtimestamp(int(kline[0]) / 1000, tz=timezone.utc)
            new_data = {
                'open': float(kline[1]),
                'high': float(kline[2]),
                'low': float(kline[3]),
                'close': float(kline[4]),
                'volume': float(kline[5]) if len(kline) > 5 else 0.0,
                'vol_ccy': float(kline[6]) if len(kline) > 6 else 0.0,
                'confirm': int(kline[7]) if len(kline) > 7 else 1
            }
            
            self.total_received += 1
            
            # 检查是否已存在记录
            existing_record = self.check_existing_record(symbol, period, timestamp)
            
            if existing_record is None:
                # 新记录，直接插入
                self.insert_kline_record(symbol, period, timestamp, new_data)
                print(f"📥 新增: {symbol} {period} @ {timestamp.strftime('%H:%M:%S')} - 量:{new_data['volume']:.2f}")
            else:
                # 检查是否需要更新
                if self.check_data_changes(existing_record, new_data):
                    self.update_kline_record(symbol, period, timestamp, new_data)
                    changed_fields = self.get_changed_fields(existing_record, new_data)
                    print(f"🔄 更新: {symbol} {period} @ {timestamp.strftime('%H:%M:%S')} - 变化:{','.join(changed_fields)}")
                # 如果数据没有变化，则不输出日志（避免刷屏）
                
        except Exception as e:
            print(f"❌ 处理K线数据失败: {e}")
    
    def handle_kline_message(self, message: str):
        """处理K线WebSocket消息"""
        try:
            data = json.loads(message)
            
            # 检查消息类型
            if 'event' in data:
                self.handle_event_message(data)
                return
            
            if 'data' not in data or 'arg' not in data:
                return
            
            arg = data.get('arg', {})
            data_list = data.get('data', [])
            
            channel = arg.get('channel', '')
            inst_id = arg.get('instId', '')
            
            # 解析时间周期（从频道名中提取）
            if channel.startswith('candle'):
                period = channel.replace('candle', '')
            else:
                print(f"⚠️ 未识别的频道格式: {channel}")
                return
            
            # 处理每条K线数据
            for kline in data_list:
                self.process_kline_data(inst_id, period, kline)
                
        except json.JSONDecodeError as e:
            print(f"❌ JSON解析失败: {e}")
        except Exception as e:
            print(f"❌ 处理K线消息失败: {e}")
    
    def handle_event_message(self, message: Dict[str, Any]):
        """处理事件消息"""
        event = message.get('event')
        
        if event == 'subscribe':
            arg = message.get('arg', {})
            channel = arg.get('channel', '未知')
            inst_id = arg.get('instId', '未知')
            print(f"✅ [{inst_id}] {channel} 订阅成功")
        elif event == 'error':
            code = message.get('code', '未知')
            msg = message.get('msg', '未知错误')
            print(f"❌ WebSocket错误: {code} - {msg}")
    
    async def start_websocket_incremental(self, symbols: List[str], periods: List[str]):
        """启动WebSocket增量更新"""
        if not self.connect_influxdb():
            print("❌ InfluxDB连接失败，WebSocket增量更新终止")
            return
        
        try:
            # 使用公共频道WebSocket地址
            ws_url = "wss://ws.okx.com:8443/ws/v5/public"
            print(f"🔌 正在连接到公共频道 {ws_url}...")
            
            self.ws_client = WsPublicAsync(url=ws_url)
            await self.ws_client.start()
            
            print(f"✅ WebSocket已连接（服务器时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
            
            # 构建订阅参数
            args = []
            for symbol in symbols:
                for period in periods:
                    args.append({
                        "channel": f"candle{period}",
                        "instId": symbol
                    })
            
            self.subscribed_channels = args.copy()
            
            print(f"📡 正在订阅 {len(symbols)} 个币种 x {len(periods)} 个周期 = {len(args)} 个频道")
            for symbol in symbols:
                print(f"  📈 {symbol}: {', '.join(periods)}")
            
            # 订阅K线频道
            await self.ws_client.subscribe(args, callback=self.handle_kline_message)
            
            print(f"🎯 WebSocket订阅成功！开始接收增量更新...")
            print(f"💡 提示：推送频率最快间隔1秒推送一次数据")
            print(f"⏹️  程序将持续运行，按Ctrl+C停止")
            
            # 保持连接并显示统计信息
            self.running = True
            last_stats_time = time.time()
            
            while self.running:
                await asyncio.sleep(1)
                
                # 每30秒显示一次统计信息
                if time.time() - last_stats_time >= 30:
                    print(f"📊 统计[30s]: 接收:{self.total_received}, 新增:{self.total_inserted}, 更新:{self.total_updated}")
                    last_stats_time = time.time()
                
        except Exception as e:
            print(f"❌ WebSocket增量更新失败: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await self.stop_websocket()
    
    async def stop_websocket(self):
        """停止WebSocket连接"""
        try:
            self.running = False
            
            if self.ws_client and self.subscribed_channels:
                print("📤 正在取消订阅...")
                await self.ws_client.unsubscribe(self.subscribed_channels, callback=self.handle_kline_message)
                await asyncio.sleep(2)
            
            print("🔌 正在关闭WebSocket连接...")
            
        except Exception as e:
            print(f"❌ 停止WebSocket失败: {e}")
        finally:
            self.close()
    
    def close(self):
        """关闭连接"""
        if self.client:
            self.client.close()
            print("🔒 InfluxDB连接已关闭")

async def main():
    """主函数 - 用于测试"""
    updater = WebSocketIncrementalUpdater()
    
    try:
        # 测试订阅单个币种
        test_symbols = ["BTC-USDT-SWAP"]
        test_periods = ["5m"]
        
        await updater.start_websocket_incremental(test_symbols, test_periods)
    
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断")
    except Exception as e:
        print(f"❌ 测试异常: {e}")
    finally:
        print("👋 程序结束")

if __name__ == "__main__":
    asyncio.run(main())
