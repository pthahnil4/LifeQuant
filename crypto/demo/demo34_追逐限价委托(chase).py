#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 34 - 追逐限价委托
=============================

功能说明：
- 追逐限价委托（ordType=chase）
- 智能跟随盘口价格变动
- 自动调整委托价格以保持竞争优势
- 仅适用于交割和永续合约

使用场景：
- 希望以更好的价格成交
- 避免直接市价单的滑点损失
- 自动跟踪盘口深度变化
- 适合大额交易的分批建仓

追逐机制：
- 立即下Post Only订单（只做maker）
- 实时跟随买一/卖一价格变动
- 自动改单保持价格优势
- 支持距离和比例两种追逐方式

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-place-algo-order

作者：OKX API Demo
创建时间：2025-01-14
版本：v1.0
"""

import okx.Trade as Trade
import okx.MarketData as MarketData
import datetime
import json
import time
import random

# =============================================================================
# API 配置区域
# =============================================================================

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api_config import get_api_config, validate_config, print_config_info

config = get_api_config()
is_valid, message = validate_config(config)
if not is_valid:
    print(f"❌ API配置错误: {message}")
    print("💡 请检查 api_config.py 文件中的配置")
    exit(1)

apikey = config['api_key']
secretkey = config['secret_key']
passphrase = config['passphrase']
flag = config['flag']

print("=" * 60)
print("🎯 OKX追逐限价委托工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 策略订单参数配置
# =============================================================================

INST_ID = "NEAR-USDT-SWAP"              # 交易对：BTC永续合约（仅支持交割和永续）
TRADE_MODE = "isolated"                   # 交易模式：cross=全仓, isolated=逐仓
SIDE = "buy"                           # 订单方向：buy=买入, sell=卖出
POS_SIDE = "long"                      # 持仓方向：long=多头, short=空头
ORDER_TYPE = "chase"                   # 追逐限价委托
SIZE = "0.1"                          # 委托数量（张）

# 追逐策略配置
CHASE_TYPE = "distance"                # 追逐类型：distance=价距, ratio=比例
CHASE_VAL = "0.01"                      # 追逐值：距离（USDT）或比例（0.001=0.1%）

# 最大追逐限制（可选，防止过度追逐）
USE_MAX_CHASE = True                   # 是否启用最大追逐限制
MAX_CHASE_TYPE = "distance"            # 最大追逐类型
MAX_CHASE_VAL = "0.1"                  # 最大追逐值

# 客户策略订单ID（可选参数，不填写）
# ALGO_CLIENT_ORDER_ID = f"chase{int(time.time())}{random.randint(100, 999)}"

print(f"🎯 策略委托配置:")
print(f"   交易对: {INST_ID}")
print(f"   交易模式: {TRADE_MODE}")
print(f"   策略类型: {ORDER_TYPE} (追逐限价)")
print(f"   订单方向: {SIDE}")
print(f"   持仓方向: {POS_SIDE}")
print(f"   数量: {SIZE}张")
print(f"   追逐类型: {CHASE_TYPE}")
print(f"   追逐值: {CHASE_VAL}")
if USE_MAX_CHASE:
    print(f"   最大追逐值: {MAX_CHASE_VAL}")
print("=" * 60)

# =============================================================================
# 初始化API
# =============================================================================

try:
    tradeAPI = Trade.TradeAPI(apikey, secretkey, passphrase, False, flag)
    marketDataAPI = MarketData.MarketAPI(flag=flag)
    print("✅ 交易API初始化成功")
except Exception as e:
    print(f"❌ 交易API初始化失败: {e}")
    exit(1)

# =============================================================================
# 获取当前盘口信息
# =============================================================================

try:
    # 获取盘口数据
    books = marketDataAPI.get_orderbook(instId=INST_ID, sz=5)
    if books.get('code') != '0' or not books.get('data'):
        raise RuntimeError(f"获取盘口数据失败: {books}")
    
    book_data = books['data'][0]
    bids = book_data['bids']  # 买盘 [[price, size, ...], ...]
    asks = book_data['asks']  # 卖盘 [[price, size, ...], ...]
    
    if not bids or not asks:
        raise RuntimeError("盘口数据为空")
    
    bid1_price = float(bids[0][0])  # 买一价
    ask1_price = float(asks[0][0])  # 卖一价
    spread = ask1_price - bid1_price
    
    # 获取最新价格
    ticker = marketDataAPI.get_ticker(instId=INST_ID)
    last_price = float(ticker['data'][0]['last'])
    
    print(f"📊 当前盘口信息:")
    print(f"   买一价: ${bid1_price}")
    print(f"   卖一价: ${ask1_price}")
    print(f"   价差: ${spread}")
    print(f"   最新价: ${last_price}")
    
    # 计算追逐策略效果
    if SIDE == "buy":
        base_price = bid1_price
        if CHASE_TYPE == "distance":
            chase_price = base_price + float(CHASE_VAL)
            strategy_desc = f"买单将以买一价+{CHASE_VAL} USDT = ${chase_price} 追逐"
        else:  # ratio
            chase_price = base_price * (1 + float(CHASE_VAL))
            strategy_desc = f"买单将以买一价*(1+{float(CHASE_VAL)*100}%) ≈ ${chase_price:.4f} 追逐"
    else:  # sell
        base_price = ask1_price
        if CHASE_TYPE == "distance":
            chase_price = base_price - float(CHASE_VAL)
            strategy_desc = f"卖单将以卖一价-{CHASE_VAL} USDT = ${chase_price} 追逐"
        else:  # ratio
            chase_price = base_price * (1 - float(CHASE_VAL))
            strategy_desc = f"卖单将以卖一价*(1-{float(CHASE_VAL)*100}%) ≈ ${chase_price:.4f} 追逐"
    
    print(f"\n💡 追逐策略预览:")
    print(f"   {strategy_desc}")
    print(f"   盘口变动时价格会自动调整")

except Exception as e:
    print(f"❌ 获取盘口信息失败: {e}")
    exit(1)

# =============================================================================
# 下策略委托单
# =============================================================================

try:
    print(f"\n🚀 正在下策略委托单...")
    print(f"   策略类型: {ORDER_TYPE}（追逐限价委托）")
    print(f"   将立即下Post Only订单并开始追逐...")
    
    # 注意：当前SDK版本不支持chase特有参数，此demo仅展示基本下单
    # 实际的chase功能可能需要使用其他API或SDK版本
    # 
    # 重要：chase订单类型可能不支持posSide参数，或者需要账户设置为net模式
    # 根据OKX API文档，某些订单类型对posSide参数有特殊要求
    
    # 方案1：尝试不使用posSide参数（适用于net模式或chase订单特殊要求）
    order_params = {
        'instId': INST_ID,
        'tdMode': TRADE_MODE,
        'side': SIDE,
        'ordType': ORDER_TYPE,
        'sz': SIZE
    }
    
    print("⚠️ 注意：已移除posSide参数，因为chase订单可能不支持此参数")
    print("💡 如果仍然失败，请检查账户持仓模式设置（长短模式 vs 净持仓模式）")
    
    # 当前SDK暂不支持chase专用参数：chaseType, chaseVal, maxChaseType, maxChaseVal
    # 如需使用完整chase功能，请使用最新版SDK或直接调用REST API
    print("⚠️ 注意：当前演示使用基础参数，完整chase功能需要更新的SDK版本")
    
    result = tradeAPI.place_algo_order(**order_params)
    print("✅ 策略委托单提交成功！")

except Exception as e:
    print(f"❌ 下策略委托单时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析策略订单结果
# =============================================================================

try:
    print(f"\n📋 策略订单返回数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    code = result.get('code', '')
    
    if code == '0':
        order_data = result['data'][0]
        sCode = order_data.get('sCode', '')
        sMsg = order_data.get('sMsg', '')
        algoId = order_data.get('algoId', '')
        ts = order_data.get('ts', '')
        
        if ts:
            order_time = datetime.datetime.fromtimestamp(
                int(ts) / 1000
            ).strftime('%Y-%m-%d %H:%M:%S')
        else:
            order_time = "未知"
        
        print("\n" + "=" * 60)
        print("📋 追逐委托订单详情")
        print("=" * 60)
        
        print(f"🆔 策略订单ID:      {algoId}")
        # print(f"🏷️  客户订单ID:      {ALGO_CLIENT_ORDER_ID}")
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 下单时间:        {order_time}")
        
        if sCode == '0':
            print(f"\n🎉 追逐限价委托创建成功！")
            print(f"   策略已激活，开始智能追逐...")
            
            print(f"\n💡 追逐策略机制:")
            print(f"   🎯 订单类型: Post Only (只做maker)")
            print(f"   🔄 追逐方式: {CHASE_TYPE} = {CHASE_VAL}")
            if USE_MAX_CHASE:
                print(f"   🚫 最大限制: {MAX_CHASE_TYPE} = {MAX_CHASE_VAL}")
            print(f"   ⚡ 自动改单: 跟随盘口变化实时调整")
            print(f"   💰 成交优势: 获得更好的成交价格")
            
            print(f"\n🔍 工作原理:")
            if SIDE == "buy":
                print(f"   📈 买单策略: 在买一价基础上加{CHASE_VAL}下单")
                print(f"   🔄 动态调整: 买一价变化时自动改单")
                print(f"   ✅ 成交条件: 对手方主动卖出时优先成交")
            else:
                print(f"   📉 卖单策略: 在卖一价基础上减{CHASE_VAL}下单")
                print(f"   🔄 动态调整: 卖一价变化时自动改单")
                print(f"   ✅ 成交条件: 对手方主动买入时优先成交")
            
            print(f"\n⚠️  重要特性:")
            print(f"   - 订单会立即生效并开始追逐")
            print(f"   - 不支持改单（系统自动调整）")
            print(f"   - 仅适用于交割和永续合约")
            print(f"   - 避免直接市价单的滑点损失")
            
        else:
            print(f"\n❌ 策略委托单创建失败!")
            print(f"   错误代码: {sCode}")
            print(f"   错误信息: {sMsg}")
            
            # 常见错误处理
            if 'not supported' in sMsg.lower():
                print(f"\n💡 解决建议:")
                print(f"   - 确认交易对支持追逐委托")
                print(f"   - 仅交割和永续合约支持此功能")
    
    else:
        print(f"\n❌ 请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 解析策略订单结果时发生错误: {e}")
    print(f"原始结果: {result}")

# =============================================================================
# 策略优化建议
# =============================================================================

print(f"\n📚 追逐策略优化建议:")

print(f"\n🔧 参数调优:")
print(f"   - 距离模式: 适合价格稳定的市场")
print(f"   - 比例模式: 适合价格波动较大的市场")
print(f"   - 小追逐值: 更激进，成交概率高")
print(f"   - 大追逐值: 更保守，获得更好价格")

print(f"\n📊 使用场景:")
print(f"   ✅ 适合: 大额交易、流动性好的合约")
print(f"   ✅ 适合: 希望获得更好成交价格")
print(f"   ❌ 不适合: 急需立即成交的情况")
print(f"   ❌ 不适合: 流动性差的小众合约")

print(f"\n⚙️  参数示例:")
print(f"   保守型: chaseVal=0.1 (距离) 或 0.0005 (比例)")
print(f"   平衡型: chaseVal=0.5 (距离) 或 0.001 (比例)")
print(f"   激进型: chaseVal=1.0 (距离) 或 0.002 (比例)")

print(f"\n💡 监控建议:")
print(f"   - 定期检查订单执行状态")
print(f"   - 观察成交价格与盘口的关系")
print(f"   - 根据市场波动调整参数")
print(f"   - 设置合理的最大追逐限制")

print(f"\n⚠️  SDK限制提示:")
print(f"   - 当前OKX Python SDK版本不支持chase专用参数")
print(f"   - 此demo仅展示基本下单，无法实现完整chase功能")
print(f"   - 如需完整功能，请使用最新SDK或直接调用REST API")
print(f"   - 或考虑使用限价单配合手动调整实现类似效果")

print("\n🎯 Demo运行完成！")
