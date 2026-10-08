#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 18 - 获取未成交订单列表
====================================

功能说明：
- 查询账户当前的未成交订单
- 支持多种筛选条件（产品类型、订单类型等）
- 提供订单详细信息和统计分析

订单状态说明：
- live: 等待成交
- partially_filled: 部分成交

产品类型：
- SPOT: 币币交易
- MARGIN: 币币杠杆
- SWAP: 永续合约
- FUTURES: 交割合约
- OPTION: 期权

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-get-order-list

作者：OKX API Demo
创建时间：2025-01-26
版本：v1.0
"""

import okx.Trade as Trade
import json
import datetime

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
print("📋 OKX未成交订单查询工具")
print("=" * 80)
print_config_info(config)
print("=" * 80)

# =============================================================================
# 查询参数配置
# =============================================================================

# 查询条件配置
INST_TYPE = "SWAP"                     # 产品类型：永续合约
ORDER_TYPE = "limit"                   # 订单类型：限价单

print(f"🎯 查询配置:")
print(f"   产品类型: {INST_TYPE}")
print(f"   订单类型: {ORDER_TYPE}")
print(f"   查询范围: 所有未成交订单")
print("=" * 80)

# =============================================================================
# 初始化交易API
# =============================================================================

try:
    # 创建交易API实例
    tradeAPI = Trade.TradeAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 交易API初始化成功")
    
except Exception as e:
    print(f"❌ 交易API初始化失败: {e}")
    exit(1)

# =============================================================================
# 获取未成交订单列表
# =============================================================================

def safe_float(value, default=0.0):
    """安全地将字符串转换为浮点数"""
    try:
        if value is None or value == '' or value == 'None':
            return default
        return float(value)
    except (ValueError, TypeError):
        return default

try:
    print(f"\n🔍 正在查询未成交订单列表...")
    
    # 调用get_order_list方法获取未成交订单
    # 参数说明：
    # - instType: 产品类型（SPOT, MARGIN, SWAP, FUTURES, OPTION）
    # - uly: 标的指数
    # - instFamily: 交易品种
    # - instId: 产品ID
    # - ordType: 订单类型（market, limit, post_only, fok, ioc等）
    # - state: 订单状态（live, partially_filled）
    # - after: 请求此ID之前的分页内容
    # - before: 请求此ID之后的分页内容
    # - limit: 返回结果的数量，最大100，默认100
    result = tradeAPI.get_order_list(
        instType=INST_TYPE,
        ordType=ORDER_TYPE
    )
    
    print("✅ 未成交订单查询成功！")
    
    # 处理返回结果
    if result.get('code') == '0' and result.get('data') is not None:
        orders = result.get('data', [])
        
        print(f"\n📊 未成交订单概览")
        print("=" * 80)
        print(f"📈 订单总数: {len(orders)} 个")
        
        if orders:
            # 统计分析
            products = {}
            sides_count = {'buy': 0, 'sell': 0}
            pos_sides_count = {'long': 0, 'short': 0, 'net': 0}
            total_order_value = 0
            
            for order in orders:
                # 产品统计
                inst_id = order.get('instId', 'Unknown')
                products[inst_id] = products.get(inst_id, 0) + 1
                
                # 买卖方向统计
                side = order.get('side', '')
                if side in sides_count:
                    sides_count[side] += 1
                
                # 持仓方向统计
                pos_side = order.get('posSide', '')
                if pos_side in pos_sides_count:
                    pos_sides_count[pos_side] += 1
                
                # 计算订单价值
                px = safe_float(order.get('px', '0'))
                sz = safe_float(order.get('sz', '0'))
                if px > 0 and sz > 0:
                    total_order_value += px * sz
            
            # 显示统计信息
            print(f"\n📊 产品分布:")
            for product, count in sorted(products.items()):
                print(f"   {product}: {count} 个订单")
            
            print(f"\n🔄 方向统计:")
            print(f"   🟢 买入订单: {sides_count['buy']} 个")
            print(f"   🔴 卖出订单: {sides_count['sell']} 个")
            
            if any(pos_sides_count.values()):
                print(f"\n📈 持仓方向:")
                if pos_sides_count['long'] > 0:
                    print(f"   🟢 做多: {pos_sides_count['long']} 个")
                if pos_sides_count['short'] > 0:
                    print(f"   🔴 做空: {pos_sides_count['short']} 个")
                if pos_sides_count['net'] > 0:
                    print(f"   ⚪ 净持仓: {pos_sides_count['net']} 个")
            
            if total_order_value > 0:
                print(f"\n💰 订单总价值: {total_order_value:,.2f} USDT")
            
            # 显示订单详情列表
            print(f"\n📋 未成交订单详情:")
            print("=" * 80)
            print(f"{'序号':<4} {'产品ID':<16} {'订单ID':<18} {'方向':<8} {'类型':<10} {'委托价':<12} {'委托量':<10} {'已成交':<10} {'状态':<12} {'创建时间':<20}")
            print("-" * 80)
            
            for i, order in enumerate(orders, 1):
                inst_id = order.get('instId', '')
                ord_id = order.get('ordId', '')
                cl_ord_id = order.get('clOrdId', '')
                side = order.get('side', '')
                ord_type = order.get('ordType', '')
                px = order.get('px', '0')
                sz = order.get('sz', '0')
                acc_fill_sz = order.get('accFillSz', '0')
                state = order.get('state', '')
                c_time = order.get('cTime', '0')
                
                # 格式化显示
                ord_id_display = ord_id[:16] + '..' if len(ord_id) > 16 else ord_id
                
                # 方向显示
                side_display = {
                    'buy': '🟢买入',
                    'sell': '🔴卖出'
                }.get(side, side)
                
                # 状态显示
                state_display = {
                    'live': '🔄挂单中',
                    'partially_filled': '🟡部分成交'
                }.get(state, state)
                
                # 时间格式化
                try:
                    c_time_int = int(c_time)
                    if c_time_int > 0:
                        create_time = datetime.datetime.fromtimestamp(c_time_int / 1000)
                        time_str = create_time.strftime('%m-%d %H:%M:%S')
                    else:
                        time_str = "N/A"
                except (ValueError, OSError):
                    time_str = "时间解析失败"
                
                # 价格和数量格式化
                try:
                    px_float = float(px)
                    sz_float = float(sz)
                    acc_fill_sz_float = float(acc_fill_sz)
                    
                    px_display = f"${px_float:.4f}" if px_float > 0 else "-"
                    sz_display = f"{sz_float:.0f}" if sz_float > 0 else "0"
                    fill_display = f"{acc_fill_sz_float:.0f}" if acc_fill_sz_float > 0 else "0"
                except ValueError:
                    px_display = px
                    sz_display = sz
                    fill_display = acc_fill_sz
                
                print(f"{i:<4} {inst_id:<16} {ord_id_display:<18} {side_display:<8} {ord_type:<10} {px_display:<12} {sz_display:<10} {fill_display:<10} {state_display:<12} {time_str:<20}")
            
            # 显示第一个订单的详细信息
            if orders:
                first_order = orders[0]
                print(f"\n🔍 订单详细信息 (第1个订单):")
                print("=" * 80)
                
                key_mappings = {
                    'instId': '产品ID',
                    'ordId': '订单ID',
                    'clOrdId': '客户订单ID',
                    'side': '买卖方向',
                    'posSide': '持仓方向',
                    'ordType': '订单类型',
                    'px': '委托价格',
                    'sz': '委托数量',
                    'accFillSz': '已成交数量',
                    'avgPx': '成交均价',
                    'state': '订单状态',
                    'lever': '杠杆倍数',
                    'cTime': '创建时间',
                    'uTime': '更新时间',
                    'tdMode': '交易模式',
                    'ccy': '保证金币种'
                }
                
                for key, value in first_order.items():
                    display_key = key_mappings.get(key, key)
                    
                    # 特殊处理时间戳
                    if key in ['cTime', 'uTime']:
                        try:
                            ts_val = int(value)
                            if ts_val > 0:
                                readable_time = datetime.datetime.fromtimestamp(ts_val / 1000)
                                value = f"{value} ({readable_time.strftime('%Y-%m-%d %H:%M:%S')})"
                        except (ValueError, OSError):
                            pass
                    
                    print(f"   {display_key}: {value}")
                
                # 计算成交进度
                px_val = safe_float(first_order.get('px', '0'))
                sz_val = safe_float(first_order.get('sz', '0'))
                acc_fill_sz_val = safe_float(first_order.get('accFillSz', '0'))
                
                if sz_val > 0:
                    fill_progress = (acc_fill_sz_val / sz_val) * 100
                    print(f"\n📊 成交进度: {fill_progress:.1f}% ({acc_fill_sz_val:.0f}/{sz_val:.0f})")
                
                if px_val > 0 and acc_fill_sz_val > 0:
                    filled_value = px_val * acc_fill_sz_val
                    print(f"💰 已成交价值: {filled_value:,.2f} USDT")
        
        else:
            print("📝 当前没有未成交订单")
            print("\n💡 可能情况:")
            print("   - 所有订单都已成交或被撤销")
            print("   - 指定的筛选条件下无订单")
            print("   - 可以尝试其他产品类型或订单类型")
        
    else:
        print(f"❌ 查询未成交订单失败: {result.get('msg', '未知错误')}")
        
        # 常见错误处理建议
        error_code = result.get('code', '')
        if error_code == '50001':
            print("💡 可能原因: API权限不足，请检查API Key权限设置")
        elif error_code == '50004':
            print("💡 可能原因: 请求过于频繁，请稍后重试")

except Exception as e:
    print(f"❌ 查询未成交订单时发生错误: {e}")

# =============================================================================
# 显示原始数据（调试用，仅显示前2条）
# =============================================================================

print(f"\n" + "=" * 80)
print("🔧 调试信息 - 原始返回数据示例")
print("=" * 80)

try:
    if result.get('data') and len(result['data']) > 0:
        sample_data = {
            'code': result.get('code'),
            'msg': result.get('msg'),
            'data': result['data'][:2]  # 只显示前2条数据
        }
        print(json.dumps(sample_data, indent=2, ensure_ascii=False))
        if len(result['data']) > 2:
            print(f"... 还有 {len(result['data']) - 2} 个订单未显示")
    else:
        print("无订单数据")
        print("原始返回数据:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
except:
    print("原始数据显示失败")

# =============================================================================
# 后续操作建议
# =============================================================================

print(f"\n" + "=" * 80)
print("🔧 后续操作建议")
print("=" * 80)

if result.get('data') and len(result['data']) > 0:
    print(f"1️⃣  订单管理:")
    print(f"   - 可使用 demo05_订单修改.py 修改订单价格或数量")
    print(f"   - 可使用 demo06_订单撤销.py 撤销不需要的订单")
    
    print(f"\n2️⃣  订单监控:")
    print(f"   - 定期检查订单成交情况")
    print(f"   - 关注市场价格变化，及时调整策略")
    
    print(f"\n3️⃣  风险控制:")
    print(f"   - 避免同时挂单过多，占用过多资金")
    print(f"   - 设置合理的委托价格")

# =============================================================================
# 使用建议
# =============================================================================

print(f"\n💡 使用建议:")
print(f"   - 定期检查未成交订单，避免资金长期占用")
print(f"   - 根据市场情况调整委托价格")
print(f"   - 合理控制同时挂单数量")
print(f"   - 关注部分成交订单的进度")

# =============================================================================
# 风险提醒
# =============================================================================

print(f"\n⚠️  风险提醒:")
print(f"   - 未成交订单占用账户资金，影响资金利用率")
print(f"   - 市场波动可能导致订单无法按预期成交")
print(f"   - 建议设置合理的订单有效期")
print(f"   - 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo05_订单修改.py 或 demo06_订单撤销.py 管理订单")

