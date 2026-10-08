#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 38 - 网格策略委托下单 (Grid Trading Bot)
=====================================================

接口: POST /api/v5/tradingBot/grid/order-algo
限速: 20次/2s (User ID + Instrument ID)
权限: 交易

功能说明：
- 网格策略委托，按指定价格区间[minPx, maxPx]将资金划分为若干网格，在区间内通过“低买高卖/高卖低买”获取价差收益
- 支持「现货网格」(grid) 与「合约网格」(contract_grid)，可选等差(runType=1)或等比(runType=2)网格
- 支持设置止盈/止损触发价(tpTriggerPx/slTriggerPx)，满足条件后自动停止网格
- Demo 通过 SDK 的 GridAPI.grid_order_algo 下单，演示请求参数组装与返回解析

使用场景：
- 震荡/区间行情：在明确的上下边界内反复捕捉波动收益
- 长期“搬砖”型被动策略：对冲“择时难”，让行情波动替你工作
- 合约场景：在看多/看空的方向上做网格，或使用中性网格(neutral)在不确定方向时赚取波动
- 已有现货/合约持仓时：开一个中性/小幅倾向的网格，用波动收益缓冲回撤、改善持仓成本

网格策略特点：
- 优势：在横盘/震荡阶段表现突出，逻辑简单、可配置性强、可设置保护(止盈/止损触发)
- 风险：趋势单边可能持续亏损；价格越出区间将停止成交；参数设置不当会导致资金利用效率低
- 关键：区间(minPx/maxPx)、网格数(gridNum)、资金规模(现货: quoteSz/baseSz；合约: sz/lever/direction) 的协调

核心参数：
- 通用：instId, algoOrdType[grid|contract_grid], maxPx, minPx, gridNum, runType[1等差|2等比], tpTriggerPx, slTriggerPx, tag
- 现货专属：quoteSz(计价币投入) / baseSz(交易币投入)，二者至少一个
- 合约专属：sz(投入保证金, USDT), direction[long|short|neutral], lever(杠杆倍数), basePos(是否开底仓, 中性忽略)
- 可选扩展：algoClOrdId(自定义策略ID), triggerParams(信号触发, 含instant/price/rsi等)
  提示：本 SDK 的 GridAPI.grid_order_algo 当前未显式暴露 algoClOrdId/triggerParams/tradeQuoteCcy/tpRatio/slRatio 等字段，
  如需使用可扩展 SDK 或直接调用底层签名请求，将字段完整透传到 /api/v5/tradingBot/grid/order-algo。

运行步骤：
1) 在“网格策略参数配置”区调整 ALGO_ORD_TYPE/INST_ID/MAX_PX/MIN_PX/GRID_NUM/RUN_TYPE 等
2) 选择现货或合约分支并配置对应资金/方向/杠杆参数
3) 运行脚本，查看原始返回与结果摘要，获取 algoId 以便后续查询/调整/停止

API文档：/api/v5/tradingBot/grid/order-algo

