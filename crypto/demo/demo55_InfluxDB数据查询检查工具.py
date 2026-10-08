#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
InfluxDB数据查询检查工具
======================

功能：
1. 查询NEAR-USDT-SWAP的K线数据
2. 查询NEAR-USDT-SWAP的技术指标数据
3. 格式化输出，便于检查数据正确性
4. 支持按时间周期过滤查询

作者：AI Assistant
创建时间：2025年1月
"""

import sys
import os
from datetime import datetime, timezone
import time

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入配置
from influxdb_config import InfluxDBConfig

# 导入InfluxDB客户端
try:
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.query_api import QueryApi
    print("✅ InfluxDB 2.x 客户端库导入成功")
except ImportError as e:
    print("❌ InfluxDB客户端库导入失败")
    print("请安装: pip install influxdb-client")
    sys.exit(1)

class InfluxDBDataViewer:
    """InfluxDB数据查询检查工具类"""
    
    def __init__(self):
        print("🔧 初始化InfluxDB数据查询工具...")
        
        # 使用配置文件中的InfluxDB设置
        print("📋 加载InfluxDB配置...")
        self.config = InfluxDBConfig()
        self.url = self.config.URL
        self.token = self.config.TOKEN
        self.org = self.config.ORG
        self.bucket = self.config.DEFAULT_BUCKETS[0]  # 使用第一个桶 "garble"
        print(f"✅ InfluxDB配置加载完成")
        print(f"   URL: {self.url}")
        print(f"   组织: {self.org}")
        print(f"   目标桶: {self.bucket}")
        
        # 数据表配置
        self.kline_measurement = "near_usdt_swap_kline"      # K线数据表
        self.indicator_measurement = "near_usdt_swap_indicators"  # 技术指标数据表
        self.inst_id = "NEAR-USDT"
        self.time_periods = ["5m", "15m", "1H", "4H", "1D", "1W"]
        
        # InfluxDB客户端
        self.client = None
        self.query_api = None
        
        print(f"🔧 数据查询工具初始化完成")
        print(f"   K线数据表: {self.kline_measurement}")
        print(f"   指标数据表: {self.indicator_measurement}")
        print(f"   合约代码: {self.inst_id}")
        
    def connect_influxdb(self):
        """连接到InfluxDB"""
        try:
            print(f"\n🔗 正在连接InfluxDB...")
            
            # 使用配置文件创建客户端
            client_config = self.config.get_client_config()
            self.client = InfluxDBClient(**client_config)
            print("✅ InfluxDB客户端创建成功")
            
            # 测试连接
            print("🔍 测试InfluxDB连接...")
            health = self.client.health()
            print(f"✅ InfluxDB连接成功")
            print(f"   状态: {health.status}")
            print(f"   版本: {health.version}")
            
            # 初始化API
            print("🔧 初始化InfluxDB查询API...")
            self.query_api = self.client.query_api()
            print("✅ InfluxDB查询API初始化完成")
            
            return True
            
        except Exception as e:
            print(f"❌ InfluxDB连接失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def query_kline_data(self, period_filter=None, limit=10):
        """查询K线数据"""
        try:
            print(f"\n📊 查询K线数据...")
            print(f"   数据表: {self.kline_measurement}")
            print(f"   桶名: {self.bucket}")
            
            # 构建查询条件
            period_condition = ""
            if period_filter:
                period_condition = f'|> filter(fn: (r) => r.period == "{period_filter}")'
                print(f"   周期过滤: {period_filter}")
            else:
                print(f"   周期过滤: 无（查询所有周期）")
            
            # Flux查询语句
            query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -7d)
                |> filter(fn: (r) => r._measurement == "{self.kline_measurement}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                {period_condition}
                |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"], desc: true)
                |> limit(n: {limit})
            '''
            
            print(f"🔍 执行查询（最近{limit}条）...")
            query_start = time.time()
            result = self.query_api.query(org=self.org, query=query)
            query_time = time.time() - query_start
            
            print(f"✅ 查询完成，耗时: {query_time:.2f}秒")
            
            records = []
            for table in result:
                for record in table.records:
                    records.append(record)
            
            if records:
                print(f"✅ 查询到 {len(records)} 条K线数据")
                self._display_kline_data(records)
                return True
            else:
                print(f"⚠️ 未找到K线数据")
                return False
                
        except Exception as e:
            print(f"❌ 查询K线数据失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def query_indicator_data(self, period_filter=None, limit=10):
        """查询技术指标数据"""
        try:
            print(f"\n📈 查询技术指标数据...")
            print(f"   数据表: {self.indicator_measurement}")
            print(f"   桶名: {self.bucket}")
            
            # 构建查询条件
            period_condition = ""
            if period_filter:
                period_condition = f'|> filter(fn: (r) => r.period == "{period_filter}")'
                print(f"   周期过滤: {period_filter}")
            else:
                print(f"   周期过滤: 无（查询所有周期）")
            
            # Flux查询语句
            query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -7d)
                |> filter(fn: (r) => r._measurement == "{self.indicator_measurement}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                {period_condition}
                |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"], desc: true)
                |> limit(n: {limit})
            '''
            
            print(f"🔍 执行查询（最近{limit}条）...")
            query_start = time.time()
            result = self.query_api.query(org=self.org, query=query)
            query_time = time.time() - query_start
            
            print(f"✅ 查询完成，耗时: {query_time:.2f}秒")
            
            records = []
            for table in result:
                for record in table.records:
                    records.append(record)
            
            if records:
                print(f"✅ 查询到 {len(records)} 条技术指标数据")
                self._display_indicator_data(records)
                return True
            else:
                print(f"⚠️ 未找到技术指标数据")
                return False
                
        except Exception as e:
            print(f"❌ 查询技术指标数据失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _display_kline_data(self, records):
        """格式化显示K线数据"""
        print(f"\n📊 K线数据详情:")
        print("=" * 140)
        print(f"{'时间':<20} {'周期':<6} {'开盘价':<12} {'最高价':<12} {'最低价':<12} {'收盘价':<12} {'成交量':<15} {'成交额':<15} {'确认':<6}")
        print("-" * 140)
        
        for record in records:
            time_str = record.get_time().strftime('%Y-%m-%d %H:%M:%S')
            period = record.values.get('period', 'N/A')
            open_val = record.values.get('open', 0)
            high_val = record.values.get('high', 0)
            low_val = record.values.get('low', 0)
            close_val = record.values.get('close', 0)
            volume_val = record.values.get('volume', 0)
            vol_ccy_val = record.values.get('vol_ccy', 0)
            confirm_val = record.values.get('confirm', 0)
            
            print(f"{time_str:<20} {period:<6} {open_val:<12.4f} {high_val:<12.4f} {low_val:<12.4f} {close_val:<12.4f} "
                  f"{volume_val:<15.2f} {vol_ccy_val:<15.2f} {'✅' if confirm_val == 1 else '❌':<6}")
        
        print("-" * 140)
        
        # 显示数据有效性检查
        print(f"\n🔍 数据有效性检查:")
        valid_count = 0
        price_count = 0
        for record in records:
            open_val = record.values.get('open', 0)
            high_val = record.values.get('high', 0)
            low_val = record.values.get('low', 0)
            close_val = record.values.get('close', 0)
            
            if open_val > 0 and high_val > 0 and low_val > 0 and close_val > 0:
                price_count += 1
                # 检查价格逻辑：最高价 >= 最大值(开盘,收盘) 且 最低价 <= 最小值(开盘,收盘)
                if high_val >= max(open_val, close_val) and low_val <= min(open_val, close_val):
                    valid_count += 1
        
        print(f"   有效价格记录: {price_count}/{len(records)}")
        print(f"   价格逻辑正确: {valid_count}/{len(records)}")
        
        if valid_count == len(records):
            print(f"   ✅ 所有K线数据价格逻辑正确！")
        else:
            print(f"   ⚠️ 发现 {len(records) - valid_count} 条数据价格逻辑可能有问题")
    
    def _display_indicator_data(self, records):
        """格式化显示技术指标数据"""
        print(f"\n📈 技术指标数据详情:")
        print("=" * 200)
        print(f"{'时间':<20} {'周期':<6} {'收盘价':<10} {'MACD-EMA':<12} {'MA12':<10} {'MA26':<10} "
              f"{'MACD':<10} {'RSI':<8} {'方向':<6} {'交易信号':<8} {'策略状态':<8} {'持仓盈亏':<10}")
        print("-" * 200)
        
        for record in records:
            time_str = record.get_time().strftime('%Y-%m-%d %H:%M:%S')
            period = record.values.get('period', 'N/A')
            close_val = record.values.get('close', 0)
            macd_ema_val = record.values.get('macd_ema', 0)
            ma12_val = record.values.get('ma12', 0)
            ma26_val = record.values.get('ma26', 0)
            macd_val = record.values.get('macd', 0)
            rsi_val = record.values.get('rsi', 0)
            
            # 策略相关字段
            direction_val = record.values.get('direction', '')
            deal_flag_val = record.values.get('deal_flag', 0)
            strategy_flag_val = record.values.get('strategy_flag', 0)
            current_profit_val = record.values.get('current_profit', 0)
            
            # 格式化显示
            direction_str = "📈上涨" if str(direction_val) == '1.0' else "📉下跌" if str(direction_val) == '-1.0' else "➖无"
            deal_flag_str = "🟢开多" if deal_flag_val == 1 else "🔴开空" if deal_flag_val == -1 else "⚪无"
            strategy_str = "EMA" if strategy_flag_val == 1 else "DMD-R" if strategy_flag_val == 2 else "DMD-F" if strategy_flag_val == 3 else "无"
            
            print(f"{time_str:<20} {period:<6} {close_val:<10.4f} {macd_ema_val:<12.6f} {ma12_val:<10.4f} {ma26_val:<10.4f} "
                  f"{macd_val:<10.6f} {rsi_val:<8.2f} {direction_str:<6} {deal_flag_str:<8} {strategy_str:<8} {current_profit_val:<10.2f}%")
        
        print("-" * 200)
        
        # 显示策略字段统计
        print(f"\n🔍 策略字段统计:")
        strategy_fields = ['direction', 'deal_flag', 'current_profit', 'strategy_flag', 'zero1', 'zero2']
        non_zero_strategy = {}
        
        for field in strategy_fields:
            non_zero_count = 0
            for record in records:
                val = record.values.get(field, 0)
                if field == 'direction':
                    # direction是字符串，检查是否为'1.0'或'-1.0'
                    if str(val) in ['1.0', '-1.0']:
                        non_zero_count += 1
                else:
                    # 其他字段检查是否非零
                    if val != 0:
                        non_zero_count += 1
            non_zero_strategy[field] = non_zero_count
        
        for field, count in non_zero_strategy.items():
            status = "✅" if count > 0 else "❌"
            print(f"   {field:<15}: {count}/{len(records)} 有效值 {status}")
        
        # 显示技术指标统计
        print(f"\n🔍 技术指标统计:")
        non_zero_indicators = {}
        indicator_fields = ['ma12', 'ma26', 'ema12', 'ema26', 'dif', 'dea', 'macd', 'ema', 'macd_ema', 
                           'boll_upper', 'boll_middle', 'boll_lower', 'rsi', 'sar', 'k', 'd', 'j']
        
        for field in indicator_fields:
            non_zero_count = 0
            for record in records:
                val = record.values.get(field, 0)
                if val != 0:
                    non_zero_count += 1
            non_zero_indicators[field] = non_zero_count
        
        for field, count in non_zero_indicators.items():
            status = "✅" if count > 0 else "❌"
            print(f"   {field:<12}: {count}/{len(records)} 非零值 {status}")
        
        # 显示策略信号分析
        print(f"\n🔍 策略信号分析:")
        direction_stats = {'上涨': 0, '下跌': 0, '无': 0}
        deal_stats = {'开多': 0, '开空': 0, '无信号': 0}
        strategy_stats = {'EMA': 0, 'DMD-waitRise': 0, 'DMD-waitFall': 0, '无': 0}
        
        for record in records:
            # 统计方向
            direction_val = str(record.values.get('direction', ''))
            if direction_val == '1.0':
                direction_stats['上涨'] += 1
            elif direction_val == '-1.0':
                direction_stats['下跌'] += 1
            else:
                direction_stats['无'] += 1
            
            # 统计交易信号
            deal_flag_val = record.values.get('deal_flag', 0)
            if deal_flag_val == 1:
                deal_stats['开多'] += 1
            elif deal_flag_val == -1:
                deal_stats['开空'] += 1
            else:
                deal_stats['无信号'] += 1
            
            # 统计策略状态
            strategy_flag_val = record.values.get('strategy_flag', 0)
            if strategy_flag_val == 1:
                strategy_stats['EMA'] += 1
            elif strategy_flag_val == 2:
                strategy_stats['DMD-waitRise'] += 1
            elif strategy_flag_val == 3:
                strategy_stats['DMD-waitFall'] += 1
            else:
                strategy_stats['无'] += 1
        
        print(f"   方向分布: 📈上涨={direction_stats['上涨']}, 📉下跌={direction_stats['下跌']}, ➖无={direction_stats['无']}")
        print(f"   交易信号: 🟢开多={deal_stats['开多']}, 🔴开空={deal_stats['开空']}, ⚪无信号={deal_stats['无信号']}")
        print(f"   策略状态: EMA={strategy_stats['EMA']}, DMD-R={strategy_stats['DMD-waitRise']}, DMD-F={strategy_stats['DMD-waitFall']}, 无={strategy_stats['无']}")
        
        # 检查指标合理性
        print(f"\n🔍 指标合理性检查:")
        reasonable_count = 0
        for record in records:
            rsi_val = record.values.get('rsi', 0)
            boll_upper = record.values.get('boll_upper', 0)
            boll_middle = record.values.get('boll_middle', 0)
            boll_lower = record.values.get('boll_lower', 0)
            close_val = record.values.get('close', 0)
            
            # 检查RSI是否在0-100范围内
            rsi_ok = 0 <= rsi_val <= 100
            # 检查BOLL通道顺序
            boll_ok = boll_upper >= boll_middle >= boll_lower > 0 if boll_lower > 0 else True
            
            if rsi_ok and boll_ok:
                reasonable_count += 1
        
        print(f"   指标合理性: {reasonable_count}/{len(records)}")
        if reasonable_count == len(records):
            print(f"   ✅ 所有技术指标数据合理！")
        else:
            print(f"   ⚠️ 发现 {len(records) - reasonable_count} 条数据可能不合理")
    
    def query_data_counts(self):
        """查询各数据表的数据统计"""
        try:
            print(f"\n📊 查询数据统计...")
            
            # 查询K线数据统计
            kline_query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -30d)
                |> filter(fn: (r) => r._measurement == "{self.kline_measurement}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                |> group(columns: ["period"])
                |> count()
            '''
            
            # 查询技术指标数据统计
            indicator_query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -30d)
                |> filter(fn: (r) => r._measurement == "{self.indicator_measurement}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                |> group(columns: ["period"])
                |> count()
            '''
            
            print(f"🔍 查询K线数据统计...")
            kline_result = self.query_api.query(org=self.org, query=kline_query)
            
            print(f"🔍 查询技术指标数据统计...")
            indicator_result = self.query_api.query(org=self.org, query=indicator_query)
            
            # 处理K线统计结果
            kline_stats = {}
            for table in kline_result:
                for record in table.records:
                    period = record.values.get('period', 'unknown')
                    field = record.values.get('_field', 'unknown')
                    count = record.values.get('_value', 0)
                    if period not in kline_stats:
                        kline_stats[period] = {}
                    kline_stats[period][field] = count
            
            # 处理技术指标统计结果  
            indicator_stats = {}
            for table in indicator_result:
                for record in table.records:
                    period = record.values.get('period', 'unknown')
                    field = record.values.get('_field', 'unknown')
                    count = record.values.get('_value', 0)
                    if period not in indicator_stats:
                        indicator_stats[period] = {}
                    indicator_stats[period][field] = count
            
            # 显示统计结果
            print(f"\n📊 数据统计报告:")
            print("=" * 100)
            print(f"{'周期':<8} {'K线数据条数':<15} {'指标数据条数':<15} {'状态':<10}")
            print("-" * 100)
            
            all_periods = set(list(kline_stats.keys()) + list(indicator_stats.keys()))
            
            for period in sorted(all_periods):
                # K线数据数量（取第一个字段的数量作为代表）
                kline_count = 0
                if period in kline_stats:
                    kline_count = max(kline_stats[period].values()) if kline_stats[period] else 0
                
                # 技术指标数量（取第一个字段的数量作为代表）
                indicator_count = 0
                if period in indicator_stats:
                    indicator_count = max(indicator_stats[period].values()) if indicator_stats[period] else 0
                
                status = "✅完整" if kline_count > 0 and indicator_count > 0 else "⚠️不完整"
                
                print(f"{period:<8} {kline_count:<15} {indicator_count:<15} {status:<10}")
            
            print("-" * 100)
            return True
            
        except Exception as e:
            print(f"❌ 查询数据统计失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def run_full_check(self, period_filter=None, limit=10):
        """运行完整的数据检查流程"""
        print("\n" + "="*100)
        print("🔍 InfluxDB数据检查流程")
        print("="*100)
        print(f"⏰ 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📊 合约代码: {self.inst_id}")
        print(f"📈 周期过滤: {period_filter or '全部'}")
        print(f"🔢 查询限制: {limit} 条")
        
        try:
            # 1. 连接InfluxDB
            print(f"\n🔄 步骤1: 连接InfluxDB")
            if not self.connect_influxdb():
                print(f"❌ 无法连接到InfluxDB，检查终止")
                return False
            
            # 2. 查询数据统计
            print(f"\n🔄 步骤2: 查询数据统计")
            self.query_data_counts()
            
            # 3. 查询K线数据
            print(f"\n🔄 步骤3: 查询K线数据详情")
            kline_success = self.query_kline_data(period_filter, limit)
            
            # 4. 查询技术指标数据
            print(f"\n🔄 步骤4: 查询技术指标数据详情")
            indicator_success = self.query_indicator_data(period_filter, limit)
            
            # 5. 显示总结
            print(f"\n" + "="*100)
            print("📋 数据检查总结")
            print("="*100)
            print(f"   InfluxDB连接: ✅ 成功")
            print(f"   K线数据查询: {'✅ 成功' if kline_success else '❌ 失败'}")
            print(f"   技术指标查询: {'✅ 成功' if indicator_success else '❌ 失败'}")
            
            if kline_success and indicator_success:
                print(f"✅ 数据检查完成！K线数据和技术指标数据都存在")
            elif kline_success:
                print(f"⚠️ 只有K线数据，缺少技术指标数据")
            elif indicator_success:
                print(f"⚠️ 只有技术指标数据，缺少K线数据")
            else:
                print(f"❌ 未找到任何数据")
            
            print(f"⏰ 完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            return True
            
        except Exception as e:
            print(f"❌ 数据检查流程失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def close(self):
        """关闭连接"""
        if self.client:
            self.client.close()
            print("🔒 InfluxDB连接已关闭")

def main():
    """主函数"""
    print("🎯 启动InfluxDB数据查询检查工具")
    viewer = InfluxDBDataViewer()
    
    try:
        # 配置参数
        period_filter = None  # 可以设置为 "5m", "1H" 等来过滤特定周期，None表示查询所有
        limit = 5  # 每种数据类型查询的条数
        
        print(f"\n📋 查询配置:")
        print(f"   周期过滤: {period_filter or '全部周期'}")
        print(f"   每类数据查询条数: {limit}")
        
        # 运行完整检查流程
        success = viewer.run_full_check(period_filter=period_filter, limit=limit)
        
        if success:
            print(f"\n🎉 数据检查完成！")
            print(f"💡 提示: 检查上述输出以验证数据正确性")
        else:
            print(f"\n⚠️ 数据检查失败，请检查InfluxDB连接和数据")
            
    except KeyboardInterrupt:
        print(f"\n⏹️ 用户中断程序")
    except Exception as e:
        print(f"\n💥 程序异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        viewer.close()
        print("👋 程序结束")

if __name__ == "__main__":
    main()
