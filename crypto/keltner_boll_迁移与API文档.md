# keltner_boll 迁移与 API 使用说明

> 模块：[`crypto/strategy/keltner_boll.py`](../crypto/strategy/keltner_boll.py)
> 冒烟测试：[`crypto/_smoke_keltner_boll.py`](../crypto/_smoke_keltner_boll.py)（99 断言全绿）
> 接线点：[`crypto/task/trend_range_trader.py`](../crypto/task/trend_range_trader.py) 步骤 8b
> 配置校验 / 选项接口：[`crypto/app.py`](../crypto/app.py)
> 前端：[`crypto/templates/task.html`](../crypto/templates/task.html) 交易配置页 · 仓位B
> 详情页回测：[`crypto/strategy_util.py`](../crypto/strategy_util.py) 分发 · [`crypto/templates/detail.html`](../crypto/templates/detail.html)（见第 11 节）

---

## 0. 术语（首次出现附全称）

| 缩写 | 全称 | 含义 |
|------|------|------|
| BOLL | Bollinger Bands（布林带） | 均值 ± N 倍标准差通道 |
| Keltner | Keltner Channel（肯特纳通道） | EMA ± N 倍 ATR 通道 |
| ATR | Average True Range（平均真实波幅） | 衡量单边波动幅度 |
| RSI | Relative Strength Index（相对强弱指标） | 力度中轴 |
| ADX | Average Directional Index（平均方向指数） | 趋势强度 |
| A 仓位 | 趋势跟踪仓（BUCKET_TREND） | Pro3 信号，单向趋势 |
| B 仓位 | 区间波动仓（BUCKET_RANGE） | BOLL 边界限价往复 |

---

## 1. 这次做了什么（迁移概述）

把研究脚本 `trend_strategy_boll_limit_scheduler_like copy 7.py`（脚本式 backtrader 回测 + 20 种长周期趋势方法）**解耦重构**为项目级通用策略工具类 `keltner_boll`，遵循 [`boll_limit_dualtimeframe.py`](../crypto/strategy/boll_limit_dualtimeframe.py) 的适配器架构规范：

1. **策略工厂 + 适配器**：`TREND_INDICATORS` 注册表（工厂）+ `KeltnerBollStrategy`（纯计算）+ `KeltnerBollAdapter`（带缓存的实时取数适配器）。
2. **统一配置接口**：`KeltnerBollConfig`（frozen dataclass，不可变），长周期趋势判断方法可参数化动态切换（`keltner`/`rsi_mid`/`vote3`… 共 20 种）。
3. **接入 B 仓位调度**：作为区间波动仓的**默认执行引擎**，在 BOLL 边界外按 ATR 外扩挂单价，并用长周期趋势方法做方向确认过滤。
4. **纯 pandas 回测**：`simulate()` 复刻 copy 7 的 backtrader 事件语义（触边即入 / 触边即出 / 长周期反转剔除亏损单），不依赖 backtrader。

> **copy 7 脚本保留原地不动**（研究/回测参考）。`keltner_boll` 是其生产化重构版本，两者可并存；如需归档 copy 7，请自行移动到 `_universe_backup/` 之类的目录。

### 与既有架构的契约对齐

- **无模块级可变全局状态**：所有配置经 `KeltnerBollConfig` 传入，多实例并发互不串扰 → 实盘路径**不需要**占用 `pro3_locked` 策略计算全局锁（那把锁保护的是 pro3 引擎的模块级全局参数，本模块不触碰）。
- **取数复用** `pro3_singletimeframe._fetch_kline_data`（自带分级 TTL 缓存 + run 级批量预热 + 弱网降级），不另起 API 通道。
- **指标函数一律无副作用**：不修改调用方传入的 DataFrame（`calculate_adx` 返回副本，区别于 copy 7 的原地写入）。

---

## 2. 模块结构

```
keltner_boll.py
├─ 一、基础指标工具（纯函数）    ema/wma/atr/rsi/calculate_adx/_hold/_sign/value_to_dir/dir_to_value
├─ 二、长周期趋势方法库（20 种）  TREND_INDICATORS 注册表 + compute_direction_series()/list_indicators()
├─ 三、统一配置接口              KeltnerBollConfig(frozen) + resolve_range_strategy()
├─ 四、策略类                    KeltnerBollStrategy（long_state/expand_bands/entry_blocked）
├─ 五、实时适配器                KeltnerBollAdapter（带缓存）+ get_shared_adapter()
├─ 六、调度器接线助手            apply_keltner_boll_boundaries()
├─ 七、纯 pandas 回测            boll_bands()/simulate()
└─ 八、诊断工具                  bar_minutes/analyze_trend_segments/compare_trend_indicators
```

---

## 3. API 参考

### 3.1 `KeltnerBollConfig`（统一配置，不可变）

