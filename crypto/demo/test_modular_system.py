#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
模块化系统测试脚本
================

测试demo61模块化架构的各个组件功能
验证系统的稳定性和可用性

作者：AI Assistant
创建时间：2025年1月
"""

import asyncio
import sys
import os

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_import_modules():
    """测试模块导入"""
    print("🔄 测试模块导入...")
    
    try:
        from demo62_multi_symbol_kline_storage import MultiSymbolKlineStorage
        print("✅ demo62 历史K线存储模块导入成功")
        
        from demo63_websocket_incremental_update import WebSocketIncrementalUpdater
        print("✅ demo63 WebSocket增量更新模块导入成功")
        
        from demo64_multi_symbol_indicators import MultiSymbolIndicatorCalculator
        print("✅ demo64 技术指标计算模块导入成功")
        
        from demo65_multi_symbol_validation import MultiSymbolDataValidator
        print("✅ demo65 数据校验模块导入成功")
        
        from demo61_quant_db_manager import QuantDBManager
        print("✅ demo61 主调度器导入成功")
        
        return True
        
    except ImportError as e:
        print(f"❌ 模块导入失败: {e}")
        return False

def test_influxdb_connection():
    """测试InfluxDB连接"""
    print("\n🔄 测试InfluxDB连接...")
    
    try:
        from demo62_multi_symbol_kline_storage import MultiSymbolKlineStorage
        
        storage = MultiSymbolKlineStorage()
        if storage.connect_influxdb():
            print("✅ InfluxDB连接测试成功")
            storage.close()
            return True
        else:
            print("❌ InfluxDB连接测试失败")
            return False
            
    except Exception as e:
        print(f"❌ InfluxDB连接测试异常: {e}")
        return False

def test_symbol_parsing():
    """测试币种解析功能"""
    print("\n🔄 测试币种解析功能...")
    
    try:
        from demo61_quant_db_manager import parse_symbol_list, parse_period_list
        
        # 测试币种解析
        symbols_all = parse_symbol_list("all")
        print(f"✅ 解析'all': 获得 {len(symbols_all)} 个币种")
        
        symbols_custom = parse_symbol_list("BTC-USDT-SWAP,ETH-USDT-SWAP")
        print(f"✅ 解析自定义币种: {symbols_custom}")
        
        # 测试周期解析
        periods_all = parse_period_list("all")
        print(f"✅ 解析'all': 获得 {len(periods_all)} 个周期")
        
        periods_custom = parse_period_list("5m,1H")
        print(f"✅ 解析自定义周期: {periods_custom}")
        
        return True
        
    except Exception as e:
        print(f"❌ 解析功能测试异常: {e}")
        return False

def test_table_naming():
    """测试表名生成功能"""
    print("\n🔄 测试表名生成功能...")
    
    try:
        from demo62_multi_symbol_kline_storage import MultiSymbolKlineStorage
        
        storage = MultiSymbolKlineStorage()
        
        # 测试K线表名生成
        kline_table = storage.get_table_name("BTC-USDT-SWAP")
        print(f"✅ K线表名: BTC-USDT-SWAP -> {kline_table}")
        
        # 测试API币种格式转换
        api_symbol = storage.get_api_symbol("BTC-USDT-SWAP")
        print(f"✅ API币种格式: BTC-USDT-SWAP -> {api_symbol}")
        
        return True
        
    except Exception as e:
        print(f"❌ 表名生成测试异常: {e}")
        return False

async def test_manager_initialization():
    """测试管理器初始化"""
    print("\n🔄 测试管理器初始化...")
    
    try:
        from demo61_quant_db_manager import QuantDBManager
        
        manager = QuantDBManager()
        print("✅ QuantDBManager 初始化成功")
        
        # 测试子模块延迟初始化状态
        print(f"✅ 初始状态检查:")
        print(f"   - kline_storage: {manager.kline_storage}")
        print(f"   - ws_updater: {manager.ws_updater}")
        print(f"   - indicator_calculator: {manager.indicator_calculator}")
        print(f"   - data_validator: {manager.data_validator}")
        
        return True
        
    except Exception as e:
        print(f"❌ 管理器初始化测试异常: {e}")
        return False

async def main():
    """主测试函数"""
    print("🎯 模块化系统测试开始")
    print("=" * 60)
    
    # 测试列表
    tests = [
        ("模块导入", test_import_modules),
        ("InfluxDB连接", test_influxdb_connection),
        ("参数解析", test_symbol_parsing),
        ("表名生成", test_table_naming),
        ("管理器初始化", test_manager_initialization),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        try:
            if asyncio.iscoroutinefunction(test_func):
                result = await test_func()
            else:
                result = test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"❌ {test_name} 测试异常: {e}")
            results.append((test_name, False))
    
    # 显示测试结果
    print("\n" + "=" * 60)
    print("📊 测试结果汇总:")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for test_name, result in results:
        if result:
            print(f"✅ {test_name:<20} 通过")
            passed += 1
        else:
            print(f"❌ {test_name:<20} 失败")
            failed += 1
    
    print("=" * 60)
    print(f"📈 总计: {len(results)} 项测试")
    print(f"   通过: {passed} 项")
    print(f"   失败: {failed} 项")
    print(f"   成功率: {passed/len(results)*100:.1f}%")
    
    if failed == 0:
        print("🎉 所有测试通过！模块化系统可以使用")
        print("\n💡 Windows 11 用户建议接下来：")
        print("   1. 双击运行: quick_test.bat")
        print("   2. 命令行: python demo61_quant_db_manager.py --mode history --symbols BTC-USDT-SWAP --periods 5m")
        print("   3. 数据校验: python demo61_quant_db_manager.py --mode validate --level high")
    else:
        print("⚠️ 部分测试失败，请检查系统配置")
    
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
