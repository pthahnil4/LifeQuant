#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实盘分析记录 - 数据访问层（迁移批次10）
==========================================
task_analysis_records 表的增删改查 + 事后复盘价格惰性回填：

    add_record(session, rec)               新增一条分析记录（可选结构价目标/失效），返回自增 id
    query_records(session, ...)            按币种/时间段/判断筛选（ts 降序）
    query_slot_records(session, hour_slot) 按小时槽取记录（分析纪律闸门口径）
    query_by_ids(session, ids)             按 id 批量取记录（打卡反查分析依据）
    update_user_fields(session, rec_id, ..) 修改个人判断/分析原因/结构价计划（终局后锁定）
    delete_record(session, rec_id)         删除记录
    backfill_due_reviews(session, limit)   惰性回填：端点价 + 先触碰判定 + 结构价计划结算
    compute_stats(records)                 先触碰口径的命中率/混淆矩阵/计划兑现统计

批次16评价口径：
- 先触碰判定（计分主口径）：窗口内短周期 bar 逐根扫描，high/low 谁先触 P0±θ 判谁；
  同根双触=conflict；全程未触=none(真横盘)；none/conflict 对方向判断计「未兑现」不进命中率分母
- 结构价计划（可选）：无固定截止窗、结构自证——先触目标=win(记录R)、先触失效=loss(-1R)、
  同根双触保守记loss；均未触=open 持续观察；记录时刻K线已超出可拉深度=终局 gap(无法观测)
- 端点价 price_1h/price_4h 保留为参考列（instinct 影子预测仍同源复用
  review_windows_for/_review_price_at/classify_move，该口径不变）