```python
@dataclass(frozen=True)
class KeltnerBollConfig:
    trend_indicator: str = 'keltner'   # 长周期趋势方法名（TREND_INDICATORS 键）
    boll_period: int = 20              # 短周期 BOLL 均线周期
    boll_dev: float = 2.0              # 短周期 BOLL 标准差倍数
    entry_atr_mult: float = 0.0        # A：开仓轨在 BOLL 外扩的 ATR 倍数
    exit_atr_mult: float = 0.0         # B：平仓轨在 BOLL 外扩的 ATR 倍数
    atr_period: int = 14               # 外扩用 ATR 周期（Wilder）
    direction_filter: bool = True      # 方向确认过滤开关
    conf_window: int = 3               # 长周期方向确认窗口（连续 N 根同向非零）
    leverage_times: float = 10.0       # 仅 simulate 盈亏% 折算用
```

- **校验（fail-fast）**：`__post_init__` 规范化 `trend_indicator` 为小写；未知方法名、`boll_period<=1`、`boll_dev<=0`、`atr_mult<0`、`atr_period<=0` 一律抛 `ValueError`。**绝不静默回退**成别的方法——静默回退会让配置写错时实盘悄悄换掉方向判断逻辑。
- `KeltnerBollConfig.from_range_cfg(rcfg)`：从调度器 `range_position` 配置 dict 构建，缺省字段用类默认值；`trend_indicator` 大小写归一化。
- **frozen**：配置构造后不可变 → 多实例 / 多线程天然隔离（A/B 仓位策略切换状态隔离性的根基）。

### 3.2 `resolve_range_strategy(rcfg) -> str`

归一化 B 仓位策略名：`'keltner_boll'`（默认）/ `'boll'`（存量纯 BOLL）。未知取值**回退 `'boll'` 并告警**——策略名写错时保持存量行为最安全。

### 3.3 `KeltnerBollStrategy`（纯计算，无状态）

```python
strat = KeltnerBollStrategy(KeltnerBollConfig(trend_indicator='vote3'))

state = strat.long_state(df_long)
# -> {'direction': 'rise'/'fall'/None, 'value': ±1/0,
#     'conf': 0.0/1.0, 'indicator': 'vote3', 'bars': N}

bands = strat.expand_bands(boll_upper=2140.0, boll_lower=2080.0, atr_value=3.5)
# -> {'range_upper': 上轨+exit_mult×ATR, 'range_lower': 下轨-entry_mult×ATR, 'atr': 3.5}

strat.entry_blocked(indicator_dir='rise', desired='short')  # -> True（冲突，拦开仓）
```

**边界外扩口径（copy 7 一致）**：

| 方向 | 开仓价 | 平仓价 |
|------|--------|--------|
| 多头 | 下轨 − `entry_atr_mult`×ATR | 上轨 + `exit_atr_mult`×ATR |
| 空头 | 上轨 + `entry_atr_mult`×ATR | 下轨 − `exit_atr_mult`×ATR |

多空对称共用同一对增强边界 `range_upper/range_lower`。ATR 无效（NaN/≤0）→ 按 0 外扩（退化纯 BOLL，fail-safe）。

**方向确认过滤 `entry_blocked(indicator_dir, desired)`**：

- `indicator_dir` 为 `None`（中性 / 数据不足）→ **不拦**（fail-open：过滤器职责是"确认冲突才拦"，不是"没确认就拦"）；
- 只拦**明确冲突**：`rise` vs `short` / `fall` vs `long`；
- `direction_filter=False` → 一律放行；
- **只应用于开仓闸门**，平仓 / 撤单路径不得调用。

### 3.4 `KeltnerBollAdapter`（实时适配器，带缓存）

```python
adapter = get_shared_adapter()      # 进程级共享，懒初始化，线程安全
state = adapter.long_state(inst_id='BTC-USDT-SWAP', long_bar='4H',
                           indicator='keltner', conf_window=3)
# 额外含 'last_ts'（最新长周期K线时间戳）
adapter.stats()      # -> {'hits':…, 'misses':…, 'size':…}
adapter.clear_cache()
```

- **缓存键** = `(inst_id, long_bar, indicator, conf_window, 最新K线时间戳)`。
- **去重的是"方向计算"，不是"取数"**：每轮仍调用 fetcher（需读最新 K 线时间戳判定是否换 bar），网络请求由 `_fetch_kline_data` 自带的分级 TTL 缓存去重。同一根长周期 K 线内多轮调度（80s/轮）→ 方向计算零重复。
- 新 K 线到达（时间戳变）→ 自动重算。缓存只存不可变结果 tuple，容量上限 512，超限整体清空。
- 取数失败 / 数据为空 → **抛异常**，由调用方兜底（调度器：本轮退化纯 BOLL）。
- 构造可注入 `fetcher`（测试用假数据），缺省用 `_fetch_kline_data`。

### 3.5 `apply_keltner_boll_boundaries(...)`（调度器接线助手）

