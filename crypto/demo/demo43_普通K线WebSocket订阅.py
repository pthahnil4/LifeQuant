#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX普通K线WebSocket订阅示例
==============================

功能说明：
1. 连接OKX WebSocket业务频道
2. 订阅指定产品的普通K线数据（交易价格）
3. 实时接收K线数据推送
4. 支持多个产品和多个时间周期同时订阅
5. 优雅的连接管理和错误处理

使用场景：
- 实时监控交易价格K线变化
- K线图表数据源
- 技术分析和策略开发
- 价格趋势分析

特点：
- 推送频率最快间隔1秒推送一次数据
- 支持17种不同时间周期的K线
- K线状态标识（0=未完结，1=已完结）
- 支持订阅和取消订阅
- 异步处理，不阻塞主程序

核心参数：
- instId: 产品ID，如 "BTC-USDT", "ETH-USDT"
- channel: 频道名，如 "candle1m", "candle5m"
- callback: 回调函数，处理接收到的K线数据

运行步骤：
1. 修改下方的产品ID和时间周期列表
2. 运行脚本：python demo43_普通K线WebSocket订阅.py
3. 观察实时K线数据
4. 按Ctrl+C停止订阅

API文档：
https://www.okx.com/docs-v5/zh/#websocket-api-business-candlesticks-channel

