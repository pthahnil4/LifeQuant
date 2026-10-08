#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日评级总控台 - Flask 蓝图（批次15，原"每日多维打卡"）
========================================================
一天一条记录：热量 / 交易 / 学习三项自动指标 + 自我评级(E~S) + 纯文本评价，
外加与日记本的双向关联（diary_entry_id 定格 + 当日日记存在性提示）。

数据口径（详见 doc/每日多维打卡设计文档.md）：
- 热量：calorie_records.calorie_deficit 当日值 ≥ 800 kcal
- 交易：当日账户总权益涨幅（剔除出入金）≥ 8%
        base = 前一日最后一个 balance_history 快照（缺则回退最近快照并标注），
        live = OKX /api/v5/account/balance 实时 totalEq，
        净入金用 OKX 近7日账单流水（/api/v5/account/bills）非盈亏类条目求和剔除
- 学习：learn 卡当日打卡格 duration_minutes 合计 ≥ 10h，
        且当日打卡格 task_links 中 state=done 的去重任务数 ≥ 2

纪律红线：
- 页面加载（GET /checkin/api/today）绝不触达 OKX，只读按日缓存；
  实拉仅由 POST refresh-trade / 提交打卡（缓存过期时）触发，且全部走
  api_routes._rl_call 统一限频/退避出口，密钥不出服务端。
- 本模块对 calorie / plan / balance_history 零写入，唯一写目标是
  daily_checkins 与 trading_daily_snapshots 两张新表。
