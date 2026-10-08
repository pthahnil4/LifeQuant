#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 39 - 查询并撤销策略委托订单
==========================================

功能说明：
- 查询未完成的策略委托订单（order-algos-list）
- 撤销策略委托订单（cancel-algos）
- 支持交互式选择要撤销的订单1
- 支持批量撤销，每次最多可以撤销10个策略委托单
- 支持止盈止损、计划委托、TWAP等类型的策略撤单
- 提供完整的撤销结果处理和状态检查

使用场景：
- 查看当前所有未完成的策略委托订单
- 市场条件变化，需要撤销未执行的策略订单
- 选择性撤销特定的策略委托订单
- 批量管理多个策略委托订单
- 风险控制，及时止损或调整策略
- 策略优化，撤销旧策略后下新策略

核心功能：
- 自动查询所有类型的未完成策略委托订单
- 表格化显示订单详细信息
- 交互式订单选择（单选/多选/全选）
- 支持多种策略类型撤销
- 批量操作提高效率
- 实时返回撤销状态
- 详细的错误信息反馈

核心参数：
- algoId: 策略委托单ID（从查询结果自动获取）
- instId: 产品ID（从查询结果自动获取）

API限制：
- 限速：20次/2s
- 限速规则（期权以外）：User ID + Instrument ID
- 限速规则（只限期权）：User ID + Instrument Family
- 权限：交易

API文档：
- 查询：https://www.okx.com/docs-v5/en/#rest-api-trade-get-algo-order-list
- 撤销：https://www.okx.com/docs-v5/en/#rest-api-trade-cancel-algo-orders

