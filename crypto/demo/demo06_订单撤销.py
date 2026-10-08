#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 06 - 订单撤销
==========================

功能说明：
- 撤销未成交的订单
- 批量撤销订单
- 撤销策略管理

适用场景：
- 市场趋势变化，不再需要该订单
- 资金需求变化，释放冻结资金
- 交易策略调整，清理旧订单

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-cancel-order

作者：OKX API Demo
创建时间：2024-06-19
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
print("🗑️ OKX订单撤销工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 撤销参数配置
# =============================================================================

# 要撤销的订单信息
INST_ID = "LTC-USDT-SWAP"           # 交易对
CLIENT_ORDER_ID = "demo02_limit"    # 要撤销的客户订单ID
# ORDER_ID = "1918373485971816448"  # 或使用系统订单ID

print(f"🎯 撤销配置:")
print(f"   交易对: {INST_ID}")
print(f"   订单ID: {CLIENT_ORDER_ID}")
print(f"   撤销方式: 单个订单撤销")
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
    
    # 先查询订单当前状态，确认是否可以撤销
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
        side = order_info['side']
        ordType = order_info['ordType']
        
        print(f"✅ 订单查询成功")
        print(f"   订单状态: {current_state}")
        print(f"   交易方向: {side.upper()}")
        print(f"   订单类型: {ordType.upper()}")
        print(f"   委托价格: ${current_px}")
        print(f"   委托数量: {current_sz}张")
        print(f"   已成交: {current_fill}张")
        
        # 检查订单是否可以撤销
        if current_state not in ['live', 'partially_filled']:
            print(f"\n❌ 订单无法撤销!")
            print(f"   原因: 订单状态为 {current_state}")
            if current_state == 'filled':
                print(f"   说明: 订单已完全成交")
            elif current_state == 'canceled':
                print(f"   说明: 订单已经被撤销")
            else:
                print(f"   说明: 订单状态不允许撤销")
            exit(1)
            
        # 计算将释放的资金
        remaining_sz = float(current_sz) - float(current_fill)
        if remaining_sz > 0:
            frozen_amount = remaining_sz * float(current_px)
            print(f"\n💰 撤销后释放资金:")
            print(f"   剩余数量: {remaining_sz}张")
            print(f"   释放资金: 约${frozen_amount:.2f}")
            
    else:
        print(f"❌ 查询订单失败，无法撤销")
        exit(1)
        
except Exception as e:
    print(f"❌ 查询订单时发生错误: {e}")
    exit(1)

# =============================================================================
# 撤销订单确认
# =============================================================================

print(f"\n⚠️  撤销确认:")
print(f"   即将撤销 {side.upper()} 订单")
print(f"   交易对: {INST_ID}")
print(f"   剩余数量: {remaining_sz}张")
print(f"   这将释放被冻结的资金")

# 在实盘环境中，可以添加用户确认
# if flag == "0":  # 实盘环境
#     confirm = input("\n请输入 'YES' 确认撤销订单: ")
#     if confirm != 'YES':
#         print("操作已取消")
#         exit(0)

# =============================================================================
# 执行撤销订单
# =============================================================================

try:
    print(f"\n🗑️ 正在撤销订单...")
    
    # 撤销订单
    # 参数说明：
    # - instId: 产品ID
    # - clOrdId: 客户订单ID（或使用ordId系统订单ID）
    result = tradeAPI.cancel_order(
        instId=INST_ID,
        clOrdId=CLIENT_ORDER_ID     # 通过客户订单ID撤销
        # ordId=ORDER_ID            # 或通过系统订单ID撤销
    )
    
    print("✅ 撤销请求提交成功！")
    
except Exception as e:
    print(f"❌ 撤销订单时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析撤销结果
# =============================================================================

try:
    print(f"\n📋 撤销结果数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    code = result['code']
    
    if code == '0':
        cancel_data = result['data'][0]
        
        sCode = cancel_data['sCode']
        sMsg = cancel_data['sMsg']
        ordId = cancel_data['ordId']
        clOrdId = cancel_data['clOrdId']
        ts = cancel_data['ts']
        
        cancel_time = datetime.datetime.fromtimestamp(
            int(ts) / 1000
        ).strftime('%Y-%m-%d %H:%M:%S')
        
        print("\n" + "=" * 60)
        print("🗑️ 订单撤销结果")
        print("=" * 60)
        
        print(f"🆔 系统订单ID:      {ordId}")
        print(f"🏷️  客户订单ID:      {clOrdId}")
        print(f"📊 撤销状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 撤销时间:        {cancel_time}")
        
        if sCode == '0':
            print(f"\n🎉 订单撤销成功！")
            
            # 撤销成功的影响分析
            print(f"\n📊 撤销影响分析:")
            print(f"   ✅ 订单已从交易系统移除")
            print(f"   ✅ 冻结资金已释放，可用于其他交易")
            print(f"   ✅ 如有部分成交，成交部分保持不变")
            
            if float(current_fill) > 0:
                print(f"\n📈 部分成交情况:")
                print(f"   已成交数量: {current_fill}张")
                print(f"   撤销数量: {remaining_sz}张")
                print(f"   成交比例: {float(current_fill)/float(current_sz)*100:.1f}%")
            else:
                print(f"\n📈 完全撤销:")
                print(f"   订单未有任何成交")
                print(f"   全部数量已撤销: {current_sz}张")
                
        else:
            print(f"\n❌ 订单撤销失败!")
            print(f"   错误代码: {sCode}")
            print(f"   错误信息: {sMsg}")
            
            # 常见失败原因分析
            print(f"\n🔍 可能的失败原因:")
            if "order not exist" in sMsg.lower():
                print(f"   - 订单不存在或已经成交/撤销")
            elif "filled" in sMsg.lower():
                print(f"   - 订单已完全成交，无法撤销")
            elif "canceled" in sMsg.lower():
                print(f"   - 订单已经被撤销")
            else:
                print(f"   - 其他原因：{sMsg}")
            
    else:
        print(f"\n❌ 请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

    # =============================================================================
    # 撤单策略指南
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("💡 撤单策略指南")
    print("=" * 60)
    
    print("🎯 何时撤单:")
    print("   📊 市场趋势改变，订单方向错误")
    print("   📊 价格偏离太远，长时间无法成交")
    print("   📊 资金需求变化，需要释放冻结资金")
    print("   📊 风险控制，减少持仓风险")
    print("   📊 策略调整，更换交易计划")
    
    print("\n🎯 撤单技巧:")
    print("   ✅ 及时撤销无效订单，释放资金")
    print("   ✅ 部分成交的订单可撤销剩余部分")
    print("   ✅ 批量撤单提高操作效率")
    print("   ✅ 设置自动撤单条件（时间或价格）")
    
    print("\n🎯 注意事项:")
    print("   ⚠️ 撤单可能需要几秒钟生效")
    print("   ⚠️ 撤单期间订单可能成交")
    print("   ⚠️ 部分成交的订单撤销后仍保留成交部分")
    print("   ⚠️ 频繁撤单可能影响账户评级")

    # =============================================================================
    # 批量撤单示例
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("📝 批量撤单示例")
    print("=" * 60)
    
    print("如需批量撤销多个订单，可以使用以下代码:")
    print("""
# 批量撤单示例
cancel_orders = [
    {"instId": "LTC-USDT-SWAP", "clOrdId": "order1"},
    {"instId": "LTC-USDT-SWAP", "clOrdId": "order2"},
    {"instId": "BTC-USDT-SWAP", "ordId": "1234567890"}
]

result = tradeAPI.cancel_multiple_orders(cancel_orders)
""")

    # =============================================================================
    # 后续操作建议
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("🔍 后续操作建议")
    print("=" * 60)
    
    if code == '0' and sCode == '0':
        print(f"✅ 撤销成功，建议:")
        print(f"   1️⃣  查看账户余额变化 (demo01_账户余额.py)")
        print(f"   2️⃣  确认资金已释放，可用余额增加")
        print(f"   3️⃣  如需继续交易，可重新下单")
        print(f"   4️⃣  查看持仓变化 (demo08_持仓查询.py)")
        
    else:
        print(f"❌ 撤销失败，建议:")
        print(f"   1️⃣  重新查询订单状态")
        print(f"   2️⃣  检查订单是否已成交")
        print(f"   3️⃣  如有问题，联系客服支持")

except Exception as e:
    print(f"❌ 解析撤销结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo08_持仓查询.py 查看持仓情况") 