```python
res = apply_keltner_boll_boundaries(
    range_cfg, analysis, adapter, inst_id, long_period, long_dir)
# -> {'applied': bool, 'blocked': bool, 'text': str}
```

**仅当 `range_cfg.strategy == 'keltner_boll'` 时生效**，成功时原地更新 `analysis`：

- `analysis['boll_upper'/'boll_lower']` 替换为 ATR 外扩后的区间边界；
- 原始值备份到 `analysis['boll_upper_raw'/'boll_lower_raw']`；
- 方向过滤元数据写入 `analysis['kb_direction_filter']`（含 indicator/indicator_dir/conf/blocked/atr_mult）。

**fail-safe**：任何失败（K线取不到 / 指标异常 / BOLL 不可用）→ 返回 `applied=False`，`analysis` **保持原样**，本轮退化存量纯 BOLL 行为，**绝不停摆主调度**。

### 3.6 `simulate(df_short, df_long, config) -> dict`（纯 pandas 回测）

事件语义与 copy 7 的 backtrader 策略一致：

- 长周期方向经 `merge_asof` 向后对齐短周期时间轴（每根短 K 线取最近已收盘长 K 线方向，**严格因果**）；
- 多头：长周期 `rise` 且 bar 范围触及 `(下轨−A×ATR)` → 该价开多；触及 `(上轨+B×ATR)` → 平多；空头镜像；
- 持仓期间长周期方向与入场时相反 → `position_flip`；平仓时 flip 且亏损 → 该笔 `excluded=True`（"长周期反转导致的短周期亏损不计入统计"）；
- 盈亏% = 价差比 × 100 × `leverage_times`；资金曲线定投口径，基数 100。

返回：`trade_records`（每笔含 dir/open_price/close_price/entry_time/exit_time/profit/max_win/max_loss/excluded）、`total_trades`（计入笔数）、`excluded_count`、`win_rate`、`total_profit_pct`、`total_loss_pct`、`avg_profit_pct`、`avg_loss_pct`、`final_equity`、`max_drawdown_pct`。

### 3.7 诊断工具

```python
analyze_trend_segments(df_long, indicator, long_bar)
# -> {num_segments, reversals, avg/median/max_dur_hours, coverage, reversals_per_day, total_days}

compare_trend_indicators(df_long, long_bar, indicators=None, verbose=True)
# 全部方法跑一遍，按"日均反转次数"升序打印（反转越少=方向越稳=越适合当长周期过滤）
```

---

## 4. 20 种长周期趋势方向方法

`trend_indicator` 的合法取值（键名即配置值，`list_indicators()` 返回全量）：

| 键名 | 中文 | 类型 |
|------|------|------|
| `macd_adx` | ADX 自适应加权平滑 MACD 柱状态机（原版基线） | 趋势跟随 |
| `rsi_mid` | RSI 中轴力度（带迟滞 hi=55/lo=45） | 趋势跟随 |
| `cmo` | Chande 动量摆动 | 趋势跟随 |
| **`keltner`** | **Keltner 通道突破（默认）** | 趋势跟随 |
| `ichimoku` | 一目均衡表云图（严格因果） | 趋势跟随 |
| `kama` | Kaufman 自适应均线差分 | 趋势跟随 |
| `hma` | Hull 低滞后均线差分 + 价格站边 | 趋势跟随 |
| `triple_ma` | 三均线排列（快>中>慢） | 趋势跟随 |
| `ma_spread` | 均线分离度 / ATR 标准化 | 趋势跟随 |
| `psar` | 抛物线 SAR | 趋势跟随 |
| `vortex` | Vortex 指标 VI+ vs VI− | 趋势跟随 |
| `aroon` | Aroon 最近高低点远近（带中性带） | 趋势跟随 |
| `trix` | TRIX 三重指数平滑**再变化率** | **拐点/加速度型** |
| `elder_ray` | Elder 多空力度（high/low 相对 EMA） | 趋势跟随 |
| `tsmom` | 时间序列动量（过去 N 根累计收益） | 趋势跟随 |
| `bb_mid_slope` | 布林中轨斜率 | 趋势跟随 |
| `dual_scale` | 双尺度一致（快慢尺度都同向才给） | 趋势跟随 |
| `htf_weekly` | 高周期确认（合成 factor 倍大周期，严格因果） | 多周期 |
| `vote3` | 三方法共识投票（RSI中轴+Aroon+Vortex，三取二） | 投票 |
| `vote5` | 五方法共识投票（+KAMA+HMA，五取三） | 投票 |

> **⚠️ `trix` 是拐点/加速度型指标**（读的是"动量的再变化率"）。在**匀速**趋势末端，其读数由噪声主导、可能给出反向信号——这是指标本性（TRIX 是趋势转折点探测器，非趋势跟随器），**不是缺陷**。若用作长周期方向过滤，请在真实数据上用 `compare_trend_indicators` 确认其 `reversals_per_day` 是否可接受；追求方向稳定优先选 `keltner`/`tsmom`/`vote3` 等趋势跟随型。

