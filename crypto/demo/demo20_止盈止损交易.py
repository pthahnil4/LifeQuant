#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 20 - 止盈止损交易
==============================

功能说明：
- 下带止盈止损的订单
- 风险控制策略
- 自动化盈亏管理

止盈止损说明：
- 止盈(Take Profit): 价格上涨到目标价位时自动卖出获利
- 止损(Stop Loss): 价格下跌到止损价位时自动卖出止损
- 可以同时设置，实现自动化风险管理

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-place-order

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
flag = config['flag']

print("=" * 60)
print("🛡️ OKX止盈止损交易工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 交易参数配置
# =============================================================================

# 主订单配置
INST_ID = "LTC-USDT-SWAP"           # 交易对
TRADE_MODE = "cross"                # 交易模式
CLIENT_ORDER_ID = "demo04_tp_sl"    # 主订单ID

# 主订单参数
SIDE = "buy"                        # 买入开仓
ORDER_TYPE = "limit"                # 限价单
PRICE = "70.0"                      # 开仓价格
SIZE = "0.2"                        # 开仓数量

# 止盈止损配置
TAKE_PROFIT_PRICE = "75.0"          # 止盈价格 (+7.14%收益)
STOP_LOSS_PRICE = "65.0"            # 止损价格 (-7.14%止损)
TP_SIZE = "0.2"                     # 止盈数量
SL_SIZE = "0.1"                     # 止损数量

print(f"🎯 交易策略配置:")
print(f"   交易对: {INST_ID}")
print(f"   开仓: {SIDE} {SIZE}张 @ ${PRICE}")
print(f"   止盈: ${TAKE_PROFIT_PRICE} ({TP_SIZE}张)")
print(f"   止损: ${STOP_LOSS_PRICE} ({SL_SIZE}张)")
print(f"   预期收益: +{((float(TAKE_PROFIT_PRICE)/float(PRICE)-1)*100):.1f}%")
print(f"   最大亏损: -{((1-float(STOP_LOSS_PRICE)/float(PRICE))*100):.1f}%")
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
# 构建止盈止损订单
# =============================================================================

# 构建附加算法订单（止盈止损）
# attachAlgoOrds参数用于设置止盈止损条件
attachAlgoOrds = [
    {
        # 止盈订单配置
        "attachAlgoClOrdId": "tp001",           # 止盈订单ID
        "tpTriggerPx": TAKE_PROFIT_PRICE,       # 止盈触发价格
        "tpOrdPx": "-1",                        # -1表示市价止盈
        "sz": TP_SIZE                           # 止盈仓位数量
    },
    {
        # 止损订单配置  
        "attachAlgoClOrdId": "sl001",           # 止损订单ID
        "slTriggerPx": STOP_LOSS_PRICE,         # 止损触发价格
        "slOrdPx": "-1",                        # -1表示市价止损
        "sz": SL_SIZE                           # 止损仓位数量
    }
]

print(f"🛡️ 风险控制策略:")
print(f"   止盈触发: 价格达到 ${TAKE_PROFIT_PRICE}")
print(f"   止损触发: 价格跌破 ${STOP_LOSS_PRICE}")
print(f"   执行方式: 市价单立即执行")

# =============================================================================
# 提交止盈止损订单
# =============================================================================

try:
    print(f"\n🚀 正在提交止盈止损订单...")
    
    # 下带止盈止损的限价单
    # attachAlgoOrds参数包含止盈止损配置
    result = tradeAPI.place_order(
        instId=INST_ID,
        tdMode=TRADE_MODE,
        clOrdId=CLIENT_ORDER_ID,
        side=SIDE,
        ordType=ORDER_TYPE,
        px=PRICE,
        sz=SIZE,
        attachAlgoOrds=attachAlgoOrds          # 关键参数：止盈止损配置
    )
    
    print("✅ 订单提交成功！")
    
except Exception as e:
    print(f"❌ 下单时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析订单结果
# =============================================================================

try:
    print(f"\n📋 订单返回数据:")
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
        print("🛡️ 止盈止损订单详情")
        print("=" * 60)
        
        print(f"🆔 主订单ID:        {ordId}")
        print(f"🏷️  客户订单ID:      {CLIENT_ORDER_ID}")
        print(f"📊 订单状态:        {sCode} - {sMsg}")
        print(f"🕐 下单时间:        {order_time}")
        
        if sCode == '0':
            print(f"\n🎉 止盈止损订单创建成功！")
            
            # 策略说明
            print(f"\n🎯 自动化策略已激活:")
            print(f"   📈 当价格上涨至 ${TAKE_PROFIT_PRICE} 时：")
            print(f"      - 自动卖出 {TP_SIZE}张 获利了结")
            print(f"      - 预期收益: +{((float(TAKE_PROFIT_PRICE)/float(PRICE)-1)*100):.1f}%")
            
            print(f"\n   📉 当价格下跌至 ${STOP_LOSS_PRICE} 时：")
            print(f"      - 自动卖出 {SL_SIZE}张 止损离场")
            print(f"      - 最大亏损: -{((1-float(STOP_LOSS_PRICE)/float(PRICE))*100):.1f}%")
            
            print(f"\n   🤖 执行特点:")
            print(f"      - 24小时自动监控，无需人工干预")
            print(f"      - 触发后立即市价执行")
            print(f"      - 有效控制风险和收益")
            
        else:
            print(f"\n❌ 订单创建失败!")
            print(f"   错误代码: {sCode}")
            print(f"   错误信息: {sMsg}")
            
    else:
        print(f"\n❌ 请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 解析结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo05_订单查询.py 监控订单状态") 