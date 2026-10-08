#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 03 - 限价单交易
============================

功能说明：
- 下限价单（指定价格成交）
- 处理订单返回结果
- 订单状态跟踪

交易类型对比：
- 限价单：指定价格成交，手续费较低（万分之2）
- 市价单：立即成交，手续费较高（万分之5）

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-place-order

作者：OKX API Demo
创建时间：2025-08-26
版本：v1.0
"""

import okx.Trade as Trade
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
flag = config['flag']  # "0"=实盘, "1"=模拟盘

print("=" * 60)
print("📈 OKX限价单交易工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 交易参数配置
# =============================================================================

# 交易产品配置
INST_ID = "LTC-USDT-SWAP"           # 交易对：LTC永续合约
TRADE_MODE = "cross"                # 交易模式：cross=全仓, isolated=逐仓
# 生成唯一的客户订单ID（必须是1-9223372036854775807之间的正整数）
import random
CLIENT_ORDER_ID = str(int(time.time() * 1000000) + random.randint(1000, 9999))    # 客户自定义订单ID（用于跟踪订单）

# 订单参数配置  
SIDE = "buy"                        # 交易方向：buy=买入, sell=卖出
POS_SIDE = "long"                   # 持仓方向：long=多头, short=空头
ORDER_TYPE = "limit"                # 订单类型：limit=限价单
PRICE = "70.0"                      # 委托价格（USD）
SIZE = "0.1"                        # 委托数量（张数）

print(f"🎯 交易配置:")
print(f"   交易对: {INST_ID}")
print(f"   交易模式: {TRADE_MODE}")
print(f"   方向: {SIDE}")
print(f"   价格: ${PRICE}")
print(f"   数量: {SIZE}张")
print("=" * 60)

# =============================================================================
# 初始化交易API
# =============================================================================

try:
    # 创建交易API实例
    tradeAPI = Trade.TradeAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 交易API初始化成功")
    
except Exception as e:
    print(f"❌ 交易API初始化失败: {e}")
    exit(1)

# =============================================================================
# 下限价单
# =============================================================================

try:
    print(f"\n🚀 正在下限价单...")
    print(f"   等待以 ${PRICE} 的价格{SIDE} {SIZE}张 {INST_ID}...")
    
    # 调用place_order方法下限价单
    # 参数说明：
    # - instId: 产品ID，如 BTC-USDT-SWAP
    # - tdMode: 交易模式（cross=全仓, isolated=逐仓, cash=现金模式）
    # - clOrdId: 客户自定义订单ID，用于跟踪订单
    # - side: 订单方向（buy=买入, sell=卖出）
    # - ordType: 订单类型（limit=限价单, market=市价单, post_only=只做maker）
    # - px: 委托价格
    # - sz: 委托数量
    result = tradeAPI.place_order(
        instId=INST_ID,
        tdMode=TRADE_MODE,
        clOrdId=CLIENT_ORDER_ID,
        side=SIDE,
        posSide=POS_SIDE,
        ordType=ORDER_TYPE,
        px=PRICE,
        sz=SIZE
    )
    
    print("✅ 订单提交成功！")
    
except Exception as e:
    print(f"❌ 下单时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析订单结果
# =============================================================================

try:
    # 显示原始返回数据（调试用）
    print(f"\n📋 订单返回数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 提取关键字段
    code = result['code']                           # 请求状态码
    
    if code == '0':
        # 请求成功
        order_data = result['data'][0]
        
        sCode = order_data['sCode']                 # 订单状态码  
        sMsg = order_data['sMsg']                   # 订单状态信息
        ordId = order_data['ordId']                 # 系统订单ID
        ts = order_data['ts']                       # 订单时间戳
        
        # 时间戳转换为可读格式
        order_time = datetime.datetime.fromtimestamp(
            int(ts) / 1000
        ).strftime('%Y-%m-%d %H:%M:%S')
        
        # =============================================================================
        # 美化输出订单结果
        # =============================================================================
        
        print("\n" + "=" * 60)
        print("📋 订单详情")
        print("=" * 60)
        
        print(f"🆔 系统订单ID:      {ordId}")
        print(f"🏷️  客户订单ID:      {CLIENT_ORDER_ID}")
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 下单时间:        {order_time}")
        
        # 根据状态码判断订单是否成功
        if sCode == '0':
            print(f"\n🎉 订单创建成功！")
            print(f"   订单已进入交易系统，等待成交...")
            
            # 订单状态说明
            print(f"\n💡 订单状态说明:")
            print(f"   - 限价单已挂单，等待市场价格达到 ${PRICE}")
            print(f"   - 可以通过订单ID查询实时状态")
            print(f"   - 如需撤单，请使用撤单功能")
            
        else:
            print(f"\n❌ 订单创建失败!")
            print(f"   错误代码: {sCode}")
            print(f"   错误信息: {sMsg}")
            
    else:
        # 请求失败
        print(f"\n❌ 请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

    # =============================================================================
    # 订单跟踪建议
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("🔍 后续操作建议")
    print("=" * 60)
    
    if code == '0' and sCode == '0':
        print(f"1️⃣  查询订单状态:")
        print(f"   可使用订单ID: {ordId}")
        print(f"   或客户订单ID: {CLIENT_ORDER_ID}")
        print(f"   运行 demo05_订单查询.py 查看详情")
        
        print(f"\n2️⃣  修改订单:")
        print(f"   如需修改价格或数量，运行 demo06_订单修改.py")
        
        print(f"\n3️⃣  撤销订单:")
        print(f"   如需撤销订单，运行 demo07_订单撤销.py")
        
        print(f"\n4️⃣  查看持仓:")
        print(f"   订单成交后查看持仓，运行 demo08_持仓查询.py")

    # =============================================================================
    # 风险提醒
    # =============================================================================
    
    print(f"\n⚠️  风险提醒:")
    print(f"   - 限价单可能无法立即成交，取决于市场价格")
    print(f"   - 请定期检查订单状态")
    print(f"   - 注意市场波动，及时调整策略")
    print(f"   - 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")

except Exception as e:
    print(f"❌ 解析订单结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo03_市价单交易.py 学习市价单")