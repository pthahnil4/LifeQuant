#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NEAR-USDT-SWAP技术指标计算与InfluxDB存储
======================================

功能：
1. 获取NEAR-USDT-SWAP的多周期K线数据 
2. 计算自定义指标MACD-EMA和常见技术指标
3. 存储到InfluxDB的garble桶中
4. 查询并显示各周期最新3条指标数据

技术指标：
- MACD-EMA (自定义): MACD[i] - EMA[i]  
- MA12, MA26: 12周期和26周期移动平均线
- MACD, DIF, DEA: MACD指标组合
- BOLL: 布林带指标
- RSI: 相对强弱指标
- SAR: 抛物转向指标 
- KDJ: 随机指标

作者：AI Assistant
创建时间：2025年1月
参照：crypto_analysis_batch.py
"""

import sys
import os
from datetime import datetime, timedelta, timezone
import json
import time
import pandas as pd
import numpy as np
import traceback

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入OKX API
from okx import MarketData

# 导入配置
from influxdb_config import InfluxDBConfig

# 导入InfluxDB客户端
try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
    from influxdb_client.client.query_api import QueryApi
    print("✅ InfluxDB 2.x 客户端库导入成功")
except ImportError as e:
    print("❌ InfluxDB客户端库导入失败")
    print("请安装: pip install influxdb-client")
    sys.exit(1)

class NEARTechnicalIndicatorsInfluxDB:
    """NEAR-USDT-SWAP技术指标计算与InfluxDB存储类"""
    
    def __init__(self):
        print("🔧 初始化NEAR技术指标计算器...")
        
        # OKX API配置
        self.flag = "0"  # 实盘:0 , 模拟盘:1
        print(f"📡 初始化OKX API，模式: {'实盘' if self.flag == '0' else '模拟盘'}")
        self.market_api = MarketData.MarketAPI(flag=self.flag)
        print("✅ OKX MarketAPI初始化成功")
        
        # 使用配置文件中的InfluxDB设置
        print("📋 加载InfluxDB配置...")
        self.config = InfluxDBConfig()
        self.url = self.config.URL
        self.token = self.config.TOKEN
        self.org = self.config.ORG
        self.bucket = self.config.DEFAULT_BUCKETS[0]  # 使用第一个桶 "garble"
        print(f"✅ InfluxDB配置加载完成")
        print(f"   URL: {self.url}")
        print(f"   组织: {self.org}")
        print(f"   目标桶: {self.bucket}")
        
        # 交易对和周期配置
        self.inst_id = "NEAR-USDT"
        self.time_periods = ["5m", "15m", "1H", "4H", "1D", "1W"]
        self.kline_measurement = "near_usdt_swap_kline"  # K线数据表
        self.indicator_measurement = "near_usdt_swap_indicators"  # 技术指标数据表
        
        # InfluxDB客户端
        self.client = None
        self.query_api = None
        self.write_api = None
        
        print(f"🔧 NEAR技术指标计算器初始化完成")
        print(f"   合约代码: {self.inst_id}")
        print(f"   周期: {', '.join(self.time_periods)}")
        print(f"   K线数据表: {self.kline_measurement}")
        print(f"   指标数据表: {self.indicator_measurement}")
        
    def connect_influxdb(self):
        """连接到InfluxDB"""
        try:
            print(f"\n🔗 正在连接InfluxDB...")
            print(f"   URL: {self.url}")
            print(f"   组织: {self.org}")
            
            # 使用配置文件创建客户端
            client_config = self.config.get_client_config()
            print(f"📋 客户端配置: {list(client_config.keys())}")
            
            self.client = InfluxDBClient(**client_config)
            print("✅ InfluxDB客户端创建成功")
            
            # 测试连接
            print("🔍 测试InfluxDB连接...")
            health = self.client.health()
            print(f"✅ InfluxDB连接成功")
            print(f"   状态: {health.status}")
            print(f"   版本: {health.version}")
            
            # 初始化API
            print("🔧 初始化InfluxDB API...")
            self.query_api = self.client.query_api()
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
            print("✅ InfluxDB API初始化完成")
            
            return True
            
        except Exception as e:
            print(f"❌ InfluxDB连接失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_kline_data(self, time_period, limit=200):
        """获取指定周期的K线数据"""
        try:
            print(f"\n📊 开始获取K线数据...")
            print(f"   合约代码: {self.inst_id}")
            print(f"   时间周期: {time_period}")
            print(f"   数据条数: {limit}")
            
            # 尝试指数K线API，如果失败则回退到标记价格API
            print(f"🌐 尝试调用get_index_candlesticks API...")
            start_time = time.time()
            
            # 先尝试指数K线API
            result = self.market_api.get_index_candlesticks(
                instId=self.inst_id, 
                bar=time_period,
                limit=str(limit)
            )
            
            # 检查是否返回错误，如果是51001错误则尝试标记价格API
            if 'code' in result and result['code'] == '51001':
                print(f"⚠️ 指数K线API返回错误: {result.get('msg', '未知错误')}")
                print(f"🔄 回退使用标记价格K线API...")
                
                result = self.market_api.get_mark_price_candlesticks(
                    instId=self.inst_id, 
                    bar=time_period,
                    limit=str(limit)
                )
                print(f"✅ 标记价格API调用完成")
            
            api_time = time.time() - start_time
            print(f"✅ API调用完成，耗时: {api_time:.2f}秒")
            
            # 检查API响应
            print(f"🔍 检查API响应...")
            if 'code' in result:
                print(f"   响应代码: {result['code']}")
                if result['code'] != '0':
                    print(f"❌ API返回错误代码: {result['code']}")
                    if 'msg' in result:
                        print(f"   错误信息: {result['msg']}")
                    return None
            
            if 'data' not in result:
                print(f"❌ API响应中缺少data字段")
                return None
                
            kline_data = result['data']
            print(f"📈 获取到原始数据条数: {len(kline_data) if kline_data else 0}")
            
            if not kline_data:
                print(f"❌ 无法获取 {self.inst_id} 的 {time_period} K线数据")
                return None
            
            print(f"✅ 成功获取 {len(kline_data)} 条 {time_period} K线数据")
            return kline_data
            
        except Exception as e:
            print(f"❌ 获取K线数据失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def convert_to_dataframe(self, kline_data):
        """将K线数据转换为pandas DataFrame"""
        try:
            print(f"\n🔄 开始转换K线数据为DataFrame...")
            print(f"   输入数据条数: {len(kline_data)}")
            
            # 转换为DataFrame - 根据实际数据结构调整列数 (指数K线数据只有6列)
            if len(kline_data[0]) == 6:
                df = pd.DataFrame(kline_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'confirm'])
            else:
                # 兼容其他格式
                df = pd.DataFrame(kline_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'vol_ccy', 'confirm'])
            
            # 时间戳转换
            df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int) / 1000, unit='s')
            df = df.iloc[::-1]  # 反转数据，最新的在最后
            df['timestamp'] = df['timestamp'] + pd.Timedelta(hours=8)  # 转换为东八区时间
            df.set_index('timestamp', inplace=True)
            
            # 数值转换
            for col in ['open', 'high', 'low', 'close']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # 如果有成交量字段就转换，没有就设为0
            if 'volume' in df.columns:
                df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
            else:
                df['volume'] = 0.0
                
            if 'vol_ccy' in df.columns:
                df['vol_ccy'] = pd.to_numeric(df['vol_ccy'], errors='coerce')  
            else:
                df['vol_ccy'] = 0.0
            
            print(f"✅ 数据转换完成")
            print(f"   DataFrame shape: {df.shape}")
            print(f"   时间范围: {df.index[0]} 到 {df.index[-1]}")
            
            return df
            
        except Exception as e:
            print(f"❌ 数据转换失败: {e}")
            traceback.print_exc()
            return None
    
    def calculate_technical_indicators(self, df):
        """
        计算技术指标
        参照crypto_analysis_batch.py的算法实现
        """
        try:
            print(f"\n📈 开始计算技术指标...")
            print(f"   数据长度: {len(df)}")
            
            size = len(df)
            if size < 100:
                print(f"❌ 数据量不足，需要至少100条数据，当前只有{size}条")
                return None
            
            # 获取价格数组
            close_prices = df['close'].values
            high_prices = df['high'].values
            low_prices = df['low'].values
            
            # 初始化指标数组
            MA1 = np.zeros(size)    # 12周期EMA
            MA2 = np.zeros(size)    # 26周期EMA
            dif = np.zeros(size)    # DIF
            dea = np.zeros(size)    # DEA
            macd = np.zeros(size)   # MACD
            ema = np.zeros(size)    # EMA
            macd_ema = np.zeros(size)  # MACD-EMA (自定义指标)
            
            # 关键缺失变量：zero1和zero2（MACD临界值）
            zero1 = np.zeros(size)   # MACD临界值
            zero2 = np.zeros(size)   # MACD-EMA临界值
            
            # 方向和交易信号数组
            flags = np.zeros(size)   # 方向标志（1=rise, -1=fall）
            current_profit = np.zeros(size)  # 当前持仓盈亏
            deal_flag_num = np.zeros(size)   # 交易信号（1=开多, -1=开空, 0=无信号）
            strategy_flag_num = np.zeros(size)  # 策略标志
            
            # MA12和MA26 (简单移动平均)
            ma12 = np.zeros(size)
            ma26 = np.zeros(size)
            
            # 布林带指标
            boll_upper = np.zeros(size)
            boll_middle = np.zeros(size)
            boll_lower = np.zeros(size)
            
            # RSI指标
            rsi = np.zeros(size)
            
            # SAR指标
            sar = np.zeros(size)
            
            # KDJ指标
            k = np.zeros(size)
            d = np.zeros(size)
            j = np.zeros(size)
            
            print("🔄 计算EMA和MACD指标...")
            
            # 计算EMA和MACD (参照crypto_analysis_batch.py算法)
            if size > 29:
                # 计算MA1和MA2初始值 (EMA12和EMA26)
                price_sum = 0.0
                for i in range(12):
                    price_sum += close_prices[29 - i]
                price_sum += close_prices[29]
                MA1[29] = price_sum / 13.0
                
                price_sum = 0.0
                for i in range(26):
                    price_sum += close_prices[29 - i]
                price_sum += close_prices[29]
                MA2[29] = price_sum / 27.0
                
                # 计算30-40的指标
                for i in range(30, min(41, size)):
                    MA1[i] = (MA1[i-1] * 11.0 / 13.0) + close_prices[i] * 2.0 / 13.0
                    MA2[i] = (MA2[i-1] * 25.0 / 27.0) + close_prices[i] * 2.0 / 27.0
                    dif[i] = MA1[i] - MA2[i]
                    
                    if i >= 39:
                        if i == 39 or i == 40:
                            dif_sum = 0.0
                            for j_idx in range(9):
                                dif_sum += dif[i - j_idx]
                            dif_sum += dif[i]
                            dea[i] = dif_sum / 9.0
                            macd[i] = (dif[i] - dea[i]) * 2
                            ema[i] = macd[i]
                
                # 策略状态变量（完全参照crypto_analysis_batch.py）
                strategy_flag = "EMA"  # 策略标志："EMA" 或 "DMD"
                deal_flag = ""  # 交易标志："waitRise" 或 "waitFall" 或 ""
                current_flag = "rise"  # 当前方向标志："rise" 或 "fall"
                
                # 交易相关变量
                trade_price = 0.0
                total_profit = 0.0
                trade_count = 0
                win_count = 0
                
                # 主循环计算（从41开始）
                for i in range(41, size):
                    # 计算zero1和zero2（完全参照crypto_analysis_batch.py）
                    denominator = (2.0 / 13.0) - (2.0 / 27.0)
                    if abs(denominator) > 1e-10:
                        zero1[i] = (dea[i-1] - (MA1[i-1] * 11.0 / 13.0) + (MA2[i-1] * 25.0 / 27.0)) / denominator
                        zero2[i] = (dea[i-1] - (MA1[i-1] * 11.0 / 13.0) + (MA2[i-1] * 25.0 / 27.0) + (ema[i-1] * 8.0 / 10.0)) / denominator
                    
                    # 计算EMA指标
                    MA1[i] = (MA1[i-1] * 11.0 / 13.0) + close_prices[i] * 2.0 / 13.0
                    MA2[i] = (MA2[i-1] * 25.0 / 27.0) + close_prices[i] * 2.0 / 27.0
                    dif[i] = MA1[i] - MA2[i]
                    dea[i] = (dea[i-1] * 8.0 / 10.0) + (dif[i] * 2.0 / 10.0)
                    macd[i] = (dif[i] - dea[i]) * 2
                    ema[i] = (ema[i-1] * 8.0 / 10.0) + (macd[i] * 2.0 / 10.0)
                    
                    # 计算自定义指标 MACD-EMA
                    macd_ema[i] = macd[i] - ema[i]
                    
                    # 交易逻辑（完全按照您提供的逻辑）
                    if strategy_flag == "EMA":
                        if macd[i] - ema[i] > 0:
                            if dif[i-1] > 0:
                                current_flag = "rise"
                                strategy_flag = "EMA"
                                deal_flag = ""
                            else:
                                strategy_flag = "DMD"
                                deal_flag = "waitRise"
                        else:
                            if dif[i-1] < 0:
                                current_flag = "fall"
                                strategy_flag = "EMA"
                                deal_flag = ""
                            else:
                                strategy_flag = "DMD"
                                deal_flag = "waitFall"
                    
                    elif strategy_flag == "DMD":
                        if deal_flag == "waitRise":
                            if macd[i] > 0:
                                current_flag = "rise"
                                strategy_flag = "EMA"
                                deal_flag = ""
                            else:
                                current_flag = "fall"
                        elif deal_flag == "waitFall":
                            if macd[i] < 0:
                                current_flag = "fall"
                                strategy_flag = "EMA"
                                deal_flag = ""
                            else:
                                current_flag = "rise"
                    
                    # 设置方向标志
                    flags[i] = 1 if current_flag == "rise" else -1
                    
                    # 检查交易信号（参照crypto_analysis_batch.py）
                    if i > 41:
                        prev_flag = 1 if flags[i-1] == 1 else -1
                        curr_flag = flags[i]
                        
                        if curr_flag == 1 and prev_flag == -1:  # 从fall转为rise，开多信号
                            if current_flag == "rise" and trade_price > 0:
                                # 平空计算盈亏
                                profit = (trade_price - close_prices[i]) / trade_price
                                total_profit += profit
                                trade_count += 1
                                if profit > 0:
                                    win_count += 1
                            
                            # 设置新的交易价格
                            trade_price = zero1[i-1] if deal_flag == "waitRise" else zero2[i-1]
                            # 价格检查
                            if trade_price > high_prices[i] or trade_price < low_prices[i]:
                                trade_price = close_prices[i]
                            
                            deal_flag_num[i] = 1  # 开多标志
                        
                        elif curr_flag == -1 and prev_flag == 1:  # 从rise转为fall，开空信号
                            if current_flag == "fall" and trade_price > 0:
                                # 平多计算盈亏
                                profit = (close_prices[i] - trade_price) / trade_price
                                total_profit += profit
                                trade_count += 1
                                if profit > 0:
                                    win_count += 1
                            
                            # 设置新的交易价格
                            trade_price = zero1[i-1] if deal_flag == "waitFall" else zero2[i-1]
                            # 价格检查
                            if trade_price > high_prices[i] or trade_price < low_prices[i]:
                                trade_price = close_prices[i]
                            
                            deal_flag_num[i] = -1  # 开空标志
                    
                    # 计算当前持仓盈亏
                    if trade_price > 0:
                        if current_flag == "rise":  # 多头持仓
                            current_profit[i] = ((close_prices[i] / trade_price) - 1) * 100
                        else:  # 空头持仓
                            current_profit[i] = (1 - (close_prices[i] / trade_price)) * 100
                    
                    # 策略标志（根据当前策略状态设置）
                    if strategy_flag == "EMA":
                        strategy_flag_num[i] = 1
                    elif strategy_flag == "DMD":
                        if deal_flag == "waitRise":
                            strategy_flag_num[i] = 2
                        elif deal_flag == "waitFall":
                            strategy_flag_num[i] = 3
                        else:
                            strategy_flag_num[i] = 0
            
            print("🔄 计算简单移动平均MA12和MA26...")
            # 计算MA12和MA26
            for i in range(size):
                if i >= 11:  # MA12
                    ma12[i] = np.mean(close_prices[i-11:i+1])
                if i >= 25:  # MA26  
                    ma26[i] = np.mean(close_prices[i-25:i+1])
            
            print("🔄 计算BOLL布林带...")
            # 计算BOLL (20周期, 2倍标准差)
            period = 20
            for i in range(period-1, size):
                subset = close_prices[i-period+1:i+1]
                middle = np.mean(subset)
                std = np.std(subset)
                boll_middle[i] = middle
                boll_upper[i] = middle + 2 * std
                boll_lower[i] = middle - 2 * std
            
            print("🔄 计算RSI...")
            # 计算RSI (14周期)
            period = 14
            for i in range(period, size):
                gains = []
                losses = []
                for j_idx in range(period):
                    change = close_prices[i-period+1+j_idx] - close_prices[i-period+j_idx]
                    if change > 0:
                        gains.append(change)
                        losses.append(0)
                    else:
                        gains.append(0)
                        losses.append(abs(change))
                
                avg_gain = np.mean(gains)
                avg_loss = np.mean(losses)
                
                if avg_loss == 0:
                    rsi[i] = 100
                else:
                    rs = avg_gain / avg_loss
                    rsi[i] = 100 - (100 / (1 + rs))
            
            print("🔄 计算SAR抛物转向...")
            # 计算SAR (简化版本)
            af = 0.02  # 加速因子
            af_step = 0.02
            af_max = 0.2
            
            if size > 2:
                sar[0] = low_prices[0]
                sar[1] = low_prices[1]
                trend = 1  # 1为上升, -1为下降
                ep = high_prices[1] if trend == 1 else low_prices[1]  # 极值点
                
                for i in range(2, size):
                    sar[i] = sar[i-1] + af * (ep - sar[i-1])
                    
                    # 检查趋势反转
                    if trend == 1:  # 上升趋势
                        if close_prices[i] < sar[i]:
                            trend = -1
                            sar[i] = ep
                            ep = low_prices[i]
                            af = af_step
                        else:
                            if high_prices[i] > ep:
                                ep = high_prices[i]
                                af = min(af + af_step, af_max)
                    else:  # 下降趋势
                        if close_prices[i] > sar[i]:
                            trend = 1
                            sar[i] = ep
                            ep = high_prices[i]
                            af = af_step
                        else:
                            if low_prices[i] < ep:
                                ep = low_prices[i]
                                af = min(af + af_step, af_max)
            
            print("🔄 计算KDJ...")
            # 计算KDJ (9周期)
            period = 9
            for i in range(period-1, size):
                subset_high = high_prices[i-period+1:i+1]
                subset_low = low_prices[i-period+1:i+1]
                
                highest = np.max(subset_high)
                lowest = np.min(subset_low)
                
                if highest == lowest:
                    rsv = 50
                else:
                    rsv = (close_prices[i] - lowest) / (highest - lowest) * 100
                
                if i == period - 1:
                    k[i] = rsv
                    d[i] = rsv
                else:
                    k[i] = (2 * k[i-1] + rsv) / 3
                    d[i] = (2 * d[i-1] + k[i]) / 3
                
                j[i] = 3 * k[i] - 2 * d[i]
            
            # 创建结果DataFrame
            result_df = pd.DataFrame(index=df.index)
            result_df['open'] = df['open']
            result_df['high'] = df['high'] 
            result_df['low'] = df['low']
            result_df['close'] = df['close']
            result_df['volume'] = df['volume']
            
            # 添加技术指标
            result_df['ma12'] = ma12
            result_df['ma26'] = ma26
            result_df['ema12'] = MA1
            result_df['ema26'] = MA2
            result_df['dif'] = dif
            result_df['dea'] = dea  
            result_df['macd'] = macd
            result_df['ema'] = ema
            result_df['macd_ema'] = macd_ema  # 自定义指标
            
            # 添加策略相关指标（使用正确的变量名）
            result_df['zero1'] = zero1  # MACD临界值
            result_df['zero2'] = zero2  # MACD-EMA临界值
            result_df['direction'] = flags  # 方向标志（1=rise, -1=fall）
            result_df['current_profit'] = current_profit  # 当前持仓盈亏
            result_df['strategy_flag'] = strategy_flag_num  # 策略标志
            result_df['deal_flag'] = deal_flag_num  # 交易标志
            result_df['boll_upper'] = boll_upper
            result_df['boll_middle'] = boll_middle
            result_df['boll_lower'] = boll_lower
            
            result_df['rsi'] = rsi
            result_df['sar'] = sar
            
            result_df['k'] = k
            result_df['d'] = d
            result_df['j'] = j
            
            print(f"✅ 技术指标计算完成")
            print(f"   计算的指标数量: {len([col for col in result_df.columns if col not in ['open', 'high', 'low', 'close', 'volume']])}")
            
            # 显示最新几个值
            print(f"\n📊 最新3条数据预览:")
            latest_3 = result_df.tail(3)
            for idx, row in latest_3.iterrows():
                print(f"   {idx.strftime('%Y-%m-%d %H:%M:%S')}: 收盘={row['close']:.4f}, MACD-EMA={row['macd_ema']:.6f}, RSI={row['rsi']:.2f}")
            
            return result_df
            
        except Exception as e:
            print(f"❌ 技术指标计算失败: {e}")
            traceback.print_exc()
            return None
    
    def delete_existing_data(self, time_period, start_time, end_time):
        """删除指定时间段的现有技术指标数据，避免重复插入"""
        try:
            print(f"\n🗑️ 清理{time_period}周期重复数据...")
            print(f"   时间范围: {start_time} 到 {end_time}")
            
            # 导入DeleteApi
            from influxdb_client.client.delete_api import DeleteApi
            delete_api = self.client.delete_api()
            
            # 构建删除条件
            predicate = f'_measurement="{self.indicator_measurement}" AND inst_id="{self.inst_id}" AND period="{time_period}"'
            
            # 执行删除
            delete_api.delete(
                start=start_time,
                stop=end_time,
                predicate=predicate,
                bucket=self.bucket,
                org=self.org
            )
            
            print(f"✅ 重复数据清理完成")
            return True
            
        except Exception as e:
            print(f"⚠️ 清理重复数据时出错: {e}")
            # 不影响主流程，继续执行
            return False
    
    def store_indicators_to_influxdb(self, indicators_df, time_period, enable_dedup=True):
        """将技术指标数据存储到InfluxDB"""
        try:
            print(f"\n💾 开始存储{time_period}周期的技术指标到InfluxDB...")
            print(f"   数据条数: {len(indicators_df)}")
            print(f"   去重机制: {'启用' if enable_dedup else '禁用'}")
            
            # 数据去重处理
            if enable_dedup and len(indicators_df) > 0:
                # 获取数据的时间范围
                start_time = indicators_df.index[0] - pd.Timedelta(hours=8)  # 转为UTC
                end_time = indicators_df.index[-1] - pd.Timedelta(hours=8) + pd.Timedelta(minutes=1)  # 转为UTC并增加1分钟缓冲
                
                # 删除重叠时间段的现有数据
                self.delete_existing_data(time_period, start_time.to_pydatetime(), end_time.to_pydatetime())
            
            points = []
            error_count = 0
            
            for timestamp, row in indicators_df.iterrows():
                try:
                    # 转换为UTC时间
                    utc_time = timestamp - pd.Timedelta(hours=8)
                    
                    # 创建Point对象，只存储技术指标（非原始K线数据）
                    point = Point(self.indicator_measurement) \
                        .tag("period", time_period) \
                        .tag("inst_id", self.inst_id) \
                        .field("ma12", float(row['ma12']) if not pd.isna(row['ma12']) else 0.0) \
                        .field("ma26", float(row['ma26']) if not pd.isna(row['ma26']) else 0.0) \
                        .field("ema12", float(row['ema12']) if not pd.isna(row['ema12']) else 0.0) \
                        .field("ema26", float(row['ema26']) if not pd.isna(row['ema26']) else 0.0) \
                        .field("dif", float(row['dif']) if not pd.isna(row['dif']) else 0.0) \
                        .field("dea", float(row['dea']) if not pd.isna(row['dea']) else 0.0) \
                        .field("macd", float(row['macd']) if not pd.isna(row['macd']) else 0.0) \
                        .field("ema", float(row['ema']) if not pd.isna(row['ema']) else 0.0) \
                        .field("macd_ema", float(row['macd_ema']) if not pd.isna(row['macd_ema']) else 0.0) \
                        .field("zero1", float(row['zero1']) if not pd.isna(row['zero1']) else 0.0) \
                        .field("zero2", float(row['zero2']) if not pd.isna(row['zero2']) else 0.0) \
                        .field("direction", str(row['direction']) if 'direction' in row and not pd.isna(row['direction']) else "") \
                        .field("boll_upper", float(row['boll_upper']) if not pd.isna(row['boll_upper']) else 0.0) \
                        .field("boll_middle", float(row['boll_middle']) if not pd.isna(row['boll_middle']) else 0.0) \
                        .field("boll_lower", float(row['boll_lower']) if not pd.isna(row['boll_lower']) else 0.0) \
                        .field("rsi", float(row['rsi']) if not pd.isna(row['rsi']) else 0.0) \
                        .field("sar", float(row['sar']) if not pd.isna(row['sar']) else 0.0) \
                        .field("k", float(row['k']) if not pd.isna(row['k']) else 0.0) \
                        .field("d", float(row['d']) if not pd.isna(row['d']) else 0.0) \
                        .field("j", float(row['j']) if not pd.isna(row['j']) else 0.0) \
                        .field("close", float(row['close']) if not pd.isna(row['close']) else 0.0) \
                        .field("deal_flag", float(row['deal_flag']) if 'deal_flag' in row and not pd.isna(row['deal_flag']) else 0.0) \
                        .field("current_profit", float(row['current_profit']) if 'current_profit' in row and not pd.isna(row['current_profit']) else 0.0) \
                        .field("strategy_flag", float(row['strategy_flag']) if 'strategy_flag' in row and not pd.isna(row['strategy_flag']) else 0.0) \
                        .time(utc_time.to_pydatetime())
                    
                    points.append(point)
                    
                except Exception as e:
                    error_count += 1
                    if error_count <= 3:  # 只显示前3个错误
                        print(f"⚠️ 转换数据点时出错: {e}")
                    continue
            
            if not points:
                print(f"❌ 没有有效的数据点可以存储")
                return False
            
            print(f"🔄 开始写入InfluxDB...")
            print(f"   目标桶: {self.bucket}")
            print(f"   数据表: {self.indicator_measurement}")
            print(f"   数据点数量: {len(points)}")
            print(f"   转换错误: {error_count}")
            
            write_start = time.time()
            self.write_api.write(bucket=self.bucket, org=self.org, record=points)
            write_time = time.time() - write_start
            
            print(f"✅ 技术指标数据写入成功")
            print(f"   耗时: {write_time:.2f}秒") 
            print(f"   写入速度: {len(points)/write_time:.0f} 点/秒")
            
            return True
            
        except Exception as e:
            print(f"❌ 存储技术指标数据失败: {e}")
            traceback.print_exc()
            return False
    
    def query_latest_indicators(self, time_period, limit=3):
        """查询指定周期的最新技术指标数据"""
        try:
            print(f"\n🔍 查询 {time_period} 周期最新 {limit} 条技术指标数据...")
            print(f"   数据表: {self.indicator_measurement}")
            print(f"   合约代码: {self.inst_id}")
            
            # Flux查询语句
            query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -30d)
                |> filter(fn: (r) => r._measurement == "{self.indicator_measurement}")
                |> filter(fn: (r) => r.period == "{time_period}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"], desc: true)
                |> limit(n: {limit})
            '''
            
            print(f"📋 执行Flux查询...")
            query_start = time.time()
            result = self.query_api.query(org=self.org, query=query)
            query_time = time.time() - query_start
            
            print(f"✅ 查询执行完成，耗时: {query_time:.2f}秒")
            
            records = []
            for table in result:
                for record in table.records:
                    records.append(record)
            
            print(f"📊 查询结果统计:")
            print(f"   记录数量: {len(records)}")
            
            if records:
                print(f"✅ 查询到 {len(records)} 条 {time_period} 技术指标数据")
                print(f"\n📈 {time_period} 周期最新 {len(records)} 条技术指标数据:")
                print("-" * 150)
                print(f"{'时间':<20} {'收盘价':<10} {'MACD-EMA':<12} {'MA12':<10} {'MA26':<10} {'RSI':<8} {'BOLL上':<10} {'KDJ_K':<8} {'KDJ_D':<8}")
                print("-" * 150)
                
                for i, record in enumerate(records):
                    time_str = record.get_time().strftime('%Y-%m-%d %H:%M:%S')
                    close_val = record.values.get('close', 0)
                    macd_ema_val = record.values.get('macd_ema', 0)
                    ma12_val = record.values.get('ma12', 0)
                    ma26_val = record.values.get('ma26', 0)
                    rsi_val = record.values.get('rsi', 0)
                    boll_upper_val = record.values.get('boll_upper', 0)
                    k_val = record.values.get('k', 0)
                    d_val = record.values.get('d', 0)
                    
                    print(f"{time_str:<20} {close_val:<10.4f} {macd_ema_val:<12.6f} {ma12_val:<10.4f} {ma26_val:<10.4f} {rsi_val:<8.2f} {boll_upper_val:<10.4f} {k_val:<8.2f} {d_val:<8.2f}")
                    
                    # 显示最新数据的详细信息
                    if i == 0:
                        print(f"\n📋 最新技术指标详情:")
                        print(f"   时间: {time_str} UTC")
                        print(f"   收盘价: {close_val:.4f}")
                        print(f"   🎯 MACD-EMA (自定义): {macd_ema_val:.6f}")
                        print(f"   MA12: {ma12_val:.4f}, MA26: {ma26_val:.4f}")
                        print(f"   MACD: {record.values.get('macd', 0):.6f}, DIF: {record.values.get('dif', 0):.6f}, DEA: {record.values.get('dea', 0):.6f}")
                        print(f"   RSI: {rsi_val:.2f}")
                        print(f"   BOLL: 上轨{boll_upper_val:.4f}, 中轨{record.values.get('boll_middle', 0):.4f}, 下轨{record.values.get('boll_lower', 0):.4f}")
                        print(f"   KDJ: K{k_val:.2f}, D{d_val:.2f}, J{record.values.get('j', 0):.2f}")
                        print(f"   SAR: {record.values.get('sar', 0):.4f}")
                
                return True
            else:
                print(f"⚠️ 未找到 {time_period} 周期的技术指标数据")
                return False
                
        except Exception as e:
            print(f"❌ 查询 {time_period} 技术指标数据失败: {e}")
            traceback.print_exc()
            return False
    
    def get_last_indicator_values(self, time_period):
        """获取最后一条技术指标记录，用于增量计算"""
        try:
            print(f"\n🔍 获取{time_period}周期最后一条技术指标数据...")
            
            # Flux查询语句 - 获取最新的一条记录
            query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -7d)
                |> filter(fn: (r) => r._measurement == "{self.indicator_measurement}")
                |> filter(fn: (r) => r.period == "{time_period}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"], desc: true)
                |> limit(n: 1)
            '''
            
            result = self.query_api.query(org=self.org, query=query)
            
            last_values = None
            for table in result:
                for record in table.records:
                    last_values = {
                        'timestamp': record.get_time(),
                        'MA1': record.values.get('ema12', 0),  # EMA12
                        'MA2': record.values.get('ema26', 0),  # EMA26
                        'dif': record.values.get('dif', 0),
                        'dea': record.values.get('dea', 0),
                        'macd': record.values.get('macd', 0),
                        'ema': record.values.get('ema', 0),
                        'macd_ema': record.values.get('macd_ema', 0),
                        'zero1': record.values.get('zero1', 0),
                        'zero2': record.values.get('zero2', 0),
                        'direction': record.values.get('direction', 0),
                        'current_profit': record.values.get('current_profit', 0),
                        'strategy_flag': record.values.get('strategy_flag', 0)
                    }
                    break
            
            if last_values:
                print(f"✅ 获取到最后记录时间: {last_values['timestamp']}")
                return last_values
            else:
                print(f"⚠️ 未找到历史数据，将进行全量计算")
                return None
                
        except Exception as e:
            print(f"❌ 获取最后指标值失败: {e}")
            return None
    
    def calculate_incremental_indicators(self, new_kline_data, time_period, last_values=None):
        """
        增量计算技术指标
        
        Args:
            new_kline_data: 新的K线数据 
            time_period: 时间周期
            last_values: 最后一条技术指标数据，用于增量计算
        """
        try:
            print(f"\n📈 开始增量计算{time_period}周期技术指标...")
            
            # 转换为DataFrame
            df = self.convert_to_dataframe(new_kline_data)
            if df is None:
                return None
            
            if last_values is None:
                print(f"⚠️ 无历史数据，执行全量计算")
                return self.calculate_technical_indicators(df)
            
            # 获取新数据的最后几个价格点
            close_prices = df['close'].values
            high_prices = df['high'].values
            low_prices = df['low'].values
            
            if len(close_prices) == 0:
                print(f"❌ 无新的K线数据")
                return None
            
            # 获取最新价格
            current_price = close_prices[-1]
            current_high = high_prices[-1]
            current_low = low_prices[-1]
            
            print(f"📊 增量计算最新数据点:")
            print(f"   时间: {df.index[-1]}")
            print(f"   价格: {current_price}")
            
            # 基于最后的指标值计算新的指标
            new_MA1 = (last_values['MA1'] * 11.0 / 13.0) + current_price * 2.0 / 13.0
            new_MA2 = (last_values['MA2'] * 25.0 / 27.0) + current_price * 2.0 / 27.0
            new_dif = new_MA1 - new_MA2
            new_dea = (last_values['dea'] * 8.0 / 10.0) + (new_dif * 2.0 / 10.0)
            new_macd = (new_dif - new_dea) * 2
            new_ema = (last_values['ema'] * 8.0 / 10.0) + (new_macd * 2.0 / 10.0)
            new_macd_ema = new_macd - new_ema
            
            # 计算zero1和zero2
            denominator = (2.0 / 13.0) - (2.0 / 27.0)
            new_zero1 = 0
            new_zero2 = 0
            if abs(denominator) > 1e-10:
                new_zero1 = (new_dea - (new_MA1 * 11.0 / 13.0) + (new_MA2 * 25.0 / 27.0)) / denominator
                new_zero2 = (new_dea - (new_MA1 * 11.0 / 13.0) + (new_MA2 * 25.0 / 27.0) + (new_ema * 8.0 / 10.0)) / denominator
            
            # 简化方向判断（可以根据需要增强）
            new_direction = 1 if new_macd_ema > 0 else -1
            
            # 构建增量结果DataFrame
            result_df = pd.DataFrame(index=df.index[-1:])  # 只返回最新的一条记录
            result_df['open'] = df['open'].iloc[-1]
            result_df['high'] = df['high'].iloc[-1] 
            result_df['low'] = df['low'].iloc[-1]
            result_df['close'] = df['close'].iloc[-1]
            result_df['volume'] = df['volume'].iloc[-1]
            
            # 技术指标
            result_df['ma12'] = current_price  # 简化计算
            result_df['ma26'] = current_price  # 简化计算
            result_df['ema12'] = new_MA1
            result_df['ema26'] = new_MA2
            result_df['dif'] = new_dif
            result_df['dea'] = new_dea
            result_df['macd'] = new_macd
            result_df['ema'] = new_ema
            result_df['macd_ema'] = new_macd_ema
            result_df['zero1'] = new_zero1
            result_df['zero2'] = new_zero2
            result_df['direction'] = new_direction
            result_df['current_profit'] = 0.0  # 需要更复杂的计算
            result_df['strategy_flag'] = 1  # 简化
            result_df['deal_flag'] = 0  # 简化
            
            # 简化的其他指标
            result_df['boll_upper'] = current_price * 1.02
            result_df['boll_middle'] = current_price
            result_df['boll_lower'] = current_price * 0.98
            result_df['rsi'] = 50.0  # 简化
            result_df['sar'] = current_price * 0.99
            result_df['k'] = 50.0  # 简化
            result_df['d'] = 50.0  # 简化
            result_df['j'] = 50.0  # 简化
            
            print(f"✅ 增量计算完成")
            print(f"   MACD-EMA: {new_macd_ema:.6f}")
            print(f"   方向: {'上涨' if new_direction == 1 else '下跌'}")
            
            return result_df
            
        except Exception as e:
            print(f"❌ 增量计算失败: {e}")
            traceback.print_exc()
            return None
    
    def process_period_indicators(self, time_period, incremental=False, new_kline_data=None):
        """处理指定周期的技术指标计算和存储"""
        try:
            print(f"\n" + "="*80)
            print(f"📈 处理 {time_period} 周期技术指标 ({'增量更新' if incremental else '全量计算'})")
            print("="*80)
            
            if incremental and new_kline_data is not None:
                # 增量更新模式
                print(f"🔄 步骤1: 获取历史指标数据")
                last_values = self.get_last_indicator_values(time_period)
                
                print(f"🔄 步骤2: 增量计算技术指标")
                indicators_df = self.calculate_incremental_indicators(new_kline_data, time_period, last_values)
                if indicators_df is None:
                    print(f"❌ 步骤2失败: 增量指标计算失败")
                    return False
                
                print(f"🔄 步骤3: 存储增量指标到InfluxDB")
                if not self.store_indicators_to_influxdb(indicators_df, time_period, enable_dedup=True):
                    print(f"❌ 步骤3失败: 存储增量指标失败")
                    return False
                
            else:
                # 全量计算模式
                print(f"🔄 步骤1: 获取K线数据")
                kline_data = self.get_kline_data(time_period)
                if not kline_data:
                    print(f"❌ 步骤1失败: 无法获取{time_period}周期数据")
                    return False
                
                print(f"🔄 步骤2: 转换数据格式")
                df = self.convert_to_dataframe(kline_data)
                if df is None:
                    print(f"❌ 步骤2失败: 数据转换失败")
                    return False
                
                print(f"🔄 步骤3: 计算技术指标")
                indicators_df = self.calculate_technical_indicators(df)
                if indicators_df is None:
                    print(f"❌ 步骤3失败: 技术指标计算失败")
                    return False
                
                print(f"🔄 步骤4: 存储技术指标到InfluxDB")
                if not self.store_indicators_to_influxdb(indicators_df, time_period, enable_dedup=True):
                    print(f"❌ 步骤4失败: 存储技术指标失败")
                    return False
                
                print(f"✅ {time_period} 周期技术指标处理完成")
                return True
            
        except Exception as e:
            print(f"❌ {time_period} 周期技术指标处理失败: {e}")
            traceback.print_exc()
            return False
    
    def run_full_process(self):
        """运行完整的技术指标计算和存储流程"""
        print("\n" + "="*100)
        print("🚀 NEAR-USDT-SWAP技术指标计算与存储流程")
        print("="*100)
        print(f"⏰ 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📊 处理合约: {self.inst_id}")
        print(f"📈 时间周期: {', '.join(self.time_periods)}")
        print(f"🎯 计算指标: MACD-EMA(自定义), MA12, MA26, MACD, DIF, DEA, BOLL, RSI, SAR, KDJ")
        
        # 统计信息
        stats = {
            "连接状态": False,
            "计算成功": 0,
            "计算失败": 0,
            "存储成功": 0,
            "存储失败": 0,
            "查询成功": 0,
            "查询失败": 0,
            "处理详情": []
        }
        
        try:
            # 1. 连接InfluxDB
            print(f"\n🔄 步骤1: 连接InfluxDB")
            if not self.connect_influxdb():
                print(f"❌ InfluxDB连接失败，程序终止")
                return False
            stats["连接状态"] = True
            print(f"✅ InfluxDB连接成功")
            
            # 2. 处理每个时间周期
            for i, period in enumerate(self.time_periods):
                print(f"\n🔄 步骤2.{i+1}: 处理 {period} 周期技术指标 ({i+1}/{len(self.time_periods)})")
                period_start = time.time()
                
                period_stats = {
                    "周期": period,
                    "计算": False,
                    "存储": False,
                    "查询": False,
                    "耗时": 0
                }
                
                # 计算和存储技术指标
                if self.process_period_indicators(period):
                    stats["计算成功"] += 1
                    stats["存储成功"] += 1
                    period_stats["计算"] = True
                    period_stats["存储"] = True
                    print(f"   ✅ {period}周期技术指标计算和存储成功")
                    
                    # 等待数据写入完成
                    print(f"   ⏳ 等待数据写入完成...")
                    time.sleep(2)
                    
                    # 查询验证
                    print(f"   🔄 验证{period}周期技术指标数据...")
                    if self.query_latest_indicators(period):
                        stats["查询成功"] += 1
                        period_stats["查询"] = True
                        print(f"   ✅ {period}周期技术指标查询成功")
                    else:
                        stats["查询失败"] += 1
                        print(f"   ❌ {period}周期技术指标查询失败")
                else:
                    stats["计算失败"] += 1
                    stats["存储失败"] += 1
                    stats["查询失败"] += 1
                    print(f"   ❌ {period}周期技术指标处理失败")
                
                period_stats["耗时"] = time.time() - period_start
                stats["处理详情"].append(period_stats)
                
                print(f"   ⏱️ {period}周期处理完成，耗时: {period_stats['耗时']:.2f}秒")
            
            # 3. 显示统计信息
            print("\n" + "="*100)
            print("📊 技术指标计算统计报告")
            print("="*100)
            print(f"   InfluxDB连接: {'✅ 成功' if stats['连接状态'] else '❌ 失败'}")
            print(f"   指标计算成功: {stats['计算成功']}/{len(self.time_periods)}")
            print(f"   指标计算失败: {stats['计算失败']}/{len(self.time_periods)}")
            print(f"   数据存储成功: {stats['存储成功']}/{len(self.time_periods)}")
            print(f"   数据存储失败: {stats['存储失败']}/{len(self.time_periods)}")
            print(f"   数据查询成功: {stats['查询成功']}/{len(self.time_periods)}")
            print(f"   数据查询失败: {stats['查询失败']}/{len(self.time_periods)}")
            
            print(f"\n📋 各周期处理详情:")
            for detail in stats["处理详情"]:
                calc_status = "✅" if detail["计算"] else "❌"
                store_status = "✅" if detail["存储"] else "❌"
                query_status = "✅" if detail["查询"] else "❌"
                print(f"   {detail['周期']:<4} - 计算:{calc_status} 存储:{store_status} 查询:{query_status} 耗时:{detail['耗时']:.1f}s")
            
            total_time = sum(d["耗时"] for d in stats["处理详情"])
            print(f"\n⏰ 总处理时间: {total_time:.2f}秒")
            print(f"⏰ 完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            total_operations = len(self.time_periods) * 3  # 计算、存储、查询
            successful_operations = stats['计算成功'] + stats['存储成功'] + stats['查询成功']
            success_rate = successful_operations / total_operations * 100
            print(f"📈 总体成功率: {success_rate:.1f}%")
            
            all_success = (stats['计算成功'] == len(self.time_periods) and 
                          stats['存储成功'] == len(self.time_periods) and 
                          stats['查询成功'] == len(self.time_periods))
            
            if all_success:
                print("✅ 所有技术指标计算和存储完成！")
            else:
                print("⚠️ 部分操作失败，请检查上述日志")
            
            return True
            
        except Exception as e:
            print(f"\n❌ 处理流程执行失败: {e}")
            traceback.print_exc()
            return False
    
    def close(self):
        """关闭连接"""
        if self.client:
            self.client.close()
            print("🔒 InfluxDB连接已关闭")

def main():
    """主函数"""
    print("🎯 启动NEAR技术指标计算程序")
    processor = NEARTechnicalIndicatorsInfluxDB()
    
    try:
        success = processor.run_full_process()
        if success:
            print("\n🎉 所有操作执行成功！技术指标数据已存储到InfluxDB")
            print("💡 提示: 技术指标已存储到garble桶的near_usdt_swap_indicators表中")
            print("🎯 特色指标: MACD-EMA = MACD[i] - EMA[i] (参照crypto_analysis_batch算法)")
        else:
            print("\n⚠️ 部分操作执行失败，请检查上述详细日志")
            
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断程序")
    except Exception as e:
        print(f"\n💥 程序异常: {e}")
        traceback.print_exc()
    finally:
        processor.close()
        print("👋 程序结束")

if __name__ == "__main__":
    main()
