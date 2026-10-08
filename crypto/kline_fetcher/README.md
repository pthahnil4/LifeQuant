# kline_fetcher —— K 线获取与本地存储工具包

从 ccxt 支持的加密货币交易所稳健地抓取 OHLCV K 线，并持续存储到本地 CSV。
本包整合并重构了项目中原先分散的多个抓取脚本（`ccxt_btc_robust_1m.py`、
各币种 `ccxt_xxx_robust_1m.py`、`ccxt_btc_robust_1h.py`、`ccxt_btc_data.py`），
把重复逻辑抽取为独立工具模块，主入口只保留参数解析与流程编排。

---

## 1. 功能概述

| 能力 | 说明 |
|------|------|
| **增量抓取** | 自动检测本地 CSV 最后一根 K 线的时间戳，从其后续传，避免重复劳动 |
| **去重写入** | 以毫秒级 `timestamp` 为唯一键，重复数据不会二次写入 |
| **多交易所容错** | 首个交易所为主源，抓取/回填缺口时按顺序切换到备用交易所 |
| **稳健重试** | 网络错误指数退避、限流(429)按 `rateLimit` 等待、5xx 服务器错误延长重试 |
| **缺口自动回填** | 抓取结束后检测时间断点，并用备用交易所补齐缺失 K 线 |

CSV 列结构（所有文件统一）：

```
timestamp, datetime, open, high, low, close, volume, exchange
```

- `timestamp`：毫秒级 UTC 时间戳，唯一去重键
- `datetime`：ISO8601 UTC 字符串，便于人工查看
- `exchange`：该根 K 线的实际来源交易所（多交易所合并后可追溯）

---

## 2. 目录结构

```
kline_fetcher/
├── __init__.py            # 包导出
├── __main__.py            # 支持 python -m kline_fetcher
├── cli.py                 # 命令行入口（参数解析 + 组装配置）
├── fetcher.py             # 流程编排（续传 + 增量抓取 + 回填）
├── csv_store.py           # CSV 读写 / 表头 / 时间戳加载 / 去重追加
├── exchange_fetcher.py    # 交易所实例 / 批次上限 / 带重试的安全抓取
├── gap_backfill.py        # 时间缺口检测与多交易所回填
├── symbols.py             # 币种配置表（替代原先每币种一个重复脚本）
├── data/                  # 随包自带的 K 线 CSV（自包含，抓取默认写这里）
├── tests/
│   └── test_kline_fetcher.py   # 离线功能测试（假交易所，四场景全覆盖）
├── _legacy/               # 原分散脚本副本归档（仅供参考，可删除）
└── README.md              # 本文档
```

模块依赖方向（单向，无环）：

```
cli → fetcher → { csv_store, exchange_fetcher, gap_backfill }
             ↘ symbols
```

---

## 3. 依赖安装

```bash
pip install ccxt            # 交易所 API 封装（必需）
pip install pandas          # 供下游回测策略加载 CSV（可选，本包不依赖）
pip install pytest          # 运行功能测试（可选）
```

- 需要 **Python 3.8+**；联网访问交易所 API 才能真实抓取。
- 若处于代理环境，本包默认 `requests_trust_env=True`，会自动读取系统
  `HTTP_PROXY` / `HTTPS_PROXY` 环境变量。

---

## 4. 使用示例

所有命令在 `src/tradequant/okx/strategyAI/` 目录下执行（便于 `python -m kline_fetcher` 定位包）。

### 4.1 单币种（推荐用 `--coin`）

```bash
# BTC 1 分钟，默认写入 data/btc_ohlcv_robust_1m_since_2022.csv，自动续传
python -m kline_fetcher --coin btc --timeframe 1m

# ETH 1 小时（覆盖任意周期，无需再各建脚本）
python -m kline_fetcher --coin eth --timeframe 1h
```

`--coin` 支持的简称：`btc eth sol xrp ada doge dot link ltc near`。

### 4.2 直接指定交易对（`--coin` 表之外的币种）

```bash
python -m kline_fetcher --symbol AVAX/USDT --timeframe 15m
```

### 4.3 多交易所（主源 + 备用回填）

```bash
# okx 为主，bybit/bitget 用于补缺口
python -m kline_fetcher --coin sol --exchanges okx,bybit,bitget
```

### 4.4 首轮爬取 / 限流保护

