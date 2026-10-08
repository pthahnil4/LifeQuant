#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
多币种量化数据管理系统 - 调度器
==============================

基于模块化架构的多币种量化数据管理系统主调度器
功能：
1. 调度demo62：多币种历史K线批量存储
2. 调度demo63：WebSocket多币种增量更新
3. 调度demo64：多币种历史指标批量计算  
4. 调度demo65：多币种数据同步校验

支持15个币种的全流程数据管理，通过命令行参数控制运行模式。
采用成熟的子模块架构，确保稳定性和可维护性。

作者：AI Assistant
创建时间：2025年1月
版本：v2.0 (模块化架构)
"""

import asyncio
import argparse
import logging
import sys
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import traceback

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入子模块
try:
    from demo62_multi_symbol_kline_storage import MultiSymbolKlineStorage
    from demo63_websocket_incremental_update import WebSocketIncrementalUpdater
    from demo64_multi_symbol_indicators import MultiSymbolIndicatorCalculator
    from demo65_multi_symbol_validation import MultiSymbolDataValidator
    print("✅ 所有子模块导入成功")
except ImportError as e:
    print("❌ 子模块导入失败")
    print(f"错误: {e}")
    print("请确保demo62~65文件存在")
    sys.exit(1)

# ==================== 全局配置 ====================

# 支持的15个币种列表
SYMBOL_LIST = [  
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",  
    "DOGE-USDT-SWAP", "ADA-USDT-SWAP", "LTC-USDT-SWAP", "NEAR-USDT-SWAP", "TRX-USDT-SWAP",  
    "BCH-USDT-SWAP", "DOT-USDT-SWAP", "UNI-USDT-SWAP", "LINK-USDT-SWAP", "TRUMP-USDT-SWAP"  
]

# 支持的时间周期
TIME_PERIODS = ["5m", "15m", "1H", "4H", "1D", "1W"]

# 默认配置
DEFAULT_CONFIG = {
    "default_days": 30,  # 默认拉取30天数据
}

# 日志配置
def setup_logging():
    """设置日志配置"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('quant_db_manager.log', encoding='utf-8')
        ]
    )

# ==================== 工具函数 ====================

def parse_symbol_list(symbols_arg: str) -> List[str]:
    """解析币种列表参数"""
    if symbols_arg.lower() == "all":
        return SYMBOL_LIST.copy()
    else:
        symbols = [s.strip().upper() for s in symbols_arg.split(',')]
        valid_symbols = []
        for symbol in symbols:
            if symbol in SYMBOL_LIST:
                valid_symbols.append(symbol)
            else:
                print(f"⚠️ 警告: 币种 {symbol} 不在支持列表中，已跳过")
        return valid_symbols

def parse_period_list(periods_arg: str) -> List[str]:
    """解析时间周期列表参数"""
    if periods_arg.lower() == "all":
        return TIME_PERIODS.copy()
    else:
        periods = [p.strip() for p in periods_arg.split(',')]
        valid_periods = []
        for period in periods:
            if period in TIME_PERIODS:
                valid_periods.append(period)
            else:
                print(f"⚠️ 警告: 时间周期 {period} 不在支持列表中，已跳过")
        return valid_periods

# ==================== 调度器主类 ====================

class QuantDBManager:
    """多币种量化数据库管理调度器"""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # 初始化子模块（延迟初始化，用时再创建）
        self.kline_storage = None
        self.ws_updater = None 
        self.indicator_calculator = None
        self.data_validator = None
    
    async def run_history_mode(self, symbols: List[str], periods: List[str], 
                              start_time: Optional[datetime], end_time: Optional[datetime]):
        """运行历史数据同步模式 - 调度demo62"""
        self.logger.info("🚀 启动历史K线数据同步模式 (调度demo62)")
        
        try:
            # 创建并使用历史K线存储器
            self.kline_storage = MultiSymbolKlineStorage()
            
            result = self.kline_storage.batch_store_history_data(
                symbols=symbols, 
                periods=periods,
                start_time=start_time,
                end_time=end_time
            )
            
            if result.get('success'):
                self.logger.info("🎉 历史数据同步完成")
            else:
                self.logger.error(f"❌ 历史数据同步失败: {result.get('error')}")
                
        except Exception as e:
            self.logger.error(f"❌ 历史数据同步异常: {e}")
            traceback.print_exc()
        finally:
            if self.kline_storage:
                self.kline_storage.close()
    
    async def run_websocket_mode(self, symbols: List[str], periods: List[str]):
        """运行WebSocket增量更新模式 - 调度demo63"""
        self.logger.info("🚀 启动WebSocket增量更新模式 (调度demo63)")
        
        try:
            # 创建并使用WebSocket增量更新器
            self.ws_updater = WebSocketIncrementalUpdater()
            
            await self.ws_updater.start_websocket_incremental(symbols, periods)
            
        except KeyboardInterrupt:
            self.logger.info("⏹️ 用户中断WebSocket连接")
        except Exception as e:
            self.logger.error(f"❌ WebSocket模式运行失败: {e}")
            traceback.print_exc()
        finally:
            if self.ws_updater:
                self.ws_updater.close()
    
    async def run_indicator_mode(self, symbols: List[str], periods: List[str]):
        """运行指标计算模式 - 调度demo64"""
        self.logger.info("🚀 启动指标计算模式 (调度demo64)")
        
        try:
            # 创建并使用指标计算器
            self.indicator_calculator = MultiSymbolIndicatorCalculator()
            
            result = self.indicator_calculator.batch_calculate_indicators(symbols, periods)
            
            if result.get('success'):
                self.logger.info("🎉 指标计算完成")
            else:
                self.logger.error(f"❌ 指标计算失败: {result.get('error')}")
                
        except Exception as e:
            self.logger.error(f"❌ 指标计算模式运行失败: {e}")
            traceback.print_exc()
        finally:
            if self.indicator_calculator:
                self.indicator_calculator.close()
    
    async def run_validate_mode(self, symbols: List[str], periods: List[str], level: str):
        """运行数据校验模式 - 调度demo65"""
        self.logger.info(f"🚀 启动数据校验模式 (调度demo65) - 级别: {level}")
        
        try:
            # 创建并使用数据校验器
            self.data_validator = MultiSymbolDataValidator()
            
            result = self.data_validator.run_validation_suite(symbols, periods, level)
            
            if result.get('success'):
                summary = result.get('summary', {})
                self.logger.info(f"🎉 数据校验完成 - 成功率: {summary.get('success_rate', 0):.1f}%")
            else:
                self.logger.error(f"❌ 数据校验失败: {result.get('error')}")
                
        except Exception as e:
            self.logger.error(f"❌ 数据校验模式运行失败: {e}")
            traceback.print_exc()
        finally:
            if self.data_validator:
                self.data_validator.close()