**如何挑选方法**：`reversals_per_day`（日均反转次数）越低 = 方向越稳 = 越适合当长周期过滤；反转过多等于没过滤。

---

## 5. 实盘接线（trend_range_trader 步骤 8b）

主循环 `analyze_and_trade_real` 的 10 步流水线中，接线刻意放在**步骤 8（趋势仓止盈止损）之后、步骤 9（两仓调度）之前**：

```
步骤 8  _check_tp_sl        # 趋势仓 A 的通道止盈消费【原始 BOLL 值】
步骤 8b _apply_range_strategy  # ← keltner_boll 接线：替换 analysis 的 BOLL 为外扩边界
步骤 9  两仓依次调度         # 区间仓 B 的挂单/追价/观察提醒/独立风险退出消费【外扩边界】
```

**为什么必须在步骤 8 之后**——这是**两仓状态隔离**的关键：趋势仓 A 的通道止盈已在步骤 8 消费完原始 `boll_upper/boll_lower`；此后 8b 替换 `analysis` 的 BOLL 值，只影响区间仓 B。两仓互不污染。

`_apply_range_strategy(inst_id, rcfg, ctx)` 行为：

1. `_get_kb_adapter()` 懒加载共享适配器（导入失败负缓存 `False`，本进程内不再重试，区间仓退化纯 BOLL）；
2. 调 `apply_keltner_boll_boundaries`；
3. `res['text']` → `ctx['kb_text']`（心跳日志：`| 区间B:KB(keltner)=看多 轨[...](ATR外扩 开0/平0)`）；
4. `applied and blocked` → `ctx['range_allow_entry'] = False`（**只拦开仓**，平仓 / 撤单 / 风险退出照常，语义同各冷却闸门）。

`rcfg.get('enabled', True)` 为假时整个 8b 跳过。失败一律 fail-safe，不停摆主调度。

---

## 6. 配置项（range_position 新增字段）

在交易配置 `currencies[i].range_position` 下（MySQL `kv_store['strategy_config']` 为权威源，`config_trend_range.json` 仅 DB 故障回退）：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `strategy` | str | `keltner_boll` | B 仓位策略引擎：`keltner_boll` / `boll`（存量） |
| `direction_source` | str | `pro3` | B 方向来源：`pro3`（存量，跟随 Pro3 长周期）/ `keltner`（独立信号引擎，须搭配 `strategy=keltner_boll`，详见第 13 节） |
| `trend_indicator` | str | `keltner` | 长周期趋势方法（见第 4 节 20 种） |
| `entry_atr_mult` | float | `0.0` | 开仓轨 ATR 外扩倍数（0=贴合 BOLL，等价存量） |
| `exit_atr_mult` | float | `0.0` | 平仓轨 ATR 外扩倍数 |
| `atr_period` | int | `14` | 外扩用 ATR 周期 |
| `direction_filter` | bool | `true` | 方向确认过滤开关 |
| `boll_period` | int | `20` | （存量字段）BOLL 周期 |
| `boll_dev` | float | `2.0` | （存量字段）BOLL 标准差倍数 |

**app.py 校验**（`POST /api/task/config/trading`）：`strategy` 白名单；`trend_indicator` 局部 import `list_indicators()` 校验；`entry/exit_atr_mult` 非负；`atr_period` 正整数；`direction_filter` 布尔。`POST` 整体透传保存，新字段自动持久化，无需字段白名单改动。

**选项接口**：`GET /api/task/range-strategy-options` → `{strategies, trend_indicators(list_indicators()), default_strategy, default_trend_indicator}`。前端下拉从此接口动态加载，**避免前端硬编码方法名与注册表漂移**。

**前端**（task.html 交易配置页 · 仓位B）：`区间策略引擎`下拉（`onchange=onRangeStrategyChange` 控制 `.kb-only` 显隐）、`长周期趋势方法`下拉、`ATR 外扩倍数`（开仓/平仓/周期）、`方向确认过滤`开关。选 `boll` 时 `.kb-only` 字段隐藏。

---

## 7. 测试与验证

### 7.1 冒烟测试（已全绿，纯内存/不触网/不碰业务库）

```bash
python crypto/_smoke_keltner_boll.py     # 99 通过 / 0 失败，退出码 0
```

覆盖用户要求的三项 + 配置校验 + 接线：

