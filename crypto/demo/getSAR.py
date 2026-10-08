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
print("📊 OKX SAR抛物线转向指标计算工具")
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

    # 计算SAR指标
    def calculate_sar(df, af=0.02, max_af=0.2):
        high = df['High']
        low = df['Low']
        close = df['Close']
        sar = close.copy()
        trend = 1  # 1 for uptrend, -1 for downtrend
        ep = high.iloc[0]  # extreme point
        af = af  # acceleration factor

        for i in range(1, len(close)):
            sar.iloc[i] = sar.iloc[i-1] + af * (ep - sar.iloc[i-1])
            if trend == 1:
                if low.iloc[i] < sar.iloc[i]:
                    trend = -1
                    sar.iloc[i] = ep
                    ep = low.iloc[i]
                    af = 0.02
                else:
                    if high.iloc[i] > ep:
                        ep = high.iloc[i]
                        af = min(af + 0.02, max_af)
            else:
                if high.iloc[i] > sar.iloc[i]:
                    trend = 1
                    sar.iloc[i] = ep
                    ep = high.iloc[i]
                    af = 0.02
                else:
                    if low.iloc[i] < ep:
                        ep = low.iloc[i]
                        af = min(af + 0.02, max_af)

        return sar

    df['SAR'] = calculate_sar(df)

    # 生成买入和卖出信号
    df['Signal'] = 0
    df.loc[df['Close'] > df['SAR'], 'Signal'] = 1  # 买入信号
    df.loc[df['Close'] < df['SAR'], 'Signal'] = -1  # 卖出信号

    # 确保买入和卖出信号交替出现
    signals = []
    last_signal = 0

    for index, row in df.iterrows():
        if row['Signal'] != 0 and row['Signal'] != last_signal:
            signals.append((index, row['Signal'], row['Close']))
            last_signal = row['Signal']

    # 打印买入点和卖出点及其时间和价格
    print("买入和卖出点：")
    for signal in signals:
        action = "买入" if signal[1] == 1 else "卖出"
        print(f"{action}点: 时间={signal[0]}, 价格={signal[2]}, 信号={signal[1]}")

def main():
   get_latest_data("LTC-USD", "1H")

if __name__ == "__main__":
    main()
