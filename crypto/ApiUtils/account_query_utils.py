#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API 账户查询工具集
===================

功能说明：
- 提供账户相关查询的工具方法
- 包含持仓信息查询、历史持仓查询、资产估值查询
- 所有方法都包含详细的API参数注释和返回数据处理
- 从api_config.py自动读取API配置信息

包含方法：
1. get_current_positions() - 获取当前持仓信息
2. get_positions_history() - 获取历史持仓信息  
3. get_asset_valuation() - 获取账户资产估值
4. get_orders_history() - 获取历史订单信息

作者：OKX API Utils
创建时间：2025-06-27
版本：v1.0
"""

import okx.Account as Account
import okx.Funding as Funding
import okx.Trade as Trade
import datetime
import json
from typing import Dict, List, Optional, Tuple

# 导入API配置
try:
    from .api_config import get_api_config, validate_config, print_config_info
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from api_config import get_api_config, validate_config, print_config_info


class AccountQueryUtils:
    """账户查询工具类"""
    
    def __init__(self, apikey: str = None, secretkey: str = None, passphrase: str = None, flag: str = None):
        """
        初始化账户查询工具
        
        Args:
            apikey: API密钥 (可选，不传时从api_config.py读取)
            secretkey: 密钥 (可选，不传时从api_config.py读取)
            passphrase: 口令 (可选，不传时从api_config.py读取)
            flag: 交易环境标识 (可选，不传时从api_config.py读取)
        """
        # 如果没有传入参数，从配置文件读取
        if not all([apikey, secretkey, passphrase]):
            config = get_api_config()
            
            # 验证配置
            is_valid, message = validate_config(config)
            if not is_valid:
                raise ValueError(f"API配置错误: {message}")
            
            self.apikey = config['api_key']
            self.secretkey = config['secret_key']
            self.passphrase = config['passphrase']
            self.flag = config['flag']
        else:
            # 使用传入的参数
            self.apikey = apikey
            self.secretkey = secretkey
            self.passphrase = passphrase
            self.flag = flag or "0"
        
        # 初始化API客户端
        self.accountAPI = Account.AccountAPI(self.apikey, self.secretkey, self.passphrase, False, self.flag)
        self.fundingAPI = Funding.FundingAPI(self.apikey, self.secretkey, self.passphrase, False, self.flag)
        self.tradeAPI = Trade.TradeAPI(self.apikey, self.secretkey, self.passphrase, False, self.flag)
    
    def get_current_positions(self, instType: str = None, instId: str = None, posId: str = None, 
                            verbose: bool = True) -> Dict:
        """获取当前持仓信息"""
        if verbose:
            print("🔍 正在查询当前持仓信息...")
        
        try:
            # 构建请求参数
            params = {}
            if instType:
                params['instType'] = instType
            if instId:
                params['instId'] = instId
            if posId:
                params['posId'] = posId
            
            # 调用API
            result = self.accountAPI.get_positions(**params)
            
            if result.get('code') != '0':
                return {
                    'success': False,
                    'message': f"API调用失败: {result.get('msg', '未知错误')}",
                    'data': [],
                    'statistics': {},
                    'risk_warnings': []
                }
            
            positions_data = result.get('data', [])
            
            # 统计分析
            statistics = {
                'total_positions': len(positions_data),
                'cross_positions': len([p for p in positions_data if p.get('mgnMode') == 'cross']),
                'isolated_positions': len([p for p in positions_data if p.get('mgnMode') == 'isolated']),
                'total_upl': sum([float(p.get('upl', '0')) for p in positions_data if p.get('upl')])
            }
            
            # 风险检查
            risk_warnings = []
            for pos in positions_data:
                inst_id = pos.get('instId', '')
                mgn_ratio = pos.get('mgnRatio', '0')
                adl = pos.get('adl', '')
                
                try:
                    if float(mgn_ratio) < 5:
                        risk_warnings.append(f"⚠️ {inst_id} 维持保证金率过低({mgn_ratio}%)")
                    if adl and int(adl) >= 4:
                        risk_warnings.append(f"⚠️ {inst_id} ADL信号区过高({adl})")
                except:
                    pass
            
            if verbose:
                print(f"✅ 查询成功，共 {len(positions_data)} 条持仓记录")
                if risk_warnings:
                    print("⚠️ 风险警告:")
                    for warning in risk_warnings:
                        print(f"   {warning}")
            
            return {
                'success': True,
                'message': f"查询成功，共 {len(positions_data)} 条持仓记录",
                'data': positions_data,
                'statistics': statistics,
                'risk_warnings': risk_warnings
            }
            
        except Exception as e:
            error_msg = f"查询持仓信息时发生错误: {e}"
            if verbose:
                print(f"❌ {error_msg}")
            return {
                'success': False,
                'message': error_msg,
                'data': [],
                'statistics': {},
                'risk_warnings': []
            }
    
    def get_positions_history(self, instType: str = None, instId: str = None, 
                            mgnMode: str = None, close_type: str = None,
                            posId: str = None, after: str = None, 
                            before: str = None, limit: str = "50",
                            verbose: bool = True) -> Dict:
        """获取历史持仓信息"""
        if verbose:
            print("🔍 正在查询历史持仓信息...")
        
        try:
            # 构建请求参数
            params = {'limit': limit}
            if instType:
                params['instType'] = instType
            if instId:
                params['instId'] = instId
            if mgnMode:
                params['mgnMode'] = mgnMode
            if close_type:
                params['type'] = close_type
            if posId:
                params['posId'] = posId
            if after:
                params['after'] = after
            if before:
                params['before'] = before
            
            # 调用API
            result = self.accountAPI.get_positions_history(**params)
            
            if result.get('code') != '0':
                return {
                    'success': False,
                    'message': f"API调用失败: {result.get('msg', '未知错误')}",
                    'data': [],
                    'statistics': {}
                }
            
            positions_data = result.get('data', [])
            
            # 统计分析
            total_pnl = 0
            profitable_trades = 0
            total_trades = 0
            
            for pos in positions_data:
                pnl = pos.get('pnl', '0')
                try:
                    pnl_float = float(pnl)
                    total_pnl += pnl_float
                    total_trades += 1
                    if pnl_float > 0:
                        profitable_trades += 1
                except:
                    pass
            
            win_rate = (profitable_trades / total_trades * 100) if total_trades > 0 else 0
            
            statistics = {
                'total_records': len(positions_data),
                'total_pnl': total_pnl,
                'win_rate': win_rate,
                'profitable_trades': profitable_trades,
                'total_trades': total_trades
            }
            
            if verbose:
                print(f"✅ 查询成功，共 {len(positions_data)} 条历史记录")
                print(f"   总盈亏: {total_pnl:.6f}")
                print(f"   胜率: {win_rate:.2f}%")
            
            return {
                'success': True,
                'message': f"查询成功，共 {len(positions_data)} 条历史记录",
                'data': positions_data,
                'statistics': statistics
            }
            
        except Exception as e:
            error_msg = f"查询历史持仓信息时发生错误: {e}"
            if verbose:
                print(f"❌ {error_msg}")
            return {
                'success': False,
                'message': error_msg,
                'data': [],
                'statistics': {}
            }
    
    def get_asset_valuation(self, ccy: str = "USDT", verbose: bool = True) -> Dict:
        """获取账户资产估值"""
        if verbose:
            print("🔍 正在查询账户资产估值...")
        
        try:
            # 调用API
            result = self.fundingAPI.get_asset_valuation(ccy=ccy)
            
            if result.get('code') != '0':
                return {
                    'success': False,
                    'message': f"API调用失败: {result.get('msg', '未知错误')}",
                    'data': {},
                    'statistics': {}
                }
            
            if not result.get('data') or len(result['data']) == 0:
                return {
                    'success': False,
                    'message': "暂无资产估值数据",
                    'data': {},
                    'statistics': {}
                }
            
            asset_data = result['data'][0]
            total_bal = asset_data.get('totalBal', '0')
            ts = asset_data.get('ts', '0')
            details = asset_data.get('details', {})
            
            # 时间戳转换
            if ts != '0':
                ts_date = datetime.datetime.fromtimestamp(int(ts) / 1000).strftime('%Y-%m-%d %H:%M:%S')
            else:
                ts_date = "未知"
            
            processed_data = {
                'totalBal': total_bal,
                'ts_date': ts_date,
                'details': details,
                'ccy': ccy
            }
            
            if verbose:
                print(f"✅ 查询成功，总资产估值: {total_bal} {ccy}")
                print(f"   更新时间: {ts_date}")
            
            return {
                'success': True,
                'message': f"查询成功，总资产估值: {total_bal} {ccy}",
                'data': processed_data,
                'statistics': {}
            }
            
        except Exception as e:
            error_msg = f"查询资产估值时发生错误: {e}"
            if verbose:
                print(f"❌ {error_msg}")
            return {
                'success': False,
                'message': error_msg,
                'data': {},
                'statistics': {}
            }

    def get_orders_history(self, instType: str = "SWAP", instId: str = None, 
                          ordType: str = None, state: str = None, category: str = None,
                          after: str = None, before: str = None, begin: str = None, 
                          end: str = None, limit: str = "50", verbose: bool = True) -> Dict:
        """获取历史订单信息"""
        if verbose:
            print("🔍 正在查询历史订单信息...")
        
        try:
            # 构建请求参数
            params = {'instType': instType, 'limit': limit}
            if instId:
                params['instId'] = instId
            if ordType:
                params['ordType'] = ordType
            if state:
                params['state'] = state
            if category:
                params['category'] = category
            if after:
                params['after'] = after
            if before:
                params['before'] = before
            if begin:
                params['begin'] = begin
            if end:
                params['end'] = end
            
            # 调用API
            result = self.tradeAPI.get_orders_history(**params)
            
            if result.get('code') != '0':
                return {
                    'success': False,
                    'message': f"API调用失败: {result.get('msg', '未知错误')}",
                    'data': [],
                    'statistics': {}
                }
            
            orders_data = result.get('data', [])
            
            # 统计分析
            total_orders = len(orders_data)
            filled_orders = [o for o in orders_data if o.get('state') == 'filled']
            canceled_orders = [o for o in orders_data if o.get('state') == 'canceled']
            buy_orders = [o for o in orders_data if o.get('side') == 'buy']
            sell_orders = [o for o in orders_data if o.get('side') == 'sell']
            
            # 计算交易量统计
            def safe_float(value, default=0.0):
                try:
                    if value is None or value == '' or value == 'None':
                        return default
                    return float(value)
                except (ValueError, TypeError):
                    return default
            
            total_order_size = sum([safe_float(order.get('sz', '0')) for order in orders_data])
            total_filled_size = sum([safe_float(order.get('accFillSz', '0')) for order in orders_data])
            
            # 计算成交金额
            total_turnover = 0
            for order in filled_orders:
                avg_px = safe_float(order.get('avgPx', '0'))
                acc_fill_sz = safe_float(order.get('accFillSz', '0'))
                if avg_px > 0 and acc_fill_sz > 0:
                    turnover = avg_px * acc_fill_sz
                    total_turnover += turnover
            
            # 计算总手续费
            total_fee = sum([safe_float(order.get('fee', '0')) for order in orders_data if order.get('fee')])
            
            statistics = {
                'total_orders': total_orders,
                'filled_orders': len(filled_orders),
                'canceled_orders': len(canceled_orders),
                'buy_orders': len(buy_orders),
                'sell_orders': len(sell_orders),
                'total_order_size': total_order_size,
                'total_filled_size': total_filled_size,
                'total_turnover': total_turnover,
                'total_fee': total_fee,
                'fill_rate': (total_filled_size / total_order_size * 100) if total_order_size > 0 else 0
            }
            
            if verbose:
                print(f"✅ 查询成功，共 {total_orders} 条历史订单")
                print(f"   成交订单: {len(filled_orders)} 条")
                print(f"   撤销订单: {len(canceled_orders)} 条")
                print(f"   总成交金额: {total_turnover:.2f} USDT")
            
            return {
                'success': True,
                'message': f"查询成功，共 {total_orders} 条历史订单",
                'data': orders_data,
                'statistics': statistics
            }
            
        except Exception as e:
            error_msg = f"查询历史订单时发生错误: {e}"
            if verbose:
                print(f"❌ {error_msg}")
            return {
                'success': False,
                'message': error_msg,
                'data': [],
                'statistics': {}
            }


# =============================================================================
# 便捷函数 - 自动读取配置
# =============================================================================

def query_current_positions(**kwargs) -> Dict:
    """便捷函数：查询当前持仓信息（自动读取配置）"""
    utils = AccountQueryUtils()  # 自动读取配置
    return utils.get_current_positions(**kwargs)


def query_positions_history(**kwargs) -> Dict:
    """便捷函数：查询历史持仓信息（自动读取配置）"""
    utils = AccountQueryUtils()  # 自动读取配置
    return utils.get_positions_history(**kwargs)


def query_asset_valuation(**kwargs) -> Dict:
    """便捷函数：查询资产估值（自动读取配置）"""
    utils = AccountQueryUtils()  # 自动读取配置
    return utils.get_asset_valuation(**kwargs)


def query_orders_history(**kwargs) -> Dict:
    """便捷函数：查询历史订单（自动读取配置）"""
    utils = AccountQueryUtils()  # 自动读取配置
    return utils.get_orders_history(**kwargs)


# 保留手动配置函数（向后兼容）
def query_current_positions_manual(apikey: str, secretkey: str, passphrase: str, flag: str = "0", **kwargs) -> Dict:
    """便捷函数：查询当前持仓信息（手动传入配置）"""
    utils = AccountQueryUtils(apikey, secretkey, passphrase, flag)
    return utils.get_current_positions(**kwargs)


def query_positions_history_manual(apikey: str, secretkey: str, passphrase: str, flag: str = "0", **kwargs) -> Dict:
    """便捷函数：查询历史持仓信息（手动传入配置）"""
    utils = AccountQueryUtils(apikey, secretkey, passphrase, flag)
    return utils.get_positions_history(**kwargs)


def query_asset_valuation_manual(apikey: str, secretkey: str, passphrase: str, flag: str = "0", **kwargs) -> Dict:
    """便捷函数：查询资产估值（手动传入配置）"""
    utils = AccountQueryUtils(apikey, secretkey, passphrase, flag)
    return utils.get_asset_valuation(**kwargs)


# =============================================================================
# 示例用法
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🔧 OKX 账户查询工具示例")
    print("=" * 60)
    
    try:
        # 获取并验证配置
        config = get_api_config()
        is_valid, message = validate_config(config)
        
        if not is_valid:
            print(f"❌ API配置错误: {message}")
            print("💡 请检查 api_config.py 文件中的配置")
            exit(1)
        
        print("✅ API配置验证通过")
        print_config_info(config)
        
        # 创建工具实例（自动读取配置）
        utils = AccountQueryUtils()
        
        # 示例1：查询当前持仓
        print("\n1. 查询当前持仓信息")
        print("-" * 30)
        positions_result = utils.get_current_positions()
        print(f"结果: {positions_result['message']}")
        
        # 示例2：查询历史持仓
        print("\n2. 查询历史持仓信息")
        print("-" * 30)
        history_result = utils.get_positions_history(limit="10")
        print(f"结果: {history_result['message']}")
        
        # 示例3：查询资产估值
        print("\n3. 查询资产估值")
        print("-" * 30)
        asset_result = utils.get_asset_valuation(ccy="USDT")
        print(f"结果: {asset_result['message']}")
        
        # 示例4：查询历史订单
        print("\n4. 查询历史订单信息")
        print("-" * 30)
        orders_result = utils.get_orders_history(instType="SWAP", limit="10")
        print(f"结果: {orders_result['message']}")
        
        print("\n✅ 示例演示完成！")
        
        # 示例5：使用便捷函数
        print("\n5. 使用便捷函数查询")
        print("-" * 30)
        quick_result = query_current_positions(verbose=False)
        print(f"便捷函数结果: {quick_result['message']}")
        
    except Exception as e:
        print(f"❌ 程序运行出错: {e}")
        print("💡 请检查网络连接和API配置") 