```bash
# 只抓 5000 根就停，且跳过缺口回填（减少请求量）
python -m kline_fetcher --coin btc --max-candles 5000 --no-backfill
```

### 4.5 自定义起始时间与输出路径

```bash
# 从 2023-01-01 UTC(毫秒) 开始，输出到自定义路径
python -m kline_fetcher --coin near --since 1672531200000 --save D:/tmp/near_1m.csv
```

### 4.6 作为库调用

```python
from kline_fetcher import cli
cli.main(["--coin", "btc", "--timeframe", "1m"])
```

---

## 5. 命令行参数详解

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--exchanges` | `okx,bybit,bitget` | 逗号分隔交易所 id，按顺序用于抓取/回填，**首个为主交易所** |
| `--coin` | 无 | 币种简称，自动映射 symbol 与默认文件名（与 `--symbol` 互斥） |
| `--symbol` | `BTC/USDT` | 交易对，如 `BTC/USDT`（未用 `--coin` 时生效） |
| `--timeframe` | `1m` | K 线周期，如 `1m` `5m` `15m` `1h` `1d` |
| `--since` | `2022-01-01 UTC`(ms) | 起始时间戳（毫秒）；**CSV 已有数据时自动续传会覆盖此值** |
| `--save` | 自动推导 | 输出 CSV 路径；缺省为 `data/{coin}_ohlcv_robust_{tf}_since_2022.csv` |
| `--no-backfill` | 关 | 跳过连续性检测与缺口回填 |
| `--max-candles` | 无 | 抓取达到该根数后停止（首轮爬取或限流保护用） |

> 续传说明：只要目标 CSV 已有数据，程序就自动从最后一根的下一周期开始，
> 无需显式开关；`--since` 仅在 CSV 为空时生效。

---

## 6. 功能测试

本包附带**离线**功能测试，使用假交易所（不联网、不触碰真实 `data/`），
完整覆盖四大场景：

```bash
cd src/tradequant/okx/strategyAI
pytest kline_fetcher/tests -q
# 或（无需 pytest）：
python kline_fetcher/tests/test_kline_fetcher.py
```

覆盖场景：
1. **首次从头抓取**：空 CSV，从指定起始时间抓取，结果连续无缺口。
2. **断点续传**：预置部分数据后继续，新增数量正确、无丢失无重复。
3. **Gap 自动回填**：主源缺中间段，备用源补齐，最终连续。
4. **多交易所合并去重**：重叠时间戳只写一次，来源可追溯。
5. **端到端 `fetcher.run`**：注入假交易所跑完整流程（含回填）。

---

## 7. 常见问题与注意事项

- **抓取报 `RequestTimeout` / 连不上交易所**：多为当前网络无法访问交易所
  API（或需要代理 / 存在地域限制）。代码已按设计跳过不可用交易所，若全部
  失败会明确抛出 `No valid exchanges available`。请检查网络或配置代理。
- **触发限流(429)**：内置退避与等待会自动处理；如需更保守，用
  `--max-candles` 分批抓取，或减少 `--exchanges` 数量。**高频请求可能导致
  交易所/IP 被临时限流**，建议按需抓取、避免并发多份。
- **数据目录解析（可移植）**：抓取默认写入**包内自带的 `kline_fetcher/data/`**，
  因此整个文件夹可原样拷贝到任意机器、带着数据直接用；若包内 `data/` 不存在，
  则自动回退到上级 `strategyAI/data/`（原仓库布局，被众多回测策略以硬编码路径
  `{coin}_ohlcv_robust_1m_since_2022.csv` 引用）。命名规则两处保持一致。
  打包带走时请连同 `data/` 一起拷贝，本文件夹自包含。
- **增量运行安全**：可放心多次运行同一命令，去重机制保证不会写入重复数据；
  每次运行只会追加新产生的 K 线。
- **`--coin` 与自定义币种**：新增常用币种只需在 `symbols.py` 的 `COINS`
  加一行；不在表中的币种直接用 `--symbol XXX/USDT`。
- **旧脚本**：原 `ccxt_*_robust_*.py` 等脚本仍保留在 `strategyAI/` 根目录（未删除），
  `_legacy/` 只是给它们的**副本归档**，供打包时自包含参考；功能已全部由本包覆盖，
  `_legacy/` 确认无需后可删除。
