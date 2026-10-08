#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 35 - 计划委托
==========================

功能说明：
- 计划委托（ordType=trigger）
- 当市场价格到达触发价格时自动下单
- 支持突破、回撤等多种交易策略
- 可选择市价或限价执行

使用场景：
- 突破交易：价格突破阻力位时开仓
- 回撤交易：价格回落到支撑位时买入
- 定点开仓：在特定价位自动建仓
- 分批建仓：设置多个触发价格逐步建仓

计划委托特点：
- 不占用保证金和仓位
- 触发后按设定价格下单
- 支持附带止盈止损
- 适用于币币、交割和永续

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
print("🎯 OKX计划委托工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 策略订单参数配置
# =============================================================================

INST_ID = "ETH-USDT-SWAP"              # 交易对：ETH永续合约
TRADE_MODE = "cross"                   # 交易模式：cross=全仓, isolated=逐仓
SIDE = "buy"                           # 订单方向：buy=买入, sell=卖出
POS_SIDE = "long"                      # 持仓方向：long=多头, short=空头
ORDER_TYPE = "trigger"                 # 计划委托
SIZE = "0.1"                           # 委托数量（张）

# 触发条件配置
TRIGGER_STRATEGY = "breakout"          # 策略类型：breakout=突破, pullback=回撤
TRIGGER_OFFSET_PCT = 0.02              # 触发价格偏移：2%

# 执行方式配置
EXECUTION_TYPE = "limit"               # 执行方式：market=市价, limit=限价
LIMIT_OFFSET_PCT = 0.001               # 限价偏移：0.1%（仅限价模式使用）

# 触发价格类型
TRIGGER_PX_TYPE = "last"               # last=最新价, index=指数价, mark=标记价

# 附带止盈止损设置（可选）
USE_ATTACH_TP_SL = True                # 是否附带止盈止损
ATTACH_TP_OFFSET = 0.03                # 附带止盈偏移：3%
ATTACH_SL_OFFSET = 0.02                # 附带止损偏移：2%

# 客户策略订单ID（可选参数，不填写）
# ALGO_CLIENT_ORDER_ID = f"trigger{int(time.time())}{random.randint(100, 999)}"

print(f"🎯 策略委托配置:")
print(f"   交易对: {INST_ID}")
print(f"   交易模式: {TRADE_MODE}")
print(f"   策略类型: {ORDER_TYPE} (计划委托)")
print(f"   订单方向: {SIDE}")
print(f"   持仓方向: {POS_SIDE}")
print(f"   数量: {SIZE}张")
print(f"   触发策略: {TRIGGER_STRATEGY}")
print(f"   执行方式: {EXECUTION_TYPE}")
if USE_ATTACH_TP_SL:
    print(f"   附带止盈止损: 是")
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
    
    # 根据策略类型和方向计算触发价格
    if TRIGGER_STRATEGY == "breakout":
        # 突破策略
        if SIDE == "buy":
            # 买入突破：价格上破阻力位
            trigger_px = round(last * (1 + TRIGGER_OFFSET_PCT), 4)
            strategy_desc = f"突破买入: 价格上破 ${trigger_px} 时开多"
        else:
            # 卖出突破：价格下破支撑位
            trigger_px = round(last * (1 - TRIGGER_OFFSET_PCT), 4)
            strategy_desc = f"突破卖出: 价格下破 ${trigger_px} 时开空"
    else:  # pullback
        # 回撤策略
        if SIDE == "buy":
            # 买入回撤：价格回落到支撑位
            trigger_px = round(last * (1 - TRIGGER_OFFSET_PCT), 4)
            strategy_desc = f"回撤买入: 价格回落至 ${trigger_px} 时开多"
        else:
            # 卖出回撤：价格反弹到阻力位
            trigger_px = round(last * (1 + TRIGGER_OFFSET_PCT), 4)
            strategy_desc = f"回撤卖出: 价格反弹至 ${trigger_px} 时开空"
    
    # 计算执行价格
    if EXECUTION_TYPE == "market":
        order_px = "-1"  # 市价执行
        exec_desc = "市价执行"
    else:  # limit
        if SIDE == "buy":
            limit_px = round(trigger_px * (1 + LIMIT_OFFSET_PCT), 4)
        else:
            limit_px = round(trigger_px * (1 - LIMIT_OFFSET_PCT), 4)
        order_px = str(limit_px)
        exec_desc = f"限价 ${limit_px} 执行"
    
    print(f"📈 最新价(last): ${last}")
    print(f"📌 计算得到的参数:")
    print(f"   {strategy_desc}")
    print(f"   触发价格: ${trigger_px}")
    print(f"   执行方式: {exec_desc}")

except Exception as e:
    print(f"❌ 获取并计算价格失败: {e}")
    exit(1)

# =============================================================================
# 下策略委托单
# =============================================================================

try:
    print(f"\n🚀 正在下策略委托单...")
    print(f"   策略类型: {ORDER_TYPE}（计划委托）")
    print(f"   等待触发价格条件...")
    
    # 构建基础请求参数
    order_params = {
        'instId': INST_ID,
        'tdMode': TRADE_MODE,
        'side': SIDE,
        'posSide': POS_SIDE,
        'ordType': ORDER_TYPE,
        'sz': SIZE,
        'triggerPx': str(trigger_px),
        'orderPx': order_px,
        'triggerPxType': TRIGGER_PX_TYPE
    }
    
    # 添加附带止盈止损（如果启用）
    if USE_ATTACH_TP_SL:
        # 计算止盈止损价格
        if SIDE == "buy":  # 开多
            attach_tp_px = round(trigger_px * (1 + ATTACH_TP_OFFSET), 4)
            attach_sl_px = round(trigger_px * (1 - ATTACH_SL_OFFSET), 4)
        else:  # 开空
            attach_tp_px = round(trigger_px * (1 - ATTACH_TP_OFFSET), 4)
            attach_sl_px = round(trigger_px * (1 + ATTACH_SL_OFFSET), 4)
        
        attach_algo_ord = {
            'tpTriggerPx': str(attach_tp_px),
            'tpOrdPx': '-1',
            'tpTriggerPxType': 'last',
            'slTriggerPx': str(attach_sl_px),
            'slOrdPx': '-1',
            'slTriggerPxType': 'last'
        }
        
        # order_params['attachAlgoOrds'] = [attach_algo_ord]  # 当前SDK版本不支持此参数
        
        print(f"   建议附带止盈: ${attach_tp_px}（需手动设置）")
        print(f"   建议附带止损: ${attach_sl_px}（需手动设置）")
        print(f"⚠️ 注意：当前SDK不支持自动附带止盈止损，需手动设置")
    
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
        print("📋 计划委托订单详情")
        print("=" * 60)
        
        print(f"🆔 策略订单ID:      {algoId}")
        # print(f"🏷️  客户订单ID:      {ALGO_CLIENT_ORDER_ID}")
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 下单时间:        {order_time}")
        
        if sCode == '0':
            print(f"\n🎉 计划委托创建成功！")
            print(f"   策略已激活，等待触发条件...")
            
            print(f"\n💡 计划委托执行逻辑:")
            print(f"   🎯 {strategy_desc}")
            print(f"   ⚡ 触发机制: 基于{TRIGGER_PX_TYPE}价格")
            print(f"   💰 执行方式: {exec_desc}")
            
            if USE_ATTACH_TP_SL:
                print(f"\n🛡️  附带止盈止损（手动设置）:")
                print(f"   ✅ 建议设置止盈: ${attach_tp_px}")
                print(f"   ❌ 建议设置止损: ${attach_sl_px}")
                print(f"   🔄 需在主订单成交后手动下单")
            
            print(f"\n📊 策略分析:")
            if TRIGGER_STRATEGY == "breakout":
                print(f"   📈 突破策略: 跟随趋势建仓")
                print(f"   🎯 适用: 趋势行情、技术突破")
                print(f"   ⚠️  注意: 假突破风险")
            else:
                print(f"   📉 回撤策略: 逢低建仓/逢高减仓")
                print(f"   🎯 适用: 震荡行情、价值投资")
                print(f"   ⚠️  注意: 持续下跌风险")
            
            print(f"\n⚠️  重要提醒:")
            print(f"   - 委托不占用资金，触发后才下单")
            print(f"   - 确保账户有足够资金和仓位")
            print(f"   - 市价执行可能存在滑点")
            print(f"   - 可随时撤销未触发的委托")
            
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
# 策略使用指南
# =============================================================================

print(f"\n📚 计划委托使用指南:")

print(f"\n🎯 策略选择:")
print(f"   突破策略 (breakout):")
print(f"   ✅ 适合强势行情、技术突破")
print(f"   ✅ 跟随趋势，避免踏空")
print(f"   ❌ 容易追高，注意假突破")

print(f"\n   回撤策略 (pullback):")
print(f"   ✅ 适合震荡行情、逢低买入")
print(f"   ✅ 获得更好的入场价格")
print(f"   ❌ 可能错过快速上涨")

print(f"\n⚙️  参数优化:")
print(f"   - 触发偏移小: 更敏感，成交频率高")
print(f"   - 触发偏移大: 更稳健，减少假信号")
print(f"   - 市价执行: 确保成交，但有滑点")
print(f"   - 限价执行: 控制成本，但可能不成交")

print(f"\n💡 实战建议:")
print(f"   1. 结合技术分析设定触发价格")
print(f"   2. 根据市场波动性调整偏移")
print(f"   3. 启用附带止盈止损控制风险")
print(f"   4. 定期审查和调整策略参数")

print(f"\n🔧 配置示例:")
print(f"   保守型: TRIGGER_OFFSET_PCT=0.01 (1%)")
print(f"   平衡型: TRIGGER_OFFSET_PCT=0.02 (2%)")
print(f"   激进型: TRIGGER_OFFSET_PCT=0.05 (5%)")

print(f"\n⚠️  SDK限制提示:")
print(f"   - 当前使用的OKX Python SDK版本不支持自动附带止盈止损")
print(f"   - 计划委托成交后，请手动设置止盈止损策略")
print(f"   - 可使用demo33或demo32文件中的方法手动添加")

print("\n🎯 Demo运行完成！")
