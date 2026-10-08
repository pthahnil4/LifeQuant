#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API 测试脚本
================

测试OKX API调用是否正常，验证配置是否正确

作者：AI Assistant
创建时间：2025年1月
"""

import sys
import os
from datetime import datetime, timedelta, timezone
import time

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入OKX API
from okx import MarketData

# 导入配置
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api_config import get_api_config

def test_api_call():
    """测试API调用"""
    print("🔧 开始测试OKX API调用...")
    
    # 获取配置
    config = get_api_config()
    print(f"📋 API配置:")
    print(f"   环境: {'🔴 实盘' if config['flag'] == '0' else '🟢 模拟盘'}")
    print(f"   API Key: {config['api_key'][:8]}***{config['api_key'][-4:]}")
    
    # 创建MarketAPI实例
    market_api = MarketData.MarketAPI(flag=config['flag'])
    print("✅ MarketAPI创建成功")
    
    # 测试不同的API调用
    test_cases = [
        {
            'name': '指数K线API (BTC-USDT)',
            'api': 'get_index_candlesticks',
            'params': {
                'instId': 'BTC-USDT',
                'bar': '5m',
                'limit': '10'
            }
        },
        {
            'name': '指数K线API (ETH-USDT)',
            'api': 'get_index_candlesticks',
            'params': {
                'instId': 'ETH-USDT',
                'bar': '5m',
                'limit': '10'
            }
        },
        {
            'name': '标记价格K线API (BTC-USDT)',
            'api': 'get_mark_price_candlesticks',
            'params': {
                'instId': 'BTC-USDT',
                'bar': '5m',
                'limit': '10'
            }
        },
        {
            'name': '标记价格K线API (ETH-USDT)',
            'api': 'get_mark_price_candlesticks',
            'params': {
                'instId': 'ETH-USDT',
                'bar': '5m',
                'limit': '10'
            }
        },
        {
            'name': '指数K线API (BTC-USDT-SWAP)',
            'api': 'get_index_candlesticks',
            'params': {
                'instId': 'BTC-USDT-SWAP',
                'bar': '5m',
                'limit': '10'
            }
        },
        {
            'name': '指数K线API (ETH-USDT-SWAP)',
            'api': 'get_index_candlesticks',
            'params': {
                'instId': 'ETH-USDT-SWAP',
                'bar': '5m',
                'limit': '10'
            }
        }
    ]
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n[{i}/{len(test_cases)}] 测试: {test_case['name']}")
        print(f"   参数: {test_case['params']}")
        
        try:
            # 调用API
            if test_case['api'] == 'get_index_candlesticks':
                result = market_api.get_index_candlesticks(**test_case['params'])
            elif test_case['api'] == 'get_mark_price_candlesticks':
                result = market_api.get_mark_price_candlesticks(**test_case['params'])
            
            # 检查结果
            print(f"   响应代码: {result.get('code', 'N/A')}")
            print(f"   响应消息: {result.get('msg', 'N/A')}")
            
            if 'data' in result:
                data_count = len(result['data']) if result['data'] else 0
                print(f"   数据条数: {data_count}")
                
                if data_count > 0:
                    print(f"   ✅ 成功获取 {data_count} 条数据")
                    # 显示第一条数据的时间
                    first_data = result['data'][0]
                    if len(first_data) > 0:
                        timestamp = datetime.fromtimestamp(int(first_data[0]) / 1000, tz=timezone.utc)
                        print(f"   最新数据时间: {timestamp}")
                else:
                    print(f"   ⚠️ 无数据返回")
            else:
                print(f"   ❌ 响应中缺少data字段")
                
        except Exception as e:
            print(f"   ❌ API调用失败: {e}")
        
        # 防止API过于频繁调用
        time.sleep(0.5)
    
    print(f"\n🎯 API测试完成")

if __name__ == "__main__":
    test_api_call()