| 组 | 验证点 |
|----|--------|
| [1] 方向稳定性 | 20 方法趋势/震荡方向正确性；`htf_weekly` 因果性（截断末尾不改历史）；震荡 vs 单边段数；`trix` 作拐点型单列不套用趋势断言 |
| [2] 状态隔离性 | 多实例配置独立；frozen 不可变；8 线程 × 20 次并发结果一致无竞态；缓存命中（misses≤8）；不同 indicator 各自缓存 |
| [3] 极端行情 | mult=0 退化；上/下轨外扩；ATR=NaN fail-safe；方向过滤冲突/中性/关闭；区间震荡成交；插针 K 线不崩溃；长周期反转亏损单 `excluded` 剔除 |
| [4] 配置校验 | 非法 indicator/负 atr_mult 抛错；`from_range_cfg` 归一化；`resolve_range_strategy` 回退 |
| [5] 调度器接线 | 外扩+备份+元数据；同向不拦；冲突拦截；`boll` 策略不动 analysis；BOLL 不可用 / 取数失败 fail-safe 不上抛 |

### 7.2 真实生产数据 A/B（**须在有网环境执行**）

> 冒烟全绿可能证明不了任何事——判定逻辑改动必须拿真实 K 线跑一次 A/B。本沙箱环境无外网（OKX `ConnectTimeout`），以下命令请在生产/联网环境执行：

```python
import keltner_boll as kb
from pro3_singletimeframe import _fetch_kline_data

inst, long_bar, short_bar = 'BTC-USDT-SWAP', '4H', '1H'
df_long  = _fetch_kline_data(inst, long_bar)
df_short = _fetch_kline_data(inst, short_bar)

# A) 长周期方向方法稳定性：按日均反转升序，挑最稳的做过滤
kb.compare_trend_indicators(df_long, long_bar)

# B) A/B 回测：默认 keltner vs 基线 macd_adx vs 共识 vote3（外扩=0 口径一致）
for ind in ('keltner', 'macd_adx', 'vote3'):
    cfg = kb.KeltnerBollConfig(trend_indicator=ind, entry_atr_mult=0.0, exit_atr_mult=0.0)
    r = kb.simulate(df_short, df_long, cfg)
    print(ind, r['total_trades'], r['win_rate'], r['final_equity'], r['max_drawdown_pct'])

# C) ATR 外扩完整能力对照
cfg_kb = kb.KeltnerBollConfig(trend_indicator='keltner', entry_atr_mult=0.5, exit_atr_mult=0.5)
print(kb.simulate(df_short, df_long, cfg_kb)['total_trades'])
```

**建议流程**：先 `compare_trend_indicators` 在多个真实币种上选出 `reversals_per_day` 低且 `coverage` 高的方法 → 再用 `simulate` 做外扩倍数 0 / 0.5 / 1.0 的 A/B → 最后模拟盘（观察模式）跑几轮确认心跳日志 `区间B:KB(...)` 文案与拦截行为符合预期，再切实盘。

### 7.3 前端运行时验证（纯静态检查不够）

task.html 的 JS 改动（`loadRangeStrategyOptions`/`setRangeTrendIndicator`/`onRangeStrategyChange`）**须在浏览器实际验证**：切到交易配置页 → 确认 B 仓位策略下拉、趋势方法下拉从接口填充、切 `boll` 时 `.kb-only` 字段隐藏、保存后重载字段回填正确。纯函数测试无法捕获运行时未定义引用。

---

## 8. 其他模块开发者接入指引

**在别的调度/详情页复用长周期方向**：

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'strategy'))
from keltner_boll import get_shared_adapter

state = get_shared_adapter().long_state('ETH-USDT-SWAP', '4H', 'vote3')
if state['direction'] == 'rise' and state['conf'] >= 1.0:
    ...   # 方向确认（conf=1 表示连续 conf_window 根同向非零）
