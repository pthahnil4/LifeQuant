#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 10 - 使用Backtrader计算MACD-EMA技术指标
=======================================

功能说明：
- 获取LTC-USD-241227指数K线数据
- 使用Backtrader计算MACD指标
- 基于MACD计算额外的MACD-EMA指标
- 输出计算结果

API文档：
https://www.okx.com/docs-v5/zh/#rest-api-market-data-get-candlesticks

作者：OKX API Demo
创建时间：2025-08-26
版本：v1.0
"""

import datetime
import pandas as pd
import backtrader as bt
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

#LTC-USD-241227
#LTC-USDT-241227
#LTC-USD-SWAP
#LTC-USDT-SWAP
# 获取指数K线数据
result = marketDataAPI.get_index_candlesticks(
    instId="LTC-USD-241227", bar='1H'
)
# 获取data部分
data = result['data']

# 将数据转换为DataFrame
df = pd.DataFrame(data, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
df['Date'] = pd.to_datetime(df['Date'].astype(int) / 1000, unit='s')
df = df.iloc[::-1]
df['Date'] = df['Date'] + pd.Timedelta(hours=8)  # 转换为东八区时间
df.set_index('Date', inplace=True)
df = df.astype(float)


# 创建一个自定义策略来计算指标
class MyStrategy(bt.Strategy):
    def __init__(self):
        self.macd = bt.indicators.MACD(self.data.close)
        self.ema = []  # 初始化EMA列表

    def next(self):
        macd_value = self.macd.macd[0]
        signal_value = self.macd.signal[0]
        histogram = 2 * (macd_value - signal_value)  # 乘以2
        if len(self.ema) == 0:
            new_ema = histogram  # 初始值
        else:
            new_ema = (self.ema[-1] * 8.0 / 10.0) + (histogram * 2.0 / 10.0)
        self.ema.append(new_ema)

        # 更新DataFrame
        df.loc[self.data.datetime.datetime(0), 'MACD'] = macd_value
        df.loc[self.data.datetime.datetime(0), 'Signal'] = signal_value
        df.loc[self.data.datetime.datetime(0), 'Histogram'] = histogram
        df.loc[self.data.datetime.datetime(0), 'EMA'] = new_ema
        df.loc[self.data.datetime.datetime(0), 'MACD-EMA'] = histogram - new_ema


# 创建Cerebro引擎
cerebro = bt.Cerebro()

# 将数据加载到Cerebro
data = bt.feeds.PandasData(dataname=df)
cerebro.adddata(data)

# 添加策略
cerebro.addstrategy(MyStrategy)

# 运行策略
cerebro.run()

# 输出结果
print(df[['Close', 'MACD', 'Signal', 'Histogram', 'EMA', 'MACD-EMA']].round(2).to_string())

