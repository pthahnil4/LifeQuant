#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo - 账户资产估值查询
=============================

功能说明：
- 查询账户资产估值信息
- 获取各币种的估值详情
- 显示总资产价值和分布情况

API文档：
https://www.okx.com/docs-v5/en/#rest-api-funding-get-asset-valuation

作者：OKX API Demo
创建时间：2025-08-26
版本：v1.0
"""

import okx.Funding as Funding
import datetime
import json

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
print("💎 OKX账户资产估值查询工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 初始化API客户端
# =============================================================================

try:
    # 创建资金账户API实例
    # 参数说明：
    # - api_key: API密钥
    # - secret_key: 密钥
    # - passphrase: 口令 
    # - use_server_time: 是否使用服务器时间（已弃用）
    # - flag: 交易环境标识
    fundingAPI = Funding.FundingAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 资金账户API客户端初始化成功")
    
except Exception as e:
    print(f"❌ API客户端初始化失败: {e}")
    exit(1)

# =============================================================================
# 查询账户资产估值
# =============================================================================

try:
    print("\n🔍 正在查询账户资产估值...")
    
    # 调用get_asset_valuation()方法查询账户资产估值
    # 
    # 请求参数说明：
    # 参数    类型     是否必须  描述
    # ccy     String   否       资产估值对应的单位
    #                           BTC、USDT
    #                           USD、CNY、JPY、KRW、RUB、EUR
    #                           VND、IDR、INR、PHP、THB、TRY
    #                           AUD、SGD、ARS、SAR、AED、IQD
    #                           默认为BTC为单位的估值
    #
    # 返回参数说明：
    # 参数        类型     描述
    # totalBal    String   账户总资产估值
    # ts          String   数据更新时间，Unix时间戳的毫秒数格式，如 1597026383085
    # details     Object   各个账户的资产估值
    # > funding   String   资金账户
    # > trading   String   交易账户
    # > classic   String   经典账户 (已废弃)
    # > earn      String   金融账户
    #
    # 返回示例：
    # {
    #   "code": "0",
    #   "data": [
    #     {
    #       "details": {
    #         "classic": "0",
    #         "earn": "0",
    #         "funding": "0.00046731",
    #         "trading": "0.00172126"
    #       },
    #       "totalBal": "0.00218857",
    #       "ts": "1750946531296"
    #     }
    #   ],
    #   "msg": ""
    # }
    
    # 使用USDT作为估值单位进行查询
    result = fundingAPI.get_asset_valuation(ccy="USDT")
    
    # 检查API调用是否成功
    if result.get('code') != '0':
        print(f"❌ 查询失败: {result.get('msg', '未知错误')}")
        exit(1)
        
    print("✅ 查询成功！")
    
except Exception as e:
    print(f"❌ 查询账户资产估值时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析和显示结果
# =============================================================================

try:
    # 原始数据输出（可选，用于调试）
    print(f"\n📋 原始返回数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 检查是否有数据返回
    if not result.get('data') or len(result['data']) == 0:
        print("\n⚠️  暂无资产估值数据")
        exit(0)
    
    # 提取主要数据字段
    asset_data = result['data'][0]
    
    # 总资产估值
    totalBal = asset_data.get('totalBal', '0')
    
    # 更新时间
    ts = asset_data.get('ts', '0')
    
    # 各账户类型详细信息
    details = asset_data.get('details', {})

    # 时间戳转换为可读日期格式
    # OKX返回的是13位毫秒时间戳，需要除以1000转换为秒
    if ts != '0':
        ts_date = datetime.datetime.fromtimestamp(
            int(ts) / 1000
        ).strftime('%Y-%m-%d %H:%M:%S')
    else:
        ts_date = "未知"

    # =============================================================================
    # 美化输出结果
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("💎 账户资产估值详情")
    print("=" * 60)
    
    print(f"💰 总资产估值 (Total Balance):  {totalBal} USDT")
    print(f"🕐 更新时间 (Update Time):     {ts_date}")
    
    print("\n" + "=" * 60)
    print("🏆 各账户类型资产估值:")
    print("=" * 60)
    
    # 账户类型映射
    account_types = {
        'funding': '💳 资金账户 (Funding)',
        'trading': '📈 交易账户 (Trading)', 
        'classic': '🏛️  经典账户 (Classic)',
        'earn': '💰 金融账户 (Earn)'
    }
    
    # 显示各账户类型的资产估值
    total_value = 0
    active_accounts = 0
    
    for account_type, display_name in account_types.items():
        balance = details.get(account_type, '0')
        balance_float = float(balance)
        
        # 计算总价值
        total_value += balance_float
        
        # 显示账户信息
        status = "✅ 有资产" if balance_float > 0 else "⭕ 无资产"
        print(f"{display_name:<25} | 估值: {balance:<15} USDT | {status}")
        
        if balance_float > 0:
            active_accounts += 1
    
    print(f"\n📊 活跃账户数量: {active_accounts}/4")
    print(f"🔢 计算总和验证: {total_value} USDT (应等于总资产估值)")
    
    # 计算和显示占比信息
    if total_value > 0:
        print("\n" + "=" * 60)
        print("📈 资产分布占比:")
        print("=" * 60)
        
        for account_type, display_name in account_types.items():
            balance = details.get(account_type, '0')
            balance_float = float(balance)
            
            if balance_float > 0:
                percentage = (balance_float / total_value) * 100
                print(f"{display_name:<25}: {percentage:>6.2f}% ({balance} USDT)")
    else:
        print("\n⚠️  所有账户余额为0")

    print("=" * 60)
    print("✅ 账户资产估值查询完成！")
    
    # =============================================================================
    # 风险提醒和使用说明
    # =============================================================================
    
    print(f"\n⚠️  风险提醒:")
    print(f"   - 当前运行在 {'实盘模式' if flag == '0' else '模拟盘模式'}")
    print(f"   - 实盘模式涉及真实资金，请谨慎操作")
    print(f"   - 建议先在模拟盘环境测试")
    
    print(f"\n💡 使用说明:")
    print(f"   - 资产估值以USDT为基准计算（可修改ccy参数选择其他币种）")
    print(f"   - 支持的估值单位：BTC、USDT、USD、CNY、JPY等")
    print(f"   - 估值会根据实时汇率变化")
    print(f"   - 数据更新频率取决于市场波动")
    print(f"   - 可用于资产配置和风险管理分析")
    
except Exception as e:
    print(f"❌ 解析结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 更多API功能请参考官方文档: https://www.okx.com/docs-v5/") 