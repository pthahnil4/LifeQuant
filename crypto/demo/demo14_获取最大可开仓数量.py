#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 14 - 获取最大可开仓数量
====================================

功能说明：
- 查询指定产品的最大可买卖数量
- 支持全仓和逐仓模式
- 帮助确定合适的下单数量

API文档：
https://www.okx.com/docs-v5/en/#rest-api-account-get-maximum-buy-sell-amount-or-open-amount

作者：OKX API Demo
创建时间：2025-01-26
版本：v1.0
"""

import okx.Account as Account
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
flag = config['flag']  # "0"=实盘, "1"=模拟盘

print("=" * 70)
print("📊 OKX最大可开仓数量查询工具")
print("=" * 70)
print_config_info(config)
print("=" * 70)

# =============================================================================
# 查询参数配置
# =============================================================================

# 查询产品配置
INST_ID = "LTC-USDT-SWAP"              # 查询产品：LTC永续合约

print(f"🎯 查询配置:")
print(f"   产品ID: {INST_ID}")
print(f"   查询模式: 全仓模式 + 逐仓模式")
print("=" * 70)

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
# 查询全仓模式最大可开仓数量
# =============================================================================

try:
    print(f"\n🔍 正在查询全仓模式最大可开仓数量...")
    
    # 查询全仓模式
    result_cross = accountAPI.get_max_order_size(
        instId=INST_ID,
        tdMode="cross"
    )
    
    print("✅ 全仓模式查询成功！")
    
    # 处理返回结果
    if result_cross.get('code') == '0' and result_cross.get('data'):
        data = result_cross['data'][0]
        
        print("\n" + "=" * 70)
        print("📈 全仓模式 - 最大可开仓数量")
        print("=" * 70)
        
        max_buy = data.get('maxBuy', '0')
        max_sell = data.get('maxSell', '0')
        
        print(f"📊 产品信息:        {data.get('instId', INST_ID)}")
        print(f"🟢 最大可买入:      {max_buy} 张")
        print(f"🔴 最大可卖出:      {max_sell} 张")
        print(f"⚙️  交易模式:        全仓模式")
        
        # 转换为数值进行比较和计算
        try:
            max_buy_float = float(max_buy)
            max_sell_float = float(max_sell)
            
            if max_buy_float > 0 or max_sell_float > 0:
                print(f"\n💡 建议交易数量:")
                if max_buy_float > 0:
                    suggested_buy = min(max_buy_float * 0.1, 10)  # 建议使用最大量的10%或10张
                    print(f"   建议买入量: {suggested_buy:.1f} 张 (最大量的10%)")
                if max_sell_float > 0:
                    suggested_sell = min(max_sell_float * 0.1, 10)
                    print(f"   建议卖出量: {suggested_sell:.1f} 张 (最大量的10%)")
            else:
                print(f"\n⚠️  当前无法开仓，可能原因:")
                print(f"   - 账户余额不足")
                print(f"   - 该产品暂停交易")
                print(f"   - 超出风险限制")
        except ValueError:
            print(f"   数值转换失败，请检查返回数据")
            
    else:
        print(f"❌ 全仓模式查询失败: {result_cross.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 查询全仓模式时发生错误: {e}")

# =============================================================================
# 查询逐仓模式最大可开仓数量
# =============================================================================

try:
    print(f"\n🔍 正在查询逐仓模式最大可开仓数量...")
    
    # 查询逐仓模式
    result_isolated = accountAPI.get_max_order_size(
        instId=INST_ID,
        tdMode="isolated"
    )
    
    print("✅ 逐仓模式查询成功！")
    
    # 处理返回结果
    if result_isolated.get('code') == '0' and result_isolated.get('data'):
        data = result_isolated['data'][0]
        
        print("\n" + "=" * 70)
        print("📈 逐仓模式 - 最大可开仓数量")
        print("=" * 70)
        
        max_buy = data.get('maxBuy', '0')
        max_sell = data.get('maxSell', '0')
        
        print(f"📊 产品信息:        {data.get('instId', INST_ID)}")
        print(f"🟢 最大可买入:      {max_buy} 张")
        print(f"🔴 最大可卖出:      {max_sell} 张")
        print(f"⚙️  交易模式:        逐仓模式")
        
        # 转换为数值进行比较和计算
        try:
            max_buy_float = float(max_buy)
            max_sell_float = float(max_sell)
            
            if max_buy_float > 0 or max_sell_float > 0:
                print(f"\n💡 建议交易数量:")
                if max_buy_float > 0:
                    suggested_buy = min(max_buy_float * 0.1, 10)
                    print(f"   建议买入量: {suggested_buy:.1f} 张 (最大量的10%)")
                if max_sell_float > 0:
                    suggested_sell = min(max_sell_float * 0.1, 10)
                    print(f"   建议卖出量: {suggested_sell:.1f} 张 (最大量的10%)")
            else:
                print(f"\n⚠️  当前无法开仓，可能原因:")
                print(f"   - 账户余额不足")
                print(f"   - 该产品暂停交易")
                print(f"   - 超出风险限制")
        except ValueError:
            print(f"   数值转换失败，请检查返回数据")
            
    else:
        print(f"❌ 逐仓模式查询失败: {result_isolated.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 查询逐仓模式时发生错误: {e}")

# =============================================================================
# 显示原始数据（调试用）
# =============================================================================

print(f"\n" + "=" * 70)
print("🔧 调试信息 - 原始返回数据")
print("=" * 70)

try:
    print("全仓模式原始数据:")
    print(json.dumps(result_cross, indent=2, ensure_ascii=False))
    
    print("\n逐仓模式原始数据:")
    print(json.dumps(result_isolated, indent=2, ensure_ascii=False))
except:
    print("原始数据显示失败")

# =============================================================================
# 使用建议
# =============================================================================

print(f"\n" + "=" * 70)
print("💡 使用建议")
print("=" * 70)

print(f"1️⃣  交易模式选择:")
print(f"   - 全仓模式: 使用账户全部资金作为保证金，风险共担")
print(f"   - 逐仓模式: 独立保证金，风险隔离，更安全")

print(f"\n2️⃣  数量控制建议:")
print(f"   - 初学者建议使用最大可开仓量的5-10%")
print(f"   - 根据风险承受能力调整仓位大小")
print(f"   - 避免满仓操作，保留应急资金")

print(f"\n3️⃣  风险管理:")
print(f"   - 设置止损点，控制最大亏损")
print(f"   - 分批建仓，降低平均成本")
print(f"   - 定期检查账户余额变化")

# =============================================================================
# 风险提醒
# =============================================================================

print(f"\n⚠️  风险提醒:")
print(f"   - 合约交易存在爆仓风险，请谨慎操作")
print(f"   - 最大可开仓数量会随市场波动实时变化")
print(f"   - 建议在下单前再次确认最新可开仓量")
print(f"   - 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo03_限价单交易.py 进行实际交易")