```

- **不要**自建 `KeltnerBollAdapter`：用 `get_shared_adapter()` 复用进程级缓存。
- **务必** `try/except` 兜底 `long_state`：取数失败会抛异常，退化到你自己的默认逻辑，别让增强模块故障拖垮主流程。
- 详情页已接入 keltner_boll 回测（见第 11 节）：`strategy_util.get_strategy_detail(strategy='keltner_boll', ...)` 分发到 `get_keltner_boll_full_data`，产出与 BOLL 限价详情同构的响应。

---

## 9. 变更文件清单

| 文件 | 变更 |
|------|------|
| `crypto/strategy/keltner_boll.py` | **新建**（核心工具类，~1130 行） |
| `crypto/task/trend_range_trader.py` | `__init__._kb_adapter`、`_get_kb_adapter`、`_apply_range_strategy`、步骤 8b 接线、心跳日志 `kb_text` |
| `crypto/app.py` | `range_position` 配置校验扩展、`GET /api/task/range-strategy-options` |
| `crypto/templates/task.html` | 仓位B 策略下拉 / 趋势方法下拉 / ATR 外扩 / 方向过滤 + JS |
| `crypto/_smoke_keltner_boll.py` | **新建**（99 断言冒烟测试） |
| `crypto/strategy/boll_limit_dualtimeframe.py` | 抽取 `build_range_detail_response` 纯构建器（boll 路径委托调用，**零行为变更**），供 boll/keltner_boll 共用统计口径 |
| `crypto/strategy/keltner_boll.py` | 新增 `get_keltner_boll_full_data`（详情页回测适配，复用共享构建器） |
| `crypto/strategy_util.py` | `get_strategy_detail` 新增 `keltner_boll` 分支 + `trend_indicator/entry_atr_mult/exit_atr_mult/atr_period` 参数；新增 `_get_keltner_boll_detail`（fail-fast 校验） |
| `crypto/app.py` | `/api/strategy/detail/<symbol>` 透传上述 4 个 keltner_boll 参数 |
| `crypto/templates/detail.html` | 新增 `Keltner×BOLL` 策略按钮 + 周期选择器 + 趋势方法下拉（动态拉 `/api/task/range-strategy-options`）+ ATR 外扩输入 + JS 加载/显隐 |

---

## 11. 详情页回测接入（detail.html）

`detail.html` 是**策略回测分析页**（区别于 task.html 的实盘交易配置），原有 `single`/`dual`/`boll` 三种回测策略，本次新增第 4 种 `keltner_boll`。

### 11.1 数据流

```
detail.html（选 Keltner×BOLL + 周期 + 趋势方法 + ATR外扩）
  → GET /api/strategy/detail/<symbol>?strategy=keltner_boll&bar=&long_bar=
        &trend_indicator=&entry_atr_mult=&exit_atr_mult=&atr_period=
  → app.py 路由透传 4 个新参数
  → strategy_util.get_strategy_detail 分发 keltner_boll 分支
  → _get_keltner_boll_detail（fail-fast 校验 trend_indicator/atr 参数）
  → keltner_boll.get_keltner_boll_full_data（真实取数 → simulate 回测）
  → boll_limit_dualtimeframe.build_range_detail_response（与 boll 共用构建器）
```

### 11.2 关键设计

- **统计口径统一**：keltner_boll 与 boll 共用 `build_range_detail_response`，两者详情页的 market/current_position/trade_records/stats/资金曲线结构完全一致，前端渲染逻辑（收益曲线、剔除标记、ADX 统计）无需为 keltner_boll 单独分支。
- **无 pro3_locked**：keltner_boll 详情走纯 pandas `simulate`，不触碰 pro3 引擎全局参数，与 boll 详情一样**不占用**策略计算全局锁（single/dual 才需要）。
- **前端趋势方法下拉动态化**：`detail.html` 的趋势方法选项从 `GET /api/task/range-strategy-options` 拉取（与 task.html 同源），**避免前端硬编码 20 种方法名与后端注册表漂移**；拉取失败时保留内置 `keltner` 兜底选项。
- **参数校验前移**：ATR 外扩输入框失焦时前端先校验（外扩非负、周期为正整数），非法值回填默认再触发加载；后端 `_get_keltner_boll_detail` 再做一次 fail-fast 校验（非法趋势方法/负外扩/非正周期直接抛 `ValueError`，由路由转成错误提示），**绝不静默回退**成别的趋势方法。
- **前端 isBoll 判定扩展**：`renderStats` 的 `isBoll` 改为 `boll || keltner_boll`，两者共用构建器（无 pro3 的 hist/curr 权重），故都隐藏 ADX分析卡片、都展示长周期反转剔除交易数；`renderMarketSummary` 靠后端 `market.is_boll=True` 自动走区间策略分支，无需改。

### 11.3 真实数据验证结论（BTC-USDT-SWAP 15m/4H）

| 场景 | 结果 |
|------|------|
| boll（回归） | 6 笔 / 胜率 0.8333 / 结构与改动前同构，**无回归** |
| keltner_boll 默认 keltner | 6 笔 / 正常产出 / `dual_period` 回显参数正确 |
| keltner_boll rsi_mid + 外扩(entry0.5/exit0.3/period20) | 5 笔 / ATR 值随 `atr_period` 14→20 由 60.62→66.85 变化，证明参数透传生效 |
| 非法趋势方法 / 负数外扩 | 均正确抛 `ValueError`（fail-fast） |

> **前端 JS 改动仍须在浏览器实际验证**：切到详情页 → 点 `Keltner×BOLL` → 确认周期选择器/趋势方法下拉/ATR 外扩输入出现且 `single/dual` 的信号算法行隐藏 → 改趋势方法与外扩值触发重载、收益曲线与交易记录刷新 → 切回 `boll`/`dual` 无残留。纯静态检查无法捕获运行时未定义引用。
>
> **常驻进程需 reload**：`app.py` / `strategy_util.py` / `keltner_boll.py` / `boll_limit_dualtimeframe.py` 改动后，已运行的 Web 服务（端口 7777）须重启才能让新路由参数与分发分支线上生效。

---

## 12. 已修正的实现问题（本次重构中）

1. **RSI 边界**：整段无下跌（如单边上涨）时 `ad==0`，原实现把 RSI 填成中性 50 → `rsi_mid` 误判 None。已修正为：`ad==0 且 au>0 → RSI=100`（超买），涨跌皆无 → 50 中性。这是 Wilder RSI 的数学正确边界，惠及 `rsi_mid`/`vote3`/`vote5`。
2. **适配器缓存文档表述**：原写"零额外请求"不准（每轮仍调 fetcher 取 last_ts）。已改为"零重复方向计算，网络去重由 fetcher TTL 负责"，并新增 `stats()` 命中计数供观测/测试。

---

## 13. B 仓位独立信号引擎模式（`direction_source='keltner'`）

### 13.1 为什么需要它

第 5 节的存量接线（下称 **pro3 模式**）里，keltner_boll 只充当"边界外扩器 + 开仓闸门"：B 仓位的**方向仍来自 Pro3 长周期** `ctx['long_dir']`（`LongDirStrategy` 的 MACD_Smoothed_ADX）。而详情页回测 `simulate()` 的方向来自 `compute_direction_series(df_long, 'keltner')`。两套方向源不同，导致"定时任务实盘 B 记录"与"详情页回测"价差显著。本轮新增 **keltner 独立模式**：B 的方向 / 边界 / 开平价全部由 keltner_boll 按 `simulate()` 同源公式计算，彻底脱离 Pro3。

**一致性口径 = 信号级一致**：B 的方向、目标开仓价、目标平仓价与回测 `simulate()` 完全同源；执行仍走限价挂单，成交价 / 成交时间受真实撮合影响，**不承诺逐笔等于回测**（回测触边即理想成交、无滑点无手续费；实盘为逐轮限价撮合，严格逐笔复刻物理上不可实现）。

### 13.2 两种模式架构对比

```
pro3 模式（direction_source='pro3'，默认 · 存量零变更）
  方向   = Pro3 ctx['long_dir']
  边界   = 步骤 8b _apply_range_strategy 改写 analysis['boll_upper/lower']（对称外扩）
  开平价 = analysis 的对称边界（多头 entry=下轨 / exit=上轨；空头镜像）
  反转清理 = 随 Pro3 long_dir_changed
  反向守卫 = B 全量受 Pro3 基准守卫

