#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 2 自动搬运（2026-10-08）。
# 原名: futures/star_market.py  →  现位置: crypto/futures/star_market.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底已移除。
"""
星标品种行情核心模块
====================
管理 star品种行情.csv 的同步、趋势刷新、拖拽排序和数据读取。
依赖 futures_adapter.py 的 Pro3 策略和 akshare 数据接口。
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import datetime
import logging
import os
import sys
import threading
import time
import traceback
from typing import Dict, List, Optional

import pandas as pd

# 数据文件定位基准目录（原用于 sys.path 兜底，现仅用于定位同目录 CSV/JSON）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logger = logging.getLogger(__name__)

STAR_CSV_PATH = os.path.join(BASE_DIR, "star品种行情.csv")

# 请求间隔（秒），防止 akshare 频率限制
REQUEST_DELAY = 1.5
COOLDOWN_BATCH_SIZE = 8
COOLDOWN_SECONDS = 4.0

# bar → CSV 趋势列名映射（期货不使用4H，日线已涵盖4~6小时时段）
BAR_TO_TREND_COL = {
    '15m': '15m趋势',
    '1H':  '1H趋势',
    '1D':  '日线趋势',
    '1W':  '周线趋势',
}

# bar → CSV MACD/ADX 列名映射
BAR_TO_IND_COLS = {
    '15m': ('15m_MACD', '15m_ADX'),
    '1H':  ('1H_MACD',  '1H_ADX'),
    '1D':  ('1D_MACD',  '1D_ADX'),
    '1W':  ('1W_MACD',  '1W_ADX'),
}

# 趋势列列表（用于保持列顺序）
ALL_TREND_COLS = [
    '15m趋势', '1H趋势', '日线趋势', '周线趋势',
]
ALL_IND_COLS = [
    '15m_MACD', '15m_ADX', '1H_MACD', '1H_ADX',
    '1D_MACD', '1D_ADX', '1W_MACD', '1W_ADX',
]
ALL_TRADE_COLS = [
    '上次交易时间(1H)', '方向(1H)', '上次交易价格(1H)', '策略盈亏(1H)', '持仓时间',
]


# ====================================================================
#  进度状态管理
# ====================================================================

class _StarProgressState:
    """线程安全的刷新进度状态"""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.status = "idle"        # idle / running / completed / error
            self.total = 0
            self.current = 0
            self.success = 0
            self.error = 0
            self.current_symbol = ""
            self.message = ""
            self.start_time = None
            self.end_time = None

    def start(self, total: int, bar: str = ""):
        with self._lock:
            self.status = "running"
            self.total = total
            self.current = 0
            self.success = 0
            self.error = 0
            self.current_symbol = ""
            self.message = "开始刷新 %s 趋势..." % bar
            self.start_time = datetime.datetime.now()
            self.end_time = None

    def update(self, current: int, symbol: str = "",
               success: int = None, error: int = None, message: str = None):
        with self._lock:
            self.current = current
            if symbol:
                self.current_symbol = symbol
            if success is not None:
                self.success = success
            if error is not None:
                self.error = error
            if message:
                self.message = message

    def finish(self, message: str = None):
        with self._lock:
            self.status = "completed"
            self.end_time = datetime.datetime.now()
            if message:
                self.message = message
            else:
                elapsed = (self.end_time - self.start_time).total_seconds() \
                    if self.start_time else 0
                self.message = (
                    "刷新完成！成功 %d/%d，失败 %d，耗时 %.1f 秒"
                    % (self.success, self.total, self.error, elapsed)
                )

    def set_error(self, message: str):
        with self._lock:
            self.status = "error"
            self.end_time = datetime.datetime.now()
            self.message = message

    def to_dict(self) -> Dict:
        with self._lock:
            elapsed = 0.0
            if self.start_time:
                if self.end_time:
                    elapsed = (self.end_time - self.start_time).total_seconds()
                else:
                    elapsed = (datetime.datetime.now() - self.start_time
                               ).total_seconds()
            return {
                "status": self.status,
                "total": self.total,
                "current": self.current,
                "success": self.success,
                "error": self.error,
                "current_symbol": self.current_symbol,
                "message": self.message,
                "elapsed_seconds": round(elapsed, 1),
                "progress_pct": (
                    round(self.current / self.total * 100, 1)
                    if self.total > 0 else 0
                ),
            }


# 全局进度实例（供 Flask 蓝图查询）
_progress = _StarProgressState()


def get_refresh_progress() -> Dict:
    """获取当前刷新进度"""
    return _progress.to_dict()


# ====================================================================
#  CSV 读写工具
# ====================================================================

def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """确保 DataFrame 包含所有必需列，缺失列补空值"""
    required = (
        ['品种', '代码', '最新价']
        + ALL_TREND_COLS
        + ALL_TRADE_COLS
        + ALL_IND_COLS
    )
    for col in required:
        if col not in df.columns:
            df[col] = ''
    return df


def _read_star_csv() -> pd.DataFrame:
    """读取 star品种行情.csv 并标准化"""
    try:
        df = pd.read_csv(STAR_CSV_PATH, encoding='utf-8-sig')
    except FileNotFoundError:
        # 文件不存在，返回空 DataFrame
        df = pd.DataFrame(columns=[
            '品种', '代码', '最新价'] + ALL_TREND_COLS + ALL_TRADE_COLS + ALL_IND_COLS)
        return df

    df = df.dropna(subset=['代码']).reset_index(drop=True)
    df['代码'] = df['代码'].astype(str).str.strip()
    df = _ensure_columns(df)
    return df


def _write_star_csv(df: pd.DataFrame):
    """写入 star品种行情.csv"""
    df.to_csv(STAR_CSV_PATH, index=False, encoding='utf-8-sig')
    logger.info("star CSV 已保存: %s (%d 行)", STAR_CSV_PATH, len(df))


# ====================================================================
#  核心功能 1：同步星标品种到 CSV（保持行顺序）
# ====================================================================

def sync_starred_to_csv() -> int:
    """将 futures_config.json 中的 starred 列表同步到 star品种行情.csv

    关键行为（Bug 修复）：
    - 按 CSV 原有顺序保留仍在 starred 中的记录
    - 仅追加新增品种到末尾
    - 删除已取消星标的品种行

    Returns
    -------
    int
        同步后 CSV 行数
    """
    from crypto.futures.futures_adapter import get_starred_futures, get_futures_code_name_map

    starred_codes = get_starred_futures()
    name_map = get_futures_code_name_map()

    df = _read_star_csv()

    # 已有代码集合
    existing_codes = set(df['代码'].tolist())

    # 1. 删除已不在 starred 中的行（保持顺序）
    starred_set = set(starred_codes)
    mask_keep = df['代码'].isin(starred_set)
    df = df[mask_keep].reset_index(drop=True)

    # 2. 找出需要新增的品种（在 starred 中但不在 CSV 中）
    current_codes = set(df['代码'].tolist())
    new_codes = [c for c in starred_codes if c not in current_codes]

    if new_codes:
        new_rows = []
        for code in new_codes:
            display_name = name_map.get(code, code)
            new_rows.append({
                '品种': display_name,
                '代码': code,
                '最新价': '',
                '15m趋势': '', '1H趋势': '',
                '日线趋势': '', '周线趋势': '',
                '上次交易时间(1H)': '', '方向(1H)': '',
                '上次交易价格(1H)': '', '策略盈亏(1H)': '', '持仓时间': '',
                '15m_MACD': '', '15m_ADX': '',
                '1H_MACD': '', '1H_ADX': '',
                '1D_MACD': '', '1D_ADX': '',
                '1W_MACD': '', '1W_ADX': '',
            })
        new_df = pd.DataFrame(new_rows)
        df = pd.concat([df, new_df], ignore_index=True)

    _write_star_csv(df)
    return len(df)


# ====================================================================
#  核心功能 2：刷新趋势列
# ====================================================================

def refresh_trend_column(bar: str) -> Dict:
    """刷新 star品种行情.csv 中指定周期的趋势数据

    Parameters
    ----------
    bar : str
        K线周期，可选: '15m', '1H', '4H', '1D', '1W'

    Returns
    -------
    dict
        {'total': int, 'success': int, 'error': int}
    """
    from crypto.futures.futures_adapter import calculate_futures_multi_period

    trend_col = BAR_TO_TREND_COL.get(bar)
    if not trend_col:
        raise ValueError("不支持的周期: %s，可选: %s" % (bar, list(BAR_TO_TREND_COL.keys())))

    ind_cols = BAR_TO_IND_COLS.get(bar, ('', ''))
    macd_col, adx_col = ind_cols

    df = _read_star_csv()
    codes = df['代码'].tolist()
    total = len(codes)

    if total == 0:
        return {'total': 0, 'success': 0, 'error': 0}

    _progress.start(total, bar)
    success_count = 0
    error_count = 0

    for idx, code in enumerate(codes):
        _progress.update(
            current=idx + 1,
            symbol=code,
            message="刷新 %s: %d/%d — %s" % (bar, idx + 1, total, code),
        )

        try:
            result = calculate_futures_multi_period(code, [bar])
            period_data = result.get(bar, {})

            if 'error' in period_data:
                raise Exception(period_data['error'])

            # 提取趋势方向
            modify_flag = period_data.get('modify_flag', 'wait')
            if modify_flag == 'rise':
                trend_text = '上涨'
            elif modify_flag == 'fall':
                trend_text = '下跌'
            else:
                trend_text = '观望'

            # 更新趋势列
            row_mask = df['代码'] == code
            df.loc[row_mask, trend_col] = trend_text

            # 更新 MACD/ADX 指标列
            macd_val = period_data.get('macd_histogram', '')
            adx_val = period_data.get('adx', '')
            if macd_col and macd_val != '':
                df.loc[row_mask, macd_col] = round(float(macd_val), 4)
            if adx_col and adx_val != '':
                df.loc[row_mask, adx_col] = round(float(adx_val), 2)

            # 更新最新价
            price = period_data.get('price', '')
            if price != '':
                df.loc[row_mask, '最新价'] = round(float(price), 4)

            # 1H 周期额外更新交易信息
            if bar == '1H':
                trade_price = period_data.get('trade_entry_price')
                trade_time = period_data.get('trade_entry_time')
                trade_dir = period_data.get('trade_direction')
                trade_profit = period_data.get('trade_profit_pct')

                if trade_time:
                    df.loc[row_mask, '上次交易时间(1H)'] = str(trade_time)
                if trade_dir:
                    dir_cn = '做多' if trade_dir == 'Long' else '做空'
                    df.loc[row_mask, '方向(1H)'] = dir_cn
                if trade_price is not None and trade_price != '':
                    df.loc[row_mask, '上次交易价格(1H)'] = round(float(trade_price), 4)
                if trade_profit is not None and trade_profit != '':
                    pct = float(trade_profit)
                    sign = '+' if pct >= 0 else ''
                    df.loc[row_mask, '策略盈亏(1H)'] = '%s%.2f%%' % (sign, pct)
                # 计算持仓时间
                if trade_time:
                    try:
                        trade_dt = pd.Timestamp(trade_time)
                        now = datetime.datetime.now()
                        delta = now - trade_dt.to_pydatetime()
                        days = delta.days
                        hours = delta.seconds // 3600
                        mins = (delta.seconds % 3600) // 60
                        if days > 0:
                            hold_str = '%d天%d小时%d分' % (days, hours, mins)
                        else:
                            hold_str = '%d小时%d分' % (hours, mins)
                        df.loc[row_mask, '持仓时间'] = hold_str
                    except Exception:
                        pass

            success_count += 1

        except Exception as e:
            logger.warning("[%s] %s 趋势刷新失败: %s", code, bar, e)
            error_count += 1

        _progress.update(
            current=idx + 1,
            success=success_count,
            error=error_count,
        )

        # 请求间延迟
        time.sleep(REQUEST_DELAY)

        # 冷却 CD
        if (idx + 1) % COOLDOWN_BATCH_SIZE == 0 and (idx + 1) < total:
            logger.info("冷却等待 %.1f 秒 (已处理 %d/%d)...",
                        COOLDOWN_SECONDS, idx + 1, total)
            time.sleep(COOLDOWN_SECONDS)

    # 保存结果
    _write_star_csv(df)
    _progress.finish()

    return {'total': total, 'success': success_count, 'error': error_count}


def refresh_trend_in_background(bar: str) -> threading.Thread:
    """后台线程运行趋势刷新

    Returns
    -------
    threading.Thread
        已启动的后台线程
    """
    thread = threading.Thread(
        target=refresh_trend_column,
        args=(bar,),
        daemon=True,
        name="star-trend-refresh-%s" % bar,
    )
    thread.start()
    return thread


# ====================================================================
#  核心功能 2b：刷新全部周期（15m + 1H + 1D + 1W）
# ====================================================================

ALL_REFRESH_BARS = ['15m', '1H', '1D', '1W']


def refresh_all_periods() -> Dict:
    """一次性刷新所有星标品种的全部周期趋势数据

    对每个品种调用一次 calculate_futures_multi_period 获取全部周期数据，
    减少重复请求，提高效率。

    Returns
    -------
    dict
        {'total': int, 'success': int, 'error': int}
    """
    from crypto.futures.futures_adapter import calculate_futures_multi_period

    df = _read_star_csv()
    codes = df['代码'].tolist()
    total = len(codes)

    if total == 0:
        return {'total': 0, 'success': 0, 'error': 0}

    _progress.start(total, '全部周期')
    success_count = 0
    error_count = 0

    for idx, code in enumerate(codes):
        _progress.update(
            current=idx + 1,
            symbol=code,
            message="刷新全部周期: %d/%d — %s" % (idx + 1, total, code),
        )

        try:
            result = calculate_futures_multi_period(code, ALL_REFRESH_BARS)

            row_mask = df['代码'] == code

            for bar in ALL_REFRESH_BARS:
                period_data = result.get(bar, {})
                if 'error' in period_data:
                    logger.warning("[%s] %s 周期数据异常: %s",
                                   code, bar, period_data['error'])
                    continue

                # 趋势方向
                trend_col = BAR_TO_TREND_COL.get(bar)
                if trend_col:
                    modify_flag = period_data.get('modify_flag', 'wait')
                    if modify_flag == 'rise':
                        trend_text = '上涨'
                    elif modify_flag == 'fall':
                        trend_text = '下跌'
                    else:
                        trend_text = '观望'
                    df.loc[row_mask, trend_col] = trend_text

                # MACD / ADX 指标
                ind_cols = BAR_TO_IND_COLS.get(bar, ('', ''))
                macd_col, adx_col = ind_cols
                macd_val = period_data.get('macd_histogram', '')
                adx_val = period_data.get('adx', '')
                if macd_col and macd_val != '':
                    df.loc[row_mask, macd_col] = round(float(macd_val), 4)
                if adx_col and adx_val != '':
                    df.loc[row_mask, adx_col] = round(float(adx_val), 2)

                # 最新价（取 1H 的为准）
                if bar == '1H':
                    price = period_data.get('price', '')
                    if price != '':
                        df.loc[row_mask, '最新价'] = round(float(price), 4)

                # 1H 周期额外更新交易信息
                if bar == '1H':
                    trade_price = period_data.get('trade_entry_price')
                    trade_time = period_data.get('trade_entry_time')
                    trade_dir = period_data.get('trade_direction')
                    trade_profit = period_data.get('trade_profit_pct')

                    if trade_time:
                        df.loc[row_mask, '上次交易时间(1H)'] = str(trade_time)
                    if trade_dir:
                        dir_cn = '做多' if trade_dir == 'Long' else '做空'
                        df.loc[row_mask, '方向(1H)'] = dir_cn
                    if trade_price is not None and trade_price != '':
                        df.loc[row_mask, '上次交易价格(1H)'] = round(
                            float(trade_price), 4)
                    if trade_profit is not None and trade_profit != '':
                        pct = float(trade_profit)
                        sign = '+' if pct >= 0 else ''
                        df.loc[row_mask, '策略盈亏(1H)'] = '%s%.2f%%' % (sign, pct)
                    # 持仓时间
                    if trade_time:
                        try:
                            trade_dt = pd.Timestamp(trade_time)
                            now = datetime.datetime.now()
                            delta = now - trade_dt.to_pydatetime()
                            days = delta.days
                            hours = delta.seconds // 3600
                            mins = (delta.seconds % 3600) // 60
                            if days > 0:
                                hold_str = '%d天%d小时%d分' % (days, hours, mins)
                            else:
                                hold_str = '%d小时%d分' % (hours, mins)
                            df.loc[row_mask, '持仓时间'] = hold_str
                        except Exception:
                            pass

            success_count += 1

        except Exception as e:
            logger.warning("[%s] 全周期刷新失败: %s", code, e)
            error_count += 1

        _progress.update(
            current=idx + 1,
            success=success_count,
            error=error_count,
        )

        # 请求间延迟
        time.sleep(REQUEST_DELAY)

        # 冷却 CD
        if (idx + 1) % COOLDOWN_BATCH_SIZE == 0 and (idx + 1) < total:
            logger.info("冷却等待 %.1f 秒 (已处理 %d/%d)...",
                        COOLDOWN_SECONDS, idx + 1, total)
            time.sleep(COOLDOWN_SECONDS)

    # 保存结果
    _write_star_csv(df)
    _progress.finish(
        "全周期刷新完成！成功 %d/%d，失败 %d" % (success_count, total, error_count)
    )

    return {'total': total, 'success': success_count, 'error': error_count}


def refresh_all_in_background() -> threading.Thread:
    """后台线程运行全周期刷新"""
    thread = threading.Thread(
        target=refresh_all_periods,
        daemon=True,
        name="star-refresh-all",
    )
    thread.start()
    return thread


# ====================================================================
#  核心功能 3：拖拽排序
# ====================================================================

def reorder_csv(order: List[str]) -> bool:
    """按指定顺序重排 star品种行情.csv

    Parameters
    ----------
    order : list of str
        品种代码的新顺序列表

    Returns
    -------
    bool
        是否成功
    """
    df = _read_star_csv()
    if df.empty:
        return False

    # 构建 code → row 的映射
    existing = {row['代码']: idx for idx, (_, row) in enumerate(df.iterrows())}

    # 验证顺序完整性
    order_set = set(order)
    csv_set = set(df['代码'].tolist())
    if order_set != csv_set:
        # 只使用 order 中存在于 CSV 的代码，忽略差异
        order = [c for c in order if c in existing]

    # 按新顺序重排
    new_indices = [existing[code] for code in order if code in existing]
    df_reordered = df.iloc[new_indices].reset_index(drop=True)

    _write_star_csv(df_reordered)
    logger.info("star CSV 已重排: %d 行", len(df_reordered))
    return True


# ====================================================================
#  核心功能 4：读取 CSV 供前端显示
# ====================================================================

def read_star_csv_for_display() -> List[Dict]:
    """读取 star品种行情.csv 数据供前端表格展示

    每次调用前先执行同步，确保 CSV 与 futures_config.json 保持一致，
    实现手动修改 config 时前端能即时反映。
    """
    try:
        sync_starred_to_csv()
    except Exception as e:
        logger.warning("read_star_csv_for_display 同步失败（继续读取 CSV）: %s", e)
    df = _read_star_csv()
    records = []
    for _, row in df.iterrows():
        record = {
            '品种': str(row.get('品种', '')),
            '代码': str(row.get('代码', '')),
            '最新价': str(row.get('最新价', '')),
            '15m趋势': str(row.get('15m趋势', '')),
            '1H趋势': str(row.get('1H趋势', '')),
            '日线趋势': str(row.get('日线趋势', '')),
            '周线趋势': str(row.get('周线趋势', '')),
            '上次交易时间(1H)': str(row.get('上次交易时间(1H)', '')),
            '方向(1H)': str(row.get('方向(1H)', '')),
            '上次交易价格(1H)': str(row.get('上次交易价格(1H)', '')),
            '策略盈亏(1H)': str(row.get('策略盈亏(1H)', '')),
            '持仓时间': str(row.get('持仓时间', '')),
            '15m_MACD': str(row.get('15m_MACD', '')),
            '15m_ADX': str(row.get('15m_ADX', '')),
            '1H_MACD': str(row.get('1H_MACD', '')),
            '1H_ADX': str(row.get('1H_ADX', '')),
            '1D_MACD': str(row.get('1D_MACD', '')),
            '1D_ADX': str(row.get('1D_ADX', '')),
            '1W_MACD': str(row.get('1W_MACD', '')),
            '1W_ADX': str(row.get('1W_ADX', '')),
        }
        records.append(record)
    return records


# ====================================================================
#  核心功能 5：数据导出（CSV / Excel）
# ====================================================================

EXPORT_COLUMNS = [
    '品种', '代码', '最新价',
    '15m趋势', '1H趋势', '日线趋势', '周线趋势',
    '方向(1H)', '上次交易价格(1H)', '上次交易时间(1H)',
    '策略盈亏(1H)', '持仓时间',
    '15m_MACD', '15m_ADX', '1H_MACD', '1H_ADX',
    '1D_MACD', '1D_ADX', '1W_MACD', '1W_ADX',
]


def export_star_csv() -> bytes:
    """导出星标行情数据为 CSV（UTF-8 BOM，Excel 可直接打开不乱码）

    Returns
    -------
    bytes
        CSV 文件的字节内容
    """
    df = _read_star_csv()
    cols = [c for c in EXPORT_COLUMNS if c in df.columns]
    df_out = df[cols]
    csv_text = df_out.to_csv(index=False, encoding='utf-8-sig')
    return csv_text.encode('utf-8-sig')


def export_star_excel() -> bytes:
    """导出星标行情数据为 Excel（.xlsx）

    Returns
    -------
    bytes
        xlsx 文件的字节内容
    """
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    df = _read_star_csv()
    cols = [c for c in EXPORT_COLUMNS if c in df.columns]

    wb = Workbook()
    ws = wb.active
    ws.title = '星标品种行情'

    # 表头样式
    header_font = Font(name='微软雅黑', bold=True, size=10)
    header_fill = PatternFill(start_color='4A90D9', end_color='4A90D9', fill_type='solid')
    header_font_white = Font(name='微软雅黑', bold=True, size=10, color='FFFFFF')
    thin_border = Border(
        left=Side(style='thin', color='CCCCCC'),
        right=Side(style='thin', color='CCCCCC'),
        top=Side(style='thin', color='CCCCCC'),
        bottom=Side(style='thin', color='CCCCCC'),
    )
    center_align = Alignment(horizontal='center', vertical='center')

    # 写表头
    for col_idx, col_name in enumerate(cols, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font_white
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    # 写数据行
    data_font = Font(name='微软雅黑', size=9)
    red_font = Font(name='微软雅黑', size=9, color='DC3545')
    green_font = Font(name='微软雅黑', size=9, color='28A745')

    trend_cols_set = {'15m趋势', '1H趋势', '日线趋势', '周线趋势'}
    profit_col = '策略盈亏(1H)'

    for row_idx, (_, row) in enumerate(df.iterrows(), 2):
        for col_idx, col_name in enumerate(cols, 1):
            val = row.get(col_name, '')
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.border = thin_border
            cell.alignment = center_align
            cell.font = data_font

            # 趋势列颜色
            if col_name in trend_cols_set:
                if val == '上涨':
                    cell.font = red_font
                elif val == '下跌':
                    cell.font = green_font
            # 盈亏列颜色
            elif col_name == profit_col:
                try:
                    pct = float(str(val).replace('%', '').replace('+', ''))
                    if pct > 0:
                        cell.font = red_font
                    elif pct < 0:
                        cell.font = green_font
                except (ValueError, TypeError):
                    pass

            cell.value = val

    # 自适应列宽
    for col_idx, col_name in enumerate(cols, 1):
        max_len = len(col_name) * 2  # 中文占两个字符宽度
        for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
            for cell in row:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)) * 1.5)
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max_len + 2, 30)

    # 冻结首行
    ws.freeze_panes = 'A2'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


# ====================================================================
#  命令行测试入口
# ====================================================================

def main():
    """命令行直接运行测试"""
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(message)s'
    )

    print("=" * 70)
    print("  星标品种行情模块 - 快速测试")
    print("=" * 70)

    # 1. 测试同步
    print("\n[1] 测试 sync_starred_to_csv()...")
    try:
        count = sync_starred_to_csv()
        print("  同步完成，CSV 行数: %d" % count)
    except Exception as e:
        print("  同步失败: %s" % e)
        traceback.print_exc()

    # 2. 测试读取
    print("\n[2] 测试 read_star_csv_for_display()...")
    try:
        records = read_star_csv_for_display()
        print("  读取 %d 条记录:" % len(records))
        for r in records:
            print("    %-6s %-8s 1H:%s 1D:%s" % (
                r['代码'], r['品种'],
                r.get('1H趋势', '--'),
                r.get('日线趋势', '--'),
            ))
    except Exception as e:
        print("  读取失败: %s" % e)


if __name__ == '__main__':
    main()
