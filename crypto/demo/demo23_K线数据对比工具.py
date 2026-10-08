#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 23 - K线数据对比工具

============================

功能说明：
- 一次性运行所有K线获取方法
- 对比不同方法的数据差异
- 分析哪些方法包含当前未完成的K线

包含的方法：
- get_candlesticks() - 普通K线数据
- get_history_candlesticks() - 历史K线数据  
- get_index_candlesticks() - 指数K线数据
- get_mark_price_candlesticks() - 标记价格K线数据



====================================================================================================     
🔍 对比分析结果
====================================================================================================     

📊 数据对比:
方法                             记录数      最新时间                 是否包含未完成K线
--------------------------------------------------------------------------------
普通K线数据                         5        2025-08-29 00:00     🔥 是
历史K线数据                         5        2025-08-28 00:00     ✅ 否
指数K线数据                         5        2025-08-29 00:00     🔥 是
标记价格K线数据                       5        2025-08-29 00:00     🔥 是


作者：OKX API Demo
创建时间：2024-01-19
版本：v1.1
"""

import okx.MarketData as MarketData
import datetime
import json

# 从配置文件导入API配置
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api_config import get_api_config, validate_config, print_config_info

# 获取API配置
config = get_api_config()
is_valid, message = validate_config(config)
if not is_valid:
    print(f"❌ API配置错误: {message}")
    exit(1)

flag = config['flag']
INST_ID = "BTC-USDT-SWAP"  # 合约交易对
INDEX_ID = "BTC-USDT"      # 对应的指数
BAR = "1H"
LIMIT = "5"

print("=" * 100)
print("📊 OKX K线数据对比工具")
print("=" * 100)
print_config_info(config)
print(f"\n🎯 测试配置: {INST_ID} | {BAR} | 显示最新3条")
print(f"📈 指数配置: {INDEX_ID} (用于指数K线)")
print("=" * 100)

# 初始化API
try:
    marketDataAPI = MarketData.MarketAPI(flag=flag)
    print("✅ 行情API初始化成功\n")
except Exception as e:
    print(f"❌ 行情API初始化失败: {e}")
    exit(1)

# 定义要测试的方法
methods = [
    ("get_candlesticks", "普通K线数据", "实时交易数据，可能包含未完成K线", INST_ID),
    ("get_history_candlesticks", "历史K线数据", "历史数据，通常不包含未完成K线", INST_ID),
    ("get_index_candlesticks", "指数K线数据", "基于指数计算的K线数据", INDEX_ID),
    ("get_mark_price_candlesticks", "标记价格K线数据", "用于合约风险控制的标记价格", INST_ID)
]

# 创建方法名到显示名的映射
method_display_names = {method[0]: method[1] for method in methods}

results = {}

# 逐个测试每种方法
for method_name, display_name, description, inst_id in methods:
    print(f"\n{'='*60}")
    print(f"📊 测试方法: {display_name} ({method_name})")
    print(f"💡 说明: {description}")
    print(f"🎯 使用标的: {inst_id}")
    print(f"{'='*60}")
    
    try:
        # 动态调用方法
        method = getattr(marketDataAPI, method_name)
        result = method(
            instId=inst_id,
            bar=BAR,
            limit=LIMIT
        )
        
        if result['code'] == '0' and result['data']:
            data = result['data']
            results[method_name] = {
                'success': True,
                'data': data,
                'count': len(data),
                'latest_time': data[0][0] if data else None
            }
            
            print(f"✅ 获取成功，共 {len(data)} 条记录")
            print(f"\n{'时间':<20} {'开盘':<10} {'最高':<10} {'最低':<10} {'收盘':<10} {'交易量':<12}")
            print("-" * 75)
            
            # 显示最新3条
            for i, record in enumerate(data[:3]):
                timestamp = int(record[0])
                date = datetime.datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M')
                marker = "🔥" if i == 0 else "  "
                volume = record[5] if len(record) > 5 else "N/A"
                print(f"{date:<20} {record[1]:<10} {record[2]:<10} {record[3]:<10} {record[4]:<10} {volume:<12} {marker}")
                
        else:
            results[method_name] = {
                'success': False,
                'error': result.get('msg', '未知错误'),
                'code': result['code']
            }
            print(f"❌ 获取失败: {result.get('msg', '未知错误')}")
            
    except Exception as e:
        results[method_name] = {
            'success': False,
            'error': str(e)
        }
        print(f"❌ 调用异常: {e}")

# 对比分析
print(f"\n\n{'='*100}")
print("🔍 对比分析结果")
print(f"{'='*100}")

successful_methods = [k for k, v in results.items() if v.get('success', False)]

if len(successful_methods) >= 2:
    print(f"\n📊 数据对比:")
    print(f"{'方法':<30} {'记录数':<8} {'最新时间':<20} {'是否包含未完成K线':<15}")
    print("-" * 80)
    
    current_time = datetime.datetime.now()
    today = datetime.date.today()
    
    for method_name in successful_methods:
        result = results[method_name]
        if result['latest_time']:
            latest_timestamp = int(result['latest_time'])
            latest_date = datetime.datetime.fromtimestamp(latest_timestamp / 1000)
            latest_date_only = latest_date.date()
            
            # 判断是否包含当前未完成的K线
            if BAR == "1D" and latest_date_only == today:
                incomplete_status = "🔥 是"
            else:
                incomplete_status = "✅ 否"
                
            display_name = method_display_names[method_name]
            print(f"{display_name:<30} {result['count']:<8} {latest_date.strftime('%Y-%m-%d %H:%M'):<20} {incomplete_status:<15}")
    
    print(f"\n💡 结论:")
    print(f"   - 包含 '🔥 是' 的方法会返回当前未完成的K线周期")
    print(f"   - 包含 '✅ 否' 的方法只返回已完成的K线周期")
    print(f"   - 用于实时交易时，需要注意未完成K线的数据变化")
    print(f"   - 用于历史回测时，建议使用只返回完成K线的方法")
    
else:
    print("❌ 成功的方法少于2个，无法进行对比分析")

print(f"\n🎯 测试完成！")
print(f"📚 建议: 根据使用场景选择合适的K线获取方法")