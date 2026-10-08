#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NEAR-USDT-SWAP指数K线数据获取与InfluxDB存储
=========================================

功能：
1. 获取NEAR-USDT-SWAP指数的多周期K线数据
2. 存储到InfluxDB的garble桶中
3. 查询并显示各周期最新10条数据

作者：AI Assistant
创建时间：2025年1月
修改：使用指数K线数据，增加调试输出
"""

import sys
import os
from datetime import datetime, timedelta, timezone
import json
import time
import pandas as pd

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

class NEARIndexKlineInfluxDB:
    """NEAR-USDT-SWAP指数K线数据获取与InfluxDB存储类"""
    
    def __init__(self):
        print("🔧 初始化NEAR指数K线数据处理器...")
        
        # OKX API配置
        self.flag = "0"  # 实盘:0 , 模拟盘:1
        print(f"📡 初始化OKX API，模式: {'实盘' if self.flag == '0' else '模拟盘'}")
        self.market_api = MarketData.MarketAPI(flag=self.flag)
        print("✅ OKX MarketAPI初始化成功")
        
        # 使用配置文件中的InfluxDB设置
        print("📋 加载InfluxDB配置...")
        self.config = InfluxDBConfig()
        self.url = self.config.URL
        self.token = self.config.TOKEN
        self.org = self.config.ORG
        self.bucket = self.config.DEFAULT_BUCKETS[0]  # 使用第一个桶 "garble"
        print(f"✅ InfluxDB配置加载完成")
        print(f"   URL: {self.url}")
        print(f"   组织: {self.org}")
        print(f"   目标桶: {self.bucket}")
        
        # 交易对和周期配置 - NEAR-USDT-SWAP K线数据
        self.inst_id = "NEAR-USDT"
        self.time_periods = ["5m", "15m", "1H", "4H", "1D", "1W"]
        self.measurement = "near_usdt_swap_kline"  # 按要求设置measurement名称
        
        # InfluxDB客户端
        self.client = None
        self.query_api = None
        self.write_api = None
        
        print(f"🔧 NEAR K线数据处理器初始化完成")
        print(f"   合约代码: {self.inst_id}")
        print(f"   周期: {', '.join(self.time_periods)}")
        print(f"   数据表: {self.measurement}")
        
    def connect_influxdb(self):
        """连接到InfluxDB"""
        try:
            print(f"\n🔗 正在连接InfluxDB...")
            print(f"   URL: {self.url}")
            print(f"   组织: {self.org}")
            
            # 使用配置文件创建客户端
            client_config = self.config.get_client_config()
            print(f"📋 客户端配置: {list(client_config.keys())}")
            
            self.client = InfluxDBClient(**client_config)
            print("✅ InfluxDB客户端创建成功")
            
            # 测试连接
            print("🔍 测试InfluxDB连接...")
            health = self.client.health()
            print(f"✅ InfluxDB连接成功")
            print(f"   状态: {health.status}")
            print(f"   版本: {health.version}")
            
            # 自动发现组织
            print("🔍 查询可用组织...")
            orgs = self.client.organizations_api().find_organizations()
            if orgs:
                self.org = orgs[0].name
                print(f"📋 使用组织: {self.org}")
                print(f"   可用组织数量: {len(orgs)}")
            else:
                print("⚠️ 未找到可用组织")
            
            # 初始化API
            print("🔧 初始化InfluxDB API...")
            self.query_api = self.client.query_api()
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
            print("✅ InfluxDB API初始化完成")
            
            return True
            
        except Exception as e:
            print(f"❌ InfluxDB连接失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_kline_data(self, time_period, limit=100):
        """获取指定周期的K线数据"""
        try:
            print(f"\n📊 开始获取K线数据...")
            print(f"   合约代码: {self.inst_id}")
            print(f"   时间周期: {time_period}")
            print(f"   数据条数: {limit}")
            
            # 尝试指数K线API，如果失败则回退到标记价格API
            print(f"🌐 尝试调用get_index_candlesticks API...")
            start_time = time.time()
            
            # 先尝试指数K线API
            result = self.market_api.get_index_candlesticks(
                instId=self.inst_id, 
                bar=time_period,
                limit=str(limit)
            )
            
            # 检查是否返回错误，如果是51001错误则尝试标记价格API
            if 'code' in result and result['code'] == '51001':
                print(f"⚠️ 指数K线API返回错误: {result.get('msg', '未知错误')}")
                print(f"🔄 回退使用标记价格K线API...")
                
                result = self.market_api.get_mark_price_candlesticks(
                    instId=self.inst_id, 
                    bar=time_period,
                    limit=str(limit)
                )
                print(f"✅ 标记价格API调用完成")
            
            api_time = time.time() - start_time
            print(f"✅ API调用完成，耗时: {api_time:.2f}秒")
            
            # 检查API响应
            print(f"🔍 检查API响应...")
            print(f"   响应类型: {type(result)}")
            print(f"   响应键: {list(result.keys()) if isinstance(result, dict) else 'N/A'}")
            
            if 'code' in result:
                print(f"   响应代码: {result['code']}")
                if result['code'] != '0':
                    print(f"❌ API返回错误代码: {result['code']}")
                    if 'msg' in result:
                        print(f"   错误信息: {result['msg']}")
                    return None
            
            if 'data' not in result:
                print(f"❌ API响应中缺少data字段")
                print(f"   完整响应: {result}")
                return None
                
            kline_data = result['data']
            print(f"📈 获取到原始数据条数: {len(kline_data) if kline_data else 0}")
            
            if not kline_data:
                print(f"❌ 无法获取 {self.inst_id} 的 {time_period} K线数据")
                print(f"   可能原因: 合约代码不正确或该周期无数据")
                return None
            
            # 显示数据样本
            print(f"📋 数据样本 (前3条):")
            for i, candle in enumerate(kline_data[:3]):
                timestamp = int(candle[0])
                dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                print(f"   [{i+1}] 时间: {dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                print(f"       开盘: {candle[1]}, 最高: {candle[2]}, 最低: {candle[3]}, 收盘: {candle[4]}")
                print(f"       数据长度: {len(candle)} 字段")
                if len(candle) > 6:
                    print(f"       成交量: {candle[5]}, 成交额: {candle[6]}")
                elif len(candle) > 5:
                    print(f"       成交量: {candle[5]}")
                else:
                    print(f"       注意: 标记价格K线只有价格数据，无成交量信息")
            
            print(f"✅ 成功获取 {len(kline_data)} 条 {time_period} K线数据")
            return kline_data
            
        except Exception as e:
            print(f"❌ 获取K线数据失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def convert_kline_to_points(self, kline_data, time_period):
        """将K线数据转换为InfluxDB Point对象"""
        print(f"\n🔄 开始转换K线数据为InfluxDB Point对象...")
        print(f"   输入数据条数: {len(kline_data)}")
        print(f"   时间周期: {time_period}")
        
        points = []
        conversion_errors = 0
        
        try:
            for i, candle in enumerate(kline_data):
                try:
                    # 显示转换进度
                    if i < 3 or i % 20 == 0:
                        print(f"   转换进度: {i+1}/{len(kline_data)}")
                    
                    # K线数据格式: [时间戳, 开盘价, 最高价, 最低价, 收盘价] (标记价格无成交量)
                    timestamp = int(candle[0])
                    open_price = float(candle[1]) if candle[1] else 0.0
                    high_price = float(candle[2]) if candle[2] else 0.0
                    low_price = float(candle[3]) if candle[3] else 0.0
                    close_price = float(candle[4]) if candle[4] else 0.0
                    volume = float(candle[5]) if len(candle) > 5 and candle[5] else 0.0
                    vol_ccy = float(candle[6]) if len(candle) > 6 and candle[6] else 0.0
                    confirm = int(candle[7]) if len(candle) > 7 and candle[7] else 1  # 标记价格默认确认
                    
                    # 转换时间戳为datetime对象
                    dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                    
                    # 数据验证
                    if i < 3:
                        print(f"   数据验证 [{i+1}]:")
                        print(f"     时间戳: {timestamp} -> {dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                        print(f"     价格: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                        print(f"     成交量: {volume}, 成交额: {vol_ccy}, 确认: {confirm}")
                    
                    # 创建Point对象
                    point = Point(self.measurement) \
                        .tag("period", time_period) \
                        .tag("inst_id", self.inst_id) \
                        .field("open", open_price) \
                        .field("high", high_price) \
                        .field("low", low_price) \
                        .field("close", close_price) \
                        .field("volume", volume) \
                        .field("vol_ccy", vol_ccy) \
                        .field("confirm", confirm) \
                        .time(dt)
                    
                    points.append(point)
                    
                except Exception as e:
                    conversion_errors += 1
                    print(f"⚠️ 转换第{i+1}条数据时出错: {e}")
                    print(f"   原始数据: {candle}")
                    continue
            
            print(f"✅ 数据转换完成")
            print(f"   成功转换: {len(points)} 个数据点")
            print(f"   转换错误: {conversion_errors} 条")
            print(f"   转换成功率: {(len(points)/(len(points)+conversion_errors)*100):.1f}%")
            
            return points
            
        except Exception as e:
            print(f"❌ 数据转换失败: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def store_kline_data(self, time_period):
        """获取并存储指定周期的K线数据"""
        try:
            print(f"\n" + "="*60)
            print(f"📈 处理 {time_period} 周期K线数据")
            print("="*60)
            
            # 1. 获取K线数据
            print(f"🔄 步骤1: 获取K线数据")
            kline_data = self.get_kline_data(time_period)
            if not kline_data:
                print(f"❌ 步骤1失败: 无法获取{time_period}周期数据")
                return False
            print(f"✅ 步骤1完成: 获取到{len(kline_data)}条数据")
            
            # 2. 转换为InfluxDB Point对象
            print(f"🔄 步骤2: 转换数据格式")
            points = self.convert_kline_to_points(kline_data, time_period)
            if not points:
                print(f"❌ 步骤2失败: 数据转换失败")
                return False
            print(f"✅ 步骤2完成: 转换为{len(points)}个数据点")
            
            # 3. 写入InfluxDB
            print(f"🔄 步骤3: 写入InfluxDB")
            print(f"   目标桶: {self.bucket}")
            print(f"   组织: {self.org}")
            print(f"   数据点数量: {len(points)}")
            
            write_start = time.time()
            self.write_api.write(bucket=self.bucket, org=self.org, record=points)
            write_time = time.time() - write_start
            
            print(f"✅ 步骤3完成: 数据写入成功，耗时: {write_time:.2f}秒")
            print(f"✅ {time_period} 周期K线数据存储成功")
            
            return True
            
        except Exception as e:
            print(f"❌ {time_period} 周期数据存储失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def query_latest_data(self, time_period, limit=3):
        """查询指定周期的最新数据"""
        try:
            print(f"\n🔍 查询 {time_period} 周期最新 {limit} 条数据...")
            print(f"   数据表: {self.measurement}")
            print(f"   合约代码: {self.inst_id}")
            
            # Flux查询语句
            query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -30d)
                |> filter(fn: (r) => r._measurement == "{self.measurement}")
                |> filter(fn: (r) => r.period == "{time_period}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"], desc: true)
                |> limit(n: {limit})
            '''
            
            print(f"📋 执行Flux查询...")
            print(f"   查询语句长度: {len(query)} 字符")
            
            query_start = time.time()
            result = self.query_api.query(org=self.org, query=query)
            query_time = time.time() - query_start
            
            print(f"✅ 查询执行完成，耗时: {query_time:.2f}秒")
            
            records = []
            table_count = 0
            for table in result:
                table_count += 1
                for record in table.records:
                    records.append(record)
            
            print(f"📊 查询结果统计:")
            print(f"   表数量: {table_count}")
            print(f"   记录数量: {len(records)}")
            
            if records:
                print(f"✅ 查询到 {len(records)} 条 {time_period} 数据")
                print(f"\n📊 {time_period} 周期最新 {len(records)} 条K线数据:")
                print("-" * 110)
                print(f"{'时间':<20} {'开盘价':<12} {'最高价':<12} {'最低价':<12} {'收盘价':<12} {'成交量':<15} {'确认':<8}")
                print("-" * 110)
                
                for i, record in enumerate(records):
                    time_str = record.get_time().strftime('%Y-%m-%d %H:%M:%S')
                    open_val = record.values.get('open', 0)
                    high_val = record.values.get('high', 0)
                    low_val = record.values.get('low', 0)
                    close_val = record.values.get('close', 0)
                    volume_val = record.values.get('volume', 0)
                    confirm_val = record.values.get('confirm', 0)
                    
                    print(f"{time_str:<20} {open_val:<12.4f} {high_val:<12.4f} {low_val:<12.4f} {close_val:<12.4f} {volume_val:<15.2f} {confirm_val:<8}")
                    
                    # 显示最新数据的详细信息
                    if i == 0:
                        print(f"\n📋 最新数据详情:")
                        print(f"   时间: {time_str} UTC")
                        print(f"   收盘价: {close_val:.4f}")
                        print(f"   确认状态: {'已确认' if confirm_val == 1 else '未确认'}")
                
                return True
            else:
                print(f"⚠️ 未找到 {time_period} 周期的K线数据")
                print(f"   可能原因:")
                print(f"   1. 数据尚未写入完成")
                print(f"   2. 查询条件不匹配")
                print(f"   3. 数据表名或字段名不正确")
                return False
                
        except Exception as e:
            print(f"❌ 查询 {time_period} 数据失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def run_full_process(self):
        """运行完整的数据获取、存储和查询流程"""
        print("\n" + "="*80)
        print("🚀 NEAR-USDT-SWAP K线数据处理流程")
        print("="*80)
        print(f"⏰ 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📊 处理合约: {self.inst_id}")
        print(f"📈 时间周期: {', '.join(self.time_periods)}")
        
        # 统计信息
        stats = {
            "连接状态": False,
            "存储成功": 0,
            "存储失败": 0,
            "查询成功": 0,
            "查询失败": 0,
            "处理详情": []
        }
        
        try:
            # 1. 连接InfluxDB
            print(f"\n🔄 步骤1: 连接InfluxDB")
            if not self.connect_influxdb():
                print(f"❌ InfluxDB连接失败，程序终止")
                return False
            stats["连接状态"] = True
            print(f"✅ InfluxDB连接成功")
            
            # 2. 处理每个时间周期
            for i, period in enumerate(self.time_periods):
                print(f"\n🔄 步骤2.{i+1}: 处理 {period} 周期 ({i+1}/{len(self.time_periods)})")
                period_start = time.time()
                
                period_stats = {
                    "周期": period,
                    "存储": False,
                    "查询": False,
                    "耗时": 0
                }
                
                # 存储数据
                print(f"   🔄 存储{period}周期数据...")
                if self.store_kline_data(period):
                    stats["存储成功"] += 1
                    period_stats["存储"] = True
                    print(f"   ✅ {period}周期数据存储成功")
                    
                    # 等待数据写入完成
                    print(f"   ⏳ 等待数据写入完成...")
                    time.sleep(2)
                    
                    # 查询验证
                    print(f"   🔄 验证{period}周期数据...")
                    if self.query_latest_data(period):
                        stats["查询成功"] += 1
                        period_stats["查询"] = True
                        print(f"   ✅ {period}周期数据查询成功")
                    else:
                        stats["查询失败"] += 1
                        print(f"   ❌ {period}周期数据查询失败")
                else:
                    stats["存储失败"] += 1
                    stats["查询失败"] += 1
                    print(f"   ❌ {period}周期数据存储失败")
                
                period_stats["耗时"] = time.time() - period_start
                stats["处理详情"].append(period_stats)
                
                print(f"   ⏱️ {period}周期处理完成，耗时: {period_stats['耗时']:.2f}秒")
            
            # 3. 显示统计信息
            print("\n" + "="*80)
            print("📊 处理统计报告")
            print("="*80)
            print(f"   InfluxDB连接: {'✅ 成功' if stats['连接状态'] else '❌ 失败'}")
            print(f"   数据存储成功: {stats['存储成功']}/{len(self.time_periods)}")
            print(f"   数据存储失败: {stats['存储失败']}/{len(self.time_periods)}")
            print(f"   数据查询成功: {stats['查询成功']}/{len(self.time_periods)}")
            print(f"   数据查询失败: {stats['查询失败']}/{len(self.time_periods)}")
            
            print(f"\n📋 各周期处理详情:")
            for detail in stats["处理详情"]:
                status = "✅" if detail["存储"] and detail["查询"] else "❌"
                print(f"   {status} {detail['周期']:<4} - 存储:{'✅' if detail['存储'] else '❌'} 查询:{'✅' if detail['查询'] else '❌'} 耗时:{detail['耗时']:.1f}s")
            
            total_time = sum(d["耗时"] for d in stats["处理详情"])
            print(f"\n⏰ 总处理时间: {total_time:.2f}秒")
            print(f"⏰ 完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            success_rate = (stats['存储成功'] + stats['查询成功']) / (len(self.time_periods) * 2) * 100
            print(f"📈 总体成功率: {success_rate:.1f}%")
            
            if stats['存储成功'] == len(self.time_periods) and stats['查询成功'] == len(self.time_periods):
                print("✅ 所有K线数据处理完成！")
            else:
                print("⚠️ 部分数据处理失败，请检查上述日志")
            
            return True
            
        except Exception as e:
            print(f"\n❌ 处理流程执行失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def close(self):
        """关闭连接"""
        if self.client:
            self.client.close()
            print("🔒 InfluxDB连接已关闭")

def main():
    """主函数"""
    print("🎯 启动NEAR K线数据处理程序")
    processor = NEARIndexKlineInfluxDB()
    
    try:
        success = processor.run_full_process()
        if success:
            print("\n🎉 所有操作执行成功！K线数据已存储到InfluxDB")
            print("💡 提示: K线数据已按要求存储到garble桶的near_usdt_swap_kline表中")
        else:
            print("\n⚠️ 部分操作执行失败，请检查上述详细日志")
            
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断程序")
    except Exception as e:
        print(f"\n💥 程序异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        processor.close()
        print("👋 程序结束")

if __name__ == "__main__":
    main()