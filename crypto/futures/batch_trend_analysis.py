#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 2 自动搬运（2026-10-08）。
# 原名: futures/batch_trend_analysis.py  →  现位置: crypto/futures/batch_trend_analysis.py
# 导入已改为 crypto.* 显式包路径，sys.path 拼接兜底已移除。
"""
期货批量多周期趋势分析脚本
==========================
遍历 future.csv 中的所有期货品种，对每个品种分别执行
1小时线和日线的 Pro3 策略计算，提取趋势方向、交易价格、
交易时间和当前盈亏百分比，结果批量写入 CSV 文件。

可独立运行，也可通过 Flask blueprint 作为后台任务调用。
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

# 确保可以从 futures 目录导入本地模块
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logger = logging.getLogger(__name__)

CSV_PATH = os.path.join(BASE_DIR, "future.csv")

# 策略参数（与监控台保持一致）
STRATEGY_PARAMS = {
    'adx_period': 9,
    'macd_fast': 12,
    'macd_slow': 26,
    'macd_signal': 9,
    'base_weight': 0.70,
    'trade_multiplier': 10.0,
}

# 请求间隔（秒），避免频率过高
REQUEST_DELAY = 1.5

# 冷却时间（CD）配置：防止 akshare 接口频率限制
# 每处理 COOLDOWN_BATCH_SIZE 个品种后，额外等待 COOLDOWN_SECONDS 秒
COOLDOWN_BATCH_SIZE = 8      # 每 N 个品种触发一次冷却
COOLDOWN_SECONDS = 4.0       # 冷却等待时间（秒）


# ====================================================================
#  进度状态管理（模块级，供 Flask 蓝图查询）
# ====================================================================

class _ProgressState:
    """线程安全的进度状态"""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.status = "idle"       # idle / running / completed / error
            self.total = 0
            self.current = 0
            self.success = 0
            self.error = 0
            self.current_symbol = ""
            self.message = ""
            self.start_time = None
            self.end_time = None

    def start(self, total: int):
        with self._lock:
            self.status = "running"
            self.total = total
            self.current = 0
            self.success = 0
            self.error = 0
            self.current_symbol = ""
            self.message = "开始批量分析..."
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
                elapsed = (self.end_time - self.start_time).total_seconds() if self.start_time else 0
                self.message = (
                    "批量分析完成！成功 %d/%d，失败 %d，耗时 %.1f 秒"
                    % (self.success, self.total, self.error, elapsed)
                )

    def set_error(self, message: str):
        with self._lock:
            self.status = "error"
            self.end_time = datetime.datetime.now()
            self.message = message

    def to_dict(self) -> Dict:
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
                "success": self.success,
                "error": self.error,
                "current_symbol": self.current_symbol,
                "message": self.message,
                "elapsed_seconds": round(elapsed, 1),
                "progress_pct": round(self.current / self.total * 100, 1)
                if self.total > 0 else 0,
            }


# 全局进度实例
_progress = _ProgressState()


def get_progress() -> Dict:
    """获取当前进度（供外部调用）"""
    return _progress.to_dict()


# ====================================================================
#  趋势信号提取
# ====================================================================

def _extract_trend_info(period_result: Dict) -> Dict:
    """从多周期结果中提取单个周期的趋势信息

    Parameters
    ----------
    period_result : dict
        calculate_futures_multi_period 返回的单个周期结果，
        如 result['1H'] 或 result['1D']。

    Returns
    -------
    dict
        {
            'trend': '上涨'/'下跌'/'观望'/'错误',
            'trend_flag': 'rise'/'fall'/'wait'/'error',
            'trade_price': float or '',
            'trade_time': str or '',
            'profit_pct': float or '',
            'macd_histogram': float or '',
            'macd_dif': float or '',
            'adx': float or '',
            'atr': float or '',
            'sar': float or '',
            'price': float or '',
        }
    """
    if 'error' in period_result:
        return {
            'trend': '错误',
            'trend_flag': 'error',
            'trade_price': '',
            'trade_time': '',
            'profit_pct': '',
            'macd_histogram': '',
            'macd_dif': '',
            'adx': '',
            'atr': '',
            'sar': '',
            'price': '',
        }

    modify_flag = period_result.get('modify_flag', 'wait')
    if modify_flag == 'rise':
        trend = '上涨'
    elif modify_flag == 'fall':
        trend = '下跌'
    else:
        trend = '观望'
        modify_flag = 'wait'

    trade_price = period_result.get('trade_entry_price')
    trade_time = period_result.get('trade_entry_time')
    profit_pct = period_result.get('trade_profit_pct')

    return {
        'trend': trend,
        'trend_flag': modify_flag,
        'trade_price': round(float(trade_price), 4) if trade_price is not None and trade_price != '' else '',
        'trade_time': str(trade_time) if trade_time else '',
        'profit_pct': round(float(profit_pct), 4) if profit_pct is not None and profit_pct != '' else '',
        'macd_histogram': round(float(period_result.get('macd_histogram', 0)), 4) if period_result.get('macd_histogram', '') != '' else '',
        'macd_dif': round(float(period_result.get('macd_dif', 0)), 4) if period_result.get('macd_dif', '') != '' else '',
        'adx': round(float(period_result.get('adx', 0)), 2) if period_result.get('adx', '') != '' else '',
        'atr': round(float(period_result.get('atr', 0)), 4) if period_result.get('atr', '') != '' else '',
        'sar': round(float(period_result.get('sar', 0)), 4) if period_result.get('sar', '') != '' else '',
        'price': round(float(period_result.get('price', 0)), 4) if period_result.get('price', '') != '' else '',
    }


# ====================================================================
#  CSV 读取与写入
# ====================================================================

def _read_csv() -> pd.DataFrame:
    """读取 future.csv 并标准化"""
    df = pd.read_csv(CSV_PATH, encoding='utf-8-sig')
    df = df.dropna(subset=['代码']).reset_index(drop=True)
    df['代码'] = df['代码'].astype(str).str.strip()
    df['种类'] = df['种类'].astype(str).str.strip()
    df['品种'] = df['品种'].astype(str).str.strip()

    # 确保趋势列和指标列存在
    new_cols = ['1H_趋势', '1H_交易价格', '1H_交易时间', '1H_盈亏%',
                '1H_MACD', '1H_DIF', '1H_ADX', '1H_ATR', '1H_SAR', '1H_价格',
                '1D_趋势', '1D_交易价格', '1D_交易时间', '1D_盈亏%',
                '1D_MACD', '1D_DIF', '1D_ADX', '1D_ATR', '1D_SAR', '1D_价格',
                '1W_趋势', '1W_交易价格', '1W_交易时间', '1W_盈亏%',
                '1W_MACD', '1W_DIF', '1W_ADX', '1W_ATR', '1W_SAR', '1W_价格']
    for col in new_cols:
        if col not in df.columns:
            df[col] = ''

    return df


def _write_csv(df: pd.DataFrame):
    """写入 future.csv"""
    df.to_csv(CSV_PATH, index=False, encoding='utf-8-sig')
    logger.info("CSV 已保存: %s (%d 行)", CSV_PATH, len(df))


def read_csv_for_display() -> List[Dict]:
    """读取 CSV 数据供前端表格展示"""
    df = _read_csv()
    records = []
    for _, row in df.iterrows():
        records.append({
            '种类': str(row.get('种类', '')),
            '品种': str(row.get('品种', '')),
            '代码': str(row.get('代码', '')),
            '1H_趋势': str(row.get('1H_趋势', '')),
            '1H_MACD': str(row.get('1H_MACD', '')),
            '1H_DIF': str(row.get('1H_DIF', '')),
            '1H_ADX': str(row.get('1H_ADX', '')),
            '1H_ATR': str(row.get('1H_ATR', '')),
            '1H_SAR': str(row.get('1H_SAR', '')),
            '1H_价格': str(row.get('1H_价格', '')),
            '1H_交易价格': str(row.get('1H_交易价格', '')),
            '1H_交易时间': str(row.get('1H_交易时间', '')),
            '1H_盈亏%': str(row.get('1H_盈亏%', '')),
            '1D_趋势': str(row.get('1D_趋势', '')),
            '1D_MACD': str(row.get('1D_MACD', '')),
            '1D_DIF': str(row.get('1D_DIF', '')),
            '1D_ADX': str(row.get('1D_ADX', '')),
            '1D_ATR': str(row.get('1D_ATR', '')),
            '1D_SAR': str(row.get('1D_SAR', '')),
            '1D_价格': str(row.get('1D_价格', '')),
            '1D_交易价格': str(row.get('1D_交易价格', '')),
            '1D_交易时间': str(row.get('1D_交易时间', '')),
            '1D_盈亏%': str(row.get('1D_盈亏%', '')),
            '1W_趋势': str(row.get('1W_趋势', '')),
            '1W_MACD': str(row.get('1W_MACD', '')),
            '1W_DIF': str(row.get('1W_DIF', '')),
            '1W_ADX': str(row.get('1W_ADX', '')),
            '1W_ATR': str(row.get('1W_ATR', '')),
            '1W_SAR': str(row.get('1W_SAR', '')),
            '1W_价格': str(row.get('1W_价格', '')),
            '1W_交易价格': str(row.get('1W_交易价格', '')),
            '1W_交易时间': str(row.get('1W_交易时间', '')),
            '1W_盈亏%': str(row.get('1W_盈亏%', '')),
        })
    return records


def get_filtered_records() -> List[Dict]:
    """获取筛选后的一致性品种记录（1H 和 1D 趋势相同）"""
    all_records = read_csv_for_display()
    filtered = []
    for r in all_records:
        trend_1h = r.get('1H_趋势', '')
        trend_1d = r.get('1D_趋势', '')
        # 两个周期都为"上涨"或都为"下跌"
        if trend_1h in ('上涨', '下跌') and trend_1h == trend_1d:
            filtered.append(r)
    return filtered


# ====================================================================
#  批量趋势分析器
# ====================================================================

class BatchTrendAnalyzer:
    """批量多周期趋势分析器

    使用示例::

        analyzer = BatchTrendAnalyzer()
        analyzer.run()              # 同步运行
        # 或
        analyzer.run_in_background()  # 后台线程运行
        progress = get_progress()     # 查询进度
    """

    def __init__(self, csv_path: str = None):
        self.csv_path = csv_path or CSV_PATH
        global _progress
        self.progress = _progress

    def _process_single_symbol(self, code: str) -> Dict:
        """处理单个品种，返回 1H、1D 和 1W 的趋势信息

        Parameters
        ----------
        code : str
            品种代码，如 'RB'

        Returns
        -------
        dict
            {'1H': {...}, '1D': {...}, '1W': {...}} 或 None（处理失败）
        """
        from crypto.futures.futures_adapter import calculate_futures_multi_period

        try:
            result = calculate_futures_multi_period(code, ['1H', '1D', '1W'])

            info_1h = _extract_trend_info(result.get('1H', {}))
            info_1d = _extract_trend_info(result.get('1D', {}))
            info_1w = _extract_trend_info(result.get('1W', {}))

            return {'1H': info_1h, '1D': info_1d, '1W': info_1w}
        except Exception as e:
            logger.warning("[%s] 批量分析失败: %s", code, str(e))
            return {
                '1H': {'trend': '错误', 'trend_flag': 'error',
                       'trade_price': '', 'trade_time': '', 'profit_pct': '',
                       'macd_histogram': '', 'macd_dif': '', 'adx': '', 'atr': '', 'sar': '', 'price': ''},
                '1D': {'trend': '错误', 'trend_flag': 'error',
                       'trade_price': '', 'trade_time': '', 'profit_pct': '',
                       'macd_histogram': '', 'macd_dif': '', 'adx': '', 'atr': '', 'sar': '', 'price': ''},
                '1W': {'trend': '错误', 'trend_flag': 'error',
                       'trade_price': '', 'trade_time': '', 'profit_pct': '',
                       'macd_histogram': '', 'macd_dif': '', 'adx': '', 'atr': '', 'sar': '', 'price': ''},
                'error': str(e),
            }

    def run(self, callback=None) -> Dict:
        """同步运行批量分析

        Parameters
        ----------
        callback : callable, optional
            每处理完一个品种后的回调函数，签名为 callback(progress_dict)。

        Returns
        -------
        dict
            {'total': int, 'success': int, 'error': int, 'results': list}
        """
        df = _read_csv()
        symbols = df['代码'].tolist()
        total = len(symbols)

        self.progress.start(total)
        success_count = 0
        error_count = 0

        for idx, code in enumerate(symbols):
            self.progress.update(
                current=idx + 1,
                symbol=code,
                message="处理中: %d/%d — %s" % (idx + 1, total, code),
            )

            info = self._process_single_symbol(code)

            if info and 'error' not in info:
                # 更新 DataFrame 中对应行
                mask = df['代码'] == code
                info_1h = info['1H']
                info_1d = info['1D']
                info_1w = info['1W']
                df.loc[mask, '1H_趋势'] = info_1h['trend']
                df.loc[mask, '1H_交易价格'] = str(info_1h['trade_price']) if info_1h['trade_price'] != '' else ''
                df.loc[mask, '1H_交易时间'] = info_1h['trade_time']
                df.loc[mask, '1H_盈亏%'] = str(info_1h['profit_pct']) if info_1h['profit_pct'] != '' else ''
                df.loc[mask, '1H_MACD'] = str(info_1h['macd_histogram']) if info_1h['macd_histogram'] != '' else ''
                df.loc[mask, '1H_DIF'] = str(info_1h['macd_dif']) if info_1h['macd_dif'] != '' else ''
                df.loc[mask, '1H_ADX'] = str(info_1h['adx']) if info_1h['adx'] != '' else ''
                df.loc[mask, '1H_ATR'] = str(info_1h['atr']) if info_1h['atr'] != '' else ''
                df.loc[mask, '1H_SAR'] = str(info_1h['sar']) if info_1h['sar'] != '' else ''
                df.loc[mask, '1H_价格'] = str(info_1h['price']) if info_1h['price'] != '' else ''
                df.loc[mask, '1D_趋势'] = info_1d['trend']
                df.loc[mask, '1D_交易价格'] = str(info_1d['trade_price']) if info_1d['trade_price'] != '' else ''
                df.loc[mask, '1D_交易时间'] = info_1d['trade_time']
                df.loc[mask, '1D_盈亏%'] = str(info_1d['profit_pct']) if info_1d['profit_pct'] != '' else ''
                df.loc[mask, '1D_MACD'] = str(info_1d['macd_histogram']) if info_1d['macd_histogram'] != '' else ''
                df.loc[mask, '1D_DIF'] = str(info_1d['macd_dif']) if info_1d['macd_dif'] != '' else ''
                df.loc[mask, '1D_ADX'] = str(info_1d['adx']) if info_1d['adx'] != '' else ''
                df.loc[mask, '1D_ATR'] = str(info_1d['atr']) if info_1d['atr'] != '' else ''
                df.loc[mask, '1D_SAR'] = str(info_1d['sar']) if info_1d['sar'] != '' else ''
                df.loc[mask, '1D_价格'] = str(info_1d['price']) if info_1d['price'] != '' else ''
                df.loc[mask, '1W_趋势'] = info_1w['trend']
                df.loc[mask, '1W_交易价格'] = str(info_1w['trade_price']) if info_1w['trade_price'] != '' else ''
                df.loc[mask, '1W_交易时间'] = info_1w['trade_time']
                df.loc[mask, '1W_盈亏%'] = str(info_1w['profit_pct']) if info_1w['profit_pct'] != '' else ''
                df.loc[mask, '1W_MACD'] = str(info_1w['macd_histogram']) if info_1w['macd_histogram'] != '' else ''
                df.loc[mask, '1W_DIF'] = str(info_1w['macd_dif']) if info_1w['macd_dif'] != '' else ''
                df.loc[mask, '1W_ADX'] = str(info_1w['adx']) if info_1w['adx'] != '' else ''
                df.loc[mask, '1W_ATR'] = str(info_1w['atr']) if info_1w['atr'] != '' else ''
                df.loc[mask, '1W_SAR'] = str(info_1w['sar']) if info_1w['sar'] != '' else ''
                df.loc[mask, '1W_价格'] = str(info_1w['price']) if info_1w['price'] != '' else ''
                success_count += 1
            else:
                error_count += 1
                logger.error("[%s] 处理失败", code)

            self.progress.update(
                current=idx + 1,
                success=success_count,
                error=error_count,
            )

            # 每处理 5 个或最后一个时保存 CSV
            if (idx + 1) % 5 == 0 or (idx + 1) == total:
                _write_csv(df)

            if callback:
                callback(self.progress.to_dict())

            # 请求间延迟
            time.sleep(REQUEST_DELAY)

            # 冷却时间（CD）：每处理 COOLDOWN_BATCH_SIZE 个品种后额外等待
            if (idx + 1) % COOLDOWN_BATCH_SIZE == 0 and (idx + 1) < total:
                logger.info("冷却等待 %.1f 秒 (已处理 %d/%d)...",
                            COOLDOWN_SECONDS, idx + 1, total)
                time.sleep(COOLDOWN_SECONDS)

        self.progress.finish()

        return {
            'total': total,
            'success': success_count,
            'error': error_count,
        }

    def run_in_background(self) -> threading.Thread:
        """后台线程运行批量分析

        Returns
        -------
        threading.Thread
            已启动的后台线程
        """
        thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="batch-trend-analyzer",
        )
        thread.start()
        return thread


# ====================================================================
#  命令行入口
# ====================================================================

def main():
    """命令行直接运行"""
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(message)s'
    )

    print("=" * 70)
    print("  期货批量多周期趋势分析")
    print("=" * 70)

    analyzer = BatchTrendAnalyzer()
    result = analyzer.run(callback=lambda p: print(
        "\r  进度: %s/%s (%.1f%%) — %s | 成功:%s 失败:%s   " % (
            p['current'], p['total'], p['progress_pct'],
            p['current_symbol'], p['success'], p['error'],
        ), end='', flush=True
    ))

    print("\n")
    print("  总计: %d  成功: %d  失败: %d" % (
        result['total'], result['success'], result['error']))

    # 打印前 10 条结果
    records = read_csv_for_display()
    filtered = [r for r in records
                if r['1H_趋势'] in ('上涨', '下跌')
                and r['1H_趋势'] == r['1D_趋势']]
    print("\n  双周期一致性品种 (%d 个):" % len(filtered))
    for r in filtered:
        print("    %-4s %-8s  1H: %s(%s%%)  1D: %s(%s%%)  1W: %s(%s%%)" % (
            r['代码'], r['品种'],
            r['1H_趋势'], r['1H_盈亏%'] or '--',
            r['1D_趋势'], r['1D_盈亏%'] or '--',
            r.get('1W_趋势', '--'), r.get('1W_盈亏%') or '--',
        ))


if __name__ == '__main__':
    main()
