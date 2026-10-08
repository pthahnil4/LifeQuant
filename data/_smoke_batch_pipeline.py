# -*- coding: utf-8 -*-
"""端到端冒烟：验证批量流水线（D2/D4/D5）在真实行情下正确工作，不写 CSV/DB。

覆盖点：
  D5 预热经令牌桶限频、并发；预热后 run 级缓存应含全部组合。
  D4 锁内计算应命中预热缓存（0 额外网络）——以"计算阶段耗时"和"缓存计数"佐证。
  D2 SAR/ER 由趋势标记价K线本地算出（非空、与收盘同价口径），不再依赖指数接口。
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
CRYPTO_DIR = os.path.join(os.path.dirname(HERE), 'crypto')
for p in (CRYPTO_DIR, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import real_strategy_adapter as RSA          # noqa: E402,F401
import pro3_singletimeframe as P             # noqa: E402
import batch_trend_updater as B              # noqa: E402

N_COINS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
rows = [r for r in B._read_csv() if r.get('inst_id')][:N_COINS]
coins = [r['inst_id'] for r in rows]
combos = [(c, bar) for c in coins for bar in B._BATCH_BARS]
print('冒烟币种=%s 周期=%s 组合=%d' % (coins, list(B._BATCH_BARS), len(combos)))

# ---- 阶段1：锁外并发预热（限频）----
P.clear_batch_cache()
t0 = time.time()
warm_err = 0
with ThreadPoolExecutor(max_workers=B._BATCH_WORKERS) as pool:
    futs = {pool.submit(B._warm_kline, c, b): (c, b) for c, b in combos}
    for f in as_completed(futs):
        try:
            f.result()
        except Exception as e:
            warm_err += 1
            print('  预热失败', futs[f], repr(e))
t_warm = time.time() - t0
cached = len(P._batch_kline_cache)
print('预热：耗时=%.1fs 命中缓存=%d/%d 失败=%d | 平均速率≈%.1f req/s'
      % (t_warm, cached, len(combos), warm_err, len(combos) / max(t_warm, 1e-6)))
assert cached == len(combos) - warm_err, '预热缓存数与成功组合数不符'

# ---- 阶段2：锁内串行计算（应命中预热，0 网络）----
# 记录计算前的常规缓存条目数；若计算阶段不新增联网取数，_kline_cache 不应因新键增长
keys_before = set(P._kline_cache.keys())
analyzer = B.BatchTrendAnalyzer()
t1 = time.time()
report = []
for c in coins:
    info = analyzer._process_single_symbol(c)
    for bar in B._BATCH_BARS:
        ib = info.get(bar, {})
        report.append((c, bar, ib))
t_comp = time.time() - t1
new_keys = set(P._kline_cache.keys()) - keys_before
print('\n计算：耗时=%.1fs（%.2fs/组合，纯计算应远小于网络）| 计算阶段新增缓存键=%d（期望0=命中预热）'
      % (t_comp, t_comp / max(len(combos), 1), len(new_keys)))

print('\n%-16s %-4s | %-8s %-12s | %-10s %-10s %-8s' %
      ('inst_id', 'bar', 'trend', 'close', 'SAR', 'SAR色', 'ER'))
print('-' * 74)
sar_ok = er_ok = 0
for c, bar, ib in report:
    sar = ib.get('sar', '')
    er = ib.get('er', '')
    if sar not in ('', None):
        sar_ok += 1
    if er not in ('', None):
        er_ok += 1
    print('%-16s %-4s | %-8s %-12s | %-10s %-10s %-8s' % (
        c, bar, ib.get('trend', ''), ib.get('close_price', ''), sar,
        ib.get('sar_color', ''), er))

P.clear_batch_cache()
assert len(P._batch_kline_cache) == 0, 'clear_batch_cache 未清空'
total = len(report)
print('-' * 74)
print('结论：SAR非空 %d/%d | ER非空 %d/%d | 预热失败 %d | 新增缓存键 %d'
      % (sar_ok, total, er_ok, total, warm_err, len(new_keys)))
ok = (warm_err == 0 and sar_ok == total and er_ok == total and len(new_keys) == 0)
print('[PASS] 流水线正确：预热限频命中、锁内0网络、SAR/ER本地算出' if ok else '[CHECK] 存在异常项，见上')
sys.exit(0 if ok else 2)
