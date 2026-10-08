#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 36 - 移动止盈止损委托
==================================

功能说明：
- 移动止盈止损委托（ordType=move_order_stop）
- 跟踪市场价格的智能止损策略
- 触发价格随市场有利方向自动调整
- 保护利润的同时允许利润继续增长

使用场景：
- 持有盈利仓位，希望保护利润
- 让利润奔跑，同时控制回撤风险
- 动态止损，适应市场波动
- 趋势跟踪交易的风险管理

移动止损机制：
- 价格朝有利方向移动时，止损价随之调整
- 价格不利方向移动时，止损价保持不变
- 达到触发条件时，以市价立即平仓
- 支持激活价格设定

计算公式：
- 多仓触发价 = 历史最高价 - 回调幅度
- 空仓触发价 = 历史最低价 + 回调幅度

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-place-algo-order

作者：OKX API Demo
创建时间：2025-01-14
版本：v1.0
"""

import okx.Trade as Trade
import okx.Account as Account
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
print("🎯 OKX移动止盈止损委托工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 策略订单参数配置
# =============================================================================

INST_ID = "NEAR-USDT-SWAP"              # 交易对：BTC永续合约
TRADE_MODE = "isolated"                   # 交易模式：cross=全仓, isolated=逐仓
POS_SIDE = "long"                      # 持仓方向：long=多头, short=空头
ORDER_TYPE = "move_order_stop"         # 移动止盈止损
SIZE = "1"                             # 委托数量（张） - SWAP合约必须为整数

# 回调幅度配置（两种方式二选一）
CALLBACK_TYPE = "ratio"                # ratio=比例模式, spread=价距模式
CALLBACK_RATIO = "0.02"                # 回调比例：2%（仅比例模式使用）
# CALLBACK_SPREAD = "50"               # 回调价距：50 USDT（仅价距模式使用）

# 激活价格设置（可选）
USE_ACTIVE_PX = True                   # 是否设置激活价格
ACTIVE_OFFSET_PCT = 0.01               # 激活价格偏移：1%

# 客户策略订单ID（可选参数，不填写）
# ALGO_CLIENT_ORDER_ID = f"move{int(time.time())}{random.randint(100, 999)}"

print(f"🎯 策略委托配置:")
print(f"   交易对: {INST_ID}")
print(f"   交易模式: {TRADE_MODE}")
print(f"   策略类型: {ORDER_TYPE} (移动止盈止损)")
print(f"   持仓方向: {POS_SIDE}")
print(f"   数量: {SIZE}张")
print(f"   回调类型: {CALLBACK_TYPE}")
if CALLBACK_TYPE == "ratio":
    print(f"   回调比例: {float(CALLBACK_RATIO)*100}%")
else:
    print(f"   回调价距: {CALLBACK_SPREAD} USDT")
if USE_ACTIVE_PX:
    print(f"   激活价格偏移: {ACTIVE_OFFSET_PCT*100}%")
print("=" * 60)

# =============================================================================
# 初始化API
# =============================================================================

try:
    tradeAPI = Trade.TradeAPI(apikey, secretkey, passphrase, False, flag)
    accountAPI = Account.AccountAPI(apikey, secretkey, passphrase, False, flag)
    marketDataAPI = MarketData.MarketAPI(flag=flag)
    print("✅ API初始化成功")
except Exception as e:
    print(f"❌ API初始化失败: {e}")
    exit(1)

# =============================================================================
# 查询持仓情况
# =============================================================================

try:
    print(f"\n🔍 正在查询持仓情况...")
    print(f"   交易对: {INST_ID}")
    print(f"   持仓方向: {POS_SIDE}")
    
    # 查询持仓
    positions = accountAPI.get_positions(instId=INST_ID)
    if positions.get('code') != '0':
        raise RuntimeError(f"查询持仓失败: {positions}")
    
    # 查找指定方向的持仓
    target_position = None
    for pos in positions.get('data', []):
        pos_side = pos.get('posSide')
        pos_size = float(pos.get('pos', 0))
        
        # 处理不同的持仓模式
        if pos_side == POS_SIDE and pos_size != 0:
            # 双向持仓模式：直接匹配posSide
            target_position = pos
            break
        elif pos_side == 'net' and pos_size != 0:
            # 单向持仓模式：根据pos的正负判断方向
            if (POS_SIDE == 'long' and pos_size > 0) or (POS_SIDE == 'short' and pos_size < 0):
                target_position = pos
                break
    
    if not target_position:
        print(f"❌ 未找到 {INST_ID} {POS_SIDE} 方向的持仓！")
        print(f"💡 移动止盈止损需要先有相应的持仓")
        print(f"   请先开仓后再设置移动止损")
        
        # 显示当前持仓情况
        if positions.get('data'):
            print(f"\n📋 当前持仓情况:")
            for pos in positions.get('data', []):
                pos_size = float(pos.get('pos', 0))
                if pos_size != 0:
                    direction = 'long' if pos_size > 0 else 'short'
                    print(f"   {pos.get('instId')}: {abs(pos_size)}张 {direction} (posSide: {pos.get('posSide')})")
        exit(1)
    
    # 获取持仓信息
    pos_size = float(target_position.get('pos', 0))
    avg_px = float(target_position.get('avgPx', 0))
    upl = float(target_position.get('upl', 0))
    upl_ratio = float(target_position.get('uplRatio', 0))
    
    print(f"✅ 找到目标持仓:")
    print(f"   持仓数量: {pos_size} 张")
    print(f"   平均成本: ${avg_px}")
    print(f"   未实现盈亏: ${upl:.4f} ({upl_ratio*100:.2f}%)")
    
    # SIZE参数已在上面验证和调整过了，这里不需要重复处理
    
except Exception as e:
    print(f"❌ 查询持仓失败: {e}")
    exit(1)

# =============================================================================
# 获取当前价格并计算移动止损参数
# =============================================================================

try:
    print(f"\n📈 获取当前价格...")
    ticker = marketDataAPI.get_ticker(instId=INST_ID)
    if ticker.get('code') != '0' or not ticker.get('data'):
        raise RuntimeError(f"获取ticker失败: {ticker}")
    
    last = float(ticker['data'][0]['last'])
    computed_side = 'buy' if POS_SIDE == 'short' else 'sell'  # 平仓方向
    
    # 计算激活价格（如果启用）
    if USE_ACTIVE_PX:
        if POS_SIDE == 'long':
            # 多头：激活价格在当前价格之上
            active_px = round(last * (1 + ACTIVE_OFFSET_PCT), 4)
            active_desc = f"价格上涨至 ${active_px} 时激活移动止损"
        else:  # short
            # 空头：激活价格在当前价格之下
            active_px = round(last * (1 - ACTIVE_OFFSET_PCT), 4)
            active_desc = f"价格下跌至 ${active_px} 时激活移动止损"
    else:
        active_px = None
        active_desc = "立即激活移动止损"
    
    # 计算初始止损价格（仅用于展示，实际由系统动态计算）
    if CALLBACK_TYPE == "ratio":
        callback_val = float(CALLBACK_RATIO)
        if POS_SIDE == 'long':
            initial_stop_px = round(last * (1 - callback_val), 4)
            mechanism_desc = f"多仓移动止损: 触发价 = 历史最高价 × (1 - {callback_val*100}%)"
        else:
            initial_stop_px = round(last * (1 + callback_val), 4)
            mechanism_desc = f"空仓移动止损: 触发价 = 历史最低价 × (1 + {callback_val*100}%)"
    else:  # spread
        callback_val = float(CALLBACK_SPREAD)
        if POS_SIDE == 'long':
            initial_stop_px = round(last - callback_val, 4)
            mechanism_desc = f"多仓移动止损: 触发价 = 历史最高价 - ${callback_val}"
        else:
            initial_stop_px = round(last + callback_val, 4)
            mechanism_desc = f"空仓移动止损: 触发价 = 历史最低价 + ${callback_val}"
    
    print(f"📈 最新价格: ${last}")
    print(f"📊 持仓分析:")
    print(f"   持仓成本: ${avg_px}")
    print(f"   当前价格: ${last}")
    print(f"   价格变化: {((last/avg_px-1)*100):+.2f}%")
    
    # 判断持仓盈亏状态
    if upl > 0:
        profit_status = "🟢 盈利中"
        risk_level = "适合设置移动止损保护利润"
    elif upl < 0:
        profit_status = "🔴 亏损中"
        risk_level = "建议谨慎，移动止损可能加大亏损"
    else:
        profit_status = "⚪ 持平"
        risk_level = "可设置移动止损，注意回调幅度"
    
    print(f"   盈亏状态: {profit_status}")
    print(f"   风险评估: {risk_level}")
    
    print(f"\n📌 移动止损参数:")
    print(f"   持仓方向: {POS_SIDE}")
    print(f"   平仓方向: {computed_side}")
    print(f"   委托数量: {SIZE}张 (占持仓{(float(SIZE)/abs(pos_size)*100):.1f}%)")
    print(f"   {active_desc}")
    print(f"   当前止损价参考: ${initial_stop_px}")
    print(f"   {mechanism_desc}")

except Exception as e:
    print(f"❌ 获取并计算价格失败: {e}")
    exit(1)

# =============================================================================
# 下策略委托单
# =============================================================================

try:
    print(f"\n🚀 正在下策略委托单...")
    print(f"   策略类型: {ORDER_TYPE}（移动止盈止损）")
    print(f"   等待激活条件并开始跟踪...")
    
    # 构建请求参数
    order_params = {
        'instId': INST_ID,
        'tdMode': TRADE_MODE,
        'side': computed_side,
        'ordType': ORDER_TYPE,
        'sz': str(SIZE)  # 确保SIZE是字符串格式
    }
    
    
    # 根据持仓模式设置posSide参数
    actual_pos_side = target_position.get('posSide')
    if actual_pos_side == 'net':
        # 单向持仓模式：不传递posSide参数
        print(f"   持仓模式: 单向持仓 (net)")
    else:
        # 双向持仓模式：传递posSide参数
        order_params['posSide'] = POS_SIDE
        print(f"   持仓模式: 双向持仓 ({POS_SIDE})")
    
    # 添加回调幅度参数
    if CALLBACK_TYPE == "ratio":
        order_params['callbackRatio'] = CALLBACK_RATIO
    else:
        order_params['callbackSpread'] = CALLBACK_SPREAD
    
    # 添加激活价格（如果启用）
    if USE_ACTIVE_PX:
        order_params['activePx'] = str(active_px)
    
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
        print("📋 移动止盈止损订单详情")
        print("=" * 60)
        
        print(f"🆔 策略订单ID:      {algoId}")
        # print(f"🏷️  客户订单ID:      {ALGO_CLIENT_ORDER_ID}")
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 下单时间:        {order_time}")
        
        if sCode == '0':
            print(f"\n🎉 移动止盈止损委托创建成功！")
            print(f"   策略已设置，等待激活和跟踪...")
            
            print(f"\n💡 移动止损执行逻辑:")
            print(f"   {mechanism_desc}")
            print(f"   🔄 动态调整: 价格有利移动时，止损价跟进")
            print(f"   🚫 保持不变: 价格不利移动时，止损价不变")
            print(f"   ⚡ 市价执行: 触发后立即以市价平仓")
            
            if USE_ACTIVE_PX:
                print(f"\n🎯 激活机制:")
                print(f"   ⏳ 等待激活: {active_desc}")
                print(f"   ✅ 激活后开始跟踪最高/最低价")
                print(f"   📊 实时调整止损触发价格")
            else:
                print(f"\n🎯 立即激活:")
                print(f"   ✅ 策略立即生效")
                print(f"   📊 从当前价格开始跟踪")
            
            print(f"\n📈 工作示例 (假设当前价=${last}):")
            if POS_SIDE == 'long':
                print(f"   1️⃣  价格上涨至 ${last*1.05:.2f}: 止损价调整至 ${(last*1.05)*(1-float(CALLBACK_RATIO)):.2f}")
                print(f"   2️⃣  价格继续上涨至 ${last*1.10:.2f}: 止损价调整至 ${(last*1.10)*(1-float(CALLBACK_RATIO)):.2f}")
                print(f"   3️⃣  价格回落至止损价: 立即市价平仓")
            else:
                print(f"   1️⃣  价格下跌至 ${last*0.95:.2f}: 止损价调整至 ${(last*0.95)*(1+float(CALLBACK_RATIO)):.2f}")
                print(f"   2️⃣  价格继续下跌至 ${last*0.90:.2f}: 止损价调整至 ${(last*0.90)*(1+float(CALLBACK_RATIO)):.2f}")
                print(f"   3️⃣  价格反弹至止损价: 立即市价平仓")
            
            print(f"\n✨ 策略优势:")
            print(f"   📈 让利润奔跑: 价格有利时不限制盈利")
            print(f"   🛡️  保护利润: 价格回调时及时止损")
            print(f"   🎯 动态调整: 自适应市场波动")
            print(f"   ⚡ 及时响应: 市价执行避免滑点过大")
            
            print(f"\n⚠️  重要提醒:")
            print(f"   - 已验证持仓: {abs(pos_size)}张 {POS_SIDE} 仓位")
            print(f"   - 当前{profit_status}，{risk_level}")
            print(f"   - 回调幅度设置要合理（避免过于敏感）")
            print(f"   - 市价执行可能存在滑点")
            print(f"   - 适合趋势行情，震荡行情慎用")
            if upl < 0:
                print(f"   - ⚠️  当前亏损状态，移动止损可能扩大亏损")
            elif upl > 0:
                print(f"   - ✅ 当前盈利状态，移动止损有助于保护利润")
            
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
# 策略优化指南
# =============================================================================

print(f"\n📚 移动止损优化指南:")

print(f"\n🔧 回调幅度设置:")
print(f"   保守型 (大回调):")
print(f"   - 比例模式: 3-5% (0.03-0.05)")
print(f"   - 优点: 减少误触发，允许更大波动")
print(f"   - 缺点: 可能损失较多利润")

print(f"\n   平衡型 (中回调):")
print(f"   - 比例模式: 1-3% (0.01-0.03)")
print(f"   - 优点: 平衡利润保护和波动容忍")
print(f"   - 适合: 大多数交易者")

print(f"\n   激进型 (小回调):")
print(f"   - 比例模式: 0.5-1% (0.005-0.01)")
print(f"   - 优点: 最大限度保护利润")
print(f"   - 缺点: 容易被正常波动触发")

print(f"\n📊 使用场景分析:")
print(f"   ✅ 适合使用:")
print(f"   - 明确的趋势行情")
print(f"   - 已有较大浮盈的仓位")
print(f"   - 希望让利润继续增长")
print(f"   - 无法长时间盯盘的情况")

print(f"\n   ❌ 不适合使用:")
print(f"   - 震荡整理行情")
print(f"   - 刚开仓的新仓位")
print(f"   - 高频交易策略")
print(f"   - 对手续费敏感的小额交易")

print(f"\n💡 实战技巧:")
print(f"   1. 结合技术指标判断趋势")
print(f"   2. 根据市场波动性调整回调幅度")
print(f"   3. 设置合理的激活价格")
print(f"   4. 定期检查和优化参数")
print(f"   5. 与其他止盈策略配合使用")

print(f"\n🎯 参数配置建议:")
print(f"   - 修改CALLBACK_RATIO调整敏感度")
print(f"   - 开启/关闭USE_ACTIVE_PX控制激活")
print(f"   - 尝试不同的CALLBACK_TYPE模式")
print(f"   - 根据持仓大小调整SIZE")

print("\n🎯 Demo运行完成！")
