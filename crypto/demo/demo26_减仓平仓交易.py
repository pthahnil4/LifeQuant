#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 26 - 减仓平仓交易
============================

功能说明：
- 减仓交易（reduceOnly=true）
- 平仓交易（开平仓模式下的平仓操作）
- 处理订单返回结果
- 订单状态跟踪

交易模式对比：
- 减仓模式：只减少持仓数量，不会增加新的持仓仓位
- 平仓模式：完全关闭持仓（开平仓模式下）

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-place-order

作者：OKX API Demo
创建时间：2025-01-27
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
print("📉 OKX减仓平仓交易工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 交易参数配置
# =============================================================================

# =============================================================================
# 交易产品配置说明
# =============================================================================

# 产品ID配置示例：
# 现货: "BTC-USDT", "ETH-USDT", "LTC-USDT"
# 永续合约: "BTC-USDT-SWAP", "ETH-USDT-SWAP", "LTC-USDT-SWAP"
# 交割合约: "BTC-USDT-240329", "ETH-USDT-240329"
# 期权: "BTC-USD-240329-50000-C", "ETH-USD-240329-3000-P"

INST_ID = "NEAR-USDT-SWAP"           # 交易对：LTC永续合约

# 交易模式说明：
# - cross: 全仓模式（推荐，资金利用率高）
# - isolated: 逐仓模式（风险隔离，适合高风险策略）
# - cash: 现金模式（仅适用于现货交易）
TRADE_MODE = "isolated"                # 交易模式：cross=全仓, isolated=逐仓



# =============================================================================
# 操作模式和交易参数配置
# =============================================================================

# 操作模式配置
OPERATION_MODE = 1                  # 1=减仓模式, 2=平仓模式
POS_SIDE = "long"                   # 持仓方向: long=多头, short=空头 (仅开平仓模式需要)
ORDER_TYPE = "limit"                # 订单类型: limit=限价单, market=市价单
PRICE = "2.67"                       # 委托价格 (USD)
SIZE = "0.1"                        # 操作数量 (张)

# 根据交易模式和操作模式自动设置参数
if TRADE_MODE == "cross" or TRADE_MODE == "isolated":
    # 单币种保证金模式：不使用posSide，通过side控制方向
    if OPERATION_MODE == 1:
        # 减仓模式：卖出减少多头持仓，买入减少空头持仓
        SIDE = "sell" if POS_SIDE == "long" else "buy"
        REDUCE_ONLY = True
        USE_POS_SIDE = False  # 单币种保证金模式不使用posSide
    else:
        # 平仓模式：同减仓逻辑
        SIDE = "sell" if POS_SIDE == "long" else "buy"
        REDUCE_ONLY = None
        USE_POS_SIDE = False
else:
    # 跨币种保证金模式或现货模式：使用posSide
    if OPERATION_MODE == 1:
        SIDE = "sell" if POS_SIDE == "long" else "buy"
        REDUCE_ONLY = True
        USE_POS_SIDE = True
    else:
        SIDE = "sell" if POS_SIDE == "long" else "buy"
        REDUCE_ONLY = None
        USE_POS_SIDE = True

# 市价单不需要价格参数
if ORDER_TYPE == "market":
    PRICE = None

print(f"🎯 交易配置:")
print(f"   交易对: {INST_ID}")
print(f"   交易模式: {TRADE_MODE}")
print(f"   操作模式: {'减仓' if OPERATION_MODE == 1 else '平仓'}")
if USE_POS_SIDE:
    print(f"   持仓方向: {POS_SIDE}")
print(f"   交易方向: {SIDE}")
if PRICE:
    print(f"   价格: ${PRICE}")
print(f"   数量: {SIZE}张")
if REDUCE_ONLY:
    print(f"   只减仓: {REDUCE_ONLY}")

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
# 执行减仓/平仓操作
# =============================================================================

try:
    if OPERATION_MODE == 1:
        print(f"\n📉 正在执行减仓操作...")
        if USE_POS_SIDE:
            print(f"   以 ${PRICE} 的价格减少 {SIZE}张 {INST_ID} {POS_SIDE}持仓...")
        else:
            print(f"   以 ${PRICE} 的价格减少 {SIZE}张 {INST_ID} 持仓...")
    else:
        print(f"\n🔄 正在执行平仓操作...")
        if ORDER_TYPE == "market":
            if USE_POS_SIDE:
                print(f"   市价平掉 {SIZE}张 {INST_ID} {POS_SIDE}持仓...")
            else:
                print(f"   市价平掉 {SIZE}张 {INST_ID} 持仓...")
        else:
            if USE_POS_SIDE:
                print(f"   以 ${PRICE} 的价格平掉 {SIZE}张 {INST_ID} {POS_SIDE}持仓...")
            else:
                print(f"   以 ${PRICE} 的价格平掉 {SIZE}张 {INST_ID} 持仓...")
    
    # 构建订单参数
    order_params = {
        'instId': INST_ID,
        'tdMode': TRADE_MODE,
        'side': SIDE,
        'ordType': ORDER_TYPE,
        'sz': SIZE
    }
    
    # 添加持仓方向参数（仅在需要时）
    if USE_POS_SIDE:
        order_params['posSide'] = POS_SIDE
    
    # 添加价格参数（限价单需要）
    if ORDER_TYPE == "limit" and PRICE:
        order_params['px'] = PRICE
    
    # 添加只减仓参数（减仓模式需要）
    if REDUCE_ONLY:
        order_params['reduceOnly'] = REDUCE_ONLY
    
    # 执行下单
    result = tradeAPI.place_order(**order_params)
    
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
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 下单时间:        {order_time}")
        
        # 根据状态码判断订单是否成功
        if sCode == '0':
            if OPERATION_MODE == 1:
                print(f"\n🎉 减仓订单创建成功！")
                print(f"   订单已进入交易系统，等待成交...")
                
                # 减仓订单状态说明
                print(f"\n💡 减仓订单说明:")
                print(f"   - 限价减仓单已挂单，等待市场价格达到 ${PRICE}")
                print(f"   - 该订单只会减少持仓，不会增加新仓位")
                print(f"   - 减仓数量不能超过当前持仓数量")
                
            else:
                print(f"\n🎉 平仓订单创建成功！")
                print(f"   市价平仓订单已提交，应该很快成交...")
                
                # 平仓订单状态说明
                print(f"\n💡 平仓订单说明:")
                print(f"   - 市价平仓单会立即执行")
                print(f"   - 开平仓模式下的平仓单自动具有只减仓逻辑")
                print(f"   - 平仓后该方向持仓将减少或归零")
                
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
        print(f"   运行 demo22_订单查询.py 查看详情")
        
        print(f"\n2️⃣  查看持仓变化:")
        print(f"   订单成交后查看持仓变化，运行 demo07_持仓查询.py")
        
        if OPERATION_MODE == 1:
            print(f"\n3️⃣  修改减仓订单:")
            print(f"   如需修改价格或数量，运行 demo05_订单修改.py")
            
            print(f"\n4️⃣  撤销减仓订单:")
            print(f"   如需撤销订单，运行 demo06_订单撤销.py")

    # =============================================================================
    # 重要说明和风险提醒
    # =============================================================================
    
    print(f"\n📚 重要说明:")
    if OPERATION_MODE == 1:
        print(f"   - reduceOnly=true 确保订单只会减少持仓")
        print(f"   - 适用于币币杠杆和买卖模式下的交割/永续")
        print(f"   - 减仓数量不能超过当前持仓数量")
        print(f"   - 所有反方向挂单+当前减仓数量不能超过仓位资产")
    else:
        print(f"   - 开平仓模式下，平仓单自动具有只减仓逻辑")
        print(f"   - side=sell + posSide=long = 平多仓")
        print(f"   - side=buy + posSide=short = 平空仓")
        print(f"   - 市价平仓通常能快速成交")
    
    print(f"\n⚠️  风险提醒:")
    print(f"   - 确保有足够的持仓可供减仓/平仓")
    print(f"   - 注意市场波动对成交价格的影响")
    print(f"   - 平仓后请及时检查持仓状态")
    print(f"   - 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")

except Exception as e:
    print(f"❌ 解析订单结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")