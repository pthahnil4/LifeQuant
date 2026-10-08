#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 全市场涨跌 / 成交量排行 + 趋势识别扫描器
===========================================
用途：从主流震荡行情中，快速筛出「正在形成明确趋势」的币种。

核心思路
--------
1. 一次 get_tickers(instType='SWAP') 拉全部永续合约行情（几百个，单次请求）。
2. 用 (last-open24h)/open24h 计算 24H 涨跌幅，volCcy24h*last 估算 USDT 成交额。
3. 排序生成三大榜单：涨幅榜 / 跌幅榜 / 成交额榜。
4. 对流动性达标的候选，再拉日线 K 线，计算：
   - 7 日涨跌幅
   - Kaufman 效率系数 (ER)：|净变动| / Σ|每根变动|
     ER→1 代表单边趋势；ER→0 代表来回震荡。这是区分「趋势」与「震荡」的关键。
5. 综合流动性 + 幅度 + ER，输出「明确趋势币种」推荐。

依赖：复用项目根目录 api_config.py（OKX 密钥/环境统一管理）。
运行：python crypto/market_scanner.py
"""

import os
import sys
import time
import json
import datetime

# 确保能 import 到 crypto 目录下的 api_config
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

# Windows 控制台默认 GBK，无法输出 emoji，这里强制 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

import httpx
import okx.MarketData as MarketData
from api_config import get_api_config

# 只读接口限频/退避（问题#8）：扫描轮会一个币种一个币种地拉日线，
# 与同进程的实盘调度、页刷新共用一个 API key，是 50011 的主要制造者。
# 导入失败时置 None，_rl() 退化为直连，绝不因为限频模块不可用而中断行情。
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


# MarketAPI 方法名 → 限频分组（行情类接口分开计量）
_MARKET_RL_GROUP = {
    'get_ticker': 'market_ticker',
    'get_tickers': 'market_ticker',
}


# =============================================================================
# 可调参数
# =============================================================================
INST_TYPE = "SWAP"          # SWAP=永续合约（流动性最好，也是本项目交易标的）

MIN_TURNOVER_USDT = 3e7     # 流动性门槛：24H 估算成交额 ≥ 3000万 USDT 才纳入趋势分析
CANDIDATE_LIMIT = 60        # 只对幅度最大的前 N 个候选拉日线（控制 API 调用量）
DAILY_LOOKBACK = 10         # 拉取的日线根数（用于 7 日涨跌 + ER 计算）

# 趋势分级阈值（基于日线 ER 与 7 日幅度）
ER_STRONG = 0.40            # ER ≥ 0.40 → 强趋势（单边行情）
ER_MILD = 0.25             # 0.25 ≤ ER < 0.40 → 温和趋势
CHG7D_MIN = 8.0             # 7 日绝对涨跌幅 ≥ 8% 才认为「幅度足够」

TOP_N = 15                  # 各榜单展示条数


# =============================================================================
# OKX 客户端
# =============================================================================
def _make_market_api(flag):
    """新建 OKX 市场数据客户端并设置显式超时。

    每次调用都新建客户端，避免复用已断开的长连接（TLS 握手/读取超时）。
    这与 pro3_singletimeframe._fetch_kline_data 的健壮模式保持一致。
    """
    api = MarketData.MarketAPI(flag=flag)
    try:
        api.timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
    except Exception:
        pass
    return api


def get_market_api(account=None):
    """复用项目 api_config，返回 OKX 市场数据 API 实例（带超时）。"""
    config = get_api_config(account)
    return _make_market_api(config['flag'])


def _robust_market_call(flag, method, max_retries=3, **kwargs):
    """带超时 + 自动重试地调用 MarketAPI 方法。

    网络/传输层异常（TLS 握手超时 ConnectTimeout、读取超时、连接重置、
    代理抖动等）时，每次重试都新建客户端（避免复用已断开的长连接），
    最多重试 max_retries 次；均失败则抛出最后一次异常。

    Args:
        flag:   OKX 环境标识（'0' 实盘 / '1' 模拟盘）。
        method: MarketAPI 的方法名字符串，如 'get_tickers'、'get_candlesticks'。
        kwargs: 传给该方法的关键字参数。
    """
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            api = _make_market_api(flag)
            # 本函数已自带 3 次「新建客户端 + 异常重试」，这里只取节流与
            # 50011 退避（retry_on_error=False），不叠加两层网络重试
            return _rl(_MARKET_RL_GROUP.get(method, 'market_candles'),
                       getattr(api, method), retry_on_error=False, **kwargs)
        except Exception as e:
            last_err = e
            print(f"[MarketScan] {method} 获取失败(第{attempt}/{max_retries}次) "
                  f"{type(e).__name__}: {e}")
            if attempt < max_retries:
                time.sleep(min(1.0 * attempt, 3))
    raise last_err


# =============================================================================
# 1. 拉全市场行情 + 计算基础指标
# =============================================================================
def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_all_tickers(flag, inst_type=INST_TYPE):
    """
    获取指定类型全部行情，并计算派生指标。

    Args:
        flag: OKX 环境标识（由 api_config 提供）。

    Returns:
        list[dict]: 每项含 instId/last/change_24h/amplitude/range_pos/turnover_usdt
    """
    result = _robust_market_call(flag, 'get_tickers', instType=inst_type)
    if result.get('code') != '0' or not result.get('data'):
        raise RuntimeError(f"get_tickers 失败: {result.get('msg', '未知错误')}")

    tickers = []
    for t in result['data']:
        inst_id = t.get('instId', '')
        # 只保留 USDT 本位合约，剔除 USDC / 币本位，保证成交额口径一致
        if not inst_id.endswith('-USDT-SWAP'):
            continue

        last = _to_float(t.get('last'))
        open24h = _to_float(t.get('open24h'))
        high24h = _to_float(t.get('high24h'))
        low24h = _to_float(t.get('low24h'))
        vol_ccy24h = _to_float(t.get('volCcy24h'))  # 24H 成交量(以币计)

        if last <= 0 or open24h <= 0:
            continue

        change_24h = (last - open24h) / open24h * 100.0
        amplitude = (high24h - low24h) / open24h * 100.0 if open24h else 0.0
        # 收盘价在 24H 高低区间的相对位置：接近 1=收在高位(强势)，接近 0=收在低位(弱势)
        range_pos = (last - low24h) / (high24h - low24h) if high24h > low24h else 0.5
        # 估算 USDT 成交额（币量 * 现价），作为跨币可比的流动性/热度指标
        turnover_usdt = vol_ccy24h * last

        tickers.append({
            'instId': inst_id,
            'symbol': inst_id.replace('-USDT-SWAP', ''),
            'last': last,
            'change_24h': change_24h,
            'amplitude': amplitude,
            'range_pos': range_pos,
            'turnover_usdt': turnover_usdt,
        })

    return tickers


# =============================================================================
# 2. 日线趋势指标：7 日涨跌 + Kaufman 效率系数
# =============================================================================
def kaufman_efficiency_ratio(closes):
    """
    Kaufman 效率系数：|净变动| / Σ|逐根变动|。
    closes 需按时间升序（旧→新）。返回 0~1，越大越单边。
    """
    if len(closes) < 2:
        return 0.0
    net = abs(closes[-1] - closes[0])
    noise = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if noise == 0:
        return 0.0
    return net / noise


def fetch_daily_trend(flag, inst_id, lookback=DAILY_LOOKBACK):
    """
    拉日线，计算 7 日涨跌幅与效率系数。

    单个币种彻底失败（重试后仍超时/报错）时返回 None，让整体扫描继续，
    不因个别合约的网络问题而中断整轮扫描。

    Returns:
        dict | None: {chg_7d, er, closes_n}
    """
    try:
        result = _robust_market_call(flag, 'get_candlesticks',
                                     instId=inst_id, bar='1D', limit=str(lookback + 2))
    except Exception:
        return None
    if result.get('code') != '0' or not result.get('data'):
        return None

    # OKX 返回 [ts, o, h, l, c, vol, ...]，最新在前 → 反转为旧→新
    rows = result['data'][::-1]
    closes = [_to_float(r[4]) for r in rows if _to_float(r[4]) > 0]
    if len(closes) < 3:
        return None

    # 7 日涨跌：以最近 8 根收盘价（含今日）为基准，不足则用现有最早一根
    base_idx = max(0, len(closes) - 8)
    chg_7d = (closes[-1] - closes[base_idx]) / closes[base_idx] * 100.0

    er = kaufman_efficiency_ratio(closes[-min(len(closes), 8):])

    return {'chg_7d': chg_7d, 'er': er, 'closes_n': len(closes)}


def classify_trend(chg_7d, er):
    """
    根据 7 日幅度 + ER 分级。
    Returns: (label, direction)  label ∈ {强趋势,温和趋势,震荡}
    """
    direction = '上涨' if chg_7d > 0 else '下跌'
    if er >= ER_STRONG and abs(chg_7d) >= CHG7D_MIN:
        return '强趋势', direction
    if er >= ER_MILD and abs(chg_7d) >= CHG7D_MIN * 0.6:
        return '温和趋势', direction
    return '震荡', direction


# =============================================================================
# 3. 榜单生成
# =============================================================================
def build_rankings(tickers, top_n=TOP_N):
    """按涨幅 / 跌幅 / 成交额生成三大榜单。"""
    gainers = sorted(tickers, key=lambda x: x['change_24h'], reverse=True)[:top_n]
    losers = sorted(tickers, key=lambda x: x['change_24h'])[:top_n]
    by_volume = sorted(tickers, key=lambda x: x['turnover_usdt'], reverse=True)[:top_n]
    return {'gainers': gainers, 'losers': losers, 'by_volume': by_volume}


def scan_trends(flag, tickers, extra_instids=None, progress_cb=None):
    """
    对流动性达标 + 幅度最大的候选做日线趋势分析。

    Args:
        extra_instids: 额外强制纳入分析的合约集合（如成交额榜），
                       保证这些卡片也能拿到 7 日涨跌 / ER 数据。
        progress_cb:   进度回调 callback(current=int, total=int, symbol=str)。

    Returns:
        list[dict]: tickers 子集，附加 chg_7d/er/trend_label/trend_dir，并按趋势强度排序
    """
    # 流动性过滤
    liquid = [t for t in tickers if t['turnover_usdt'] >= MIN_TURNOVER_USDT]
    # 取 24H 幅度（绝对涨跌）最大的前 N 个做候选，减少 API 调用
    candidates = sorted(liquid, key=lambda x: abs(x['change_24h']), reverse=True)[:CANDIDATE_LIMIT]

    # 强制纳入指定合约（成交额榜龙头往往 24H 涨跌不大，不会进上面的候选）
    if extra_instids:
        have = {t['instId'] for t in candidates}
        by_id = {t['instId']: t for t in tickers}
        for iid in extra_instids:
            if iid not in have and iid in by_id:
                candidates.append(by_id[iid])

    total = len(candidates)
    analyzed = []
    for i, t in enumerate(candidates):
        if progress_cb:
            progress_cb(current=i + 1, total=total, symbol=t['symbol'])
        trend = fetch_daily_trend(flag, t['instId'])
        if not trend:
            continue
        label, direction = classify_trend(trend['chg_7d'], trend['er'])
        row = dict(t)
        row.update({
            'chg_7d': trend['chg_7d'],
            'er': trend['er'],
            'trend_label': label,
            'trend_dir': direction,
        })
        analyzed.append(row)
        time.sleep(0.06)  # 轻微限速，避免触发频控

    # 趋势强度排序：先按 label 权重，再按 ER
    weight = {'强趋势': 2, '温和趋势': 1, '震荡': 0}
    analyzed.sort(key=lambda x: (weight[x['trend_label']], x['er']), reverse=True)
    return analyzed


# =============================================================================
# 4. 打印
# =============================================================================
def _fmt_usdt(v):
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"


def _print_rank_table(title, rows):
    print(f"\n{title}")
    print("-" * 78)
    print(f"{'#':<3}{'币种':<12}{'现价':>14}{'24H涨跌':>12}{'振幅':>10}{'24H成交额':>14}")
    print("-" * 78)
    for i, r in enumerate(rows, 1):
        chg = r['change_24h']
        arrow = '🟢+' if chg > 0 else ('🔴' if chg < 0 else '⚪')
        print(f"{i:<3}{r['symbol']:<12}{r['last']:>14.6g}"
              f"{arrow + f'{chg:.2f}%':>13}{r['amplitude']:>9.1f}%"
              f"{_fmt_usdt(r['turnover_usdt']):>14}")


def _print_trend_table(analyzed):
    trending = [r for r in analyzed if r['trend_label'] != '震荡']
    print("\n" + "=" * 78)
    print("🎯 明确趋势币种（已剔除窄幅震荡） —— 建议重点关注")
    print("=" * 78)
    if not trending:
        print("   当前候选中没有满足强/温和趋势标准的币种（市场整体偏震荡）。")
    else:
        print(f"{'#':<3}{'币种':<10}{'趋势':<12}{'方向':<6}"
              f"{'7日涨跌':>11}{'24H涨跌':>11}{'ER':>7}{'成交额':>12}")
        print("-" * 78)
        for i, r in enumerate(trending, 1):
            tag = ('🚀' if r['trend_label'] == '强趋势' else '📈')
            print(f"{i:<3}{r['symbol']:<10}{tag + r['trend_label']:<12}{r['trend_dir']:<6}"
                  f"{r['chg_7d']:>+10.2f}%{r['change_24h']:>+10.2f}%"
                  f"{r['er']:>7.2f}{_fmt_usdt(r['turnover_usdt']):>12}")

    ranging = [r for r in analyzed if r['trend_label'] == '震荡']
    print(f"\n   ⚪ 被判定为震荡而剔除的候选：{len(ranging)} 个"
          f"（ER<{ER_MILD} 或 7日幅度不足）")


def print_report(rankings, analyzed, flag):
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    env = '实盘' if flag == '0' else '模拟盘'
    print("=" * 78)
    print(f"📊 OKX 全市场趋势扫描  |  {env}  |  {now}")
    print(f"   合约类型：{INST_TYPE}(USDT本位)  |  流动性门槛：≥{_fmt_usdt(MIN_TURNOVER_USDT)} USDT")
    print("=" * 78)

    _print_rank_table("📈 榜单一：24H 涨幅榜 TOP", rankings['gainers'])
    _print_rank_table("📉 榜单二：24H 跌幅榜 TOP", rankings['losers'])
    _print_rank_table("🔄 榜单三：24H 成交额榜 TOP", rankings['by_volume'])
    _print_trend_table(analyzed)

    print("\n" + "=" * 78)
    print("💡 使用说明")
    print("-" * 78)
    print("  • ER(效率系数)：|净变动|/Σ|逐日变动|，越接近1越单边，越接近0越震荡。")
    print(f"  • 强趋势：ER≥{ER_STRONG} 且 7日幅度≥{CHG7D_MIN}%；温和趋势：ER≥{ER_MILD}。")
    print("  • range_pos(未列出)可辅助判断：收盘接近24H高点=强势，接近低点=弱势。")
    print("  • 榜单仅供选标的，实际入场请结合本项目 Pro3/BOLL 策略信号二次确认。")
    print("=" * 78)


# =============================================================================
# 对外接口（供 Flask / 其他模块调用）
# =============================================================================
def get_trend_scan(account=None):
    """
    返回结构化扫描结果，便于接入 Web 接口或 CSV。

    Returns:
        dict: {rankings, trending, ranging, generated_at}
    """
    config = get_api_config(account)
    flag = config['flag']
    tickers = fetch_all_tickers(flag)
    rankings = build_rankings(tickers)
    analyzed = scan_trends(flag, tickers)
    return {
        'rankings': rankings,
        'trending': [r for r in analyzed if r['trend_label'] != '震荡'],
        'ranging': [r for r in analyzed if r['trend_label'] == '震荡'],
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


# =============================================================================
# Web 集成：卡片结构化 + 后台扫描 + 进度/结果缓存
# =============================================================================
import threading


def _card_from_row(r, trend=None):
    """将行情行（+可选趋势）转为前端卡片 dict（字段与需求对齐）。"""
    card = {
        'symbol': r['symbol'],
        'instId': r['instId'],
        'last': r['last'],
        'change_24h': round(r['change_24h'], 2),
        'amplitude': round(r['amplitude'], 2),
        'range_pos': round(r['range_pos'], 2),
        'turnover_usdt': r['turnover_usdt'],
        'turnover_text': _fmt_usdt(r['turnover_usdt']),
        'chg_7d': None,
        'er': None,
        'trend_label': None,
        'trend_dir': None,
    }
    # trend 可能就是 r 本身（已 update 过），也可能是 analyzed_map 里的行
    src = trend if trend is not None else {}
    if src.get('trend_label') is not None:
        card['chg_7d'] = round(src['chg_7d'], 2)
        card['er'] = round(src['er'], 3)
        card['trend_label'] = src['trend_label']
        card['trend_dir'] = src['trend_dir']
    return card


def get_market_scan(account=None, progress_cb=None):
    """
    一次扫描，返回前端可直接渲染的完整结构。

    与 get_trend_scan 的区别：
      - 三大榜单的每一行都会尝试补齐 7日涨跌/ER/趋势分类（若已分析）；
      - 成交额榜龙头会被强制纳入趋势分析，保证卡片数据完整。

    Returns:
        dict: 供 /api/market-scan/data 直接返回
    """
    config = get_api_config(account)
    flag = config['flag']

    if progress_cb:
        progress_cb(stage='tickers', message='正在拉全市场行情...')
    tickers = fetch_all_tickers(flag)
    rankings = build_rankings(tickers)

    # 强制把成交额榜纳入趋势候选，确保成交额卡片也有 7日/ER
    vol_ids = {t['instId'] for t in rankings['by_volume']}

    if progress_cb:
        progress_cb(stage='trend', message='正在做日线趋势分析...')
    analyzed = scan_trends(flag, tickers, extra_instids=vol_ids, progress_cb=progress_cb)
    amap = {r['instId']: r for r in analyzed}

    def _cards(rows):
        return [_card_from_row(r, amap.get(r['instId'])) for r in rows]

    trending = [_card_from_row(r, r) for r in analyzed if r['trend_label'] != '震荡']

    return {
        'rankings': {
            'gainers': _cards(rankings['gainers']),
            'losers': _cards(rankings['losers']),
            'by_volume': _cards(rankings['by_volume']),
        },
        'trending': trending,
        'ranging_count': sum(1 for r in analyzed if r['trend_label'] == '震荡'),
        'analyzed_count': len(analyzed),
        'ticker_count': len(tickers),
        'env': '实盘' if flag == '0' else '模拟盘',
        'thresholds': {
            'er_strong': ER_STRONG,
            'er_mild': ER_MILD,
            'chg7d_min': CHG7D_MIN,
            'min_turnover_text': _fmt_usdt(MIN_TURNOVER_USDT),
        },
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


class _ScanProgress:
    """线程安全的扫描进度状态。"""
    def __init__(self):
        self._lock = threading.Lock()
        self.status = 'idle'      # idle / running / completed / error
        self.current = 0
        self.total = 0
        self.symbol = ''
        self.message = ''
        self.start_time = None
        self.end_time = None

    def start(self):
        with self._lock:
            self.status = 'running'
            self.current = 0
            self.total = 0
            self.symbol = ''
            self.message = '扫描任务启动中...'
            self.start_time = datetime.datetime.now()
            self.end_time = None

    def update(self, current=None, total=None, symbol=None, stage=None, message=None):
        with self._lock:
            if current is not None:
                self.current = current
            if total is not None:
                self.total = total
            if symbol is not None:
                self.symbol = symbol
            if message is not None:
                self.message = message
            elif stage == 'tickers':
                self.message = '正在拉全市场行情...'
            elif stage == 'trend' and self.total:
                self.message = f'日线趋势分析 {self.current}/{self.total} ({self.symbol})...'

    def finish(self):
        with self._lock:
            self.status = 'completed'
            self.end_time = datetime.datetime.now()
            elapsed = (self.end_time - self.start_time).total_seconds() if self.start_time else 0
            self.message = f'扫描完成，耗时 {elapsed:.1f} 秒'

    def set_error(self, msg):
        with self._lock:
            self.status = 'error'
            self.end_time = datetime.datetime.now()
            self.message = f'扫描失败: {msg}'

    def to_dict(self):
        with self._lock:
            elapsed = 0
            if self.start_time:
                end = self.end_time or datetime.datetime.now()
                elapsed = (end - self.start_time).total_seconds()
            pct = round(self.current / self.total * 100, 1) if self.total > 0 else 0
            return {
                'status': self.status,
                'current': self.current,
                'total': self.total,
                'symbol': self.symbol,
                'message': self.message,
                'progress_pct': pct,
                'elapsed_seconds': round(elapsed, 1),
            }


_scan_progress = _ScanProgress()
_scan_result = None
_scan_lock = threading.Lock()

# 扫描结果持久化文件：Flask 重启后仍可加载上次榜单（缓存优先，不自动分析）
# 外置到统一数据目录（data_paths.py，四级优先级解析）；不可用时回退项目内
try:
    from data_paths import resolve_data_file
except ImportError:
    def resolve_data_file(filename, legacy_path=None):
        return legacy_path

_SCAN_CACHE_FILE = resolve_data_file(
    'market_scan_cache.json', os.path.join(_THIS_DIR, 'market_scan_cache.json'))

# 扫描结果缓存已迁移 MySQL（迁移批次8，kv_store key='market_scan_cache'）：
# 读 DB 优先、不可用/无数据时回退磁盘文件；写 DB 主存 + 磁盘双写。
# 双模式导入：Flask 包内（cryptoTrade 根在 sys.path）/ 独立脚本（仅 crypto 目录）
try:
    from crypto.database import session_scope as _db_session_scope
    from crypto import config_store_repo as _config_store_repo
except ImportError:
    _db_session_scope = None
    _config_store_repo = None


def _load_scan_cache():
    """进程启动时加载上次扫描结果到内存：迁移批次8 起 DB 优先，回退磁盘文件。"""
    global _scan_result
    if _db_session_scope is not None and _config_store_repo is not None:
        try:
            with _db_session_scope() as s:
                data = _config_store_repo.load_json_config(
                    s, _config_store_repo.KEY_MARKET_SCAN_CACHE)
            if isinstance(data, dict) and data:
                _scan_result = data
                return
        except Exception as e:
            print(f"[MarketScan] 加载DB缓存失败，回退磁盘文件: {type(e).__name__}: {e}")
    try:
        if os.path.exists(_SCAN_CACHE_FILE):
            with open(_SCAN_CACHE_FILE, 'r', encoding='utf-8') as f:
                _scan_result = json.load(f)
    except Exception as e:
        print(f"[MarketScan] 加载缓存失败: {type(e).__name__}: {e}")
        _scan_result = None


def _save_scan_cache(result):
    """将扫描结果持久化：DB 主存 + 磁盘文件双写（文件保留为兜底数据源）。"""
    if _db_session_scope is not None and _config_store_repo is not None:
        try:
            with _db_session_scope() as s:
                _config_store_repo.save_json_config(
                    s, _config_store_repo.KEY_MARKET_SCAN_CACHE, result)
        except Exception as e:
            print(f"[MarketScan] 保存DB缓存失败: {type(e).__name__}: {e}")
    try:
        with open(_SCAN_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        print(f"[MarketScan] 保存缓存失败: {type(e).__name__}: {e}")


def get_scan_progress():
    """获取当前扫描进度（供前端轮询）。"""
    return _scan_progress.to_dict()


def get_scan_result():
    """获取最近一次扫描结果（尚未扫描则返回 None）。"""
    with _scan_lock:
        return _scan_result


def set_scan_result(result):
    """更新内存缓存并持久化到磁盘（供同步接口如「直接获取」使用）。"""
    global _scan_result
    with _scan_lock:
        _scan_result = result
    _save_scan_cache(result)


def get_scan_thresholds():
    """返回当前趋势扫描的筛选参数，供前端「筛选条件」弹窗展示。"""
    return {
        'min_turnover_usdt': MIN_TURNOVER_USDT,
        'min_turnover_text': _fmt_usdt(MIN_TURNOVER_USDT),
        'candidate_limit': CANDIDATE_LIMIT,
        'daily_lookback': DAILY_LOOKBACK,
        'er_strong': ER_STRONG,
        'er_mild': ER_MILD,
        'chg7d_min': CHG7D_MIN,
        'top_n': TOP_N,
    }


def get_market_rankings(account=None):
    """仅拉全市场行情并生成三大榜单（涨幅/跌幅/成交额），不做日线趋势分析。

    与 get_market_scan 的区别：只发 1 次 get_tickers 请求 + 内存排序，速度快；
    不对候选逐个拉日线，因此 trending 为空、卡片无 ER/趋势字段。
    返回结构与 get_market_scan 对齐，便于前端复用同一套渲染逻辑。
    """
    config = get_api_config(account)
    flag = config['flag']
    tickers = fetch_all_tickers(flag)
    rankings = build_rankings(tickers)

    def _cards(rows):
        return [_card_from_row(r, None) for r in rows]

    return {
        'rankings': {
            'gainers': _cards(rankings['gainers']),
            'losers': _cards(rankings['losers']),
            'by_volume': _cards(rankings['by_volume']),
        },
        'trending': [],
        'ranging_count': 0,
        'analyzed_count': 0,
        'ticker_count': len(tickers),
        'env': '实盘' if flag == '0' else '模拟盘',
        'scan_mode': 'rankings',  # 标记：仅榜单模式（未做趋势分析）
        'thresholds': {
            'er_strong': ER_STRONG,
            'er_mild': ER_MILD,
            'chg7d_min': CHG7D_MIN,
            'min_turnover_text': _fmt_usdt(MIN_TURNOVER_USDT),
        },
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def _scan_worker(account):
    global _scan_result

    def _cb(current=None, total=None, symbol=None, stage=None, message=None):
        _scan_progress.update(current=current, total=total, symbol=symbol,
                              stage=stage, message=message)

    try:
        result = get_market_scan(account, progress_cb=_cb)
        with _scan_lock:
            _scan_result = result
        _save_scan_cache(result)
        _scan_progress.finish()
    except Exception as e:
        _scan_progress.set_error(str(e))


def run_scan_background(account=None):
    """启动后台扫描线程并立即返回。若已有任务在跑则不重复启动。"""
    with _scan_lock:
        if _scan_progress.status == 'running':
            return {'status': 'running', 'message': '已有扫描任务正在运行中'}
        _scan_progress.start()

    thread = threading.Thread(
        target=_scan_worker,
        args=(account,),
        daemon=True,
        name='market-scan',
    )
    thread.start()
    return {'status': 'started', 'message': '扫描任务已在后台启动'}


# 模块加载时就尝试从磁盘恢复上次扫描结果（供 Flask 重启后缓存优先展示）
_load_scan_cache()


# =============================================================================
# 主入口
# =============================================================================
def main():
    config = get_api_config()
    flag = config['flag']
    print(f"🔍 正在拉取 OKX {INST_TYPE} 全市场行情...")

    tickers = fetch_all_tickers(flag)
    print(f"✅ 获取到 {len(tickers)} 个 USDT 本位合约行情")

    rankings = build_rankings(tickers)

    print(f"🔍 正在对流动性达标的候选做日线趋势分析（最多 {CANDIDATE_LIMIT} 个）...")
    analyzed = scan_trends(flag, tickers)
    print(f"✅ 趋势分析完成，共分析 {len(analyzed)} 个候选")

    print_report(rankings, analyzed, flag)


if __name__ == '__main__':
    main()