作者：OKX API Demo
创建时间：2025-01-14
版本：v1.0
"""

import json
import time
import random

import okx.Grid as Grid
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api_config import get_api_config, validate_config, print_config_info

# =============================================================================
# 读取并校验 API 配置
# =============================================================================
config = get_api_config()
is_valid, message = validate_config(config)
if not is_valid:
    print(f"❌ API配置错误: {message}")
    print("💡 请检查 okx/demo/api_config.py 文件中的配置")
    exit(1)

apikey = config['api_key']
secretkey = config['secret_key']
passphrase = config['passphrase']
flag = config['flag']

print("=" * 60)
print("🎯 OKX网格策略委托工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 网格策略参数配置
# =============================================================================
# algoOrdType: grid(现货网格) / contract_grid(合约网格)
ALGO_ORD_TYPE = "grid"           # "grid" or "contract_grid"

# 为方便演示, 根据 ALGO_ORD_TYPE 默认给出示例产品
INST_ID = "BTC-USDT" if ALGO_ORD_TYPE == "grid" else "BTC-USDT-SWAP"

# 网格区间与基础参数
MAX_PX = "45000"                 # 区间最高价格
MIN_PX = "20000"                 # 区间最低价格
GRID_NUM = "20"                  # 网格数量
RUN_TYPE = "1"                    # 1:等差, 2:等比(默认等差)

# 止盈/止损触发价(可选, 现货/合约均支持)
TP_TRIGGER_PX = ""               # 止盈触发价, 例如 "48000"
SL_TRIGGER_PX = ""               # 止损触发价, 例如 "19000"

# 标签(可选)
TAG = "demo-grid"

# 客户策略订单ID（可选参数，不填写）
# ALGO_CLIENT_ORDER_ID = f"grid{int(time.time())}{random.randint(100,999)}"

# 现货网格资金配置(quoteSz/baseSz 至少指定一个)
QUOTE_SZ = "100"                 # 计价币投入数量(USDT)
BASE_SZ = ""                     # 交易币投入数量(例如 BTC 数量)
# tradeQuoteCcy 仅适用于现货网格, 默认使用 instId 的计价币, 当前方法未暴露

# 合约网格配置
MARGIN_SZ = "300"                # 投入保证金(USDT), 字段名: sz
DIRECTION = "long"               # long/short/neutral
LEVER = "3"                      # 杠杆倍数
BASE_POS = False                  # 是否开底仓(中性网格忽略)
# tpRatio/slRatio 为合约网格的止盈/止损比率, 当前方法未暴露

# 友好打印
print("📌 策略配置:")
print(f"   类型: {ALGO_ORD_TYPE}")
print(f"   产品: {INST_ID}")
print(f"   最高价: {MAX_PX}  最低价: {MIN_PX}  网格数: {GRID_NUM}  类型(runType): {RUN_TYPE}")
if TP_TRIGGER_PX:
    print(f"   止盈触发价: {TP_TRIGGER_PX}")
if SL_TRIGGER_PX:
    print(f"   止损触发价: {SL_TRIGGER_PX}")
print(f"   标签: {TAG}")
# print(f"   客户策略订单ID(仅展示): {ALGO_CLIENT_ORDER_ID}")

if ALGO_ORD_TYPE == "grid":
    print("   现货网格资金: ")
    print(f"     quoteSz={QUOTE_SZ or '-'}  baseSz={BASE_SZ or '-'}")
else:
    print("   合约网格资金: ")
    print(f"     sz={MARGIN_SZ}  direction={DIRECTION}  lever={LEVER}  basePos={BASE_POS}")

# =============================================================================
# 初始化 API
# =============================================================================
try:
    gridAPI = Grid.GridAPI(apikey, secretkey, passphrase, False, flag)
    print("✅ GridAPI 初始化成功")
except Exception as e:
    print(f"❌ GridAPI 初始化失败: {e}")
    exit(1)

# =============================================================================
# 组织请求参数并下单
# =============================================================================
try:
    print("\n🚀 提交网格策略委托...")

    # 通用参数
    common_params = {
        'instId': INST_ID,
        'algoOrdType': ALGO_ORD_TYPE,
        'maxPx': MAX_PX,
        'minPx': MIN_PX,
        'gridNum': GRID_NUM,
        'runType': RUN_TYPE,
        'tpTriggerPx': TP_TRIGGER_PX,
        'slTriggerPx': SL_TRIGGER_PX,
        'tag': TAG,
    }

    # 根据类型补充专属参数
    if ALGO_ORD_TYPE == "grid":
        # 现货网格: quoteSz 和 baseSz 至少指定一个
        params = dict(common_params)
        params['quoteSz'] = QUOTE_SZ
        params['baseSz'] = BASE_SZ
    else:
        # 合约网格: sz/direction/lever/basePos
        params = dict(common_params)
        params['sz'] = MARGIN_SZ
        params['direction'] = DIRECTION
        params['lever'] = LEVER
        params['basePos'] = BASE_POS

    # 提交请求
    result = gridAPI.grid_order_algo(**params)

    print("✅ 请求已发送，返回结果如下：")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # 解析返回
    code = result.get('code', '')
    if code == '0' and result.get('data'):
        data0 = result['data'][0]
        algo_id = data0.get('algoId', '')
        s_code = data0.get('sCode', '')
        s_msg = data0.get('sMsg', '')
        print("\n=" * 30)
        print("📋 委托结果摘要")
        print("=" * 60)
        print(f"🆔 策略订单ID:  {algo_id}")
        print(f"🏷️  标签:        {data0.get('tag','')}")
        print(f"📊 状态码:      {s_code}")
        print(f"💬 状态信息:    {s_msg}")
        if s_code == '0':
            print("\n🎉 网格策略委托创建成功！")
        else:
            print("\n⚠️ 委托创建未成功，请检查参数与账户状态。")
    else:
        print("\n❌ 请求失败或返回数据为空！")
        print(f"错误代码: {code}")
        print(f"错误信息: {result.get('msg', '未知错误')}")

except Exception as e:
    print(f"❌ 下单过程中发生异常: {e}")

# =============================================================================
# 重要提示与补充说明
# =============================================================================
print("\n📌 提示:")
print("- 本示例使用 SDK 的 GridAPI.grid_order_algo 进行请求。")
print("- 若需使用 algoClOrdId、triggerParams、tradeQuoteCcy、tpRatio、slRatio 等字段, 目前该方法未直接暴露, 可考虑:")
print("  1) 扩展 SDK 在 GridAPI.grid_order_algo 的参数与转发; 或")
print("  2) 参考 SDK 直接调用底层签名请求方法, 将完整字段透传到 /api/v5/tradingBot/grid/order-algo。")

print("\n🎯 Demo 运行完成！")