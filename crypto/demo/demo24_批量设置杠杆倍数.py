#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 24 - 批量设置杠杆倍数
==============================

功能说明：
- 批量设置多个币种的杠杆倍数为10倍
- 支持从数据库记录中提取币种列表
- 自动处理逐仓和全仓模式的杠杆设置
- 提供详细的执行报告和错误处理

支持的币种：
从用户提供的数据库记录中提取的15个主流币种

API文档：
https://www.okx.com/docs-v5/en/#rest-api-account-set-leverage

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
print("⚡ OKX批量杠杆倍数设置工具")
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
# 币种配置 - 从用户数据库记录中提取
# =============================================================================

# 从用户提供的SQL数据中提取的币种列表
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

# 杠杆倍数设置
LEVERAGE = "10"  # 设置为10倍杠杆

print(f"\n📊 准备为 {len(CRYPTO_SYMBOLS)} 个币种设置杠杆倍数")
print(f"🎯 目标杠杆倍数: {LEVERAGE} 倍")
print(f"⏰ 开始时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =============================================================================
# 杠杆设置函数
# =============================================================================

def set_leverage_with_retry(params, max_retries=3):
    """
    设置杠杆倍数（带重试机制）
    
    参数:
        params: 杠杆设置参数
        max_retries: 最大重试次数
    
    返回:
        tuple: (是否成功, 结果数据或错误信息)
    """
    for attempt in range(max_retries):
        try:
            # 调用set_leverage()方法设置杠杆倍数
            result = accountAPI.set_leverage(**params)
            
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

def batch_set_leverage(symbols, leverage):
    """
    批量设置杠杆倍数 - 支持多种杠杆设置场景
    
    参数:
        symbols: 币种列表
        leverage: 杠杆倍数
    
    返回:
        dict: 执行结果统计
    """
    results = {
        'success': [],
        'failed': [],
        'total': 0,
        'success_count': 0,
        'failed_count': 0
    }
    
    # 定义5种杠杆设置场景（基于原demo24）
    leverage_scenarios = [
        {
            "name": "逐仓币币杠杆",
            "params": {
                "lever": leverage,
                "mgnMode": "isolated"
            },
            "instId_suffix": ""  # 现货交易对
        },
        {
            "name": "全仓币币杠杆", 
            "params": {
                "lever": leverage,
                "mgnMode": "cross"
            },
            "instId_suffix": ""  # 现货交易对
        },
        {
            "name": "全仓永续合约杠杆",
            "params": {
                "lever": leverage,
                "mgnMode": "cross"
            },
            "instId_suffix": "-SWAP"  # 永续合约
        },
        {
            "name": "逐仓永续合约杠杆",
            "params": {
                "lever": leverage,
                "mgnMode": "isolated"
            },
            "instId_suffix": "-SWAP"  # 永续合约
        },
        {
            "name": "逐仓永续合约杠杆（开平仓模式）",
            "params": {
                "lever": leverage,
                "mgnMode": "isolated",
                "posSide": "long"
            },
            "instId_suffix": "-SWAP"  # 永续合约
        }
    ]
    
    print("\n" + "="*60)
    print("🚀 开始批量设置杠杆倍数 - 支持5种场景")
    print("="*60)
    
    for i, symbol in enumerate(symbols, 1):
        print(f"\n📈 [{i}/{len(symbols)}] 处理币种: {symbol}")
        
        # 提取基础币种名称（去掉-SWAP后缀）
        base_symbol = symbol.replace("-SWAP", "")
        
        # 为每个币种尝试所有杠杆设置场景
        symbol_success_count = 0
        symbol_results = []
        
        for scenario in leverage_scenarios:
            scenario_name = scenario['name']
            print(f"   🔧 尝试 {scenario_name}...")
            
            # 构建完整的交易对ID
            if scenario['instId_suffix']:
                instId = base_symbol + scenario['instId_suffix']
            else:
                instId = base_symbol
            
            params = scenario['params'].copy()
            params['instId'] = instId
            
            success, result = set_leverage_with_retry(params)
            
            if success:
                print(f"   ✅ {scenario_name} 设置成功!")
                symbol_results.append({
                    'symbol': symbol,
                    'scenario': scenario_name,
                    'instId': instId,
                    'leverage': leverage,
                    'result': result,
                    'status': 'success'
                })
                symbol_success_count += 1
            else:
                print(f"   ❌ {scenario_name} 失败: {result}")
                symbol_results.append({
                    'symbol': symbol,
                    'scenario': scenario_name,
                    'instId': instId,
                    'error': result,
                    'status': 'failed'
                })
            
            # 每个场景之间稍作延迟
            time.sleep(0.2)
        
        # 将所有结果添加到总结果中
        for result_item in symbol_results:
            if result_item['status'] == 'success':
                results['success'].append(result_item)
            else:
                results['failed'].append(result_item)
        
        # 统计币种级别的结果
        if symbol_success_count > 0:
            results['success_count'] += 1
            print(f"   📊 币种 {symbol} 总结: {symbol_success_count}/5 个场景成功")
        else:
            results['failed_count'] += 1
            print(f"   📊 币种 {symbol} 总结: 所有场景都失败")
        
        results['total'] += 1
        
        # 避免请求过于频繁
        if i < len(symbols):
            time.sleep(0.5)
    
    return results

# =============================================================================
# 执行批量杠杆设置
# =============================================================================

# 执行批量设置
results = batch_set_leverage(CRYPTO_SYMBOLS, LEVERAGE)

# =============================================================================
# 结果汇总和报告
# =============================================================================

print("\n" + "="*60)
print("📊 批量杠杆设置结果汇总")
print("="*60)

print(f"📈 总处理币种: {results['total']} 个")
print(f"✅ 至少一个场景成功的币种: {results['success_count']} 个")
print(f"❌ 所有场景都失败的币种: {results['failed_count']} 个")
print(f"📊 币种成功率: {(results['success_count']/results['total']*100):.1f}%" if results['total'] > 0 else "📊 币种成功率: 0%")
print(f"🎯 总场景成功数: {len(results['success'])} 个")
print(f"💥 总场景失败数: {len(results['failed'])} 个")
print(f"📈 场景成功率: {(len(results['success'])/(len(results['success'])+len(results['failed']))*100):.1f}%" if (len(results['success'])+len(results['failed'])) > 0 else "📈 场景成功率: 0%")

# 显示成功的场景
if results['success']:
    print("\n✅ 成功设置的场景:")
    for item in results['success']:
        print(f"   📈 {item['symbol']} - {item['scenario']} - {item['leverage']}倍杠杆")
        print(f"      🎯 实际交易对: {item['instId']}")

# 显示失败的场景
if results['failed']:
    print("\n❌ 设置失败的场景:")
    for item in results['failed']:
        print(f"   📉 {item['symbol']} - {item['scenario']} - {item['error']}")
        print(f"      🎯 尝试的交易对: {item['instId']}")

print(f"\n⏰ 完成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =============================================================================
# 重要提示
# =============================================================================

print("\n" + "="*60)
print("⚠️  重要提示")
print("="*60)
print("1. 杠杆倍数设置会影响您的交易风险")
print("2. 高杠杆意味着高风险，请谨慎使用")
print("3. 不同币种可能支持不同的杠杆倍数")
print("4. 建议在模拟盘环境下先进行测试")
print("5. 部分币种可能因流动性或风险控制而限制杠杆")
print("6. 请根据自己的风险承受能力合理使用杠杆")
print("="*60)

print("\n🎉 批量杠杆设置完成!")
print(f"📋 处理了来自数据库的 {len(CRYPTO_SYMBOLS)} 个主流币种")
print("💡 如需调整特定币种的杠杆，请使用单独的杠杆设置工具")