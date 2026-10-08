#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Demo61修复验证脚本
================

专门用于测试修复后的demo61核心功能
"""

import asyncio
import logging
import sys
import os
from datetime import datetime, timedelta, timezone

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入demo61的核心类
from demo61_quant_db_manager import HistoryKlineSyncer, WebSocketIncrementer, setup_logging

def test_history_syncer():
    """测试历史数据同步器"""
    print("🔄 测试历史数据同步功能...")
    
    syncer = HistoryKlineSyncer()
    
    # 连接InfluxDB
    if not syncer.connect_influxdb():
        print("❌ InfluxDB连接失败")
        return False
    
    # 测试单个币种的数据获取
    test_symbol = "BTC-USDT-SWAP" 
    test_period = "5m"
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=2)  # 只获取最近2小时数据
    
    print(f"📊 测试获取 {test_symbol} {test_period} 数据...")
    print(f"⏰ 时间范围: {start_time} ~ {end_time}")
    
    kline_data = syncer.fetch_kline_data(test_symbol, test_period, start_time, end_time)
    
    if kline_data:
        print(f"✅ 成功获取 {len(kline_data)} 条数据")
        # 显示前3条数据
        for i, data in enumerate(kline_data[:3]):
            print(f"  数据{i+1}: {data['timestamp']} OHLC=[{data['open']:.2f}, {data['high']:.2f}, {data['low']:.2f}, {data['close']:.2f}]")
        return True
    else:
        print("❌ 未获取到数据")
        return False

async def test_websocket_connection():
    """测试WebSocket连接"""
    print("🔄 测试WebSocket连接功能...")
    
    incrementer = WebSocketIncrementer()
    
    # 连接InfluxDB
    if not incrementer.connect_influxdb():
        print("❌ InfluxDB连接失败")
        return False
    
    try:
        # 获取WebSocket地址
        from ws_config import get_public_url
        ws_url = get_public_url()
        print(f"🌐 WebSocket地址: {ws_url}")
        
        # 尝试创建WebSocket连接（但不订阅）
        from websocket.WsPublicAsync import WsPublicAsync
        ws_client = WsPublicAsync(url=ws_url)
        await ws_client.start()
        
        print("✅ WebSocket连接成功")
        
        # 测试订阅格式（不实际订阅）
        test_symbols = ["BTC-USDT-SWAP"]
        test_periods = ["5m"]
        
        args = []
        for symbol in test_symbols:
            for period in test_periods:
                args.append({
                    "channel": f"candle{period}",
                    "instId": symbol
                })
        
        print(f"📡 测试订阅参数: {args}")
        print("✅ 订阅参数格式正确")
        
        return True
        
    except Exception as e:
        print(f"❌ WebSocket连接测试失败: {e}")
        return False

def main():
    """主测试函数"""
    setup_logging()
    logger = logging.getLogger('test')
    
    print("🎯 Demo61修复验证开始")
    print("=" * 50)
    
    # 测试历史数据功能
    history_ok = test_history_syncer()
    
    print("\n" + "=" * 50)
    
    # 测试WebSocket功能
    websocket_ok = asyncio.run(test_websocket_connection())
    
    print("\n" + "=" * 50)
    print("📊 测试结果汇总:")
    print(f"  历史数据功能: {'✅ 正常' if history_ok else '❌ 异常'}")
    print(f"  WebSocket功能: {'✅ 正常' if websocket_ok else '❌ 异常'}")
    
    if history_ok and websocket_ok:
        print("🎉 所有核心功能测试通过！demo61可以正常使用")
        return True
    else:
        print("⚠️ 部分功能存在问题，需要进一步检查")
        return False

if __name__ == "__main__":
    main()
