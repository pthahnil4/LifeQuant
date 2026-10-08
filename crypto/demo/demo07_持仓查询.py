#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 07 - 持仓查询
==========================

功能说明：
- 查询当前所有持仓
- 分析持仓盈亏
- 持仓风险评估

持仓信息包括：
- 持仓数量和方向
- 平均开仓价格
- 未实现盈亏
- 保证金信息
- 强平价格等

API文档：
https://www.okx.com/docs-v5/en/#rest-api-account-get-positions

作者：OKX API Demo
创建时间：2024-06-19
版本：v1.0

📋 持仓原始数据:
{
  "code": "0",
  "data": [
    {
      "adl": "1",
      "availPos": "",
      "avgPx": "2.704",
      "baseBal": "",
      "baseBorrowed": "",
      "baseInterest": "",
      "bePx": "2.709676538269135",
      "bizRefId": "",
      "bizRefType": "",
      "cTime": "1757403616681",
      "ccy": "USDT",
      "clSpotInUseAmt": "",
      "closeOrderAlgo": [],
      "deltaBS": "",
      "deltaPA": "",
      "fee": "-0.001352",
      "fundingFee": "-0.0029697",
      "gammaBS": "",
      "gammaPA": "",
      "idxPx": "2.721",
      "imr": "0.906",
      "instId": "NEAR-USDT-SWAP",
      "instType": "SWAP",
      "interest": "",
      "last": "2.718",
      "lever": "3",
      "liab": "",
      "liabCcy": "",
      "liqPenalty": "0",
      "liqPx": "",
      "margin": "",
      "markPx": "2.718",
      "maxSpotInUseAmt": "",
      "mgnMode": "cross",
      "mgnRatio": "385.77659693752406",
      "mmr": "0.02718",
      "nonSettleAvgPx": "",
      "notionalUsd": "2.71862514",
      "optVal": "",
      "pendingCloseOrdLiabVal": "",
      "pnl": "0",
      "pos": "0.1",
      "posCcy": "",
      "posId": "2848812101871149056",
      "posSide": "net",
      "quoteBal": "",
      "quoteBorrowed": "",
      "quoteInterest": "",
      "realizedPnl": "-0.0043217",
      "settledPnl": "",
      "spotInUseAmt": "",
      "spotInUseCcy": "",
      "thetaBS": "",
      "thetaPA": "",
      "tradeId": "174351410",
      "uTime": "1757692801150",
      "upl": "0.0139999999999998",
      "uplLastPx": "0.0139999999999998",
      "uplRatio": "0.0155325443786982",
      "uplRatioLastPx": "0.0155325443786982",
      "usdPx": "1.00023",
      "vegaBS": "",
      "vegaPA": ""
    }
  ],
  "msg": ""
}

