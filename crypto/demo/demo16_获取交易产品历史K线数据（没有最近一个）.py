#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 16 - 获取交易产品历史K线数据（没有最近一个）
========================================

功能说明：
- 获取指定产品的历史K线数据
- 支持多种时间周期（1m、5m、15m、30m、1H、4H、1D等）
- 数据分析和可视化展示
- 注意：历史K线数据不包含最新的未完成K线

时间周期说明：
- 1m, 3m, 5m, 15m, 30m: 分钟级
- 1H, 2H, 4H, 6H, 12H: 小时级
- 1D, 2D, 3D, 1W, 1M, 3M: 日周月级

API文档：
https://www.okx.com/docs-v5/en/#rest-api-market-data-get-candlesticks-history

作者：OKX API Demo
创建时间：2025-01-26
版本：v1.0
"""

import okx.MarketData as MarketData
import json
import datetime

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

print("=" * 80)
print("📈 OKX历史K线数据获取工具")
print("=" * 80)
print_config_info(config)
print("=" * 80)

# =============================================================================
# 查询参数配置
# =============================================================================

# 查询产品和周期配置
INST_ID = "LTC-USD-SWAP"               # 查询产品：LTC-USD永续合约
BAR_SIZE = "1H"                        # K线周期：1小时

print(f"🎯 查询配置:")
print(f"   产品ID: {INST_ID}")
print(f"   K线周期: {BAR_SIZE}")
print(f"   数据类型: 历史K线数据（不含最新未完成K线）")
print("=" * 80)

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
# 获取历史K线数据
# =============================================================================

try:
    print(f"\n🔍 正在获取 {INST_ID} 的 {BAR_SIZE} 历史K线数据...")
    
    # 调用get_history_candlesticks方法获取历史K线数据
    # 参数说明：
    # - instId: 产品ID，如BTC-USDT-SWAP
    # - bar: K线周期
    #   1m, 3m, 5m, 15m, 30m (分钟级)
    #   1H, 2H, 4H, 6H, 12H (小时级) 
    #   1D, 2D, 3D, 1W, 1M, 3M (日周月级)
    # - after: 请求此时间戳之前的分页内容
    # - before: 请求此时间戳之后的分页内容
    # - limit: 返回结果的数量，最大300，默认100
    result = marketDataAPI.get_history_candlesticks(
        instId=INST_ID,
        bar=BAR_SIZE
    )
    
    print("✅ 历史K线数据获取成功！")
    
    # 处理返回结果
    if result.get('code') == '0' and result.get('data'):
        candlesticks = result['data']
        
        print(f"\n📊 {INST_ID} - {BAR_SIZE} K线数据概览")
        print("=" * 80)
        print(f"📈 获取K线数量: {len(candlesticks)} 根")
        
        if candlesticks:
            # K线数据格式说明
            print(f"\n💡 K线数据格式说明:")
            print(f"   [时间戳, 开盘价, 最高价, 最低价, 收盘价, 成交量, 成交额, 确认状态]")
            print(f"   确认状态: 0=已确认, 1=未确认")
            
            # 显示K线数据统计
            print(f"\n📊 数据统计分析:")
            print("=" * 80)
            
            # 解析K线数据
            prices_open = []
            prices_high = []
            prices_low = []
            prices_close = []
            volumes = []
            timestamps = []
            
            for candle in candlesticks:
                try:
                    timestamp = int(candle[0])
                    open_price = float(candle[1])
                    high_price = float(candle[2])
                    low_price = float(candle[3])
                    close_price = float(candle[4])
                    volume = float(candle[5])
                    
                    timestamps.append(timestamp)
                    prices_open.append(open_price)
                    prices_high.append(high_price)
                    prices_low.append(low_price)
                    prices_close.append(close_price)
                    volumes.append(volume)
                except (ValueError, IndexError):
                    continue
            
            if prices_close:
                # 价格统计
                max_price = max(prices_high)
                min_price = min(prices_low)
                first_price = prices_open[0] if prices_open else 0
                last_price = prices_close[-1] if prices_close else 0
                
                print(f"💰 价格区间:")
                print(f"   最高价: ${max_price:.4f}")
                print(f"   最低价: ${min_price:.4f}")
                print(f"   期间开盘: ${first_price:.4f}")
                print(f"   期间收盘: ${last_price:.4f}")
                
                # 计算涨跌
                if first_price > 0 and last_price > 0:
                    price_change = last_price - first_price
                    price_change_pct = (price_change / first_price) * 100
                    change_emoji = "🟢" if price_change >= 0 else "🔴"
                    
                    print(f"📈 涨跌幅: {change_emoji} {price_change:+.4f} ({price_change_pct:+.2f}%)")
                
                # 成交量统计
                if volumes:
                    total_volume = sum(volumes)
                    avg_volume = total_volume / len(volumes)
                    max_volume = max(volumes)
                    
                    print(f"\n🔄 成交量统计:")
                    print(f"   总成交量: {total_volume:,.0f} 张")
                    print(f"   平均成交量: {avg_volume:,.0f} 张")
                    print(f"   最大成交量: {max_volume:,.0f} 张")
                
                # 时间范围
                if timestamps:
                    start_time = datetime.datetime.fromtimestamp(timestamps[0] / 1000)
                    end_time = datetime.datetime.fromtimestamp(timestamps[-1] / 1000)
                    
                    print(f"\n📅 时间范围:")
                    print(f"   开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"   结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"   数据跨度: {(timestamps[0] - timestamps[-1]) // (1000 * 3600 * 24)} 天")
            
            # 显示最新几根K线详情
            print(f"\n📋 最新 5 根K线详情:")
            print("=" * 80)
            print(f"{'时间':<20} {'开盘':<10} {'最高':<10} {'最低':<10} {'收盘':<10} {'成交量':<12} {'涨跌幅':<8}")
            print("-" * 80)
            
            for i, candle in enumerate(candlesticks[:5]):  # 显示最新5根K线
                try:
                    timestamp = int(candle[0])
                    open_price = float(candle[1])
                    high_price = float(candle[2])
                    low_price = float(candle[3])
                    close_price = float(candle[4])
                    volume = float(candle[5])
                    
                    # 时间格式化
                    time_str = datetime.datetime.fromtimestamp(timestamp / 1000).strftime('%m-%d %H:%M')
                    
                    # 计算涨跌幅
                    if open_price > 0:
                        change_pct = ((close_price - open_price) / open_price) * 100
                        change_str = f"{change_pct:+.2f}%"
                    else:
                        change_str = "0.00%"
                    
                    print(f"{time_str:<20} {open_price:<10.4f} {high_price:<10.4f} {low_price:<10.4f} {close_price:<10.4f} {volume:<12.0f} {change_str:<8}")
                    
                except (ValueError, IndexError):
                    print(f"数据解析失败: {candle}")
            
            if len(candlesticks) > 5:
                print(f"... 还有 {len(candlesticks) - 5} 根K线数据")
            
            # 技术分析指标建议
            print(f"\n💡 技术分析建议:")
            print("=" * 80)
            
            if len(candlesticks) >= 20:
                # 计算简单移动平均线
                recent_20_closes = [float(c[4]) for c in candlesticks[:20]]
                ma_20 = sum(recent_20_closes) / len(recent_20_closes)
                current_price = recent_20_closes[0]
                
                print(f"📊 技术指标:")
                print(f"   当前价格: ${current_price:.4f}")
                print(f"   20周期均线: ${ma_20:.4f}")
                
                if current_price > ma_20:
                    print(f"   趋势判断: 🟢 价格在均线上方，偏多头")
                else:
                    print(f"   趋势判断: 🔴 价格在均线下方，偏空头")
            
            print(f"\n🎯 分析建议:")
            print(f"   - 结合成交量变化判断趋势强度")
            print(f"   - 关注关键价格支撑阻力位")
            print(f"   - 可进一步获取更多周期数据进行对比")
        
    else:
        print(f"❌ 获取历史K线数据失败: {result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 获取历史K线数据时发生错误: {e}")

# =============================================================================
# 显示原始数据（调试用，仅显示前3条）
# =============================================================================

print(f"\n" + "=" * 80)
print("🔧 调试信息 - 原始返回数据示例")
print("=" * 80)

try:
    if result.get('data') and len(result['data']) > 0:
        sample_data = {
            'code': result.get('code'),
            'msg': result.get('msg'),
            'data': result['data'][:3]  # 只显示前3条数据
        }
        print(json.dumps(sample_data, indent=2, ensure_ascii=False))
        if len(result['data']) > 3:
            print(f"... 还有 {len(result['data']) - 3} 根K线数据未显示")
    else:
        print("无数据可显示")
except:
    print("原始数据显示失败")

# =============================================================================
# 使用建议
# =============================================================================

print(f"\n" + "=" * 80)
print("💡 使用建议")
print("=" * 80)

print(f"1️⃣  K线周期选择:")
print(f"   - 短线交易: 1m, 5m, 15m")
print(f"   - 中线交易: 1H, 4H, 1D")
print(f"   - 长线分析: 1D, 1W, 1M")

print(f"\n2️⃣  技术分析应用:")
print(f"   - 趋势判断: 观察K线形态和均线方向")
print(f"   - 支撑阻力: 识别关键价格位")
print(f"   - 成交量: 配合价格变化分析趋势强度")

print(f"\n3️⃣  数据获取优化:")
print(f"   - 使用limit参数控制获取数量")
print(f"   - 使用after/before参数获取特定时间段数据")
print(f"   - 定期更新数据保持分析准确性")

# =============================================================================
# 重要提醒
# =============================================================================

print(f"\n⚠️  重要提醒:")
print(f"   - 历史K线数据不包含最新未完成的K线")
print(f"   - 获取实时数据请使用当前K线接口")
print(f"   - 建议结合多个时间周期进行综合分析")
print(f"   - K线数据仅供参考，不构成投资建议")
print(f"   - 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo09_用talib计算技术指标.py 进行技术分析")
