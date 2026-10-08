#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 32 - 单向止盈止损委托
=====================================

功能说明：
- 单向止盈止损委托（ordType=conditional）
- 可以设置单独的止盈或单独的止损
- 适用于已有持仓的风险控制
- 触发后自动执行平仓操作

使用场景：
- 已有多头持仓，只想设置止损保护
- 已有空头持仓，只想设置止盈目标
- 风险控制的基础策略

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
print("🎯 OKX单向止盈止损委托工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 策略订单参数配置
# =============================================================================

INST_ID = "BTC-USDT-SWAP"              # 交易对：BTC永续合约
TRADE_MODE = "cross"                   # 交易模式：cross=全仓, isolated=逐仓
POS_SIDE = "long"                      # 持仓方向：long=多头, short=空头
ORDER_TYPE = "conditional"             # 单向止盈止损
SIZE = "0.01"                          # 委托数量（张）

# 策略选择：只能选择其中一种
STRATEGY_TYPE = "stop_loss"            # "take_profit" = 只设止盈, "stop_loss" = 只设止损

# 动态价格偏移（百分比）
TP_OFFSET_PCT = 0.02  # 2% 止盈
SL_OFFSET_PCT = 0.02  # 2% 止损

# 客户策略订单ID（可选参数，不填写）
# ALGO_CLIENT_ORDER_ID = f"cond{int(time.time())}{random.randint(100, 999)}"

print(f"🎯 策略委托配置:")
print(f"   交易对: {INST_ID}")
print(f"   交易模式: {TRADE_MODE}")
print(f"   策略类型: {ORDER_TYPE} ({STRATEGY_TYPE})")
print(f"   目标持仓方向: {POS_SIDE}")
print(f"   数量: {SIZE}张")
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
# 获取当前价格并计算触发价格
# =============================================================================

try:
    ticker = marketDataAPI.get_ticker(instId=INST_ID)
    if ticker.get('code') != '0' or not ticker.get('data'):
        raise RuntimeError(f"获取ticker失败: {ticker}")
    
    last = float(ticker['data'][0]['last'])
    computed_side = 'buy' if POS_SIDE == 'short' else 'sell'
    
    # 根据策略类型和持仓方向计算触发价格
    if STRATEGY_TYPE == "take_profit":
        # 只设置止盈
        if POS_SIDE == 'long':
            trigger_px = round(last * (1 + TP_OFFSET_PCT), 4)   # 多仓止盈在现价上方
            strategy_desc = f"多仓止盈: 价格上涨至 ≥ ${trigger_px} 时平仓"
        else:  # short
            trigger_px = round(last * (1 - TP_OFFSET_PCT), 4)   # 空仓止盈在现价下方
            strategy_desc = f"空仓止盈: 价格下跌至 ≤ ${trigger_px} 时平仓"
        trigger_type = "tpTriggerPx"
        
    else:  # stop_loss
        # 只设置止损
        if POS_SIDE == 'long':
            trigger_px = round(last * (1 - SL_OFFSET_PCT), 4)   # 多仓止损在现价下方
            strategy_desc = f"多仓止损: 价格下跌至 ≤ ${trigger_px} 时平仓"
        else:  # short
            trigger_px = round(last * (1 + SL_OFFSET_PCT), 4)   # 空仓止损在现价上方
            strategy_desc = f"空仓止损: 价格上涨至 ≥ ${trigger_px} 时平仓"
        trigger_type = "slTriggerPx"
    
    print(f"📈 最新价(last): ${last}")
    print(f"📌 计算得到的参数:")
    print(f"   持仓方向: {POS_SIDE}")
    print(f"   平仓方向: {computed_side}")
    print(f"   {strategy_desc}")

except Exception as e:
    print(f"❌ 获取并计算价格失败: {e}")
    exit(1)

# =============================================================================
# 下策略委托单
# =============================================================================

try:
    print(f"\n🚀 正在下策略委托单...")
    print(f"   策略类型: {ORDER_TYPE}（单向止盈止损）")
    print(f"   {strategy_desc}")
    
    # 构建请求参数
    order_params = {
        'instId': INST_ID,
        'tdMode': TRADE_MODE,
        'side': computed_side,
        'posSide': POS_SIDE,
        'ordType': ORDER_TYPE,
        'sz': SIZE
    }
    
    # 根据策略类型添加相应的触发参数
    if STRATEGY_TYPE == "take_profit":
        order_params.update({
            'tpTriggerPx': str(trigger_px),
            'tpOrdPx': '-1',  # -1表示市价执行
            'tpTriggerPxType': 'last'
        })
    else:  # stop_loss
        order_params.update({
            'slTriggerPx': str(trigger_px),
            'slOrdPx': '-1',  # -1表示市价执行
            'slTriggerPxType': 'last'
        })
    
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
        print("📋 策略订单详情")
        print("=" * 60)
        
        print(f"🆔 策略订单ID:      {algoId}")
        # print(f"🏷️  客户订单ID:      {ALGO_CLIENT_ORDER_ID}")
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 下单时间:        {order_time}")
        
        if sCode == '0':
            print(f"\n🎉 单向止盈止损委托创建成功！")
            print(f"   策略已激活，等待触发条件...")
            print(f"\n💡 策略执行逻辑:")
            print(f"   {strategy_desc}")
            print(f"   ⚡ 触发机制: 基于最新价格（last price）")
            print(f"   🔄 执行方式: 市价自动执行")
            
            # 风险提醒
            print(f"\n⚠️  重要提醒:")
            print(f"   - 单向止盈止损只设置一个触发条件")
            print(f"   - 触发后将以市价执行，可能存在滑点")
            print(f"   - 请确保有对应的持仓才能触发")
            print(f"   - 可以随时撤销未触发的策略委托")
            
        else:
            print(f"\n❌ 策略委托单创建失败!")
            print(f"   错误代码: {sCode}")
            print(f"   错误信息: {sMsg}")
    
    else:
        print(f"\n❌ 请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 解析策略订单结果时发生错误: {e}")
    print(f"原始结果: {result}")

# =============================================================================
# 使用说明
# =============================================================================

print(f"\n📚 使用说明:")
print(f"   1. 修改 STRATEGY_TYPE 可选择止盈或止损")
print(f"      - 'take_profit': 只设置止盈")
print(f"      - 'stop_loss': 只设置止损")
print(f"   2. 修改 POS_SIDE 指定持仓方向")
print(f"   3. 调整价格偏移百分比控制触发距离")
print(f"   4. 确保有相应持仓才能正常触发")

print(f"\n💡 下一步建议:")
print(f"   - 查询策略订单状态: demo33_查询策略订单.py")
print(f"   - 撤销策略委托: demo34_撤销策略订单.py")
print(f"   - 尝试双向止盈止损: demo31_双向止盈止损(oco).py")

print("\n🎯 Demo运行完成！")