- 提交时服务端重算三项，不信任前端传来的数值。
"""

import datetime
import logging

from flask import Blueprint, jsonify, request, render_template

from .database import session_scope
from . import checkin_repo as repo

checkin_bp = Blueprint('checkin_bp', __name__)
logger = logging.getLogger(__name__)

# =============================================================================
# 阈值配置（kv_store key='checkin_config'，缺省用硬编码默认值）
# =============================================================================
KEY_CHECKIN_CONFIG = 'checkin_config'

_DEFAULT_CONFIG = {
    'calorie_target': 800.0,      # kcal，当日热量缺口 ≥ 该值达标
    'trade_pct_target': 8.0,      # %，当日总权益涨幅 ≥ 该值达标
    'learn_hours_target': 10.0,   # 小时，当日学习时长 ≥ 该值达标
    'task_count_target': 2,       # 个，当日去重完成任务 ≥ 该值达标
    'trade_cache_ttl_sec': 300,   # 交易指标缓存 TTL（秒）
    'learn_include_trade_cards': False,  # 学习时长是否把交易卡打卡也算入
}


def load_checkin_config() -> dict:
    """读打卡阈值配置：kv_store 覆盖默认值；DB 不可用时回退默认。"""
    cfg = dict(_DEFAULT_CONFIG)
    try:
        from . import config_store_repo
        stored = config_store_repo.load_json_config_cached(KEY_CHECKIN_CONFIG)
        if isinstance(stored, dict):
            for k in _DEFAULT_CONFIG:
                if k in stored:
                    cfg[k] = stored[k]
    except Exception:
        pass
    # 数值项兜底：非法值一律回落默认，避免比较运算炸掉
    for k, num in (('calorie_target', float), ('trade_pct_target', float),
                   ('learn_hours_target', float), ('task_count_target', int),
                   ('trade_cache_ttl_sec', float)):
        try:
            cfg[k] = num(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = _DEFAULT_CONFIG[k]
    cfg['learn_include_trade_cards'] = bool(cfg['learn_include_trade_cards'])
    return cfg


# =============================================================================
# OKX 采集（只读，统一走 api_routes 的限频出口）
# =============================================================================
# OKX 账单 type 中属于「当日盈亏」的类别（差分基准内应保留）；
# 其余（划转/充提/建仓-lp 产生/债转/资产转换等）均视为资金进出，从差额中剔除。
# 参考 SDK 枚举：1=归集转账在交易账户一般不出现；2=交易 3=交割 5=资金费用
# 8=策略收入 9=策略分配 11=强平结算 12=探测费 252=做市返佣 291=现金兑换。
_PNL_BILL_TYPES = {'2', '3', '5', '8', '9', '11', '12', '252', '291'}


def _resolve_account():
    """打卡交易项取数账号：与「交易配置」页同源（运行中调度器账号 → 默认账号）。

    延迟导入 app._resolve_task_account 避开蓝图↔app 循环依赖；
    导入失败时返回 None，get_api_config(None) 落到默认账号。
    """
    try:
        from .app import _resolve_task_account
        return _resolve_task_account()
    except Exception:
        return None


def _okx_client_and_call(account, method, **kwargs):
    """经统一限频出口调用只读 OKX 接口，返回 (data, error_msg)"""
    from api_config import get_api_config
    from .api_routes import _get_api_client, _rl_call, _okx_success
    config = get_api_config(account if account else None)
    client = _get_api_client('account', account or None)
    result = _rl_call(client, method, **kwargs)
    if not _okx_success(result) or not result.get('data'):
        return None, (result or {}).get('msg', 'OKX 查询失败'), config
    return result['data'], '', config


def _day_range_ms(date_str: str):
    """本地日 → [起始毫秒, 结束毫秒)，与 balance_history 的 ts 口径一致"""
    d = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    start_ms = int(d.timestamp() * 1000)
    end_ms = int((d + datetime.timedelta(days=1)).timestamp() * 1000)
    return start_ms, end_ms


def _pick_base(points, start_ms):
    """从升序快照点里选基准：启动日（date）之前最后一个点；
    没有则回退当日（或范围内）第一个点。返回 (ts, balance) 或 None。"""
    before = [p for p in points if p['ts'] < start_ms]
    if before:
        p = before[-1]
        return int(p['ts']), float(p['balance'])
    inside = [p for p in points if p['ts'] >= start_ms]
    if inside:
        p = inside[0]
        return int(p['ts']), float(p['balance'])
    return None


def _sum_non_pnl_bills(bills, start_ms, end_ms):
    """账单流水中非盈亏类（资金进出）条目的 balChg 净额。

    bills 为 OKX /api/v5/account/bills 原始记录；仅统计 balChg 为账户余额
    变化的划转/充提类条目。返回 None 表示无可剔除项（净入金 0）。
    """
    total = 0.0
    found = False
    for b in bills or []:
        try:
            ts = int(b.get('ts') or 0)
        except (TypeError, ValueError):
            continue
        if not (start_ms <= ts < end_ms):
            continue
        if str(b.get('type') or '') in _PNL_BILL_TYPES:
            continue
        try:
            total += float(b.get('balChg') or 0)
            found = True
        except (TypeError, ValueError):
            continue
    return total if found else 0.0


def _collect_trade_metric(date_str, force_live):
    """采集指定日期的交易指标，返回 dict（status/pct/base/live/transfer/...）。

    - 今天：live 走 OKX 实时权益（先查按日缓存，TTL 内或 force_live=False 用缓存），
      拉取成功后覆盖写缓存并顺带追加 balance_history 快照（复用现有口径）；
    - 历史（补录）：纯本地 balance_history 差分，无论 force_live 都不做 OKX 调用
      （账户权益无历史接口，实拉只会拿到"现在"的值，对历史日期无意义）；
      账单修正仅对近 7 日内的当天有效，历史日期 transfer 置 None（未修正）。
    """
    cfg = load_checkin_config()
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    if date_str != today:
        force_live = True  # 历史路径本就不打 OKX，跳过缓存判断
    account = _resolve_account()

    from api_config import get_api_config
    config = get_api_config(account if account else None)
    account_key = config['account']

    start_ms, end_ms = _day_range_ms(date_str)
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 今日 + 非强制刷新：只回缓存（TTL 内为新鲜，过期为陈旧快照），缺缓存返回
    # no_data 占位，绝不实拉 —— 这是「页面加载/状态条绝不触达 OKX」的硬保证；
    # 实拉只能由 force_live（手动 refresh-trade / 提交打卡）进入下面的路径。
    if date_str == today and not force_live:
        with session_scope() as session:
            row = repo.load_snapshot(session, account_key, date_str)
        if row is not None:
            d = row.to_dict()
            d['src'] = 'cache' if repo.snapshot_is_fresh(row, cfg['trade_cache_ttl_sec']) else 'cache_stale'
            return d
        return {
            'account_key': account_key, 'date': date_str,
            'base': None, 'base_date': '', 'live': None, 'transfer': None,
            'pct': None, 'status': 'no_data', 'src': 'none', 'fetched_at': '',
        }

    # ── 实拉路径 ──
    # 1) 基准：本地 balance_history（拉当日之前的最后一个快照；缺则回退当日内首个）
    base = base_date = None
    with session_scope() as session:
        from . import balance_repo
        prev_points = balance_repo.load_account_points(
            session, account_key, end_ms=start_ms)
        day_points = balance_repo.load_account_points(
            session, account_key, start_ms=start_ms,
            end_ms=None if date_str == today else end_ms)
    picked = _pick_base(prev_points, start_ms)
    if picked is None and date_str != today and day_points:
        # 历史补录没有前一日快照时，退化用当日首个快照做基准（pct≈0 但有值可看）
        picked = _pick_base(day_points, start_ms - 1)
    if picked:
        base_ts, base = picked
        base_date = datetime.datetime.fromtimestamp(base_ts / 1000).strftime('%Y-%m-%d')

    # 2) 当前值
    live = None
    status = 'ok'
    if date_str == today:
        try:
            data, err, _ = _okx_client_and_call(account, 'get_account_balance')
        except Exception as e:
            data = None
            logger.warning(f'[Checkin] 实时权益拉取异常: {e}')
        if data is None:
            status = 'api_error'
        else:
            try:
                live = round(float(data[0].get('totalEq') or 0), 4)
            except (TypeError, ValueError, IndexError):
                status = 'api_error'
            if status == 'ok':
                # 顺带追加快照（与 /plan/api/account-balance 同款行为），
                # 保证基准点持续积累；同秒重复 ts 天然覆盖去重
                try:
                    from .api_routes import _append_balance_snapshot
                    _append_balance_snapshot(
                        account_key, int(datetime.datetime.now().timestamp() * 1000), live)
                except Exception as snap_err:
                    logger.warning(f'[Checkin] 追加快照失败（不影响返回）: {snap_err}')
    else:
        # 历史补录：取当日最后一个本地快照（方案 §5-3；更细的分钟粒度不做）
        if day_points:
            live = round(float(day_points[-1]['balance']), 4)
            # 当日首个与末个同点（只有一条快照）时基准即当前值，pct=0
        else:
            status = 'no_data'

    # 3) 净入金修正（仅今日实拉路径；bills 只有近 7 日且限频成本高，拉一页 100 条）
    transfer = None
    if status == 'ok' and date_str == today and live is not None:
        try:
            bills, _err, _cfg = _okx_client_and_call(
                account, 'get_account_bills', limit='100')
            transfer = _sum_non_pnl_bills(bills, start_ms, end_ms)
        except Exception as e:
            logger.warning(f'[Checkin] 账单流水拉取失败（按未修正口径）: {e}')
            transfer = None

    # 4) 收益率
    pct = None
    if status == 'ok':
        if base is None or base <= 0:
            status = 'no_base' if (live is not None or date_str == today) else 'no_data'
        elif live is None:
            status = 'api_error'
        else:
            delta = live - base - (transfer or 0.0)
            pct = round(delta / base * 100, 2)

    result = {
        'account_key': account_key, 'date': date_str,
        'base': base, 'base_date': base_date or '',
        'live': live, 'transfer': transfer, 'pct': pct,
        'status': status, 'src': 'live', 'fetched_at': now_str,
    }

    # 5) 今日实拉结果落缓存（历史补录不写缓存，避免污染按日缓存语义）
    if date_str == today:
        try:
            with session_scope() as session:
                repo.upsert_snapshot(session, account_key, date_str, {
                    'base_balance': base, 'base_date': base_date or '',
                    'live_balance': live, 'net_transfer': transfer,
                    'pct': pct, 'status': status, 'fetched_at': now_str,
                })
        except Exception as e:
            logger.warning(f'[Checkin] 交易指标缓存写入失败（不影响返回）: {e}')

    return result


# =============================================================================
# 三项指标采集（热量 / 学习 为本地 DB 只读）
# =============================================================================

def _collect_calorie_metric(date_str, cfg):
    from .models import CalorieRecord
    with session_scope() as session:
        row = session.get(CalorieRecord, date_str)
        deficit = float(row.calorie_deficit) if row is not None else None
    if deficit is None:
        return {'value': None, 'passed': False, 'status': 'no_data'}
    return {'value': round(deficit, 2),
            'passed': deficit >= cfg['calorie_target'], 'status': 'ok'}


def _collect_learn_metric(date_str, cfg):
    from . import plan_repo
    with session_scope() as session:
        stats = plan_repo.load_daily_learn_stats(
            session, date_str, include_trade=cfg['learn_include_trade_cards'])
    hours = stats['hours']
    tasks = stats['task_done_count']
    passed = (hours >= cfg['learn_hours_target']
              and tasks >= cfg['task_count_target'])
    return {'hours': hours, 'task_done_count': tasks,
            'passed': passed, 'status': 'ok'}


def _collect_diary_status(date_str):
    """当日日记存在性（只读 diary_entries，零写入）。用于打卡页「是否已写日记」
    提示与提交时定格 diary_entry_id。隐私条目只暴露存在性，绝不带出正文。

    优先取回指本模块的条目（linked_from='checkin:YYYY-MM-DD'），否则退化为
    当日最后写的一条。返回 {'has_entry', 'entry_count', 'entry_id'}。
    """
    from sqlalchemy import select
    from .models import DiaryEntry
    with session_scope() as session:
        rows = session.execute(
            select(DiaryEntry.id, DiaryEntry.linked_from)
            .where(DiaryEntry.date == date_str)
            .order_by(DiaryEntry.created_at, DiaryEntry.id)).all()
    count = len(rows)
    linked = f'checkin:{date_str}'
    entry_id = ''
    for _rid, lf in rows:               # 正序遍历，最后一条 linked 命中即取最新
        if (lf or '') == linked:
            entry_id = _rid
    if not entry_id and rows:
        entry_id = rows[-1][0]          # 无回指条目则退化取当日最后一条
    return {'has_entry': count > 0, 'entry_count': count, 'entry_id': entry_id}


def _collect_all(date_str, force_trade_live, cfg=None):
    """三项一起采集 + 达标判定（提交时服务端重算的同一入口）。"""
    cfg = cfg or load_checkin_config()
    calorie = _collect_calorie_metric(date_str, cfg)
    learn = _collect_learn_metric(date_str, cfg)
    trade = _collect_trade_metric(date_str, force_trade_live)
    trade['passed'] = (trade.get('status') == 'ok'
                       and trade.get('pct') is not None
                       and trade['pct'] >= cfg['trade_pct_target'])
    all_passed = bool(calorie['passed'] and trade['passed'] and learn['passed'])
    return cfg, calorie, trade, learn, all_passed


def _metric_payload(cfg, calorie, trade, learn, all_passed):
    """把采集结果组装成 API data 结构（含阈值，前端显示 800/8%/10h/2 个）"""
    return {
        'targets': {
            'calorie': cfg['calorie_target'], 'trade_pct': cfg['trade_pct_target'],
            'learn_hours': cfg['learn_hours_target'], 'task_count': cfg['task_count_target'],
        },
        'calorie': {'value': calorie['value'], 'passed': calorie['passed'],
                    'status': calorie['status']},
        'trade': {
            'pct': trade.get('pct'), 'base': trade.get('base'),
            'base_date': trade.get('base_date') or '', 'live': trade.get('live'),
            'transfer': trade.get('transfer'), 'passed': bool(trade.get('passed')),
            'status': trade.get('status') or 'no_data', 'src': trade.get('src') or '',
            'fetched_at': trade.get('fetched_at') or '',
            'account_key': trade.get('account_key') or '',
        },
        'learn': {'hours': learn['hours'], 'task_done_count': learn['task_done_count'],
                  'passed': learn['passed'], 'status': learn['status']},
        'all_passed': all_passed,
    }


# =============================================================================
# 页面路由
# =============================================================================

@checkin_bp.route('/checkin')
def checkin_page():
    """每日打卡页"""
    return render_template('checkin.html', active_page='checkin')


# =============================================================================
# API 路由
# =============================================================================

@checkin_bp.route('/checkin/api/today', methods=['GET'])
def api_today():
    """聚合当日（?date= 可查历史）三项自动指标 + 已存打卡。

    交易项只读按日缓存，绝不触达 OKX（页面自动加载/首页状态条共用此接口）。
    GET 不写任何表。
    """
    try:
        date_str = (request.args.get('date') or '').strip() \
            or datetime.datetime.now().strftime('%Y-%m-%d')
        try:
            datetime.datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'code': 400, 'message': '日期格式应为 YYYY-MM-DD', 'data': None})
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        if date_str > today:
            return jsonify({'code': 400, 'message': '不能查询未来日期', 'data': None})

        cfg, calorie, trade, learn, all_passed = _collect_all(date_str, force_trade_live=False)
        # 未实时拉取且无缓存时（如今日尚未刷过），交易项标注 no_data 供前端引导
        if date_str == today and trade.get('status') in ('', None):
            trade = dict(trade, status='no_data', passed=False)

        with session_scope() as session:
            saved = repo.load_checkin(session, date_str)
            saved_dict = saved.to_dict() if saved is not None else None

        return jsonify({'code': 200, 'message': 'success', 'data': {
            'date': date_str, 'is_today': date_str == today,
            'metrics': _metric_payload(cfg, calorie, trade, learn, all_passed),
            'diary': _collect_diary_status(date_str),
            'saved': saved_dict,
        }})
    except Exception as e:
        logger.error(f'[Checkin] api_today 错误: {e}', exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@checkin_bp.route('/checkin/api/refresh-trade', methods=['POST'])
def api_refresh_trade():
    """手动刷新交易指标：实拉 OKX（限频出口）+ 写按日缓存。

    TTL 内重复调用直接回缓存（force=1 可强制重拉）。
    """
    try:
        body = request.get_json(silent=True) or {}
        date_str = (body.get('date') or '').strip() \
            or datetime.datetime.now().strftime('%Y-%m-%d')
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        if date_str != today:
            # 历史日期不提供实拉（OKX 权益无历史接口，bills 仅近 7 日）
            return jsonify({'code': 400, 'message': '仅今日支持刷新交易数据', 'data': None})
        force = bool(body.get('force'))
        cfg = load_checkin_config()
        if not force:
            from api_config import get_api_config
            account_key = get_api_config(_resolve_account())['account']
            with session_scope() as session:
                row = repo.load_snapshot(session, account_key, date_str)
            if row is not None and repo.snapshot_is_fresh(row, cfg['trade_cache_ttl_sec']):
                d = row.to_dict()
                d['src'] = 'cache'
                return jsonify({'code': 200, 'message': '缓存命中', 'data': d})
        trade = _collect_trade_metric(date_str, force_live=True)
        return jsonify({'code': 200, 'message': 'success', 'data': trade})
    except Exception as e:
        logger.error(f'[Checkin] api_refresh_trade 错误: {e}', exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@checkin_bp.route('/checkin/api/checkin', methods=['POST'])
def api_submit_checkin():
    """提交/覆盖当日打卡：服务端重算三项 + 定格落库。

    body: {date?, rating?, comment?}；date 仅允许今天或过去（补录），禁止未来；
    评级非法值归空（可选字段），评价按长度截断。
    """
    try:
        body = request.get_json(silent=True) or {}
        now = datetime.datetime.now()
        date_str = (body.get('date') or '').strip() or now.strftime('%Y-%m-%d')
        try:
            datetime.datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'code': 400, 'message': '日期格式应为 YYYY-MM-DD', 'data': None})
        today = now.strftime('%Y-%m-%d')
        if date_str > today:
            return jsonify({'code': 400, 'message': '不能打卡未来日期', 'data': None})
        raw_comment = str(body.get('comment') or '')
        if len(raw_comment) > repo.MAX_COMMENT_CHARS + 200:
            return jsonify({'code': 400,
                            'message': f'评价过长（上限 {repo.MAX_COMMENT_CHARS} 字符）',
                            'data': None})

        cfg, calorie, trade, learn, all_passed = _collect_all(date_str, force_trade_live=True)

        record = {
            'date': date_str,
            'cal_value': calorie['value'], 'cal_passed': calorie['passed'],
            'cal_status': calorie['status'],
            'trade_base': trade.get('base'), 'trade_base_date': trade.get('base_date') or '',
            'trade_live': trade.get('live'), 'trade_transfer': trade.get('transfer'),
            'trade_pct': trade.get('pct'), 'trade_passed': bool(trade.get('passed')),
            'trade_status': trade.get('status') or 'no_data', 'trade_src': 'live',
            'learn_hours': learn['hours'], 'task_done_count': learn['task_done_count'],
            'learn_passed': learn['passed'], 'learn_status': learn['status'],
            'rating': repo.normalize_rating(body.get('rating')),
            'comment': repo.normalize_comment(body.get('comment')),
            'all_passed': all_passed,
            'account_key': trade.get('account_key') or '',
            # 提交时定格当日日记关联（此时无日记则留空；23:30 提醒按实时存在性再判）
            'diary_entry_id': _collect_diary_status(date_str)['entry_id'],
        }
        with session_scope() as session:
            row = repo.upsert_checkin(session, record)
            saved = row.to_dict()
        return jsonify({'code': 200, 'message': '打卡已保存', 'data': saved})
    except ValueError as e:
        return jsonify({'code': 400, 'message': str(e), 'data': None})
    except Exception as e:
        logger.error(f'[Checkin] api_submit 错误: {e}', exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@checkin_bp.route('/checkin/api/history', methods=['GET'])
def api_history():
    """历史打卡列表：?start=&end=（缺省最近 30 天），范围 ≤ 62 天。"""
    try:
        today = datetime.datetime.now()
        end = (request.args.get('end') or '').strip() or today.strftime('%Y-%m-%d')
        start = (request.args.get('start') or '').strip() \
            or (today - datetime.timedelta(days=29)).strftime('%Y-%m-%d')
        try:
            d_start = datetime.datetime.strptime(start, '%Y-%m-%d')
            d_end = datetime.datetime.strptime(end, '%Y-%m-%d')
        except ValueError:
            return jsonify({'code': 400, 'message': '日期格式应为 YYYY-MM-DD', 'data': None})
        if d_start > d_end:
            return jsonify({'code': 400, 'message': '开始日期不能晚于结束日期', 'data': None})
        if (d_end - d_start).days > 62:
            return jsonify({'code': 400, 'message': '单次查询范围不能超过 62 天', 'data': None})

        with session_scope() as session:
            rows = repo.load_history(session, start, end)
            items = [r.to_dict() for r in rows]
        return jsonify({'code': 200, 'message': 'success',
                        'data': {'start': start, 'end': end, 'items': items}})
    except Exception as e:
        logger.error(f'[Checkin] api_history 错误: {e}', exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})
