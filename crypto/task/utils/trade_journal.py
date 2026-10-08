#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
结构化成交流水（trade journal）
================================

以 append-only 流水记录每一笔**实际成交**（限价成交 / 市价平仓），
供「实盘 vs 策略理论」对比引擎做机器可读的逐笔对齐。

与 trade_operations.log 文本日志的分工：
- 文本日志面向人工排查，格式自由；
- journal 面向程序消费，字段固定（迁移批次6 起存 MySQL trade_journal 表，
  原 logs/trade_journal.jsonl 仅作历史备份）。

写入点（三处）：
1. position_order_manager._reconcile_slot / _cancel_slot —— 限价单成交入账
2. trend_range_trader._close_bucket —— 市价平仓（止盈/止损/反转强平等）
3. trend_range_trader 全仓/逐仓强平（人工强平/反向风控，bucket='account'）

记录 schema：
{
  "ts": "2026-07-29 10:15:03",   # 成交确认时间（本地时钟）
  "run_id": "xxxx",              # 调度轮次ID（可为空）
  "inst_id": "NEAR-USDT-SWAP",
  "bucket": "trend",             # trend / range / account(跨仓位强平)
  "direction": "long",           # 持仓方向
  "action": "open",              # open / close
  "price": 3.4210,               # 成交价（市价单为下单时最新价，近似值）
  "amount": 0.2,                 # 成交张数
  "ord_id": "1234567890",        # 交易所订单ID（可为空）
  "reason": "signal"             # signal=信号挂单成交；其余为平仓原因原文
}
"""

import datetime
from typing import Dict, List

# 双模式导入：作为 crypto 包成员 / 以 task 目录为根的独立脚本
try:
    from crypto.database import session_scope
    from crypto import trade_journal_repo as journal_repo
except ImportError:
    session_scope = None
    journal_repo = None


def record_fill(inst_id: str, bucket: str, direction: str, action: str,
                price: float, amount: float, reason: str = 'signal',
                ord_id: str = None, run_id: str = '') -> None:
    """追加一条成交记录（任何异常都不能影响交易主流程，只静默吞掉）。"""
    try:
        rec = {
            'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'run_id': run_id or '',
            'inst_id': inst_id,
            'bucket': bucket,
            'direction': direction,
            'action': action,
            'price': round(float(price or 0), 8),
            'amount': round(float(amount or 0), 4),
            'ord_id': str(ord_id) if ord_id else '',
            'reason': reason or 'signal',
        }
        if session_scope is None:
            return  # DB 不可用时静默放弃（与 JSONL 版"绝不阻断交易"一致）
        with session_scope() as s:
            journal_repo.append_fill(s, rec)
    except Exception:
        pass  # journal 只做旁路记录，绝不阻断交易


def read_journal(inst_id: str = None, bucket: str = None,
                 start: str = None, end: str = None) -> List[Dict]:
    """按条件读取成交流水（时间为 'YYYY-MM-DD HH:MM:SS' 字符串，闭区间）。

    DB 读取失败时返回空列表，不抛异常（对比引擎按"无实盘记录"降级）。
    """
    if session_scope is None:
        return []
    try:
        with session_scope() as s:
            return journal_repo.query_fills(s, inst_id=inst_id, bucket=bucket,
                                            start=start, end=end)
    except Exception:
        return []


def classify_reason(reason: str) -> str:
    """自由文本平仓原因 → 归因分类码（对比引擎/前端展示用）。

    signal=信号交易 tp=止盈 sl=止损 reverse=长周期反转强平
    smart_reduce=智能减仓（全局强制：无论盈亏只减仓不平仓，保留残留，统计上不计入已了结交易）
    manual=人工强平 guard=反向持仓风控 other=其他
    """
    r = str(reason or '')
    if not r or r == 'signal':
        return 'signal'
    # 智能减仓优先于反转判定：其 reason 同时包含“反转”与“智能减仓”字样
    if '智能减仓' in r:
        return 'smart_reduce'
    if '止损' in r:
        return 'sl'
    if '止盈' in r or '分批' in r or '时间兜底' in r:
        return 'tp'
    if '反转' in r or '睡眠' in r:
        return 'reverse'
    if '人工' in r or '手动' in r:
        return 'manual'
    if '风控' in r or '反向持仓' in r:
        return 'guard'
    return 'other'
