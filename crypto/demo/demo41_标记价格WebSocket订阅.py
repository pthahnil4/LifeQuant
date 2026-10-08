#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX标记价格WebSocket订阅示例
==============================

功能说明：
1. 连接OKX WebSocket公共频道
2. 订阅指定产品的标记价格数据
3. 实时接收标记价格变化推送
4. 支持多个产品同时订阅
5. 优雅的连接管理和错误处理

使用场景：
- 实时监控标记价格变化
- 价格预警和通知
- 交易策略中的价格参考
- 套利机会识别

特点：
- 标记价格有变化时，每200ms推送一次数据
- 标记价格没变化时，每10s推送一次数据
- 支持订阅和取消订阅
- 异步处理，不阻塞主程序

核心参数：
- instId: 产品ID，如 "BTC-USDT", "ETH-USDT"
- channel: 频道名，固定为 "mark-price"
- callback: 回调函数，处理接收到的数据

运行步骤：
1. 修改下方的产品ID列表
2. 运行脚本：python demo41_标记价格WebSocket订阅.py
3. 观察实时标记价格数据
4. 按Ctrl+C停止订阅

API文档：
https://www.okx.com/docs-v5/zh/#websocket-api-public-channel-mark-price-channel

作者：AI Assistant
创建时间：2025年1月17日
"""

import asyncio
import json
import datetime
import signal
import sys
import os
from typing import List, Dict, Any

# 添加父目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from websocket.WsPublicAsync import WsPublicAsync
from ws_config import get_public_url, get_connection_config, print_ws_config

# ==================== 配置参数 ====================

# 要订阅的产品ID列表
INST_IDS = [
    "BTC-USDT"
]

# 是否显示详细日志
VERBOSE = True

# ==================== 全局变量 ====================

# WebSocket连接实例
ws_client = None

# 运行状态
running = True

# 价格数据存储
price_data = {}

# ==================== 回调函数 ====================

def mark_price_callback(message: str):  # 注意：message 是字符串，不是字典
    """
    标记价格数据回调函数
    
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
            # 标记价格数据推送
            handle_price_data(data)
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
        inst_id = arg.get('instId', '未知')
        print(f"✅ [SUBSCRIBE] {inst_id} 订阅成功")
        
    elif event == 'unsubscribe':
        arg = message.get('arg', {})
        inst_id = arg.get('instId', '未知')
        print(f"❌ [UNSUBSCRIBE] {inst_id} 取消订阅成功")
        
    elif event == 'error':
        code = message.get('code', '未知')
        msg = message.get('msg', '未知错误')
        print(f"❌ [ERROR] 错误码: {code}, 消息: {msg}")
        
    else:
        print(f"[EVENT] {event}: {message}")

def handle_price_data(message: Dict[str, Any]):
    """
    处理标记价格数据
    
    Args:
        message: 价格数据消息
    """
    arg = message.get('arg', {})
    data_list = message.get('data', [])
    
    channel = arg.get('channel')
    inst_id = arg.get('instId')
    
    if channel != 'mark-price':
        return
    
    for data in data_list:
        inst_type = data.get('instType', '')
        product_id = data.get('instId', '')
        mark_price = data.get('markPx', '0')
        timestamp = data.get('ts', '0')
        
        # 转换时间戳
        try:
            dt = datetime.datetime.fromtimestamp(int(timestamp) / 1000)
            time_str = dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        except:
            time_str = timestamp
        
        # 存储价格数据
        if product_id not in price_data:
            price_data[product_id] = {
                'current_price': mark_price,
                'previous_price': None,
                'update_time': time_str,
                'update_count': 0
            }
        else:
            price_data[product_id]['previous_price'] = price_data[product_id]['current_price']
            price_data[product_id]['current_price'] = mark_price
            price_data[product_id]['update_time'] = time_str
            price_data[product_id]['update_count'] += 1
        
        # 计算价格变化
        price_change = ""
        if price_data[product_id]['previous_price']:
            try:
                current = float(mark_price)
                previous = float(price_data[product_id]['previous_price'])
                change = current - previous
                change_pct = (change / previous) * 100 if previous != 0 else 0
                
                if change > 0:
                    price_change = f" 📈 +{change:.4f} (+{change_pct:.4f}%)"
                elif change < 0:
                    price_change = f" 📉 {change:.4f} ({change_pct:.4f}%)"
                else:
                    price_change = f" ➡️ 无变化"
            except:
                price_change = ""
        
        # 显示价格信息
        update_count = price_data[product_id]['update_count']
        print(f"💰 [{time_str}] {product_id} ({inst_type}): {mark_price}{price_change} [第{update_count}次更新]")

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

