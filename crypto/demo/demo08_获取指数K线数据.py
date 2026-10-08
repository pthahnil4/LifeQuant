#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 08 - 获取指数K线数据

============================

功能说明：
- 获取指定交易对的指数K线数据
- 支持多种时间周期（1m/3m/5m/15m/30m/1H/2H/4H）
- 美化数据输出展示

适用场景：
- 技术分析和策略研究
- 历史数据回测
- 市场趋势分析

API文档：
https://www.okx.com/docs-v5/en/#rest-api-market-data-get-candlesticks

作者：OKX API Demo
创建时间：2024-01-19
版本：v1.0
"""

import okx.MarketData as MarketData
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
flag = config['flag']  # "0"=实盘, "1"=模拟盘

print("=" * 60)
print("📊 OKX指数K线数据获取工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# K线参数配置
# =============================================================================

# K线配置
INST_ID = "LTC-USDT"              # 交易对
BAR = "1H"                       # K线周期（1m/3m/5m/15m/30m/1H/2H/4H）
LIMIT = "100"                    # 获取记录条数（最大值为100）

print(f"🎯 数据配置:")
print(f"   交易对: {INST_ID}")
print(f"   K线周期: {BAR}")
print(f"   记录条数: {LIMIT}条")
print("=" * 60)

# =============================================================================
# 初始化行情API
# =============================================================================

try:
    # 创建行情API实例
    marketDataAPI = MarketData.MarketAPI(flag=flag)
    print("✅ 行情API初始化成功")
    
except Exception as e:
    print(f"❌ 行情API初始化失败: {e}")
    exit(1)

# =============================================================================
# 获取K线数据
# =============================================================================

try:
    print(f"\n📊 正在获取K线数据...")
    print(f"   获取 {INST_ID} 的 {BAR} K线数据...")
    
    # 调用get_index_candlesticks方法获取K线数据
    result = marketDataAPI.get_index_candlesticks(
        instId=INST_ID,
        bar=BAR,
        limit=LIMIT
    )
    
    print("✅ 数据获取成功！")
    
except Exception as e:
    print(f"❌ 获取K线数据时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析K线数据
# =============================================================================

try:
    # 显示原始返回数据（调试用）
    print(f"\n📋 API返回数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 提取data部分
    if result['code'] == '0' and result['data']:
        data = result['data']
        total_records = len(data)
        
        print("\n" + "=" * 60)
        print("📊 K线数据")
        print("=" * 60)
        
        print(f"📈 获取到 {total_records} 条K线记录")
        print(f"\n时间戳\t\t\t开盘价\t\t最高价\t\t最低价\t\t收盘价\t\t交易量")
        print("-" * 100)
        
        # 遍历并格式化输出每条记录
        for record in data:
            # 解析数据字段
            timestamp = int(record[0])
            date = datetime.datetime.fromtimestamp(
                timestamp / 1000
            ).strftime('%Y-%m-%d %H:%M:%S')
            
            open_price = record[1]
            high_price = record[2]
            low_price = record[3]
            close_price = record[4]
            volume = record[5]
            
            # 格式化输出
            print(f"{date}\t{open_price}\t{high_price}\t{low_price}\t{close_price}\t{volume}")
            
        # 计算价格统计
        latest_price = float(data[0][4])  # 最新价格
        highest_price = max([float(x[2]) for x in data])  # 区间最高价
        lowest_price = min([float(x[3]) for x in data])   # 区间最低价
        price_range = highest_price - lowest_price         # 价格区间
        
        print("\n" + "=" * 60)
        print("📊 数据统计")
        print("=" * 60)
        
        print(f"📈 价格统计:")
        print(f"   最新价: ${latest_price}")
        print(f"   最高价: ${highest_price}")
        print(f"   最低价: ${lowest_price}")
        print(f"   价格区间: ${price_range:.2f}")
        
    else:
        print(f"\n❌ 数据获取失败!")
        print(f"   错误代码: {result['code']}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

    # =============================================================================
    # 后续操作建议
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("🔍 后续操作建议")
    print("=" * 60)
    
    if result['code'] == '0':
        print(f"1️⃣  数据分析:")
        print(f"   运行技术指标计算脚本分析数据趋势")
        print(f"   - demo10_RSI技术指标.py")
        print(f"   - getBOLL.py")
        print(f"   - getEMA.py")
        
        print(f"\n2️⃣  历史数据:")
        print(f"   如需更多历史数据，可以:")
        print(f"   - 修改limit参数（最大100条）")
        print(f"   - 使用after参数获取更早的数据")
        
        print(f"\n3️⃣  实时行情:")
        print(f"   获取最新市场行情，运行:")
        print(f"   - demo14获取所有产品行情信息.py")

    # =============================================================================
    # 注意事项
    # =============================================================================
    
    print(f"\n⚠️  注意事项:")
    print(f"   - K线数据有一定延迟")
    print(f"   - 建议结合多个指标进行分析")
    print(f"   - 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")

except Exception as e:
    print(f"❌ 解析K线数据时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行技术指标分析脚本")