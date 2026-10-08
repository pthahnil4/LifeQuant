#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星标币种行情管理模块
====================
管理 star币种行情.csv 的数据读取、价格刷新和导出功能。
支持 OKX API 实时行情刷新、CSV/XLSX 导出。
"""

import csv
import os
import io
import datetime
import logging
import threading
import time

logger = logging.getLogger(__name__)


# 只读接口限频/退避（问题#8）：本页刷新与同进程的实盘调度、行情扫描共用一个
# API key。导入失败时置 None，_rl() 退化为直连，不因限频模块不可用而拿不到价格。
try:
    from task.utils.okx_ratelimit import limited as _rl_limited
except ImportError:
    try:
        from crypto.task.utils.okx_ratelimit import limited as _rl_limited
    except ImportError:
        _rl_limited = None


def _rl(group, func, *args, **kwargs):
    """只读 OKX 接口的统一出口：节流 + 50011 退避；限频模块缺失时直通。"""
    return _rl_limited(group, func, *args, **kwargs) if _rl_limited else func(*args, **kwargs)

# =============================================================================
# 币种到 OKX 交易对映射
# =============================================================================
SYMBOL_MAP = {
    'BTC': 'BTC-USDT-SWAP',
    'NEAR': 'NEAR-USDT-SWAP',
    'INJ': 'INJ-USDT-SWAP',
    'XLM': 'XLM-USDT-SWAP',
    'TON': 'TON-USDT-SWAP',
    'TRUMP': 'TRUMP-USDT-SWAP',
    'DOT': 'DOT-USDT-SWAP',
    'UNI': 'UNI-USDT-SWAP',
    'LTC': 'LTC-USDT-SWAP',
    'TRX': 'TRX-USDT-SWAP',
    'BCH': 'BCH-USDT-SWAP',
}

# =============================================================================
# 币种中文名称映射（用于同步时自动填写）
# =============================================================================
NAME_MAP = {
    'BTC': '比特币',
    'ETH': '以太坊',
    'XRP': '瑞波币',
    'BNB': '币安币',
    'USDC': '美元稳定币',
    'SOL': '索拉纳',
    'TRX': '波场',
    'DOGE': '狗狗币',
    'ADA': '艾达币',
    'AVAX': '雪崩协议',
    'LTC': '莱特币',
    'SHIB': '柴犬币',
    'POL': '多边形',
    'XLM': '恒星币',
    'DASH': '达世币',
    'DOT': '波卡',
    'ATOM': '宇宙',
    'ETC': '以太经典',
    'BCH': '比特现金',
    'TON': '电报币',
    'CRO': '克罗诺斯',
    'XAUT': '数字黄金',
    'INJ': '因哲媞',
    'LINK': '链环',
    'NEAR': '万物协议',
    'UNI': '优尼',
    'TRUMP': '川普币',
}

# CSV 文件路径（相对于本模块）
_csv_path = None

# 最近刷新时间
_last_refresh_time = None
_refresh_status = {'status': 'idle', 'message': '', 'updated': 0, 'failed': 0}

# =============================================================================
# 列定义
# =============================================================================
CSV_COLUMNS = [
    '名称', '代码', '现价', '15分钟', '60分钟', '4小时', '日线',
    '上次交易时间(1H)', '方向(1H)', '上次交易价格(1H)', '策略盈亏(1H)',
    '持仓时间', '预测涨跌', '建议操作'
]


# =============================================================================
# 后台刷新进度追踪（类似 BatchTrendAnalyzer 的进度管理）
# =============================================================================

class _StarRefreshProgress:
    """线程安全的进度状态"""
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.status = "idle"       # idle / running / completed / error
            self.total = 0
            self.current = 0
            self.updated = 0
            self.failed = 0
            self.current_symbol = ""
            self.message = ""
            self.start_time = None
            self.end_time = None

    def start(self, total):
        with self._lock:
            self.status = "running"
            self.total = total
            self.current = 0
            self.updated = 0
            self.failed = 0
            self.current_symbol = ""
            self.message = "开始刷新..."
            self.start_time = datetime.datetime.now()
            self.end_time = None

    def update(self, current=0, symbol="", updated=None, failed=None, message=None):
        with self._lock:
            if current:
                self.current = current
            if symbol:
                self.current_symbol = symbol
            if updated is not None:
                self.updated = updated
            if failed is not None:
                self.failed = failed
            if message:
                self.message = message

    def finish(self):
        with self._lock:
            self.status = "completed"
            self.end_time = datetime.datetime.now()
            elapsed = (self.end_time - self.start_time).total_seconds() if self.start_time else 0
            self.message = f'刷新完成！成功 {self.updated}/{self.total}，失败 {self.failed}，耗时 {elapsed:.1f} 秒'

    def set_error(self, msg):
        with self._lock:
            self.status = "error"
            self.end_time = datetime.datetime.now()
            self.message = msg

    def to_dict(self):
        with self._lock:
            elapsed = 0
            if self.start_time:
                if self.end_time:
                    elapsed = (self.end_time - self.start_time).total_seconds()
                else:
                    elapsed = (datetime.datetime.now() - self.start_time).total_seconds()
            return {
                "status": self.status,
                "total": self.total,
                "current": self.current,
                "updated": self.updated,
                "failed": self.failed,
                "current_symbol": self.current_symbol,
                "message": self.message,
                "elapsed_seconds": round(elapsed, 1),
                "progress_pct": round(self.current / self.total * 100, 1) if self.total > 0 else 0,
            }


_refresh_progress = _StarRefreshProgress()


def get_refresh_progress():
    """获取当前刷新进度（供 Flask 查询）"""
    return _refresh_progress.to_dict()


# =============================================================================
# 后台刷新任务
# =============================================================================

def _refresh_background(records):
    """
    后台线程执行的刷新任务。
    每币种只调 4 次 calculate_single_coin_data（15m, 1H, 4H, 1D），
    从 1H 结果中直接提取 trade_info，消除冗余的第 5 次调用。
    
    Args:
        records: 预读取的 CSV 数据列表（进度已由调用方初始化）
    """
    global _last_refresh_time, _refresh_status

    # 不再调用 reset()/start()——调用方 refresh_full_data() 已初始化进度
    from .real_strategy_adapter import calculate_single_coin_data

    total = len(records)
    updated = 0
    failed = 0
    errors = []

    for i, record in enumerate(records):
        code = record.get('代码', '').strip()
        if not code:
            continue

        inst_id = SYMBOL_MAP.get(code)
        if not inst_id:
            failed += 1
            errors.append(f'{code}(无映射)')
            _refresh_progress.update(current=i + 1, updated=updated, failed=failed)
            continue

        _refresh_progress.update(current=i + 1, symbol=code, message=f'正在处理 {code} ({i + 1}/{total})...')

        try:
            # 1. 获取最新现价（快速，单独从 OKX ticker API 获取）
            price = get_okx_ticker_price(inst_id)
            if price is not None:
                record['现价'] = price

            # 2. 分别获取各周期数据（15m, 1H, 4H, 1D），从 1H 提取交易数据
            #    不再调用 calculate_multi_period_data（它内部循环调4次 + 丢弃 trade_info）
            bars_config = [
                ('15m', '15分钟', False),    # (bar, CSV列名, 是否提取交易数据)
                ('1H', '60分钟', True),      # 1H 还要提取交易时间/方向/盈亏/持仓
                ('4H', '4小时', False),
                ('1D', '日线', False),
            ]

            for bar, col_name, extract_trade in bars_config:
                try:
                    data = calculate_single_coin_data(inst_id, bar=bar, max_retries=1)
                    if data:
                        # 趋势方向
                        signal = data.get('analysis', {}).get('action_signal', '观望')
                        if signal == '上涨':
                            record[col_name] = '上涨'
                        elif signal == '下跌':
                            record[col_name] = '下跌'
                        else:
                            record[col_name] = '--'

                        # 如果是 1H 周期，提取交易数据
                        if extract_trade and data.get('trade_info'):
                            ti = data['trade_info']
                            analysis = data.get('analysis', {})

                            trade_time = ti.get('trade_time', '')
                            if trade_time:
                                record['上次交易时间(1H)'] = trade_time

                            modify_flag = analysis.get('modify_flag', 'wait')
                            direction = '做多' if modify_flag == 'rise' else ('做空' if modify_flag == 'fall' else '')
                            if direction:
                                record['方向(1H)'] = direction

                            last_trade_price = ti.get('last_trade_price', 0)
                            if last_trade_price and last_trade_price > 0:
                                record['上次交易价格(1H)'] = str(last_trade_price)

                            profit = ti.get('current_profit', None)
                            if profit is not None:
                                record['策略盈亏(1H)'] = f'{profit:+.2f}%'

                            hold_time = ti.get('hold_time', '')
                            if hold_time:
                                record['持仓时间'] = hold_time
                except Exception as e:
                    logger.warning(f'获取 {code} {bar} 数据失败: {e}')

            updated += 1

        except Exception as e:
            failed += 1
            errors.append(f'{code}({str(e)[:30]})')
            logger.error(f'刷新 {code} 完整数据失败: {e}')

        _refresh_progress.update(updated=updated, failed=failed)

        # 每处理 3 个或最后一个时保存 CSV（避免丢失全部数据）
        if (i + 1) % 3 == 0 or (i + 1) == total:
            write_star_market_data(records)

    # 最终保存（如果总数不是 3 的倍数）
    if total % 3 != 0:
        write_star_market_data(records)

    _last_refresh_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    msg_parts = []
    if updated > 0:
        msg_parts.append(f'成功刷新 {updated}/{total} 个币种')
    if failed > 0:
        msg_parts.append(f'{failed} 个币种失败: {", ".join(errors[:3])}')
        if len(errors) > 3:
            msg_parts[-1] += f' 等{len(errors)}个'

    _refresh_status = {
        'status': 'completed' if failed == 0 else 'partial',
        'message': '; '.join(msg_parts),
        'updated': updated,
        'failed': failed,
        'total': total,
        'last_refresh_time': _last_refresh_time,
    }

    _refresh_progress.finish()
    logger.info(f'星标行情刷新完成: {_refresh_status}')


def start_refresh():
    """启动后台刷新线程，立即返回。必须先调用 refresh_full_data 初始化进度。"""
    # 由 refresh_full_data() 传入预读取的数据
    raise RuntimeError('请使用 refresh_full_data() 而非直接调用 start_refresh()')


# =============================================================================
# 主入口：refresh_full_data（预初始化进度 + 启动后台线程）
# =============================================================================

def refresh_full_data():
    """
    启动后台刷新并立即返回（不再同步阻塞）。
    
    关键改进：在启动线程之前，先读取 CSV 并初始化进度状态为 "running"，
    确保前端调用 refresh-progress 时立即能看到正确的运行状态。
    """
    # 1. 预读取 CSV 数据
    records = read_star_market_data()
    if not records:
        _refresh_progress.set_error('CSV 无数据可刷新')
        return {
            'status': 'error',
            'message': 'CSV 无数据可刷新',
            'updated': 0,
            'failed': 0,
        }

    # 2. 在启动线程前初始化进度（保证 HTTP 响应时状态已是 running）
    total = len(records)
    _refresh_progress.reset()
    _refresh_progress.start(total)

    # 3. 启动后台线程，传入预读取的数据
    thread = threading.Thread(
        target=_refresh_background,
        args=(records,),
        daemon=True,
        name='star-market-refresh',
    )
    thread.start()

    return {
        'status': 'started',
        'message': '刷新任务已在后台启动，请使用 /api/star-market/refresh-progress 查询进度',
        'updated': 0,
        'failed': 0,
    }


def _get_csv_path():
    """获取 CSV 文件的绝对路径"""
    global _csv_path
    if _csv_path is None:
        _csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'star币种行情.csv')
    return _csv_path


def set_csv_path(path):
    """设置 CSV 文件路径（用于测试）"""
    global _csv_path
    _csv_path = path


def read_star_market_data():
    """
    读取星标币种行情数据（迁移批次7b 起 DB 优先，star_market 表）。
    DB 无数据/不可用时回退 CSV 文件，保证容错行为与 CSV 版一致。

    Returns:
        list[dict]: 每行数据的字典列表
    """
    try:
        from .database import session_scope
        from . import market_data_repo
        with session_scope() as s:
            records = market_data_repo.load_star_rows(s)
        if records:
            return records
    except Exception as e:
        logger.warning(f'从DB读取星标行情失败，回退CSV文件: {e}')

    csv_path = _get_csv_path()
    if not os.path.exists(csv_path):
        logger.warning(f'CSV 文件不存在: {csv_path}')
        return []

    records = []
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # 过滤空行
                if row.get('代码', '').strip():
                    records.append(row)
    except Exception as e:
        logger.error(f'读取 CSV 文件失败: {e}')
        return []

    return records


def write_star_market_data(records):
    """
    将数据写回（迁移批次7b 起 DB 主存 + CSV 文件双写，文件保留为兜底数据源）。

    Args:
        records (list[dict]): 数据记录列表
    """
    try:
        from .database import session_scope
        from . import market_data_repo
        with session_scope() as s:
            market_data_repo.save_star_rows(s, records)
    except Exception as e:
        logger.warning(f'写入星标行情到DB失败，仅写CSV文件: {e}')

    csv_path = _get_csv_path()
    try:
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(records)
        logger.info(f'成功写入 {len(records)} 条数据到 {csv_path}')
        return True
    except Exception as e:
        logger.error(f'写入 CSV 文件失败: {e}')
        return False


def get_okx_ticker_price(inst_id):
    """
    从 OKX API 获取指定交易对的最新价格。

    Args:
        inst_id (str): OKX 交易对 ID, 如 'BTC-USDT-SWAP'

    Returns:
        str: 最新价格字符串，失败返回 None
    """
    try:
        from api_config import get_api_config
        config = get_api_config()
        flag = config['flag']

        import okx.MarketData as MarketData
        market_api = MarketData.MarketAPI(flag=flag)
        result = _rl('market_ticker', market_api.get_ticker, instId=inst_id)

        if result.get('code') == '0' and result.get('data'):
            return result['data'][0].get('last')
        else:
            logger.warning(f'获取 {inst_id} 行情失败: {result.get("msg", "未知错误")}')
            return None
    except ImportError as e:
        logger.error(f'导入 OKX SDK 失败: {e}')
        return None
    except Exception as e:
        logger.error(f'获取 {inst_id} 价格异常: {e}')
        return None


def update_record(code, updates):
    """
    更新指定币种记录的特定字段并写回 CSV。

    Args:
        code (str): 币种代码，如 'BTC'
        updates (dict): 要更新的字段字典，如 {'预测涨跌': '看涨', '建议操作': '买入'}

    Returns:
        dict: 包含 status, message, record 的结果
    """
    records = read_star_market_data()
    if not records:
        return {'status': 'error', 'message': 'CSV 无数据'}

    target = None
    for record in records:
        if record.get('代码', '').strip() == code:
            target = record
            break

    if target is None:
        return {'status': 'error', 'message': f'未找到代码为 {code} 的币种'}

    # 只允许更新允许的字段
    allowed_fields = {'预测涨跌', '建议操作'}
    changed = {}
    for key, value in updates.items():
        if key in allowed_fields:
            target[key] = str(value).strip() if value is not None else ''
            changed[key] = target[key]

    if not changed:
        return {'status': 'error', 'message': '没有可更新的字段'}

    success = write_star_market_data(records)
    if not success:
        return {'status': 'error', 'message': '写入 CSV 失败'}

    return {
        'status': 'success',
        'message': f'已更新 {len(changed)} 个字段',
        'changed': changed,
        'record': target
    }


def get_all_coin_codes():
    """获取 CSV 中所有币种代码列表"""
    records = read_star_market_data()
    return [r.get('代码', '').strip() for r in records if r.get('代码', '').strip()]


def sync_from_starred_config():
    """
    从 config.json 的 starred_coins 同步星标行情 CSV。
    
    单向同步（监控台 → 星标行情）：
    - 新增监控台有星标但 CSV 中没有的币种
    - 移除 CSV 中存在但监控台已取消星标的币种
    - 保留 CSV 中已有数据的币种不受影响
    
    Returns:
        dict: 包含 status, message, added, removed, kept 的结果
    """
    try:
        from .real_strategy_adapter import get_starred_coins
    except ImportError:
        return {'status': 'error', 'message': '无法获取配置模块'}

    # 1. 获取监控台的星标币种列表（instId 格式）
    starred_instids = get_starred_coins()
    if not starred_instids:
        # 如果监控台没有星标，清空 CSV（仅保留表头）
        old_records = read_star_market_data()
        if old_records:
            write_star_market_data([])
            return {'status': 'success', 'message': '监控台无星标币种，已清空 CSV', 'added': 0, 'removed': len(old_records), 'kept': 0}
        return {'status': 'success', 'message': '监控台无星标币种，CSV 已为空', 'added': 0, 'removed': 0, 'kept': 0}

    # 2. 构建反向映射（instId → 短代码）
    #    优先使用 SYMBOL_MAP，未覆盖的从 instId 提取
    instid_to_code = {}
    for code, instid in SYMBOL_MAP.items():
        instid_to_code[instid] = code

    starred_codes = set()
    for instid in starred_instids:
        if instid in instid_to_code:
            code = instid_to_code[instid]
        else:
            # 从 "XXX-USDT-SWAP" 中提取 "XXX"
            code = instid.replace('-USDT-SWAP', '').strip()
        if code:
            starred_codes.add(code)

    if not starred_codes:
        write_star_market_data([])
        return {'status': 'success', 'message': '星标币种解析后为空，已清空 CSV', 'added': 0, 'removed': 0, 'kept': 0}

    # 3. 读取现有的 CSV 数据（按代码索引）
    old_records = read_star_market_data()
    existing_map = {}  # code → record
    for rec in old_records:
        code = rec.get('代码', '').strip()
        if code:
            existing_map[code] = rec

    # 4. 计算增删
    existing_codes = set(existing_map.keys())
    codes_to_add = starred_codes - existing_codes   # 需要新增
    codes_to_remove = existing_codes - starred_codes  # 需要删除

    # 5. 构建新数据（保留 CSV 原有行顺序）
    new_records = []
    kept_codes = existing_codes & starred_codes

    # 先按 CSV 原有顺序保留仍在星标中的记录
    for code, rec in existing_map.items():
        if code in kept_codes:
            new_records.append(rec)

    # 再追加新增的币种（按 SYMBOL_MAP 定义顺序）
    for code in SYMBOL_MAP:
        if code in (starred_codes - existing_codes):
            name = NAME_MAP.get(code, code)  # 优先使用名称映射
            new_records.append({
                '名称': name,
                '代码': code,
                '现价': '',
                '15分钟': '',
                '60分钟': '',
                '4小时': '',
                '日线': '',
                '上次交易时间(1H)': '',
                '方向(1H)': '',
                '上次交易价格(1H)': '',
                '策略盈亏(1H)': '',
                '持仓时间': '',
                '预测涨跌': '',
                '建议操作': '',
            })

    # 最后追加不在 SYMBOL_MAP 中的新增币种（防御性处理）
    for code in sorted(starred_codes - existing_codes - set(SYMBOL_MAP.keys())):
        name = NAME_MAP.get(code, code)
        new_records.append({
            '名称': name,
            '代码': code,
            '现价': '',
            '15分钟': '',
            '60分钟': '',
            '4小时': '',
            '日线': '',
            '上次交易时间(1H)': '',
            '方向(1H)': '',
            '上次交易价格(1H)': '',
            '策略盈亏(1H)': '',
            '持仓时间': '',
            '预测涨跌': '',
            '建议操作': '',
        })

    # 6. 写回 CSV
    success = write_star_market_data(new_records)
    if not success:
        return {'status': 'error', 'message': '写入 CSV 失败'}

    added_count = len(codes_to_add)
    removed_count = len(codes_to_remove)
    kept_count = len(kept_codes)

    msg_parts = []
    if added_count > 0:
        msg_parts.append(f'新增 {added_count} 个币种')
    if removed_count > 0:
        msg_parts.append(f'移除 {removed_count} 个币种')
    if kept_count > 0:
        msg_parts.append(f'保留 {kept_count} 个币种')

    logger.info(f'星标行情同步完成: {" | ".join(msg_parts)}')

    return {
        'status': 'success',
        'message': ' | '.join(msg_parts),
        'added': added_count,
        'removed': removed_count,
        'kept': kept_count,
        'total': len(new_records),
    }


def clear_all_data():
    """
    清空所有币种除「名称」「代码」外的其他列数据，并写回 CSV。

    Returns:
        dict: 包含 status, message, cleared_count 的结果
    """
    records = read_star_market_data()
    if not records:
        return {'status': 'error', 'message': 'CSV 无数据'}

    # 需要清空的列（名称和代码保留）
    clear_columns = [
        '现价', '15分钟', '60分钟', '4小时', '日线',
        '上次交易时间(1H)', '方向(1H)', '上次交易价格(1H)',
        '策略盈亏(1H)', '持仓时间', '预测涨跌', '建议操作'
    ]

    cleared = 0
    for record in records:
        for col in clear_columns:
            if col in record:
                record[col] = ''
        cleared += 1

    success = write_star_market_data(records)
    if not success:
        return {'status': 'error', 'message': '写入 CSV 失败'}

    return {
        'status': 'success',
        'message': f'已清空 {cleared} 个币种的行情数据（保留名称和代码）',
        'cleared_count': cleared
    }


def refresh_prices():
    """
    从 OKX API 刷新 CSV 中所有币种的现价。

    Returns:
        dict: 包含 status, message, updated, failed 的刷新结果
    """
    global _last_refresh_time, _refresh_status

    records = read_star_market_data()
    if not records:
        _refresh_status = {'status': 'error', 'message': 'CSV 无数据可刷新', 'updated': 0, 'failed': 0}
        return _refresh_status

    total = len(records)
    updated = 0
    failed = 0
    failed_coins = []
    errors = []

    for i, record in enumerate(records):
        code = record.get('代码', '').strip()
        if not code:
            continue

        # 获取 OKX 交易对
        inst_id = SYMBOL_MAP.get(code)
        if not inst_id:
            failed += 1
            failed_coins.append(code)
            logger.warning(f'币种 {code} 无对应 OKX 交易对映射')
            continue

        # 获取最新价格
        price = get_okx_ticker_price(inst_id)
        if price is not None:
            record['现价'] = price
            updated += 1
        else:
            failed += 1
            failed_coins.append(code)
            errors.append(f'{code}({inst_id})')

    # 写回 CSV
    if updated > 0 or failed > 0:
        write_success = write_star_market_data(records)
        if not write_success:
            _refresh_status = {
                'status': 'error',
                'message': '价格数据获取成功但写入文件失败',
                'updated': updated,
                'failed': failed,
            }
            return _refresh_status

    _last_refresh_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    msg_parts = []
    if updated > 0:
        msg_parts.append(f'成功更新 {updated}/{total} 个币种价格')
    if failed > 0:
        msg_parts.append(f'{failed} 个币种更新失败: {", ".join(errors[:3])}')
        if len(errors) > 3:
            msg_parts[-1] += f' 等{len(errors)}个'

    _refresh_status = {
        'status': 'completed' if failed == 0 else 'partial',
        'message': '; '.join(msg_parts),
        'updated': updated,
        'failed': failed,
        'total': total,
        'last_refresh_time': _last_refresh_time,
    }

    return _refresh_status


def get_refresh_status():
    """获取最近一次刷新状态"""
    global _refresh_status
    status = dict(_refresh_status)
    status['last_refresh_time'] = _last_refresh_time
    return status


def refresh_trend_column(bar, col_name):
    """
    刷新指定周期趋势方向列（仅刷新一个周期，不更新其他字段）。
    同步执行，完成后返回结果。

    Args:
        bar (str): OKX K线周期，如 '15m', '1H'
        col_name (str): CSV 列名，如 '15分钟', '60分钟'

    Returns:
        dict: 包含 status, message, updated, failed 的结果
    """
    from .real_strategy_adapter import calculate_single_coin_data

    records = read_star_market_data()
    if not records:
        return {'status': 'error', 'message': 'CSV 无数据可刷新'}

    total = len(records)
    updated = 0
    failed = 0
    errors = []

    for record in records:
        code = record.get('代码', '').strip()
        if not code:
            continue

        inst_id = SYMBOL_MAP.get(code)
        if not inst_id:
            failed += 1
            errors.append(f'{code}(无映射)')
            continue

        try:
            data = calculate_single_coin_data(inst_id, bar=bar, max_retries=1)
            if data:
                signal = data.get('analysis', {}).get('action_signal', '观望')
                if signal == '上涨':
                    record[col_name] = '上涨'
                elif signal == '下跌':
                    record[col_name] = '下跌'
                else:
                    record[col_name] = '--'
                updated += 1
            else:
                failed += 1
                errors.append(code)
        except Exception as e:
            failed += 1
            errors.append(f'{code}({str(e)[:30]})')
            logger.error(f'刷新 {code} {bar} 趋势失败: {e}')

    # 写回 CSV
    write_star_market_data(records)

    msg_parts = []
    if updated > 0:
        msg_parts.append(f'成功刷新 {updated}/{total} 个币种')
    if failed > 0:
        msg_parts.append(f'{failed} 个失败: {", ".join(errors[:3])}')
        if len(errors) > 3:
            msg_parts[-1] += f' 等{len(errors)}个'

    return {
        'status': 'completed' if failed == 0 else 'partial',
        'message': '; '.join(msg_parts),
        'updated': updated,
        'failed': failed,
        'total': total,
    }


# =============================================================================
# 拖拽排序支持
# =============================================================================


def reorder_records(ordered_codes):
    """
    按照 ordered_codes 的顺序重新排列 CSV 行。
    只在 CSV 中存在的代码行受影响，不在 ordered_codes 中的行追加到末尾。

    Args:
        ordered_codes (list[str]): 排序后的币种代码列表

    Returns:
        dict: 包含 status, message 的结果
    """
    records = read_star_market_data()
    if not records:
        return {'status': 'error', 'message': 'CSV 无数据'}

    # 建立 code→record 索引
    record_map = {}
    for rec in records:
        code = rec.get('代码', '').strip()
        if code:
            record_map[code] = rec

    # 按 ordered_codes 排序
    new_records = []
    remaining = set(record_map.keys())

    for code in ordered_codes:
        if code in record_map:
            new_records.append(record_map[code])
            remaining.discard(code)

    # 追加不在 ordered_codes 中的记录
    for code in remaining:
        new_records.append(record_map[code])

    success = write_star_market_data(new_records)
    if not success:
        return {'status': 'error', 'message': '写入 CSV 失败'}

    return {
        'status': 'success',
        'message': f'已更新 {len(new_records)} 条记录顺序',
        'total': len(new_records),
    }


# =============================================================================
# 导出功能
# =============================================================================

def export_as_csv():
    """
    导出 CSV 数据为字节流。

    Returns:
        (bytes, str): (CSV 字节数据, 文件名)
    """
    records = read_star_market_data()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(records)

    csv_content = output.getvalue().encode('utf-8-sig')
    output.close()

    filename = f'star币种行情_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    return csv_content, filename


def export_as_xlsx():
    """
    导出数据为 XLSX 格式字节流。

    Returns:
        (bytes, str): (XLSX 字节数据, 文件名) 或 (None, error_msg)
    """
    records = read_star_market_data()
    if not records:
        return None, '无数据可导出'

    try:
        import pandas as pd
        df = pd.DataFrame(records)
        output = io.BytesIO()

        # 使用 pandas 写入 Excel，依赖 openpyxl
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='星标币种行情', index=False)

        xlsx_content = output.getvalue()
        output.close()

        filename = f'star币种行情_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        return xlsx_content, filename

    except ImportError as e:
        logger.error(f'导出 XLSX 所需依赖缺失: {e}')
        return None, f'缺少依赖: {e}. 请安装 openpyxl: pip install openpyxl'
    except Exception as e:
        logger.error(f'导出 XLSX 失败: {e}')
        return None, f'导出失败: {e}'


def get_data_for_display():
    """
    获取用于前端展示的数据（直接读取 CSV，不自动同步）。

    不再自动调用 sync_from_starred_config()，避免每次加载数据时
    打乱 CSV 行顺序（用户拖拽排序会被覆盖）。
    用户如需同步，请使用页面上的「同步星标」按钮。

    Returns:
        dict: {records, total, last_refresh_time}
    """
    records = read_star_market_data()
    return {
        'records': records,
        'total': len(records),
        'last_refresh_time': _last_refresh_time,
    }
