#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 40 - 修改策略委托订单
==============================

功能说明：
- 修改策略委托订单（仅支持止盈止损和计划委托订单）
- 不支持冰山委托、时间加权、移动止盈止损等订单类型
- 支持修改订单数量、触发价格、委托价格等参数
- 支持修改止盈止损参数和计划委托参数

使用场景：
- 调整已下达的策略委托订单参数
- 修改止盈止损的触发价和委托价
- 调整计划委托的触发条件
- 优化策略订单的执行条件

支持的订单类型：
- 止盈止损订单（TP/SL）
- 计划委托订单（Trigger Order）
- 附带止盈止损的计划委托

核心参数：
- algoId/algoClOrdId: 策略订单标识
- newSz: 修改后的数量
- 止盈止损相关参数
- 计划委托相关参数

API文档：
https://www.okx.com/docs-v5/en/#rest-api-trade-amend-algo-order

作者：OKX API Demo
创建时间：2025-01-18
版本：v1.0
"""

import okx.Trade as Trade
import okx.MarketData as MarketData
import datetime
import json
import time
import random

# =============================================================================
# API 配置区域
# =============================================================================

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api_config import get_api_config, validate_config, print_config_info

config = get_api_config()
is_valid, message = validate_config(config)
if not is_valid:
    print(f"❌ API配置错误: {message}")
    print("💡 请检查 api_config.py 文件中的配置")
    exit(1)

apikey = config['api_key']
secretkey = config['secret_key']
passphrase = config['passphrase']
flag = config['flag']

print("=" * 60)
print("🎯 OKX修改策略委托订单工具")
print("=" * 60)
print_config_info(config)
print("=" * 60)

# =============================================================================
# 修改订单参数配置
# =============================================================================

INST_ID = "BTC-USDT-SWAP"              # 交易对：BTC永续合约

# 订单标识（二选一，优先使用algoId）
ALGO_ID = "590919993110396111"          # 策略委托单ID（从查询接口获取）
ALGO_CLIENT_ORDER_ID = ""               # 客户自定义策略订单ID

# 通用修改参数
CXL_ON_FAIL = False                     # 修改失败时是否自动撤单
REQ_ID = f"amend{int(time.time())}{random.randint(100, 999)}"  # 用户自定义修改事件ID
NEW_SIZE = "0.5"                        # 修改后的新数量

# 止盈止损修改参数
NEW_TP_TRIGGER_PX = "45000"             # 新的止盈触发价
NEW_TP_ORD_PX = "44900"                 # 新的止盈委托价（-1为市价）
NEW_SL_TRIGGER_PX = "40000"             # 新的止损触发价
NEW_SL_ORD_PX = "40100"                 # 新的止损委托价（-1为市价）
NEW_TP_TRIGGER_PX_TYPE = "last"         # 止盈触发价类型：last/index/mark
NEW_SL_TRIGGER_PX_TYPE = "last"         # 止损触发价类型：last/index/mark

# 计划委托修改参数
NEW_TRIGGER_PX = "42000"                # 新的触发价格
NEW_ORD_PX = "41950"                    # 新的委托价格（-1为市价）
NEW_TRIGGER_PX_TYPE = "last"            # 触发价格类型：last/index/mark

print(f"🎯 修改策略委托配置:")
print(f"   交易对: {INST_ID}")
print(f"   策略订单ID: {ALGO_ID}")
print(f"   修改事件ID: {REQ_ID}")
print(f"   失败自动撤单: {CXL_ON_FAIL}")
print("=" * 60)

# =============================================================================
# 初始化API
# =============================================================================

try:
    tradeAPI = Trade.TradeAPI(apikey, secretkey, passphrase, False, flag)
    marketDataAPI = MarketData.MarketAPI(flag=flag)
    print("✅ 交易API初始化成功")
except Exception as e:
    print(f"❌ 交易API初始化失败: {e}")
    exit(1)

# =============================================================================
# 获取当前价格信息
# =============================================================================

try:
    ticker = marketDataAPI.get_ticker(instId=INST_ID)
    if ticker.get('code') != '0' or not ticker.get('data'):
        raise RuntimeError(f"获取ticker失败: {ticker}")
    
    last = float(ticker['data'][0]['last'])
    
    print(f"📈 当前价格信息:")
    print(f"   最新价(last): ${last}")
    print(f"   止盈触发价: ${NEW_TP_TRIGGER_PX}")
    print(f"   止损触发价: ${NEW_SL_TRIGGER_PX}")
    print(f"   计划委托触发价: ${NEW_TRIGGER_PX}")

except Exception as e:
    print(f"❌ 获取价格信息失败: {e}")
    exit(1)

# =============================================================================
# 修改策略委托订单演示
# =============================================================================

def amend_tp_sl_order_demo():
    """
    修改止盈止损订单演示
    """
    print(f"\n" + "-" * 50)
    print("示例1：修改止盈止损订单")
    print("-" * 50)
    
    try:
        # 构建修改止盈止损订单的参数
        amend_params = {
            'instId': INST_ID,
            'algoId': ALGO_ID,
            'cxlOnFail': CXL_ON_FAIL,
            'reqId': REQ_ID + "_tpsl",
            'newSz': NEW_SIZE,
            'newTpTriggerPx': NEW_TP_TRIGGER_PX,
            'newTpOrdPx': NEW_TP_ORD_PX,
            'newSlTriggerPx': NEW_SL_TRIGGER_PX,
            'newSlOrdPx': NEW_SL_ORD_PX,
            'newTpTriggerPxType': NEW_TP_TRIGGER_PX_TYPE,
            'newSlTriggerPxType': NEW_SL_TRIGGER_PX_TYPE
        }
        
        print(f"准备修改止盈止损订单:")
        print(f"  订单ID: {ALGO_ID}")
        print(f"  新数量: {NEW_SIZE}")
        print(f"  止盈: 触发价${NEW_TP_TRIGGER_PX}, 委托价${NEW_TP_ORD_PX}")
        print(f"  止损: 触发价${NEW_SL_TRIGGER_PX}, 委托价${NEW_SL_ORD_PX}")
        
        # 注意：这里注释掉实际的API调用，避免误操作
        # result = tradeAPI.amend_algo_order(**amend_params)
        # print(f"修改结果: {json.dumps(result, indent=2, ensure_ascii=False)}")
        
        print("⚠️  实际API调用已注释，请根据需要取消注释")
        
    except Exception as e:
        print(f"❌ 修改止盈止损订单时发生错误: {str(e)}")
        import traceback
        traceback.print_exc()

def amend_trigger_order_demo():
    """
    修改计划委托订单演示
    """
    print(f"\n" + "-" * 50)
    print("示例2：修改计划委托订单")
    print("-" * 50)
    
    try:
        # 构建修改计划委托订单的参数
        amend_params = {
            'instId': INST_ID,
            'algoId': ALGO_ID,
            'cxlOnFail': CXL_ON_FAIL,
            'reqId': REQ_ID + "_trigger",
            'newSz': NEW_SIZE,
            'newTriggerPx': NEW_TRIGGER_PX,
            'newOrdPx': NEW_ORD_PX,
            'newTriggerPxType': NEW_TRIGGER_PX_TYPE
        }
        
        print(f"准备修改计划委托订单:")
        print(f"  订单ID: {ALGO_ID}")
        print(f"  新数量: {NEW_SIZE}")
        print(f"  触发价: ${NEW_TRIGGER_PX} ({NEW_TRIGGER_PX_TYPE})")
        print(f"  委托价: ${NEW_ORD_PX}")
        
        # 注意：这里注释掉实际的API调用，避免误操作
        # result = tradeAPI.amend_algo_order(**amend_params)
        # print(f"修改结果: {json.dumps(result, indent=2, ensure_ascii=False)}")
        
        print("⚠️  实际API调用已注释，请根据需要取消注释")
        
    except Exception as e:
        print(f"❌ 修改计划委托订单时发生错误: {str(e)}")
        import traceback
        traceback.print_exc()

def amend_trigger_with_attach_demo():
    """
    修改带附加止盈止损的计划委托订单演示
    """
    print(f"\n" + "-" * 50)
    print("示例3：修改带附加止盈止损的计划委托订单")
    print("-" * 50)
    
    try:
        # 构建修改带附加止盈止损的计划委托订单参数
        attach_algo_ords = [
            {
                "newTpTriggerPx": "46000",
                "newTpTriggerPxType": "last",
                "newTpOrdPx": "45900",
                "newSlTriggerPx": "39000",
                "newSlTriggerPxType": "last",
                "newSlOrdPx": "39100"
            }
        ]
        
        amend_params = {
            'instId': INST_ID,
            'algoId': ALGO_ID,
            'cxlOnFail': CXL_ON_FAIL,
            'reqId': REQ_ID + "_attach",
            'newSz': NEW_SIZE,
            'newTriggerPx': NEW_TRIGGER_PX,
            'newOrdPx': NEW_ORD_PX,
            'newTriggerPxType': NEW_TRIGGER_PX_TYPE,
            'attachAlgoOrds': attach_algo_ords
        }
        
        print(f"准备修改带附加止盈止损的计划委托订单:")
        print(f"  订单ID: {ALGO_ID}")
        print(f"  新数量: {NEW_SIZE}")
        print(f"  触发价: ${NEW_TRIGGER_PX}")
        print(f"  委托价: ${NEW_ORD_PX}")
        print(f"  附加止盈: 触发价$46000, 委托价$45900")
        print(f"  附加止损: 触发价$39000, 委托价$39100")
        
        # 注意：这里注释掉实际的API调用，避免误操作
        # result = tradeAPI.amend_algo_order(**amend_params)
        # print(f"修改结果: {json.dumps(result, indent=2, ensure_ascii=False)}")
        
        print("⚠️  实际API调用已注释，请根据需要取消注释")
        
    except Exception as e:
        print(f"❌ 修改带附加止盈止损的计划委托订单时发生错误: {str(e)}")
        import traceback
        traceback.print_exc()

def handle_amend_result_demo():
    """
    处理修改结果演示
    """
    print(f"\n" + "-" * 50)
    print("示例4：修改结果处理")
    print("-" * 50)
    
    # 模拟返回结果
    mock_result = {
        "code": "0",
        "msg": "",
        "data": [
            {
                "algoClOrdId": "algo_01",
                "algoId": "2510789768709120",
                "reqId": "po103ux",
                "sCode": "0",
                "sMsg": ""
            }
        ]
    }
    
    print("模拟返回结果:")
    print(json.dumps(mock_result, indent=2, ensure_ascii=False))
    
    # 解析结果
    if mock_result['code'] == '0':
        print("\n✅ 请求成功")
        for item in mock_result['data']:
            if item['sCode'] == '0':
                print(f"  ✅ 策略订单 {item['algoId']} 修改成功")
                print(f"     客户订单ID: {item['algoClOrdId']}")
                print(f"     修改事件ID: {item['reqId']}")
            else:
                print(f"  ❌ 策略订单 {item['algoId']} 修改失败: {item['sMsg']}")
    else:
        print(f"❌ 请求失败: {mock_result['msg']}")

def show_usage_tips():
    """
    显示使用提示
    """
    print("\n" + "=" * 60)
    print("使用提示")
    print("=" * 60)
    print("1. 订单标识说明:")
    print("   - algoId和algoClOrdId必须传一个")
    print("   - 若传两个，以algoId为主")
    print("   - 建议优先使用algoId")
    
    print("\n2. 支持的订单类型:")
    print("   ✅ 止盈止损订单")
    print("   ✅ 计划委托订单")
    print("   ✅ 带附加止盈止损的计划委托")
    print("   ❌ 冰山委托")
    print("   ❌ 时间加权委托")
    print("   ❌ 移动止盈止损")
    
    print("\n3. 修改参数说明:")
    print("   - newSz: 新数量，必须大于0")
    print("   - 触发价为0表示删除对应的止盈/止损")
    print("   - 委托价为-1表示市价执行")
    print("   - 触发价类型: last(最新价)/index(指数价)/mark(标记价)")
    
    print("\n4. 注意事项:")
    print("   - API限速：20次/2s")
    print("   - 限速规则：User ID + Instrument ID")
    print("   - 建议先在模拟盘环境测试")
    print("   - 修改失败时可选择自动撤单")
    
    print("\n5. 最佳实践:")
    print("   - 修改前先查询订单状态")
    print("   - 使用reqId跟踪修改事件")
    print("   - 合理设置cxlOnFail参数")
    print("   - 及时处理修改结果")
    
    print("\n⚠️  重要提醒:")
    print("   - 修改操作会影响策略执行")
    print("   - 请确保新参数的合理性")
    print("   - 实际使用时请取消代码中的注释")
    print("   - 建议先查询策略委托订单列表获取有效的algoId")

if __name__ == "__main__":
    try:
        print(f"\n🚀 开始修改策略委托订单演示...")
        
        # 演示各种修改场景
        amend_tp_sl_order_demo()
        amend_trigger_order_demo()
        amend_trigger_with_attach_demo()
        handle_amend_result_demo()
        
        show_usage_tips()
        
        print(f"\n🎯 Demo运行完成！")
        
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序执行出错: {str(e)}")
        import traceback
        traceback.print_exc()