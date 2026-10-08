#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 11 - RSI技术指标计算
===============================

功能说明：
- 获取K线数据计算RSI指标
- RSI指标分析和交易信号
- 超买超卖区域判断

RSI指标说明：
- RSI (Relative Strength Index) 相对强弱指标
- 取值范围：0-100
- > 70：超买区域，可能下跌
- < 30：超卖区域，可能上涨
- 50：多空分界线

计算公式：
RSI = 100 - (100 / (1 + RS))
RS = 平均涨幅 / 平均跌幅

作者：OKX API Demo
创建时间：2024-06-19
版本：v1.0
"""

import okx.MarketData as MarketData
import datetime
import json
import pandas as pd

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
flag = config['flag']  # "0"=实盘数据, "1"=模拟盘数据

print("=" * 60)
print("📊 OKX RSI技术指标分析工具")
print("=" * 60)
print(f"📈 数据来源: {'实盘数据' if flag == '0' else '模拟盘数据'}")
print("=" * 60)

# =============================================================================
# 参数配置
# =============================================================================

# 查询参数
INST_ID = "LTC-USD"         # 交易对
BAR_SIZE = "1H"             # K线周期
DATA_LIMIT = 50             # 获取数据量（RSI计算需要足够历史数据）

# RSI参数
RSI_PERIOD = 14             # RSI计算周期（标准为14）
RSI_OVERBOUGHT = 70         # 超买线
RSI_OVERSOLD = 30           # 超卖线

print(f"🎯 分析配置:")
print(f"   交易对: {INST_ID}")
print(f"   K线周期: {BAR_SIZE}")
print(f"   数据量: {DATA_LIMIT} 根")
print(f"   RSI周期: {RSI_PERIOD}")
print(f"   超买线: {RSI_OVERBOUGHT}")
print(f"   超卖线: {RSI_OVERSOLD}")
print("=" * 60)

# =============================================================================
# 初始化市场数据API
# =============================================================================

try:
    marketDataAPI = MarketData.MarketAPI(flag=flag)
    print("✅ 市场数据API初始化成功")
except Exception as e:
    print(f"❌ 市场数据API初始化失败: {e}")
    exit(1)

# =============================================================================
# RSI指标计算函数
# =============================================================================

def calculate_rsi(prices, period=14):
    """
    计算RSI指标
    
    参数:
    prices: 价格列表（通常是收盘价）
    period: 计算周期（默认14）
    
    返回:
    RSI值列表
    """
    if len(prices) < period + 1:
        return []
    
    # 计算价格变化
    deltas = []
    for i in range(1, len(prices)):
        deltas.append(prices[i] - prices[i-1])
    
    # 分离涨跌
    gains = []
    losses = []
    
    for delta in deltas:
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-delta)
    
    # 计算RSI
    rsi_values = []
    
    for i in range(period-1, len(gains)):
        if i == period - 1:
            # 第一个RSI值：使用简单平均
            avg_gain = sum(gains[i-period+1:i+1]) / period
            avg_loss = sum(losses[i-period+1:i+1]) / period
        else:
            # 后续RSI值：使用指数平滑
            avg_gain = (avg_gain * (period-1) + gains[i]) / period
            avg_loss = (avg_loss * (period-1) + losses[i]) / period
        
        # 避免除零错误
        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        
        rsi_values.append(rsi)
    
    return rsi_values

# =============================================================================
# 获取K线数据
# =============================================================================

try:
    print(f"\n📈 正在获取K线数据...")
    
    result = marketDataAPI.get_index_candlesticks(
        instId=INST_ID,
        bar=BAR_SIZE,
        limit=DATA_LIMIT
    )
    
    print("✅ K线数据获取成功！")
    
except Exception as e:
    print(f"❌ 获取K线数据时发生错误: {e}")
    exit(1)

# =============================================================================
# 数据处理和RSI计算
# =============================================================================

try:
    code = result['code']
    
    if code == '0':
        kline_data = result['data']
        
        if not kline_data:
            print("❌ 未获取到K线数据")
            exit(1)
        
        print(f"📊 获取到 {len(kline_data)} 根K线数据")
        
        # 提取收盘价和时间（OKX数据是倒序的，需要反转）
        kline_data.reverse()  # 反转为正序（从旧到新）
        
        timestamps = []
        close_prices = []
        
        for kline in kline_data:
            timestamp = int(kline[0])
            close_price = float(kline[4])  # 收盘价
            
            timestamps.append(timestamp)
            close_prices.append(close_price)
        
        print(f"📊 价格范围: ${min(close_prices):.4f} - ${max(close_prices):.4f}")
        
        # 计算RSI指标
        print(f"\n🔄 正在计算RSI指标...")
        rsi_values = calculate_rsi(close_prices, RSI_PERIOD)
        
        if not rsi_values:
            print(f"❌ RSI计算失败：数据不足（需要至少{RSI_PERIOD+1}根K线）")
            exit(1)
        
        print(f"✅ RSI计算完成，共 {len(rsi_values)} 个数值")
        
        # =============================================================================
        # RSI分析结果展示
        # =============================================================================
        
        print("\n" + "=" * 80)
        print("📊 RSI技术指标分析结果")
        print("=" * 80)
        
        # 显示最近的RSI数据
        display_count = min(10, len(rsi_values))
        
        print(f"{'时间':<19} {'价格':<10} {'RSI':<8} {'状态':<12} {'信号'}")
        print("=" * 80)
        
        for i in range(display_count):
            # 从最新数据开始显示
            idx = len(rsi_values) - display_count + i
            timestamp = timestamps[idx + RSI_PERIOD]  # RSI对应的时间
            price = close_prices[idx + RSI_PERIOD]
            rsi = rsi_values[idx]
            
            # 判断RSI状态
            if rsi >= RSI_OVERBOUGHT:
                status = "🔴超买"
                signal = "卖出信号"
            elif rsi <= RSI_OVERSOLD:
                status = "🟢超卖"
                signal = "买入信号"
            elif rsi > 50:
                status = "🟡偏强"
                signal = "观望"
            else:
                status = "🟡偏弱"
                signal = "观望"
            
            # 时间格式化
            dt = datetime.datetime.fromtimestamp(timestamp / 1000)
            time_str = dt.strftime('%Y-%m-%d %H:%M')
            
            print(f"{time_str:<19} ${price:<9.4f} {rsi:<7.2f} {status:<12} {signal}")
        
        # =============================================================================
        # RSI统计分析
        # =============================================================================
        
        current_rsi = rsi_values[-1]
        max_rsi = max(rsi_values)
        min_rsi = min(rsi_values)
        avg_rsi = sum(rsi_values) / len(rsi_values)
        
        print("\n" + "=" * 60)
        print("📈 RSI统计分析")
        print("=" * 60)
        
        print(f"💯 当前RSI: {current_rsi:.2f}")
        print(f"📊 最高RSI: {max_rsi:.2f}")
        print(f"📊 最低RSI: {min_rsi:.2f}")  
        print(f"📊 平均RSI: {avg_rsi:.2f}")
        
        # 当前RSI状态分析
        print(f"\n🎯 当前状态分析:")
        if current_rsi >= RSI_OVERBOUGHT:
            print(f"   🔴 超买状态 (RSI ≥ {RSI_OVERBOUGHT})")
            print(f"   📉 市场可能出现回调")
            print(f"   💡 建议：考虑减仓或等待回调")
            
        elif current_rsi <= RSI_OVERSOLD:
            print(f"   🟢 超卖状态 (RSI ≤ {RSI_OVERSOLD})")
            print(f"   📈 市场可能出现反弹")
            print(f"   💡 建议：考虑加仓或等待确认")
            
        elif current_rsi > 50:
            print(f"   🟡 多头偏强 (RSI > 50)")
            print(f"   📊 多方力量占优")
            print(f"   💡 建议：可适量持有多仓")
            
        else:
            print(f"   🟡 空头偏强 (RSI < 50)")
            print(f"   📊 空方力量占优")
            print(f"   💡 建议：谨慎做多，可考虑做空")
        
        # =============================================================================
        # RSI背离分析
        # =============================================================================
        
        print(f"\n🔍 RSI背离分析:")
        
        # 简单的背离检测（需要至少5个数据点）
        if len(rsi_values) >= 5:
            recent_prices = close_prices[-5:]
            recent_rsi = rsi_values[-5:]
            
            price_trend = "上涨" if recent_prices[-1] > recent_prices[0] else "下跌"
            rsi_trend = "上涨" if recent_rsi[-1] > recent_rsi[0] else "下跌"
            
            if price_trend != rsi_trend:
                print(f"   ⚠️ 可能存在背离现象")
                print(f"   价格趋势: {price_trend}")
                print(f"   RSI趋势: {rsi_trend}")
                print(f"   💡 背离可能预示趋势反转")
            else:
                print(f"   ✅ 价格与RSI趋势一致")
                print(f"   当前趋势: {price_trend}")
        else:
            print(f"   📊 数据不足，无法分析背离")
        
        # =============================================================================
        # RSI交易策略建议
        # =============================================================================
        
        print("\n" + "=" * 60)
        print("💡 RSI交易策略建议")
        print("=" * 60)
        
        print("📈 经典RSI交易策略:")
        print(f"   🟢 买入信号：RSI从超卖区（<{RSI_OVERSOLD}）向上突破")
        print(f"   🔴 卖出信号：RSI从超买区（>{RSI_OVERBOUGHT}）向下跌破")
        print(f"   📊 中性区域：RSI在{RSI_OVERSOLD}-{RSI_OVERBOUGHT}之间，观望为主")
        
        print(f"\n📊 高级策略:")
        print(f"   🔄 RSI背离：价格创新高但RSI不创新高（看跌背离）")
        print(f"   🔄 RSI背离：价格创新低但RSI不创新低（看涨背离）")
        print(f"   📈 趋势确认：RSI突破50线确认趋势方向")
        print(f"   ⚡ 快速反弹：RSI快速从极端区域回归")
        
        print(f"\n⚠️ 使用注意事项:")
        print(f"   - RSI是震荡指标，在趋势市场中可能失效")
        print(f"   - 建议结合其他指标（如MACD、布林带）综合判断")
        print(f"   - 超买超卖可能持续较长时间，需耐心等待")
        print(f"   - 设置合理的止损止盈，控制风险")
        
        # =============================================================================
        # 实时交易建议
        # =============================================================================
        
        print("\n" + "=" * 60)
        print("🎯 基于当前RSI的交易建议")
        print("=" * 60)
        
        if current_rsi >= 80:
            print("🚨 强烈超买警告")
            print("   建议：立即减仓或全部卖出")
            print("   风险：极高回调风险")
            
        elif current_rsi >= RSI_OVERBOUGHT:
            print("⚠️ 超买警告")
            print("   建议：谨慎加仓，可考虑部分获利")
            print("   风险：回调风险较高")
            
        elif current_rsi <= 20:
            print("💚 强烈超卖机会")
            print("   建议：可考虑加仓买入")
            print("   机会：强反弹可能性大")
            
        elif current_rsi <= RSI_OVERSOLD:
            print("🟢 超卖机会")
            print("   建议：可适量买入")
            print("   机会：反弹可能性较大")
            
        else:
            print("📊 正常区间")
            print("   建议：观望为主，等待明确信号")
            print("   策略：跟随趋势，设置止损")

    else:
        print(f"\n❌ 获取数据失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ RSI计算时发生错误: {e}")
    print(f"请检查数据是否充足，RSI计算需要至少{RSI_PERIOD+1}根K线数据")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行其他技术指标分析工具") 