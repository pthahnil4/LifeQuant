#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 09 - 获取指数K线数据并计算技术指标
=======================================

功能说明：
- 获取BTC-USDT指数K线数据
- 计算并展示主要技术指标：
  * MACD (趋势)
  * 布林带 (波动)
  * SAR (趋势反转)
  * RSI (超买超卖)
- 提供技术分析和交易建议

API文档：
https://www.okx.com/docs-v5/zh/#rest-api-market-data-get-candlesticks

作者：OKX API Demo
创建时间：2025-08-26
版本：v1.0
"""

import datetime
import json
import okx.MarketData as MarketData
import pandas as pd
import numpy as np
import talib

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
print("📊 OKX技术指标分析工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 参数配置
# =============================================================================

# 交易产品配置
INST_ID = "BTC-USDT"              # 交易对：BTC-USDT
BAR = "1H"                       # K线周期：1小时
LIMIT = "100"                    # 获取记录数：最近100条

print(f"🎯 数据配置:")
print(f"   交易对: {INST_ID}")
print(f"   K线周期: {BAR}")
print(f"   记录数量: {LIMIT}条")
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

print(f"\n📊 正在获取{INST_ID}指数K线数据...")
print(f"   周期: {BAR}, 数量: {LIMIT}条")

try:
    # 获取K线数据
    result = marketDataAPI.get_index_candlesticks(
        instId=INST_ID,
        bar=BAR,
        limit=LIMIT
    )
    
    # 检查API返回结果
    if not isinstance(result, dict):
        raise ValueError(f"API返回格式错误: {result}")
        
    if 'code' not in result or result['code'] != '0':
        error_code = result.get('code', 'unknown')
        error_msg = result.get('msg', '未知错误')
        raise ValueError(f"API返回错误 {error_code}: {error_msg}")
        
    if 'data' not in result or not result['data']:
        raise ValueError("API返回数据为空")
        
    # 获取数据
    data = result['data']
    print(f"✅ 成功获取 {len(data)} 条K线数据")
    
except Exception as e:
    print(f"\n❌ 获取K线数据失败: {str(e)}")
    print("💡 可能的原因:")
    print("   1. 网络连接问题")
    print("   2. API参数错误")
    print("   3. 服务器响应超时")
    exit(1)

# =============================================================================
# 数据预处理
# =============================================================================

print("\n📊 正在处理K线数据...")

try:
    # 创建DataFrame
    df = pd.DataFrame(
        data,
        columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
    )
    
    # 转换时间戳
    df['timestamp'] = pd.to_numeric(df['timestamp'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    
    # 转换数值类型
    numeric_columns = ['open', 'high', 'low', 'close', 'volume']
    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col], errors='coerce')
        
    # 检查数据质量
    if df.isnull().any().any():
        print("⚠️ 警告: 数据中存在空值，已自动处理")
        df = df.fillna(method='ffill')  # 使用前值填充
        
    print("✅ 数据预处理完成")
    
    # 显示数据概览
    print("\n📊 数据统计:")
    print(f"   时间范围: {df.index.min()} 至 {df.index.max()}")
    print(f"   最新价格: ${df['close'].iloc[-1]:.2f}")
    print(f"   最高价格: ${df['high'].max():.2f}")
    print(f"   最低价格: ${df['low'].min():.2f}")
    print(f"   价格区间: ${df['high'].max() - df['low'].min():.2f}")
    
except Exception as e:
    print(f"\n❌ 数据处理失败: {str(e)}")
    print("💡 请检查数据格式是否正确")
    exit(1)

# =============================================================================
# 计算技术指标
# =============================================================================

print("\n" + "=" * 60)
print("📊 计算技术指标")
print("=" * 60)

try:
    # 设置pandas显示选项
    pd.set_option('display.max_rows', 10)
    pd.set_option('display.float_format', lambda x: '%.2f' % x)
    pd.set_option('display.width', None)
    pd.set_option('display.max_columns', None)
    
    # 创建技术指标DataFrame
    tech_df = pd.DataFrame(index=df.index)
    
    print("\n🔄 正在计算技术指标...")
    
    # 1. MACD指标
    print("   计算MACD (趋势指标)...")
    tech_df['MACD'], tech_df['MACD信号'], tech_df['MACD柱状'] = talib.MACD(
        df['close'],
        fastperiod=12,    # 快线周期
        slowperiod=26,    # 慢线周期
        signalperiod=9    # 信号线周期
    )
    
    # 2. 布林带指标
    print("   计算布林带 (波动指标)...")
    tech_df['布林上轨'], tech_df['布林中轨'], tech_df['布林下轨'] = talib.BBANDS(
        df['close'],
        timeperiod=20,    # 计算周期
        nbdevup=2,        # 上轨标准差倍数
        nbdevdn=2,        # 下轨标准差倍数
        matype=0          # 移动平均类型 (SMA)
    )
    
    # 3. SAR指标
    print("   计算SAR (趋势反转指标)...")
    tech_df['SAR'] = talib.SAR(
        df['high'],
        df['low'],
        acceleration=0.02,  # 加速因子
        maximum=0.2        # 最大加速值
    )
    
    # 4. RSI指标
    print("   计算RSI (超买超卖指标)...")
    tech_df['RSI'] = talib.RSI(
        df['close'],
        timeperiod=14      # 计算周期
    )
    
    print("✅ 技术指标计算完成")
    
    # 获取最新数据进行分析
    latest = tech_df.iloc[-1]
    current_price = float(df['close'].iloc[-1])
    
    print("\n" + "=" * 60)
    print("📊 技术分析结果")
    print("=" * 60)
    
    # 1. MACD分析
    macd_diff = latest['MACD'] - latest['MACD信号']
    macd_signal = "看涨 🚀" if macd_diff > 0 else "看跌 📉"
    print(f"\n1️⃣ MACD分析:")
    print(f"   信号: {macd_signal}")
    print(f"   • MACD值: {latest['MACD']:.2f}")
    print(f"   • 信号线: {latest['MACD信号']:.2f}")
    print(f"   • 差值: {macd_diff:.2f}")
    
    # 2. RSI分析
    rsi = latest['RSI']
    rsi_signal = "超卖 ⬇️" if rsi < 30 else "超买 ⬆️" if rsi > 70 else "中性 ↔️"
    print(f"\n2️⃣ RSI分析:")
    print(f"   状态: {rsi_signal}")
    print(f"   • RSI值: {rsi:.2f}")
    print(f"   • 超买区间: > 70")
    print(f"   • 超卖区间: < 30")
    
    # 3. 布林带分析
    bb_width = latest['布林上轨'] - latest['布林下轨']
    bb_position = "上轨 ⚠️" if current_price >= latest['布林上轨'] else "下轨 💡" if current_price <= latest['布林下轨'] else "中轨 ✅"
    
    print(f"\n3️⃣ 布林带分析:")
    print(f"   位置: {bb_position}")
    print(f"   • 当前价格: ${current_price:.2f}")
    print(f"   • 上轨: ${latest['布林上轨']:.2f}")
    print(f"   • 中轨: ${latest['布林中轨']:.2f}")
    print(f"   • 下轨: ${latest['布林下轨']:.2f}")
    print(f"   • 带宽: ${bb_width:.2f}")
    
    # 4. SAR分析
    sar_position = "做多 📈" if current_price > latest['SAR'] else "做空 📉"
    print(f"\n4️⃣ SAR分析:")
    print(f"   信号: {sar_position}")
    print(f"   • 当前价格: ${current_price:.2f}")
    print(f"   • SAR价格: ${latest['SAR']:.2f}")
    
    # 综合分析
    print("\n" + "=" * 60)
    print("📈 交易建议")
    print("=" * 60)
    
    signals = []
    if macd_diff > 0:
        signals.append("MACD金叉")
    if 30 <= rsi <= 70:
        signals.append("RSI中性")
    if latest['布林下轨'] <= current_price <= latest['布林上轨']:
        signals.append("布林带中轨")
    if current_price > latest['SAR']:
        signals.append("SAR看多")
    
    print(f"\n当前信号: {', '.join(signals)}")
    
    # 风险提示
    print("\n⚠️ 风险提示:")
    print("   1. 以上分析仅供参考，请勿作为唯一决策依据")
    print("   2. 建议结合基本面、资金管理等多因素")
    print("   3. 市场瞬息万变，及时止损同样重要")
    print(f"   4. 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")
    
except Exception as e:
    print(f"\n❌ 计算技术指标时发生错误: {str(e)}")
    print("💡 可能的原因:")
    print("   1. 数据量不足")
    print("   2. 数据格式错误")
    print("   3. 计算参数无效")
    exit(1)