# ==================== 命令行接口 ====================

def create_argument_parser():
    """创建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        description='多币种量化数据管理系统 (模块化架构)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 同步所有币种的历史K线（最近30天）
  python demo61_quant_db_manager.py --mode history --symbols all
  
  # 启动WebSocket订阅（仅订阅BTC、ETH）
  python demo61_quant_db_manager.py --mode ws --symbols BTC-USDT-SWAP,ETH-USDT-SWAP
  
  # 计算指定币种的技术指标
  python demo61_quant_db_manager.py --mode indicator --symbols NEAR-USDT-SWAP --periods 5m,1H
  
  # 执行全量数据校验
  python demo61_quant_db_manager.py --mode validate --level full
        """
    )
    
    parser.add_argument('--mode', required=True, 
                       choices=['history', 'ws', 'indicator', 'validate'],
                       help='运行模式: history(历史数据), ws(WebSocket增量), indicator(指标计算), validate(数据校验)')
    
    parser.add_argument('--symbols', default='all',
                       help='币种列表，"all"表示所有币种，或使用逗号分隔的币种列表 (默认: all)')
    
    parser.add_argument('--periods', default='all',
                       help='时间周期列表，"all"表示所有周期，或使用逗号分隔的周期列表 (默认: all)')
    
    parser.add_argument('--start', type=str,
                       help='开始时间 (格式: YYYY-MM-DD, 仅history模式使用)')
    
    parser.add_argument('--end', type=str,
                       help='结束时间 (格式: YYYY-MM-DD, 仅history模式使用)')
    
    parser.add_argument('--level', default='high',
                       choices=['high', 'medium', 'full'],
                       help='校验级别 (仅validate模式使用): high(高频), medium(中频), full(全量) (默认: high)')
    
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='启用详细日志输出')
    
    return parser

async def main():
    """主函数"""
    # 设置日志
    setup_logging()
    logger = logging.getLogger('main')
    
    # 解析命令行参数
    parser = create_argument_parser()
    args = parser.parse_args()
    
    # 设置详细日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    logger.info("🎯 多币种量化数据管理系统启动 (v2.0 模块化架构)")
    logger.info("=" * 100)
    
    # 解析参数
    symbols = parse_symbol_list(args.symbols)
    periods = parse_period_list(args.periods)
    
    if not symbols:
        logger.error("❌ 没有有效的币种，程序退出")
        return
    
    if not periods:
        logger.error("❌ 没有有效的时间周期，程序退出")
        return
    
    # 解析时间参数
    start_time = None
    end_time = None
    
    if args.start:
        try:
            start_time = datetime.strptime(args.start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            logger.error(f"❌ 开始时间格式错误: {args.start}")
            return
    
    if args.end:
        try:
            end_time = datetime.strptime(args.end, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            logger.error(f"❌ 结束时间格式错误: {args.end}")
            return
    
    logger.info(f"📈 选择币种: {symbols}")
    logger.info(f"⏰ 选择周期: {periods}")
    logger.info(f"🔧 运行模式: {args.mode}")
    if start_time:
        logger.info(f"📅 开始时间: {start_time.date()}")
    if end_time:
        logger.info(f"📅 结束时间: {end_time.date()}")
    logger.info("=" * 100)
    
    # 创建管理器并运行
    manager = QuantDBManager()
    
    try:
        if args.mode == 'history':
            await manager.run_history_mode(symbols, periods, start_time, end_time)
        elif args.mode == 'ws':
            await manager.run_websocket_mode(symbols, periods)
        elif args.mode == 'indicator':
            await manager.run_indicator_mode(symbols, periods)
        elif args.mode == 'validate':
            await manager.run_validate_mode(symbols, periods, args.level)
        
    except KeyboardInterrupt:
        logger.info("\n⏹️ 用户中断程序")
    except Exception as e:
        logger.error(f"\n💥 程序运行异常: {e}")
        traceback.print_exc()
    
    logger.info("👋 程序结束")

if __name__ == "__main__":
    # 运行主程序
    asyncio.run(main())