"""

import okx.Account as Account
import datetime
import json

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

print("=" * 60)
print("📊 OKX持仓查询工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 查询参数配置
# =============================================================================

# 可选的过滤参数
INST_TYPE = ""          # 产品类型：SPOT现货, FUTURES期货, SWAP永续, OPTION期权
INST_ID = ""            # 特定产品ID，如 "LTC-USDT-SWAP"

print(f"🎯 查询配置:")
if INST_TYPE:
    print(f"   产品类型: {INST_TYPE}")
if INST_ID:
    print(f"   特定产品: {INST_ID}")
else:
    print(f"   查询范围: 所有持仓")
print("=" * 60)

# =============================================================================
# 初始化账户API
# =============================================================================

try:
    accountAPI = Account.AccountAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ 账户API初始化成功")
except Exception as e:
    print(f"❌ 账户API初始化失败: {e}")
    exit(1)

# =============================================================================
# 查询持仓信息
# =============================================================================

try:
    print(f"\n🔍 正在查询持仓信息...")
    
    # 查询持仓
    # 参数说明：
    # - instType: 产品类型（可选）
    # - instId: 产品ID（可选）
    result = accountAPI.get_positions(
        instType=INST_TYPE,
        instId=INST_ID
    )
    
    print("✅ 持仓查询成功！")
    
except Exception as e:
    print(f"❌ 查询持仓时发生错误: {e}")
    exit(1)

# =============================================================================
# 解析持仓数据
# =============================================================================

try:
    print(f"\n📋 持仓原始数据:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    code = result['code']
    
    if code == '0':
        positions_data = result['data']
        
        print("\n" + "=" * 60)
        print("📊 持仓信息总览")
        print("=" * 60)
        
        print(f"📈 持仓总数: {len(positions_data)} 个")
        
        if not positions_data:
            print("\n🎯 当前无任何持仓")
            print("   可以开始交易建立持仓")
            exit(0)
        
        # 统计变量
        total_upl = 0          # 总未实现盈亏
        total_margin = 0       # 总保证金
        profitable_count = 0   # 盈利持仓数
        loss_count = 0         # 亏损持仓数
        
        # 分类统计
        cross_positions = []   # 全仓持仓
        isolated_positions = [] # 逐仓持仓
        
        print("\n" + "=" * 80)
        print("📋 详细持仓列表")
        print("=" * 80)
        
        # 遍历所有持仓
        for i, position in enumerate(positions_data, 1):
            # 提取关键信息
            instId = position['instId']                 # 产品ID
            instType = position['instType']             # 产品类型
            mgnMode = position['mgnMode']               # 保证金模式
            pos = position['pos']                       # 持仓数量
            posSide = position['posSide']               # 持仓方向
            
            # 价格信息
            avgPx = position['avgPx']                   # 平均开仓价
            markPx = position['markPx']                 # 标记价格
            liqPx = position.get('liqPx', '')           # 强平价格
            
            # 盈亏信息
            upl = float(position.get('upl', '0'))                # 未实现盈亏
            uplRatio = float(position.get('uplRatio', '0')) * 100 # 盈亏比例
            pnl = float(position.get('pnl', '0'))              # 已实现盈亏
            
            # 保证金信息
            margin = float(position.get('imr', '0')) # 保证金（使用imr字段）
            lever = position.get('lever', '')           # 杠杆倍数
            
            # 时间信息
            uTime = position['uTime']
            update_time = datetime.datetime.fromtimestamp(
                int(uTime) / 1000
            ).strftime('%Y-%m-%d %H:%M:%S')
            
            # 显示持仓详情
            print(f"\n🔹 持仓 #{i}: {instId}")
            print(f"   📊 产品类型: {instType}")
            print(f"   🏷️  保证金模式: {'全仓' if mgnMode == 'cross' else '逐仓'}")
            print(f"   📈 持仓方向: {posSide} ({'做多' if float(pos) > 0 else '做空' if float(pos) < 0 else '无持仓'})")
            print(f"   📊 持仓数量: {pos}张")
            
            if avgPx:
                print(f"   💰 平均开仓价: ${avgPx}")
            print(f"   📊 标记价格: ${markPx}")
            if liqPx:
                print(f"   ⚠️  强平价格: ${liqPx}")
            if lever:
                print(f"   📊 杠杆倍数: {lever}倍")
            
            # 盈亏分析
            if upl != 0:
                status = "📈 盈利" if upl > 0 else "📉 亏损"
                print(f"   {status}: ${upl:.4f} ({uplRatio:+.2f}%)")
                
                if upl > 0:
                    profitable_count += 1
                else:
                    loss_count += 1
                    
                total_upl += upl
                
            if margin > 0:
                print(f"   💵 占用保证金: ${margin:.4f}")
                total_margin += margin
                
            print(f"   🕐 更新时间: {update_time}")
            
            # 分类统计
            if mgnMode == 'cross':
                cross_positions.append(position)
            else:
                isolated_positions.append(position)
        
        # =============================================================================
        # 持仓统计分析
        # =============================================================================
        
        print("\n" + "=" * 60)
        print("📊 持仓统计分析")
        print("=" * 60)
        
        print(f"📈 总体盈亏:")
        print(f"   总未实现盈亏: ${total_upl:+.4f}")
        print(f"   盈利持仓数: {profitable_count}")
        print(f"   亏损持仓数: {loss_count}")
        if len(positions_data) > 0:
            win_rate = (profitable_count / len(positions_data)) * 100
            print(f"   盈利比例: {win_rate:.1f}%")
        
        print(f"\n💰 保证金使用:")
        print(f"   总占用保证金: ${total_margin:.4f}")
        
        print(f"\n🏦 保证金模式分布:")
        print(f"   全仓持仓: {len(cross_positions)} 个")
        print(f"   逐仓持仓: {len(isolated_positions)} 个")
        
        # =============================================================================
        # 风险分析
        # =============================================================================
        
        print("\n" + "=" * 60)
        print("⚠️ 风险分析")
        print("=" * 60)
        
        # 检查高风险持仓
        high_risk_positions = []
        for position in positions_data:
            uplRatio = float(position['uplRatio']) * 100
            if uplRatio < -10:  # 亏损超过10%
                high_risk_positions.append(position)
        
        if high_risk_positions:
            print(f"🚨 高风险持仓 ({len(high_risk_positions)} 个):")
            for pos in high_risk_positions:
                instId = pos['instId']
                uplRatio = float(pos['uplRatio']) * 100
                print(f"   ⚠️ {instId}: {uplRatio:+.2f}%")
            print(f"   建议: 考虑止损或减仓")
        else:
            print(f"✅ 当前无高风险持仓")
        
        # 检查接近强平的持仓
        near_liquidation = []
        for position in positions_data:
            if position.get('liqPx'):
                markPx = float(position['markPx'])
                liqPx = float(position['liqPx'])
                pos = float(position['pos'])
                
                if pos > 0:  # 多仓
                    distance = (markPx - liqPx) / markPx * 100
                else:  # 空仓
                    distance = (liqPx - markPx) / markPx * 100
                
                if distance < 20:  # 距离强平价格小于20%
                    near_liquidation.append((position['instId'], distance))
        
        if near_liquidation:
            print(f"\n🚨 接近强平风险:")
            for instId, distance in near_liquidation:
                print(f"   ⚠️ {instId}: 距强平 {distance:.1f}%")
            print(f"   建议: 及时补充保证金或减仓")
        else:
            print(f"\n✅ 无强平风险")

        # =============================================================================
        # 操作建议
        # =============================================================================
        
        print("\n" + "=" * 60)
        print("💡 操作建议")
        print("=" * 60)
        
        if total_upl > 0:
            print(f"📈 当前整体盈利，建议:")
            print(f"   ✅ 可考虑部分止盈锁定收益")
            print(f"   ✅ 设置跟踪止损保护利润")
            print(f"   ✅ 关注市场趋势变化")
            
        elif total_upl < 0:
            print(f"📉 当前整体亏损，建议:")
            print(f"   ⚠️ 检查止损设置是否合理")
            print(f"   ⚠️ 评估是否需要减仓止损")
            print(f"   ⚠️ 避免情绪化追加投入")
            
        else:
            print(f"📊 当前盈亏平衡")
            
        print(f"\n🎯 风险管理建议:")
        print(f"   📊 定期监控持仓盈亏")
        print(f"   📊 设置合理的止盈止损")
        print(f"   📊 注意仓位分配和风险控制")
        print(f"   📊 避免过度杠杆")

    else:
        print(f"\n❌ 查询失败!")
        print(f"   错误代码: {code}")
        print(f"   错误信息: {result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 解析持仓数据时发生错误: {e}")
    print(f"原始结果: {result}")

print("\n🎯 Demo运行完成！")
print("📚 下一步: 尝试运行 demo09_成交查询.py 查看成交记录")