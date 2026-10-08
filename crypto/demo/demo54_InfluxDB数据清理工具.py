#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
InfluxDB数据清理工具
===================

功能：
1. 根据measurement名称删除时序数据
2. 可选择指定tag进行精确删除
3. 支持时间范围删除
4. 删除前显示将要删除的数据统计

作者：AI Assistant
创建时间：2025年1月
"""

import sys
import os
from datetime import datetime, timedelta, timezone
import time

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入配置
from influxdb_config import InfluxDBConfig

# 导入InfluxDB客户端
try:
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.delete_api import DeleteApi
    from influxdb_client.client.query_api import QueryApi
    print("✅ InfluxDB 2.x 客户端库导入成功")
except ImportError as e:
    print("❌ InfluxDB客户端库导入失败")
    print("请安装: pip install influxdb-client")
    sys.exit(1)

class InfluxDBDataCleaner:
    """InfluxDB数据清理工具类"""
    
    def __init__(self):
        print("🔧 初始化InfluxDB数据清理工具...")
        
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
        
        # InfluxDB客户端
        self.client = None
        self.query_api = None
        self.delete_api = None
        
        print(f"🔧 InfluxDB数据清理工具初始化完成")
        
    def connect_influxdb(self):
        """连接到InfluxDB"""
        try:
            print(f"\n🔗 正在连接InfluxDB...")
            
            # 使用配置文件创建客户端
            client_config = self.config.get_client_config()
            self.client = InfluxDBClient(**client_config)
            print("✅ InfluxDB客户端创建成功")
            
            # 测试连接
            print("🔍 测试InfluxDB连接...")
            health = self.client.health()
            print(f"✅ InfluxDB连接成功")
            print(f"   状态: {health.status}")
            print(f"   版本: {health.version}")
            
            # 初始化API
            print("🔧 初始化InfluxDB API...")
            self.query_api = self.client.query_api()
            self.delete_api = self.client.delete_api()
            print("✅ InfluxDB API初始化完成")
            
            return True
            
        except Exception as e:
            print(f"❌ InfluxDB连接失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def query_data_statistics(self, measurement, tag_filters=None, time_start=None, time_stop=None):
        """查询将要删除的数据统计"""
        try:
            print(f"\n📊 查询数据统计...")
            print(f"   测量表: {measurement}")
            
            # 构建查询条件
            conditions = [f'r._measurement == "{measurement}"']
            
            if tag_filters:
                for tag_key, tag_value in tag_filters.items():
                    conditions.append(f'r.{tag_key} == "{tag_value}"')
                    print(f"   标签过滤: {tag_key} = {tag_value}")
            else:
                print(f"   标签过滤: 无 (删除所有标签)")
            
            # 时间范围
            if time_start and time_stop:
                time_range = f'range(start: {time_start}, stop: {time_stop})'
                print(f"   时间范围: {time_start} 到 {time_stop}")
            else:
                # 查询所有历史数据
                time_range = 'range(start: 1970-01-01T00:00:00Z)'
                print(f"   时间范围: 所有数据 (从1970年开始)")
            
            # 构建Flux查询语句
            filter_conditions = ' and '.join(conditions)
            query = f'''
            from(bucket: "{self.bucket}")
                |> {time_range}
                |> filter(fn: (r) => {filter_conditions})
                |> count()
            '''
            
            print(f"📋 执行统计查询...")
            result = self.query_api.query(org=self.org, query=query)
            
            total_points = 0
            table_count = 0
            field_stats = {}
            
            for table in result:
                table_count += 1
                for record in table.records:
                    field_name = record.values.get('_field', 'unknown')
                    count = record.values.get('_value', 0)
                    field_stats[field_name] = field_stats.get(field_name, 0) + count
                    total_points += count
            
            print(f"✅ 数据统计完成")
            print(f"📊 统计结果:")
            print(f"   数据表数量: {table_count}")
            print(f"   总数据点数: {total_points:,}")
            
            if field_stats:
                print(f"   字段分布:")
                for field, count in field_stats.items():
                    print(f"     {field}: {count:,} 点")
            
            return total_points > 0, total_points, field_stats
            
        except Exception as e:
            print(f"❌ 查询数据统计失败: {e}")
            import traceback
            traceback.print_exc()
            return False, 0, {}
    
    def delete_data(self, measurement, tag_filters=None, time_start=None, time_stop=None, confirm=True):
        """删除数据"""
        try:
            print(f"\n🗑️ 准备删除数据...")
            print(f"   测量表: {measurement}")
            
            # 构建删除谓词
            predicate_parts = [f'_measurement="{measurement}"']
            
            if tag_filters:
                for tag_key, tag_value in tag_filters.items():
                    predicate_parts.append(f'{tag_key}="{tag_value}"')
                    print(f"   标签过滤: {tag_key} = {tag_value}")
            else:
                print(f"   标签过滤: 无 (删除所有标签)")
            
            predicate = ' AND '.join(predicate_parts)
            print(f"   删除谓词: {predicate}")
            
            # 时间范围
            if time_start and time_stop:
                start_time = datetime.fromisoformat(time_start.replace('Z', '+00:00'))
                stop_time = datetime.fromisoformat(time_stop.replace('Z', '+00:00'))
                print(f"   时间范围: {time_start} 到 {time_stop}")
            else:
                # 删除所有历史数据
                start_time = datetime(1970, 1, 1, tzinfo=timezone.utc)
                stop_time = datetime.now(tz=timezone.utc)
                print(f"   时间范围: 所有数据 (从1970年到现在)")
            
            # 确认删除
            if confirm:
                print(f"\n⚠️ 确认删除信息:")
                print(f"   桶: {self.bucket}")
                print(f"   组织: {self.org}")
                print(f"   删除条件: {predicate}")
                print(f"   时间范围: {start_time} 到 {stop_time}")
                
                response = input("\n❓ 确定要删除这些数据吗? (输入 'YES' 确认): ")
                if response != 'YES':
                    print("❌ 删除操作已取消")
                    return False
            
            print(f"\n🔄 执行删除操作...")
            delete_start = time.time()
            
            # 执行删除
            self.delete_api.delete(
                start=start_time,
                stop=stop_time,
                predicate=predicate,
                bucket=self.bucket,
                org=self.org
            )
            
            delete_time = time.time() - delete_start
            print(f"✅ 删除操作完成，耗时: {delete_time:.2f}秒")
            
            return True
            
        except Exception as e:
            print(f"❌ 删除数据失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def clean_measurement_data(self, measurement, tag_filters=None, time_start=None, time_stop=None, force=False):
        """清理指定measurement的数据 - 主要功能接口"""
        print(f"\n" + "="*80)
        print(f"🧹 数据清理任务")
        print(f"="*80)
        print(f"⏰ 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📊 目标测量表: {measurement}")
        
        try:
            # 1. 连接InfluxDB
            if not self.connect_influxdb():
                print(f"❌ 无法连接到InfluxDB，任务终止")
                return False
            
            # 2. 查询数据统计
            print(f"\n🔄 步骤1: 查询数据统计")
            has_data, total_points, field_stats = self.query_data_statistics(
                measurement, tag_filters, time_start, time_stop
            )
            
            if not has_data:
                print(f"ℹ️ 未找到匹配的数据，无需删除")
                return True
            
            print(f"⚠️ 找到 {total_points:,} 个数据点将被删除")
            
            # 3. 执行删除
            print(f"\n🔄 步骤2: 执行删除操作")
            success = self.delete_data(
                measurement, tag_filters, time_start, time_stop, confirm=not force
            )
            
            if success:
                # 等待删除完成
                print(f"⏳ 等待删除操作生效...")
                time.sleep(2)
                
                # 验证删除结果
                print(f"\n🔄 步骤3: 验证删除结果")
                has_data_after, remaining_points, _ = self.query_data_statistics(
                    measurement, tag_filters, time_start, time_stop
                )
                
                if not has_data_after or remaining_points == 0:
                    print(f"✅ 数据清理完成！已删除 {total_points:,} 个数据点")
                else:
                    print(f"⚠️ 部分数据可能未完全删除，剩余: {remaining_points:,} 个数据点")
                
                return True
            else:
                print(f"❌ 删除操作失败")
                return False
                
        except Exception as e:
            print(f"❌ 清理任务执行失败: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        finally:
            print(f"⏰ 结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    def close(self):
        """关闭连接"""
        if self.client:
            self.client.close()
            print("🔒 InfluxDB连接已关闭")

def main():
    """主函数"""
    print("🎯 启动InfluxDB数据清理工具")
    
    cleaner = InfluxDBDataCleaner()
    
    try:
        # 清理配置
        measurement = "near_usdt_swap_kline"
        tag_filters = None  # 不指定tag，删除所有相关数据
        force = False  # 是否强制删除（跳过确认）
        
        print(f"\n📋 清理配置:")
        print(f"   目标测量表: {measurement}")
        print(f"   标签过滤: {'无 (删除所有标签)' if not tag_filters else tag_filters}")
        print(f"   强制模式: {'是' if force else '否 (需要确认)'}")
        
        # 执行清理
        success = cleaner.clean_measurement_data(
            measurement=measurement,
            tag_filters=tag_filters,
            force=force
        )
        
        if success:
            print(f"\n🎉 数据清理任务完成！")
            print(f"💡 提示: {measurement} 测量表的数据已清理完毕")
        else:
            print(f"\n⚠️ 数据清理任务失败，请检查上述日志")
            
    except KeyboardInterrupt:
        print(f"\n⏹️ 用户中断程序")
    except Exception as e:
        print(f"\n💥 程序异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        cleaner.close()
        print("👋 程序结束")

def clean_with_custom_config(measurement, tag_filters=None, time_start=None, time_stop=None, force=False):
    """自定义配置的清理接口"""
    cleaner = InfluxDBDataCleaner()
    try:
        success = cleaner.clean_measurement_data(
            measurement=measurement,
            tag_filters=tag_filters,
            time_start=time_start,
            time_stop=time_stop,
            force=force
        )
        return success
    finally:
        cleaner.close()

if __name__ == "__main__":
    # 示例用法：
    # 1. 清理所有near_usdt_swap_kline数据：
    main()
    
    # 2. 清理特定标签的数据：
    # clean_with_custom_config("near_usdt_swap_kline", {"period": "5m"})
    
    # 3. 清理指定时间范围的数据：
    # clean_with_custom_config("near_usdt_swap_kline", None, "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z")
    
    # 4. 强制删除（不需要确认）:
    # clean_with_custom_config("near_usdt_swap_kline", None, None, None, force=True)
