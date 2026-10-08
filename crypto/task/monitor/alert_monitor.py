#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
监控告警引擎 — 异常行情与持仓盈亏监控报警（迁移批次9）
=========================================================

两类监控：
1. 行情异动 (price)：监控币种在 N 分钟窗口内的涨跌幅，
   超 warning/critical 阈值分别触发预警/严重告警，回落后发缓解告警。
2. 持仓盈亏 (pnl)：监控指定账号的永续合约持仓浮动盈亏率（标的价格口径，
   多头 (现价-均价)/均价、空头 (均价-现价)/均价），亏损超阈值告警。

【级别递进状态机】（运行时状态存 kv_store key='alert_runtime_state'）
- 新命中           → 发送告警，记录级别
- 同级别重复命中   → 抑制不发（alert_log 仍留档，notify_sent=0）
- warning→critical → 升级发送
- critical→warning → 降级抑制（仍在风险区，不重复打扰）
- 回落至阈值以内   → 发送 recover 缓解告警，清除状态

【邮件】同一检测轮多个命中合并为一封摘要邮件（防批量轰炸），
单命中发专用邮件；冷却完全由上述状态机控制，不叠加通知器冷却。

【旁路约定】alert_log 落库失败只记日志，不阻断监控主循环。

配置存 kv_store key='alert_config'（config_store_repo 整份存取），
由 alert_routes 的配置保存接口热更新并调 register_alert_job 重注册。
"""

import copy
import datetime
import logging
import time

# 双模式导入：Flask 包内（crypto.task.monitor）/ 独立脚本（task 目录在 sys.path）
try:
    from ..utils.logger import get_task_logger
    from ..notification.message_notifier import MessageNotifier
except ImportError:
    from utils.logger import get_task_logger
    from notification.message_notifier import MessageNotifier

try:
    from crypto.database import session_scope
    from crypto import config_store_repo
    from crypto.models import AlertLog
    from crypto.api_config import get_api_config
except ImportError:
    session_scope = None
    config_store_repo = None
    AlertLog = None
    get_api_config = None

import okx.MarketData as MarketData
import okx.Account as Account

# 只读接口限频/退避（问题#8）：本监控线程与实盘调度、页面轮询共用同一个 API key，
# 不限频时互相打爆就集体回 50011，表现为“该发告警的那轮拿不到数据”静默漏报。
# 导入失败时置 None，_rl() 直通原始调用，绝不因为限频模块不可用而断掉告警链路。
# 两个调用点都在 except Exception 里，所以抛 RateLimited 也只是“本轮跳过”。
try:
    try:
        from ..utils.okx_ratelimit import limited as _rl_limited
    except ImportError:
        from utils.okx_ratelimit import limited as _rl_limited
except ImportError:
    _rl_limited = None


def _rl(group, func, *args, **kwargs):
    """只读 OKX 接口的统一出口：节流 + 50011 退避；限频模块缺失时直通。

    严禁把下单/撤单类请求塞进来（自动重发会重复下单）。
    """
    return _rl_limited(group, func, *args, **kwargs) if _rl_limited else func(*args, **kwargs)

logger = logging.getLogger(__name__)

_MARKET_API = None


def _get_market_api():
    """模块级单例：复用 OKX 行情客户端，避免每轮新建连接/会话 churn"""
    global _MARKET_API
    if _MARKET_API is None:
        _MARKET_API = MarketData.MarketAPI(flag='0')
    return _MARKET_API

task_log = get_task_logger()

# kv_store 键名
KEY_ALERT_CONFIG = 'alert_config'
KEY_ALERT_RUNTIME_STATE = 'alert_runtime_state'

# 调度任务 ID（与 scheduler 注册/热重注册口径一致）
ALERT_JOB_ID = 'alert_monitor'

# 默认配置（页面未保存过配置时使用；数值均可在风险警报页调整）
DEFAULT_ALERT_CONFIG = {
    'enabled': True,              # 监控总开关
    'interval_seconds': 300,      # 检测周期（秒）
    'account': '',                # 持仓监控账号（空=默认账号）
    'price': {
        'enabled': True,          # 行情异动监控开关
        'inst_ids': [],           # 监控币种；为空时跟随实盘交易配置的 currencies
        'window_minutes': 15,     # 涨跌幅检测窗口（分钟）
        'warning_pct': 5.0,       # 预警阈值（绝对值 %）
        'critical_pct': 8.0,      # 严重阈值（绝对值 %）
    },
    'pnl': {
        'enabled': True,          # 持仓盈亏监控开关（同时管浮亏与浮盈两侧）
        'warning_pct': -10.0,     # 浮亏预警阈值（% ，负值）
        'critical_pct': -20.0,    # 浮亏严重阈值（%，负值）
        # 盈利侧提醒（用户诉求：暴涨/盈利达阈值也要提醒，阈值全由配置决定）
        'profit_enabled': True,   # 盈利提醒开关（受 pnl.enabled 总开关约束）
        'profit_warning_pct': 10.0,    # 浮盈预警阈值（%，正值）
        'profit_critical_pct': 20.0,   # 浮盈达标提醒阈值（%，正值）
    },
    'liq': {
        'enabled': True,          # 强平距离监控开关（现价距强平价的百分比）
        'warning_pct': 5.0,       # 距离 ≤ 此值预警（越近越危险）
        'critical_pct': 2.0,      # 距离 ≤ 此值严重告警（随时可能爆仓）
    },
    'acct': {
        'enabled': True,            # 账户余额/保证金率巡检开关（只读旁路）
        # 保证金率 = 调整权益/维持保证金 ×100%，越接近 100% 越接近全仓强平
        'margin_warning_pct': 200.0,   # 保证金率 ≤ 此倍数预警
        'margin_critical_pct': 120.0,  # ≤ 此倍数严重告警（临近连环强平）
        # 可用 USDT 过低 = 下一单大概率 51008、也无钱追加保证金
        'min_avail_warning': 100.0,    # 可用余额 ≤ 此值预警（USDT）
        'min_avail_critical': 20.0,    # ≤ 此值严重告警（USDT）；设 0 关闭余额检查
    },
    'liveness': {
        'enabled': True,               # 调度轮存活检测开关（主循环停滞/线程意外死亡旁路告警）
        'grace_multiplier': 5,         # 停滞预警下限 = 执行间隔 × 此倍数（跟随轮次节奏）
        'min_stale_seconds': 900,      # 绝对下限（秒）：轮次间隔再小也至少停滞这么久才报，避开正常长批次
        'critical_stale_seconds': 2400,  # 停滞超此秒数升为严重（默认40分钟，线程活着也几乎确定卡死）
    },
}

# 最近一轮检测摘要（供 alert_routes 状态接口展示）
_LAST_RUN = {
    'finished_at': None,     # 上一轮结束时间
    'duration_ms': 0,        # 耗时
    'hit_count': 0,          # 命中数（含被抑制）
    'sent_count': 0,         # 实际发出邮件的命中数
    'email_ok': None,        # 汇总邮件是否发送成功（无命中时为 None）
    'error': None,           # 本轮异常信息
}


def get_last_run() -> dict:
    """最近一轮检测摘要（副本）"""
    return dict(_LAST_RUN)


# =============================================================================
# 配置 / 运行时状态存取（DB 优先，失败降级）
# =============================================================================

def _deep_merge(base: dict, override: dict) -> dict:
    """默认配置深合并用户配置，保证新增字段自动补齐"""
    merged = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_alert_config() -> dict:
    """读取监控配置：带 TTL 缓存的 kv_store 优先，DB 不可用/无数据时用默认配置；
    save_alert_config 写入时写穿透失效缓存"""
    if session_scope is None or config_store_repo is None:
        return copy.deepcopy(DEFAULT_ALERT_CONFIG)
    try:
        saved = config_store_repo.load_json_config_cached(KEY_ALERT_CONFIG)
        return _deep_merge(DEFAULT_ALERT_CONFIG, saved or {})
    except Exception as e:
        task_log.warning(f'[AlertMonitor] 读取监控配置失败，使用默认配置: {e}')
        return copy.deepcopy(DEFAULT_ALERT_CONFIG)


def save_alert_config(cfg: dict):
    """整份覆盖保存监控配置到 kv_store"""
    with session_scope() as s:
        config_store_repo.save_json_config(s, KEY_ALERT_CONFIG, cfg)


def _load_runtime_state() -> dict:
    """读取级别递进状态机；DB 不可用/无数据时返回空（本轮按新命中处理）"""
    if session_scope is None or config_store_repo is None:
        return {}
    try:
        with session_scope() as s:
            state = config_store_repo.load_json_config(s, KEY_ALERT_RUNTIME_STATE)
        return state if isinstance(state, dict) else {}
    except Exception as e:
        task_log.warning(f'[AlertMonitor] 读取运行时状态失败: {e}')
        return {}


def _save_runtime_state(state: dict):
    try:
        with session_scope() as s:
            config_store_repo.save_json_config(s, KEY_ALERT_RUNTIME_STATE, state)
    except Exception as e:
        task_log.warning(f'[AlertMonitor] 保存运行时状态失败（不影响本轮告警发送）: {e}')


# =============================================================================
# 数据获取
# =============================================================================

def _resolve_price_symbols(cfg: dict) -> list:
    """行情监控币种：配置显式指定优先；为空时跟随实盘交易配置的 currencies"""
    inst_ids = [x.strip() for x in (cfg.get('price', {}).get('inst_ids') or []) if str(x).strip()]
    if inst_ids:
        return inst_ids
    try:
        trading_cfg = config_store_repo.load_live_strategy_config_cached()
        return [c.get('instId', '') for c in (trading_cfg or {}).get('currencies', [])
                if c.get('instId')]
    except Exception as e:
        task_log.warning(f'[AlertMonitor] 读取实盘交易配置失败，行情监控本轮跳过: {e}')
        return []


def _fetch_window_change_pct(market_api, inst_id: str, window_minutes: int):
    """取 1m K 线计算窗口内涨跌幅 %；数据不足返回 None"""
    limit = min(int(window_minutes) + 2, 100)
    res = _rl('market_candles', market_api.get_candlesticks,
              instId=inst_id, bar='1m', limit=str(limit))
    data = (res or {}).get('data') or []
    if len(data) < 2:
        return None
    # OKX 返回新→旧，反转为旧→新；用窗口起点收盘与最新收盘对比
    closes = [float(row[4]) for row in reversed(data)]
    base, last = closes[0], closes[-1]
    if base <= 0:
        return None
    return (last / base - 1.0) * 100.0


def _fetch_positions(account: str):
    """查询指定账号的永续合约有效持仓（原始字段，含 avgPx/last/pos/posSide）"""
    api_cfg = get_api_config(account or None)
    account_api = Account.AccountAPI(
        api_cfg['api_key'], api_cfg['secret_key'], api_cfg['passphrase'],
        False, api_cfg['flag'])
    res = _rl('positions', account_api.get_positions, instType='SWAP')
    if not res or res.get('code') != '0':
        raise RuntimeError(f"持仓查询失败: {(res or {}).get('msg', res)}")
    positions = []
    for pos in res.get('data', []):
        try:
            if float(pos.get('pos', '0') or 0) == 0:
                continue
        except (TypeError, ValueError):
            continue
        positions.append(pos)
    return positions


def _fetch_account_risk(account: str) -> dict:
    """查询账户可用余额与保证金率（余额/保证金率巡检，只读）。

    保证金率口径 = 调整权益 adjEq / 维持保证金 maintMargin × 100%（越接近 100% 越
    接近全仓强平）；无维持保证金（无全仓敞口）时保证金率返回 None（跳过该项检查）。
    可用余额取 USDT 明细 availBal（51008 卡的就是它）。余额接口取不到直接抛，
    由调用方按"本轮跳过"降级；维持保证金查询失败则只丢保证金率、保留余额检查。
    """
    api_cfg = get_api_config(account or None)
    account_api = Account.AccountAPI(
        api_cfg['api_key'], api_cfg['secret_key'], api_cfg['passphrase'],
        False, api_cfg['flag'])

    def _f(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    bal = _rl('balance', account_api.get_account_balance)
    if not bal or bal.get('code') != '0' or not bal.get('data'):
        raise RuntimeError(f"余额查询失败: {(bal or {}).get('msg', bal)}")
    data = (bal.get('data') or [{}])[0]
    avail_usdt = None
    for d in (data.get('details') or []):
        if str(d.get('ccy')) == 'USDT':
            try:
                avail_usdt = float(d.get('availBal') or 0)
            except (TypeError, ValueError):
                avail_usdt = None
            break
    adj_eq = _f(data.get('adjEq'))
    maint_margin = 0.0
    try:
        pr = _rl('account_risk', account_api.get_account_position_risk)
        if pr and pr.get('code') == '0' and pr.get('data'):
            maint_margin = _f(pr['data'][0].get('maintMargin'))
    except Exception as e:
        task_log.warning(f'[AlertMonitor] 维持保证金查询失败（本轮跳过保证金率）: {e}')
    margin_ratio_pct = (adj_eq / maint_margin * 100.0) if maint_margin > 0 else None
    return {'avail_usdt': avail_usdt, 'adj_eq': adj_eq, 'maint_margin': maint_margin,
            'margin_ratio_pct': margin_ratio_pct}


def _position_pnl_pct(pos: dict):
    """按标的价格口径计算浮动盈亏率 %（多头 (现价-均价)/均价，空头反向）。

    返回 (pnl_pct, direction, avg_px, last_px)，数据缺失返回 None。
    """
    try:
        avg_px = float(pos.get('avgPx') or 0)
        last_px = float(pos.get('last') or 0)
        qty = float(pos.get('pos') or 0)
    except (TypeError, ValueError):
        return None
    if avg_px <= 0 or last_px <= 0 or qty == 0:
        return None
    pos_side = pos.get('posSide', 'net')
    if pos_side == 'short' or (pos_side == 'net' and qty < 0):
        direction = '空头'
        pnl_pct = (avg_px - last_px) / avg_px * 100.0
    else:
        direction = '多头'
        pnl_pct = (last_px - avg_px) / avg_px * 100.0
    return pnl_pct, direction, avg_px, last_px


def _position_liq_info(pos: dict):
    """计算现价距强平价的百分比。

    距离 = |标记价 − 强平价| / 标记价 × 100（越接近 0 越危险）。标记价缺失回退最新成交价
    （强平价由交易所按标记价推算，用标记价口径最贴合）。数据缺失/无强平价（如全仓合并
    头寸可能不返回逐笔 liqPx）返回 None。返回 (dist_pct, liq_px, mark_px, direction)。
    """
    try:
        liq_px = float(pos.get('liqPx') or 0)
        mark_px = float(pos.get('markPx') or 0) or float(pos.get('last') or 0)
    except (TypeError, ValueError):
        return None
    if liq_px <= 0 or mark_px <= 0:
        return None
    qty = float(pos.get('pos') or 0)
    pos_side = pos.get('posSide', 'net')
    direction = ('空头' if (pos_side == 'short' or (pos_side == 'net' and qty < 0))
                 else '多头')
    return abs(mark_px - liq_px) / mark_px * 100.0, liq_px, mark_px, direction


# =============================================================================
# 级别递进状态机
# =============================================================================

def _classify_price_level(change_pct: float, price_cfg: dict) -> str:
    """行情异动级别判定（阈值取绝对值比较）；未命中返回 ''"""
    if abs(change_pct) >= float(price_cfg.get('critical_pct', 8.0)):
        return 'critical'
    if abs(change_pct) >= float(price_cfg.get('warning_pct', 5.0)):
        return 'warning'
    return ''


def _classify_pnl_level(pnl_pct: float, pnl_cfg: dict) -> str:
    """持仓盈亏级别判定（阈值为负值，亏损越深级别越高）；未命中返回 ''"""
    if pnl_pct <= float(pnl_cfg.get('critical_pct', -20.0)):
        return 'critical'
    if pnl_pct <= float(pnl_cfg.get('warning_pct', -10.0)):
        return 'warning'
    return ''


def _classify_profit_level(pnl_pct: float, pnl_cfg: dict) -> str:
    """盈利侧提醒级别判定（阈值为正值，盈利越大级别越高）；未命中/关闭返回 ''。

    与浮亏侧互斥（pnl_pct 带符号，正=盈利、负=亏损），二者各用独立状态机键位，
    互不干扰。目的＝提醒用户"暴涨/达标盈利"，阈值全由配置决定。
    """
    if not pnl_cfg.get('profit_enabled', True):
        return ''
    warn = float(pnl_cfg.get('profit_warning_pct', 10.0))
    crit = float(pnl_cfg.get('profit_critical_pct', 20.0))
    if crit > 0 and pnl_pct >= crit:
        return 'critical'
    if warn > 0 and pnl_pct >= warn:
        return 'warning'
    return ''


def _classify_liq_level(dist_pct: float, liq_cfg: dict) -> str:
    """强平距离级别判定（距离越小越危险，阈值为正百分比）；未命中返回 ''"""
    if dist_pct <= float(liq_cfg.get('critical_pct', 2.0)):
        return 'critical'
    if dist_pct <= float(liq_cfg.get('warning_pct', 5.0)):
        return 'warning'
    return ''


def _classify_margin_level(ratio_pct, acct_cfg: dict) -> str:
    """保证金率级别判定（越接近 100% 越危险，阈值为正百分比）；None/未命中返回 ''"""
    if ratio_pct is None:
        return ''
    if ratio_pct <= float(acct_cfg.get('margin_critical_pct', 120.0)):
        return 'critical'
    if ratio_pct <= float(acct_cfg.get('margin_warning_pct', 200.0)):
        return 'warning'
    return ''


def _classify_avail_level(avail_usdt, acct_cfg: dict) -> str:
    """可用余额级别判定（越少越危险）；None/未配置(≤0)/未命中返回 ''"""
    if avail_usdt is None:
        return ''
    warn = float(acct_cfg.get('min_avail_warning', 100.0))
    crit = float(acct_cfg.get('min_avail_critical', 20.0))
    if warn <= 0:               # 阈值设 0 视为关闭余额检查
        return ''
    if avail_usdt <= crit:
        return 'critical'
    if avail_usdt <= warn:
        return 'warning'
    return ''


def _liveness_thresholds(liveness_cfg: dict, interval_seconds) -> tuple:
    """按执行间隔与配置算出 (预警停滞秒数, 严重停滞秒数)。

    预警下限取「间隔×倍数」与「绝对下限」较大者，避免小间隔下正常长批次误报；
    严重阈值不低于预警阈值。
    """
    interval = max(1, int(interval_seconds or 60))
    grace = float(liveness_cfg.get('grace_multiplier', 5))
    warn_thr = max(interval * grace, float(liveness_cfg.get('min_stale_seconds', 900)))
    crit_thr = max(float(liveness_cfg.get('critical_stale_seconds', 2400)), warn_thr)
    return warn_thr, crit_thr


def _classify_liveness_level(thread_alive, lag_seconds, running,
                             liveness_cfg: dict, interval_seconds) -> str:
    """调度轮存活级别判定；未命中返回 ''。

    - 关闭检测 / 用户已停止（running=False）→ 不判停滞（走 recover 清历史状态）；
    - 期望在跑但线程已死 → critical（致命，交易已停摆）；
    - 线程活着但轮次时间戳停滞（lag）超严重阈值 → critical，超预警阈值 → warning；
    - 尚未跑出可判定轮次（lag None）→ 不命中，避免启动初期误报。
    """
    if not liveness_cfg.get('enabled', True):
        return ''
    if not running:
        return ''
    if not thread_alive:
        return 'critical'
    if lag_seconds is None:
        return ''
    warn_thr, crit_thr = _liveness_thresholds(liveness_cfg, interval_seconds)
    if lag_seconds >= crit_thr:
        return 'critical'
    if lag_seconds >= warn_thr:
        return 'warning'
    return ''


def _classify_boll_level(price, upper, lower, boll_cfg: dict) -> str:
    """BOLL 出轨强度分级（只读旁路）。

    以「价格相对轨边的越界幅度」为准（band = 上轨−下轨，做尺度归一）：
    - 价格 ≤ 下轨：跌破下轨；价格 ≥ 上轨：冲破上轨（暴涨/插针越界即在此命中）。
    - 顺势（与长周期方向同侧破轨）达 warning_band→warning；逆势（与持仓/长周期方向相反
      一侧破轨，代表行情打脸当前方向）达阈值直接→critical（逆势破轨更该叫醒）。
      无方向信息时按普通破轨给 warning，升级由 band 倍数决定。
    - 价格回到 [下轨, 上轨] 区间内 → 不命中（由状态机自动 recover）。
    数据缺失（upper/lower≤0 或 price≤0）返回 ''。
    """
    try:
        price = float(price)
        upper = float(upper)
        lower = float(lower)
    except (TypeError, ValueError):
        return ''
    if price <= 0 or upper <= 0 or lower <= 0 or upper <= lower:
        return ''
    band = upper - lower
    if band <= 0:
        return ''
    warn_ratio = float(boll_cfg.get('warning_band_ratio', 0.5))
    crit_ratio = float(boll_cfg.get('critical_band_ratio', 1.5))
    direction = str(boll_cfg.get('direction', '') or '')   # 'long'/'short'/''
    exceed = 0.0
    if price >= upper:
        exceed = (price - upper) / band
    elif price <= lower:
        exceed = (lower - price) / band
    else:
        return ''                                          # 仍在轨内
    if exceed < warn_ratio:
        return ''
    # 逆势破轨：价冲破上轨却持空/看空，或跌破下轨却持多/看多 → 直接严重
    against = (('short' == direction and price >= upper)
               or ('long' == direction and price <= lower))
    if against or exceed >= crit_ratio:
        return 'critical'
    return 'warning'


_LEVEL_RANK = {'': 0, 'warning': 1, 'critical': 2}


def _transition(state: dict, key: str, level: str):
    """状态机迁移，返回 (动作, 需落库的记录级别)。

    动作: 'send' 发送 | 'suppress' 抑制 | 'recover' 缓解 | 'none' 无动作
    """
    prev_level = (state.get(key) or {}).get('level', '')
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if level:
        if prev_level == level:
            return 'suppress', level          # 同级别重复命中 → 抑制
        if _LEVEL_RANK[level] > _LEVEL_RANK[prev_level]:
            state[key] = {'level': level, 'updated_at': now_str}
            return 'send', level              # 新命中或升级 → 发送
        # critical → warning 降级：仍在风险区，不打扰
        state[key] = {'level': level, 'updated_at': now_str}
        return 'suppress', level
    if prev_level:
        state.pop(key, None)
        return 'recover', 'recover'           # 回落阈值以内 → 缓解
    return 'none', ''


# =============================================================================
# 检测主循环
# =============================================================================

def run_alert_check():
    """执行一轮监控检测（调度器周期调用 / 页面手动触发共用）"""
    global _LAST_RUN
    started = datetime.datetime.now()
    run_id = started.strftime('A%Y%m%d-%H%M%S')
    hits = []          # 全部命中（含抑制），写 alert_log
    notify_items = []  # 实际需发送的命中
    liq_items = []     # 强平距离需发送的命中（单独邮件，不并入行情/盈亏摘要）
    profit_items = []  # 盈利侧需发送的命中（单独邮件，正向配色，不并入亏损摘要）
    acct_items = []    # 账户余额/保证金率需发送的命中（单独风控邮件）
    liveness_items = []  # 调度轮存活（主循环停滞/线程死亡）需发送的命中（单独风控邮件）
    error = None

    try:
        cfg = load_alert_config()
        if not cfg.get('enabled'):
            task_log.info(f'[{run_id}] 监控告警已关闭，本轮跳过')
            return

        state = _load_runtime_state()
        price_cfg, pnl_cfg, liq_cfg = (cfg.get('price', {}), cfg.get('pnl', {}),
                                       cfg.get('liq', {}))
        acct_cfg = cfg.get('acct', {})
        liveness_cfg = cfg.get('liveness', {})
        market_api = _get_market_api()
        # 持仓盈亏与强平距离共用同一份持仓数据，仅需一次查询
        positions = None
        if pnl_cfg.get('enabled') or liq_cfg.get('enabled'):
            try:
                positions = _fetch_positions(cfg.get('account', ''))
            except Exception as e:
                task_log.warning(f'[{run_id}] 持仓查询失败，盈亏/强平距离监控本轮跳过: {e}')
                positions = None

        # ---------- 行情异动检测 ----------
        if price_cfg.get('enabled'):
            window = max(1, int(price_cfg.get('window_minutes', 15)))
            for inst_id in _resolve_price_symbols(cfg):
                try:
                    change = _fetch_window_change_pct(market_api, inst_id, window)
                except Exception as e:
                    task_log.warning(f'[{run_id}] {inst_id} K线获取失败: {e}')
                    continue
                if change is None:
                    continue
                level = _classify_price_level(change, price_cfg)
                action, rec_level = _transition(state, f'price|{inst_id}', level)
                threshold = float(price_cfg.get('critical_pct', 8.0) if level == 'critical'
                                  else price_cfg.get('warning_pct', 5.0))
                if action == 'none':
                    continue
                if action == 'recover':
                    threshold = float(price_cfg.get('warning_pct', 5.0))
                message = f'{inst_id} 近{window}分钟涨跌幅 {change:+.2f}%'
                item = {'type': 'price', 'inst_id': inst_id, 'level': rec_level,
                        'value': change, 'threshold': threshold, 'message': message,
                        'sent': action in ('send', 'recover'),
                        'window_minutes': window}
                hits.append(item)
                if item['sent']:
                    notify_items.append(item)
                    task_log.info(f'[{run_id}] 【行情告警-{rec_level}】{message}')
                else:
                    task_log.info(f'[{run_id}] 【行情告警-抑制】{message}（级别未变化）')

        # ---------- 持仓盈亏检测 ----------
        if pnl_cfg.get('enabled'):
            if positions is not None:
                for pos in positions:
                    calc = _position_pnl_pct(pos)
                    if calc is None:
                        continue
                    pnl_pct, direction, avg_px, last_px = calc
                    inst_id = pos.get('instId', '')
                    key = f'pnl|{inst_id}|{direction}'
                    level = _classify_pnl_level(pnl_pct, pnl_cfg)
                    action, rec_level = _transition(state, key, level)
                    threshold = float(pnl_cfg.get('critical_pct', -20.0) if level == 'critical'
                                      else pnl_cfg.get('warning_pct', -10.0))
                    if action == 'none':
                        continue
                    if action == 'recover':
                        threshold = float(pnl_cfg.get('warning_pct', -10.0))
                    message = f'{inst_id} {direction}浮动盈亏 {pnl_pct:+.2f}%'
                    item = {'type': 'pnl', 'inst_id': inst_id, 'level': rec_level,
                            'value': pnl_pct, 'threshold': threshold, 'message': message,
                            'sent': action in ('send', 'recover'),
                            'direction': direction, 'avg_px': avg_px, 'last_px': last_px,
                            'mgn_mode': pos.get('mgnMode', '')}
                    hits.append(item)
                    if item['sent']:
                        notify_items.append(item)
                        task_log.info(f'[{run_id}] 【盈亏告警-{rec_level}】{message}')
                    else:
                        task_log.info(f'[{run_id}] 【盈亏告警-抑制】{message}（级别未变化）')

                    # ---------- 盈利侧提醒（与浮亏互斥，独立状态机键位）----------
                    # 用户诉求：定时检测到持仓盈利/暴涨也要提醒，阈值由配置决定；
                    # 单独邮件走正向配色，不与"亏损告警"摘要混在一起。
                    p_key = f'profit|{inst_id}|{direction}'
                    p_level = _classify_profit_level(pnl_pct, pnl_cfg)
                    p_action, p_rec = _transition(state, p_key, p_level)
                    if p_action != 'none':
                        p_thr = float(pnl_cfg.get('profit_critical_pct', 20.0)
                                      if p_level == 'critical'
                                      else pnl_cfg.get('profit_warning_pct', 10.0))
                        if p_action == 'recover':
                            p_thr = float(pnl_cfg.get('profit_warning_pct', 10.0))
                        p_msg = f'{inst_id} {direction}浮动盈利 {pnl_pct:+.2f}%'
                        p_item = {'type': 'profit', 'inst_id': inst_id, 'level': p_rec,
                                  'value': pnl_pct, 'threshold': p_thr, 'message': p_msg,
                                  'sent': p_action in ('send', 'recover'),
                                  'direction': direction, 'avg_px': avg_px, 'last_px': last_px,
                                  'mgn_mode': pos.get('mgnMode', '')}
                        hits.append(p_item)
                        if p_item['sent']:
                            profit_items.append(p_item)
                            task_log.info(f'[{run_id}] 【盈利提醒-{p_rec}】{p_msg}')
                        else:
                            task_log.info(f'[{run_id}] 【盈利提醒-抑制】{p_msg}（级别未变化）')

        # ---------- 强平距离检测（现价逼近强平价 = 爆仓风险）----------
        #     独立于盈亏维度：全仓合并头寸浮亏未必临仓，逐仓空头可能高杠杆先爆；
        #     用标记价与交易所回传的 liqPx 直接量出"还剩多少%到强平"。
        if liq_cfg.get('enabled') and positions is not None:
            for pos in positions:
                info = _position_liq_info(pos)
                if info is None:
                    continue
                dist_pct, liq_px, mark_px, direction = info
                inst_id = pos.get('instId', '')
                key = f'liq|{inst_id}|{direction}'
                level = _classify_liq_level(dist_pct, liq_cfg)
                action, rec_level = _transition(state, key, level)
                if action == 'none':
                    continue
                threshold = float(liq_cfg.get('critical_pct', 2.0) if level == 'critical'
                                  else liq_cfg.get('warning_pct', 5.0))
                if action == 'recover':
                    threshold = float(liq_cfg.get('warning_pct', 5.0))
                message = (f'{inst_id} {direction}距强平 {dist_pct:.2f}%'
                           f'（标记价{mark_px:.6g}/强平价{liq_px:.6g}）')
                item = {'type': 'liq', 'inst_id': inst_id, 'level': rec_level,
                        'value': dist_pct, 'threshold': threshold, 'message': message,
                        'sent': action in ('send', 'recover'), 'direction': direction,
                        'liq_px': liq_px, 'mark_px': mark_px,
                        'mgn_mode': pos.get('mgnMode', '')}
                hits.append(item)
                if item['sent']:
                    liq_items.append(item)
                    task_log.info(f'[{run_id}] 【强平距离-{rec_level}】{message}')
                else:
                    task_log.info(f'[{run_id}] 【强平距离-抑制】{message}（级别未变化）')

        # ---------- 账户余额 / 保证金率巡检（只读旁路，逐账户两项指标）----------
        #     保证金率临近 100% = 全仓即将连环强平；可用 USDT 过低 = 下一单 51008
        #     且无钱补保证金。二者各用独立状态机键位，级别递进与缓解同其他维度。
        if acct_cfg.get('enabled'):
            acct_label = cfg.get('account', '') or 'DEFAULT'
            try:
                risk = _fetch_account_risk(cfg.get('account', ''))
            except Exception as e:
                risk = None
                task_log.warning(f'[{run_id}] 账户余额/保证金查询失败，本轮跳过: {e}')
            if risk is not None:
                # 保证金率
                mr = risk.get('margin_ratio_pct')
                m_level = _classify_margin_level(mr, acct_cfg)
                m_action, m_rec = _transition(state, f'acct|margin|{acct_label}', m_level)
                if m_action != 'none':
                    m_thr = float(acct_cfg.get('margin_critical_pct', 120.0)
                                  if m_level == 'critical'
                                  else acct_cfg.get('margin_warning_pct', 200.0))
                    if m_action == 'recover':
                        m_thr = float(acct_cfg.get('margin_warning_pct', 200.0))
                    mr_disp = f'{mr:.1f}%' if mr is not None else '—'
                    m_msg = (f'账户 {acct_label} 保证金率 {mr_disp}'
                             f'（维持保证金{risk.get("maint_margin", 0):.2f}/调整权益'
                             f'{risk.get("adj_eq", 0):.2f}）')
                    m_item = {'type': 'acct', 'kind': 'margin', 'inst_id': f'acct:{acct_label}',
                              'level': m_rec, 'value': (mr if mr is not None else 0.0),
                              'threshold': m_thr, 'message': m_msg,
                              'sent': m_action in ('send', 'recover'), 'risk': risk}
                    hits.append(m_item)
                    if m_item['sent']:
                        acct_items.append(m_item)
                        task_log.info(f'[{run_id}] 【保证金率-{m_rec}】{m_msg}')
                    else:
                        task_log.info(f'[{run_id}] 【保证金率-抑制】{m_msg}（级别未变化）')
                # 可用余额
                av = risk.get('avail_usdt')
                a_level = _classify_avail_level(av, acct_cfg)
                a_action, a_rec = _transition(state, f'acct|avail|{acct_label}', a_level)
                if a_action != 'none':
                    a_thr = float(acct_cfg.get('min_avail_critical', 20.0)
                                  if a_level == 'critical'
                                  else acct_cfg.get('min_avail_warning', 100.0))
                    if a_action == 'recover':
                        a_thr = float(acct_cfg.get('min_avail_warning', 100.0))
                    av_disp = f'{av:.2f}' if av is not None else '—'
                    a_msg = f'账户 {acct_label} 可用余额 {av_disp} USDT'
                    a_item = {'type': 'acct', 'kind': 'avail', 'inst_id': f'acct:{acct_label}',
                              'level': a_rec, 'value': (av if av is not None else 0.0),
                              'threshold': a_thr, 'message': a_msg,
                              'sent': a_action in ('send', 'recover'), 'risk': risk}
                    hits.append(a_item)
                    if a_item['sent']:
                        acct_items.append(a_item)
                        task_log.info(f'[{run_id}] 【可用余额-{a_rec}】{a_msg}')
                    else:
                        task_log.info(f'[{run_id}] 【可用余额-抑制】{a_msg}（级别未变化）')

        # ---------- 调度轮存活检测（主循环停滞 / 线程意外死亡的旁路告警）----------
        #     主交易循环每轮开始刷新 last_execution['batch']；若在批次内卡死或线程被
        #     致命异常带走，时间戳停止推进 → now-ts 持续增长即检出；标志为真而线程已死
        #     = 交易停摆。只读旁路（懒加载同进程 scheduler 单例），绝不影响交易主流程。
        if liveness_cfg.get('enabled'):
            live = None
            try:
                from ..scheduler import task_scheduler as _ts
                live = _ts.get_loop_liveness()
            except Exception as e:
                task_log.warning(f'[{run_id}] 读取主循环存活状态失败，本轮跳过: {e}')
            if live is not None:
                _lbt = live.get('last_batch_ts')
                lag = (time.time() - float(_lbt)) if _lbt else None
                level = _classify_liveness_level(
                    live.get('thread_alive'), lag, live.get('running'),
                    liveness_cfg, live.get('interval_seconds', 60))
                action, rec_level = _transition(state, 'liveness', level)
                if action != 'none':
                    warn_thr, crit_thr = _liveness_thresholds(
                        liveness_cfg, live.get('interval_seconds', 60))
                    thr = (warn_thr if action == 'recover'
                           else (crit_thr if level == 'critical' else warn_thr))
                    l_msg = _liveness_message(live, lag)
                    l_item = {'type': 'liveness', 'inst_id': 'scheduler', 'level': rec_level,
                              'value': (lag if lag is not None else 0.0), 'threshold': thr,
                              'message': l_msg, 'sent': action in ('send', 'recover'),
                              'live': live, 'lag': lag}
                    hits.append(l_item)
                    if l_item['sent']:
                        liveness_items.append(l_item)
                        task_log.info(f'[{run_id}] 【调度轮存活-{rec_level}】{l_msg}')
                    else:
                        task_log.info(f'[{run_id}] 【调度轮存活-抑制】{l_msg}（级别未变化）')

        _save_runtime_state(state)
        _persist_alert_log(hits)
        _send_notifications(run_id, notify_items)
        _send_liq_notifications(run_id, liq_items)
        _send_profit_notifications(run_id, profit_items)
        _send_account_notifications(run_id, acct_items)
        _send_liveness_notifications(run_id, liveness_items)

        # 发信失败兜底：SMTP 恢复后补发此前落盘的死信（旁路线程每轮尝试，带节流，
        # 绝不碰 80s 交易主循环；本监控自身发送失败也会入死信，恢复后一并补投）
        try:
            _fl = MessageNotifier().flush_dead_letters()
            if _fl.get('flushed'):
                task_log.info(
                    f'[{run_id}] 死信补发成功 {_fl["flushed"]} 封，剩余积压 {_fl["pending"]}')
        except Exception as _fe:
            task_log.warning(f'[{run_id}] 死信补发异常（不影响告警）: {_fe}')
    except Exception as e:
        error = str(e)
        task_log.error(f'[{run_id}] 监控告警检测异常: {e}')

    _LAST_RUN = {
        'finished_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'duration_ms': int((datetime.datetime.now() - started).total_seconds() * 1000),
        'hit_count': len(hits),
        'sent_count': len(notify_items),
        'email_ok': _LAST_RUN.get('email_ok'),  # _send_notifications 已就地更新
        'error': error,
    }


def _persist_alert_log(hits: list):
    """命中记录旁路落库：失败只记日志，不阻断监控主循环"""
    if not hits or AlertLog is None or session_scope is None:
        return
    try:
        with session_scope() as s:
            for it in hits:
                s.add(AlertLog(
                    alert_type=it['type'], inst_id=it['inst_id'], level=it['level'],
                    metric_value=float(it['value']), threshold=float(it['threshold']),
                    message=it['message'], notify_sent=bool(it['sent'])))
    except Exception as e:
        task_log.warning(f'[AlertMonitor] alert_log 落库失败（不影响告警发送）: {e}')


def _new_notifier():
    """读取监控账号对应的交易模式；混合模式邮件按币种标注。"""
    notifier = MessageNotifier()
    try:
        account = load_alert_config().get('account')
        cfg = (config_store_repo.load_strategy_config_cached(account) if account
               else config_store_repo.load_live_strategy_config_cached()) or {}
        currencies = cfg.get('currencies', [])
        watched = {c['instId'] for c in currencies
                   if c.get('instId') and not c.get('trade_enabled', True)}
        all_watched = bool(currencies) and len(watched) == len(currencies)
        notifier.watch_mode_check = lambda inst: inst in watched if inst else all_watched
    except Exception as e:
        task_log.warning(f'监控邮件读取观察模式失败: {e}')
    return notifier


def _send_notifications(run_id: str, notify_items: list):
    """本轮需发送的命中：单条发专用邮件，多条合并为一封摘要（防轰炸）"""
    if not notify_items:
        _LAST_RUN['email_ok'] = None
        return
    try:
        notifier = _new_notifier()
        if len(notify_items) == 1:
            it = notify_items[0]
            if it['type'] == 'price':
                ok = notifier.send_price_volatility_alert(
                    it['inst_id'], it['value'], it.get('window_minutes', 15), it['level'])
            else:
                ok = notifier.send_position_pnl_alert(
                    it['inst_id'], {'cross': '全仓', 'isolated': '逐仓'}.get(it.get('mgn_mode'), ''),
                    it.get('direction', ''), it['value'],
                    it.get('avg_px', 0), it.get('last_px', 0), it['level'])
        else:
            ok = notifier.send_alert_digest([
                {'type': it['type'], 'level': it['level'], 'inst_id': it['inst_id'],
                 'value': it['value'], 'threshold': it['threshold'], 'message': it['message']}
                for it in notify_items])
        _LAST_RUN['email_ok'] = bool(ok)
        task_log.info(f'[{run_id}] 告警邮件{"发送成功" if ok else "发送失败"}'
                      f'（{len(notify_items)} 项命中）')
    except Exception as e:
        _LAST_RUN['email_ok'] = False
        task_log.error(f'[{run_id}] 告警邮件发送异常: {e}')


def _send_liq_notifications(run_id: str, liq_items: list):
    """强平距离告警逐条发专用邮件（爆仓风险是叫醒级，不与行情/盈亏合并摘要）。

    冷却由级别递进状态机控制：同级别持续命中已被抑制，这里只收到"新命中/升级/缓解"。
    失败仅记日志并置 email_ok=False，不阻断监控主循环。
    """
    if not liq_items:
        return
    try:
        notifier = _new_notifier()
        ok_all = True
        for it in liq_items:
            ok = notifier.send_liq_distance_alert(
                it['inst_id'], it['value'], it['liq_px'], it['mark_px'],
                it.get('direction', ''), it['level'],
                mgn_mode=it.get('mgn_mode', ''))
            ok_all = ok_all and bool(ok)
            task_log.info(f'[{run_id}] 强平距离邮件{"发送成功" if ok else "发送失败"}'
                          f'：{it["message"]}')
        if not ok_all:
            _LAST_RUN['email_ok'] = False
    except Exception as e:
        _LAST_RUN['email_ok'] = False
        task_log.error(f'[{run_id}] 强平距离告警邮件发送异常: {e}')


def _send_profit_notifications(run_id: str, profit_items: list):
    """盈利侧提醒逐条发专用邮件（正向配色，不并入亏损摘要）。

    冷却由级别递进状态机控制：同级持续盈利已抑制，这里只收到"新命中/升级/回落"。
    失败仅记日志并置 email_ok=False，不阻断监控主循环。
    """
    if not profit_items:
        return
    try:
        notifier = _new_notifier()
        ok_all = True
        for it in profit_items:
            ok = notifier.send_profit_alert(
                it['inst_id'], it.get('direction', ''), it['value'],
                it.get('avg_px', 0), it.get('last_px', 0), it['level'],
                mgn_mode=it.get('mgn_mode', ''))
            ok_all = ok_all and bool(ok)
            task_log.info(f'[{run_id}] 盈利提醒邮件{"发送成功" if ok else "发送失败"}'
                          f'：{it["message"]}')
        if not ok_all:
            _LAST_RUN['email_ok'] = False
    except Exception as e:
        _LAST_RUN['email_ok'] = False
        task_log.error(f'[{run_id}] 盈利提醒邮件发送异常: {e}')


def _send_account_notifications(run_id: str, acct_items: list):
    """账户余额/保证金率风险逐条发风控邮件（复用 send_risk_alert 结构化模板）。

    冷却由级别递进状态机控制：同级别持续命中已抑制，这里只收到"新命中/升级/缓解"。
    失败仅记日志并置 email_ok=False，不阻断监控主循环。
    """
    if not acct_items:
        return
    try:
        notifier = _new_notifier()
        ok_all = True
        for it in acct_items:
            risk = it.get('risk') or {}
            if it.get('kind') == 'margin':
                mr = risk.get('margin_ratio_pct')
                mr_disp = f'{mr:.1f}%' if mr is not None else '—'
                title = '保证金率已回升，风险缓解' if it['level'] == 'recover' else '保证金率临近强平'
                rows = [('保证金率', mr_disp),
                        ('调整权益(adjEq)', f"{risk.get('adj_eq', 0):.2f}"),
                        ('维持保证金(maint)', f"{risk.get('maint_margin', 0):.2f}"),
                        ('触发级别', it['level']),
                        ('说明', '保证金率=调整权益/维持保证金，越接近100%越接近全仓强平；'
                             '若持续下降请立即减仓或追加保证金。')]
            else:
                av = risk.get('avail_usdt')
                av_disp = f'{av:.2f} USDT' if av is not None else '—'
                title = '可用余额已回升' if it['level'] == 'recover' else '可用余额过低'
                rows = [('可用余额', av_disp),
                        ('阈值', f"{it.get('threshold', 0):g}"),
                        ('调整权益(adjEq)', f"{risk.get('adj_eq', 0):.2f}"),
                        ('说明', '可用 USDT 过低会导致下一单 51008 被拒、也无余量追加保证金；'
                             '建议结算部分挂单冻结或入金。')]
            ok = notifier.send_risk_alert(it['inst_id'], title, detail_rows=rows,
                                          level=it['level'])
            ok_all = ok_all and bool(ok)
            task_log.info(f'[{run_id}] 账户风险邮件{"发送成功" if ok else "发送失败"}'
                          f'：{it["message"]}')
        if not ok_all:
            _LAST_RUN['email_ok'] = False
    except Exception as e:
        _LAST_RUN['email_ok'] = False
        task_log.error(f'[{run_id}] 账户风险告警邮件发送异常: {e}')


def _liveness_message(live: dict, lag) -> str:
    """调度轮存活告警文本：线程死亡 / 轮次停滞分别描述（供日志与 alert_log 展示）。"""
    if not live.get('thread_alive'):
        return '实盘交易线程已停止（调度器仍标记运行中，交易实际已停摆）'
    interval = int(live.get('interval_seconds', 60) or 60)
    if lag is None:
        return '实盘交易主循环尚未产生可判定的轮次'
    return f'实盘交易主循环已停滞 {lag/60:.1f} 分钟无新轮次（设定间隔 {interval}s）'


def _send_liveness_notifications(run_id: str, liveness_items: list):
    """调度轮存活告警逐条发风控邮件（复用 send_risk_alert 结构化模板，叫醒级）。

    冷却由级别递进状态机控制：同级别持续停滞已抑制，这里只收到"新命中/升级/缓解"。
    失败仅记日志并置 email_ok=False，不阻断监控主循环。
    """
    if not liveness_items:
        return
    try:
        notifier = _new_notifier()
        ok_all = True
        for it in liveness_items:
            live = it.get('live') or {}
            lag = it.get('lag')
            thread_alive = bool(live.get('thread_alive'))
            interval = int(live.get('interval_seconds', 60) or 60)
            if it['level'] == 'recover':
                title = '实盘交易主循环已恢复推进'
            elif not thread_alive:
                title = '实盘交易线程意外停止'
            else:
                title = '实盘交易主循环停滞'
            lag_disp = f'{lag/60:.1f} 分钟' if lag is not None else '—'
            rows = [('交易线程存活', '是' if thread_alive else '否'),
                    ('最近轮次距今', lag_disp),
                    ('设定执行间隔', f'{interval}s'),
                    ('触发级别', it['level']),
                    ('说明', '主循环每轮开始会刷新轮次时间戳；停滞或线程死亡通常意味着'
                          '批次内卡死（网络/锁）或线程被异常带走。请查看实盘日志，'
                          '必要时在运维页手动重启实盘调度器。')]
            ok = notifier.send_risk_alert(it['inst_id'], title, detail_rows=rows,
                                          level=it['level'])
            ok_all = ok_all and bool(ok)
            task_log.info(f'[{run_id}] 调度轮存活邮件{"发送成功" if ok else "发送失败"}'
                          f'：{it["message"]}')
        if not ok_all:
            _LAST_RUN['email_ok'] = False
    except Exception as e:
        _LAST_RUN['email_ok'] = False
        task_log.error(f'[{run_id}] 调度轮存活告警邮件发送异常: {e}')


# =============================================================================
# 调度注册（scheduler.register_default_jobs 与 alert_routes 配置保存共用）
# =============================================================================

def register_alert_job() -> int:
    """按当前配置注册/热重注册监控任务，返回检测周期（秒）。

    关闭监控时移除已注册任务。APScheduler replace_existing 保证热重注册幂等。
    """
    from ..scheduler import task_scheduler

    cfg = load_alert_config()
    interval = max(30, int(cfg.get('interval_seconds', 300)))
    if cfg.get('enabled'):
        task_scheduler.register_job(
            run_alert_check, trigger='interval', seconds=interval,
            job_id=ALERT_JOB_ID, job_name='监控告警（行情异动/持仓盈亏）')
    else:
        task_scheduler.remove_job(ALERT_JOB_ID)
    return interval