复盘回填语义：
- 复盘窗口按每条记录自身短周期动态计算：近=REVIEW_NEAR_MULT×短周期，远=REVIEW_FAR_MULT×短周期
- 端点取价：以短周期 K 线，ts+窗口时刻所在 bar 的 close（bar 必须已收线确认）
- 触碰判定可在窗口内提前终局（首触已发生）；'none' 需窗口末 bar 收线确认后才定格
- K线拉取失败（断网等）静默跳过该记录，下次查询时重试，不阻塞列表返回
"""

import os
import re
import sys
import math
import logging
import datetime
from typing import Dict, List, Optional

from sqlalchemy import select, or_, and_, delete as sa_delete

from .models import TaskAnalysisRecord

logger = logging.getLogger(__name__)

_TS_FMT = '%Y-%m-%d %H:%M:%S'

# 复盘窗口倍数：以每条记录自身的短周期为基准动态计算
#   近窗口 = REVIEW_NEAR_MULT × 短周期，远窗口 = REVIEW_FAR_MULT × 短周期
#   例：短周期 15m → 近 1H(4×)、远 2H(8×)；5m → 20m/40m；30m → 2H/4H
#   想调窗口只改这两个倍数即可，前端标签会自动同步（stats 里回传倍数）
REVIEW_NEAR_MULT = 4
REVIEW_FAR_MULT = 8

# 命中率中性带（横盘死区）：实际涨跌幅 |chg%| <= θ 判为「横盘」，否则按方向判「涨/跌」
#   θ = max(HIT_FLOOR_PCT, HIT_ATR_K × atr_pct × 窗口缩放)
#   - ATR 自适应：波动大的币自动放宽死区、横盘币自动收紧（atr_pct 每条记录已存库）
#   - 近/远窗口按时间开方缩放（随机游走尺度 ~ √时长）：远窗口 θ = 近窗口 θ × √(far/near)
#   - HIT_FLOOR_PCT 地板：atr_pct 缺失/极小时仍保留最小死区，避免退化成 0 阈值
#   实测近/远窗口净波动中位≈1×ATR，取 0.3×ATR 约卡住最低 15% 噪声归为横盘
HIT_ATR_K = 0.3
HIT_FLOOR_PCT = 0.1
HIT_FAR_SCALE = math.sqrt(REVIEW_FAR_MULT / REVIEW_NEAR_MULT)   # ≈1.414（远窗口时长是近窗口 2 倍）

_UNIT_MIN = {'M': 1, 'H': 60, 'W': 10080, 'D': 1440}


def period_to_minutes(p) -> int:
    """把 '5m'/'15m'/'1H'/'4H'/'1D' 等周期串解析为分钟数，无法解析返回 0"""
    m = re.match(r'^\s*(\d+)\s*([mMhHwWdD])\s*$', str(p or ''))
    if not m:
        return 0
    return int(m.group(1)) * _UNIT_MIN.get(m.group(2).upper(), 0)


def review_windows_for(short_period: str):
    """返回该记录的两个复盘窗口: ((price_col, ts_col, kline_bar, offset_minutes), ...)

    以短周期 K 线为取价粒度（最贴合用户实际操作周期）；短周期无法解析时
    回退到旧的固定 1H/4H，保证兼容。
    """
    sm = period_to_minutes(short_period)
    if sm <= 0:
        return (('price_1h', 'ts_1h', '1H', 60),
                ('price_4h', 'ts_4h', '4H', 240))
    bar = str(short_period).strip()
    return (('price_1h', 'ts_1h', bar, sm * REVIEW_NEAR_MULT),
            ('price_4h', 'ts_4h', bar, sm * REVIEW_FAR_MULT))


def add_record(session, rec: dict) -> int:
    """新增分析记录，返回自增 id（rec 为路由层校验后的字段 dict）

    分析纪律（批次11）：hour_slot / source 由服务端自动推导，不信任前端传入——
      hour_slot  取 ts 前 13 位 'YYYY-MM-DD HH'，闸门/巡检按此聚合
      source     |now - ts| <= 宽限期 → live，否则 backfill（事后补记）
    前端无法把补记伪造成"当时就分析了"，看板则把补记率作为诚实指标呈现。
    """
    ts = str(rec.get('ts') or '')
    grace = int(rec.get('_grace_minutes') or 15)
    source = str(rec.get('source') or '') or _classify_source(ts, grace)
    row = TaskAnalysisRecord(
        ts=ts,
        inst_id=str(rec.get('inst_id') or ''),
        price=float(rec.get('price') or 0),
        short_period=str(rec.get('short_period') or ''),
        long_period=str(rec.get('long_period') or ''),
        short_dir=str(rec.get('short_dir') or ''),
        long_dir=str(rec.get('long_dir') or ''),
        long_dir_prev=rec.get('long_dir_prev') or None,
        atr_pct=float(rec.get('atr_pct') or 0),
        user_judgment=str(rec.get('user_judgment') or ''),
        user_reason=str(rec.get('user_reason') or ''),
        hour_slot=ts[:13] if len(ts) >= 13 else '',
        source=source,
        # 结构价计划（批次16-B）：未填时为 None，前端不挂计划
        target_price=rec.get('target_price'),
        invalid_price=rec.get('invalid_price'),
    )
    session.add(row)
    session.flush()
    return row.id


def _classify_source(ts: str, grace_minutes: int) -> str:
    """按 |now - ts| 判定记录来源；ts 非法时按 live（此刻正在写入）"""
    try:
        rec_ts = datetime.datetime.strptime(str(ts or '').strip()[:19], _TS_FMT)
    except ValueError:
        return 'live'
    delta = abs((datetime.datetime.now() - rec_ts).total_seconds())
    return 'live' if delta <= max(0, int(grace_minutes)) * 60 else 'backfill'


def query_slot_records(session, hour_slot: str) -> List[dict]:
    """按小时槽取分析记录（走 idx_tar_slot，ts 升序）"""
    if not hour_slot:
        return []
    stmt = (select(TaskAnalysisRecord)
            .where(TaskAnalysisRecord.hour_slot == hour_slot)
            .order_by(TaskAnalysisRecord.ts.asc(), TaskAnalysisRecord.id.asc()))
    return [r.to_dict() for r in session.execute(stmt).scalars().all()]


def query_by_ids(session, ids: List[int]) -> List[dict]:
    """按 id 批量取记录（打卡详情反查“这次打卡依据的是哪几条分析”）"""
    ids = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
    if not ids:
        return []
    stmt = (select(TaskAnalysisRecord)
            .where(TaskAnalysisRecord.id.in_(ids))
            .order_by(TaskAnalysisRecord.ts.asc()))
    return [r.to_dict() for r in session.execute(stmt).scalars().all()]


def query_records(session, inst_id: str = None, start: str = None,
                  end: str = None, judgment: str = None) -> List[dict]:
    """按条件查询分析记录，返回 dict 列表（ts 降序，同 ts 按 id 降序）。

    start/end 为 'YYYY-MM-DD HH:MM:SS' 字符串闭区间（与项目其他模块一致）。
    """
    stmt = select(TaskAnalysisRecord)
    if inst_id:
        stmt = stmt.where(TaskAnalysisRecord.inst_id == inst_id)
    if judgment:
        stmt = stmt.where(TaskAnalysisRecord.user_judgment == judgment)
    if start:
        stmt = stmt.where(TaskAnalysisRecord.ts >= start)
    if end:
        stmt = stmt.where(TaskAnalysisRecord.ts <= end)
    stmt = stmt.order_by(TaskAnalysisRecord.ts.desc(), TaskAnalysisRecord.id.desc())
    rows = session.execute(stmt).scalars().all()
    return [r.to_dict() for r in rows]


_UNSET = object()


def update_user_fields(session, rec_id: int, judgment: str, reason: str,
                       target_price=_UNSET, invalid_price=_UNSET) -> bool:
    """修改个人判断/分析原因，及可选的结构价计划；记录不存在返回 False。

    结构价语义：传 _UNSET 表示不变，传 None 表示清空计划，传数值表示设置（成对）。
    结构价变动时重置未终局的 plan 判定（win/loss/gap 终局锁定，由路由层拦截，
    这里再保险一道：终局记录无视结构价修改请求）。方向切换（rise↔fall）不影响
    已挂计划：目标/失效价本身与方向无关，先触哪边哪边说了算。
    """
    row = session.get(TaskAnalysisRecord, rec_id)
    if row is None:
        return False
    row.user_judgment = judgment
    row.user_reason = reason or ''
    if row.plan_result not in ('win', 'loss', 'gap'):
        tp_changed = target_price is not _UNSET and float(target_price or 0) != (row.target_price or 0.0)
        ip_changed = invalid_price is not _UNSET and float(invalid_price or 0) != (row.invalid_price or 0.0)
        if target_price is not _UNSET:
            row.target_price = None if target_price is None else float(target_price)
        if invalid_price is not _UNSET:
            row.invalid_price = None if invalid_price is None else float(invalid_price)
        if (tp_changed or ip_changed
                or (row.target_price is not None) != (row.invalid_price is not None)):
            # 计划改动→重新观察：清空旧终局状态，由下次回填重扫
            row.plan_result, row.plan_r, row.plan_touch_ts = None, None, None
    return True


def delete_record(session, rec_id: int) -> bool:
    """删除记录；记录不存在返回 False"""
    result = session.execute(
        sa_delete(TaskAnalysisRecord).where(TaskAnalysisRecord.id == rec_id))
    return (result.rowcount or 0) > 0


def delete_records(session, rec_ids: List[int]) -> int:
    """批量删除记录，返回实际删除的条数（不存在的 id 自动忽略）。"""
    ids = [int(i) for i in (rec_ids or []) if str(i).strip() != '']
    if not ids:
        return 0
    result = session.execute(
        sa_delete(TaskAnalysisRecord).where(TaskAnalysisRecord.id.in_(ids)))
    return result.rowcount or 0


# =============================================================================
# 复盘价格惰性回填
# =============================================================================

def _fetch_kline_module():
    """惰性导入策略模块的K线工具（路径处理与 strategy_adapter 同款）"""
    strategy_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'strategy')
    crypto_dir = os.path.dirname(os.path.abspath(__file__))
    for d in (strategy_dir, crypto_dir):
        if d not in sys.path:
            sys.path.insert(0, d)
    import pro3_singletimeframe as _single
    return _single


def _review_price_at(single_mod, inst_id: str, bar: str, target: datetime.datetime):
    """取 target 时刻所在K线 bar 的 close 价。

    返回 (price, bar_ts_str)；bar 未收线/数据不足/异常时返回 (None, None)，
    由调用方静默跳过下次重试。
    """
    df = _get_kline_df(single_mod, inst_id, bar, {})
    if df is None or len(df) == 0:
        return None, None
    # 所在 bar = 起始时间 <= target 的最后一根（K线时间戳为 bar 起点，东八区）
    past = df[df.index <= target]
    if len(past) == 0:
        return None, None
    row = past.iloc[-1]
    # 未收线的 bar（OKX confirm=0）close 仍在变动，等待下次查询再回填
    confirm = float(row.get('confirm', 1) or 0)
    if confirm < 1:
        return None, None
    price = float(row.get('close', 0) or 0)
    if price <= 0:
        return None, None
    return price, past.index[-1].strftime(_TS_FMT)


def _get_kline_df(single_mod, inst_id: str, bar: str, cache: dict):
    """拉短周期 K 线 DataFrame（同 (inst,bar) 在一次回填内只拉一次；模块内另有 TTL 缓存）。

    拉取失败返回 None（静默，下次查询重试）。
    """
    key = (inst_id, bar)
    if key in cache:
        return cache[key]
    try:
        df = single_mod._fetch_kline_data(inst_id, bar)
    except Exception as e:
        logger.warning(f'[AnalysisRecord] {inst_id} {bar} K线拉取失败，跳过回填: {e}')
        df = None
    cache[key] = df
    return df


def _window_scan(df, base_ts: datetime.datetime, end_ts: datetime.datetime):
    """扫描窗口 (base_ts, end_ts] 内的已收线 bar。

    返回 (bars, endpoint_price, endpoint_ts, complete)：
      bars       [{'ts','high','low'}...] 升序；快照所在 bar 的起点 <= base_ts，其
                 high/low 含记录时刻之前的走势，故从下一根开始扫
      endpoint   含 end_ts 且已收线 bar 的 close（即旧端点取价口径）
      complete   含 end_ts 的 bar 存在且已收线 —— '未触带' 结论（none）需此标志才能定格
    """
    if df is None or len(df) == 0:
        return [], None, None, False
    sub = df[df.index <= end_ts]
    price = price_ts = None
    complete = False
    if len(sub):
        last = sub.iloc[-1]
        if float(last.get('confirm', 1) or 0) >= 1:
            p = float(last.get('close', 0) or 0)
            if p > 0:
                price = p
                price_ts = sub.index[-1].strftime(_TS_FMT)
                complete = True
    fut = sub[sub.index > base_ts]
    bars = []
    for idx, r in fut.iterrows():
        if float(r.get('confirm', 1) or 0) < 1:
            continue
        bars.append({'ts': idx.strftime(_TS_FMT),
                     'high': float(r.get('high', 0) or 0),
                     'low': float(r.get('low', 0) or 0)})
    return bars, price, price_ts, complete


def backfill_due_reviews(session, limit: int = 500) -> int:
    """惰性回填：端点价 + 先触碰判定 + 结构价计划结算，返回新写入字段数。

    - 近/远窗口（REVIEW_NEAR/FAR_MULT×短周期）：到窗口终点且端点或触碰列仍空 →
      扫 (ts, ts+窗口] bar 序列：端点 close 照旧回填（instinct 同源 + 参考列）；
      classify_touch 按 θ 判 up/down/conflict 立即定格，'none' 需窗口末 bar 收线
    - 结构价计划：填了目标+失效价且未终局（win/loss/gap）的记录，按近窗口粒度
      扫记录时刻后全部已收线 bar（无固定截止窗，结构自证），未终局结果每次刷新
    - 复盘窗口按每条记录自身短周期动态计算；K线拉取失败静默跳过下次重试
    """
    now = datetime.datetime.now()
    plan_pending = and_(
        TaskAnalysisRecord.target_price.isnot(None),
        TaskAnalysisRecord.invalid_price.isnot(None),
        or_(TaskAnalysisRecord.plan_result.is_(None),
            TaskAnalysisRecord.plan_result == 'open'))
    stmt = (select(TaskAnalysisRecord)
            .where(or_(TaskAnalysisRecord.price_1h.is_(None),
                       TaskAnalysisRecord.price_4h.is_(None),
                       TaskAnalysisRecord.touch_near.is_(None),
                       TaskAnalysisRecord.touch_far.is_(None),
                       plan_pending))
            .order_by(TaskAnalysisRecord.ts.asc())
            .limit(limit))
    rows = session.execute(stmt).scalars().all()
    if not rows:
        return 0

    single_mod = _fetch_kline_module()
    df_cache: Dict[tuple, object] = {}
    filled = 0
    for row in rows:
        try:
            base_ts = datetime.datetime.strptime(row.ts, _TS_FMT)
        except (TypeError, ValueError):
            continue
        windows = review_windows_for(row.short_period)
        for (price_col, ts_col, bar, offset_min), touch_col, tts_col, is_far in (
                (windows[0], 'touch_near', 'touch_near_ts', False),
                (windows[1], 'touch_far', 'touch_far_ts', True)):
            need_price = getattr(row, price_col) is None
            need_touch = getattr(row, touch_col) is None
            if not (need_price or need_touch):
                continue
            end_ts = base_ts + datetime.timedelta(minutes=offset_min)
            if now < end_ts:
                continue
            df = _get_kline_df(single_mod, row.inst_id, bar, df_cache)
            bars, price, price_ts, complete = _window_scan(df, base_ts, end_ts)
            if need_price and price is not None:
                setattr(row, price_col, price)
                setattr(row, ts_col, price_ts)
                filled += 1
            if need_touch and (bars or complete):
                res, res_ts = classify_touch(
                    bars, row.price, neutral_theta(row.atr_pct, is_far))
                if res in ('up', 'down', 'conflict') or (res == 'none' and complete):
                    setattr(row, touch_col, res)
                    setattr(row, tts_col, res_ts)
                    filled += 1
        # 结构价计划：无固定截止窗，持续重扫直到终局（win/loss/gap）
        if (row.target_price and row.invalid_price
                and row.plan_result in (None, 'open')):
            bar = windows[0][2]
            df = _get_kline_df(single_mod, row.inst_id, bar, df_cache)
            if df is None or len(df) == 0:
                continue
            fut = df[df.index > base_ts]
            bars = [{'ts': idx.strftime(_TS_FMT),
                     'high': float(r.get('high', 0) or 0),
                     'low': float(r.get('low', 0) or 0)}
                    for idx, r in fut.iterrows()
                    if float(r.get('confirm', 1) or 0) >= 1]
            # head 断档：记录时刻所在 bar 已被挤出可拉深度，早期走势无法观测
            head_ok = len(df[df.index <= base_ts]) > 0
            state = resolve_plan(bars, row.price, row.target_price,
                                 row.invalid_price, head_ok)
            if state and (row.plan_result, row.plan_r, row.plan_touch_ts) != (
                    state['result'], state['r'], state['touch_ts']):
                row.plan_result = state['result']
                row.plan_r = state['r']
                row.plan_touch_ts = state['touch_ts']
                filled += 1
    if filled:
        session.flush()
        logger.info(f'[AnalysisRecord] 复盘回填完成：{len(rows)} 条记录新写 {filled} 个字段')
    return filled


def historical_prices(inst_id: str, targets: List[datetime.datetime],
                      bar: str = '1H') -> Dict[str, Optional[float]]:
    """批量取历史时点价格：一个币种只拉一次 K 线，再从同一 DataFrame 里查全部时点。

    供「回溯分析」录入使用：为过去的小时槽补记分析记录时，快照价必须是
    当时的真实价格而不是现价，否则复盘口径彻底失真。

    取价口径：target 所在 bar 的【上一根已收线 bar】的 close——bar 时间戳为
    起点，上一根的 close 恰好就是 target 时刻的价格，不引入未来数据。
    K 线拉不到/超出历史深度时该时点返回 None，由调用方如实报错。

    返回 {target.strftime('%Y-%m-%d %H:%M:%S'): price or None}
    """
    keys = [t.strftime(_TS_FMT) for t in targets]
    result = {k: None for k in keys}
    if not targets:
        return result
    try:
        single_mod = _fetch_kline_module()
        df = single_mod._fetch_kline_data(inst_id, bar)
    except Exception as e:
        logger.warning(f'[AnalysisRecord] {inst_id} {bar} 历史K线拉取失败: {e}')
        return result
    if df is None or len(df) == 0:
        return result

    for t in targets:
        key = t.strftime(_TS_FMT)
        # 严格小于 target 的最后一根 = 已在 target 时刻收线的 bar
        past = df[df.index < t]
        if len(past) == 0:
            continue
        row = past.iloc[-1]
        if float(row.get('confirm', 1) or 0) < 1:
            continue
        price = float(row.get('close', 0) or 0)
        if price > 0:
            result[key] = price
    return result


# =============================================================================
# 命中率统计（路由层聚合口径）
# =============================================================================

def _empty_bucket():
    """先触碰口径计分桶：scored=命中+判错（命中率分母），open=未兑现(none/conflict)，
    pending=窗口未回填完；avg_pct 按端点参考价均涨跌幅（chg_n 为累加样本数）"""
    return {'scored': 0, 'hit': 0, 'miss': 0, 'open': 0, 'pending': 0,
            'rate': 0.0, 'avg_pct': 0.0, 'chg_n': 0}


def _empty_confusion():
    """个人判断混淆矩阵骨架：行=预测(rise/watch/fall)，列=路径实际(up/down/none/conflict)"""
    return {j: {'up': 0, 'down': 0, 'none': 0, 'conflict': 0} for j in ('rise', 'watch', 'fall')}


# 判断/方向 → 期望的实际类别；命中 = 期望类别与实际一致
# （端点口径旧映射，仅供 instinct 冒烟等外部引用保留；本模块计分已改用 *_TOUCH_*）
_JUDGMENT_EXPECT = {'rise': 'up', 'fall': 'down', 'watch': 'flat'}
_DIR_EXPECT = {'long': 'up', 'short': 'down'}
# 先触碰口径：观望的合格答案是 none（窗口内确实没行情）；up/down 即错失
_TOUCH_EXPECT = {'rise': 'up', 'fall': 'down', 'watch': 'none'}
_DIR_TOUCH_EXPECT = {'long': 'up', 'short': 'down'}


def neutral_theta(atr_pct, is_far: bool) -> float:
    """中性带阈值 θ(%)：θ = max(地板, K × atr_pct × 窗口缩放)。远窗口按 √时长比放大。"""
    scale = HIT_FAR_SCALE if is_far else 1.0
    return max(HIT_FLOOR_PCT, HIT_ATR_K * scale * float(atr_pct or 0.0))


def classify_move(price, follow, atr_pct, is_far: bool) -> Optional[str]:
    """把后续价相对快照价的涨跌归为 'up'/'flat'/'down'（θ 内为横盘）；数据无效返回 None。

    端点口径（旧）：仅供 instinct 影子预测结算与参考列展示使用；
    分析记录命中率计分已切换到先触碰口径 classify_touch（批次16）。
    """
    if price is None or follow is None:
        return None
    price = float(price)
    follow = float(follow)
    if price <= 0 or follow <= 0:
        return None
    chg = (follow / price - 1) * 100
    th = neutral_theta(atr_pct, is_far)
    if chg > th:
        return 'up'
    if chg < -th:
        return 'down'
    return 'flat'


def classify_touch(bars: List[dict], p0, theta_pct: float):
    """先触碰判定：按时间升序扫描 bar 序列 [{'ts','high','low'}...]，谁先触 P0±θ 判谁。

    返回 (result, bar_ts)：
      'up'/'down'    首触上/下边界，立即终局（后续走势无法推翻已发生的首触）
      'conflict'     同一根 bar 上下双触，无法定先后 → 未兑现（保守口径）
      'none'         序列扫完未触 —— 仅当调用方确认窗口已扫完整（complete=True）才可定格
      (None, None)   输入无效（p0<=0 或 bars 非空但数据缺失不会发生）
    """
    p0 = float(p0 or 0)
    if p0 <= 0:
        return None, None
    up_level = p0 * (1 + theta_pct / 100.0)
    dn_level = p0 * (1 - theta_pct / 100.0)
    for b in bars or []:
        hit_up = float(b['high']) >= up_level
        hit_dn = float(b['low']) <= dn_level
        if hit_up and hit_dn:
            return 'conflict', b['ts']
        if hit_up:
            return 'up', b['ts']
        if hit_dn:
            return 'down', b['ts']
    return 'none', None


def resolve_plan(bars: List[dict], p0, target, invalid, head_ok: bool = True):
    """结构价计划结算（与方向无关：价位落在 bar 的 [low,high] 区间内即视为触及）。

    无固定截止窗：bars 为记录时刻之后全部已收线 bar，先触哪边哪边说了算。
    返回 {'result','r','touch_ts'} 或 None（尚无可扫 bar，下次重扫）：
      win    先触目标价，R = |target-P0| / |P0-invalid|
      loss   先触失效价，或同一根 bar 双触（保守按失效先行），R = -1
      open   扫到当前仍未触，分析继续有效、持续观察
      gap    记录时刻的 K 线已超出可拉深度（head_ok=False），无法判定，终局
    """
    p0 = float(p0 or 0)
    target = float(target or 0)
    invalid = float(invalid or 0)
    if p0 <= 0 or target <= 0 or invalid <= 0 or target == invalid:
        return None
    if not head_ok:
        return {'result': 'gap', 'r': None, 'touch_ts': None}
    for b in bars or []:
        lo, hi = float(b['low']), float(b['high'])
        i_hit = lo <= invalid <= hi
        t_hit = lo <= target <= hi
        if i_hit:
            # 含同根双触：保守按失效先行
            return {'result': 'loss', 'r': -1.0, 'touch_ts': b['ts']}
        if t_hit:
            risk = abs(p0 - invalid)
            r = round(abs(target - p0) / risk, 3) if risk > 0 else 0.0
            return {'result': 'win', 'r': r, 'touch_ts': b['ts']}
    if not bars:
        return None            # 还没有未来已收线 bar，下次查询重扫
    return {'result': 'open', 'r': None, 'touch_ts': None}


def _score_touch(b: dict, key: str, touch: str, expect_map: dict) -> None:
    """按先触碰结果给一个计分桶记一笔。

    方向判断(rise/fall/long/short)：先触期望边=hit，先触对侧=miss，
      none/conflict=未兑现(open，不进命中率分母)；
    观望(watch)：none=hit（确实没行情），up/down=miss（错失），conflict=open。
    """
    exp = expect_map[key]
    if exp == 'none':
        if touch == 'none':
            b['hit'] += 1
        elif touch in ('up', 'down'):
            b['miss'] += 1
        else:
            b['open'] += 1
    else:
        if touch in ('none', 'conflict'):
            b['open'] += 1
        elif touch == exp:
            b['hit'] += 1
        else:
            b['miss'] += 1


def compute_stats(records: List[dict]) -> dict:
    """基于记录列表计算先触碰口径的命中率、混淆矩阵与结构价计划统计。

    实际走势 = 窗口内 K 线路径先触碰判定（touch_near/touch_far 列）：
      up/down 谁先触 ±θ 带；none 全程未触(真横盘)；conflict 同根双触。
    - 个人判断：rise/fall 先触期望边=命中；none/conflict=未兑现不进分母；
      watch：none=命中，up/down=错失，conflict=未兑现
    - 策略方向：取 long_dir_prev（实际决策方向，缺省回退 long_dir），同上升/降规则
    - 命中率 = hit / (hit + miss)；未兑现(open)与待回填(pending)单独计数呈现
    - avg_pct：端点参考价均涨跌幅（信息列，不参与计分）
    - confusion：预测×路径 3×4 计数，供前端定位误判模式
    - plan：挂结构价计划的 win/loss/open/gap 计数、win_rate=win/(win+loss)、avg_r
    - 回传 near_mult/far_mult/hit_k/hit_floor_pct/hit_far_scale/score_mode，前端据此对齐口径
    """
    stats = {'total': len(records), 'score_mode': 'touch',
             'near_mult': REVIEW_NEAR_MULT, 'far_mult': REVIEW_FAR_MULT,
             'hit_k': HIT_ATR_K, 'hit_floor_pct': HIT_FLOOR_PCT, 'hit_far_scale': HIT_FAR_SCALE,
             'user': {'near': _empty_bucket(), 'far': _empty_bucket()},
             'strategy': {'near': _empty_bucket(), 'far': _empty_bucket()},
             'confusion': {'near': _empty_confusion(), 'far': _empty_confusion()},
             'plan': {'total': 0, 'win': 0, 'loss': 0, 'open': 0, 'gap': 0,
                      'pending': 0, 'win_rate': 0.0, 'avg_r': 0.0, '_rn': 0, '_rs': 0.0}}
    for rec in records:
        price = float(rec.get('price') or 0)
        if price <= 0:
            continue
        judgment = rec.get('user_judgment') or ''
        strategy_dir = rec.get('long_dir_prev') or rec.get('long_dir') or ''
        for win_key, touch_col, price_col in (('near', 'touch_near', 'price_1h'),
                                              ('far', 'touch_far', 'price_4h')):
            touch = rec.get(touch_col)
            settled = touch in ('up', 'down', 'none', 'conflict')
            follow = rec.get(price_col)
            chg = None
            if follow is not None and float(follow) > 0:
                chg = (float(follow) / price - 1) * 100
            if judgment in _TOUCH_EXPECT:
                b = stats['user'][win_key]
                if not settled:
                    b['pending'] += 1
                else:
                    _score_touch(b, judgment, touch, _TOUCH_EXPECT)
                    if chg is not None:
                        b['avg_pct'] += chg
                        b['chg_n'] += 1
                    stats['confusion'][win_key][judgment][touch] += 1
            if strategy_dir in _DIR_TOUCH_EXPECT:
                b = stats['strategy'][win_key]
                if not settled:
                    b['pending'] += 1
                else:
                    _score_touch(b, strategy_dir, touch, _DIR_TOUCH_EXPECT)
        # 结构价计划：只要挂了成对结构价就计入（与判断可改性解耦）
        if rec.get('target_price') and rec.get('invalid_price'):
            p = stats['plan']
            res = rec.get('plan_result')
            if res in ('win', 'loss', 'open', 'gap'):
                p[res] += 1
                p['total'] += 1
                if res in ('win', 'loss') and rec.get('plan_r') is not None:
                    p['_rs'] += float(rec['plan_r'])
                    p['_rn'] += 1
            else:
                p['pending'] += 1
                p['total'] += 1
    for side in ('user', 'strategy'):
        for win_key in ('near', 'far'):
            b = stats[side][win_key]
            b['scored'] = b['hit'] + b['miss']
            if b['scored'] > 0:
                b['rate'] = round(b['hit'] / b['scored'] * 100, 1)
            if b['chg_n'] > 0:
                b['avg_pct'] = round(b['avg_pct'] / b['chg_n'], 3)
            else:
                b['avg_pct'] = 0.0
    plan = stats['plan']
    decided = plan['win'] + plan['loss']
    if decided > 0:
        plan['win_rate'] = round(plan['win'] / decided * 100, 1)
    if plan['_rn'] > 0:
        plan['avg_r'] = round(plan['_rs'] / plan['_rn'], 3)
    plan.pop('_rn')
    plan.pop('_rs')
    return stats
