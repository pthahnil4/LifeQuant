#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 04 - 市价单交易
============================

功能说明：
- 下市价单（立即成交）
- 市价单vs限价单对比
- 成交结果分析

交易特点：
- 市价单：立即按市场最优价格成交，手续费万分之5
- 限价单：指定价格等待成交，手续费万分之2

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-place-order

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
print("⚡ OKX市价单交易工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 交易参数配置
# =============================================================================

# 交易产品配置
INST_ID = "LTC-USDT-SWAP"           # 交易对
TRADE_MODE = "cross"                # 交易模式  

# 生成唯一的客户订单ID（必须是1-9223372036854775807之间的正整数）
import random, time
CLIENT_ORDER_ID = str(int(time.time() * 1000000) + random.randint(1000, 9999))   # 客户订单ID

# 市价单参数
SIDE = "buy"                        # 交易方向：buy=买入, sell=卖出
POS_SIDE = "long"                   # 持仓方向：long=多头, short=空头
ORDER_TYPE = "market"               # 订单类型：market=市价单
SIZE = "0.1"                        # 委托数量（张数）

print(f"🎯 市价单配置:")
print(f"   交易对: {INST_ID}")
print(f"   方向: {SIDE}")
print(f"   数量: {SIZE}张")
print(f"   特点: 立即成交，按市场最优价格")
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
# 下市价单
# =============================================================================

try:
    print(f"\n⚡ 正在下市价单...")
    print(f"   立即{SIDE} {SIZE}张 {INST_ID}...")
    
    # 市价单参数说明：
    # - 不需要指定px（价格），系统自动按最优价格成交
    # - sz为委托数量
    # - ordType="market"表示市价单
    result = tradeAPI.place_order(
        instId=INST_ID,
        tdMode=TRADE_MODE,
        clOrdId=CLIENT_ORDER_ID,
        side=SIDE,
        posSide=POS_SIDE,
        ordType=ORDER_TYPE,
        sz=SIZE
        # 注意：市价单不需要px参数
    )
    
    print("✅ 市价单提交成功！")
    
except Exception as e:
    print(f"❌ 下单时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析交易结果
# =============================================================================

try:
    print(f"\n📋 交易返回数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    code = result['code']
    
    if code == '0':
        order_data = result['data'][0]
        
        sCode = order_data['sCode']
        sMsg = order_data['sMsg']
        ordId = order_data['ordId']
        ts = order_data['ts']
        
        order_time = datetime.datetime.fromtimestamp(
            int(ts) / 1000
        ).strftime('%Y-%m-%d %H:%M:%S')
        
        print("\n" + "=" * 60)
        print("⚡ 市价单交易结果")
        print("=" * 60)
        
        print(f"🆔 系统订单ID:      {ordId}")
        print(f"🏷️  客户订单ID:      {CLIENT_ORDER_ID}")
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 交易时间:        {order_time}")
        
        if sCode == '0':
            print(f"\n🎉 市价单成交成功！")
            print(f"   订单已按市场价格立即成交")
            
            # 市价单特点说明
            print(f"\n💡 市价单特点:")
            print(f"   ✅ 立即成交，无需等待")
            print(f"   ✅ 确保成交，适合急需买卖")
            print(f"   ⚠️ 价格可能略差于预期")
            print(f"   ⚠️ 手续费较高（万分之5）")
            
        else:
            print(f"\n❌ 市价单失败!")
            print(f"   错误代码: {sCode}")
            print(f"   错误信息: {sMsg}")
            
    else:
        print(f"\n❌ 请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

    # =============================================================================
    # 市价单 vs 限价单对比
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("📊 市价单 vs 限价单对比")
    print("=" * 60)
    
    print("市价单特点:")
    print("   ✅ 立即成交")
    print("   ✅ 保证成交")  
    print("   ❌ 价格不确定")
    print("   ❌ 手续费高(万分之5)")
    print("   🎯 适用场景: 急需成交、追涨杀跌")
    
    print("\n限价单特点:")
    print("   ✅ 价格可控")
    print("   ✅ 手续费低(万分之2)")
    print("   ❌ 可能不成交")
    print("   ❌ 需要等待")
    print("   🎯 适用场景: 精确控价、等待机会")
    
    # =============================================================================
    # 后续操作建议
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("🔍 后续操作建议") 
    print("=" * 60)
    
    if code == '0' and sCode == '0':
        print(f"1️⃣  查看成交详情:")
        print(f"   运行 demo09_成交查询.py 查看具体成交价格和手续费")
        
        print(f"\n2️⃣  查看持仓变化:")
        print(f"   运行 demo08_持仓查询.py 确认持仓更新")
        
        print(f"\n3️⃣  资金变化:")
        print(f"   运行 demo01_账户余额.py 查看资金变化")

    print(f"\n⚠️  交易提醒:")
    print(f"   - 市价单适合小额交易和紧急情况")
    print(f"   - 大额交易建议使用限价单控制成本")
    print(f"   - 注意滑点风险，特别是在波动较大时")
    print(f"   - 建议配合止损止盈策略")

except Exception as e:
    print(f"❌ 解析结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo04_止盈止损交易.py 学习风险控制")