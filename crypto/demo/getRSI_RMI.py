import datetime
import pandas as pd
import backtrader as bt
from okx import MarketData

# API 初始化
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

print("=" * 80)
print("📊 OKX RSI/RMI相对强弱指标计算工具")
print("=" * 80)
print_config_info(config)
print("=" * 80)

marketDataAPI = MarketData.MarketAPI(flag=flag)  # 初始化市场数据API

def get_latest_data(instId, bar):
    # 获取指数K线数据
    result = marketDataAPI.get_index_candlesticks(instId=instId, bar=bar)
    # 获取data部分
    data = result['data']

    print(data)

    # 将数据转换为DataFrame
    df = pd.DataFrame(data, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['Date'] = pd.to_datetime(df['Date'].astype(int) / 1000, unit='s')
    df = df.iloc[::-1]
    df['Date'] = df['Date'] + pd.Timedelta(hours=8)  # 转换为东八区时间
    df.set_index('Date', inplace=True)
    df = df.astype(float)

    # 计算RSI指标
    def calculate_rsi(df, period=14):
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    df['RSI'] = calculate_rsi(df)

    # 计算RMI指标
    def calculate_rmi(df, period=14):
        delta = df['Close'].diff(periods=period)
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rmi = 100 - (100 / (1 + rs))
        return rmi

    df['RMI'] = calculate_rmi(df)

    # 输出每个时段的RSI、RMI和价格，RSI和RMI值保留两位小数
    print("每个时段的RSI、RMI和价格：")
    for index, row in df.iterrows():
        print(f"时间={index}, RSI={row['RSI']:.2f}, RMI={row['RMI']:.2f}, 价格={row['Close']}")

def main():
   get_latest_data("LTC-USD", "1D")

if __name__ == "__main__":
    main()
