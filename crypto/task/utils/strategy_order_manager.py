#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
策略委托管理模块
================

功能：
1. 管理不同类型的策略委托单
2. 跟踪委托单状态
3. 处理信号变化时的委托单调整
4. 管理委托单的生命周期

作者：AI Assistant
创建时间：2025年9月17日
"""

import logging
import time
import json
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)


class SignalType(Enum):
    """信号类型枚举"""
    UNCERTAIN = "uncertain"  # 不确定信号（当前周期）
    CONFIRMED = "confirmed"  # 确定信号（上周期）
    DISAPPEARED = "disappeared"  # 信号消失


class OrderStrategy(Enum):
    """委托策略枚举"""
    TWAP = "twap"  # 时间加权委托
    CHASE_LIMIT = "chase_limit"  # 追逐限价委托
    NORMAL = "normal"  # 普通委托


class StrategyOrderManager:
    """策略委托管理器"""
    
    def __init__(self, trade_executor=None, scheduler=None):
        """
        初始化策略委托管理器
        
        Args:
            trade_executor: 交易执行器实例
            scheduler: 调度器实例（用于日志记录）
        """
        self.trade_executor = trade_executor
        self.scheduler = scheduler  # 用于访问日志记录方法
        
        # 活动委托单记录 {inst_id: {period: {algo_id, strategy, status, ...}}}
        self.active_orders = {}
        
        # 历史委托单记录
        self.order_history = []
        
        # 委托单状态映射
        self.order_status_map = {
            'live': '活动',
            'effective': '已生效',
            'partially_filled': '部分成交',
            'filled': '完全成交',
            'cancelled': '已撤销',
            'order_failed': '失败'
        }
    
    def handle_signal_change(self, inst_id: str, period: str, signal_type: SignalType,
                            direction: str, amount: float, current_position: float,
                            is_same_direction: bool = True) -> Dict:
        """
        处理信号变化
        
        Args:
            inst_id: 产品ID
            period: 时间周期
            signal_type: 信号类型
            direction: 交易方向（buy/sell）
            amount: 需要交易的数量
            current_position: 当前持仓
            is_same_direction: 长短周期是否同向（决定交易模式）
        
        Returns:
            dict: 处理结果
        """
        try:
            logger.info(f"处理信号变化: {inst_id} {period} 信号={signal_type.value} "
                       f"方向={direction} 数量={amount}")
            
            # 获取当前活动委托单
            current_order = self.get_active_order(inst_id, period)
            
            if signal_type == SignalType.UNCERTAIN:
                # 不确定信号：使用时间加权委托
                return self._handle_uncertain_signal(
                    inst_id, period, direction, amount, current_position, current_order, is_same_direction
                )
            
            elif signal_type == SignalType.CONFIRMED:
                # 确定信号：使用追逐限价委托
                return self._handle_confirmed_signal(
                    inst_id, period, direction, amount, current_position, current_order, is_same_direction
                )
            
            elif signal_type == SignalType.DISAPPEARED:
                # 信号消失：撤销当前委托
                return self._handle_disappeared_signal(inst_id, period, current_order)
            
            else:
                logger.error(f"未知信号类型: {signal_type}")
                return {'success': False, 'error': '未知信号类型'}
                
        except Exception as e:
            logger.error(f"处理信号变化异常: {e}")
            return {'success': False, 'error': str(e)}
    
    def _handle_uncertain_signal(self, inst_id: str, period: str, direction: str,
                                amount: float, current_position: float,
                                current_order: Dict, is_same_direction: bool = True) -> Dict:
        """处理不确定信号"""
        try:
            # 如果已有相同方向的委托单，检查状态
            if current_order and current_order.get('direction') == direction:
                if current_order.get('strategy') == OrderStrategy.TWAP.value:
                    logger.info(f"已存在相同方向的TWAP委托: {current_order['algo_id']}")
                    return {'success': True, 'action': 'keep_existing'}
                else:
                    # 撤销非TWAP委托，改为TWAP
                    self._cancel_order(current_order['algo_id'], inst_id)
            
            # 如果有反向委托单，撤销
            if current_order and current_order.get('direction') != direction:
                self._cancel_order(current_order['algo_id'], inst_id)
            
            # 修正后的逻辑：考虑方向和持仓的匹配
            logger.info(f"不确定信号: 方向={direction}, 当前持仓={current_position:.1f}张, 目标数量={amount:.1f}张")
            
            # 获取TWAP参数（提前获取）
            from .position_manager import PositionManager
            pos_manager = PositionManager()
            estimated_amount = max(amount, abs(current_position))  # 预估最大可能需要的数量
            twap_params = pos_manager.get_twap_parameters(period, estimated_amount)
            
            if not twap_params:
                return {'success': False, 'error': '获取TWAP参数失败'}
            
            # 正确的分步交易逻辑：先平仓，再开仓
            if direction == 'buy':
                target_position = amount  # 目标正数持仓
            else:
                target_position = -amount  # 目标负数持仓
            
            logger.info(f"目标持仓={target_position:.1f}张, 当前持仓={current_position:.1f}张")
            
            # 判断是否需要分步操作
            position_sign = 1 if current_position > 0 else -1 if current_position < 0 else 0
            target_sign = 1 if target_position > 0 else -1 if target_position < 0 else 0
            
            if position_sign != 0 and target_sign != 0 and position_sign != target_sign:
                # 方向完全相反，需要分步操作：先平仓，再开仓
                logger.info(f"检测到方向反转：{current_position:.1f}张 → {target_position:.1f}张，执行分步操作")
                
                # 第一步：平仓（只平当前持仓）
                close_amount = abs(current_position)
                close_direction = 'sell' if current_position > 0 else 'buy'
                
                # 获取当前持仓的交易模式
                current_mode = self.trade_executor.get_position_trading_mode(inst_id)
                close_is_same_direction = (current_mode == 'cross')  # 全仓=True, 逐仓=False
                
                mode_desc = "全仓模式" if current_mode == 'cross' else "逐仓模式"
                logger.info(f"第一步-平仓: {close_direction} {close_amount:.1f}张 (使用{mode_desc})")
                
                # 执行平仓操作（使用与当前持仓相同的交易模式）
                close_result = self.trade_executor.execute_twap_order(
                    inst_id=inst_id,
                    side=close_direction,
                    total_amount=close_amount,
                    interval=twap_params['interval'],
                    single_amount=min(close_amount, twap_params['single_contracts']),
                    duration=twap_params['total_duration'] // 2,  # 平仓用一半时间
                    is_same_direction=close_is_same_direction  # 使用与持仓相同的模式
                )
                
                if close_result.get('success'):
                    logger.info(f"平仓委托创建成功: {close_result.get('algo_id')}")
                    
                    # 记录平仓委托操作
                    if self.scheduler:
                        self.scheduler.log_trading_operation("ORDER_CREATE", inst_id, period, {
                            'order_type': f"TWAP平仓({mode_desc})",
                            'trading_mode': mode_desc,
                            'side': close_direction,
                            'amount': close_amount,
                            'price': 0,  # TWAP委托没有固定价格
                            'algo_id': close_result.get('algo_id', 'N/A'),
                            'action': 'close_position_first'
                        })
                    
                    # 注意：这里不立即开反向仓，等待平仓完成
                    return {
                        'success': True, 
                        'action': 'close_position_first',
                        'algo_id': close_result.get('algo_id'),
                        'next_step': 'open_reverse_position',
                        'target_amount': abs(target_position)
                    }
                else:
                    # 记录平仓失败
                    if self.scheduler:
                        self.scheduler.log_trading_operation("ERROR", inst_id, period, {
                            'error': close_result.get('error', '平仓失败'),
                            'operation': f"TWAP平仓 {close_direction} {close_amount:.1f}张",
                            'impact': "无法执行方向反转"
                        })
                    return close_result
            
            else:
                # 同方向调整或从零开始，直接计算差值
                need_to_trade = target_position - current_position
                
                logger.info(f"同方向调整: 需要交易={need_to_trade:.1f}张")
                
                # 如果需要交易的数量很小，认为已达目标
                if abs(need_to_trade) < 0.2:
                    logger.info(f"当前持仓已接近目标，无需调整")
                    return {'success': True, 'action': 'position_sufficient'}
                
                # 确定实际交易方向和数量
                if need_to_trade > 0:
                    actual_direction = 'buy'
                    remaining_amount = need_to_trade
                else:
                    actual_direction = 'sell'
                    remaining_amount = abs(need_to_trade)
                
                logger.info(f"执行TWAP委托: 方向={actual_direction}, 数量={remaining_amount:.1f}张")
                
                # 检查是否有重复委托
                duplicate_check = self.trade_executor.check_pending_orders_before_trade(
                    inst_id, actual_direction, remaining_amount
                )
                
                if duplicate_check.get('has_duplicate'):
                    logger.warning(f"跳过重复委托: {duplicate_check.get('message')}")
                    return {'success': True, 'action': 'duplicate_order_skipped'}
                
                # 创建TWAP委托（使用正确的参数名）
                result = self.trade_executor.execute_twap_order(
                    inst_id=inst_id,
                    side=actual_direction,  # 使用实际计算出的方向
                    total_amount=remaining_amount,  # 总委托量
                    interval=twap_params['interval'],
                    single_amount=twap_params['single_contracts'],  # 单笔执行量
                    duration=twap_params['total_duration'],
                    is_same_direction=is_same_direction  # 传递方向关系
                )
                
                if result.get('success'):
                    # 记录委托单
                    self._record_order(inst_id, period, result['algo_id'],
                                     OrderStrategy.TWAP, actual_direction, remaining_amount)
                    logger.info(f"TWAP委托创建成功: {result['algo_id']}")
                    
                    # 记录已由crypto_deal_scheduler统一处理，避免重复记录
                else:
                    # 记录TWAP委托失败
                    if self.scheduler:
                        self.scheduler.log_trading_operation("ERROR", inst_id, period, {
                            'error': result.get('error', 'TWAP委托失败'),
                            'operation': f"TWAP {actual_direction} {remaining_amount:.1f}张",
                            'impact': "委托创建失败"
                        })
                
                return result
            
        except Exception as e:
            logger.error(f"处理不确定信号异常: {e}")
            return {'success': False, 'error': str(e)}
    
    def _handle_confirmed_signal(self, inst_id: str, period: str, direction: str,
                                amount: float, current_position: float,
                                current_order: Dict, is_same_direction: bool = True) -> Dict:
        """处理确定信号"""
        try:
            # 如果有TWAP委托，取消并获取剩余数量
            remaining_from_twap = 0
            if current_order and current_order.get('strategy') == OrderStrategy.TWAP.value:
                # 获取TWAP执行进度
                order_details = self.trade_executor.get_algo_order_details(
                    current_order['algo_id']
                )
                if order_details:
                    filled_qty = float(order_details.get('accFillSz', '0'))
                    total_qty = float(order_details.get('sz', '0'))
                    remaining_from_twap = total_qty - filled_qty
                
                # 撤销TWAP委托
                self._cancel_order(current_order['algo_id'], inst_id)
            
            # 修正后的逻辑：直接使用传入的方向和数量
            logger.info(f"信号方向={direction}, 当前持仓={current_position:.1f}张, 目标数量={amount:.1f}张")
            
            # 直接使用传入的交易方向和数量，不需要重新计算目标持仓
            # direction='sell' amount=1.1 就表示要执行sell 1.1张的操作
            actual_direction = direction
            actual_amount = amount
            
            logger.info(f"直接执行交易: 方向={actual_direction}, 数量={actual_amount:.1f}张")
            
            # 检查交易数量是否足够
            if actual_amount < 0.2:
                logger.info(f"交易数量({actual_amount:.1f}张)小于最小值0.2张，跳过交易")
                return {'success': True, 'action': 'amount_too_small'}
            
            # 检查是否有重复委托
            duplicate_check = self.trade_executor.check_pending_orders_before_trade(
                inst_id, actual_direction, actual_amount
            )
            
            if duplicate_check.get('has_duplicate'):
                logger.warning(f"跳过重复委托: {duplicate_check.get('message')}")
                return {'success': True, 'action': 'duplicate_order_skipped'}
            
            # 创建追逐限价委托
            result = self.trade_executor.execute_chase_limit_order(
                inst_id=inst_id,
                side=actual_direction,
                amount=actual_amount,
                is_same_direction=is_same_direction  # 传递方向关系
            )
            
            if result.get('success'):
                # 记录委托单
                self._record_order(inst_id, period, result['algo_id'],
                                 OrderStrategy.CHASE_LIMIT, actual_direction, actual_amount)
                logger.info(f"追逐限价委托创建成功: {result['algo_id']}")
                
                # 记录已由crypto_deal_scheduler统一处理，避免重复记录
            else:
                # 记录追逐限价委托失败
                if self.scheduler:
                    self.scheduler.log_trading_operation("ERROR", inst_id, period, {
                        'error': result.get('error', '追逐限价委托失败'),
                        'operation': f"追逐限价 {actual_direction} {actual_amount:.1f}张",
                        'impact': "委托创建失败"
                    })
            
            return result
            
        except Exception as e:
            logger.error(f"处理确定信号异常: {e}")
            return {'success': False, 'error': str(e)}
    
    def _handle_disappeared_signal(self, inst_id: str, period: str,
                                  current_order: Dict) -> Dict:
        """处理信号消失"""
        try:
            if not current_order:
                logger.info(f"无活动委托单，无需处理")
                return {'success': True, 'action': 'no_order'}
            
            # 只撤销TWAP委托，不撤销追逐限价委托
            if current_order.get('strategy') == OrderStrategy.TWAP.value:
                success = self._cancel_order(current_order['algo_id'], inst_id)
                if success:
                    logger.info(f"信号消失，已撤销TWAP委托: {current_order['algo_id']}")
                    return {'success': True, 'action': 'cancelled_twap'}
                else:
                    return {'success': False, 'error': '撤销委托失败'}
            else:
                logger.info(f"信号消失，但保留追逐限价委托: {current_order['algo_id']}")
                return {'success': True, 'action': 'keep_chase_limit'}
                
        except Exception as e:
            logger.error(f"处理信号消失异常: {e}")
            return {'success': False, 'error': str(e)}
    
    def check_period_transition(self, inst_id: str, period: str,
                               current_time: datetime) -> bool:
        """
        检查是否进入新周期
        
        Args:
            inst_id: 产品ID
            period: 时间周期
            current_time: 当前时间
        
        Returns:
            bool: 是否需要转换委托策略
        """
        try:
            # 获取当前活动委托单
            current_order = self.get_active_order(inst_id, period)
            if not current_order:
                return False
            
            # 只处理TWAP委托
            if current_order.get('strategy') != OrderStrategy.TWAP.value:
                return False
            
            # 检查委托创建时间
            order_time = datetime.fromisoformat(current_order.get('create_time'))
            
            # 根据周期判断是否跨越
            period_minutes = {
                '5m': 5,
                '15m': 15,
                '1H': 60,
                '4H': 240,
                '1D': 1440,
                '1W': 10080
            }
            
            if period not in period_minutes:
                return False
            
            minutes = period_minutes[period]
            
            # 计算是否跨越周期
            order_period = order_time.replace(
                minute=(order_time.minute // minutes) * minutes,
                second=0,
                microsecond=0
            )
            current_period = current_time.replace(
                minute=(current_time.minute // minutes) * minutes,
                second=0,
                microsecond=0
            )
            
            return current_period > order_period
            
        except Exception as e:
            logger.error(f"检查周期转换异常: {e}")
            return False
    
    def transition_to_chase_limit(self, inst_id: str, period: str) -> Dict:
        """
        将TWAP委托转换为追逐限价委托
        
        Args:
            inst_id: 产品ID
            period: 时间周期
        
        Returns:
            dict: 转换结果
        """
        try:
            current_order = self.get_active_order(inst_id, period)
            if not current_order:
                return {'success': False, 'error': '无活动委托单'}
            
            if current_order.get('strategy') != OrderStrategy.TWAP.value:
                return {'success': True, 'action': 'not_twap'}
            
            # 获取TWAP执行进度
            order_details = self.trade_executor.get_algo_order_details(
                current_order['algo_id']
            )
            
            if not order_details:
                return {'success': False, 'error': '获取委托详情失败'}
            
            filled_qty = float(order_details.get('accFillSz', '0'))
            total_qty = float(order_details.get('sz', '0'))
            remaining_qty = total_qty - filled_qty
            
            if remaining_qty <= 0:
                logger.info(f"TWAP已完成，无需转换")
                return {'success': True, 'action': 'completed'}
            
            # 撤销TWAP委托
            self._cancel_order(current_order['algo_id'], inst_id)
            
            # 创建追逐限价委托
            result = self.trade_executor.execute_chase_limit_order(
                inst_id=inst_id,
                side=current_order['direction'],
                amount=remaining_qty
            )
            
            if result.get('success'):
                # 更新记录
                self._record_order(inst_id, period, result['algo_id'],
                                 OrderStrategy.CHASE_LIMIT,
                                 current_order['direction'], remaining_qty)
                logger.info(f"成功转换为追逐限价委托: {result['algo_id']}")
            
            return result
            
        except Exception as e:
            logger.error(f"转换委托策略异常: {e}")
            return {'success': False, 'error': str(e)}
    
    def get_active_order(self, inst_id: str, period: str) -> Optional[Dict]:
        """获取活动委托单"""
        if inst_id in self.active_orders:
            return self.active_orders[inst_id].get(period)
        return None
    
    def _record_order(self, inst_id: str, period: str, algo_id: str,
                     strategy: OrderStrategy, direction: str, amount: float):
        """记录委托单"""
        if inst_id not in self.active_orders:
            self.active_orders[inst_id] = {}
        
        order_info = {
            'algo_id': algo_id,
            'strategy': strategy.value,
            'direction': direction,
            'amount': amount,
            'create_time': datetime.now().isoformat(),
            'status': 'live'
        }
        
        self.active_orders[inst_id][period] = order_info
        
        # 添加到历史记录
        self.order_history.append({
            'inst_id': inst_id,
            'period': period,
            **order_info
        })
    
    def _cancel_order(self, algo_id: str, inst_id: str) -> bool:
        """撤销委托单"""
        success = self.trade_executor.cancel_algo_order(algo_id, inst_id)
        
        if success:
            # 更新状态
            for inst in self.active_orders:
                for period in self.active_orders[inst]:
                    if self.active_orders[inst][period].get('algo_id') == algo_id:
                        self.active_orders[inst][period]['status'] = 'cancelled'
                        # 从活动列表移除
                        del self.active_orders[inst][period]
                        break
        
        return success
    
    def update_order_status(self):
        """更新所有活动委托单状态"""
        try:
            for inst_id in list(self.active_orders.keys()):
                for period in list(self.active_orders[inst_id].keys()):
                    order = self.active_orders[inst_id][period]
                    
                    # 获取最新状态
                    details = self.trade_executor.get_algo_order_details(order['algo_id'])
                    if details:
                        new_status = details.get('state', 'unknown')
                        
                        # 更新状态
                        order['status'] = new_status
                        
                        # 如果已完成或已撤销，从活动列表移除
                        if new_status in ['filled', 'cancelled', 'order_failed']:
                            del self.active_orders[inst_id][period]
                            logger.info(f"委托单 {order['algo_id']} 状态: {new_status}")
                    
        except Exception as e:
            logger.error(f"更新委托单状态异常: {e}")
    
    def get_summary(self) -> Dict:
        """获取委托管理摘要"""
        summary = {
            'active_orders_count': sum(len(periods) for periods in self.active_orders.values()),
            'active_orders': self.active_orders,
            'total_history': len(self.order_history),
            'last_update': datetime.now().isoformat()
        }
        
        return summary
