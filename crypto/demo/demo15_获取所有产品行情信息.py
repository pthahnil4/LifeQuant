#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 15 - 获取所有产品行情信息
====================================

功能说明：
- 获取指定类型的所有产品行情信息
- 支持多种产品类型查询
- 筛选和显示特定产品信息

产品类型说明：
- SPOT: 币币交易
- MARGIN: 币币杠杆
- SWAP: 永续合约
- FUTURES: 交割合约
- OPTION: 期权

API文档：
https://www.okx.com/docs-v5/en/#rest-api-market-data-get-tickers

作者：OKX API Demo
创建时间：2025-01-26
版本：v1.0
"""

import okx.MarketData as MarketData
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
flag = config['flag']  # "0"=实盘, "1"=模拟盘

print("=" * 70)
print("📊 OKX产品行情信息查询工具")
print("=" * 70)
print_config_info(config)
print("=" * 70)

# =============================================================================
# 查询参数配置
# =============================================================================

# 查询产品类型配置
INST_TYPE = "FUTURES"                  # 查询产品类型：交割合约
FILTER_SYMBOL = "LTC"                  # 筛选包含此符号的产品

print(f"🎯 查询配置:")
print(f"   产品类型: {INST_TYPE}")
print(f"   筛选条件: 包含 '{FILTER_SYMBOL}' 的产品")
print("=" * 70)

# =============================================================================
# 初始化市场数据API
# =============================================================================

try:
    # 创建市场数据API实例
    marketDataAPI = MarketData.MarketAPI(flag=flag)
    print("✅ 市场数据API初始化成功")
    
except Exception as e:
    print(f"❌ 市场数据API初始化失败: {e}")
    exit(1)

# =============================================================================
# 获取所有产品行情信息
# =============================================================================

try:
    print(f"\n🔍 正在获取 {INST_TYPE} 产品行情信息...")
    
    # 调用get_tickers方法获取行情数据
    # 参数说明：
    # - instType: 产品类型
    #   SPOT: 币币交易
    #   MARGIN: 币币杠杆
    #   SWAP: 永续合约
    #   FUTURES: 交割合约
    #   OPTION: 期权
    result = marketDataAPI.get_tickers(
        instType=INST_TYPE
    )
    
    print("✅ 行情数据获取成功！")
    
    # 处理返回结果
    if result.get('code') == '0' and result.get('data'):
        all_tickers = result['data']
        
        print(f"\n📈 {INST_TYPE} 产品行情概览")
        print("=" * 70)
        print(f"📊 总产品数量: {len(all_tickers)} 个")
        
        # 统计不同基础资产的数量
        base_assets = {}
        ltc_products = []
        
        for ticker in all_tickers:
            inst_id = ticker.get('instId', '')
            
            # 提取基础资产（如BTC-USDT-SWAP中的BTC）
            if '-' in inst_id:
                base_asset = inst_id.split('-')[0]
                base_assets[base_asset] = base_assets.get(base_asset, 0) + 1
            
            # 筛选包含特定符号的产品
            if FILTER_SYMBOL in inst_id:
                ltc_products.append(ticker)
        
        print(f"\n🏆 热门基础资产统计 (Top 10):")
        sorted_assets = sorted(base_assets.items(), key=lambda x: x[1], reverse=True)[:10]
        for i, (asset, count) in enumerate(sorted_assets, 1):
            print(f"   {i:2d}. {asset}: {count} 个产品")
        
        # 显示筛选后的产品详情
        if ltc_products:
            print(f"\n🎯 包含 '{FILTER_SYMBOL}' 的产品详情")
            print("=" * 70)
            print(f"{'序号':<4} {'产品ID':<20} {'最新价格':<12} {'24H涨跌':<10} {'24H成交量':<15} {'更新时间':<20}")
            print("-" * 70)
            
            for i, product in enumerate(ltc_products, 1):
                inst_id = product.get('instId', '')
                last_price = product.get('last', '0')
                price_change = product.get('sodUtc8', '0')  # 24小时涨跌幅
                volume_24h = product.get('vol24h', '0')     # 24小时成交量
                timestamp = product.get('ts', '0')
                
                # 时间戳转换
                try:
                    import datetime
                    update_time = datetime.datetime.fromtimestamp(int(timestamp) / 1000)
                    update_time_str = update_time.strftime('%m-%d %H:%M:%S')
                except:
                    update_time_str = "N/A"
                
                # 格式化显示
                try:
                    last_price_float = float(last_price)
                    price_change_float = float(price_change)
                    volume_24h_float = float(volume_24h)
                    
                    last_price_str = f"${last_price_float:.4f}"
                    
                    # 涨跌幅颜色标识
                    if price_change_float > 0:
                        change_str = f"🟢+{price_change_float:.2f}%"
                    elif price_change_float < 0:
                        change_str = f"🔴{price_change_float:.2f}%"
                    else:
                        change_str = f"⚪{price_change_float:.2f}%"
                    
                    volume_str = f"{volume_24h_float:,.0f}"
                    
                except ValueError:
                    last_price_str = last_price
                    change_str = price_change
                    volume_str = volume_24h
                
                print(f"{i:<4} {inst_id:<20} {last_price_str:<12} {change_str:<10} {volume_str:<15} {update_time_str:<20}")
        
        else:
            print(f"\n⚠️  未找到包含 '{FILTER_SYMBOL}' 的产品")
        
        # 显示所有产品ID（简化版）
        print(f"\n📋 所有 {INST_TYPE} 产品列表:")
        print("-" * 70)
        
        products_per_line = 4
        for i in range(0, len(all_tickers), products_per_line):
            line_products = []
            for j in range(products_per_line):
                if i + j < len(all_tickers):
                    inst_id = all_tickers[i + j].get('instId', '')
                    line_products.append(f"{inst_id:<18}")
            print("   " + "".join(line_products))
        
        # 市场概览统计
        print(f"\n📊 市场概览统计:")
        print("-" * 70)
        
        # 计算价格统计
        prices = []
        changes = []
        volumes = []
        
        for ticker in all_tickers:
            try:
                price = float(ticker.get('last', '0'))
                change = float(ticker.get('sodUtc8', '0'))
                volume = float(ticker.get('vol24h', '0'))
                
                if price > 0:
                    prices.append(price)
                if change != 0:
                    changes.append(change)
                if volume > 0:
                    volumes.append(volume)
            except ValueError:
                continue
        
        if prices:
            print(f"💰 价格范围: ${min(prices):.4f} - ${max(prices):.4f}")
        
        if changes:
            up_count = len([c for c in changes if c > 0])
            down_count = len([c for c in changes if c < 0])
            print(f"📈 涨跌统计: 🟢{up_count}个上涨 🔴{down_count}个下跌")
            print(f"📊 涨跌范围: {min(changes):.2f}% - {max(changes):.2f}%")
        
        if volumes:
            total_volume = sum(volumes)
            print(f"🔄 总成交量: {total_volume:,.0f}")
            print(f"📊 成交量范围: {min(volumes):,.0f} - {max(volumes):,.0f}")
        
    else:
        print(f"❌ 获取行情数据失败: {result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 获取行情数据时发生错误: {e}")

# =============================================================================
# 显示原始数据（调试用，仅显示前3条）
# =============================================================================

print(f"\n" + "=" * 70)
print("🔧 调试信息 - 原始返回数据示例")
print("=" * 70)

try:
    if result.get('data') and len(result['data']) > 0:
        sample_data = {
            'code': result.get('code'),
            'msg': result.get('msg'),
            'data': result['data'][:3]  # 只显示前3条数据
        }
        print(json.dumps(sample_data, indent=2, ensure_ascii=False))
        if len(result['data']) > 3:
            print(f"... 还有 {len(result['data']) - 3} 条数据未显示")
    else:
        print("无数据可显示")
except:
    print("原始数据显示失败")

# =============================================================================
# 使用建议
# =============================================================================

print(f"\n" + "=" * 70)
print("💡 使用建议")
print("=" * 70)

print(f"1️⃣  产品类型选择:")
print(f"   - SPOT: 现货交易，风险较低")
print(f"   - SWAP: 永续合约，适合短期交易")
print(f"   - FUTURES: 交割合约，有到期日")
print(f"   - OPTION: 期权，高级交易工具")

print(f"\n2️⃣  行情分析建议:")
print(f"   - 关注24小时涨跌幅，识别趋势")
print(f"   - 观察成交量，判断市场活跃度")
print(f"   - 比较不同产品价格走势")

print(f"\n3️⃣  风险管理:")
print(f"   - 选择流动性好的产品进行交易")
print(f"   - 关注市场整体趋势")
print(f"   - 避免交易冷门或低成交量产品")

# =============================================================================
# 风险提醒
# =============================================================================

print(f"\n⚠️  风险提醒:")
print(f"   - 行情数据存在延迟，请以实际交易为准")
print(f"   - 合约产品存在杠杆风险，请谨慎操作")
print(f"   - 24小时涨跌幅仅供参考，不代表未来走势")
print(f"   - 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo15获取交易产品历史K线数据.py 查看K线数据")