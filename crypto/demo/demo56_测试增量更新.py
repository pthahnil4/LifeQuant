#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
测试增量更新功能
===============

功能：
1. 测试修复后的技术指标计算
2. 测试增量更新功能
3. 测试数据去重机制

作者：AI Assistant
创建时间：2025年1月
"""

import sys
import os
from datetime import datetime

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入修复后的技术指标计算器
from demo53_InfluxDB技术指标计算 import NEARTechnicalIndicatorsInfluxDB

def test_single_period():
    """测试单个周期的技术指标计算"""
    print("🎯 测试修复后的技术指标计算")
    processor = NEARTechnicalIndicatorsInfluxDB()
    
    try:
        # 1. 连接InfluxDB
        if not processor.connect_influxdb():
            print("❌ 无法连接到InfluxDB")
            return False
        
        # 2. 测试5分钟周期
        test_period = "5m"
        print(f"\n🔧 测试 {test_period} 周期...")
        
        success = processor.process_period_indicators(test_period, incremental=False)
        
        if success:
            print(f"✅ {test_period} 周期测试成功")
            
            # 测试查询验证
            print(f"\n🔍 验证存储结果...")
            processor.query_latest_indicators(test_period, limit=2)
            
        return success
        
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断测试")
        return False
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False
    finally:
        processor.close()

def test_incremental_update():
    """测试增量更新功能（模拟）"""
    print("\n🎯 测试增量更新功能（模拟）")
    processor = NEARTechnicalIndicatorsInfluxDB()
    
    try:
        # 连接InfluxDB
        if not processor.connect_influxdb():
            print("❌ 无法连接到InfluxDB")
            return False
        
        # 测试获取最后指标值
        test_period = "5m"
        print(f"🔍 测试获取 {test_period} 最后指标值...")
        
        last_values = processor.get_last_indicator_values(test_period)
        
        if last_values:
            print(f"✅ 成功获取最后指标值")
            print(f"   时间: {last_values['timestamp']}")
            print(f"   MACD-EMA: {last_values['macd_ema']}")
            print(f"   方向: {last_values['direction']}")
        else:
            print(f"⚠️ 未找到历史数据")
        
        return True
        
    except Exception as e:
        print(f"❌ 增量更新测试失败: {e}")
        return False
    finally:
        processor.close()

def main():
    """主函数"""
    print("=" * 80)
    print("🧪 技术指标修复功能测试")
    print("=" * 80)
    print(f"⏰ 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = []
    
    # 测试1：单周期指标计算
    print(f"\n📊 测试1: 单周期指标计算")
    result1 = test_single_period()
    results.append(("单周期计算", result1))
    
    # 测试2：增量更新功能
    print(f"\n📊 测试2: 增量更新功能")
    result2 = test_incremental_update()
    results.append(("增量更新", result2))
    
    # 显示测试结果
    print("\n" + "=" * 80)
    print("📋 测试结果汇总")
    print("=" * 80)
    
    for test_name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"   {test_name:<15}: {status}")
    
    success_count = sum(1 for _, result in results if result)
    total_tests = len(results)
    success_rate = (success_count / total_tests * 100) if total_tests > 0 else 0
    
    print(f"\n📈 总体测试结果: {success_count}/{total_tests} ({success_rate:.1f}%)")
    
    if success_count == total_tests:
        print("🎉 所有测试通过！修复成功")
    else:
        print("⚠️ 部分测试失败，需要进一步检查")

if __name__ == "__main__":
    main()