作者：OKX API Demo
创建时间：2025-01-20
版本：v2.0 - 新增查询功能
"""

import okx.Trade as Trade
import okx.MarketData as MarketData
import datetime
import json
import time
import random

# =============================================================================
# API 配置区域
# =============================================================================

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api_config import get_api_config, validate_config, print_config_info

config = get_api_config()
is_valid, message = validate_config(config)
if not is_valid:
    print(f"❌ API配置错误: {message}")
    print("💡 请检查 api_config.py 文件中的配置")
    exit(1)

apikey = config['api_key']
secretkey = config['secret_key']
passphrase = config['passphrase']
flag = config['flag']

print("=" * 60)
print("🗑️ OKX策略委托订单撤销工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 查询和撤销配置
# =============================================================================

# 操作模式配置
OPERATION_MODE = "demo"  # demo=演示模式, real=实际操作
MAX_CANCEL_COUNT = 10    # 每次最多撤销订单数量
AUTO_SELECT_ALL = False  # 是否自动选择所有订单进行撤销

# 支持的订单类型
SUPPORTED_ORDER_TYPES = [
    "conditional",    # 单向止盈止损
    "oco",           # 双向止盈止损
    "trigger",       # 计划委托
    "move_order_stop", # 移动止盈止损
    "twap",          # 时间加权委托
    "chase"          # 追逐限价委托
]

print(f"🎯 操作配置:")
print(f"   操作模式: {OPERATION_MODE}")
print(f"   最大撤销数量: {MAX_CANCEL_COUNT}")
print(f"   自动全选: {AUTO_SELECT_ALL}")
print("=" * 60)

# =============================================================================
# 初始化API
# =============================================================================

try:
    tradeAPI = Trade.TradeAPI(apikey, secretkey, passphrase, False, flag)
    marketDataAPI = MarketData.MarketAPI(flag=flag)
    print("✅ 交易API初始化成功")
except Exception as e:
    print(f"❌ 交易API初始化失败: {e}")
    exit(1)

# =============================================================================
# 查询未完成策略委托订单
# =============================================================================

def query_pending_algo_orders():
    """
    查询未完成的策略委托订单列表
    
    Returns:
        list: 未完成的策略委托订单列表
    """
    try:
        print(f"\n🔍 正在查询未完成的策略委托订单...")
        
        all_orders = []
        
        # 查询不同类型的策略订单
        for order_type in SUPPORTED_ORDER_TYPES:
            try:
                print(f"   查询 {order_type} 类型订单...")
                result = tradeAPI.order_algos_list(ordType=order_type)
                
                if result and result.get('code') == '0':
                    orders = result.get('data', [])
                    if orders:
                        print(f"   找到 {len(orders)} 个 {order_type} 订单")
                        all_orders.extend(orders)
                    else:
                        print(f"   未找到 {order_type} 订单")
                else:
                    print(f"   查询 {order_type} 订单失败: {result.get('msg', '未知错误') if result else '无响应'}")
                    
                # 避免API限速
                time.sleep(0.1)
                
            except Exception as e:
                print(f"   查询 {order_type} 订单时发生错误: {e}")
                continue
        
        print(f"\n📊 查询完成，共找到 {len(all_orders)} 个未完成的策略委托订单")
        return all_orders
        
    except Exception as e:
        print(f"❌ 查询策略委托订单时发生错误: {e}")
        return []

def format_order_info(order):
    """
    格式化订单信息显示
    
    Args:
        order: 订单数据
    
    Returns:
        dict: 格式化的订单信息
    """
    order_type_map = {
        "conditional": "单向止盈止损",
        "oco": "双向止盈止损", 
        "trigger": "计划委托",
        "move_order_stop": "移动止盈止损",
        "twap": "时间加权委托",
        "chase": "追逐限价委托"
    }
    
    side_map = {
        "buy": "买入",
        "sell": "卖出"
    }
    
    state_map = {
        "live": "待生效",
        "pause": "暂停生效"
    }
    
    return {
        "algoId": order.get('algoId', ''),
        "instId": order.get('instId', ''),
        "ordType": order.get('ordType', ''),
        "ordTypeDesc": order_type_map.get(order.get('ordType', ''), order.get('ordType', '')),
        "side": order.get('side', ''),
        "sideDesc": side_map.get(order.get('side', ''), order.get('side', '')),
        "sz": order.get('sz', ''),
        "state": order.get('state', ''),
        "stateDesc": state_map.get(order.get('state', ''), order.get('state', '')),
        "cTime": order.get('cTime', ''),
        "tpTriggerPx": order.get('tpTriggerPx', ''),
        "slTriggerPx": order.get('slTriggerPx', ''),
        "triggerPx": order.get('triggerPx', ''),
        "last": order.get('last', '')
    }

def display_orders_table(orders):
    """
    以表格形式显示订单列表
    
    Args:
        orders: 订单列表
    """
    if not orders:
        print("\n📋 未找到任何未完成的策略委托订单")
        return
    
    print(f"\n📋 未完成策略委托订单列表 (共 {len(orders)} 个):")
    print("=" * 120)
    print(f"{'序号':>4} {'订单ID':>20} {'交易对':>15} {'类型':>12} {'方向':>6} {'数量':>12} {'状态':>8} {'创建时间':>19}")
    print("-" * 120)
    
    for i, order in enumerate(orders, 1):
        info = format_order_info(order)
        
        # 格式化创建时间
        try:
            if info['cTime']:
                timestamp = int(info['cTime']) / 1000
                create_time = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
            else:
                create_time = '未知'
        except:
            create_time = '未知'
        
        print(f"{i:>4} {info['algoId']:>20} {info['instId']:>15} {info['ordTypeDesc']:>12} "
              f"{info['sideDesc']:>6} {info['sz']:>12} {info['stateDesc']:>8} {create_time:>19}")
    
    print("-" * 120)

# 查询未完成的策略委托订单
pending_orders = query_pending_algo_orders()

if not pending_orders:
    print("\n❌ 未找到任何未完成的策略委托订单，程序退出")
    exit(0)

# 显示订单列表
display_orders_table(pending_orders)

# =============================================================================
# 用户选择要撤销的订单
# =============================================================================

def select_orders_to_cancel(orders):
    """
    让用户选择要撤销的订单
    
    Args:
        orders: 可选择的订单列表
    
    Returns:
        list: 用户选择的订单列表
    """
    if AUTO_SELECT_ALL:
        print(f"\n🔄 自动选择模式：将撤销所有 {len(orders)} 个订单")
        return orders[:MAX_CANCEL_COUNT]
    
    print(f"\n🎯 请选择要撤销的订单:")
    print(f"   输入格式说明:")
    print(f"   - 单个订单: 输入序号，如 '1'")
    print(f"   - 多个订单: 用逗号分隔，如 '1,3,5'")
    print(f"   - 连续订单: 用横线连接，如 '1-5'")
    print(f"   - 全部订单: 输入 'all' 或 'a'")
    print(f"   - 退出程序: 输入 'quit' 或 'q'")
    print(f"   - 最多可选择 {MAX_CANCEL_COUNT} 个订单")
    
    while True:
        try:
            user_input = input(f"\n请输入选择 (1-{len(orders)}): ").strip().lower()
            
            if user_input in ['quit', 'q', 'exit']:
                print("👋 用户取消操作，程序退出")
                exit(0)
            
            if user_input in ['all', 'a']:
                selected_orders = orders[:MAX_CANCEL_COUNT]
                print(f"✅ 已选择所有订单 (共 {len(selected_orders)} 个)")
                return selected_orders
            
            # 解析用户输入
            selected_indices = set()
            
            for part in user_input.split(','):
                part = part.strip()
                if '-' in part:
                    # 处理范围选择，如 "1-5"
                    try:
                        start, end = map(int, part.split('-'))
                        if start < 1 or end > len(orders) or start > end:
                            raise ValueError("范围无效")
                        selected_indices.update(range(start, end + 1))
                    except ValueError:
                        print(f"❌ 无效的范围格式: {part}")
                        continue
                else:
                    # 处理单个选择
                    try:
                        index = int(part)
                        if 1 <= index <= len(orders):
                            selected_indices.add(index)
                        else:
                            print(f"❌ 序号超出范围: {index}")
                            continue
                    except ValueError:
                        print(f"❌ 无效的序号: {part}")
                        continue
            
            if not selected_indices:
                print("❌ 未选择任何有效订单，请重新输入")
                continue
            
            if len(selected_indices) > MAX_CANCEL_COUNT:
                print(f"⚠️  选择的订单数量({len(selected_indices)})超过限制({MAX_CANCEL_COUNT})")
                print(f"   将只处理前{MAX_CANCEL_COUNT}个订单")
                selected_indices = sorted(list(selected_indices))[:MAX_CANCEL_COUNT]
            
            # 获取选中的订单
            selected_orders = [orders[i-1] for i in sorted(selected_indices)]
            
            # 显示选择的订单
            print(f"\n✅ 已选择 {len(selected_orders)} 个订单:")
            print("-" * 80)
            for i, order in enumerate(selected_orders, 1):
                info = format_order_info(order)
                print(f"{i:2d}. {info['instId']:15s} | {info['algoId']:20s} | {info['ordTypeDesc']}")
            print("-" * 80)
            
            # 确认选择
            confirm = input(f"\n确认撤销以上订单吗？(y/n): ").strip().lower()
            if confirm in ['y', 'yes', '是', '确认']:
                return selected_orders
            else:
                print("❌ 用户取消选择，请重新选择")
                continue
                
        except KeyboardInterrupt:
            print("\n👋 用户中断操作，程序退出")
            exit(0)
        except Exception as e:
            print(f"❌ 输入处理错误: {e}，请重新输入")
            continue

# 用户选择要撤销的订单
selected_orders = select_orders_to_cancel(pending_orders)

print(f"\n📋 最终选择撤销 {len(selected_orders)} 个订单")

# =============================================================================
# 撤销策略委托订单
# =============================================================================

def cancel_algo_orders(orders_to_cancel, mode="demo"):
    """
    撤销策略委托订单
    
    Args:
        orders_to_cancel: 待撤销订单列表
        mode: 操作模式 (demo/real)
    
    Returns:
        dict: 撤销结果
    """
    try:
        print(f"\n🚀 开始撤销策略委托订单...")
        print(f"   撤销模式: {mode}")
        print(f"   订单数量: {len(orders_to_cancel)}")
        
        # 构建撤销请求参数
        cancel_params = []
        for order in orders_to_cancel:
            cancel_params.append({
                "instId": order.get("instId", ""),
                "algoId": order.get("algoId", "")
            })
        
        print(f"\n📤 撤销请求参数:")
        print(json.dumps(cancel_params, indent=2, ensure_ascii=False))
        
        if mode == "demo":
            print(f"\n⚠️  演示模式: 不会实际发送撤销请求")
            # 模拟返回结果
            result = {
                "code": "0",
                "data": [
                    {
                        "algoClOrdId": "",
                        "algoId": order.get("algoId", ""),
                        "clOrdId": "",
                        "sCode": "0",
                        "sMsg": "",
                        "tag": ""
                    } for order in orders_to_cancel
                ],
                "msg": ""
            }
        else:
            # 实际API调用
            result = tradeAPI.cancel_algo_order(cancel_params)
        
        return result
        
    except Exception as e:
        print(f"❌ 撤销策略委托订单时发生错误: {e}")
        return None

# 执行撤销操作
cancel_result = cancel_algo_orders(selected_orders, OPERATION_MODE)

if cancel_result:
    print(f"\n📋 撤销操作返回数据:")
    print(json.dumps(cancel_result, indent=2, ensure_ascii=False))
else:
    print(f"❌ 撤销操作失败")
    exit(1)

# =============================================================================
# 解析撤销结果
# =============================================================================

try:
    code = cancel_result.get('code', '')
    
    if code == '0':
        cancel_data = cancel_result.get('data', [])
        
        print("\n" + "=" * 60)
        print("📊 策略订单撤销结果详情")
        print("=" * 60)
        
        success_count = 0
        failed_count = 0
        
        for i, item in enumerate(cancel_data):
            order_info = selected_orders[i] if i < len(selected_orders) else {}
            
            algoId = item.get('algoId', '')
            algoClOrdId = item.get('algoClOrdId', '')
            sCode = item.get('sCode', '')
            sMsg = item.get('sMsg', '')
            clOrdId = item.get('clOrdId', '')
            tag = item.get('tag', '')
            
            print(f"\n📋 订单 {i+1}:")
            print(f"   🆔 策略订单ID:     {algoId}")
            print(f"   🏷️  客户策略ID:     {algoClOrdId or '无'}")
            print(f"   📊 撤销状态码:     {sCode}")
            print(f"   💬 状态信息:       {sMsg or '成功'}")
            print(f"   🔗 关联订单ID:     {clOrdId or '无'}")
            print(f"   🏷️  标签:          {tag or '无'}")
            
            if order_info:
                formatted_info = format_order_info(order_info)
                print(f"   📈 交易对:         {formatted_info['instId']}")
                print(f"   📝 订单类型:       {formatted_info['ordTypeDesc']}")
                print(f"   📊 订单方向:       {formatted_info['sideDesc']}")
                print(f"   💰 订单数量:       {formatted_info['sz']}")
            
            if sCode == '0':
                print(f"   ✅ 撤销状态:       成功")
                success_count += 1
            else:
                print(f"   ❌ 撤销状态:       失败 - {sMsg}")
                failed_count += 1
        
        # 撤销统计
        print("\n" + "=" * 60)
        print("📈 撤销操作统计")
        print("=" * 60)
        print(f"✅ 撤销成功: {success_count} 个订单")
        print(f"❌ 撤销失败: {failed_count} 个订单")
        print(f"📊 总计处理: {len(cancel_data)} 个订单")
        print(f"🕐 操作时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        if success_count > 0:
            print(f"\n🎉 策略订单撤销操作完成！")
            print(f"   成功撤销了 {success_count} 个策略委托订单")
            
            if OPERATION_MODE == "demo":
                print(f"\n💡 演示模式说明:")
                print(f"   - 以上结果为模拟数据")
                print(f"   - 实际使用时请将 OPERATION_MODE 改为 'real'")
                print(f"   - 请确保 algoId 为真实有效的策略订单ID")
        
        if failed_count > 0:
            print(f"\n⚠️  部分订单撤销失败:")
            print(f"   - 请检查订单ID是否正确")
            print(f"   - 确认订单是否仍然有效")
            print(f"   - 检查账户权限和余额")
    
    else:
        print(f"\n❌ 撤销请求失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {cancel_result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 解析撤销结果时发生错误: {e}")
    print(f"原始结果: {cancel_result}")

# =============================================================================
# 撤销策略优化指南
# =============================================================================

print(f"\n📚 策略撤销优化指南:")

print(f"\n🔧 撤销时机建议:")
print(f"   最佳撤销时机:")
print(f"   - 市场趋势发生重大变化")
print(f"   - 策略参数需要调整")
print(f"   - 风险控制需要")
print(f"   - 资金需要重新配置")

print(f"\n   避免撤销时机:")
print(f"   - 策略即将触发执行")
print(f"   - 市场波动剧烈期间")
print(f"   - 临近重要数据发布")
print(f"   - 流动性不足时段")

print(f"\n📊 批量撤销策略:")
print(f"   分批处理:")
print(f"   - 每次最多10个订单")
print(f"   - 按重要性排序撤销")
print(f"   - 预留API调用频次")
print(f"   - 监控撤销结果")

print(f"\n   错误处理:")
print(f"   - 记录失败订单ID")
print(f"   - 分析失败原因")
print(f"   - 重试机制设计")
print(f"   - 手动处理备案")

print(f"\n💡 实战技巧:")
print(f"   撤销前检查:")
print(f"   - 确认订单状态")
print(f"   - 检查市场条件")
print(f"   - 评估撤销影响")
print(f"   - 准备替代策略")

print(f"\n   撤销后处理:")
print(f"   - 确认撤销成功")
print(f"   - 检查持仓变化")
print(f"   - 调整风险敞口")
print(f"   - 记录操作日志")

print(f"\n🎯 常见撤销场景:")
print(f"   止盈止损调整:")
print(f"   - 价格目标变化")
print(f"   - 风险承受调整")
print(f"   - 市场环境变化")

print(f"\n   TWAP策略撤销:")
print(f"   - 执行进度不理想")
print(f"   - 市场流动性变化")
print(f"   - 时间窗口调整")

print(f"\n   计划委托撤销:")
print(f"   - 触发条件变化")
print(f"   - 策略逻辑调整")
print(f"   - 风险控制需要")

print(f"\n⚠️  重要提醒:")
print(f"   - 撤销操作不可逆")
print(f"   - 部分成交订单无法撤销")
print(f"   - 注意API限速规则")
print(f"   - 建议先在模拟盘测试")
print(f"   - 保持充足的风险缓冲")

print(f"\n🔄 撤销后续操作建议:")
print(f"   1. 检查账户状态")
print(f"   2. 评估持仓风险")
print(f"   3. 调整策略参数")
print(f"   4. 重新下单或等待")
print(f"   5. 记录操作日志")

print("\n🎯 Demo运行完成！")