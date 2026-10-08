#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 17 - 获取账户计息记录
==================================

功能说明：
- 查询账户的计息记录信息
- 包括借贷利息、资金费用等
- 支持时间范围筛选和分页查询

计息类型说明：
- 借币利息：币币杠杆和合约借币产生的利息
- 资金费用：合约交易的资金费率
- 其他费用：平台相关的利息收支

API文档：
https://www.okx.com/docs-v5/en/#rest-api-account-get-interest-accrued-data

作者：OKX API Demo
创建时间：2025-01-26
版本：v1.0
"""

import okx.Account as Account
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
print("💰 OKX账户计息记录查询工具")
print("=" * 80)
print_config_info(config)
print("=" * 80)

# =============================================================================
# 查询参数配置
# =============================================================================

print(f"🎯 查询配置:")
print(f"   查询范围: 全部计息记录")
print(f"   包含内容: 借贷利息、资金费用等")
print("=" * 80)

# =============================================================================
# 初始化账户API
# =============================================================================

try:
    # 创建账户API实例
    accountAPI = Account.AccountAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 账户API初始化成功")
    
except Exception as e:
    print(f"❌ 账户API初始化失败: {e}")
    exit(1)

# =============================================================================
# 获取计息记录
# =============================================================================

try:
    print(f"\n🔍 正在获取账户计息记录...")
    
    # 调用get_interest_accrued方法获取计息记录
    # 参数说明：
    # - type: 计息类型
    #   1: 借币计息
    #   2: 用户借币给平台的计息
    # - ccy: 币种，如BTC、ETH、USDT等
    # - after: 请求此ID之前的分页内容
    # - before: 请求此ID之后的分页内容
    # - limit: 返回结果的数量，最大100，默认100
    result = accountAPI.get_interest_accrued()
    
    print("✅ 计息记录获取成功！")
    
    # 处理返回结果
    if result.get('code') == '0' and result.get('data') is not None:
        interest_records = result.get('data', [])
        
        print(f"\n📊 计息记录概览")
        print("=" * 80)
        print(f"📈 记录总数: {len(interest_records)} 条")
        
        if interest_records:
            # 统计计息信息
            currencies = {}
            total_interest = {}
            interest_types = {}
            
            for record in interest_records:
                ccy = record.get('ccy', 'Unknown')
                interest = float(record.get('interest', '0'))
                interest_type = record.get('type', 'Unknown')
                
                # 统计币种
                currencies[ccy] = currencies.get(ccy, 0) + 1
                
                # 统计总利息（按币种）
                if ccy not in total_interest:
                    total_interest[ccy] = 0
                total_interest[ccy] += interest
                
                # 统计计息类型
                interest_types[interest_type] = interest_types.get(interest_type, 0) + 1
            
            # 显示统计信息
            print(f"\n💰 币种统计:")
            for ccy, count in sorted(currencies.items()):
                total = total_interest.get(ccy, 0)
                print(f"   {ccy}: {count} 条记录, 总利息: {total:.8f}")
            
            print(f"\n📊 计息类型分布:")
            for itype, count in interest_types.items():
                type_name = {
                    '1': '借币计息',
                    '2': '放贷计息'
                }.get(itype, f'类型{itype}')
                print(f"   {type_name}: {count} 条记录")
            
            # 显示最新计息记录详情
            print(f"\n📋 最新 10 条计息记录:")
            print("=" * 80)
            print(f"{'序号':<4} {'币种':<8} {'利息金额':<15} {'类型':<8} {'计息时间':<20} {'备注':<20}")
            print("-" * 80)
            
            for i, record in enumerate(interest_records[:10], 1):
                ccy = record.get('ccy', 'N/A')
                interest = record.get('interest', '0')
                interest_type = record.get('type', 'N/A')
                timestamp = record.get('ts', '0')
                
                # 时间格式化
                try:
                    ts_int = int(timestamp)
                    if ts_int > 0:
                        time_obj = datetime.datetime.fromtimestamp(ts_int / 1000)
                        time_str = time_obj.strftime('%m-%d %H:%M:%S')
                    else:
                        time_str = "N/A"
                except (ValueError, OSError):
                    time_str = "时间解析失败"
                
                # 格式化利息金额
                try:
                    interest_float = float(interest)
                    if interest_float >= 0:
                        interest_str = f"🟢+{interest_float:.8f}"
                    else:
                        interest_str = f"🔴{interest_float:.8f}"
                except ValueError:
                    interest_str = interest
                
                # 类型显示
                type_display = {
                    '1': '📉借币',
                    '2': '📈放贷'
                }.get(interest_type, interest_type)
                
                # 备注信息
                remark = record.get('instId', record.get('mgnMode', ''))
                if not remark:
                    remark = "常规计息"
                
                print(f"{i:<4} {ccy:<8} {interest_str:<15} {type_display:<8} {time_str:<20} {remark:<20}")
            
            if len(interest_records) > 10:
                print(f"\n💡 还有 {len(interest_records) - 10} 条记录未显示")
            
            # 详细展示一条记录的完整信息
            if interest_records:
                latest_record = interest_records[0]
                print(f"\n🔍 最新记录详细信息:")
                print("=" * 80)
                
                for key, value in latest_record.items():
                    key_display = {
                        'ccy': '币种',
                        'interest': '利息金额',
                        'type': '计息类型',
                        'ts': '时间戳',
                        'instId': '产品ID',
                        'mgnMode': '保证金模式',
                        'interestRate': '利率'
                    }.get(key, key)
                    
                    # 特殊处理时间戳
                    if key == 'ts':
                        try:
                            ts_val = int(value)
                            if ts_val > 0:
                                readable_time = datetime.datetime.fromtimestamp(ts_val / 1000)
                                value = f"{value} ({readable_time.strftime('%Y-%m-%d %H:%M:%S')})"
                        except (ValueError, OSError):
                            pass
                    
                    print(f"   {key_display}: {value}")
        
        else:
            print("📝 当前账户暂无计息记录")
            print("\n💡 可能原因:")
            print("   - 账户未进行借贷操作")
            print("   - 未产生需要计息的交易")
            print("   - 记录已过期清理")
        
    else:
        print(f"❌ 获取计息记录失败: {result.get('msg', '未知错误')}")
        
        # 常见错误处理建议
        error_code = result.get('code', '')
        if error_code == '50001':
            print("💡 可能原因: API权限不足，请检查API Key权限设置")
        elif error_code == '50004':
            print("💡 可能原因: 请求过于频繁，请稍后重试")

except Exception as e:
    print(f"❌ 获取计息记录时发生错误: {e}")

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
            print(f"... 还有 {len(result['data']) - 2} 条记录未显示")
    else:
        print("无数据可显示")
        print("原始返回数据:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
except:
    print("原始数据显示失败")

# =============================================================================
# 使用建议
# =============================================================================

print(f"\n" + "=" * 80)
print("💡 使用建议")
print("=" * 80)

print(f"1️⃣  计息记录分析:")
print(f"   - 定期检查借贷成本，优化资金使用")
print(f"   - 比较不同币种的借贷利率")
print(f"   - 关注资金费率变化趋势")

print(f"\n2️⃣  成本控制:")
print(f"   - 及时归还高利率借币")
print(f"   - 选择成本较低的融资方式")
print(f"   - 避免不必要的资金占用")

print(f"\n3️⃣  记录管理:")
print(f"   - 导出数据用于财务统计")
print(f"   - 设置计息提醒，控制成本")
print(f"   - 定期审查资金使用效率")

# =============================================================================
# 重要提醒
# =============================================================================

print(f"\n⚠️  重要提醒:")
print(f"   - 计息记录用于成本分析和财务管理")
print(f"   - 借贷操作存在利息成本，请谨慎使用")
print(f"   - 及时关注利率变化，合理安排资金")
print(f"   - 数据仅供参考，以平台实际扣费为准")
print(f"   - 当前在 {'实盘' if flag == '0' else '模拟盘'} 环境")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo02_账户余额查询.py 查看资产情况")
