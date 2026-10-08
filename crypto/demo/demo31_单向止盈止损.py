#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 31 - 策略委托概览
============================

功能说明：
- 策略委托下单概览和快速入门
- 展示各种策略类型的基本用法
- 本文件使用OCO（双向止盈止损）作为示例

策略类型完整列表：
- conditional：单向止盈止损委托 → 详见 demo32_单向止盈止损(conditional).py
- oco：双向止盈止损委托 → 本文件演示 + demo33_双向止盈止损(oco).py
- chase：追逐限价委托 → 详见 demo34_追逐限价委托(chase).py
- trigger：计划委托 → 详见 demo35_计划委托(trigger).py
- move_order_stop：移动止盈止损 → 详见 demo36_移动止盈止损(move_order_stop).py
- twap：时间加权委托 → 详见 demo37_时间加权委托(twap).py

策略委托原理：
1. 针对现有持仓：conditional、oco、move_order_stop
2. 计划开仓：trigger、chase、twap
3. 不占用保证金，触发时自动执行

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-place-algo-order

作者：OKX API Demo
创建时间：2025-01-14
版本：v1.1
"""

import okx.Trade as Trade
import datetime
import json
import time
# 新增：行情API
import okx.MarketData as MarketData

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
print("🎯 OKX策略委托概览工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 策略订单参数配置
# =============================================================================

# 交易产品配置
INST_ID = "LTC-USDT-SWAP"           # 交易对：LTC永续合约
TRADE_MODE = "cross"                # 交易模式：cross=全仓, isolated=逐仓
# 客户策略订单ID（可选参数，不填写）
import random
# ALGO_CLIENT_ORDER_ID = f"{int(time.time())}{random.randint(100, 999)}"

# 基础订单参数
# 原SIDE含义在conditional下容易混淆，这里根据持仓方向自动计算真正的平仓方向
SIDE = "sell"                       # 初始占位，不再直接使用
POS_SIDE = "short"                  # 持仓方向：long=多头, short=空头
ORDER_TYPE = "oco"                  # 使用OCO同时设置止盈止损
SIZE = "0.1"                         # 委托数量（张）

# 动态止盈止损偏移（百分比），可按需调整
TP_OFFSET_PCT = 0.01  # 1%
SL_OFFSET_PCT = 0.01  # 1%

print(f"🎯 策略委托配置:")
print(f"   交易对: {INST_ID}")
print(f"   交易模式: {TRADE_MODE}")
print(f"   策略类型: {ORDER_TYPE}")
print(f"   目标持仓方向: {POS_SIDE}")
print(f"   数量: {SIZE}张")
print("=" * 60)

# =============================================================================
# 初始化交易/行情API
# =============================================================================

try:
    # 创建交易API实例
    tradeAPI = Trade.TradeAPI(apikey, secretkey, passphrase, False, flag)
    marketDataAPI = MarketData.MarketAPI(flag=flag)
    print("✅ 交易API初始化成功")
except Exception as e:
    print(f"❌ 交易API初始化失败: {e}")
    exit(1)

# 获取最新价格并计算合规的TP/SL价格
try:
    ticker = marketDataAPI.get_ticker(instId=INST_ID)
    if ticker.get('code') != '0' or not ticker.get('data'):
        raise RuntimeError(f"获取ticker失败: {ticker}")
    last = float(ticker['data'][0]['last'])
    # 根据持仓方向确定平仓方向与TP/SL相对关系
    computed_side = 'buy' if POS_SIDE == 'short' else 'sell'
    if POS_SIDE == 'long':
        tp_px = round(last * (1 + TP_OFFSET_PCT), 4)   # 多仓止盈在现价上方
        sl_px = round(last * (1 - SL_OFFSET_PCT), 4)   # 多仓止损在现价下方
    else:  # short
        tp_px = round(last * (1 - TP_OFFSET_PCT), 4)   # 空仓止盈在现价下方
        sl_px = round(last * (1 + SL_OFFSET_PCT), 4)   # 空仓止损在现价上方

    print(f"📈 最新价(last): ${last}")
    print(f"📌 计算得到的参数 (posSide={POS_SIDE}, side={computed_side}):")
    print(f"   止盈触发价(tpTriggerPx): ${tp_px}  (tpOrdPx=-1 市价)")
    print(f"   止损触发价(slTriggerPx): ${sl_px}  (slOrdPx=-1 市价)")
except Exception as e:
    print(f"❌ 获取并计算价格失败: {e}")
    exit(1)

# =============================================================================
# 下策略委托单
# =============================================================================

try:
    print(f"\n🚀 正在下策略委托单...")
    print(f"   策略类型: {ORDER_TYPE}（双向止盈止损）")
    print(f"   等待价格触发条件后自动执行...")

    # oco 同时设置止盈止损；posSide区分多空；side为平仓方向；触发类型统一使用last
    result = tradeAPI.place_algo_order(
        instId=INST_ID,
        tdMode=TRADE_MODE,
        side=computed_side,
        posSide=POS_SIDE,
        ordType=ORDER_TYPE,
        sz=SIZE,
        tpTriggerPx=str(tp_px),
        tpOrdPx='-1',
        tpTriggerPxType='last',
        slTriggerPx=str(sl_px),
        slOrdPx='-1',
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
    # 显示原始返回数据（调试用）
    print(f"\n📋 策略订单返回数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # 提取关键字段
    code = result.get('code', '')                   # 请求状态码

    if code == '0':
        # 请求成功
        order_data = result['data'][0]

        sCode = order_data.get('sCode', '')         # 订单状态码  
        sMsg = order_data.get('sMsg', '')           # 订单状态信息
        algoId = order_data.get('algoId', '')       # 策略订单ID
        ts = order_data.get('ts', '')               # 订单时间戳

        # 时间戳转换为可读格式
        if ts:
            order_time = datetime.datetime.fromtimestamp(
                int(ts) / 1000
            ).strftime('%Y-%m-%d %H:%M:%S')
        else:
            order_time = "未知"

        # =============================================================================
        # 美化输出策略订单结果
        # =============================================================================

        print("\n" + "=" * 60)
        print("📋 策略订单详情")
        print("=" * 60)

        print(f"🆔 策略订单ID:      {algoId}")
        # print(f"🏷️  客户订单ID:      {ALGO_CLIENT_ORDER_ID}")
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 下单时间:        {order_time}")

        # 根据状态码判断订单是否成功
        if sCode == '0':
            print(f"\n🎉 策略委托单创建成功！")
            print(f"   策略已激活，等待触发条件...")

            # 策略订单状态说明
            print(f"\n💡 策略执行逻辑 (posSide={POS_SIDE}):")
            if POS_SIDE == 'long':
                print(f"   📈 止盈条件: 价格上涨至 ≥ ${tp_px} 时，以市价卖出平多")
                print(f"   📉 止损条件: 价格下跌至 ≤ ${sl_px} 时，以市价卖出平多")
            else:
                print(f"   📉 止盈条件: 价格下跌至 ≤ ${tp_px} 时，以市价买入平空")
                print(f"   📈 止损条件: 价格上涨至 ≥ ${sl_px} 时，以市价买入平空")
            print(f"   ⚡ 触发机制: 基于最新价格（last price）")
            print(f"   🔄 执行方式: 自动触发，无需人工干预")
        else:
            print(f"\n❌ 策略委托单创建失败!")
            print(f"   错误代码: {sCode}")
            print(f"   错误信息: {sMsg}")

    else:
        # 请求失败
        print(f"\n❌ 请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

    # =============================================================================
    # 策略类型说明
    # =============================================================================

    print(f"\n📚 其他策略类型详细Demo:")
    print(f"   📋 conditional: demo32_单向止盈止损(conditional).py")
    print(f"   🔄 oco: demo33_双向止盈止损(oco).py")
    print(f"   🏃 chase: demo34_追逐限价委托(chase).py")
    print(f"   ⏰ trigger: demo35_计划委托(trigger).py")
    print(f"   🎯 move_order_stop: demo36_移动止盈止损(move_order_stop).py")
    print(f"   📊 twap: demo37_时间加权委托(twap).py")

    print(f"\n⚠️  风险提醒:")
    print(f"   - 策略委托基于市场价格自动触发，请确保参数设置合理")
    print(f"   - 多仓要求: 止盈>当前价>止损；空仓要求: 止盈<当前价<止损")
    print(f"   - 策略执行时可能因市场波动导致滑点")
    print(f"   - 请定期检查策略状态，及时调整参数")
    print(f"   - 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")
    print(f"   - 策略委托不会预先占用仓位或保证金")

except Exception as e:
    print(f"❌ 解析策略订单结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 下一步建议:")
print("   - 尝试单向止盈止损: demo32_单向止盈止损(conditional).py")
print("   - 尝试追逐限价委托: demo34_追逐限价委托(chase).py")
print("   - 尝试计划委托: demo35_计划委托(trigger).py")
print("   - 查询策略订单状态和管理")
print("💡 提示: 每种策略类型都有专门的demo文件，建议逐个学习")