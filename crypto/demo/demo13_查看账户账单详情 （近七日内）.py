#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 12 - 账户账单查询
============================

功能说明：
- 查询近7天内的账户账单
- 解析账单数据并格式化显示
- 筛选特定订单（如doubleEMA策略）
- 分析最近订单的时间差

API文档：
https://www.okx.com/docs-v5/zh/#rest-api-account-get-bills-details-last-7-days

作者：OKX API Demo
创建时间：2025-08-26
版本：v1.0
"""

import okx.Account as Account
import datetime

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
flag = config['flag']  # "0"=实盘, "1"=模拟盘

print("=" * 60)
print("📊 OKX账户账单查询工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 初始化账户API
# =============================================================================

try:
    # 创建账户API实例
    accountAPI = Account.AccountAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 账户API初始化成功")
    
except Exception as e:
    print(f"❌ 账户API初始化失败: {e}")
    exit(1)

# =============================================================================
# 查询账户账单
# =============================================================================

try:
    print("\n🔍 正在查询近7天账单...")
    result = accountAPI.get_account_bills()
    
    if result.get('code') != '0':
        print(f"❌ 查询失败: {result.get('msg', '未知错误')}")
        exit(1)
        
    data = result['data']
    print(f"✅ 查询成功，共获取 {len(data)} 条记录\n")
    
    # =============================================================================
    # 账单数据处理与展示
    # =============================================================================
    
    print("=" * 60)
    print("📋 账单明细")
    print("=" * 60)
    
    # 遍历账单记录
    for record in data:
        try:
            # 时间格式化
            ts = int(record['ts'])
            fill_time = int(record['fillTime'])
            date_ts = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
            date_fill = datetime.datetime.fromtimestamp(fill_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
            
            # 数值格式化
            bal = round(float(record.get('bal', 0)), 2)
            fill_idx_px = round(float(record.get('fillIdxPx', 0)), 2) if record.get('fillIdxPx') else None
            px = round(float(record.get('px', 0)), 2)
            
            print(f"\n📅 交易时间: {date_ts}")
            print(f"💰 账户余额: {bal} {record.get('ccy', 'Unknown')}")
            print(f"💸 手续费: {record.get('fee', 'N/A')}")
            print(f"🏷️  订单ID: {record.get('clOrdId', 'N/A')}")
            print(f"📊 成交价格: {px}")
            if fill_idx_px:
                print(f"📈 指数价格: {fill_idx_px}")
            print(f"📐 成交数量: {record.get('sz', 'N/A')}")
            print("-" * 30)
            
        except Exception as e:
            print(f"⚠️ 记录解析错误: {e}")
            continue
    
    # =============================================================================
    # 策略订单分析
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("🤖 策略订单分析 (doubleEMA)")
    print("=" * 60)
    
    # 筛选策略订单
    strategy_orders = [r for r in data if r.get('clOrdId') == 'doubleEMA']
    
    if strategy_orders:
        latest = max(strategy_orders, key=lambda x: int(x['ts']))
        
        # 时间格式化
        ts = int(latest['ts'])
        date_ts = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"\n📊 最新策略订单:")
        print(f"📅 交易时间: {date_ts}")
        print(f"💰 账户余额: {round(float(latest.get('bal', 0)), 2)} {latest.get('ccy', 'Unknown')}")
        print(f"📈 成交价格: {round(float(latest.get('px', 0)), 2)}")
        
        # 时间差分析
        current_time = datetime.datetime.now()
        record_time = datetime.datetime.fromtimestamp(ts / 1000)
        time_diff = current_time - record_time
        
        print(f"\n⏱️ 时间分析:")
        print(f"距离现在: {time_diff}")
        if time_diff > datetime.timedelta(hours=1):
            print("⚠️ 注意: 最后交易已超过1小时")
        else:
            print("✅ 最后交易在1小时内")
    else:
        print("\n⚠️ 未找到doubleEMA策略的交易记录")
    
    # =============================================================================
    # 账单分析建议
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("💡 分析建议")
    print("=" * 60)
    print("1. 定期检查账单，关注异常交易")
    print("2. 分析手续费支出，优化交易策略")
    print("3. 跟踪策略订单执行情况")
    print("4. 注意账户余额变动趋势")
    
except Exception as e:
    print(f"\n❌ 程序执行错误: {e}")
    exit(1)

print("\n🎯 账单查询完成！")
