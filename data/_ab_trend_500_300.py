# -*- coding: utf-8 -*-
"""A/B 回归：验证趋势主取数 500 根 → 300 根 不改变批量分析决策结果。

方法学（关键）：利润% 与各项指标都依赖"最新一根K线的现价"，若分两次联网跑
500/300，两次之间现价会漂移，差异并非 500 vs 300 造成。因此本脚本对每个组合
**只联网抓一份快照**，再用【全量 raw】与【最近 300 根 raw[:300]】两次即时计算，
喂给策略的当前价完全相同，从而干净隔离"历史长度"这一唯一变量。

判定门槛（用户确认）：决策四列（趋势/交易价格/交易时间/盈亏%）必须一致。
只调用计算路径，不读写 CSV/DB、不触碰实盘。
用法：python data/_ab_trend_500_300.py [币种数]
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8')  # 防 Windows GBK 控制台打印崩
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CRYPTO_DIR = os.path.join(os.path.dirname(HERE), 'crypto')
for p in (CRYPTO_DIR, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import real_strategy_adapter as RSA          # noqa: E402,F401  触发 strategy/ 进 sys.path
import pro3_singletimeframe as P             # noqa: E402
from real_strategy_adapter import calculate_single_coin_data  # noqa: E402
from batch_trend_updater import _read_csv, _extract_trend_info, _BATCH_BARS  # noqa: E402

DECISION_COLS = ('trend', 'trade_price', 'trade_time', 'profit_pct')
INFO_COLS = ('close_price', 'macd_hist', 'dif', 'adx', 'atr_pct', 'sar', 'er')
FULL_TARGET = 500   # 旧口径：翻两页取满 500
NEW_TARGET = 300    # 新口径：一页 300


def _fetch_snapshot(inst_id, bar):
    """联网抓一份 rawK线（最新在前，最多 FULL_TARGET 根）。整个组合只抓这一次。"""
    data = P._request_candles(inst_id, bar, after=None, max_retries=3)
    if not data:
        raise RuntimeError('empty')
    afterts = data[-1][0]
    while len(data) < FULL_TARGET:
        page = P._request_candles(inst_id, bar, after=str(afterts), max_retries=3)
        if not page:
            break
        data.extend(page)
        afterts = page[-1][0]
    return list(data)


def _calc_with_cache(inst_id, bar, combined):
    """把指定 raw 快照塞进 _kline_cache（新鲜），令 _fetch_kline_data 直接命中该子集。"""
    now = time.time()
    P.clear_batch_cache()
    with P._kline_cache_lock:
        P._kline_cache[(inst_id, bar)] = (now, list(combined))
    result = calculate_single_coin_data(inst_id, bar=bar, max_retries=1)
    return _extract_trend_info(result)


def main():
    n_coins = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    rows = [r for r in _read_csv() if r.get('inst_id')]
    if not rows:
        print('无可用币种（DB/CSV 均空），跳过 A/B')
        return 0
    coins = [r['inst_id'] for r in rows[:n_coins]]
    combos = [(c, bar) for c in coins for bar in _BATCH_BARS]
    print('A/B：同一快照上比对 全量(%d) vs 最近(%d) | 币种=%s | 组合=%d'
          % (FULL_TARGET, NEW_TARGET, coins, len(combos)))
    print('%-16s %-4s | %-6s | %s' % ('inst_id', 'bar', 'n', '决策四列 500==300 ?'))
    print('-' * 78)

    mismatch = 0
    err = 0
    info_diff = 0
    for inst_id, bar in combos:
        try:
            raw = _fetch_snapshot(inst_id, bar)          # 只联网一次
            a = _calc_with_cache(inst_id, bar, raw)                       # 全量(≤500)
            b = _calc_with_cache(inst_id, bar, raw[:NEW_TARGET])          # 最近300
        except Exception as e:
            err += 1
            print('%-16s %-4s | %-6s | 取数/计算异常: %s' % (inst_id, bar, '-', e))
            continue
        same = all(str(a.get(c, '')) == str(b.get(c, '')) for c in DECISION_COLS)
        if not same:
            mismatch += 1
        if any(str(a.get(c, '')) != str(b.get(c, '')) for c in INFO_COLS):
            info_diff += 1
        diff_detail = '' if same else {c: (a.get(c), b.get(c)) for c in DECISION_COLS
                                       if str(a.get(c, '')) != str(b.get(c, ''))}
        print('%-16s %-4s | n=%-4d | %-4s trend=%s price=%s %s' % (
            inst_id, bar, len(raw), 'OK' if same else 'DIFF',
            a.get('trend'), a.get('trade_price'), diff_detail))

    print('-' * 78)
    print('汇总：决策四列不一致=%d/%d | 指标末位差异=%d | 异常=%d'
          % (mismatch, len(combos), info_diff, err))
    verdict = ('[PASS] 同快照下 300 与 500 决策四列完全一致，可安全切换到 300'
               if mismatch == 0 else
               '[FAIL] 存在决策四列差异，需复核（暂缓切换 300）')
    print('结论：%s' % verdict)
    return 0 if mismatch == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
