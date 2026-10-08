# _legacy —— 旧版分散抓取脚本归档

本目录下的脚本已被 `kline_fetcher` 包完全取代，仅作为历史参考保留，**可随时删除**。

对应关系：

| 旧脚本 | 新用法 |
|--------|--------|
| `ccxt_btc_robust_1m.py` | `python -m kline_fetcher --coin btc --timeframe 1m` |
| `ccxt_{ada,doge,dot,eth,link,ltc,near,sol,xrp}_robust_1m.py` | `python -m kline_fetcher --coin <币种>` |
| `ccxt_btc_robust_1h.py` | `python -m kline_fetcher --coin btc --timeframe 1h` |
| `ccxt_btc_data.py` | `python -m kline_fetcher --coin btc --since <now-N天毫秒> --no-backfill` |
| `kline_fetch_storage.py` | 已拆分为 csv_store / exchange_fetcher / gap_backfill / fetcher |

注意：
- 各币种 wrapper（`ccxt_{coin}_robust_1m.py`）的输出路径已修正为 `../data`，
  即运行它们会把 K 线写入**包内 `kline_fetcher/data/`**（相对 `__file__` 定位，可移植）。
  已实测：`python _legacy/ccxt_near_robust_1m.py --no-backfill --max-candles 300`
  正确追加到 `kline_fetcher/data/near_ohlcv_robust_1m_since_2022.csv`。
- 引擎脚本（`ccxt_btc_robust_1m.py` / `_1h.py` / `ccxt_btc_data.py`）若脱离 wrapper
  **单独直接运行**，其 `default_save_path` 仍指向 `_legacy/data`；请始终通过 wrapper
  或下面的新命令来抓取。
- 推荐统一用新包入口（更健壮、支持全部周期/币种）：
  `python -m kline_fetcher --coin near --timeframe 1m`（在 `strategyAI/` 目录下执行，
  默认写入 `kline_fetcher/data/`）。
