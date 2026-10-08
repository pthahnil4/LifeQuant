#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 25 - 查询杠杆倍数
==============================

功能说明：
- 查询demo24中设置的15个币种的杠杆倍数
- 验证批量杠杆设置是否成功
- 支持多种保证金模式的杠杆查询
- 提供详细的查询报告

支持的币种：
与demo24相同的15个主流币种

API文档：
https://www.okx.com/docs-v5/en/#rest-api-account-get-leverage

作者：OKX API Demo
创建时间：2025-01-27
版本：v1.0
"""

import okx.Account as Account
import datetime
import json
import time

# =============================================================================
# API 配置区域 - 使用统一配置文件
# =============================================================================

# 从配置文件导入API配置
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api_config import get_api_config, validate_config, print_config_info

# 获取API配置
config = get_api_config()

# 验证配置
is_valid, message = validate_config(config)
if not is_valid:
    print(f"❌ API配置错误: {message}")
    print("💡 请检查 api_config.py 文件中的配置")
    exit(1)

# 提取配置参数
apikey = config['api_key']
secretkey = config['secret_key']
passphrase = config['passphrase']
flag = config['flag']

print("=" * 60)
print("🔍 OKX杠杆倍数查询工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 初始化API客户端
# =============================================================================

try:
    # 创建账户API实例
    accountAPI = Account.AccountAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 账户API客户端初始化成功")
    
except Exception as e:
    print(f"❌ API客户端初始化失败: {e}")
    exit(1)

# =============================================================================
# 币种配置 - 与demo24相同的币种列表
# =============================================================================

# 与demo24相同的币种列表
CRYPTO_SYMBOLS = [
    "BTC-USDT-SWAP",    # 比特币
    "ETH-USDT-SWAP",    # 以太坊
    "SOL-USDT-SWAP",    # 索拉纳
    "BNB-USDT-SWAP",    # 币安币
    "XRP-USDT-SWAP",    # 瑞波币
    "DOGE-USDT-SWAP",   # 狗狗币
    "ADA-USDT-SWAP",    # 卡尔达诺
    "LTC-USDT-SWAP",    # 莱特币
    "NEAR-USDT-SWAP",   # 尼尔
    "TRX-USDT-SWAP",    # 波场币
    "BCH-USDT-SWAP",    # 比特币现金
    "DOT-USDT-SWAP",    # 波卡币
    "UNI-USDT-SWAP",    # Uniswap币
    "LINK-USDT-SWAP",   # 链克
    "TRUMP-USDT-SWAP"   # 特朗普币
]

print(f"\n📊 准备查询 {len(CRYPTO_SYMBOLS)} 个币种的杠杆倍数")
print(f"⏰ 开始时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =============================================================================
# 杠杆查询函数
# =============================================================================

def get_leverage_with_retry(params, max_retries=3):
    """
    查询杠杆倍数（带重试机制）
    
    参数:
        params: 杠杆查询参数
        max_retries: 最大重试次数
    
    返回:
        tuple: (是否成功, 结果数据或错误信息)
    """
    for attempt in range(max_retries):
        try:
            # 调用get_leverage()方法查询杠杆倍数
            result = accountAPI.get_leverage(**params)
            
            # 检查返回结果
            if result and 'code' in result:
                if result['code'] == '0':
                    return True, result
                else:
                    error_msg = result.get('msg', '未知错误')
                    if attempt < max_retries - 1:
                        time.sleep(1)  # 等待1秒后重试
                    else:
                        return False, error_msg
            else:
                return False, "返回结果格式异常"
                
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)  # 等待1秒后重试
            else:
                return False, str(e)
    
    return False, "达到最大重试次数"

def batch_query_leverage(symbols):
    """
    批量查询杠杆倍数 - 支持多种保证金模式
    
    参数:
        symbols: 币种列表
    
    返回:
        dict: 查询结果统计
    """
    results = {
        'success': [],
        'failed': [],
        'total_queries': 0,
        'success_queries': 0,
        'failed_queries': 0,
        'symbols_processed': 0
    }
    
    # 定义查询场景（对应demo24的5种设置场景）
    query_scenarios = [
        {
            "name": "逐仓币币杠杆",
            "params": {
                "mgnMode": "isolated"
            },
            "instId_suffix": ""  # 现货交易对
        },
        {
            "name": "全仓币币杠杆", 
            "params": {
                "mgnMode": "cross"
            },
            "instId_suffix": ""  # 现货交易对
        },
        {
            "name": "全仓永续合约杠杆",
            "params": {
                "mgnMode": "cross"
            },
            "instId_suffix": "-SWAP"  # 永续合约
        },
        {
            "name": "逐仓永续合约杠杆",
            "params": {
                "mgnMode": "isolated"
            },
            "instId_suffix": "-SWAP"  # 永续合约
        }
    ]
    
    print("\n" + "="*60)
    print("🔍 开始批量查询杠杆倍数 - 支持4种场景")
    print("="*60)
    
    for i, symbol in enumerate(symbols, 1):
        print(f"\n📈 [{i}/{len(symbols)}] 查询币种: {symbol}")
        
        # 提取基础币种名称（去掉-SWAP后缀）
        base_symbol = symbol.replace("-SWAP", "")
        
        # 为每个币种查询所有杠杆场景
        symbol_success_count = 0
        symbol_results = []
        
        for scenario in query_scenarios:
            scenario_name = scenario['name']
            print(f"   🔧 查询 {scenario_name}...")
            
            # 构建完整的交易对ID
            if scenario['instId_suffix']:
                instId = base_symbol + scenario['instId_suffix']
            else:
                instId = base_symbol
            
            params = scenario['params'].copy()
            params['instId'] = instId
            
            success, result = get_leverage_with_retry(params)
            results['total_queries'] += 1
            
            if success and result.get('data'):
                print(f"   ✅ {scenario_name} 查询成功!")
                
                # 处理返回的数据
                for item in result['data']:
                    symbol_results.append({
                        'symbol': symbol,
                        'scenario': scenario_name,
                        'instId': item.get('instId', instId),
                        'mgnMode': item.get('mgnMode', ''),
                        'posSide': item.get('posSide', ''),
                        'lever': item.get('lever', ''),
                        'ccy': item.get('ccy', ''),
                        'status': 'success'
                    })
                    print(f"      📊 {item.get('instId', instId)} - {item.get('mgnMode', '')} - {item.get('posSide', 'net')} - {item.get('lever', '')}倍杠杆")
                
                symbol_success_count += 1
                results['success_queries'] += 1
            else:
                print(f"   ❌ {scenario_name} 查询失败: {result}")
                symbol_results.append({
                    'symbol': symbol,
                    'scenario': scenario_name,
                    'instId': instId,
                    'error': result,
                    'status': 'failed'
                })
                results['failed_queries'] += 1
            
            # 每个场景之间稍作延迟
            time.sleep(0.2)
        
        # 将所有结果添加到总结果中
        for result_item in symbol_results:
            if result_item['status'] == 'success':
                results['success'].append(result_item)
            else:
                results['failed'].append(result_item)
        
        print(f"   📊 币种 {symbol} 总结: {symbol_success_count}/4 个场景查询成功")
        results['symbols_processed'] += 1
        
        # 避免请求过于频繁
        if i < len(symbols):
            time.sleep(0.5)
    
    return results

# =============================================================================
# 执行批量杠杆查询
# =============================================================================

# 执行批量查询
results = batch_query_leverage(CRYPTO_SYMBOLS)

# =============================================================================
# 结果汇总和报告
# =============================================================================

print("\n" + "="*60)
print("📊 批量杠杆查询结果汇总")
print("="*60)

print(f"📈 总处理币种: {results['symbols_processed']} 个")
print(f"🔍 总查询次数: {results['total_queries']} 次")
print(f"✅ 成功查询次数: {results['success_queries']} 次")
print(f"❌ 失败查询次数: {results['failed_queries']} 次")
print(f"📊 查询成功率: {(results['success_queries']/results['total_queries']*100):.1f}%" if results['total_queries'] > 0 else "📊 查询成功率: 0%")

# 显示成功查询的结果
if results['success']:
    print("\n✅ 成功查询的杠杆设置:")
    current_symbol = None
    for item in results['success']:
        if item['symbol'] != current_symbol:
            current_symbol = item['symbol']
            print(f"\n   📈 {item['symbol']}:")
        
        pos_side_text = f" ({item['posSide']})" if item['posSide'] and item['posSide'] != 'net' else ""
        print(f"      🎯 {item['instId']} - {item['mgnMode']} - {item['lever']}倍杠杆{pos_side_text}")

# 显示失败查询的结果
if results['failed']:
    print("\n❌ 查询失败的场景:")
    for item in results['failed']:
        print(f"   📉 {item['symbol']} - {item['scenario']} - {item['error']}")
        print(f"      🎯 尝试查询的交易对: {item['instId']}")

print(f"\n⏰ 完成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =============================================================================
# 重要提示
# =============================================================================

print("\n" + "="*60)
print("💡 查询结果说明")
print("="*60)
print("1. 显示的是当前账户的实际杠杆设置")
print("2. 不同保证金模式可能有不同的杠杆倍数")
print("3. 永续合约在开平仓模式下会显示多空两个方向的杠杆")
print("4. 如果某个场景查询失败，可能是该币种不支持该模式")
print("5. 杠杆设置会影响您的交易风险，请谨慎使用")
print("="*60)

print("\n🎉 杠杆倍数查询完成!")
print(f"📋 查询了 {len(CRYPTO_SYMBOLS)} 个币种的杠杆设置")
print("💡 可以对比demo24的设置结果来验证批量设置是否成功")