async def subscribe_mark_price(inst_ids: List[str]):
    """
    订阅标记价格
    
    Args:
        inst_ids: 产品ID列表
    """
    global ws_client
    
    try:
        # 获取WebSocket地址
        ws_url = get_public_url()
        
        # 创建WebSocket连接
        print(f"🔗 正在连接到 {ws_url}...")
        ws_client = WsPublicAsync(url=ws_url)
        await ws_client.start()
        print("✅ WebSocket连接成功")
        
        # 构建订阅参数
        args = []
        for inst_id in inst_ids:
            args.append({
                "channel": "mark-price",
                "instId": inst_id
            })
        
        print(f"📡 正在订阅 {len(inst_ids)} 个产品的标记价格...")
        for inst_id in inst_ids:
            print(f"  - {inst_id}")
        
        # 订阅标记价格频道
        await ws_client.subscribe(args, callback=mark_price_callback)
        
        print("\n🎯 订阅成功！开始接收标记价格数据...")
        print("💡 提示：标记价格有变化时每200ms推送一次，无变化时每10s推送一次")
        print("⏹️  按 Ctrl+C 停止订阅\n")
        print("=" * 80)
        
        # 保持连接
        while running:
            await asyncio.sleep(1)
        
        # 取消订阅
        print("\n📤 正在取消订阅...")
        await ws_client.unsubscribe(args, callback=mark_price_callback)
        await asyncio.sleep(2)
        
    except Exception as e:
        print(f"❌ [ERROR] 订阅过程中发生异常: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # 关闭连接
        if ws_client:
            print("🔌 正在关闭WebSocket连接...")
            # 注意：WsPublicAsync可能没有close方法，这里只是示例
            # await ws_client.close()
        
        print("👋 程序已退出")

async def show_price_summary():
    """
    定期显示价格汇总信息
    """
    while running:
        await asyncio.sleep(30)  # 每30秒显示一次汇总
        
        if price_data:
            print("\n" + "=" * 60)
            print("📊 价格汇总信息")
            print("=" * 60)
            
            for inst_id, data in price_data.items():
                current_price = data['current_price']
                update_time = data['update_time']
                update_count = data['update_count']
                
                print(f"{inst_id:12} | 价格: {current_price:>12} | 更新: {update_count:>3}次 | 时间: {update_time}")
            
            print("=" * 60 + "\n")

# ==================== 主函数 ====================

async def main():
    """
    主函数
    """
    print("🚀 OKX标记价格WebSocket订阅示例")
    print("=" * 80)
    
    # 显示WebSocket配置
    print_ws_config()
    
    print(f"📈 订阅产品: {', '.join(INST_IDS)}")
    print(f"🔍 详细日志: {'开启' if VERBOSE else '关闭'}")
    print("=" * 80)
    
    # 创建任务
    tasks = [
        asyncio.create_task(subscribe_mark_price(INST_IDS)),
        asyncio.create_task(show_price_summary())
    ]
    
    # 等待任务完成
    try:
        await asyncio.gather(*tasks)
    except Exception as e:
        print(f"❌ [ERROR] 主程序异常: {e}")

if __name__ == "__main__":
    print("\n💡 使用说明:")
    print("1. 修改 INST_IDS 列表来订阅不同的产品")
    print("2. 设置 VERBOSE = False 来减少日志输出")
    print("3. 按 Ctrl+C 优雅退出程序")
    print("\n🎯 开始运行...\n")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 程序被用户中断")
    except Exception as e:
        print(f"\n❌ 程序运行异常: {e}")
        import traceback
        traceback.print_exc()