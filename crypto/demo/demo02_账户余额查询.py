#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 02 - 账户余额查询
=============================

功能说明：
- 查询账户余额信息
- 获取总权益、可用余额、现金余额等
- 时间戳格式化显示

API文档：
https://www.okx.com/docs-v5/en/#rest-api-account-get-balance

作者：OKX API Demo
创建时间：2025-08-26
版本：v1.0
"""

import okx.Account as Account
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
print("🏦 OKX账户余额查询工具")
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
    print("✅ API客户端初始化成功")
    
except Exception as e:
    print(f"❌ API客户端初始化失败: {e}")
    exit(1)

# =============================================================================
# 查询账户余额
# =============================================================================

try:
    print("\n🔍 正在查询账户余额...")
    
    # 调用get_account_balance()方法查询账户余额
    # 该方法返回账户的详细余额信息，包括：
    # - 总权益 (totalEq)
    # - 可用余额 (availBal) 
    # - 现金余额 (cashBal)
    # - 各币种的详细信息等
    result = accountAPI.get_account_balance()
    
    # 检查API调用是否成功
    if result.get('code') != '0':
        print(f"❌ 查询失败: {result.get('msg', '未知错误')}")
        exit(1)
        
    print("✅ 查询成功！")
    
except Exception as e:
    print(f"❌ 查询账户余额时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析和显示结果
# =============================================================================

try:
    # 原始数据输出（可选，用于调试）
    print(f"\n📋 原始返回数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 提取主要数据字段
    # data[0] 包含账户的总体信息
    account_data = result['data'][0]
    
    # 总权益：账户总价值（所有币种按USD计算的总和）
    totalEq = account_data['totalEq']
    
    # 更新时间：账户信息最后更新的时间戳
    uTime = account_data['uTime']
    
    # 币种详细信息：details数组包含每个币种的详细余额
    # 这里取第一个币种的信息作为示例
    if account_data['details']:
        first_currency = account_data['details'][0]
        
        # 可用余额：可以用于交易的余额
        availBal = first_currency['availBal']
        
        # 现金余额：实际持有的现金余额
        cashBal = first_currency['cashBal']
        
        # 币种代码
        currency = first_currency['ccy']
    else:
        availBal = "0"
        cashBal = "0" 
        currency = "无"

    # 时间戳转换为可读日期格式
    # OKX返回的是13位毫秒时间戳，需要除以1000转换为秒
    uTime_date = datetime.datetime.fromtimestamp(
        int(uTime) / 1000
    ).strftime('%Y-%m-%d %H:%M:%S')

    # =============================================================================
    # 美化输出结果
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("📊 账户余额详情")
    print("=" * 60)
    
    print(f"💰 总权益 (Total Equity):     ${totalEq}")
    print(f"💳 可用余额 (Available):      {availBal} {currency}")
    print(f"💵 现金余额 (Cash Balance):   {cashBal} {currency}")
    print(f"🕐 更新时间 (Update Time):    {uTime_date}")
    
    print("\n" + "=" * 60)
    print("🏆 各币种详细余额:")
    print("=" * 60)
    
    # 遍历所有币种，显示详细信息
    for i, currency_detail in enumerate(account_data['details'], 1):
        ccy = currency_detail['ccy']                    # 币种名称
        eq = currency_detail['eq']                      # 币种权益  
        availBal = currency_detail['availBal']          # 可用余额
        frozenBal = currency_detail['frozenBal']        # 冻结余额
        
        # 只显示有余额的币种（避免显示太多0余额的币种）
        if float(eq) > 0:
            print(f"{i:2d}. 币种: {ccy:<6} | "
                  f"权益: {eq:<15} | "
                  f"可用: {availBal:<15} | "
                  f"冻结: {frozenBal}")

    print("=" * 60)
    print("✅ 账户余额查询完成！")
    
    # =============================================================================
    # 风险提醒和使用说明
    # =============================================================================
    
    print(f"\n⚠️  风险提醒:")
    print(f"   - 当前运行在 {'实盘模式' if flag == '0' else '模拟盘模式'}")
    print(f"   - 实盘模式涉及真实资金，请谨慎操作")
    print(f"   - 建议先在模拟盘环境测试")
    
    print(f"\n💡 使用说明:")
    print(f"   - 总权益 = 所有币种的USD价值总和")
    print(f"   - 可用余额 = 可以用于新订单的资金")
    print(f"   - 冻结余额 = 被订单占用暂时无法使用的资金")
    
except Exception as e:
    print(f"❌ 解析结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 更多API功能请参考官方文档: https://www.okx.com/docs-v5/") 