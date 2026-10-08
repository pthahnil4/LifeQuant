#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
InfluxDB 2.x CRUD 操作演示
========================

功能：
1. 连接InfluxDB 2.x数据库
2. 对garble和scs桶进行CRUD操作演示
3. 插入、查询、修改、删除记录

作者：AI Assistant
创建时间：2025年1月
"""

import sys
from datetime import datetime, timedelta, timezone
import json
import time
import uuid

# 导入配置
from influxdb_config import InfluxDBConfig

try:
    from influxdb_client import InfluxDBClient, Point, DeleteApi
    from influxdb_client.client.write_api import SYNCHRONOUS
    from influxdb_client.client.query_api import QueryApi
    print("✅ InfluxDB 2.x 客户端库导入成功")
except ImportError as e:
    print("❌ InfluxDB客户端库导入失败")
    print("请安装: pip install influxdb-client")
    sys.exit(1)

class InfluxDBCRUDDemo:
    """InfluxDB 2.x CRUD 操作演示类"""
    
    def __init__(self):
        # 使用配置文件中的设置
        self.config = InfluxDBConfig()
        self.url = self.config.URL
        self.token = self.config.TOKEN
        self.org = self.config.ORG
        
        # 用户指定的桶
        self.buckets = self.config.DEFAULT_BUCKETS
        
        self.client = None
        self.query_api = None
        self.write_api = None
        self.delete_api = None
        
        # 用于演示的测试数据ID
        self.test_record_id = str(uuid.uuid4())
        
    def connect(self):
        """连接到InfluxDB"""
        try:
            print(f"🔗 正在连接InfluxDB: {self.url}")
            
            # 使用配置文件创建客户端
            client_config = self.config.get_client_config()
            self.client = InfluxDBClient(**client_config)
            
            # 测试连接
            health = self.client.health()
            print(f"✅ InfluxDB连接成功")
            print(f"   状态: {health.status}")
            print(f"   版本: {health.version}")
            
            # 自动发现组织
            orgs = self.client.organizations_api().find_organizations()
            if orgs:
                self.org = orgs[0].name
                print(f"📋 使用组织: {self.org}")
            
            # 初始化API
            self.query_api = self.client.query_api()
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
            self.delete_api = self.client.delete_api()
            
            return True
            
        except Exception as e:
            print(f"❌ InfluxDB连接失败: {e}")
            return False
    
    def ensure_buckets_exist(self):
        """确保指定的桶存在"""
        try:
            buckets_api = self.client.buckets_api()
            existing_buckets = buckets_api.find_buckets().buckets
            existing_names = [b.name for b in existing_buckets] if existing_buckets else []
            
            print(f"📊 现有桶: {existing_names}")
            
            for bucket_name in self.buckets:
                if bucket_name not in existing_names:
                    print(f"🆕 创建桶: {bucket_name}")
                    # 使用配置文件中的保留规则创建桶
                    retention_rules = self.config.get_retention_rules()
                    buckets_api.create_bucket(
                        bucket_name=bucket_name,
                        org=self.org,
                        retention_rules=retention_rules
                    )
                else:
                    print(f"✅ 桶已存在: {bucket_name}")
                    
        except Exception as e:
            print(f"⚠️ 桶管理警告: {e}")
    
    def create_sample_data(self, bucket_name):
        """创建示例数据"""
        try:
            print(f"\n📝 向桶 '{bucket_name}' 插入示例数据...")
            
            # 创建多个测试点
            points = []
            
            # 当前时间点
            current_time = datetime.now(timezone.utc)
            
            for i in range(3):
                point = Point("demo_measurement") \
                    .tag("location", f"server_{i+1}") \
                    .tag("record_id", self.test_record_id) \
                    .field("temperature", 20.0 + i * 2.5) \
                    .field("humidity", 45.0 + i * 5.0) \
                    .field("cpu_usage", 30.0 + i * 10.0) \
                    .time(current_time - timedelta(minutes=i*5))
                
                points.append(point)
            
            # 写入数据
            self.write_api.write(bucket=bucket_name, org=self.org, record=points)
            print(f"✅ 成功插入 {len(points)} 条记录")
            
            return True
            
        except Exception as e:
            print(f"❌ 数据插入失败: {e}")
            return False
    
    def query_data(self, bucket_name):
        """查询数据"""
        try:
            print(f"\n🔍 查询桶 '{bucket_name}' 中的数据...")
            
            # 查询最近1小时的数据
            query = f'''
            from(bucket: "{bucket_name}")
                |> range(start: -1h)
                |> filter(fn: (r) => r._measurement == "demo_measurement")
                |> filter(fn: (r) => r.record_id == "{self.test_record_id}")
            '''
            
            result = self.query_api.query(org=self.org, query=query)
            
            records_count = 0
            for table in result:
                for record in table.records:
                    records_count += 1
                    print(f"   📊 {record.get_time()}: {record.get_field()}={record.get_value()} (标签: {record.values.get('location', 'N/A')})")
            
            print(f"✅ 查询完成，找到 {records_count} 条记录")
            return records_count > 0
            
        except Exception as e:
            print(f"❌ 数据查询失败: {e}")
            return False
    
    def update_data(self, bucket_name):
        """更新数据（通过插入新值实现）"""
        try:
            print(f"\n🔄 更新桶 '{bucket_name}' 中的数据...")
            
            # 在InfluxDB中，更新通常通过插入相同时间戳的新值来实现
            current_time = datetime.now(timezone.utc)
            
            point = Point("demo_measurement") \
                .tag("location", "server_updated") \
                .tag("record_id", self.test_record_id) \
                .field("temperature", 99.9) \
                .field("humidity", 88.8) \
                .field("cpu_usage", 77.7) \
                .field("status", "updated") \
                .time(current_time)
            
            self.write_api.write(bucket=bucket_name, org=self.org, record=point)
            print("✅ 数据更新成功（插入了新的数据点）")
            
            return True
            
        except Exception as e:
            print(f"❌ 数据更新失败: {e}")
            return False
    
    def delete_data(self, bucket_name):
        """删除数据"""
        try:
            print(f"\n🗑️ 删除桶 '{bucket_name}' 中的测试数据...")
            
            # 删除指定时间范围内的数据
            start_time = datetime.now(timezone.utc) - timedelta(hours=2)
            stop_time = datetime.now(timezone.utc)
            
            # 删除条件：指定的record_id
            predicate = f'record_id="{self.test_record_id}"'
            
            self.delete_api.delete(
                start=start_time,
                stop=stop_time,
                predicate=predicate,
                bucket=bucket_name,
                org=self.org
            )
            
            print("✅ 测试数据删除成功")
            return True
            
        except Exception as e:
            print(f"❌ 数据删除失败: {e}")
            return False
    
    def run_crud_demo(self):
        """运行完整的CRUD演示"""
        print("\n" + "="*60)
        print("🚀 InfluxDB 2.x CRUD 操作演示")
        print("="*60)
        print(f"⏰ 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"🆔 测试记录ID: {self.test_record_id}")
        
        # 统计信息
        stats = {
            "连接": False,
            "桶管理": False,
            "插入操作": 0,
            "查询操作": 0,
            "更新操作": 0,
            "删除操作": 0
        }
        
        try:
            # 1. 连接数据库
            if not self.connect():
                return False
            stats["连接"] = True
            
            # 2. 确保桶存在
            self.ensure_buckets_exist()
            stats["桶管理"] = True
            
            # 3. 对每个桶进行CRUD操作
            for bucket_name in self.buckets:
                print(f"\n" + "="*40)
                print(f"📦 处理桶: {bucket_name}")
                print("="*40)
                
                # Create - 插入数据
                if self.create_sample_data(bucket_name):
                    stats["插入操作"] += 1
                
                # 等待数据写入完成
                time.sleep(1)
                
                # Read - 查询数据
                if self.query_data(bucket_name):
                    stats["查询操作"] += 1
                
                # Update - 更新数据
                if self.update_data(bucket_name):
                    stats["更新操作"] += 1
                
                # 等待更新完成
                time.sleep(1)
                
                # 再次查询以验证更新
                print(f"\n🔍 验证更新后的数据...")
                self.query_data(bucket_name)
                
                # Delete - 删除数据
                if self.delete_data(bucket_name):
                    stats["删除操作"] += 1
            
            # 显示统计信息
            print("\n" + "="*60)
            print("📊 操作统计")
            print("="*60)
            for operation, count in stats.items():
                if isinstance(count, bool):
                    status = "✅ 成功" if count else "❌ 失败"
                    print(f"   {operation}: {status}")
                else:
                    print(f"   {operation}: {count}/{len(self.buckets)}")
            
            print(f"\n⏰ 完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("✅ CRUD 演示程序执行完成")
            
            return True
            
        except Exception as e:
            print(f"\n❌ 演示程序执行失败: {e}")
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
    demo = InfluxDBCRUDDemo()
    
    try:
        success = demo.run_crud_demo()
        if success:
            print("\n🎉 所有操作执行成功！")
        else:
            print("\n⚠️ 部分操作执行失败，请检查日志")
            
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断程序")
    except Exception as e:
        print(f"\n💥 程序异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        demo.close()

if __name__ == "__main__":
    main()