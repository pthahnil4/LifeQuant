#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Demo19: 查询永续合约历史订单
==========================

功能说明：
- 查询永续合约的历史订单信息
- 支持多种筛选条件（订单类型、状态、时间范围等）
- 包含完整的请求参数和返回参数说明

API接口：GET /api/v5/trade/orders-history
限频：40次/2s
限频规则：UserID

作者：OKX API Demo
创建时间：2025-06-27
版本：v1.0
"""

import okx.Trade as Trade
import json
from datetime import datetime, timedelta

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 导入API配置
try:
    from api_config import get_api_config, validate_config, print_config_info
except ImportError:
    print("❌ 无法导入api_config，请确保api_config.py文件存在")
    exit(1)


def safe_float(value, default=0.0):
    """
    安全地将字符串转换为浮点数
    
    参数:
        value: 要转换的值
        default: 转换失败时的默认值
        
    返回:
        float: 转换后的浮点数
    """
    try:
        if value is None or value == '' or value == 'None':
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value, default=0):
    """
    安全地将字符串转换为整数
    
    参数:
        value: 要转换的值
        default: 转换失败时的默认值
        
    返回:
        int: 转换后的整数
    """
    try:
        if value is None or value == '' or value == 'None':
            return default
        return int(float(value))
    except (ValueError, TypeError):
        return default


def query_swap_orders_history():
    """
    查询永续合约历史订单
    
    请求参数说明：
    ============
    instType        String  是    产品类型
                                  SPOT：币币
                                  MARGIN：币币杠杆
                                  SWAP：永续合约 ⭐
                                  FUTURES：交割合约
                                  OPTION：期权
    
    uly             String  否    标的指数
    instFamily      String  否    交易品种，适用于交割/永续/期权
    instId          String  否    产品ID，如BTC-USDT-SWAP
    
    ordType         String  否    订单类型
                                  market：市价单
                                  limit：限价单
                                  post_only：只做maker单
                                  fok：全部成交或立即取消
                                  ioc：立即成交并取消剩余
                                  optimal_limit_ioc：市价委托立即成交并取消剩余
                                  mmp：做市商保护
                                  mmp_and_post_only：做市商保护且只做maker单
                                  op_fok：期权简选
    
    state           String  否    订单状态
                                  canceled：撤单成功
                                  filled：完全成交
                                  mmp_canceled：做市商保护机制导致的自动撤单
    
    category        String  否    订单种类
                                  twap：TWAP自动换币
                                  adl：ADL自动减仓
                                  full_liquidation：强制平仓
                                  partial_liquidation：强制减仓
                                  delivery：交割
                                  ddh：对冲减仓类型订单
    
    after           String  否    请求此ID之前（更旧的数据）的分页内容，传ordId
    before          String  否    请求此ID之后（更新的数据）的分页内容，传ordId
    begin           String  否    筛选的开始时间戳，Unix毫秒时间戳
    end             String  否    筛选的结束时间戳，Unix毫秒时间戳
    limit           String  否    返回结果的数量，最大为100，默认100条
    
    返回参数说明：
    ============
    instType        String        产品类型
    instId          String        产品ID
    tgtCcy          String        币币市价单委托数量sz的单位
    ccy             String        保证金币种
    ordId           String        订单ID
    clOrdId         String        客户自定义订单ID
    tag             String        订单标签
    px              String        委托价格
    pxUsd           String        期权价格，以USD为单位
    pxVol           String        期权订单的隐含波动率
    pxType          String        期权的价格类型
    sz              String        委托数量
    ordType         String        订单类型
    side            String        订单方向（buy/sell）
    posSide         String        持仓方向（long/short/net）
    tdMode          String        交易模式
    accFillSz       String        累计成交数量
    fillPx          String        最新成交价格
    tradeId         String        最新成交ID
    fillSz          String        最新成交数量
    fillTime        String        最新成交时间
    avgPx           String        成交均价
    state           String        订单状态
    lever           String        杠杆倍数
    attachAlgoClOrdId String      下单附带止盈止损时的客户自定义策略订单ID
    tpTriggerPx     String        止盈触发价
    tpTriggerPxType String        止盈触发价类型
    tpOrdPx         String        止盈委托价
    slTriggerPx     String        止损触发价
    slTriggerPxType String        止损触发价类型
    slOrdPx         String        止损委托价
    attachAlgoOrds  Array         下单附带止盈止损信息
    linkedAlgoOrd   Object        止损订单信息
    stpId           String        自成交保护ID
    stpMode         String        自成交保护模式
    feeCcy          String        交易手续费币种
    fee             String        手续费与返佣
    rebateCcy       String        返佣金币种
    source          String        订单来源
    rebate          String        返佣金额
    pnl             String        收益
    category        String        订单种类
    reduceOnly      String        是否只减仓
    cancelSource    String        订单取消来源的原因枚举值代码
    cancelSourceReason String     订单取消来源的对应具体原因
    algoClOrdId     String        客户自定义策略订单ID
    algoId          String        策略委托单ID
    isTpLimit       String        是否为限价止盈
    uTime           String        订单状态更新时间（毫秒时间戳）
    cTime           String        订单创建时间（毫秒时间戳）
    """
    
    print("🔍 正在查询永续合约历史订单...")
    
    try:
        # 获取API配置
        config = get_api_config()
        
        # 验证配置
        is_valid, message = validate_config(config)
        if not is_valid:
            print(f"❌ API配置错误: {message}")
            return
        
        print("✅ API配置验证通过")
        print_config_info(config)
        
        # 初始化交易API
        tradeAPI = Trade.TradeAPI(
            config['api_key'], 
            config['secret_key'], 
            config['passphrase'], 
            False, 
            config['flag']
        )
        
        # 计算查询时间范围（最近7天）
        end_time = datetime.now()
        start_time = end_time - timedelta(days=7)
        
        # 转换为毫秒时间戳
        begin_timestamp = str(int(start_time.timestamp() * 1000))
        end_timestamp = str(int(end_time.timestamp() * 1000))
        
        print(f"\n📅 查询时间范围:")
        print(f"   开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 查询永续合约历史订单
        result = tradeAPI.get_orders_history(
            instType="SWAP",           # 永续合约
            # instId="BTC-USDT-SWAP",  # 可指定具体产品
            # ordType="limit,market",   # 可指定订单类型
            # state="filled,canceled",  # 可指定订单状态
            begin=begin_timestamp,     # 开始时间
            end=end_timestamp,         # 结束时间
            limit="50"                 # 返回50条记录
        )
        
        # 处理查询结果
        if result.get('code') == '0':
            orders = result.get('data', [])
            
            print(f"\n✅ 查询成功，共找到 {len(orders)} 条历史订单")
            
            if orders:
                print("\n📊 订单详情:")
                print("-" * 170)
                print(f"{'序号':<4} {'产品ID':<16} {'订单ID':<18} {'方向':<8} {'类型':<10} {'状态':<12} {'委托量':<10} {'成交量':<10} {'委托价':<14} {'成交价':<14} {'成交率':<8} {'创建时间':<20}")
                print("-" * 170)
                
                for i, order in enumerate(orders[:20]):  # 显示前10条
                    # 时间戳转换
                    create_time = datetime.fromtimestamp(int(order.get('cTime', '0')) / 1000)
                    create_time_str = create_time.strftime('%m-%d %H:%M:%S')
                    
                    # 订单信息
                    inst_id = order.get('instId', '')
                    ord_id = order.get('ordId', '')[:16] + '..' if len(order.get('ordId', '')) > 16 else order.get('ordId', '')
                    side = order.get('side', '')
                    ord_type = order.get('ordType', '')
                    state = order.get('state', '')
                    
                    # 价格和数量信息
                    px = order.get('px', '0')  # 委托价格
                    avg_px = order.get('avgPx', '0')  # 成交均价
                    sz = order.get('sz', '0')  # 委托数量
                    acc_fill_sz = order.get('accFillSz', '0')  # 累计成交数量
                    
                    # 计算成交率
                    sz_float = safe_float(sz)
                    acc_fill_sz_float = safe_float(acc_fill_sz)
                    
                    if sz_float > 0:
                        fill_rate = (acc_fill_sz_float / sz_float) * 100
                        fill_rate_str = f"{fill_rate:.1f}%"
                    else:
                        fill_rate_str = "0.0%"
                    
                    # 状态显示优化
                    state_display = {
                        'filled': '✅成交',
                        'canceled': '❌撤销',
                        'mmp_canceled': '⚠️MMP撤销',
                        'live': '🔄挂单',
                        'partially_filled': '🟡部分成交'
                    }.get(state, state[:6])
                    
                    # 方向显示优化
                    side_display = {
                        'buy': '🟢买入',
                        'sell': '🔴卖出'
                    }.get(side, side)
                    
                    # 格式化价格显示
                    px_float = safe_float(px)
                    avg_px_float = safe_float(avg_px)
                    
                    px_display = f"{px_float:.3f}" if px_float > 0 else "-"
                    avg_px_display = f"{avg_px_float:.3f}" if avg_px_float > 0 else "-"
                    sz_display = f"{sz_float}" if sz_float > 0 else "0"
                    acc_fill_sz_display = f"{acc_fill_sz_float}" if acc_fill_sz_float > 0 else "0"
                    
                    print(f"{i+1:<4} {inst_id:<16} {ord_id:<18} {side_display:<8} {ord_type:<10} {state_display:<12} {sz_display:<10} {acc_fill_sz_display:<10} {px_display:<14} {avg_px_display:<14} {fill_rate_str:<8} {create_time_str:<20}")
                
                if len(orders) > 10:
                    print(f"\n💡 还有 {len(orders) - 10} 条订单未显示")
                
                # 统计信息
                print(f"\n📈 统计信息:")
                filled_orders = [o for o in orders if o.get('state') == 'filled']
                canceled_orders = [o for o in orders if o.get('state') == 'canceled']
                buy_orders = [o for o in orders if o.get('side') == 'buy']
                sell_orders = [o for o in orders if o.get('side') == 'sell']
                
                print(f"   总订单数: {len(orders)}")
                print(f"   已成交: {len(filled_orders)} 条")
                print(f"   已撤销: {len(canceled_orders)} 条")
                print(f"   买入订单: {len(buy_orders)} 条")
                print(f"   卖出订单: {len(sell_orders)} 条")
                
                # 计算交易量统计
                total_order_size = sum([safe_float(order.get('sz', '0')) for order in orders])
                total_filled_size = sum([safe_float(order.get('accFillSz', '0')) for order in orders])
                
                print(f"\n📊 交易量统计:")
                print(f"   总委托量: {total_order_size} 张")
                print(f"   总成交量: {total_filled_size} 张")
                
                if total_order_size > 0:
                    overall_fill_rate = (total_filled_size / total_order_size) * 100
                    print(f"   整体成交率: {overall_fill_rate:.1f}%")
                
                # 按产品统计历史定量成交量
                print(f"\n📈 历史定量成交量统计:")
                product_volumes = {}
                product_turnovers = {}
                
                for order in orders:
                    inst_id = order.get('instId', '')
                    acc_fill_sz = safe_float(order.get('accFillSz', '0'))
                    avg_px = safe_float(order.get('avgPx', '0'))
                    
                    if inst_id not in product_volumes:
                        product_volumes[inst_id] = 0
                        product_turnovers[inst_id] = 0
                    
                    product_volumes[inst_id] += acc_fill_sz
                    if avg_px > 0 and acc_fill_sz > 0:
                        product_turnovers[inst_id] += avg_px * acc_fill_sz
                
                # 按成交量排序显示
                sorted_products = sorted(product_volumes.items(), key=lambda x: x[1], reverse=True)
                
                print(f"   {'产品':<18} {'成交量(张)':<12} {'成交金额(USDT)':<18} {'占比':<8}")
                print(f"   {'-'*18} {'-'*12} {'-'*18} {'-'*8}")
                
                for inst_id, volume in sorted_products:
                    if volume > 0:
                        turnover = product_turnovers.get(inst_id, 0)
                        volume_ratio = (volume / total_filled_size * 100) if total_filled_size > 0 else 0
                        
                        print(f"   {inst_id:<18} {volume:<12} {turnover:<18,.2f} {volume_ratio:<7.1f}%")
                
                # 买卖方向成交量统计
                buy_volume = sum([safe_float(order.get('accFillSz', '0')) for order in orders if order.get('side') == 'buy'])
                sell_volume = sum([safe_float(order.get('accFillSz', '0')) for order in orders if order.get('side') == 'sell'])
                
                print(f"\n📊 买卖方向成交量:")
                print(f"   买入成交量: {buy_volume} 张 ({(buy_volume/total_filled_size*100):.1f}%)" if total_filled_size > 0 else f"   买入成交量: {buy_volume} 张")
                print(f"   卖出成交量: {sell_volume} 张 ({(sell_volume/total_filled_size*100):.1f}%)" if total_filled_size > 0 else f"   卖出成交量: {sell_volume} 张")
                
                # 按时间段统计成交量
                print(f"\n⏰ 时间段成交量分布:")
                time_volumes = {}
                
                for order in orders:
                    ctime = safe_int(order.get('cTime', '0'))
                    if ctime > 0:
                        order_time = datetime.fromtimestamp(ctime / 1000)
                        time_key = order_time.strftime('%Y-%m-%d')
                        acc_fill_sz = safe_float(order.get('accFillSz', '0'))
                        
                        if time_key not in time_volumes:
                            time_volumes[time_key] = 0
                        time_volumes[time_key] += acc_fill_sz
                
                sorted_time_volumes = sorted(time_volumes.items(), key=lambda x: x[0], reverse=True)
                
                for date, volume in sorted_time_volumes:
                    volume_ratio = (volume / total_filled_size * 100) if total_filled_size > 0 else 0
                    print(f"   {date}: {volume} 张 ({volume_ratio:.1f}%)")
                
                # 计算成交金额统计
                total_turnover = 0
                for order in filled_orders:
                    avg_px = safe_float(order.get('avgPx', '0'))
                    acc_fill_sz = safe_float(order.get('accFillSz', '0'))
                    if avg_px > 0 and acc_fill_sz > 0:
                        turnover = avg_px * acc_fill_sz
                        total_turnover += turnover
                
                if total_turnover > 0:
                    print(f"   总成交金额: {total_turnover:,.2f} USDT")
                
                # 计算总手续费
                total_fee = sum([safe_float(order.get('fee', '0')) for order in orders if order.get('fee')])
                if total_fee != 0:
                    print(f"   总手续费: {total_fee:.6f} USDT")
                    if total_turnover > 0:
                        fee_rate = abs(total_fee / total_turnover) * 100
                        print(f"   手续费率: {fee_rate:.4f}%")
                
                # 显示最新订单的详细信息
                if orders:
                    latest_order = orders[0]
                    print(f"\n🔍 最新订单详情:")
                    print(f"   订单ID: {latest_order.get('ordId', '')}")
                    print(f"   产品: {latest_order.get('instId', '')}")
                    print(f"   方向: {latest_order.get('side', '')} / {latest_order.get('posSide', '')}")
                    print(f"   委托数量: {latest_order.get('sz', '')} 张")
                    print(f"   成交数量: {latest_order.get('accFillSz', '')} 张")
                    
                    # 计算成交率
                    sz_value = safe_float(latest_order.get('sz', '0'))
                    acc_fill_sz_value = safe_float(latest_order.get('accFillSz', '0'))
                    
                    if sz_value > 0:
                        fill_rate = (acc_fill_sz_value / sz_value) * 100
                        print(f"   成交率: {fill_rate:.1f}%")
                    
                    print(f"   委托价格: {latest_order.get('px', '')} USDT")
                    print(f"   成交均价: {latest_order.get('avgPx', '')} USDT")
                    
                    # 计算成交金额
                    avg_px_value = safe_float(latest_order.get('avgPx', '0'))
                    if avg_px_value > 0 and acc_fill_sz_value > 0:
                        turnover = avg_px_value * acc_fill_sz_value
                        print(f"   成交金额: {turnover:,.2f} USDT")
                    
                    print(f"   杠杆倍数: {latest_order.get('lever', '')}x")
                    print(f"   手续费: {latest_order.get('fee', '')} USDT")
                    
                    pnl_value = safe_float(latest_order.get('pnl', '0'))
                    if pnl_value != 0:
                        pnl_display = f"{'🟢' if pnl_value > 0 else '🔴'} {pnl_value:.6f} USDT"
                        print(f"   收益: {pnl_display}")
                    
                    # 显示订单时间信息
                    ctime_value = safe_int(latest_order.get('cTime', '0'))
                    if ctime_value > 0:
                        create_time = datetime.fromtimestamp(ctime_value / 1000)
                        print(f"   创建时间: {create_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    
                    utime_value = safe_int(latest_order.get('uTime', '0'))
                    if utime_value > 0:
                        update_time = datetime.fromtimestamp(utime_value / 1000)
                        print(f"   更新时间: {update_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            else:
                print("📝 在指定时间范围内未找到永续合约历史订单")
                print("💡 建议:")
                print("   - 扩大查询时间范围")
                print("   - 检查是否有永续合约交易记录")
                print("   - 尝试查询其他产品类型")
        
        else:
            print(f"❌ 查询失败")
            print(f"   错误代码: {result.get('code', 'Unknown')}")
            print(f"   错误信息: {result.get('msg', '未知错误')}")
            
            # 常见错误处理建议
            error_code = result.get('code', '')
            if error_code == '50001':
                print("💡 可能原因: API权限不足，请检查API Key权限设置")
            elif error_code == '50004':
                print("💡 可能原因: 请求过于频繁，请稍后重试")
            elif error_code == '50013':
                print("💡 可能原因: 系统繁忙，请稍后重试")
    
    except Exception as e:
        print(f"❌ 程序执行异常: {e}")
        print("💡 请检查:")
        print("   - 网络连接是否正常")
        print("   - API配置是否正确")
        print("   - 是否有足够的API权限")


def query_specific_product_orders():
    """查询指定产品的历史订单"""
    print("\n" + "="*60)
    print("🎯 查询指定产品历史订单")
    print("="*60)
    
    # 可以查询的热门永续合约产品
    popular_products = [
        "BTC-USDT-SWAP",
        "ETH-USDT-SWAP", 
        "BNB-USDT-SWAP",
        "SOL-USDT-SWAP",
        "DOGE-USDT-SWAP"
    ]
    
    try:
        config = get_api_config()
        tradeAPI = Trade.TradeAPI(
            config['api_key'], 
            config['secret_key'], 
            config['passphrase'], 
            False, 
            config['flag']
        )
        
        # 查询BTC永续合约订单
        result = tradeAPI.get_orders_history(
            instType="SWAP",
            instId="BTC-USDT-SWAP",  # 指定BTC永续合约
            state="filled",          # 只查询已成交订单
            limit="20"
        )
        
        if result.get('code') == '0':
            orders = result.get('data', [])
            print(f"✅ BTC-USDT-SWAP 已成交订单: {len(orders)} 条")
            
            if orders:
                total_volume = sum([safe_float(order.get('accFillSz', '0')) for order in orders])
                print(f"   总成交量: {total_volume:.2f} 张")
        else:
            print(f"❌ 查询BTC永续合约订单失败: {result.get('msg', '')}")
    
    except Exception as e:
        print(f"❌ 查询指定产品订单异常: {e}")


def main():
    """主函数"""
    print("=" * 80)
    print("📋 OKX永续合约历史订单查询Demo")
    print("=" * 80)
    print("功能说明:")
    print("  - 查询最近7天的永续合约历史订单")
    print("  - 支持多种筛选条件")
    print("  - 显示订单详细信息和统计数据")
    print("  - 包含完整的API参数说明")
    print("=" * 80)
    
    try:
        # 查询所有永续合约历史订单
        query_swap_orders_history()
        
        # 查询指定产品订单
        query_specific_product_orders()
        
        print("\n✅ Demo执行完成")
        
    except KeyboardInterrupt:
        print("\n🛑 程序被用户中断")
    except Exception as e:
        print(f"\n❌ 程序执行异常: {e}")


if __name__ == "__main__":
    main()
