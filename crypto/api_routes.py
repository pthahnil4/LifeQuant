#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 路由模块 - 封装 demo 逻辑为 HTTP 接口
=========================================
将 demo 目录下的脚本封装成 Flask 可调用的 API 接口
"""

import json
import datetime
import sys
import os
import logging
import threading
from flask import Blueprint, jsonify, request

# =============================================================================
# 【关键】确保优先加载项目根目录的 api_config.py
# real_strategy_adapter.py 会把 MACD 子目录插入 sys.path[0]，
# 其中包含另一个 api_config.py（不同密钥），导致密钥错误。
# 这里强制把项目根目录提到 sys.path 最前面。
# =============================================================================
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT in sys.path:
    sys.path.remove(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger(__name__)

# MySQL 数据层（迁移批次4：余额快照，原 account_balance_history.json）
# 数据库初始化已收敛到 database.init_db() 进程内单例，由应用启动时后台预热完成
from .database import session_scope
from . import balance_repo

# =============================================================================
# 只读接口限频/退避（问题#8）
# =============================================================================
# 本文件 18 个接口全部是只读代理（没有任何下单/撤单能力），但前端轮询与
# 同进程的实盘调度、行情扫描抢的是同一个 API key 配额。不限频时一旦互相
# 打爆就集体回 50011（表现为“页面暂时拿不到数据 + 交易轮次误判为查询失败”）。
# 导入失败时置 None，_rl_call 退化为直连，绝不因为限频模块不可用而弄挂页面。
try:
    from task.utils.okx_ratelimit import limited as _rl_limited
except ImportError:
    try:
        from crypto.task.utils.okx_ratelimit import limited as _rl_limited
    except ImportError:
        _rl_limited = None

# OKX 方法名 → 限频分组（与 trade_executor 共用同一套桶，同 key 共享额度）
_RL_GROUP_BY_METHOD = {
    'get_ticker': 'market_ticker',
    'get_tickers': 'market_ticker',
    'get_orderbook': 'market_ticker',
    'get_candlesticks': 'market_candles',
    'get_index_candlesticks': 'market_candles',
    'get_history_candlesticks': 'market_candles',
    'get_mark_price_candlesticks': 'market_candles',
    'get_account_balance': 'balance',
    'get_account_bills': 'order_query',
    'get_asset_valuation': 'balance',
    'get_balances': 'balance',
    'get_max_order_size': 'balance',
    'get_positions': 'positions',
    'get_positions_history': 'positions',
    'get_order_list': 'order_query',
    'get_order': 'order_query',
    'get_orders_history': 'order_query',
    'get_interest_accrued': 'order_query',
    'get_bills': 'order_query',
    'get_leverage': 'leverage',
}


def _rl_call(client, method, **kwargs):
    """按方法名选分组，节流 + 退避地调用只读 OKX 接口。

    只给只读接口用；限频模块不可用时直通原始调用，行为与改前一致。
    """
    func = getattr(client, method)
    if _rl_limited is None:
        return func(**kwargs)
    return _rl_limited(_RL_GROUP_BY_METHOD.get(method, 'order_query'), func, **kwargs)

# =============================================================================
# API 客户端初始化（延迟加载，避免项目启动时就加载）
# =============================================================================
_api_clients = {}

def _get_api_client(api_type, account=None):
    """获取或创建 API 客户端实例（懒加载 + 缓存，支持多账号）

    Args:
        api_type: 客户端类型（market/account/funding/trade）
        account: 账号标识，为 None 或空串时使用默认账号
    """
    cache_key = f"{api_type}:{account or '_default'}"
    if cache_key in _api_clients:
        return _api_clients[cache_key]

    # 导入 OKX SDK 和配置
    from api_config import get_api_config
    config = get_api_config(account if account else None)

    apikey = config['api_key']
    secretkey = config['secret_key']
    passphrase = config['passphrase']
    flag = config['flag']

    if api_type == 'market':
        import okx.MarketData as MarketData
        _api_clients[cache_key] = MarketData.MarketAPI(flag=flag)
    elif api_type == 'account':
        import okx.Account as Account
        _api_clients[cache_key] = Account.AccountAPI(apikey, secretkey, passphrase, False, flag)
    elif api_type == 'funding':
        import okx.Funding as Funding
        _api_clients[cache_key] = Funding.FundingAPI(apikey, secretkey, passphrase, False, flag)
    elif api_type == 'trade':
        import okx.Trade as Trade
        _api_clients[cache_key] = Trade.TradeAPI(apikey, secretkey, passphrase, False, flag)

    return _api_clients.get(cache_key)


def _ts_to_str(ts_ms):
    """将13位毫秒时间戳转为可读字符串"""
    try:
        ts_int = int(ts_ms)
        if ts_int <= 0:
            return 'N/A'
        return datetime.datetime.fromtimestamp(ts_int / 1000).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        return 'N/A'


def _okx_success(result):
    """判断 OKX API 返回是否成功"""
    return result.get('code') == '0'


def _make_response(okx_result):
    """将 OKX API 结果统一包装为标准响应格式"""
    if _okx_success(okx_result):
        return {
            "code": 200,
            "message": "success",
            "data": okx_result.get('data', [])
        }
    else:
        return {
            "code": 500,
            "message": okx_result.get('msg', 'API调用失败'),
            "data": None
        }


# =============================================================================
# 创建蓝图
# =============================================================================
api_bp = Blueprint('api_routes', __name__)


# =============================================================================
# 📈 市场类 - 行情查询 (对应 demo15)
# GET /api/market/ticker?instId=BTC-USDT-SWAP
# =============================================================================
@api_bp.route('/api/market/ticker', methods=['GET'])
def market_ticker():
    """
    获取单个币种最新行情
    对应 demo: demo15_获取所有产品行情信息.py
    """
    try:
        inst_id = request.args.get('instId', '')
        acct = request.args.get('account', '')
        market_api = _get_api_client('market', acct or None)

        if inst_id:
            # 单币种查询
            result = _rl_call(market_api, 'get_ticker', instId=inst_id)
            if not _okx_success(result) or not result.get('data'):
                return jsonify(_make_response(result))

            ticker = result['data'][0]
            data = {
                "instId": ticker.get('instId', inst_id),
                "last": ticker.get('last', '0'),
                "bid": ticker.get('bid', '0'),
                "ask": ticker.get('ask', '0'),
                "vol24h": ticker.get('vol24h', '0'),
                "change24h": ticker.get('sodUtc8', '0') + '%',
                "high24h": ticker.get('high24h', '0'),
                "low24h": ticker.get('low24h', '0'),
                "timestamp": _ts_to_str(ticker.get('ts', '0'))
            }
            return jsonify({"code": 200, "message": "success", "data": data})
        else:
            # 全量产品查询（默认SWAP）
            inst_type = request.args.get('instType', 'SWAP')
            result = _rl_call(market_api, 'get_tickers', instType=inst_type)
            return jsonify(_make_response(result))

    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 📈 市场类 - 盘口深度
# GET /api/market/orderbook?instId=BTC-USDT-SWAP
# =============================================================================
@api_bp.route('/api/market/orderbook', methods=['GET'])
def market_orderbook():
    """
    获取币种盘口深度数据
    """
    try:
        inst_id = request.args.get('instId', '')
        acct = request.args.get('account', '')
        if not inst_id:
            return jsonify({"code": 400, "message": "缺少参数 instId", "data": None})
        depth = request.args.get('depth', '20')
        market_api = _get_api_client('market', acct or None)
        result = _rl_call(market_api, 'get_orderbook', instId=inst_id, sz=depth)

        if not _okx_success(result) or not result.get('data'):
            return jsonify(_make_response(result))

        book = result['data'][0]
        data = {
            "instId": inst_id,
            "asks": book.get('asks', [])[:int(depth)],
            "bids": book.get('bids', [])[:int(depth)],
            "ts": _ts_to_str(book.get('ts', '0'))
        }
        return jsonify({"code": 200, "message": "success", "data": data})

    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 👤 账户类 - 账户信息 (对应 demo02)
# GET /api/account/info
# =============================================================================
@api_bp.route('/api/account/info', methods=['GET'])
def account_info():
    """
    获取账户基本信息
    对应 demo: demo02_账户余额查询.py
    """
    try:
        acct = request.args.get('account', '')
        account_api = _get_api_client('account', acct or None)
        result = _rl_call(account_api, 'get_account_balance')

        if not _okx_success(result) or not result.get('data'):
            return jsonify(_make_response(result))

        acct = result['data'][0]

        # 提取各币种信息
        details = acct.get('details', [])
        currency_details = []
        for ccy_info in details:
            if float(ccy_info.get('eq', '0')) > 0:
                currency_details.append({
                    "ccy": ccy_info.get('ccy', ''),
                    "eq": ccy_info.get('eq', '0'),
                    "availBal": ccy_info.get('availBal', '0'),
                    "frozenBal": ccy_info.get('frozenBal', '0'),
                    "eqUsd": ccy_info.get('eqUsd', '0')
                })

        data = {
            "totalEq": acct.get('totalEq', '0'),
            "totalEqUsd": acct.get('totalEq', '0'),
            "marginMode": acct.get('marginMode', ''),
            "uTime": _ts_to_str(acct.get('uTime', '0')),
            "details": currency_details
        }
        return jsonify({"code": 200, "message": "success", "data": data})

    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 👤 账户类 - 当前持仓
# GET /api/account/positions?instId=BTC-USDT-SWAP
# =============================================================================
@api_bp.route('/api/account/positions', methods=['GET'])
def account_positions():
    """
    获取当前持仓信息
    """
    try:
        inst_id = request.args.get('instId', '')
        acct = request.args.get('account', '')
        account_api = _get_api_client('account', acct or None)

        if inst_id:
            result = _rl_call(account_api, 'get_positions', instId=inst_id)
        else:
            result = _rl_call(account_api, 'get_positions')

        if not _okx_success(result):
            return jsonify(_make_response(result))

        positions = result.get('data', [])
        pos_list = []
        for pos in positions:
            pos_list.append({
                "instId": pos.get('instId', ''),
                "posSide": pos.get('posSide', ''),
                "size": pos.get('pos', '0'),
                "avgPx": pos.get('avgPx', '0'),
                "markPx": pos.get('markPx', '0'),
                "upl": pos.get('upl', '0'),
                "uplRatio": pos.get('uplRatio', '0') + '%',
                "lever": pos.get('lever', '0') + 'x',
                "margin": pos.get('imr', '0'),
                "cTime": _ts_to_str(pos.get('cTime', '0'))
            })

        return jsonify({"code": 200, "message": "success", "data": pos_list})

    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 💰 资金类 - 资金余额 (对应 demo01)
# GET /api/funds/balance?ccy=USDT
# =============================================================================
@api_bp.route('/api/funds/balance', methods=['GET'])
def funds_balance():
    """
    获取各币种资金余额
    对应 demo: demo01_账户财产估值.py
    """
    try:
        ccy = request.args.get('ccy', '')
        acct = request.args.get('account', '')
        funding_api = _get_api_client('funding', acct or None)

        # 获取资产估值
        val_result = _rl_call(funding_api, 'get_asset_valuation', ccy=ccy if ccy else "USDT")
        if not _okx_success(val_result):
            return jsonify(_make_response(val_result))
        valuation_data = {}
        if val_result.get('data'):
            val = val_result['data'][0]
            valuation_data = {
                "totalBal": val.get('totalBal', '0'),
                "ts": _ts_to_str(val.get('ts', '0')),
                "details": val.get('details', {})
            }

        # 获取各币种余额
        bal_result = (_rl_call(funding_api, 'get_balances', ccy=ccy) if ccy
                      else _rl_call(funding_api, 'get_balances'))
        if not _okx_success(bal_result):
            return jsonify(_make_response(bal_result))
        balance_list = []
        for item in bal_result.get('data', []):
            if float(item.get('bal', '0')) > 0:
                balance_list.append({
                        "ccy": item.get('ccy', ''),
                        "bal": item.get('bal', '0'),
                        "frozenBal": item.get('frozenBal', '0'),
                        "availBal": item.get('availBal', '0'),
                        "eq": item.get('eq', '0')
                    })

        data = {
            "valuation": valuation_data,
            "balances": balance_list
        }
        return jsonify({"code": 200, "message": "success", "data": data})

    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 💰 资金类 - 资金流水
# GET /api/funds/history?limit=10
# =============================================================================
@api_bp.route('/api/funds/history', methods=['GET'])
def funds_history():
    """
    获取资金流水 / 账单记录
    """
    try:
        limit = request.args.get('limit', '10')
        acct = request.args.get('account', '')
        funding_api = _get_api_client('funding', acct or None)
        result = _rl_call(funding_api, 'get_bills', type='2', limit=limit)

        if not _okx_success(result):
            return jsonify(_make_response(result))

        bills = result.get('data', [])
        bill_list = []
        for bill in bills:
            bill_list.append({
                "txId": bill.get('billId', ''),
                "ccy": bill.get('ccy', ''),
                "type": bill.get('type', ''),
                "change": bill.get('balChg', '0'),
                "balAfter": bill.get('bal', '0'),
                "ts": _ts_to_str(bill.get('ts', '0'))
            })

        return jsonify({"code": 200, "message": "success", "data": bill_list})

    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 🔄 交易类 - 当前委托 (对应 demo18)
# GET /api/trade/open_orders?instId=BTC-USDT-SWAP
# =============================================================================
@api_bp.route('/api/trade/open_orders', methods=['GET'])
def trade_open_orders():
    """
    获取当前委托列表
    对应 demo: demo18_获取未成交订单列表.py
    """
    try:
        inst_id = request.args.get('instId', '')
        acct = request.args.get('account', '')
        trade_api = _get_api_client('trade', acct or None)

        kwargs = {'state': 'live'}
        if inst_id:
            kwargs['instId'] = inst_id

        result = _rl_call(trade_api, 'get_order_list', **kwargs)

        if not _okx_success(result):
            return jsonify(_make_response(result))

        orders = result.get('data', [])
        order_list = []
        for order in orders:
            order_list.append({
                "ordId": order.get('ordId', ''),
                "instId": order.get('instId', ''),
                "side": order.get('side', ''),
                "posSide": order.get('posSide', ''),
                "ordType": order.get('ordType', ''),
                "px": order.get('px', '0'),
                "sz": order.get('sz', '0'),
                "accFillSz": order.get('accFillSz', '0'),
                "state": order.get('state', ''),
                "cTime": _ts_to_str(order.get('cTime', '0')),
                # 客户订单号：下单时由 gen_cl_ord_id 打标，人工核实/对账时靠它一眼
                # 定位是「哪一轮、哪个篮子」下的单（2026-09-11 打标上线时漏透出）
                "clOrdId": order.get('clOrdId', ''),
                "lever": order.get('lever', '')
            })

        return jsonify({"code": 200, "message": "success", "data": order_list})

    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 🔄 交易类 - 订单详情
# GET /api/trade/order_info?instId=BTC-USDT-SWAP&ordId=xxx
# GET /api/trade/order_info?instId=BTC-USDT-SWAP&clOrdId=xxx   (二选一)
# =============================================================================
@api_bp.route('/api/trade/order_info', methods=['GET'])
def trade_order_info():
    """
    查询订单详情

    ordId 与 clOrdId 至少给一个：交易日志/告警里透出的是本侧打标的 clOrdId，
    只支持 ordId 就得先人肉翻日志找交易所单号，反查链路等于断在半路。
    """
    try:
        inst_id = request.args.get('instId', '')
        ord_id = request.args.get('ordId', '')
        cl_ord_id = request.args.get('clOrdId', '')
        acct = request.args.get('account', '')
        if not inst_id or not (ord_id or cl_ord_id):
            return jsonify({"code": 400, "message": "缺少参数 instId，且 ordId 与 clOrdId 至少给一个",
                            "data": None})

        trade_api = _get_api_client('trade', acct or None)
        kwargs = {'instId': inst_id}
        if ord_id:
            kwargs['ordId'] = ord_id
        else:
            kwargs['clOrdId'] = cl_ord_id
        result = _rl_call(trade_api, 'get_order', **kwargs)

        if not _okx_success(result) or not result.get('data'):
            return jsonify(_make_response(result))

        order = result['data'][0]
        data = {
            "ordId": order.get('ordId', ''),
            "instId": order.get('instId', ''),
            "side": order.get('side', ''),
            "posSide": order.get('posSide', ''),
            "ordType": order.get('ordType', ''),
            "px": order.get('px', '0'),
            "sz": order.get('sz', '0'),
            "accFillSz": order.get('accFillSz', '0'),
            "avgPx": order.get('avgPx', '0'),
            "state": order.get('state', ''),
            "fee": order.get('fee', '0'),
            "cTime": _ts_to_str(order.get('cTime', '0')),
            "uTime": _ts_to_str(order.get('uTime', '0')),
            "clOrdId": order.get('clOrdId', '')
        }
        return jsonify({"code": 200, "message": "success", "data": data})

    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 🔄 交易类 - 历史委托
# GET /api/trade/history?instId=BTC-USDT-SWAP&limit=10
# =============================================================================
@api_bp.route('/api/trade/history', methods=['GET'])
def trade_history():
    """
    获取历史委托记录
    """
    try:
        inst_id = request.args.get('instId', '')
        limit = request.args.get('limit', '10')
        acct = request.args.get('account', '')
        trade_api = _get_api_client('trade', acct or None)

        kwargs = {'limit': limit}
        if inst_id:
            kwargs['instId'] = inst_id

        result = _rl_call(trade_api, 'get_order_list', **kwargs)

        if not _okx_success(result):
            return jsonify(_make_response(result))

        orders = result.get('data', [])
        order_list = []
        for order in orders:
            order_list.append({
                "ordId": order.get('ordId', ''),
                "instId": order.get('instId', ''),
                "side": order.get('side', ''),
                "ordType": order.get('ordType', ''),
                "px": order.get('px', '0'),
                "sz": order.get('sz', '0'),
                "fillPx": order.get('avgPx', '0'),
                "fillSz": order.get('accFillSz', '0'),
                "state": order.get('state', ''),
                "cTime": _ts_to_str(order.get('cTime', '0')),
                "uTime": _ts_to_str(order.get('uTime', '0')),
                "clOrdId": order.get('clOrdId', '')
            })

        return jsonify({"code": 200, "message": "success", "data": order_list})

    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 📈 市场类 - 指数K线 (对应 demo08)
# GET /api/market/index_kline?instId=BTC-USDT&bar=1H&limit=10
# =============================================================================
@api_bp.route('/api/market/index_kline', methods=['GET'])
def market_index_kline():
    """获取指数K线数据"""
    try:
        inst_id = request.args.get('instId', 'BTC-USDT')
        bar = request.args.get('bar', '1H')
        limit = request.args.get('limit', '10')
        acct = request.args.get('account', '')
        market_api = _get_api_client('market', acct or None)
        result = _rl_call(market_api, 'get_index_candlesticks', instId=inst_id, bar=bar, limit=limit)
        if not _okx_success(result):
            return jsonify(_make_response(result))
        data = result.get('data', [])
        kline_list = []
        for candle in data:
            kline_list.append({
                "ts": _ts_to_str(candle[0]),
                "o": candle[1], "h": candle[2], "l": candle[3], "c": candle[4],
                "vol": candle[5] if len(candle) > 5 else '0'
            })
        return jsonify({"code": 200, "message": "success", "data": {
            "instId": inst_id, "bar": bar, "total": len(kline_list), "klines": kline_list
        }})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 📈 市场类 - 历史K线 (对应 demo16)
# GET /api/market/history_kline?instId=BTC-USDT-SWAP&bar=1H&limit=10
# =============================================================================
@api_bp.route('/api/market/history_kline', methods=['GET'])
def market_history_kline():
    """获取历史K线数据（不含最新）"""
    try:
        inst_id = request.args.get('instId', 'BTC-USDT-SWAP')
        bar = request.args.get('bar', '1H')
        limit = request.args.get('limit', '10')
        acct = request.args.get('account', '')
        market_api = _get_api_client('market', acct or None)
        result = _rl_call(market_api, 'get_history_candlesticks', instId=inst_id, bar=bar, limit=limit)
        if not _okx_success(result):
            return jsonify(_make_response(result))
        data = result.get('data', [])
        kline_list = []
        for candle in data:
            kline_list.append({
                "ts": _ts_to_str(candle[0]),
                "o": candle[1], "h": candle[2], "l": candle[3], "c": candle[4],
                "vol": candle[5],
                "volCcy": candle[6] if len(candle) > 6 else '0',
                "confirm": candle[7] if len(candle) > 7 else '0'
            })
        return jsonify({"code": 200, "message": "success", "data": {
            "instId": inst_id, "bar": bar, "total": len(kline_list), "klines": kline_list
        }})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 📈 市场类 - K线对比 (对应 demo23)
# GET /api/market/kline_compare?instId=BTC-USDT-SWAP&bar=1H&limit=5
# =============================================================================
@api_bp.route('/api/market/kline_compare', methods=['GET'])
def market_kline_compare():
    """K线数据对比（运行4种K线方法）"""
    try:
        inst_id = request.args.get('instId', 'BTC-USDT-SWAP')
        index_id = request.args.get('indexId', 'BTC-USDT')
        bar = request.args.get('bar', '1H')
        limit = request.args.get('limit', '5')
        acct = request.args.get('account', '')
        market_api = _get_api_client('market', acct or None)

        methods = [
            ("get_candlesticks", "普通K线数据", inst_id),
            ("get_history_candlesticks", "历史K线数据", inst_id),
            ("get_index_candlesticks", "指数K线数据", index_id),
            ("get_mark_price_candlesticks", "标记价格K线数据", inst_id)
        ]
        results = {}
        for method_name, display_name, target_id in methods:
            try:
                method = getattr(market_api, method_name)
                r = method(instId=target_id, bar=bar, limit=limit)
                if r.get('code') == '0' and r.get('data'):
                    data = r['data']
                    results[display_name] = {
                        "instId": target_id, "count": len(data),
                        "latest_ts": _ts_to_str(data[0][0]) if data else None,
                        "latest_close": data[0][4] if data else None
                    }
                else:
                    results[display_name] = {"error": r.get('msg', '')}
            except Exception as e:
                results[display_name] = {"error": str(e)}
        return jsonify({"code": 200, "message": "success", "data": results})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 👤 账户类 - 最大可开仓数量 (对应 demo14)
# GET /api/account/max_order_size?instId=BTC-USDT-SWAP&tdMode=cross
# =============================================================================
@api_bp.route('/api/account/max_order_size', methods=['GET'])
def account_max_order_size():
    """获取最大可开仓数量"""
    try:
        inst_id = request.args.get('instId', 'BTC-USDT-SWAP')
        td_mode = request.args.get('tdMode', 'cross')
        acct = request.args.get('account', '')
        account_api = _get_api_client('account', acct or None)
        result = _rl_call(account_api, 'get_max_order_size', instId=inst_id, tdMode=td_mode)
        if not _okx_success(result) or not result.get('data'):
            return jsonify(_make_response(result))
        item = result['data'][0]
        data = {
            "instId": item.get('instId', inst_id),
            "tdMode": td_mode,
            "maxBuy": item.get('maxBuy', '0'),
            "maxSell": item.get('maxSell', '0')
        }
        return jsonify({"code": 200, "message": "success", "data": data})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 👤 账户类 - 计息记录 (对应 demo17)
# GET /api/account/interest?ccy=USDT&limit=10
# =============================================================================
@api_bp.route('/api/account/interest', methods=['GET'])
def account_interest():
    """获取账户计息记录"""
    try:
        ccy = request.args.get('ccy', '')
        limit = request.args.get('limit', '10')
        acct = request.args.get('account', '')
        kwargs = {'limit': limit}
        if ccy:
            kwargs['ccy'] = ccy
        account_api = _get_api_client('account', acct or None)
        result = _rl_call(account_api, 'get_interest_accrued', **kwargs)
        if not _okx_success(result):
            return jsonify(_make_response(result))
        records = result.get('data', [])
        interest_list = []
        for rec in records:
            interest_list.append({
                "ccy": rec.get('ccy', ''),
                "interest": rec.get('interest', '0'),
                "interestRate": rec.get('interestRate', '0'),
                "type": '借币' if rec.get('type') == '1' else '放贷' if rec.get('type') == '2' else rec.get('type', ''),
                "ts": _ts_to_str(rec.get('ts', '0')),
                "instId": rec.get('instId', '')
            })
        return jsonify({"code": 200, "message": "success", "data": interest_list})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 👤 账户类 - 历史持仓 (对应 demo21)
# GET /api/account/positions_history?instId=BTC-USDT-SWAP&limit=10
# =============================================================================
@api_bp.route('/api/account/positions_history', methods=['GET'])
def account_positions_history():
    """获取历史持仓信息"""
    try:
        inst_id = request.args.get('instId', '')
        limit = request.args.get('limit', '10')
        acct = request.args.get('account', '')
        kwargs = {'limit': limit}
        if inst_id:
            kwargs['instId'] = inst_id
        account_api = _get_api_client('account', acct or None)
        result = _rl_call(account_api, 'get_positions_history', **kwargs)
        if not _okx_success(result):
            return jsonify(_make_response(result))
        positions = result.get('data', [])
        pos_list = []
        for pos in positions:
            pos_list.append({
                "instId": pos.get('instId', ''),
                "direction": pos.get('direction', ''),
                "lever": pos.get('lever', ''),
                "type": pos.get('type', ''),
                "openAvgPx": pos.get('openAvgPx', '0'),
                "closeAvgPx": pos.get('closeAvgPx', '0'),
                "pnl": pos.get('pnl', '0'),
                "pnlRatio": pos.get('pnlRatio', '0'),
                "closeTotalPos": pos.get('closeTotalPos', '0'),
                "cTime": _ts_to_str(pos.get('cTime', '0')),
                "uTime": _ts_to_str(pos.get('uTime', '0'))
            })
        return jsonify({"code": 200, "message": "success", "data": pos_list})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 👤 账户类 - 杠杆倍数查询 (对应 demo25)
# GET /api/account/leverage?instId=BTC-USDT-SWAP&mgnMode=cross
# =============================================================================
@api_bp.route('/api/account/leverage', methods=['GET'])
def account_leverage():
    """查询杠杆倍数"""
    try:
        inst_id = request.args.get('instId', 'BTC-USDT-SWAP')
        mgn_mode = request.args.get('mgnMode', 'cross')
        acct = request.args.get('account', '')
        account_api = _get_api_client('account', acct or None)
        result = _rl_call(account_api, 'get_leverage', instId=inst_id, mgnMode=mgn_mode)
        if not _okx_success(result) or not result.get('data'):
            return jsonify(_make_response(result))
        lev_list = []
        for item in result['data']:
            lev_list.append({
                "instId": item.get('instId', inst_id),
                "mgnMode": item.get('mgnMode', mgn_mode),
                "posSide": item.get('posSide', 'net'),
                "lever": item.get('lever', '0')
            })
        return jsonify({"code": 200, "message": "success", "data": lev_list})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


# =============================================================================
# 📋 账号列表（供前端账号选择器）
# GET /api/accounts/list
# =============================================================================
@api_bp.route('/api/accounts/list', methods=['GET'])
def accounts_list():
    """获取所有可选 OKX 账号列表"""
    try:
        from api_config import list_accounts
        return jsonify({'code': 200, 'message': 'success', 'data': list_accounts()})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =============================================================================
# 📉 账户类 - 账户余额历史（自建：MySQL 余额快照 + OKX 近7日账单流水）
# GET /api/account/balance-history?days=7
# -----------------------------------------------------------------------------
# 数据来源：
#   1. 余额快照表 balance_history（迁移批次4，替代 account_balance_history.json，
#      每次查询后追加最新总权益快照）
#   2. OKX /api/v5/account/bills 近7日账单流水（提供资金变动时间与原因）
#   3. 回溯初始化：首次查询或历史点少于阈值（5个）时，以当前余额为基准
#      按天衰减倒推（0.95^d）生成历史点（source=backfill），保证折线图
#      起步即有完整趋势；回溯点不覆盖真实快照
# =============================================================================
_BILL_TYPE_MAP = {
    '1': '划转', '2': '交易', '3': '交割', '4': '自动换币',
    '5': '余币宝申购', '6': '余币宝赎回', '7': '计息收入',
    '8': '策略转入', '9': '策略转出'
}
_BAL_HISTORY_MAX_POINTS = 5000          # 单账号快照保留上限
_BAL_BACKFILL_MIN_POINTS = 5            # 历史点少于该阈值时触发回溯初始化
_BAL_BACKFILL_DECAY = 0.95              # 回溯衰减系数：每向前推一天余额 ×0.95
_bal_history_lock = threading.Lock()


def _append_balance_snapshot(account_key, ts_ms, balance):
    """每次查询后追加余额快照（同 ts 去重，超上限自动裁剪）"""
    with _bal_history_lock:
        with session_scope() as session:
            balance_repo.upsert_point(session, account_key, ts_ms, balance)
            balance_repo.trim_oldest(session, account_key, _BAL_HISTORY_MAX_POINTS)


def _backfill_balance_history(account_key, account_name, now_ms, total_eq, days=7):
    """回溯初始化：历史数据点不足时，以当前余额为基准按天倒推补齐历史点

    算法：向前推第 d 天（1..days）的余额 = 当前余额 × (0.95 ^ d)，
    即越往前余额越低（每倒退一天递减约5%），曲线平滑收敛到当前真实余额。
    回溯点标记 source=backfill 与真实快照区分，同 ts 已有点不覆盖。

    Returns:
        实际生成的回溯点数量（历史点已达阈值时返回 0）
    """
    with _bal_history_lock:
        with session_scope() as session:
            if balance_repo.count_points(session, account_key) >= _BAL_BACKFILL_MIN_POINTS:
                return 0
            existing_ts = {p['ts'] for p in
                           balance_repo.load_account_points(session, account_key)}
            added_points = []
            for d in range(1, days + 1):
                ts = now_ms - d * 86400000  # UTC 毫秒级时间戳
                if ts in existing_ts:
                    continue
                added_points.append({
                    'ts': ts,
                    'balance': round(total_eq * (_BAL_BACKFILL_DECAY ** d), 4),
                    'source': 'backfill'
                })
            if added_points:
                balance_repo.insert_points(session, account_key, added_points)
                balance_repo.trim_oldest(session, account_key, _BAL_HISTORY_MAX_POINTS)
                logger.info(f"[BalanceHistory] {account_name} | 回溯初始化 | 生成{len(added_points)}天历史点 | 当前余额: {total_eq} USDT")
            return len(added_points)


def _fetch_recent_bills(account_api, cutoff_ms, max_pages=20):
    """分页拉取近7日账单流水（GET /api/v5/account/bills）

    Returns:
        (bills, error_msg)：bills 为 OKX 原始记录列表，error_msg 非空表示部分拉取失败
    """
    all_bills = []
    after_ts = ''
    for _ in range(max_pages):
        kwargs = {'limit': '100'}
        if after_ts:
            kwargs['after'] = after_ts
        result = _rl_call(account_api, 'get_account_bills', **kwargs)
        if not _okx_success(result):
            return all_bills, result.get('msg', '账单流水查询失败')
        batch = result.get('data') or []
        if not batch:
            break
        all_bills.extend(batch)
        try:
            oldest_ts = min(int(b.get('ts') or 0) for b in batch)
        except (TypeError, ValueError):
            break
        if oldest_ts <= cutoff_ms or len(batch) < 100:
            break
        after_ts = str(oldest_ts)
    return all_bills, ''


@api_bp.route('/api/account/balance-history', methods=['GET'])
def account_balance_history():
    """账户余额历史：本地余额快照 + OKX 账单流水合并为余额时间序列"""
    try:
        acct = request.args.get('account', '')
        try:
            days = max(1, min(7, int(request.args.get('days', '7'))))
        except (TypeError, ValueError):
            days = 7

        # 解析真实账号标识（兼容多账号切换）
        from api_config import get_api_config
        config = get_api_config(acct if acct else None)
        account_key = config['account']
        account_name = config.get('account_name', account_key)

        account_api = _get_api_client('account', acct or None)

        # 1) 实时总权益（USD估值）作为最新数据点，并追加快照入库
        bal_result = _rl_call(account_api, 'get_account_balance')
        if not _okx_success(bal_result) or not bal_result.get('data'):
            return jsonify(_make_response(bal_result))
        total_eq = round(float(bal_result['data'][0].get('totalEq') or 0), 4)
        now_ms = int(datetime.datetime.now().timestamp() * 1000)

        # 1.5) 回溯初始化：历史点少于阈值时，以当前余额按天衰减倒推补齐
        _backfill_balance_history(account_key, account_name, now_ms, total_eq, days)

        # 实时点追加快照入库
        _append_balance_snapshot(account_key, now_ms, total_eq)

        # 2) 拉取 days 范围内账单流水（OKX 仅支持近7日）
        cutoff_ms = now_ms - days * 86400000
        raw_bills, bill_err = _fetch_recent_bills(account_api, cutoff_ms)
        bills = []
        for b in raw_bills:
            try:
                ts = int(b.get('ts') or 0)
            except (TypeError, ValueError):
                continue
            if ts < cutoff_ms:
                continue
            bill_type = b.get('type', '')
            bills.append({
                'ts': ts,
                'time': _ts_to_str(ts),
                'ccy': b.get('ccy', ''),
                'change': b.get('balChg', '0'),
                'balAfter': b.get('bal', '0'),
                'type': bill_type,
                'reason': _BILL_TYPE_MAP.get(bill_type, f'类型{bill_type}' if bill_type else '未知')
            })
        bills.sort(key=lambda x: x['ts'])
        if bill_err:
            logger.warning(f"[BalanceHistory] {account_name} | 账单流水部分拉取失败: {bill_err}")

        # 3) 合并余额快照 + 实时点为时间序列，并用就近账单标注变动原因
        with _bal_history_lock, session_scope() as session:
            account_points = balance_repo.load_account_points(session, account_key)
        merged = {}
        for p in account_points:
            if isinstance(p, dict) and p.get('ts'):
                try:
                    merged[int(p['ts'])] = {
                        'ts': int(p['ts']),
                        'balance': round(float(p.get('balance') or 0), 4),
                        'source': 'backfill' if p.get('source') == 'backfill' else 'snapshot'
                    }
                except (TypeError, ValueError):
                    continue
        merged[now_ms] = {'ts': now_ms, 'balance': total_eq, 'source': 'live'}
        points = sorted(merged.values(), key=lambda x: x['ts'])
        for p in points:
            nearest = None
            for b in bills:
                if abs(b['ts'] - p['ts']) <= 600000:  # 10分钟窗口内就近匹配
                    if nearest is None or abs(b['ts'] - p['ts']) < abs(nearest['ts'] - p['ts']):
                        nearest = b
            p['reason'] = nearest['reason'] if nearest else ''
            p['time'] = _ts_to_str(p['ts'])

        # 4) 汇总指标（全量范围）
        balances = [p['balance'] for p in points]
        change_pct = round((balances[-1] - balances[0]) / balances[0] * 100, 2) if balances and balances[0] else 0.0
        summary = {
            'current': balances[-1] if balances else 0,
            'max': max(balances) if balances else 0,
            'min': min(balances) if balances else 0,
            'changePct': change_pct,
            'pointCount': len(points),
            'billCount': len(bills)
        }

        logger.info(f"[BalanceHistory] {account_name} | 查询 | 范围: 近{days}天 | 数据点: {len(points)} | 账单流水: {len(bills)} | 当前余额: {total_eq} USDT")

        data = {
            'account': account_key,
            'accountName': account_name,
            'days': days,
            'points': points,
            'bills': bills,
            'summary': summary
        }
        if bill_err:
            data['billWarning'] = f'部分账单流水拉取失败: {bill_err}'
        return jsonify({'code': 200, 'message': 'success', 'data': data})

    except Exception as e:
        logger.error(f"[BalanceHistory] 查询异常: {type(e).__name__}: {e}")
        return jsonify({'code': 500, 'message': str(e), 'data': None})
