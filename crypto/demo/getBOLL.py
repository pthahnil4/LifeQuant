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
print("📊 OKX BOLL布林带技术指标计算工具")
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
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = df.iloc[::-1]

    # 将字符串转换为浮点数
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)

    # 计算20日移动平均线
    df['MA20'] = df['close'].rolling(window=20).mean()

    # 计算20日标准差
    df['STD20'] = df['close'].rolling(window=20).std()

    # 计算布林带的上轨和下轨
    df['Upper'] = df['MA20'] + (df['STD20'] * 2)
    df['Lower'] = df['MA20'] - (df['STD20'] * 2)

    # 计算布林带上下轨的斜率
    df['UpperSlope'] = df['Upper'].diff()
    df['LowerSlope'] = df['Lower'].diff()

    # 计算斜率绝对值之和
    df['SlopeAbsSum'] = df['UpperSlope'].abs() + df['LowerSlope'].abs()

    # 检查是否碰到上轨或下轨
    def check_touch(row, last_touch):
        if row['high'] >= row['Upper'] and last_touch != '上轨':
            return '碰到上轨'
        elif row['low'] <= row['Lower'] and last_touch != '下轨':
            return '碰到下轨'
        else:
            return '未碰到'

    last_touch = None
    touch_records = []

    for index, row in df.iterrows():
        touch = check_touch(row, last_touch)
        if touch != '未碰到':
            row['Touch'] = touch
            touch_records.append(row)
            last_touch = '上轨' if touch == '碰到上轨' else '下轨'

    touch_df = pd.DataFrame(touch_records)
    print("第一次触碰上轨或下轨的记录：")
    print(touch_df[['timestamp', 'close', 'Upper', 'Lower', 'Touch']])

    print("\n布林带上下轨的斜率：")
    print(df[['timestamp','close', 'UpperSlope', 'LowerSlope', 'SlopeAbsSum']].to_string())

def main():
   get_latest_data("LTC-USD", "m")

if __name__ == "__main__":
    main()
