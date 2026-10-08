#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 24 - 设置杠杆倍数
==============================

功能说明：
- 设置不同交易模式下的杠杆倍数
- 支持币币杠杆、交割合约、永续合约的杠杆设置
- 演示10种不同的杠杆设置场景

杠杆设置场景：
1. 逐仓交易模式下，设置币币杠杆的杠杆倍数（币对层面）
2. 现货模式账户已开通借币功能，在全仓交易模式下，设置币币杠杆的杠杆倍数（币种层面）
3. 合约模式账户在全仓交易模式下，设置币币杠杆的杠杆倍数（币对层面）
4. 跨币种保证金模式账户在全仓交易模式下，设置币币杠杆的杠杆倍数（币种层面）
5. 组合保证金模式账户在全仓交易模式下，设置币币杠杆的杠杆倍数（币种层面）
6. 在全仓交易模式下，设置交割的杠杆倍数（指数层面）
7. 在逐仓交易模式、买卖持仓模式下，设置交割的杠杆倍数（合约层面）
8. 在逐仓交易模式、开平仓持仓模式下，设置交割的杠杆倍数（合约与持仓方向层面）
9. 在全仓交易模式下，设置永续的杠杆倍数（合约层面）
10. 在逐仓交易模式、买卖持仓模式下，设置永续的杠杆倍数（合约层面）
11. 在逐仓交易模式、开平仓持仓模式下，设置永续的杠杆倍数（合约与持仓方向层面）

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
print("⚡ OKX杠杆倍数设置工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 初始化API客户端
# =============================================================================

try:
    # 创建账户API实例
    # 参数说明：
    # - api_key: API密钥
    # - secret_key: 密钥
    # - passphrase: 口令
    # - use_server_time: 是否使用服务器时间（已弃用）
    # - flag: 交易环境标识
    accountAPI = Account.AccountAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 账户API客户端初始化成功")
    
except Exception as e:
    print(f"❌ API客户端初始化失败: {e}")
    exit(1)

# =============================================================================
# 杠杆设置配置
# =============================================================================

# 杠杆倍数设置
LEVERAGE = "10"  # 设置为10倍杠杆

# 不同场景的杠杆设置示例
leverage_scenarios = [
    {
        "name": "逐仓币币杠杆（币对层面）",
        "description": "在逐仓交易模式下，设置币币杠杆的杠杆倍数",
        "params": {
            "instId": "ETH-USDT",
            "lever": LEVERAGE,
            "mgnMode": "isolated"
        }
    },
    {
        "name": "全仓币币杠杆（币对层面）",
        "description": "在全仓交易模式下，设置币币杠杆的杠杆倍数（币对层面）",
        "params": {
            "instId": "ETH-USDT",  # 使用ETH-USDT币对
            "lever": LEVERAGE,
            "mgnMode": "cross"
        },
        "note": "此场景使用币对层面设置，适用于全仓交易模式"
    },
    {
        "name": "全仓永续合约杠杆",
        "description": "在全仓交易模式下，设置永续的杠杆倍数（合约层面）",
        "params": {
            "instId": "ETH-USDT-SWAP",
            "lever": LEVERAGE,
            "mgnMode": "cross"
        }
    },
    {
        "name": "逐仓永续合约杠杆",
        "description": "在逐仓交易模式、买卖持仓模式下，设置永续的杠杆倍数",
        "params": {
            "instId": "ETH-USDT-SWAP",
            "lever": LEVERAGE,
            "mgnMode": "isolated"
        }
    },
    {
        "name": "逐仓永续合约杠杆（开平仓模式）",
        "description": "在逐仓交易模式、开平仓持仓模式下，设置永续的杠杆倍数",
        "params": {
            "instId": "ETH-USDT-SWAP",
            "lever": LEVERAGE,
            "mgnMode": "isolated",
            "posSide": "long"  # 仅在开平仓持仓模式下需要
        }
    }
]

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
            print(f"   📡 发送杠杆设置请求... (尝试 {attempt + 1}/{max_retries})")
            
            # 调用set_leverage()方法设置杠杆倍数
            result = accountAPI.set_leverage(**params)
            
            # 检查返回结果
            if result and 'code' in result:
                if result['code'] == '0':
                    return True, result
                else:
                    error_msg = result.get('msg', '未知错误')
                    print(f"   ❌ API返回错误: {error_msg}")
                    if attempt < max_retries - 1:
                        print(f"   🔄 等待2秒后重试...")
                        time.sleep(2)
                    else:
                        return False, error_msg
            else:
                print(f"   ❌ 返回结果格式异常: {result}")
                return False, "返回结果格式异常"
                
        except Exception as e:
            print(f"   ❌ 请求异常: {e}")
            if attempt < max_retries - 1:
                print(f"   🔄 等待2秒后重试...")
                time.sleep(2)
            else:
                return False, str(e)
    
    return False, "达到最大重试次数"

def format_leverage_result(result_data):
    """
    格式化杠杆设置结果
    
    参数:
        result_data: API返回的结果数据
    
    返回:
        str: 格式化后的结果字符串
    """
    if not result_data or 'data' not in result_data:
        return "无返回数据"
    
    data = result_data['data']
    if not data or len(data) == 0:
        return "返回数据为空"
    
    item = data[0]
    result_lines = []
    result_lines.append(f"   📊 杠杆倍数: {item.get('lever', 'N/A')}")
    result_lines.append(f"   🏦 保证金模式: {item.get('mgnMode', 'N/A')}")
    
    if 'instId' in item:
        result_lines.append(f"   📈 产品ID: {item['instId']}")
    
    if 'posSide' in item:
        result_lines.append(f"   📍 持仓方向: {item['posSide']}")
    
    return "\n".join(result_lines)

# =============================================================================
# 执行杠杆设置
# =============================================================================

print(f"\n🎯 开始设置杠杆倍数为 {LEVERAGE} 倍")
print(f"⏰ 当前时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("\n" + "="*60)

success_count = 0
fail_count = 0

for i, scenario in enumerate(leverage_scenarios, 1):
    print(f"\n📋 场景 {i}: {scenario['name']}")
    print(f"📝 描述: {scenario['description']}")
    
    # 显示注意事项（如果有）
    if 'note' in scenario:
        print(f"⚠️  注意: {scenario['note']}")
    
    print(f"⚙️  参数: {json.dumps(scenario['params'], ensure_ascii=False, indent=2)}")
    
    # 设置杠杆
    success, result = set_leverage_with_retry(scenario['params'])
    
    if success:
        print("   ✅ 杠杆设置成功!")
        print(format_leverage_result(result))
        success_count += 1
    else:
        print(f"   ❌ 杠杆设置失败: {result}")
        if 'note' in scenario:
            print(f"   💡 这可能是预期的失败，请检查账户配置")
        fail_count += 1
    
    print("-" * 60)
    
    # 避免请求过于频繁
    if i < len(leverage_scenarios):
        time.sleep(1)

# =============================================================================
# 结果汇总
# =============================================================================

print("\n" + "="*60)
print("📊 杠杆设置结果汇总")
print("="*60)
print(f"✅ 成功: {success_count} 个场景")
print(f"❌ 失败: {fail_count} 个场景")
print(f"📈 成功率: {(success_count/(success_count+fail_count)*100):.1f}%" if (success_count+fail_count) > 0 else "📈 成功率: 0%")
print(f"⏰ 完成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =============================================================================
# 重要提示
# =============================================================================

print("\n" + "="*60)
print("⚠️  重要提示")
print("="*60)
print("1. 杠杆倍数设置会影响您的交易风险")
print("2. 高杠杆意味着高风险，请谨慎使用")
print("3. 不同产品和账户模式支持的杠杆倍数不同")
print("4. 某些场景可能因账户配置不支持而失败")
print("5. 建议在模拟盘环境下先进行测试")
print("6. posSide参数仅在开平仓持仓模式下需要填写")
print("="*60)

print("\n🎉 杠杆设置演示完成!")