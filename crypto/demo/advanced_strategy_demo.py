#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
高级策略系统使用示例
==================

演示如何使用高级策略系统：
1. 资金阶梯式管理
2. 双周期策略
3. 时间加权委托
4. ATR止盈管理

作者：AI Assistant
创建时间：2025年1月17日
"""

import datetime
import decimal
import time
import sys
import os

# 添加父目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.advanced_strategy_manager import AdvancedStrategyManager
from strategy.crypto_analysis_batch import CryptoAnalysisBatch

def demo_capital_management():
    """演示资金阶梯式管理"""
    print("\n" + "="*60)
    print("📊 资金阶梯式管理演示")
    print("="*60)
    
    manager = AdvancedStrategyManager()
    if not manager.connect_database():
        return
    
    try:
        # 获取BTC的1D-1H策略配置
        config = manager.get_strategy_config('BTC-USDT-SWAP', '1D', '1H')
        if not config:
            print("❌ 未找到策略配置")
            return
        
        print(f"💰 当前配置:")
        print(f"   货币对: {config.currency_code}")
        print(f"   可用资金: {config.available_capital} USDT")
        print(f"   当前本金: {config.base_capital} USDT")
        
        # 获取资金管理信息
        capital_mgmt = manager.get_capital_management(config.id)
        if capital_mgmt:
            print(f"   资金等级: {capital_mgmt.capital_level}")
            print(f"   下一阶梯目标: {capital_mgmt.next_upgrade_target} USDT")
            print(f"   累计盈亏: {capital_mgmt.total_profit_loss} USDT")
            print(f"   风险等级: {capital_mgmt.risk_level}")
            
            # 模拟盈利场景
            print(f"\n📈 模拟盈利场景: +30 USDT")
            new_base, new_level, risk_level = manager.calculate_capital_adjustment(
                capital_mgmt, decimal.Decimal('30.00')
            )
            print(f"   调整后本金: {new_base} USDT")
            print(f"   调整后等级: {new_level}")
            print(f"   风险等级: {risk_level}")
    
    finally:
        manager.close_connection()

def demo_dual_period_strategy():
    """演示双周期策略"""
    print("\n" + "="*60)
    print("📊 双周期策略演示")
    print("="*60)
    
    manager = AdvancedStrategyManager()
    if not manager.connect_database():
        return
    
    try:
        # 获取策略配置
        config = manager.get_strategy_config('BTC-USDT-SWAP', '1D', '1H')
        if not config:
            print("❌ 未找到策略配置")
            return
        
        print(f"🔄 双周期组合: {config.long_period} (长) + {config.short_period} (短)")
        
        # 模拟信号更新
        print(f"\n📡 模拟信号更新:")
        print(f"   长周期(1D): rise")
        print(f"   短周期(1H): rise")
        print(f"   → 同向信号，使用全仓模式，70%仓位")
        
        success = manager.update_dual_period_signal(
            strategy_config_id=config.id,
            currency_code=config.currency_code,
            long_direction='rise',
            short_direction='rise',
            long_signal_time=datetime.datetime.now(),
            short_signal_time=datetime.datetime.now(),
            atr_value=decimal.Decimal('500.00'),
            atr_percentage=decimal.Decimal('0.45'),
            long_strength=decimal.Decimal('75.0'),
            short_strength=decimal.Decimal('68.0')
        )
        
        if success:
            print(f"✅ 双周期信号更新成功")
        else:
            print(f"❌ 双周期信号更新失败")
        
        # 模拟反向信号
        print(f"\n📡 模拟反向信号:")
        print(f"   长周期(1D): rise")
        print(f"   短周期(1H): fall")
        print(f"   → 反向信号，使用逐仓模式，30%仓位")
        
        success = manager.update_dual_period_signal(
            strategy_config_id=config.id,
            currency_code=config.currency_code,
            long_direction='rise',
            short_direction='fall',
            long_signal_time=datetime.datetime.now(),
            short_signal_time=datetime.datetime.now(),
            atr_value=decimal.Decimal('500.00'),
            atr_percentage=decimal.Decimal('0.45'),
            long_strength=decimal.Decimal('75.0'),
            short_strength=decimal.Decimal('45.0')
        )
        
        if success:
            print(f"✅ 反向信号更新成功")
    
    finally:
        manager.close_connection()

def demo_weighted_orders():
    """演示时间加权委托"""
    print("\n" + "="*60)
    print("📊 时间加权委托演示")
    print("="*60)
    
    manager = AdvancedStrategyManager()
    if not manager.connect_database():
        return
    
    try:
        # 获取策略配置
        config = manager.get_strategy_config('BTC-USDT-SWAP', '1D', '1H')
        if not config:
            print("❌ 未找到策略配置")
            return
        
        # 创建持仓跟踪
        target_position = config.base_capital * config.same_direction_ratio  # 70%仓位
        print(f"💼 创建持仓跟踪:")
        print(f"   目标仓位: {target_position} USDT")
        print(f"   持仓方向: long")
        print(f"   仓位模式: cross (全仓)")
        
        position_id = manager.create_position_tracking(
            strategy_config_id=config.id,
            currency_code=config.currency_code,
            position_side='long',
            position_mode='cross',
            target_position_size=target_position
        )
        
        if position_id:
            print(f"✅ 持仓跟踪创建成功: ID={position_id}")
            
            # 创建时间加权委托
            single_order_size = config.base_capital * config.order_size_ratio  # 1%本金
            print(f"\n⏰ 创建时间加权委托:")
            print(f"   总目标: {target_position} USDT")
            print(f"   单次下单: {single_order_size} USDT")
            print(f"   时间间隔: {config.order_interval_seconds} 秒")
            
            strategy_order_id = manager.create_weighted_order(
                position_tracking_id=position_id,
                currency_code=config.currency_code,
                total_target_size=target_position,
                single_order_size=single_order_size,
                order_interval_seconds=config.order_interval_seconds,
                signal_period='1H',
                signal_direction='rise',
                signal_timestamp=datetime.datetime.now()
            )
            
            if strategy_order_id:
                print(f"✅ 时间加权委托创建成功: {strategy_order_id}")
                
                # 模拟委托执行进度
                print(f"\n📈 模拟委托执行进度:")
                for i in range(3):
                    print(f"   第{i+1}次下单: {single_order_size} USDT")
                    
                    # 更新委托进度
                    manager.update_weighted_order_progress(
                        strategy_order_id=strategy_order_id,
                        filled_size=single_order_size,
                        last_order_id=f"ORDER_{i+1}"
                    )
                    
                    # 更新持仓跟踪
                    manager.update_position_tracking(
                        position_tracking_id=position_id,
                        filled_size=single_order_size,
                        current_price=decimal.Decimal('110000.00')
                    )
                    
                    print(f"   ✅ 进度更新成功")
            else:
                print(f"❌ 时间加权委托创建失败")
        else:
            print(f"❌ 持仓跟踪创建失败")
    
    finally:
        manager.close_connection()

def demo_take_profit_management():
    """演示ATR止盈管理"""
    print("\n" + "="*60)
    print("📊 ATR止盈管理演示")
    print("="*60)
    
    manager = AdvancedStrategyManager()
    if not manager.connect_database():
        return
    
    try:
        # 模拟持仓数据 (假设已有持仓)
        print(f"💰 模拟持仓状态:")
        print(f"   持仓大小: 84.00 USDT")
        print(f"   入场价格: 110000.00 USDT")
        print(f"   当前价格: 112000.00 USDT")
        print(f"   当前ATR: 500.00 USDT (0.45%)")
        
        # 假设我们有一个持仓ID (在实际使用中会从数据库获取)
        position_id = 1
        current_price = decimal.Decimal('112000.00')
        atr_value = decimal.Decimal('500.00')
        
        # 检查止盈条件
        print(f"\n🎯 检查止盈条件:")
        take_profit_triggers = manager.check_take_profit_conditions(
            position_tracking_id=position_id,
            current_price=current_price,
            atr_value=atr_value
        )
        
        if take_profit_triggers:
            print(f"✅ 发现 {len(take_profit_triggers)} 个止盈触发条件:")
            for trigger in take_profit_triggers:
                print(f"   等级 {trigger['level']}: {trigger['atr_multiplier']}倍ATR")
                print(f"   盈利率: {trigger['profit_ratio']:.2%}")
                print(f"   减仓比例: {trigger['reduce_ratio']:.1%}")
                
                # 记录止盈执行
                success = manager.record_take_profit_execution(
                    position_tracking_id=position_id,
                    take_profit_info=trigger,
                    atr_value=atr_value,
                    current_price=current_price,
                    entry_price=decimal.Decimal('110000.00')
                )
                
                if success:
                    print(f"   ✅ 止盈记录成功")
                else:
                    print(f"   ❌ 止盈记录失败")
        else:
            print(f"⏳ 暂无止盈触发条件")
    
    finally:
        manager.close_connection()

def demo_strategy_overview():
    """演示策略概览"""
    print("\n" + "="*60)
    print("📊 策略概览演示")
    print("="*60)
    
    manager = AdvancedStrategyManager()
    if not manager.connect_database():
        return
    
    try:
        # 获取所有策略概览
        overview = manager.get_strategy_overview()
        
        print(f"📈 策略概览 (共 {len(overview)} 个策略):")
        print(f"{'货币对':<15} {'周期组合':<10} {'当前资金':<12} {'本金':<12} {'等级':<4} {'盈亏':<12} {'风险':<4}")
        print("-" * 80)
        
        for row in overview:
            currency_code = row[0] or 'N/A'
            period_combination = row[1] or 'N/A'
            current_capital = f"{row[2]:.2f}" if row[2] else 'N/A'
            base_capital = f"{row[3]:.2f}" if row[3] else 'N/A'
            capital_level = str(row[4]) if row[4] else 'N/A'
            total_pnl = f"{row[5]:.2f}" if row[5] else '0.00'
            risk_level = row[6] or 'N/A'
            
            print(f"{currency_code:<15} {period_combination:<10} {current_capital:<12} {base_capital:<12} {capital_level:<4} {total_pnl:<12} {risk_level:<4}")
        
        # 获取活跃委托
        active_orders = manager.get_active_weighted_orders()
        print(f"\n⏰ 活跃委托 (共 {len(active_orders)} 个):")
        if active_orders:
            for order in active_orders:
                print(f"   {order[2]} - {order[1]} - 剩余: {order[9]} USDT")
        else:
            print("   暂无活跃委托")
        
        # 清理过期委托
        expired_count = manager.cleanup_expired_orders()
        if expired_count > 0:
            print(f"\n🧹 清理过期委托: {expired_count} 个")
    
    finally:
        manager.close_connection()

def main():
    """主演示函数"""
    print("🚀 高级策略系统使用演示")
    print("=" * 80)
    
    # 1. 资金阶梯式管理演示
    demo_capital_management()
    
    # 2. 双周期策略演示
    demo_dual_period_strategy()
    
    # 3. 时间加权委托演示
    demo_weighted_orders()
    
    # 4. ATR止盈管理演示
    demo_take_profit_management()
    
    # 5. 策略概览演示
    demo_strategy_overview()
    
    print("\n" + "="*80)
    print("🎊 演示完成！")
    print("\n📋 接下来您可以:")
    print("1. 查看数据库中的数据变化")
    print("2. 修改策略参数进行测试")
    print("3. 集成到实际交易系统中")
    print("4. 查看文档: okx/docs/ADVANCED_STRATEGY_SYSTEM.md")

if __name__ == "__main__":
    main()
