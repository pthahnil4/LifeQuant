#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
资金仓位管理模块
================

功能：
1. 阶梯式资金管理
2. 根据盈利情况动态调整可用本金
3. 计算仓位大小
4. 管理全仓和逐仓模式

作者：AI Assistant
创建时间：2025年1月17日
"""

import logging
import math
from typing import Dict, Tuple, Optional

logger = logging.getLogger(__name__)


class PositionManager:
    """资金仓位管理器"""
    
    def __init__(self, profit_share_ratio=0.4):
        """初始化资金管理器
        
        Args:
            profit_share_ratio: 盈利分润比例(r)，默认0.4（40%）
        """
        # 资金管理参数
        self.profit_share_ratio = profit_share_ratio  # 盈利分润比例
        
        # 仓位模式
        self.POSITION_MODE_FULL = 1.0      # 全仓模式：100%可用本金
        self.POSITION_MODE_PARTIAL = 0.5   # 逐仓模式：50%可用本金
        
        # 时间加权委托配置（按用户要求的精确配置）
        self.twap_config = {
            '1H': {
                'interval': 300,           # 5分钟间隔
                'single_ratio': 0.1,      # 10%单笔执行量
                'total_duration': 3000,    # 50分钟执行完成
                'long_period': '1D'
            },
            '15m': {
                'interval': 60,            # 15秒间隔  
                'single_ratio': 0.1,      # 10%单笔执行量
                'total_duration': 600,     # 10分钟执行完成
                'long_period': '4H'
            },
            '5m': {
                'interval': 60,             # 5秒间隔
                'single_ratio': 0.2,      # 20%单笔执行量  
                'total_duration': 300,     # 5分钟执行完成
                'long_period': '1H'
            }
        }
    
    def calculate_available_capital(self, initial_capital: float, current_equity: float) -> float:
        """
        计算当前应使用的交易本金
        直接调用fund_management.py中的标准实现
        
        Args:
            initial_capital: 初始本金(P₀)
            current_equity: 当前权益/总资产(C)
        
        Returns:
            float: 交易本金(P)
        """
        # 🎯 修复：直接调用fund_management中的标准函数
        from .fund_management import calculate_trading_capital
        
        trading_capital = calculate_trading_capital(
            initial_capital=initial_capital,
            profit_share_ratio=self.profit_share_ratio,
            current_assets=current_equity
        )
        
        # 计算详细信息用于日志
        profit = current_equity - initial_capital
        additional_capital = profit * self.profit_share_ratio if profit > 0 else 0
        
        # 生成说明
        if profit <= 0:
            explanation = "亏损或持平，使用初始本金"
        else:
            explanation = f"盈利{profit:.2f}，追加{additional_capital:.2f}（盈利的{self.profit_share_ratio*100:.0f}%）"
        
        logger.info(f"资金管理计算: 初始本金={initial_capital:.2f}, 当前权益={current_equity:.2f}, "
                   f"交易本金={trading_capital:.2f}, {explanation}")
        
        return trading_capital
    
    def calculate_contract_amount(self, available_capital: float, current_position_contracts: float,
                                is_same_direction: bool, current_price: float, period: str,
                                signal_direction: str, leverage: float = 10.0, min_contract_amount: float = 0.2) -> float:
        """
        计算目标合约张数（按周期组合分层管理）
        
        Args:
            available_capital: 可用本金(USDT)
            current_position_contracts: 当前持仓张数（带方向，正数=多头，负数=空头）
            is_same_direction: 长短周期是否同向
            current_price: 当前价格(USDT)
            period: 短周期（用于确定分层比例）
            signal_direction: 信号方向（long/short）
            leverage: 杠杆倍数
            min_contract_amount: 最小合约张数
        
        Returns:
            float: 需要增加/减少的合约张数（保疙1位小数）
        """
        # 根据周期组合确定基础仓位比例（修正：同向应该用更高比例）
        base_ratios = {
            '1H': {'same': 1.0, 'diff': 0.5},    # 1H/1D: 同向100%, 不同向50%
            '15m': {'same': 0.5, 'diff': 0.25},  # 15m/4H: 同向50%, 不同向25%
            '5m': {'same': 0.25, 'diff': 0.125}   # 5m/1H: 同向25%, 不同向12.5%
        }
        
        if period not in base_ratios:
            logger.error(f"不支持的周期: {period}")
            return 0
        
        # 获取对应的仓位比例
        ratio_config = base_ratios[period]
        position_ratio = ratio_config['same'] if is_same_direction else ratio_config['diff']
        
        # 🎯 详细计算过程日志
        logger.info(f"=== 详细仓位计算 ({period}周期) ===")
        logger.info(f"1. 输入参数:")
        logger.info(f"   - 可用本金: {available_capital:.2f} USDT")
        logger.info(f"   - 当前价格: {current_price:.4f} USDT") 
        logger.info(f"   - 双周期同向: {is_same_direction}")
        logger.info(f"   - 信号方向: {signal_direction}")
        logger.info(f"   - 当前持仓: {current_position_contracts:.1f} 张")
        
        # 计算目标仓位价值
        target_position_value = available_capital * position_ratio
        logger.info(f"2. 仓位比例计算:")
        logger.info(f"   - 基础比例: {position_ratio*100:.1f}% (同向={is_same_direction})")
        logger.info(f"   - 目标价值: {available_capital:.2f} × {position_ratio:.3f} = {target_position_value:.2f} USDT")
        
        # 转换为合约张数
        target_contracts_abs = target_position_value / current_price
        logger.info(f"3. 张数计算:")
        logger.info(f"   - 绝对张数: {target_position_value:.2f} ÷ {current_price:.4f} = {target_contracts_abs:.2f} 张")
        
        # 根据信号方向确定目标持仓的正负号
        if signal_direction == 'long' or signal_direction == '上涨':
            target_contracts = target_contracts_abs  # 正数（多头）
        elif signal_direction == 'short' or signal_direction == '下跌':
            target_contracts = -target_contracts_abs  # 负数（空头）
        else:
            logger.error(f"未知信号方向: {signal_direction}")
            return 0
        
        # 保留1位小数
        target_contracts = round(target_contracts, 1)
        
        logger.info(f"4. 方向调整:")
        logger.info(f"   - 目标持仓: {target_contracts:.1f} 张 ({signal_direction})")
        
        # 计算需要调整的合约张数
        contract_adjustment = target_contracts - current_position_contracts
        contract_adjustment = round(contract_adjustment, 1)
        
        logger.info(f"5. 调整计算:")
        logger.info(f"   - 目标 - 当前: {target_contracts:.1f} - ({current_position_contracts:.1f}) = {contract_adjustment:.1f} 张")
        
        # 检查是否达到最小交易张数
        if abs(contract_adjustment) < min_contract_amount:
            logger.info(f"⚠️  调整量({abs(contract_adjustment):.1f}张) < 最小交易张数({min_contract_amount})，跳过交易")
            return 0
        
        logger.info(f"✅ 最终结果: 需要调整 {contract_adjustment:.1f} 张")
        logger.info(f"=== 仓位计算完成 ===")
        
        return contract_adjustment
    
    def calculate_target_position(self, available_capital: float, is_same_direction: bool, 
                                current_price: float, period: str, signal_direction: str, 
                                leverage: float = 10.0) -> float:
        """
        计算目标持仓张数（不考虑当前持仓，纯粹基于资金和信号）
        
        Args:
            available_capital: 可用本金(USDT)
            is_same_direction: 长短周期是否同向
            current_price: 当前价格(USDT)
            period: 短周期
            signal_direction: 信号方向（long/short）
            leverage: 杠杆倍数
        
        Returns:
            float: 目标持仓张数（带方向）
        """
        # 根据周期组合确定基础仓位比例（与calculate_contract_amount保持一致）
        base_ratios = {
            '1H': {'same': 1.0, 'diff': 0.5},    # 1H/1D: 同向100%, 不同向50%
            '15m': {'same': 0.5, 'diff': 0.25},  # 15m/4H: 同向50%, 不同向25%
            '5m': {'same': 0.25, 'diff': 0.125}   # 5m/1H: 同向25%, 不同向12.5%
        }
        
        if period not in base_ratios:
            logger.error(f"不支持的周期: {period}")
            return 0
        
        # 获取对应的仓位比例
        ratio_config = base_ratios[period]
        position_ratio = ratio_config['same'] if is_same_direction else ratio_config['diff']
        
        # 计算目标仓位价值
        target_position_value = available_capital * position_ratio
        
        # 转换为合约张数
        target_contracts_abs = target_position_value / current_price
        
        # 根据信号方向确定目标持仓的正负号
        if signal_direction == 'long' or signal_direction == '上涨':
            target_contracts = target_contracts_abs  # 正数（多头）
        elif signal_direction == 'short' or signal_direction == '下跌':
            target_contracts = -target_contracts_abs  # 负数（空头）
        else:
            logger.error(f"未知信号方向: {signal_direction}")
            return 0
        
        # 保疙1位小数精度
        target_contracts = round(target_contracts, 1)
        
        logger.info(f"目标持仓计算: 可用本金={available_capital:.2f}, 比例={position_ratio*100:.1f}%, "
                   f"信号={signal_direction}, 目标={target_contracts:.1f}张")
        
        return target_contracts
    
    def get_twap_parameters(self, period: str, total_contracts: float) -> Dict:
        """
        获取时间加权委托参数
        
        Args:
            period: 时间周期
            total_contracts: 总委托张数
        
        Returns:
            dict: 包含执行间隔、单笔执行量、执行次数等参数
        """
        if period not in self.twap_config:
            logger.error(f"不支持的时间周期: {period}")
            return None
        
        config = self.twap_config[period]
        
        # 计算单笔执行张数（最小0.2张）
        single_contracts = total_contracts * config['single_ratio']
        single_contracts = max(0.2, single_contracts)  # 最小0.2张
        single_contracts = round(single_contracts, 1)  # 保疙1位小数
        
        # 计算执行次数
        execution_count = math.ceil(total_contracts / single_contracts)
        
        # 实际总执行时间
        actual_duration = execution_count * config['interval']
        
        # 详细日志验证计算
        logger.info(f"TWAP参数计算 {period}周期:")
        logger.info(f"  总委托量: {total_contracts:.1f}张")
        logger.info(f"  单笔比例: {config['single_ratio']*100:.1f}%")
        logger.info(f"  计算单笔: {total_contracts:.1f} × {config['single_ratio']:.3f} = {total_contracts * config['single_ratio']:.3f}")
        logger.info(f"  单笔执行: {single_contracts:.1f}张 (最小0.1张)")
        logger.info(f"  执行次数: {total_contracts:.1f} ÷ {single_contracts:.1f} = {execution_count}次")
        logger.info(f"  执行间隔: {config['interval']}秒")
        logger.info(f"  总执行时间: {execution_count} × {config['interval']} = {actual_duration}秒 ({actual_duration/60:.1f}分钟)")
        
        return {
            'interval': config['interval'],              # 执行间隔（秒）
            'single_contracts': single_contracts,        # 单笔执行张数
            'execution_count': execution_count,          # 执行次数
            'total_duration': actual_duration,           # 总执行时间（秒）
            'long_period': config['long_period']         # 对应的长周期
        }
    
    def calculate_stop_profit_levels(self, entry_price: float, atr_value: float, 
                                    is_same_direction: bool, is_long: bool) -> list:
        """
        计算止盈级别
        
        Args:
            entry_price: 入场价格
            atr_value: ATR值
            is_same_direction: 长短周期是否同向
            is_long: 是否做多
        
        Returns:
            list: 止盈级别列表
        """
        levels = []
        
        if is_same_direction:
            # 长短周期同向：2ATR、4ATR、6ATR
            atr_multiples = [2, 4, 6]
            reduce_ratios = [0.3, 0.4, 0.3]  # 减仓比例：30%、40%、剩余
        else:
            # 长短周期反向：1ATR、2ATR、3ATR
            atr_multiples = [1, 2, 3]
            reduce_ratios = [0.3, 0.4, 0.3]  # 减仓比例：30%、40%、剩余
        
        for i, (multiple, ratio) in enumerate(zip(atr_multiples, reduce_ratios)):
            if is_long:
                target_price = entry_price + (atr_value * multiple)
            else:
                target_price = entry_price - (atr_value * multiple)
            
            levels.append({
                'level': i + 1,
                'atr_multiple': multiple,
                'target_price': target_price,
                'reduce_ratio': ratio,
                'is_trailing': i == len(atr_multiples) - 1  # 最后一级使用移动止盈
            })
        
        return levels
    
    def should_use_full_position(self, short_direction: str, long_direction: str) -> bool:
        """
        判断是否使用全仓模式
        
        Args:
            short_direction: 短周期方向
            long_direction: 长周期方向
        
        Returns:
            bool: True表示全仓，False表示逐仓
        """
        # 方向相同使用全仓，不同使用逐仓
        return short_direction == long_direction
    
    def calculate_trailing_stop_price(self, current_price: float, atr_value: float, 
                                     is_long: bool, trailing_atr_multiple: float = 1.0) -> float:
        """
        计算移动止盈价格
        
        Args:
            current_price: 当前价格
            atr_value: ATR值
            is_long: 是否做多
            trailing_atr_multiple: 移动止盈的ATR倍数
        
        Returns:
            float: 移动止盈价格
        """
        if is_long:
            # 做多时，止盈价格在当前价格下方
            stop_price = current_price - (atr_value * trailing_atr_multiple)
        else:
            # 做空时，止盈价格在当前价格上方
            stop_price = current_price + (atr_value * trailing_atr_multiple)
        
        return stop_price
    
    def format_position_info(self, position_data: Dict) -> str:
        """
        格式化仓位信息用于日志输出
        
        Args:
            position_data: 仓位数据
        
        Returns:
            str: 格式化的仓位信息
        """
        info = []
        info.append(f"币种: {position_data.get('currency_code', 'N/A')}")
        info.append(f"方向: {position_data.get('direction', 'N/A')}")
        info.append(f"持仓量: {position_data.get('position_amount', 0):.4f}")
        info.append(f"持仓价值: {position_data.get('position_value', 0):.2f} USDT")
        info.append(f"未实现盈亏: {position_data.get('unrealized_pnl', 0):.2f} USDT")
        info.append(f"盈亏率: {position_data.get('pnl_ratio', 0):.2%}")
        
        return " | ".join(info)
