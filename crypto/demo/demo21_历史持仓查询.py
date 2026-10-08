#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 21 - 历史持仓信息查询
=================================

功能说明：
- 查询账户历史持仓信息
- 支持多种筛选条件和分页查询
- 统计分析持仓数据和盈亏情况

API接口：获取历史持仓信息 (get_positions_history)
API文档：https://www.okx.com/docs-v5/en/#rest-api-account-get-positions-history

请求参数说明：
参数名      类型     是否必须  描述
instType   String   否       产品类型
                              MARGIN：币币杠杆
                              SWAP：永续合约
                              FUTURES：交割合约
                              OPTION：期权
instId     String   否       交易产品ID，如：BTC-USD-SWAP
mgnMode    String   否       保证金模式
                              cross：全仓
                              isolated：逐仓
type       String   否       最近一次平仓的类型
                              1：部分平仓
                              2：完全平仓
                              3：强平
                              4：强减
                              5：ADL自动减仓
posId      String   否       持仓ID
                              存在有效期的属性，自最近一次完全平仓算起，满30天 posId 会失效
after      String   否       查询仓位更新 (uTime) 之前的内容，值为时间戳，Unix 时间戳为毫秒数格式
before     String   否       查询仓位更新 (uTime) 之后的内容，值为时间戳，Unix 时间戳为毫秒数格式
limit      String   否       分页返回结果的数量，最大为100，默认100条

返回参数说明：
参数名          类型     描述
instType       String   产品类型
instId         String   交易产品ID
mgnMode        String   保证金模式 (cross：全仓, isolated：逐仓)
type           String   最近一次平仓的类型
                         1：部分平仓
                         2：完全平仓
                         3：强平
                         4：强减
                         5：ADL自动减仓
cTime          String   仓位创建时间，Unix时间戳的毫秒数格式
uTime          String   仓位更新时间，Unix时间戳的毫秒数格式
openAvgPx      String   开仓均价
closeAvgPx     String   平仓均价
posId          String   仓位ID
closeTotalPos  String   累计平仓量
pnlRatio       String   已实现收益率
pnl            String   已实现收益
lever          String   杠杆倍数
direction      String   持仓方向
                         long：多
                         short：空
triggerPx      String   触发价格，适用于交割合约、期权和指数多空策略
pnlRatioLastPx String   以最新成交价格计算的已实现收益率
pnlLastPx      String   以最新成交价格计算的已实现收益
settledPnl     String   已结算收益，仅适用于交割全仓模式
maxPos         String   最大持仓量
liqPenalty     String   强平罚金
fee            String   手续费
fundingFee     String   资金费用
pos            String   最新持仓量

