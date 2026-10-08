#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 37 - 时间加权委托
==============================

功能说明：
- 时间加权委托（ordType=twap，Time-Weighted Average Price）
- 大额订单智能拆分为多笔小额订单
- 按时间间隔分批执行，减少市场冲击
- 获得接近平均价格的成交结果

使用场景：
- 大额交易避免市场冲击
- 机构级别的资金进出场
- 分散执行风险，平滑成交价格
- 长期建仓或减仓策略

TWAP策略特点：
- 智能拆分大单为小单
- 按设定时间间隔执行
- 优于盘口价格的优势定价
- 自动化执行，无需人工干预

核心参数：
- szLimit: 单笔执行数量
- timeInterval: 执行时间间隔
- pxLimit: 价格限制条件
- pxVar/pxSpread: 价格优势设定

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
print("🎯 OKX时间加权委托工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 策略订单参数配置
# =============================================================================

INST_ID = "NEAR-USDT-SWAP"              # 交易对：ETH永续合约
TRADE_MODE = "isolated"               # 交易模式：cross=全仓, isolated=逐仓
                                       # 注意：TWAP订单在某些环境下需要使用逐仓模式
SIDE = "buy"                           # 订单方向：buy=买入, sell=卖出
POS_SIDE = "long"                      # 持仓方向：long=多头, short=空头
ORDER_TYPE = "twap"                    # 时间加权委托
TOTAL_SIZE = "0.9"                     # 总委托数量（张）

# TWAP核心参数
SZ_LIMIT = "0.2"                       # 单笔执行数量（张）- 实盘最小值0.2
TIME_INTERVAL = "120"                   # 执行间隔（秒）
PX_LIMIT_OFFSET_PCT = 0.005            # 限制价偏移：0.5%

# 价格优势设置（二选一）
PRICE_ADVANTAGE_TYPE = "ratio"         # ratio=比例模式, spread=价距模式
PX_VAR = "0.001"                       # 价格优势比例：0.1%
# PX_SPREAD = "1.0"                    # 价格优势价距：1.0 USDT

# 客户策略订单ID（可选参数，不填写）
# ALGO_CLIENT_ORDER_ID = f"twap{int(time.time())}{random.randint(100, 999)}"

# 计算预估执行信息
estimated_batches = int(float(TOTAL_SIZE) / float(SZ_LIMIT))
estimated_duration_minutes = (estimated_batches * int(TIME_INTERVAL)) / 60

print(f"🎯 策略委托配置:")
print(f"   交易对: {INST_ID}")
print(f"   交易模式: {TRADE_MODE}")
print(f"   策略类型: {ORDER_TYPE} (时间加权)")
print(f"   订单方向: {SIDE}")
print(f"   持仓方向: {POS_SIDE}")
print(f"   总数量: {TOTAL_SIZE}张")
print(f"   单次数量: {SZ_LIMIT}张")
print(f"   执行间隔: {TIME_INTERVAL}秒")
print(f"   预估批次: {estimated_batches}次")
print(f"   预估时长: {estimated_duration_minutes:.1f}分钟")
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
# 获取当前价格并计算TWAP参数
# =============================================================================

try:
    ticker = marketDataAPI.get_ticker(instId=INST_ID)
    if ticker.get('code') != '0' or not ticker.get('data'):
        raise RuntimeError(f"获取ticker失败: {ticker}")
    
    last = float(ticker['data'][0]['last'])
    
    # 计算限制价格
    if SIDE == "buy":
        # 买单：限制价高于当前价（买入上限）
        px_limit = round(last * (1 + PX_LIMIT_OFFSET_PCT), 4)
        limit_desc = f"买单限制价: ${px_limit} (当前价+{PX_LIMIT_OFFSET_PCT*100}%)"
    else:
        # 卖单：限制价低于当前价（卖出下限）
        px_limit = round(last * (1 - PX_LIMIT_OFFSET_PCT), 4)
        limit_desc = f"卖单限制价: ${px_limit} (当前价-{PX_LIMIT_OFFSET_PCT*100}%)"
    
    # 价格优势说明
    if PRICE_ADVANTAGE_TYPE == "ratio":
        advantage_desc = f"价格优势: {float(PX_VAR)*100}% 比例优于盘口"
    else:
        advantage_desc = f"价格优势: ${PX_SPREAD} 价距优于盘口"
    
    print(f"📈 最新价(last): ${last}")
    print(f"📌 计算得到的参数:")
    print(f"   {limit_desc}")
    print(f"   {advantage_desc}")
    print(f"   执行条件: 市价 {'<' if SIDE == 'buy' else '>'} 限制价时开始执行")

except Exception as e:
    print(f"❌ 获取并计算价格失败: {e}")
    exit(1)

# =============================================================================
# 下策略委托单
# =============================================================================

try:
    print(f"\n🚀 正在下策略委托单...")
    print(f"   策略类型: {ORDER_TYPE}（时间加权委托）")
    print(f"   将按计划分批执行...")
    
    # 重要：TWAP订单类型可能不支持posSide参数，或者需要账户设置为net模式
    # 根据OKX API文档，某些订单类型对posSide参数有特殊要求
    
    # 构建请求参数（实盘环境下移除posSide参数）
    order_params = {
        'instId': INST_ID,
        'tdMode': TRADE_MODE,
        'side': SIDE,
        'ordType': ORDER_TYPE,
        'sz': TOTAL_SIZE,
        'szLimit': SZ_LIMIT,
        'timeInterval': TIME_INTERVAL,
        'pxLimit': str(px_limit)
    }
    
    print("⚠️ 注意：实盘环境下已移除posSide参数")
    print("💡 TWAP订单在实盘环境下可能不支持posSide参数，使用净持仓模式")
    
    # 添加价格优势参数
    if PRICE_ADVANTAGE_TYPE == "ratio":
        order_params['pxVar'] = PX_VAR
    else:
        order_params['pxSpread'] = PX_SPREAD
    
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
            # 计算预估完成时间
            estimated_completion = datetime.datetime.fromtimestamp(
                (int(ts) / 1000) + (estimated_batches * int(TIME_INTERVAL))
            ).strftime('%Y-%m-%d %H:%M:%S')
        else:
            order_time = "未知"
            estimated_completion = "未知"
        
        print("\n" + "=" * 60)
        print("📋 TWAP委托订单详情")
        print("=" * 60)
        
        print(f"🆔 策略订单ID:      {algoId}")
        # print(f"🏷️  客户订单ID:      {ALGO_CLIENT_ORDER_ID}")
        print(f"📊 订单状态码:      {sCode}")
        print(f"💬 状态信息:        {sMsg}")
        print(f"🕐 下单时间:        {order_time}")
        print(f"⏰ 预估完成时间:    {estimated_completion}")
        
        if sCode == '0':
            print(f"\n🎉 TWAP时间加权委托创建成功！")
            print(f"   策略已激活，开始分批执行...")
            
            print(f"\n💡 TWAP执行计划:")
            print(f"   📦 总数量: {TOTAL_SIZE}张")
            print(f"   🔄 分批执行: 每次{SZ_LIMIT}张，共{estimated_batches}次")
            print(f"   ⏱️  时间间隔: {TIME_INTERVAL}秒")
            print(f"   🎯 价格条件: 市价 {'<' if SIDE == 'buy' else '>'} ${px_limit} 时执行")
            print(f"   💰 价格优势: {advantage_desc}")
            
            print(f"\n🔄 执行机制:")
            if SIDE == "buy":
                print(f"   1️⃣  监控条件: 市价 < ${px_limit} 时开始执行")
                print(f"   2️⃣  下单策略: 买一价 + {PX_VAR if PRICE_ADVANTAGE_TYPE == 'ratio' else PX_SPREAD}的价格下单")
                print(f"   3️⃣  时间控制: 每{TIME_INTERVAL}秒执行{SZ_LIMIT}张")
                print(f"   4️⃣  自动重复: 直到完成{TOTAL_SIZE}张")
            else:
                print(f"   1️⃣  监控条件: 市价 > ${px_limit} 时开始执行")
                print(f"   2️⃣  下单策略: 卖一价 - {PX_VAR if PRICE_ADVANTAGE_TYPE == 'ratio' else PX_SPREAD}的价格下单")
                print(f"   3️⃣  时间控制: 每{TIME_INTERVAL}秒执行{SZ_LIMIT}张")
                print(f"   4️⃣  自动重复: 直到完成{TOTAL_SIZE}张")
            
            print(f"\n📊 预期效果:")
            print(f"   ✅ 减少市场冲击: 分批小额执行")
            print(f"   ✅ 平均成交价: 接近TWAP价格")
            print(f"   ✅ 价格优势: 优于直接市价单")
            print(f"   ✅ 自动化执行: 无需人工干预")
            
            print(f"\n⚠️  重要提醒:")
            print(f"   - 策略执行需要{estimated_duration_minutes:.1f}分钟")
            print(f"   - 市场价格不满足条件时会暂停执行")
            print(f"   - 可随时查询进度或撤销未完成部分")
            print(f"   - 适合大额交易，小额交易手续费占比高")
            
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
# TWAP策略优化指南
# =============================================================================

print(f"\n📚 TWAP策略优化指南:")

print(f"\n🔧 参数调优建议:")
print(f"   单笔数量 (szLimit):")
print(f"   - 太大: 市场冲击仍然明显")
print(f"   - 太小: 执行次数多，手续费高")
print(f"   - 建议: 总量的5-20%，根据市场流动性调整")

print(f"\n   时间间隔 (timeInterval):")
print(f"   - 太短: 类似一次性下单，冲击大")
print(f"   - 太长: 执行周期过长，市场风险增加")
print(f"   - 建议: 30-300秒，根据预期执行时长调整")

print(f"\n   价格限制 (pxLimit):")
print(f"   - 买单: 设置买入价格上限，避免追高")
print(f"   - 卖单: 设置卖出价格下限，避免杀跌")
print(f"   - 建议: 当前价±0.3-1%的合理范围")

print(f"\n   价格优势 (pxVar/pxSpread):")
print(f"   - 比例模式: 适合高价商品，0.05-0.2%")
print(f"   - 价距模式: 适合价格稳定，固定价距")
print(f"   - 目标: 获得比市价更好的成交价")

print(f"\n📊 适用场景分析:")
print(f"   ✅ 最佳使用场景:")
print(f"   - 大额交易 (> 10万USDT)")
print(f"   - 流动性好的主流币对")
print(f"   - 长期建仓/减仓策略")
print(f"   - 不急需立即完成的交易")

print(f"\n   ❌ 不适合场景:")
print(f"   - 小额交易 (< 1万USDT)")
print(f"   - 急需立即成交")
print(f"   - 流动性差的小币种")
print(f"   - 短期投机交易")

print(f"\n💡 实战技巧:")
print(f"   1. 在市场相对稳定时使用")
print(f"   2. 结合量化分析选择执行时机")
print(f"   3. 监控执行进度，必要时调整参数")
print(f"   4. 考虑时区和交易活跃度")
print(f"   5. 预留足够的执行时间窗口")

print(f"\n🎯 配置模板:")
print(f"   保守型 (慢速执行):")
print(f"   - szLimit: 总量的5%")
print(f"   - timeInterval: 120-300秒")
print(f"   - pxVar: 0.001-0.002")

print(f"\n   平衡型 (标准执行):")
print(f"   - szLimit: 总量的10%")
print(f"   - timeInterval: 60-120秒")
print(f"   - pxVar: 0.001")

print(f"\n   激进型 (快速执行):")
print(f"   - szLimit: 总量的20%")
print(f"   - timeInterval: 30-60秒")
print(f"   - pxVar: 0.0005")

print("\n🎯 Demo运行完成！")

# =============================================================================
# 实盘与模拟盘差异说明
# =============================================================================

print("\n⚠️  实盘与模拟盘差异提醒:")
print("   📋 关键差异总结:")
print("   - 模拟盘: 支持posSide参数，建议使用逐仓模式")
print("   - 实盘: 不支持posSide参数，使用净持仓模式")
print("   - 单笔数量限制: 实盘最小0.2张，模拟盘可能更小")

print("\n   🔧 参数配置差异:")
print("   - posSide参数: 模拟盘需要，实盘必须移除")
print("   - szLimit最小值: 实盘≥0.2张，模拟盘≥0.1张")
print("   - 交易模式: 两个环境都支持逐仓模式")
print("   - 其他参数: 基本保持一致")

print("\n   💡 环境切换最佳实践:")
print("   - 模拟盘测试: 使用posSide参数验证逻辑")
print("   - 实盘部署: 移除posSide参数，调整最小数量")
print("   - 参数验证: 注意不同环境的限制条件")
print("   - 渐进测试: 小额→大额逐步验证")
