#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 33 - 双向止盈止损委托
=================================

功能说明：
- 双向止盈止损委托（ordType=oco）
- 同时设置止盈和止损条件
- 一边触发后另一边自动失效（One-Cancels-Other）
- 适用于已有持仓的全面风险控制

使用场景：
- 持有仓位后，同时设置盈利目标和止损保护
- 无论哪个条件先触发，都会自动平仓并取消另一个条件
- 最常用的止盈止损策略

OCO特点：
- 同时设置上下两个触发条件
- 互斥执行：只有一个会被触发
- 触发后另一个自动撤销

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
print("🎯 OKX双向止盈止损委托工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 策略订单参数配置
# =============================================================================

INST_ID = "ETH-USDT-SWAP"              # 交易对：ETH永续合约
TRADE_MODE = "cross"                   # 交易模式：cross=全仓, isolated=逐仓
POS_SIDE = "long"                      # 持仓方向：long=多头, short=空头
ORDER_TYPE = "oco"                     # 双向止盈止损
SIZE = "0.1"                           # 委托数量（张）

# 价格偏移设置（可根据市场波动性调整）
TP_OFFSET_PCT = 0.03  # 3% 止盈
SL_OFFSET_PCT = 0.02  # 2% 止损

# 客户策略订单ID（可选参数，不填写）
# ALGO_CLIENT_ORDER_ID = f"oco{int(time.time())}{random.randint(100, 999)}"

print(f"🎯 策略委托配置:")
print(f"   交易对: {INST_ID}")
print(f"   交易模式: {TRADE_MODE}")
print(f"   策略类型: {ORDER_TYPE} (双向止盈止损)")
print(f"   目标持仓方向: {POS_SIDE}")
print(f"   数量: {SIZE}张")
print(f"   止盈偏移: {TP_OFFSET_PCT*100}%")
print(f"   止损偏移: {SL_OFFSET_PCT*100}%")
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
# 获取当前价格并计算止盈止损价格
# =============================================================================

try:
    ticker = marketDataAPI.get_ticker(instId=INST_ID)
    if ticker.get('code') != '0' or not ticker.get('data'):
        raise RuntimeError(f"获取ticker失败: {ticker}")
    
    last = float(ticker['data'][0]['last'])
    computed_side = 'buy' if POS_SIDE == 'short' else 'sell'
    
    # 根据持仓方向计算止盈止损价格
    if POS_SIDE == 'long':
        # 多头持仓：止盈在上方，止损在下方
        tp_px = round(last * (1 + TP_OFFSET_PCT), 4)
        sl_px = round(last * (1 - SL_OFFSET_PCT), 4)
        tp_desc = f"止盈: 价格上涨至 ≥ ${tp_px} 时卖出平多"
        sl_desc = f"止损: 价格下跌至 ≤ ${sl_px} 时卖出平多"
    else:  # short
        # 空头持仓：止盈在下方，止损在上方
        tp_px = round(last * (1 - TP_OFFSET_PCT), 4)
        sl_px = round(last * (1 + SL_OFFSET_PCT), 4)
        tp_desc = f"止盈: 价格下跌至 ≤ ${tp_px} 时买入平空"
        sl_desc = f"止损: 价格上涨至 ≥ ${sl_px} 时买入平空"
    
    print(f"📈 最新价(last): ${last}")
    print(f"📌 计算得到的参数:")
    print(f"   持仓方向: {POS_SIDE}")
    print(f"   平仓方向: {computed_side}")
    print(f"   {tp_desc}")
    print(f"   {sl_desc}")
    print(f"   ⚡ OCO机制: 任一条件触发，另一条件自动取消")

except Exception as e:
    print(f"❌ 获取并计算价格失败: {e}")
    exit(1)

# =============================================================================
# 下策略委托单
# =============================================================================

try:
    print(f"\n🚀 正在下策略委托单...")
    print(f"   策略类型: {ORDER_TYPE}（双向止盈止损）")
    print(f"   等待任一价格条件触发...")
    
    # OCO订单需要同时设置止盈和止损参数
    result = tradeAPI.place_algo_order(
        instId=INST_ID,
        tdMode=TRADE_MODE,
        side=computed_side,
        posSide=POS_SIDE,
        ordType=ORDER_TYPE,
        sz=SIZE,
        # 止盈参数
        tpTriggerPx=str(tp_px),
        tpOrdPx='-1',               # -1表示市价执行
        tpTriggerPxType='last',
        # 止损参数
        slTriggerPx=str(sl_px),
        slOrdPx='-1',               # -1表示市价执行
        slTriggerPxType='last'
    )
    
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
        print("📋 OCO策略订单详情")
        print("=" * 60)
        
        print(f"🆔 策略订单ID:      {algoId}")
        # print(f"🏷️  客户订单ID:      {ALGO_CLIENT_ORDER_ID}")
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 下单时间:        {order_time}")
        
        if sCode == '0':
            print(f"\n🎉 OCO双向止盈止损委托创建成功！")
            print(f"   策略已激活，等待任一触发条件...")
            
            print(f"\n💡 OCO执行逻辑 (posSide={POS_SIDE}):")
            print(f"   ✅ {tp_desc}")
            print(f"   ❌ {sl_desc}")
            print(f"   🔄 互斥执行: 任一条件触发后，另一条件自动撤销")
            print(f"   ⚡ 触发机制: 基于最新价格（last price）")
            print(f"   💰 执行方式: 市价自动执行")
            
            # 风险收益分析
            if POS_SIDE == 'long':
                profit_range = ((tp_px / last) - 1) * 100
                loss_range = (1 - (sl_px / last)) * 100
            else:
                profit_range = (1 - (tp_px / last)) * 100
                loss_range = ((sl_px / last) - 1) * 100
            
            print(f"\n📊 风险收益分析:")
            print(f"   📈 预期盈利: +{profit_range:.2f}% (如果触发止盈)")
            print(f"   📉 最大亏损: -{loss_range:.2f}% (如果触发止损)")
            print(f"   🎯 盈亏比: {profit_range/loss_range:.2f}:1")
            
            print(f"\n⚠️  重要提醒:")
            print(f"   - OCO确保风险可控，收益可期")
            print(f"   - 触发后立即以市价执行，注意滑点风险")
            print(f"   - 请确保有相应的{POS_SIDE}仓位")
            print(f"   - 可随时查询或撤销未触发的策略")
            
        else:
            print(f"\n❌ 策略委托单创建失败!")
            print(f"   错误代码: {sCode}")
            print(f"   错误信息: {sMsg}")
            
            # 常见错误处理建议
            if sCode in ['51008', '51009']:
                print(f"\n💡 可能的解决方案:")
                print(f"   - 检查是否有对应的持仓")
                print(f"   - 确认触发价格设置合理")
                print(f"   - 验证账户余额是否充足")
    
    else:
        print(f"\n❌ 请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 解析策略订单结果时发生错误: {e}")
    print(f"原始结果: {result}")

# =============================================================================
# 使用技巧和建议
# =============================================================================

print(f"\n📚 OCO使用技巧:")
print(f"   1. 合理设置盈亏比（建议1.5:1以上）")
print(f"   2. 根据市场波动性调整触发距离")
print(f"   3. 避免触发价格过于接近当前价")
print(f"   4. 定期检查和调整策略参数")

print(f"\n🔧 参数调整建议:")
print(f"   - 提高TP_OFFSET_PCT增加止盈空间")
print(f"   - 降低SL_OFFSET_PCT减少止损风险")
print(f"   - 修改POS_SIDE适配不同持仓方向")

print(f"\n💡 下一步操作:")
print(f"   - 查询策略状态: demo_查询策略订单.py")
print(f"   - 修改策略参数: demo_修改策略订单.py")
print(f"   - 撤销策略: demo_撤销策略订单.py")

print("\n🎯 Demo运行完成！")
