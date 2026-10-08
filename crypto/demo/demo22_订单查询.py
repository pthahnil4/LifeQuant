#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 22 - 订单查询
==========================

功能说明：
- 查询指定订单详情
- 订单状态解读
- 成交信息分析

订单状态说明：
- live: 等待成交
- partially_filled: 部分成交
- filled: 完全成交
- canceled: 已撤销

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-get-order-details

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
print("🔍 OKX订单查询工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 查询参数配置
# =============================================================================

# 要查询的订单信息
INST_ID = "LTC-USDT-SWAP"           # 交易对
CLIENT_ORDER_ID = "demo02_limit"    # 客户订单ID（在之前的demo中创建的）
# 或者使用系统订单ID
# ORDER_ID = "1918373485971816448"  # 系统订单ID

print(f"🎯 查询配置:")
print(f"   交易对: {INST_ID}")
print(f"   客户订单ID: {CLIENT_ORDER_ID}")
print(f"   查询方式: 通过客户自定义订单ID")
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
# 查询订单详情
# =============================================================================

try:
    print(f"\n🔍 正在查询订单详情...")
    
    # 通过客户订单ID查询订单
    # 也可以使用ordId参数通过系统订单ID查询
    result = tradeAPI.get_order(
        instId=INST_ID,
        clOrdId=CLIENT_ORDER_ID  # 使用客户订单ID查询
        # ordId=ORDER_ID         # 或使用系统订单ID查询
    )
    
    print("✅ 查询成功！")
    
except Exception as e:
    print(f"❌ 查询订单时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析订单详情
# =============================================================================

try:
    print(f"\n📋 订单原始数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    code = result['code']
    
    if code == '0' and result['data']:
        order_info = result['data'][0]
        
        # 提取关键订单信息
        ordId = order_info['ordId']                 # 系统订单ID
        clOrdId = order_info['clOrdId']             # 客户订单ID
        instId = order_info['instId']               # 交易对
        state = order_info['state']                 # 订单状态
        
        # 订单基本信息
        side = order_info['side']                   # 买卖方向
        ordType = order_info['ordType']             # 订单类型
        px = order_info['px']                       # 委托价格
        sz = order_info['sz']                       # 委托数量
        
        # 成交信息
        accFillSz = order_info['accFillSz']         # 累计成交数量
        avgPx = order_info.get('avgPx', '')         # 成交均价
        fillTime = order_info.get('fillTime', '')   # 成交时间
        
        # 手续费信息
        fee = order_info['fee']                     # 手续费
        feeCcy = order_info['feeCcy']               # 手续费币种
        
        # 时间信息
        cTime = order_info['cTime']                 # 创建时间
        uTime = order_info['uTime']                 # 更新时间
        
        # 转换时间格式
        create_time = datetime.datetime.fromtimestamp(
            int(cTime) / 1000
        ).strftime('%Y-%m-%d %H:%M:%S')
        
        update_time = datetime.datetime.fromtimestamp(
            int(uTime) / 1000
        ).strftime('%Y-%m-%d %H:%M:%S')
        
        # =============================================================================
        # 美化输出订单详情
        # =============================================================================
        
        print("\n" + "=" * 60)
        print("📋 订单详细信息")
        print("=" * 60)
        
        # 基本信息
        print(f"🆔 系统订单ID:      {ordId}")
        print(f"🏷️  客户订单ID:      {clOrdId}")
        print(f"📊 交易对:          {instId}")
        print(f"📈 交易方向:        {side.upper()}")
        print(f"📋 订单类型:        {ordType.upper()}")
        
        # 价格和数量
        print(f"\n💰 价格信息:")
        print(f"   委托价格:        ${px}")
        print(f"   委托数量:        {sz}张")
        if avgPx:
            print(f"   成交均价:        ${avgPx}")
        
        # 成交信息
        print(f"\n📊 成交信息:")
        print(f"   累计成交:        {accFillSz}张")
        print(f"   成交比例:        {float(accFillSz)/float(sz)*100:.1f}%")
        if fillTime:
            fill_time = datetime.datetime.fromtimestamp(
                int(fillTime) / 1000
            ).strftime('%Y-%m-%d %H:%M:%S')
            print(f"   成交时间:        {fill_time}")
        
        # 手续费信息
        print(f"\n💸 费用信息:")
        print(f"   手续费:          {fee} {feeCcy}")
        
        # 时间信息
        print(f"\n🕐 时间信息:")
        print(f"   创建时间:        {create_time}")
        print(f"   更新时间:        {update_time}")
        
        # 订单状态分析
        print(f"\n📈 订单状态分析:")
        print(f"   当前状态:        {state}")
        
        # 根据不同状态提供详细说明
        if state == 'live':
            print(f"   状态说明:        ⏳ 订单等待成交中")
            print(f"   操作建议:        可以继续等待或修改/撤销订单")
            
        elif state == 'partially_filled':
            print(f"   状态说明:        🔄 订单部分成交")
            remaining = float(sz) - float(accFillSz)
            print(f"   剩余数量:        {remaining}张")
            print(f"   操作建议:        等待剩余部分成交或撤销剩余订单")
            
        elif state == 'filled':
            print(f"   状态说明:        ✅ 订单完全成交")
            print(f"   操作建议:        查看持仓变化和账户余额")
            
        elif state == 'canceled':
            print(f"   状态说明:        ❌ 订单已撤销")
            print(f"   操作建议:        如需交易可重新下单")
            
        else:
            print(f"   状态说明:        ❓ 其他状态: {state}")

        # =============================================================================
        # 操作建议
        # =============================================================================
        
        print("\n" + "=" * 60)
        print("🛠️ 操作建议")
        print("=" * 60)
        
        if state == 'live':
            print(f"📌 订单等待成交中，您可以:")
            print(f"   1️⃣  继续等待成交")
            print(f"   2️⃣  修改订单价格或数量 (demo06_订单修改.py)")
            print(f"   3️⃣  撤销订单 (demo07_订单撤销.py)")
            
        elif state == 'filled':
            print(f"🎉 订单已完全成交，建议:")
            print(f"   1️⃣  查看持仓变化 (demo08_持仓查询.py)")
            print(f"   2️⃣  查看账户余额 (demo01_账户余额.py)")
            print(f"   3️⃣  查看成交记录 (demo09_成交查询.py)")
            
        elif state == 'partially_filled':
            print(f"🔄 订单部分成交，建议:")
            print(f"   1️⃣  等待剩余部分成交")
            print(f"   2️⃣  撤销剩余订单 (demo07_订单撤销.py)")
            print(f"   3️⃣  查看已成交部分 (demo09_成交查询.py)")

    else:
        if code != '0':
            print(f"\n❌ 查询失败!")
            print(f"   错误代码: {code}")
            print(f"   错误信息: {result.get('msg', '未知错误')}")
        else:
            print(f"\n🔍 未找到订单!")
            print(f"   请检查订单ID是否正确")
            print(f"   或者订单可能已经过期")

    # =============================================================================
    # 查询技巧说明
    # =============================================================================
    
    print(f"\n💡 查询技巧:")
    print(f"   - 可使用系统订单ID (ordId) 或客户订单ID (clOrdId)")
    print(f"   - 客户订单ID由您自定义，便于跟踪")
    print(f"   - 建议定期查询重要订单状态")
    print(f"   - 大额订单建议设置状态提醒")

except Exception as e:
    print(f"❌ 解析订单信息时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo06_订单修改.py 学习订单修改") 