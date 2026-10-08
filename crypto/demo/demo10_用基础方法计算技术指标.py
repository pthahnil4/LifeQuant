#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 10 - 获取指数K线数据并计算自定义指标
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
import pandas as pd
import numpy as np
from okx import MarketData

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

# 创建行情API实例
marketDataAPI = MarketData.MarketAPI(flag=flag)

# 获取指数K线数据
result = marketDataAPI.get_index_candlesticks(
    instId="BTC-USD",bar='1H'
)

# 获取data部分
data = result['data']

# 输出一共多少条记录
print(f"一共 {len(data)} 条记录")

# 遍历result，逐行输出记录，并把其中的时间戳改成日期格式输出
# for record in data:
#     timestamp = int(record[0])
#     date = datetime.datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')
#     print([date] + record[1:])

# 将数据转换为DataFrame
df = pd.DataFrame(data, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
# print(df['Date'])
# df['Date'] = pd.to_datetime(df['Date']/1000, format='%Y-%m-%d %H:%M:%S')
# print(df['Date'])
df.set_index('Date', inplace=True)
df = df.astype(float)

# 计算MACD
def calculate_macd(df, short_window=12, long_window=26, signal_window=9):
    df['EMA12'] = df['Close'].ewm(span=short_window, adjust=False).mean()
    df['EMA26'] = df['Close'].ewm(span=long_window, adjust=False).mean()
    df['MACD'] = df['EMA12'] - df['EMA26']
    df['Signal'] = df['MACD'].ewm(span=signal_window, adjust=False).mean()
    return df

# 计算BOLL
def calculate_bollinger_bands(df, window=20, num_std_dev=2):
    df['MA20'] = df['Close'].rolling(window=window).mean()
    df['STD20'] = df['Close'].rolling(window=window).std()
    df['UpperBand'] = df['MA20'] + (df['STD20'] * num_std_dev)
    df['LowerBand'] = df['MA20'] - (df['STD20'] * num_std_dev)
    return df

# 计算SAR
def calculate_sar(df, af=0.02, max_af=0.2):
    df['SAR'] = df['Close'].shift(1)  # 初始SAR值
    df['EP'] = df['High']  # 极值点
    df['AF'] = af  # 加速因子
    for i in range(1, len(df)):
        if df['Close'][i] > df['SAR'][i-1]:
            df['SAR'][i] = df['SAR'][i-1] + df['AF'][i-1] * (df['EP'][i-1] - df['SAR'][i-1])
            if df['High'][i] > df['EP'][i-1]:
                df['EP'][i] = df['High'][i]
                df['AF'][i] = min(df['AF'][i-1] + af, max_af)
        else:
            df['SAR'][i] = df['SAR'][i-1] - df['AF'][i-1] * (df['SAR'][i-1] - df['EP'][i-1])
            if df['Low'][i] < df['EP'][i-1]:
                df['EP'][i] = df['Low'][i]
                df['AF'][i] = min(df['AF'][i-1] + af, max_af)
    return df

# 计算RSI
def calculate_rsi(df, window=14):
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

# 计算所有指标
df = calculate_macd(df)
df = calculate_bollinger_bands(df)
df = calculate_sar(df)
df = calculate_rsi(df)

# 输出结果
print(df[['MACD', 'Signal', 'UpperBand', 'LowerBand', 'SAR', 'RSI']])