keltner 独立模式（direction_source='keltner'，须搭配 strategy='keltner_boll'）
  方向   = 步骤 2b current_range_signal → ctx['range_dir']（长周期 keltner，merge_asof 向后对齐到最新短K）
  边界   = 不改写 analysis；开平价取 ctx['range_signal'] 的 entry_px/exit_px（_bar_levels 多空非对称触发价）
  反转清理 = 步骤 6b _run_range_independent_cleanup（无状态：B 持有与 range_dir 相反的仓即全平）
  反向守卫 = exclude_buckets=(BUCKET_RANGE,)，B 从 Pro3 基准守卫中排除（A 趋势仓仍全量受保护）
```

### 13.3 价位同源：`_bar_levels` 与 `current_range_signal`

- **`_bar_levels(top, bot, a, entry_mult, exit_mult)`**（keltner_boll.py 纯函数）是回测与独立模式**唯一**的价位口径来源：多头 `entry=bot−entry×a / exit=top+exit×a`；空头 `entry=top+entry×a / exit=bot−exit×a`（多空**非对称**）。`simulate()` 循环已重构为调用它（行为零变更，仅去重）。
- **`current_range_signal(inst_id, short_bar, long_bar, config=None, fetcher=None)`**：取数 → `compute_direction_series(df_long, trend_indicator)` 经 `merge_asof(direction='backward')` 对齐到短周期取最后一根短K 方向 → `boll_bands/atr` 取 `.iloc[-1]` → `_bar_levels` 按方向得 `entry_px/exit_px`。返回 `{direction, entry_px, exit_px, band_upper, band_lower, top, bot, atr, indicator, conf, last_short_ts, last_long_ts}`；方向为 None 或 BOLL 不可用时 `entry_px/exit_px/band_*` 均为 None。取数失败抛异常，由调度器 fail-safe 兜底。

### 13.4 已修正的空头侧倍数 bug

存量 `expand_bands` 用**一对对称边界**（`range_upper=boll_upper+exit×ATR`、`range_lower=boll_lower−entry×ATR`），pro3 模式空头侧取 `entry=range_upper`、`exit=range_lower`——当 `entry_mult≠exit_mult`（如 0.7/0.3）时，空头 entry 实际用了 `exit_mult`、exit 用了 `entry_mult`，与 `simulate()`（空头 `entry=top+entry×a`、`exit=bot−exit×a`）**不等价**。独立模式改用 `_bar_levels` 的 simulate 同源公式，天然修正该偏差。pro3 模式维持原对称边界口径不变（存量行为不受影响）。

### 13.5 三处 Pro3 解耦点

| 解耦点 | pro3 模式 | keltner 独立模式 |
|--------|-----------|------------------|
| **同向额度池** | B 计入 `targets[long_direction]` | B 计入 `targets[ctx['range_dir']]`；`range_dir` 为 None 时不占开仓额度；A 仍按 `long_direction` |
| **反转清理** | 随 Pro3 `long_dir_changed` 上升沿清 B（靠 `last_directions` 簿记） | 步骤 6b `_run_range_independent_cleanup`：**无状态**——若 B 当前持有与 `range_dir` 相反的仓即全平（`scene='reversal'`），幂等、重启不丢状态；清理失败置 `range_allow_entry=False` 下轮重试 |
| **反向持仓守卫** | B 全量受 Pro3 基准守卫 | `_detect_reverse_position(exclude_buckets=(BUCKET_RANGE,))`：B 的合法反向持仓仍计入 `owned` 抵扣真实净持仓（避免被误判为账本外人工单），但**不列入 ledger 冲突仓**（不强平）；A 趋势仓不受 exclude 影响 |

> **`exclude_buckets` 的陷阱**：不能简单在遍历篮子时 `continue` 跳过 B——那样 `owned` 不含 B 持仓，`unbooked=real−owned` 会把 B 的合法反向持仓当成"账本外人工反向单"强平。正确做法是 exclude 的 bucket **仍 `owned += held`**，只是不列入 ledger（`continue` 在 owned 累加之后）。

### 13.6 价格基准与一致性边界（日志字段）

- **回测**：触边即理想成交价（`open_price/close_price` = `_bar_levels` 触发价），无滑点无手续费。
- **实盘独立模式**：同一目标价挂**限价单**靠真实撮合成交；`_run_range_position` 打 `【区间独立挂单】方向=X 目标开仓价=… 目标平仓价=…（keltner_boll 触发价；限价挂单靠真实撮合成交，成交价/时间可能偏离目标）`，心跳行 `ctx['kb_text']` 记 `KB独立(indicator)=多/空 开…/平… ATR外扩 开A/平B(周期N)`。
- 本轮为**信号级一致**，不引入滑点补偿；如需逐笔复刻须走 paper-trading 镜像（列为未来可选项）。

### 13.7 配置项与前端

- `range_position.direction_source`：`'pro3'`（默认，缺省即视为 pro3）/ `'keltner'`。app.py 校验：取值白名单；`'keltner'` 须搭配 `strategy='keltner_boll'`，否则报错。
- `GET /api/task/range-strategy-options` 返回体新增 `direction_sources`（`[{value,label}]`）与 `default_direction_source:'pro3'`，前端下拉动态填充避免与后端漂移。
- task.html 仓位B `.kb-only` 区块新增"方向来源"下拉（`onRangeDirectionSourceChange` 联动：keltner 模式隐藏"方向确认过滤"行 `.kb-pro3-only`，并更新趋势方法 hint 文案）；加载回显读 `rpos.direction_source || 'pro3'`，收集提交写回该字段。
- 独立模式下 `entry_atr_mult / exit_atr_mult / atr_period` 经 `current_range_signal` 生效（含 `atr_period`，修正 pro3 模式外扩写死 period=14 的问题）。

### 13.8 冒烟测试

```bash
python crypto/_smoke_range_independent.py   # 50 通过 / 0 失败，退出码 0
```

覆盖：① 价位同源（`current_range_signal` == `_bar_levels` 最后一根短K；simulate 成交价反查 `_bar_levels`，多空各验）② 空头公式回归（0.7/0.3 非对称，锁死旧 bug）③ 方向一致性（signal.direction == simulate LONG_DIR 映射；中性→None）④ 调度接线离线（`_run_range_position` plan 来自信号、`exclude_buckets` 生效、`_run_range_independent_cleanup` 按 flip 触发）⑤ fail-safe（信号异常→`range_dir=None`、主调度不崩、B 不挂单）。隔离手法同 `_test_range_risk.py`（stub 持久化子模块 + `__new__` 裸实例）。

### 13.9 假设与遗留

- `direction_source` 缺省视为 `'pro3'`，现网存量配置零行为变更。
- 独立模式下 B 与 A 可能持相反方向，二者在交易所侧仍会**净持仓合并**——这是既有账本机制的固有特性，本方案只保证 B 的目标方向独立，不改变 A/B 同向合并为一份真实仓位的底层事实。
- **未做（遗留增强点）**：本轮不为 B 新增基于 keltner 方向的独立反向守卫。
- `current_range_signal` 用最新已收盘长K 方向（backward 对齐），与 simulate 口径一致；实时轮次可能读到未收盘短K 的边界值，属信号级一致可接受范围。
- **常驻进程需 reload**（端口 7777 Web + 交易调度）才线上生效；task.html 的 JS 须浏览器实测。