作者：OKX API Demo
创建时间：2024-12-23
版本：v1.0
"""

import okx.Account as Account
import datetime
import json

# =============================================================================
# API 配置区域
# =============================================================================

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
print("📈 OKX历史持仓信息查询工具")
print("=" * 80)
print_config_info(config)
print("=" * 80)

# =============================================================================
# 初始化API客户端
# =============================================================================

try:
    # 创建账户API实例
    accountAPI = Account.AccountAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 账户API客户端初始化成功")
    
except Exception as e:
    print(f"❌ API客户端初始化失败: {e}")
    exit(1)

# =============================================================================
# 辅助函数
# =============================================================================

def timestamp_to_beijing_time(timestamp_ms):
    """将毫秒时间戳转换为北京时间（东八区）"""
    if not timestamp_ms:
        return "N/A"
    try:
        timestamp_s = int(timestamp_ms) / 1000
        utc_time = datetime.datetime.utcfromtimestamp(timestamp_s)
        beijing_time = utc_time + datetime.timedelta(hours=8)
        return beijing_time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        return f"转换错误: {e}"

# =============================================================================
# 查询历史持仓信息
# =============================================================================

try:
    print("\n🔍 正在查询历史持仓信息...")
    
    # 调用get_positions_history()方法查询历史持仓信息
    # 
    # 请求参数说明：
    # 参数      类型     是否必须  描述
    # instType  String   否       产品类型 (MARGIN：币币杠杆, SWAP：永续合约, FUTURES：交割合约, OPTION：期权)
    # instId    String   否       交易产品ID，如：BTC-USD-SWAP
    # mgnMode   String   否       保证金模式 (cross：全仓，isolated：逐仓)
    # type      String   否       最近一次平仓的类型 (1：部分平仓;2：完全平仓;3：强平;4：强减;5：ADL自动减仓)
    # posId     String   否       持仓ID (存在有效期的属性，自最近一次完全平仓算起，满30天 posId 会失效)
    # after     String   否       查询仓位更新 (uTime) 之前的内容，值为时间戳，Unix 时间戳为毫秒数格式
    # before    String   否       查询仓位更新 (uTime) 之后的内容，值为时间戳，Unix 时间戳为毫秒数格式
    # limit     String   否       分页返回结果的数量，最大为100，默认100条
    #
    # 返回参数说明：
    # instType        String   产品类型
    # instId          String   交易产品ID
    # mgnMode         String   保证金模式 (cross：全仓, isolated：逐仓)
    # type            String   最近一次平仓的类型 (1：部分平仓, 2：完全平仓, 3：强平, 4：强减, 5：ADL自动减仓)
    # cTime           String   仓位创建时间
    # uTime           String   仓位更新时间
    # openAvgPx       String   开仓均价
    # closeAvgPx      String   平仓均价
    # posId           String   仓位ID
    # closeTotalPos   String   累计平仓量
    # pnlRatio        String   已实现收益率
    # pnl             String   已实现收益
    # lever           String   杠杆倍数
    # direction       String   持仓方向 (long：多, short：空)
    
    result = accountAPI.get_positions_history(limit="50")
    
    # 检查API调用是否成功
    if result.get('code') != '0':
        print(f"❌ 查询失败: {result.get('msg', '未知错误')}")
        exit(1)
        
    print("✅ 查询成功！")
    
except Exception as e:
    print(f"❌ 查询历史持仓信息时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析和显示结果
# =============================================================================

try:
    # 原始数据输出（可选，用于调试）
    print(f"\n📋 原始返回数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 检查是否有数据返回
    if not result.get('data') or len(result['data']) == 0:
        print("\n⚠️  暂无历史持仓数据")
        exit(0)
    
    # 提取持仓数据
    positions_data = result['data']
    
    # 按更新时间排序（降序）
    positions_data.sort(key=lambda x: x.get('uTime', '0'), reverse=True)

    # =============================================================================
    # 美化输出结果
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("📈 历史持仓详情")
    print("=" * 60)
    
    print(f"📊 总记录数: {len(positions_data)} 条")
    
    # 平仓类型映射
    close_type_map = {
        '1': '部分平仓',
        '2': '完全平仓', 
        '3': '强平',
        '4': '强减',
        '5': 'ADL自动减仓'
    }
    
    # 方向映射
    direction_map = {
        'long': '多头',
        'short': '空头'
    }
    
    print("\n" + "=" * 140)
    print("🏆 持仓记录详情:")
    print("=" * 140)
    print(f"{'合约ID':<18} {'方向':<6} {'杠杆':<6} {'平仓类型':<12} {'累计平仓量':<12} {'开仓均价':<12} {'平仓均价':<12} {'收益率(%)':<12} {'盈亏':<12} {'更新时间':<20}")
    print("-" * 140)
    
    # 输出每条记录
    for position in positions_data:
        instId = position.get('instId', 'N/A')
        direction = direction_map.get(position.get('direction', 'N/A'), position.get('direction', 'N/A'))
        lever = position.get('lever', 'N/A')
        close_type = close_type_map.get(position.get('type', 'N/A'), position.get('type', 'N/A'))
        closeTotalPos = position.get('closeTotalPos', 'N/A')
        openAvgPx = position.get('openAvgPx', 'N/A')
        closeAvgPx = position.get('closeAvgPx', 'N/A')
        pnlRatio = position.get('pnlRatio', 'N/A')
        
        # 如果pnlRatio存在且不是N/A，将其转换为百分比格式
        if pnlRatio != 'N/A':
            try:
                pnlRatio_float = float(pnlRatio) * 100
                pnlRatio = f"{pnlRatio_float:.2f}"
            except:
                pass
                
        pnl = position.get('pnl', 'N/A')
        update_time = timestamp_to_beijing_time(position.get('uTime', ''))
        
        print(f"{instId:<18} {direction:<6} {lever:<6} {close_type:<12} {closeTotalPos:<12} {openAvgPx:<12} {closeAvgPx:<12} {pnlRatio:<12} {pnl:<12} {update_time:<20}")
    
    # =============================================================================
    # 统计分析
    # =============================================================================
    
    print("\n" + "=" * 60)
    print("📊 统计分析")
    print("=" * 60)
    
    # 按合约ID分组统计
    instId_counts = {}
    direction_counts = {}
    type_counts = {}
    total_pnl = 0
    profitable_trades = 0
    total_trades = 0
    
    for position in positions_data:
        # 合约ID统计
        instId = position.get('instId', 'N/A')
        instId_counts[instId] = instId_counts.get(instId, 0) + 1
        
        # 方向统计
        direction = position.get('direction', 'N/A')
        direction_counts[direction] = direction_counts.get(direction, 0) + 1
        
        # 平仓类型统计
        close_type = position.get('type', 'N/A')
        type_counts[close_type] = type_counts.get(close_type, 0) + 1
        
        # 盈亏统计
        pnl = position.get('pnl', '0')
        try:
            pnl_float = float(pnl)
            total_pnl += pnl_float
            total_trades += 1
            if pnl_float > 0:
                profitable_trades += 1
        except:
            pass
    
    # 显示统计结果
    print("\n🔸 按合约ID统计:")
    for instId, count in sorted(instId_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"   {instId}: {count} 条记录")
    
    print("\n🔸 按方向统计:")
    for direction, count in direction_counts.items():
        direction_name = direction_map.get(direction, direction)
        print(f"   {direction_name}: {count} 条记录")
    
    print("\n🔸 按平仓类型统计:")
    for close_type, count in type_counts.items():
        type_name = close_type_map.get(close_type, close_type)
        print(f"   {type_name}: {count} 条记录")
    
    # 盈亏统计
    print(f"\n💰 总盈亏: {total_pnl:.6f}")
    if total_trades > 0:
        win_rate = (profitable_trades / total_trades) * 100
        print(f"🎯 胜率: {win_rate:.2f}% ({profitable_trades}/{total_trades})")

    print("=" * 60)
    print("✅ 历史持仓信息查询完成！")
    
    # =============================================================================
    # 风险提醒和使用说明
    # =============================================================================
    
    print(f"\n⚠️  风险提醒:")
    print(f"   - 当前运行在 {'实盘模式' if flag == '0' else '模拟盘模式'}")
    print(f"   - 历史数据仅供参考，不代表未来表现")
    print(f"   - 请注意风险控制，合理设置止损")
    
    print(f"\n💡 使用说明:")
    print(f"   - 可通过修改查询参数筛选特定条件的持仓")
    print(f"   - 支持按产品类型、交易对、保证金模式等筛选")
    print(f"   - 数据按更新时间降序排列，显示最新的持仓记录")
    print(f"   - 统计数据包含胜率、总盈亏等关键指标")
    
except Exception as e:
    print(f"❌ 解析结果时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 更多API功能请参考官方文档: https://www.okx.com/docs-v5/") 