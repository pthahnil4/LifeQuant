#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 05 - 订单修改
==========================

功能说明：
- 修改未成交订单的价格
- 修改未成交订单的数量
- 订单修改策略

适用场景：
- 市场价格变化，需要调整委托价格
- 资金变化，需要调整委托数量
- 策略调整，优化订单参数

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-amend-order

作者：OKX API Demo
创建时间：2025-08-26
版本：v1.0
"""

import okx.Trade as Trade
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
flag = config['flag']  # "0"=实盘, "1"=模拟盘

print("=" * 60)
print("✏️ OKX订单修改工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 修改参数配置
# =============================================================================

# 要修改的订单信息
INST_ID = "LTC-USDT-SWAP"           # 交易对
CLIENT_ORDER_ID = "demo02_limit"    # 要修改的客户订单ID
# ORDER_ID = "1918373485971816448"  # 或使用系统订单ID

# 修改参数（根据需要选择修改项目）
NEW_PRICE = "71.0"                  # 新的委托价格
NEW_SIZE = "0.15"                   # 新的委托数量
CANCEL_ON_FAIL = "false"            # 修改失败时是否撤销订单

print(f"🎯 修改配置:")
print(f"   交易对: {INST_ID}")
print(f"   订单ID: {CLIENT_ORDER_ID}")
print(f"   新价格: ${NEW_PRICE}")
print(f"   新数量: {NEW_SIZE}张")
print(f"   失败处理: {'撤销订单' if CANCEL_ON_FAIL == 'true' else '保留订单'}")
print("=" * 60)

# =============================================================================
# 初始化交易API
# =============================================================================

try:
    tradeAPI = Trade.TradeAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 交易API初始化成功")
except Exception as e:
    print(f"❌ 交易API初始化失败: {e}")
    exit(1)

# =============================================================================
# 查询订单当前状态
# =============================================================================

try:
    print(f"\n🔍 正在查询订单当前状态...")
    
    # 先查询订单当前状态
    current_order = tradeAPI.get_order(
        instId=INST_ID,
        clOrdId=CLIENT_ORDER_ID
    )
    
    if current_order['code'] == '0' and current_order['data']:
        order_info = current_order['data'][0]
        current_state = order_info['state']
        current_px = order_info['px']
        current_sz = order_info['sz']
        current_fill = order_info['accFillSz']
        
        print(f"✅ 订单查询成功")
        print(f"   当前状态: {current_state}")
        print(f"   当前价格: ${current_px}")
        print(f"   当前数量: {current_sz}张")
        print(f"   已成交: {current_fill}张")
        
        # 检查订单是否可以修改
        if current_state not in ['live', 'partially_filled']:
            print(f"\n❌ 订单无法修改!")
            print(f"   原因: 订单状态为 {current_state}")
            print(f"   说明: 只有 'live' 或 'partially_filled' 状态的订单可以修改")
            exit(1)
            
    else:
        print(f"❌ 查询订单失败，无法修改")
        exit(1)
        
except Exception as e:
    print(f"❌ 查询订单时发生错误: {e}")
    exit(1)

# =============================================================================
# 修改订单
# =============================================================================

try:
    print(f"\n✏️ 正在修改订单...")
    print(f"   原价格: ${current_px} → 新价格: ${NEW_PRICE}")
    print(f"   原数量: {current_sz}张 → 新数量: {NEW_SIZE}张")
    
    # 修改订单
    # 参数说明：
    # - instId: 产品ID
    # - clOrdId: 客户订单ID（或使用ordId系统订单ID）
    # - newPx: 新的委托价格
    # - newSz: 新的委托数量
    # - cxlOnFail: 修改失败时是否撤销订单
    result = tradeAPI.amend_order(
        instId=INST_ID,
        clOrdId=CLIENT_ORDER_ID,    # 通过客户订单ID修改
        # ordId=ORDER_ID,           # 或通过系统订单ID修改
        newPx=NEW_PRICE,            # 新价格
        newSz=NEW_SIZE,             # 新数量
        cxlOnFail=CANCEL_ON_FAIL    # 失败处理策略
    )
    
    print("✅ 修改请求提交成功！")
    
except Exception as e:
    print(f"❌ 修改订单时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析修改结果
# =============================================================================

try:
    print(f"\n📋 修改结果数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    code = result['code']
    
    if code == '0':
        amend_data = result['data'][0]
        
        sCode = amend_data['sCode']
        sMsg = amend_data['sMsg']
        ordId = amend_data['ordId']
        clOrdId = amend_data['clOrdId']
        ts = amend_data['ts']
        
        amend_time = datetime.datetime.fromtimestamp(
            int(ts) / 1000
        ).strftime('%Y-%m-%d %H:%M:%S')
        
        print("\n" + "=" * 60)
        print("✏️ 订单修改结果")
        print("=" * 60)
        
        print(f"🆔 系统订单ID:      {ordId}")
        print(f"🏷️  客户订单ID:      {clOrdId}")
        print(f"📊 修改状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 修改时间:        {amend_time}")
        
        if sCode == '0':
            print(f"\n🎉 订单修改成功！")
            
            # 修改前后对比
            print(f"\n📊 修改前后对比:")
            print(f"   价格: ${current_px} → ${NEW_PRICE}")
            print(f"   数量: {current_sz}张 → {NEW_SIZE}张")
            
            # 修改策略分析
            price_change = float(NEW_PRICE) - float(current_px)
            price_change_pct = (price_change / float(current_px)) * 100
            
            if price_change > 0:
                print(f"   价格调整: 上调 ${abs(price_change):.2f} (+{price_change_pct:.1f}%)")
                print(f"   策略分析: 提高价格，可能更容易成交（如果是卖单）")
            elif price_change < 0:
                print(f"   价格调整: 下调 ${abs(price_change):.2f} ({price_change_pct:.1f}%)")
                print(f"   策略分析: 降低价格，可能更容易成交（如果是买单）")
            else:
                print(f"   价格调整: 无变化")
            
            size_change = float(NEW_SIZE) - float(current_sz)
            if size_change > 0:
                print(f"   数量调整: 增加 {abs(size_change)}张")
                print(f"   策略分析: 增加仓位，加大投入")
            elif size_change < 0:
                print(f"   数量调整: 减少 {abs(size_change)}张")
                print(f"   策略分析: 减少仓位，降低风险")
            else:
                print(f"   数量调整: 无变化")
                
        else:
            print(f"\n❌ 订单修改失败!")
            print(f"   错误代码: {sCode}")
            print(f"   错误信息: {sMsg}")
            
            # 常见失败原因分析
            print(f"\n🔍 可能的失败原因:")
            if "order not exist" in sMsg.lower():
                print(f"   - 订单不存在或已经成交/撤销")
            elif "price" in sMsg.lower():
                print(f"   - 价格设置不合理（超出价格限制）")
            elif "size" in sMsg.lower():
                print(f"   - 数量设置不合理（低于最小数量或超出限制）")
            else:
                print(f"   - 其他原因，请查看错误信息")
            
    else:
        print(f"\n❌ 请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

    # =============================================================================
    # 修改策略指南
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("💡 订单修改策略指南")
    print("=" * 60)
    
    print("🎯 修改时机:")
    print("   ✅ 市场价格变化，当前委托价格偏离市场")
    print("   ✅ 资金状况变化，需要调整仓位大小")
    print("   ✅ 策略调整，优化风险收益比")
    print("   ✅ 长时间未成交，需要调整价格促成交")
    
    print("\n🎯 修改技巧:")
    print("   📊 价格修改建议小幅调整，避免过大偏离")
    print("   📊 数量修改需考虑资金和风险管理")
    print("   📊 可以只修改价格或只修改数量")
    print("   📊 部分成交的订单修改时注意剩余数量")
    
    print("\n🎯 注意事项:")
    print("   ⚠️ 只有未成交或部分成交的订单可以修改")
    print("   ⚠️ 修改可能失败，建议设置失败处理策略")
    print("   ⚠️ 频繁修改可能被系统限制")
    print("   ⚠️ 修改后订单会重新排队")

    # =============================================================================
    # 后续操作建议
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("🔍 后续操作建议")
    print("=" * 60)
    
    if code == '0' and sCode == '0':
        print(f"✅ 修改成功，建议:")
        print(f"   1️⃣  查询修改后的订单状态 (demo05_订单查询.py)")
        print(f"   2️⃣  监控订单成交情况")
        print(f"   3️⃣  如仍未成交可继续修改或撤销")
        
    else:
        print(f"❌ 修改失败，建议:")
        print(f"   1️⃣  检查修改参数是否合理")
        print(f"   2️⃣  查询订单最新状态")
        print(f"   3️⃣  考虑撤销后重新下单")

except Exception as e:
    print(f"❌ 解析修改结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo07_订单撤销.py 学习订单撤销") 