作者：AI Assistant
创建时间：2025年1月17日
"""

import asyncio
import json
import datetime
import signal
import sys
import os
from typing import List, Dict, Any, Tuple

# 添加父目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from websocket.WsPublicAsync import WsPublicAsync
from ws_config import get_business_url, get_connection_config, print_ws_config

# ==================== 配置参数 ====================

# 要订阅的产品ID列表
INST_IDS = [
    "BTC-USDT",
    "ETH-USDT"
]

# 要订阅的K线时间周期列表
# 可选值：1m, 3m, 5m, 15m, 30m, 1H, 2H, 4H, 6H, 12H, 1D, 2D, 3D, 5D, 1W, 1M, 3M
# 对应的频道名：candle1m, candle3m, etc.
TIMEFRAMES = [
    "1m",   # 1分钟K线
    "5m",   # 5分钟K线
    "15m",  # 15分钟K线
    "1H",   # 1小时K线
]

# 是否显示详细日志
VERBOSE = True

# 是否显示K线图表（简单的ASCII图表）
SHOW_CHART = False

# ==================== 全局变量 ====================

# WebSocket连接实例
ws_client = None

# 运行状态
running = True

# K线数据存储
kline_data = {}

# K线统计信息
kline_stats = {}

# ==================== 回调函数 ====================

def kline_callback(message: str):
    """
    普通K线数据回调函数
    
    Args:
        message: WebSocket接收到的消息（JSON字符串）
    """
    try:
        # 首先解析 JSON 字符串
        data = json.loads(message)
        
        if VERBOSE:
            print(f"\n[RAW] {json.dumps(data, indent=2, ensure_ascii=False)}")
        
        # 检查消息类型
        if 'event' in data:
            # 订阅确认或错误消息
            handle_event_message(data)
        elif 'data' in data and 'arg' in data:
            # K线数据推送
            handle_kline_data(data)
        else:
            print(f"[UNKNOWN] 未知消息格式: {data}")
            
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON解析失败: {e}")
        print(f"[ERROR] 原始消息: {message}")
    except Exception as e:
        print(f"[ERROR] 处理消息时发生异常: {e}")
        print(f"[ERROR] 消息内容: {message}")

def handle_event_message(message: Dict[str, Any]):
    """
    处理事件消息（订阅确认、错误等）
    
    Args:
        message: 事件消息
    """
    event = message.get('event')
    
    if event == 'subscribe':
        arg = message.get('arg', {})
        channel = arg.get('channel', '未知')
        inst_id = arg.get('instId', '未知')
        print(f"✅ [SUBSCRIBE] {inst_id} - {channel} 订阅成功")
        
    elif event == 'unsubscribe':
        arg = message.get('arg', {})
        channel = arg.get('channel', '未知')
        inst_id = arg.get('instId', '未知')
        print(f"❌ [UNSUBSCRIBE] {inst_id} - {channel} 取消订阅成功")
        
    elif event == 'error':
        code = message.get('code', '未知')
        msg = message.get('msg', '未知错误')
        print(f"❌ [ERROR] 错误码: {code}, 消息: {msg}")
        
    else:
        print(f"[EVENT] {event}: {message}")

def handle_kline_data(message: Dict[str, Any]):
    """
    处理K线数据
    
    Args:
        message: K线数据消息
    """
    arg = message.get('arg', {})
    data_list = message.get('data', [])
    
    channel = arg.get('channel', '')
    inst_id = arg.get('instId', '')
    
    # 检查是否为K线数据
    if not channel.startswith('candle'):
        print(f"[WARNING] 接收到非K线数据: {channel}")
        return
    
    # 提取时间周期
    timeframe = channel.replace('candle', '')
    
    for kline in data_list:
        # K线数据格式: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        if len(kline) >= 9:
            ts, o, h, l, c, vol, vol_ccy, vol_ccy_quote, confirm = kline[:9]
            
            # 转换时间戳
            try:
                dt = datetime.datetime.fromtimestamp(int(ts) / 1000)
                time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            except:
                time_str = str(ts)
            
            # 存储K线数据
            key = f"{inst_id}_{timeframe}"
            if key not in kline_data:
                kline_data[key] = []
                kline_stats[key] = {
                    'total_count': 0,
                    'completed_count': 0,
                    'last_update': time_str
                }
            
            # 添加K线数据
            kline_info = {
                'timestamp': ts,
                'datetime': time_str,
                'open': float(o),
                'high': float(h),
                'low': float(l),
                'close': float(c),
                'volume': float(vol),
                'volume_ccy': float(vol_ccy),
                'volume_quote': float(vol_ccy_quote),
                'confirm': str(confirm),
                'is_completed': str(confirm) == '1'
            }
            
            kline_data[key].append(kline_info)
            
            # 更新统计信息
            kline_stats[key]['total_count'] += 1
            kline_stats[key]['last_update'] = time_str
            if str(confirm) == '1':
                kline_stats[key]['completed_count'] += 1
            
            # 保持最近100条K线数据
            if len(kline_data[key]) > 100:
                kline_data[key] = kline_data[key][-100:]
            
            # 计算价格变化
            price_change = ""
            if len(kline_data[key]) > 1:
                prev_close = kline_data[key][-2]['close']
                current_close = kline_info['close']
                change = current_close - prev_close
                change_pct = (change / prev_close) * 100 if prev_close != 0 else 0
                
                if change > 0:
                    price_change = f" 📈 +{change:.4f} (+{change_pct:.2f}%)"
                elif change < 0:
                    price_change = f" 📉 {change:.4f} ({change_pct:.2f}%)"
                else:
                    price_change = f" ➡️ 无变化"
            
            # 状态标识
            status = "✅ 已完结" if str(confirm) == '1' else "🔄 进行中"
            
            # 显示K线信息
            print(f"📊 [{time_str}] {inst_id} ({timeframe}) OHLC: {o}/{h}/{l}/{c} Vol: {vol} {status}{price_change}")
            
            # 显示简单图表
            if SHOW_CHART and str(confirm) == '1':
                show_simple_chart(key, kline_info)
        else:
            print(f"[WARNING] K线数据格式不正确: {kline}")

def show_simple_chart(key: str, kline: Dict[str, Any]):
    """
    显示简单的ASCII K线图表
    
    Args:
        key: K线数据键
        kline: K线数据
    """
    try:
        o, h, l, c = kline['open'], kline['high'], kline['low'], kline['close']
        
        # 简单的涨跌标识
        if c > o:
            candle = "🟢"  # 阳线
        elif c < o:
            candle = "🔴"  # 阴线
        else:
            candle = "⚪"  # 十字线
        
        print(f"    📈 {candle} O:{o} H:{h} L:{l} C:{c} V:{kline['volume']}")
        
    except Exception as e:
        print(f"[ERROR] 图表显示错误: {e}")

# ==================== 信号处理 ====================

def signal_handler(signum, frame):
    """
    处理停止信号
    
    Args:
        signum: 信号编号
        frame: 当前栈帧
    """
    global running
    print(f"\n[SIGNAL] 收到停止信号 {signum}，正在关闭连接...")
    running = False

# 注册信号处理器
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ==================== 主要功能函数 ====================

async def subscribe_normal_klines(inst_ids: List[str], timeframes: List[str]):
    """
    订阅普通K线
    
    Args:
        inst_ids: 产品ID列表
        timeframes: 时间周期列表
    """
    global ws_client
    
    try:
        # 获取WebSocket业务频道地址
        ws_url = get_business_url()
        
        # 创建WebSocket连接
        print(f"🔗 正在连接到业务频道 {ws_url}...")
        ws_client = WsPublicAsync(url=ws_url)
        await ws_client.start()
        print("✅ WebSocket连接成功")
        
        # 构建订阅参数
        args = []
        for inst_id in inst_ids:
            for timeframe in timeframes:
                args.append({
                    "channel": f"candle{timeframe}",
                    "instId": inst_id
                })
        
        print(f"📡 正在订阅 {len(inst_ids)} 个产品 x {len(timeframes)} 个时间周期 = {len(args)} 个频道...")
        for inst_id in inst_ids:
            print(f"  📈 {inst_id}: {', '.join(timeframes)}")
        
        # 订阅K线频道
        await ws_client.subscribe(args, callback=kline_callback)
        
        print("\n🎯 订阅成功！开始接收普通K线数据...")
        print("💡 提示：推送频率最快间隔1秒推送一次数据")
        print("📊 K线状态：0=未完结，1=已完结")
        print("⏹️  按 Ctrl+C 停止订阅\n")
        print("=" * 100)
        
        # 保持连接
        while running:
            await asyncio.sleep(1)
        
        # 取消订阅
        print("\n📤 正在取消订阅...")
        await ws_client.unsubscribe(args, callback=kline_callback)
        await asyncio.sleep(2)
        
    except Exception as e:
        print(f"❌ [ERROR] 订阅过程中发生异常: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # 关闭连接
        if ws_client:
            print("🔌 正在关闭WebSocket连接...")
        
        print("👋 程序已退出")

async def show_kline_summary():
    """
    定期显示K线汇总信息
    """
    while running:
        await asyncio.sleep(60)  # 每60秒显示一次汇总
        
        if kline_stats:
            print("\n" + "=" * 100)
            print("📊 K线数据汇总信息")
            print("=" * 100)
            
            for key, stats in kline_stats.items():
                inst_id, timeframe = key.split('_', 1)
                total = stats['total_count']
                completed = stats['completed_count']
                last_update = stats['last_update']
                completion_rate = (completed / total * 100) if total > 0 else 0
                
                print(f"{inst_id:12} {timeframe:6} | 总计: {total:>4} | 完结: {completed:>4} ({completion_rate:>5.1f}%) | 最新: {last_update}")
            
            print("=" * 100 + "\n")

# ==================== 主函数 ====================

async def main():
    """
    主函数
    """
    print("🚀 OKX普通K线WebSocket订阅示例")
    print("=" * 100)
    
    # 显示WebSocket配置
    print_ws_config()
    
    print(f"📈 订阅产品: {', '.join(INST_IDS)}")
    print(f"⏰ 时间周期: {', '.join(TIMEFRAMES)}")
    print(f"🔍 详细日志: {'开启' if VERBOSE else '关闭'}")
    print(f"📊 显示图表: {'开启' if SHOW_CHART else '关闭'}")
    print("=" * 100)
    
    # 创建任务
    tasks = [
        asyncio.create_task(subscribe_normal_klines(INST_IDS, TIMEFRAMES)),
        asyncio.create_task(show_kline_summary())
    ]
    
    # 等待任务完成
    try:
        await asyncio.gather(*tasks)
    except Exception as e:
        print(f"❌ [ERROR] 主程序异常: {e}")

if __name__ == "__main__":
    print("\n💡 使用说明:")
    print("1. 修改 INST_IDS 列表来订阅不同的产品")
    print("2. 修改 TIMEFRAMES 列表来订阅不同的时间周期")
    print("3. 设置 VERBOSE = False 来减少日志输出")
    print("4. 设置 SHOW_CHART = True 来显示简单图表")
    print("5. 按 Ctrl+C 优雅退出程序")
    print("\n🎯 开始运行...\n")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 程序被用户中断")
    except Exception as e:
        print(f"\n❌ 程序运行异常: {e}")
        import traceback
        traceback.print_exc()