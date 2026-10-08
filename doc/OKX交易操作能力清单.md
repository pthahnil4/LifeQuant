# OKX 交易操作能力清单

> **数据来源与核验方式**（本文件不靠记忆，全部实测/实抓核验）
> - `okx list-tools --json` —— `@okx_ai/okx-trade-cli@1.4.7` 本机实测导出，**totalTools=166 / 18 个 CLI 模块**，含每个工具的 `path`(CLI) + `toolName`(MCP) + 参数必填性与描述
> - `github.com/okx/agent-trade-kit` README + `docs/cli-reference.md` + `skills/okx-cex-trade/SKILL.md`(v1.4.7) + `skills/okx-cex-bot|portfolio/SKILL.md`
> - 本机 Python SDK 源码：`D:\Develop\Python\Python313\Lib\site-packages\okx\`（`python-okx==0.4.3`，`consts.py` 端点常量 + `Trade.py`/`Account.py`/`Grid.py` 方法签名，**清单中全部 SDK 方法已逐条实测存在**）
> - 本项目已有实证：`crypto/demo/demo03~demo44`（真实跑通过的下单/策略单/杠杆代码）
>
> **原始证据留档 + 一键重生成**：
> ```bash
> powershell -NoProfile -ExecutionPolicy Bypass -File data\okx_capability_dump.ps1   # Kit 侧
> python data\okx_sdk_probe.py                                                       # SDK 侧
> python data\okx_capability_check.py                                                # 本文档 ↔ 阅读页 一致性校验
> ```
> - `data/_okx_list_tools.json`（262 KB，官方机器可读全量 schema）
> - `data/_okx_tools_flat.txt`（18 模块 / **178 条 CLI 命令**扁平化：CLI path ‖ MCP tool ‖ 必填 ‖ 选填 ‖ 描述）
> - `data/_okx_param_detail.txt`（16 个关键下单/仓位/杠杆/机器人工具的逐参数说明）
>
> **本文件的网页版**：启动本项目 Web 后访问 `/okx-capability`（导航栏「📖 OKX 能力」）。
> 页面**直接读取本文件渲染**，不另存一份内容；表格/图示/工具矩阵由
> `crypto/capability_routes.py` 在服务端加工，图示定义在 `crypto/capability_diagrams.py`。

---

## 0. 先读懂这张表：三种接入方式的真实关系

这是本清单最重要的一节。不先厘清这层关系，后面的 ✅/❌ 会被误读。

```
OKX REST API v5  (真正的能力边界)
      ↑
  Python SDK (okx.*)            ← 覆盖面最广，含 Kit 未封装的原生接口
      ↑
  okx-trade-mcp  ═╗  同一套 core 包 (packages/core)，
  okx-trade-cli   ╝  同一版本号，同一参数 schema
      ↑
  okx-cex-* Skills  ← 不是新能力，是"给 Agent 看的 CLI 说明书 + 护栏"
```

| 接入方式 | 本体 | 能力边界 | 认证 |
|---|---|---|---|
| **MCP**（`okx-trade-mcp`） | 本地 stdio 进程 | 166 tools / 11 业务模块 | `~/.okx/config.toml` 的 profile |
| **CLI**（`okx-trade-cli`） | 终端可执行文件 `okx` | 与 MCP **一一对应**，仅命名风格不同 | 同上；或 OAuth (`okx config init`) |
| **Skills**（`okx-cex-*`） | Markdown 指令包 | **⊆ CLI**，`requires: bins:["okx"]`，靠 shell 调 CLI | 复用 CLI 认证 |

**结论 1（可省去大量纠结）**：MCP 与 CLI 之间**不存在能力差**。凡 MCP 有的工具 CLI 必有，反之亦然。真正需要甄别的是 **Kit（MCP/CLI/Skills） vs 原生 REST/SDK** 的缺口 —— 见第 7 节。

**结论 2（命名铁律，会踩坑）**：
- MCP 工具名 = 下划线：`swap_place_algo_order`
- CLI 命令 = 空格分层：`okx swap algo place`
- ❌ **绝不能**把 MCP 名拼成连字符命令：`okx swap place-algo` → `Unknown command`
- ⚠️ **CLI 子命令用空格，不是连字符** —— 这条官方文档专门用粗体警示，说明是高频错误

**结论 3（Skills 的真实价值不是能力而是护栏）**：Skill 里含金量最高的是流程约束，不是命令列表 —— 强制先查 `ctVal`、写操作前确认参数、写后回读校验、**错误信息建议的破坏性操作不得自动执行**。详见第 9 节。

**可用性图例**：
- `✅` = 在 v1.4.7 `list-tools` schema 或 Skill 命令索引中**实测确认存在**
- `🟡 SDK/REST only` = Kit 未封装，需直连 REST 或用 Python SDK
- `❌` = 未提供

---

## 1. 基础委托类型

### 1.1 现货 / 永续 / 交割 / 期权 的普通订单

REST 统一入口：`POST /api/v5/trade/order`（批量 `POST /api/v5/trade/batch-orders`）

| 委托类型 | `ordType` 取值 | 关键参数 | 说明 | MCP | CLI | Skills |
|---|---|---|---|---|---|---|
| 限价委托 | `limit` | `px` 必填 | 挂单等成交，最常用 | ✅ | ✅ | ✅ trade |
| 市价委托 | `market` | 禁传 `px` | 立即吃单成交 | ✅ | ✅ | ✅ trade |
| 只挂不吃（Maker） | `post_only` | `px` 必填 | 若会立即成交则被拒，省 taker 费 | ✅ | ✅ | ✅ trade |
| 全部成交或取消 | `fok` | `px` 必填 | 不能整单成交即全撤，不留部分仓 | ✅ | ✅ | ✅ trade |
| 立即成交剩余取消 | `ioc` | `px` 必填 | 吃多少算多少，余量自动撤 | ✅ | ✅ | ✅ trade |

**逐产品封装矩阵**（同一 REST 端点，Kit 按产品线拆成独立工具）：

| 产品线 | MCP 工具 | CLI 命令 | 必填参数 | SDK 方法 |
|---|---|---|---|---|
| 现货 | `spot_place_order` | `okx spot place` | `instId,tdMode,side,ordType,sz` | `Trade.place_order` |
| 永续 | `swap_place_order` | `okx swap place` | `instId,tdMode,side,ordType,sz` | `Trade.place_order` |
| 交割 | `futures_place_order` | `okx futures place` | 同上 | `Trade.place_order` |
| 期权 | `option_place_order` | `okx option place` | 同上 | `Trade.place_order` |
| 事件合约 | `event_place_order` | `okx event place` | `instId,side,outcome,sz` | 🟡 无封装 |

> 本项目实证：`demo03_限价单交易.py`、`demo04_市价单交易.py` → `tradeAPI.place_order(...)`

### 1.2 下单时的三种"数量语义"（高频出错点）

`sz` 的含义由 `tgtCcy` 决定，**这是合约下单最容易亏钱的一个参数**：

| `tgtCcy` | `sz` 含义 | 换算 | 典型话术 |
|---|---|---|---|
| `base_ccy`（默认） | **合约张数** | 每张 = `ctVal` 个标的 | "买 2 张" |
| `quote_ccy` | **名义价值** USDT | `sz ÷ (ctVal × lastPx)` | "开 500U 仓位" |
| `margin` | **保证金** USDT | `sz × lever ÷ (ctVal × lastPx)` | "拿 500U 保证金开 10 倍" |

- 现货买 USDT 金额：`okx spot place --instId SOL-USDT --side buy --ordType market --sz 10 --tgtCcy quote_ccy`
- ⚠️ **用户只说"500U"时是歧义**（名义价值 or 保证金成本？），官方 Skill 明确要求**必须先追问**，不允许替用户猜
- ⚠️ 每张合约的 `ctVal` **因品种而异**（BTC-USDT-SWAP=0.01 BTC，ETH-USDT-SWAP=0.1 ETH），**下单前必须先 `okx market instruments` 查**，不能凭印象
- `posSide`：单向净持仓填 `net`（默认）；双向对冲模式必须 `long`/`short`

### 1.3 主订单上直接"附带"止盈止损（一步下单）

普通下单参数里可直接挂 TP/SL，无需第二次调 algo 接口：

| 参数 | 含义 | MCP | CLI | Skills |
|---|---|---|---|---|
| `tpTriggerPx` / `tpOrdPx` | 止盈触发价 / 委托价（`-1`=市价） | ✅ | `--tpTriggerPx/--tpOrdPx` | ✅ |
| `slTriggerPx` / `slOrdPx` | 止损触发价 / 委托价（`-1`=市价） | ✅ | `--slTriggerPx/--slOrdPx` | ✅ |
| `tpOrdKind` | `condition`=触发式；**`limit`=立即挂限价止盈（无触发阶段）** | ✅ | `--tpOrdKind` | ✅ |
| `tpTriggerPxType`/`slTriggerPxType` | 触发价源：`last`(默认)/`index`/`mark` | ✅ | 同名 | ✅ |
| `stpMode` | 自成交保护：`cancel_maker`/`cancel_taker`/`cancel_both` | ✅ | `--stpMode` | ✅ |
| `clOrdId` | 自定义订单 ID | ✅ | `--clOrdId` | ✅ |

> 📌 **`clOrdId` 约束（本项目已踩过的坑）**：OKX 只接受**纯字母数字、1–32 位**，带 `-` `_` 等分隔符会导致下单被拒**且反查定性同时失效**。
> `aiBuilderCode`（1–16 位字母数字）是 v1.4.7 新增的 AI 归因字段，所有下单类工具均支持，可用于审计"这笔单是 AI 下的"。

示例（一步开多 BTC 永续并带止盈止损）：
```bash
okx swap place --instId BTC-USDT-SWAP --side buy --ordType market --sz 1 \
  --tdMode cross --posSide long \
  --tpTriggerPx 105000 --tpOrdPx=-1 --slTriggerPx 88000 --slOrdPx=-1
```

---

## 2. 策略委托类型（Algo Order）

REST 统一入口：`POST /api/v5/trade/order-algo`（本项目 `demo31~demo37` 用的就是这个）

### 2.1 七种策略委托全表

| # | 策略 | `ordType` | 中文常用名 | 专属参数 | MCP | CLI | Skills |
|---|---|---|---|---|---|---|---|
| 1 | `conditional` | 单向止盈止损 | 只设 TP **或** 只设 SL（也可两个都设） | `tpTriggerPx/tpOrdPx/slTriggerPx/slOrdPx` | ✅ | `okx swap algo place` | ✅ |
| 2 | `oco` | 双向止盈止损 | TP+SL 成对挂，**一边触发另一边自动失效** | 同上，必须成对 | ✅ | `okx swap algo place` | ✅ |
| 3 | `trigger` | 计划委托 | 到价才下单（突破单/回撤单） | `triggerPx`、`orderPx`(`-1`=市价)、`triggerPxType`、`advanceOrdType` | ✅ | `okx swap algo place` | ✅ |
| 4 | `move_order_stop` | 移动止盈止损 | 跟踪市价，回落 N% 触发（吊灯止损） | `callbackRatio` 或 `callbackSpread`、`activePx`(启动价) | ✅ | **`okx swap algo trail`** | ✅ |
| 5 | `chase` | 追逐限价委托 | 自动跟随盘口最优价，保持排队优势 | `chaseType`(`distance`档/`ratio`比例)、`chaseVal`、`maxChaseType`、`maxChaseVal`(追价上限) | ✅ | `okx swap algo place` | ✅ |
| 6 | `iceberg` | 冰山委托 | 大单**按数量梯度**拆小，隐藏真实规模 | `szLimit`(子单量)、`pxVar`/`pxSpread`、`pxLimit`、`timeInterval` | ✅ | `okx swap algo place` | ✅ |
| 7 | `twap` | 时间加权委托 | 大单**按时间维度**分批，逼近均价 | `szLimit`、`timeInterval`(秒)、`pxVar`/`pxSpread`、`pxLimit` | ✅ | `okx swap algo place` | ✅ |

> 🔴 **三处命名纠偏**（你的原始需求清单里有的名字和实际不一致，照抄会报参数错误）：
> 1. **`chase_limit` 不存在** → 实际 `ordType=chase`（本项目 `demo34` 文件名已写作 `chase`，是对的）
> 2. **网格不是策略委托** → `grid` **不走** `/api/v5/trade/order-algo`，它是**交易机器人** `/api/v5/tradingBot/grid/order-algo`，在 Kit 里属于独立的 `bot` 模块（见 §3.4）
> 3. **移动止盈止损有两条路** → 通用工具 `*_place_algo_order(ordType=move_order_stop)`，但 CLI **额外给了专用子命令** `okx swap algo trail`（MCP 工具名 `swap_place_move_stop_order`），参数更窄更不容易填错。**建议优先用 trail**

### 2.2 各产品线的策略委托覆盖度（差异很大，别一刀切）

| 产品线 | 策略委托 MCP 工具 | 支持的 ordType | 说明 |
|---|---|---|---|
| 永续 swap | `swap_place_algo_order` | **全部 7 种** | 覆盖最全，官方描述明列 "TP/SL, pending order, chase, iceberg, twap" |
| 现货 spot | `spot_place_algo_order` | **全部 7 种** | 同上 |
| 交割 futures | `futures_place_algo_order` | schema 参数与 swap **完全相同**（7 种齐备） | ⚠️ 但工具描述只写 "take-profit/stop-loss"，描述与参数表不一致 —— 以实测为准，用前先小仓验 |
| 期权 option | `option_place_algo_order` | **仅 TP/SL** | 参数只有 `tpTriggerPx/slTriggerPx` 一族，**无** trigger/chase/iceberg/twap |
| 事件合约 | ❌ | 无 | 只能普通下单 + 手动卖出平仓 |

**策略委托的生命周期工具**（每产品线一套，命名规律）：

| 操作 | MCP（以 swap 为例） | CLI | 必填 |
|---|---|---|---|
| 下策略单 | `swap_place_algo_order` | `okx swap algo place` | `instId,tdMode,side,ordType` |
| 移动止损（专用） | `swap_place_move_stop_order` | `okx swap algo trail` | `instId,tdMode,side,sz` |
| 改策略单 | `swap_amend_algo_order` | `okx swap algo amend` | `instId,algoId` |
| 撤策略单 | `swap_cancel_algo_orders` | `okx swap algo cancel` | `orders`（**数组**，注意是复数） |
| 查策略单 | `swap_get_algo_orders` | `okx swap algo orders` | 无（可 `--status/--ordType/--algoId` 过滤） |

> ⚠️ `*_cancel_algo_orders` 与 `*_amend_algo_order` 参数不对称：撤是**批量数组**入参，改是**单个** algoId。批量撤多个要构造 `orders=[{instId,algoId},...]`
> 🔴 `amend` 的**边界**：普通订单 amend 只能改价/量（`newPx/newSz/newClOrdId`），**改 TP/SL 必须走 `algo amend`**。CLI 帮助里明确写了这句提示，说明又是高频坑
> SDK 对应：`Trade.place_algo_order` / `amend_algo_order` / `cancel_algo_order` / `order_algos_list` / `order_algos_history` / `get_algo_order_details`

### 2.3 按比例的"条件性全平"

`closeFraction` —— 只用于 `conditional`/`oco`，**唯一取值 `"1"`（全平）**，替代 `sz`。
- 当 `posSide=net` 时**必须同时** `reduceOnly=true`
- `cxlOnClosePos=true` → 关联持仓被平掉时，这套 TP/SL 自动撤销（避免留下"孤儿止损单"）

> 本项目已有「手动平仓后账本自动清零」相关机制，`cxlOnClosePos` 正是交易所侧的对应保险，建议实盘默认置真。

---

## 3. 高级交易功能：拆单与算法策略

这一节直接回答你需求里的"均匀梯度下单 / 按时间维度分批 / 按价格梯度阶梯"。**OKX 没有一个叫这些名字的原生类型**，但有三条正交实现路径，且**各自能力边界不同**：

### 3.1 需求 → 实现 映射表（先查这张，再往下看细节）

| 你的诉求 | 原生最优解 | 层次 | MCP | CLI | Skills | 备注 |
|---|---|---|---|---|---|---|
| **均匀梯度下单**（把大单切成等量小单，不暴露规模） | `ordType=iceberg` + `szLimit` | 交易所撮合层 | ✅ | `okx swap algo place --ordType iceberg` | ✅ | 官方名"冰山委托"；**无固定批次数**，靠 `sz/szLimit` 自然切分 |
| **按时间维度分批建仓/平仓** | `ordType=twap` + `szLimit` + `timeInterval` | 交易所撮合层 | ✅ | `okx swap algo place --ordType twap` | ✅ | 每 `timeInterval` 秒下一张子单，`pxVar/pxSpread` 控让价 |
| **按时间维度定额定投建仓** | **Recurring Buy 定投计划** | 交易所调度层 | ❌ | ❌ | ❌ | 🟡 **REST/SDK only**：`/api/v5/tradingBot/recurring/*`，Kit 完全未封装 |
| **按价格梯度阶梯下单**（如 -3%/-6%/-9% 三档挂多） | `batch-orders` 手工拼多档限价 | 一次请求多单 | ✅ | `okx swap batch place orders.json` | ✅ | 最灵活，自己算梯度；单次上限 **20 笔** |
| **按价格梯度阶梯加仓（马丁格尔）** | **DCA Bot** + `pxSteps`/`pxStepsMult`/`volMult` | 交易所机器人层 | ✅ | `okx bot dca create` | ✅ bot | 越跌越买，梯度**自动放大**，无需自己维护 |
| **按价格梯度网格反复吃波动** | **Grid Bot** + `gridNum`/`runType` | 交易所机器人层 | ✅ | `okx bot grid create` | ✅ bot | 等差(1)/等比(2) 网格 |
| **追单不缺席**（保证排队优势） | `ordType=chase` | 交易所撮合层 | ✅ | `--ordType chase` | ✅ | 有 `maxChaseVal` 兜底，不会无限追价 |

### 3.2 冰山委托 iceberg（最贴近"均匀梯度"）

参数集与 twap 共用一族：

| 参数 | 含义 | 约束 |
|---|---|---|
| `sz` | 母单总量 | — |
| `szLimit` | **每笔子单数量**（梯度粒度） | iceberg/twap 共用 |
| `pxVar` | 让价比例 | **区间 `[0.0001, 0.01]`**，与 `pxSpread` 二选一 |
| `pxSpread` | 让价绝对价差 | `>= 0`，与 `pxVar` 二选一 |
| `pxLimit` | 价格天花板（买）/地板（卖） | `>= 0`；越过即停 |
| `timeInterval` | 子单间隔秒数 | iceberg 亦可设，用来摊薄节奏 |

```bash
# 冰山：总量 5 张，每笔子单 0.5 张，让价 0.1%
okx swap algo place --instId BTC-USDT-SWAP --tdMode cross --side buy \
  --ordType iceberg --sz 5 --szLimit 0.5 --pxVar 0.001 --pxLimit 100000
```

### 3.3 TWAP（按时间维度的分批建仓/平仓）

本项目 `demo37_时间加权委托(twap).py` 已完整实证，参数完全对应：

| 参数 | demo37 中的常量 | 作用 |
|---|---|---|
| `szLimit` | `SZ_LIMIT="0.2"` | 单笔执行数量（实盘最小 0.2 张） |
| `timeInterval` | `TIME_INTERVAL="120"` | 执行间隔（秒） |
| `pxVar` / `pxSpread` | `PX_VAR="0.001"` | 价格优势（比例 或 价距，二选一） |
| `pxLimit` | 由 `PX_LIMIT_OFFSET_PCT` 偏移算出 | 价格限制条件 |

- 预估批次 = `sz / szLimit`；预估时长 = 批次 × `timeInterval`
- ⚠️ **demo37 记录的实测坑**：TWAP 在某些环境下 `tdMode` 需用 **`isolated`（逐仓）**，`cross` 会被拒
- 平仓方向同理：`--side sell --posSide long --reduceOnly` 即"按时间维度分批平仓"

### 3.4 机器人层：Grid 与 DCA（真·阶梯/网格）

Grid **14 个工具**、DCA **5 个**，全部在 `bot` 模块（Skill：`okx-cex-bot`）：

| 操作 | MCP | CLI | 必填 |
|---|---|---|---|
| 创建网格 | `grid_create_order` | `okx bot grid create` | `instId,algoOrdType,maxPx,minPx,gridNum` |
| 修改网格 | `grid_amend_order` | `okx bot grid amend` | `algoId` |
| 停止网格 | `grid_stop_order` | `okx bot grid stop` | `algoId,algoOrdType,instId` |
| 网格列表/详情/子单 | `grid_get_orders` / `grid_get_order_details` / `grid_get_sub_orders` | `okx bot grid orders/details/sub-orders` | `algoOrdType`(+`algoId`) |
| 网格持仓 | `grid_get_positions` | `okx bot grid positions` | `algoId,algoOrdType` |
| **预估强平价** | `grid_get_liquidate_price` | `okx bot grid liquidate-price` | `instId,sz,lever,direction,maxPx,minPx,gridNum` |
| 停后残留仓平掉 | `grid_close_position` | `okx bot grid close-position` | `algoId,mktClose` |

- `algoOrdType`：`grid`(现货网格) / `contract_grid`(合约网格) / `moon_grid`(月网格，**只读**，不能创建)
- `runType`：`1`=等差（均匀价格梯度）/ `2`=等比
- 现货网格投入：`quoteSz`(计价币) **或** `baseSz`(交易币)；合约网格：`sz`(保证金) + `direction`(`long`/`short`/`neutral`) + `lever` + `basePos`(是否开底仓)
- `stopType`：现货 `1`=全卖 / `2`=保留币；合约 `1`=平仓并停 / `2`=保留仓并停
- 网格可改：`maxPx/minPx/gridNum/tpTriggerPx/slTriggerPx/tpRatio/slRatio/topUpAmt`(**追加保证金**)
- 📌 本项目 `demo38` 已注明：SDK 的 `GridAPI.grid_order_algo` **未暴露** `algoClOrdId/triggerParams/tpRatio/slRatio`，要用得扩 SDK 或直连 REST

DCA（马丁格尔，即"价格梯度自动放大加仓"）：
```bash
okx bot dca create --instId BTC-USDT-SWAP --lever 3 --direction long \
  --initOrdAmt 100 --safetyOrdAmt 50 --maxSafetyOrds 3 \
  --pxSteps 0.03 --pxStepsMult 1 --volMult 1 --tpPct 0.03 --slPct 0.15 --slMode market
```
- `algoOrdType`：`spot_dca` / `contract_dca`（必填，决定走现货还是合约分支；`lever` 仅 `contract_dca` 需要）
- 必填五件套：`instId, algoOrdType, direction, initOrdAmt, maxSafetyOrds, tpPct`
- `maxSafetyOrds`：**`0`=纯定投不补仓，上限 100 次补仓**
- `pxSteps`=补仓价格步长（`0.03`=跌 3% 补一次），`pxStepsMult`=**步长倍率**（梯度越来越疏/密），`volMult`=**加仓量倍率**
- ⚠️ `maxSafetyOrds > 0` 时 `safetyOrdAmt/pxSteps/pxStepsMult/volMult` **变为必填**
- `triggerStrategy` 取值**两分支不同**：`contract_dca`=`instant|price|rsi`，`spot_dca`=**只有** `instant|rsi`（现货无价格触发）；配 `triggerPx`(price)、`thold`/`timeframe`/`timePeriod`(RSI，默认周期 14)
- `allowReinvest` 默认 `true`（盈利再投入），`reserveFunds` 默认 `'true'`，`slMode` 可 `market`
- ⚠️ 官方 CLI 示例里 `okx bot dca create` **没有**显式传 `--algoOrdType`（推测由 `instId` 后缀推断），但 schema 标为 required —— **显式带上 `--algoOrdType contract_dca` 最保险**

### 3.5 手工阶梯：batch-orders

`spot_batch_orders` / `swap_batch_orders` / `futures_batch_orders`，一个 `action` 参数三合一（`place`/`amend`/`cancel`），**单次上限 20 笔**。

```bash
# ladder.json: 三档阶梯建仓
[{"instId":"BTC-USDT-SWAP","tdMode":"cross","side":"buy","ordType":"limit","posSide":"long","sz":"1","px":"97000"},
 {"instId":"BTC-USDT-SWAP","tdMode":"cross","side":"buy","ordType":"limit","posSide":"long","sz":"1","px":"94000"},
 {"instId":"BTC-USDT-SWAP","tdMode":"cross","side":"buy","ordType":"limit","posSide":"long","sz":"1","px":"91000"}]
okx swap batch place ladder.json --json
```
> 🔴 **脚本必读的坑**：批量只要**有一笔** `sCode != "0"`，整条命令 **exit code = 1**。不能用退出码判断"全成功/全失败"，**必须逐条 parse JSON 看每笔的 `sCode`**。
> SDK：`place_multiple_orders` / `cancel_multiple_orders` / `amend_multiple_orders`

### 3.6 Kit 未封装的算法能力：定投 Recurring Buy

- `okx skill` 里没有，`list-tools` 里也没有 —— **`Trade`/`Grid` 之外唯一线索在 SDK**
- SDK `Grid.py`：`place_recurring_buy_order` / `amend_recurring_buy_order` / `stop_recurring_buy_order` / `get_recurring_buy_order_list` / `get_recurring_buy_order_details` / `get_recurring_buy_sub_orders`
- 端点：`/api/v5/tradingBot/recurring/order-algo`、`amend-order-algo`、`stop-order-algo`、`orders-algo-pending`、`orders-algo-history`、`order-algo-details`、`sub-orders`
- **适用**：每周/每 N 天固定金额建仓（真正的时间维度均匀建仓），比自建 cron + CLI 更稳（挂在交易所，本机断电不影响）

### 3.7 关于"其他智能拆单"的边界澄清

OKX **v5 个人 API 不提供**机构级算法（VWAP、POV、Iceberg-with-randomization、暗池、嗅探规避等）。上表 7 种 `ordType` + Grid + DCA + batch 已是**全集**，不要再往清单里加"VWAP"之类的想当然条目。

---

## 4. 仓位管理操作

### 4.1 开仓 / 加仓 / 减仓 / 平仓

| 操作 | 实现方式 | MCP | CLI | Skills | REST |
|---|---|---|---|---|---|
| **开仓** | `place` 带方向 | ✅ | `okx swap place` | ✅ | `/api/v5/trade/order` |
| **加仓** | **同向再下一笔**（OKX 无"加仓"独立接口，同向单即自动累加） | ✅ | `okx swap place` | ✅ | `/api/v5/trade/order` |
| **部分减仓** | `place` + `reduceOnly=true`（单向模式必带） | ✅ | `--reduceOnly` | ✅ | `/api/v5/trade/order` |
| **市价全平（单笔持仓）** | 专用平仓接口 | ✅ `swap_close_position` | `okx swap close` | ✅ | `/api/v5/trade/close-position` |
| **条件全平** | `algo place --ordType conditional --closeFraction 1` | ✅ | `okx swap algo place` | ✅ | `/api/v5/trade/order-algo` |
| **分批/梯度平仓** | `twap` / `iceberg` + `reduceOnly` | ✅ | `okx swap algo place` | ✅ | `/api/v5/trade/order-algo` |
| **一键全平 + 顺带撤该品种挂单** | `close` + `autoCxl=true` | ✅ | `--autoCxl` | ✅ | `/api/v5/trade/close-position` |
| **网格停后清残仓** | `grid_close_position` | ✅ | `okx bot grid close-position` | ✅ bot | `/api/v5/tradingBot/grid/close-position` |
| 期权批量撤单 | `option_batch_cancel` | ✅ | `okx option batch-cancel` | ✅ | `/api/v5/trade/cancel-batch-orders` |

- `close` 必填 `instId, mgnMode`；对冲模式必须给 `posSide`，单向净持仓省略即可
- ⚠️ **`reduceOnly` 在单向(`net`)模式下是"只减不增"的保险栓**；对冲模式下用 `posSide` + `side` 反向表达平仓语义
- ⚠️ **没有 `cancel-all-orders`（撤回全部挂单）** —— Kit 和 SDK 都没有。只能 `orders` 查列表 → `batch cancel` 分批撤（每批 ≤20）
- 本项目实证：`demo26_减仓平仓交易.py`、`demo27_市价减仓平仓交易.py`（均用 `place_order` + reduceOnly 路线）

### 4.2 杠杆设置与管理

| 操作 | MCP | CLI | 必填 | Skills |
|---|---|---|---|---|
| 设永续杠杆 | `swap_set_leverage` | `okx swap leverage --lever 10 --mgnMode cross` | `instId,lever,mgnMode` | ✅ |
| 查永续杠杆 | `swap_get_leverage` | `okx swap get-leverage` | `instId,mgnMode` | ✅ |
| 设交割杠杆 | `futures_set_leverage` / `futures_get_leverage` | `okx futures leverage` / `get-leverage` | 同上 | ✅ |
| 设**现货保证金**杠杆 | `spot_set_leverage` | `okx spot leverage` | `lever,mgnMode`（+`instId` **或** `ccy`） | ✅ |
| **批量**设杠杆（多品种一次） | ❌ | ❌ | — | — |
| 查最大可用开仓量 | `account_get_max_size` / `account_get_max_avail_size` | `okx account max-size` / `max-avail-size` | `instId,tdMode` | ✅ |

> 本项目实证：`demo24_设置杠杆倍数.py`、`demo24_批量设置杠杆倍数.py`、`demo25_查询杠杆倍数.py` → `Account.set_leverage` / `get_leverage`（端点 `/api/v5/account/set-leverage`、`/api/v5/account/leverage-info`）。**批量版靠同一端点传 `instIds` 数组实现，SDK 有、Kit 无**。
>
> 🔴 **设杠杆的三个实测拦路虎**（来自官方 Skill 排障顺序，照做可省一大截调试时间）：
> 1. `lever` 必须是**正数且 ≤ 品种上限**（先 `okx market instruments` 看 `lever` 字段）
> 2. 对冲模式下 `mgnMode=isolated` 时 **`posSide` 必填**，且 `long`/`short` **必须分别各调一次**，设一边不会自动同步另一边
> 3. **组合保证金(PM)账户不能调 SWAP/FUTURES 的 `cross` 杠杆**，OKX 直接拒；动手前先 `okx account config` 看 `acctLv`
> - 若报"请撤单或停策略"：**按顺序**先查 `algo orders --status pending`（最常见真凶），无 pending 再查 `bot grid orders`。**不要盲目撤单/停机器人**

### 4.3 保证金与账户结构

| 操作 | MCP | CLI | SDK / REST | 状态 |
|---|---|---|---|---|
| 切持仓模式（单向/双向） | ✅ `account_set_position_mode` | ✅ `okx account set-position-mode --posMode net_mode\|long_short_mode` | `Account.set_position_mode` → `/api/v5/account/set-position-mode` | Kit ✅ |
| 资金账户↔交易账户划转 | ✅ `account_transfer` | ✅ `okx account transfer` | — | Kit ✅ |
| **手动加减保证金** | ❌ | ❌ | `Account.adjustment_margin` → `/api/v5/account/position/margin-balance` | 🟡 SDK only |
| 全仓隔离（多空隔离） | ❌ | ❌ | `Account.set_isolated_mode` → `/api/v5/account/set-isolated-mode` | 🟡 SDK only |
| 网格追加/提取保证金 | ❌ | ❌ | `Grid.grid_adjust_margin_balance` → `/api/v5/tradingBot/grid/margin-balance`；预览用 `grid_compute_margin_balance` | 🟡 SDK only |
| 网格追加保证金（走 amend） | ✅ `grid_amend_order --topUpAmt` | ✅ | — | Kit ✅ |
| 保证金模拟试算 | ❌ | ❌ | `/api/v5/account/simulated_margin` | 🟡 SDK only |
| 借币/还币 | ❌ | ❌ | `Account.borrow_repay` → `/api/v5/account/borrow-repay` | 🟡 SDK only |
| 一键还币 | ❌ | ❌ | `Trade.oneclick_repay(_v2)` | 🟡 SDK only |

> ⚠️ **v5 不存在 `set-margin-mode` 端点**（那是 v3 时代的东西）。全仓/逐仓在 v5 是**每笔订单的 `tdMode` 参数**（`cross`/`isolated`/`cash`），不是账户级开关。凡清单里写"切换保证金模式端点"的都是过时资料。
> 本项目实盘策略以 `isolated`（逐仓）为主，`demo37` 更记录 TWAP 需 `isolated` —— 改杠杆/下单前务必确认 `tdMode` 与已有持仓一致。

---

## 5. 订单生命周期管理

### 5.1 全生命周期矩阵（每产品线一套，命名完全规律）

| 阶段 | 现货 | 永续 | 交割 | 期权 | 事件合约 |
|---|---|---|---|---|---|
| **提交** | `spot_place_order` | `swap_place_order` | `futures_place_order` | `option_place_order` | `event_place_order` |
| **批量提交** | `spot_batch_orders(action=place)` | `swap_batch_orders` | `futures_batch_orders` | — | — |
| **改** | `spot_amend_order` | `spot_amend_order`(别名) | `futures_amend_order` | `option_amend_order` | `event_amend_order` |
| **撤** | `spot_cancel_order` | `swap_cancel_order` | `futures_cancel_order` | `option_cancel_order` | `event_cancel_order` |
| **批量撤** | `*_batch_orders(action=cancel)` | 同 | 同 | `option_batch_cancel` | — |
| **查单笔** | `spot_get_order` | `swap_get_order` | `futures_get_order` | `option_get_order` | — |
| **查挂单/历史列表** | `*_get_orders(status=open\|history)` | 同 | 同 | 同 | `event_get_orders` |
| **查成交明细** | `*_get_fills` | 同 | 同 | 同 | `event_get_fills` |
| **查策略单** | `*_get_algo_orders` | 同 | 同 | 同 | — |
| **归档(>7天/3月)** | `spot_get_fills(archive)` | `swap_get_fills --archive` | 同 | `option_get_orders` archive | 同 |

CLI 对应关系（Skill 命令索引实测共 **61** 条交易命令：spot 12 + swap 15 + futures 15 + option 10 + event 9）：
```bash
okx spot orders | get | fills | amend | cancel | place | batch | algo orders
okx swap positions | orders | get | fills | amend | cancel | place | close | leverage | get-leverage | batch | algo orders
okx account bills | bills --archive | positions | positions-history | balance | balance-all | config | fees | audit
```

### 5.2 状态跟踪的正确姿势（Skill 强制流程，值得抄进本项目）

| 写完操作后 | 必须回读校验 |
|---|---|
| `place` | `okx spot orders`（限价确认挂单存活）/ `okx spot fills`（市价确认已成交） |
| `swap place` | `okx swap orders` 或 `okx swap positions` |
| `swap close` | `okx swap positions` → 确认 size 归 **0** |
| `algo place` / `algo trail` | `okx swap algo orders` → 确认 algo 为 active |
| `cancel` | 对应 `orders` → 确认单已消失 |
| `leverage` | `okx swap get-leverage` |

- **`--history` 语义**：`orders` 默认返回**活动/挂单**；只有用户明确要历史时才加 `--history`（否则 Agent 会拿到空列表还自信汇报"无挂单"）
- **分页游标**：`--after` / `--before` / `--limit`（OKX v5 是**时间戳游标**，不是 offset）
- **归档窗口**：`bills --archive` 覆盖 7 天前～最多 3 个月；更久需 `orders-history-archive`
- SDK：`get_order` / `get_order_list` / `get_orders_history` / `get_orders_history_archive` / `get_fills` / `get_fills_history`（本项目 `demo18/19/22` 已用）
- 私有 WebSocket 推送：`okx.websocket.WsPrivateAsync` 可订阅 `orders`/`account` 频道做实时状态跟踪；**但该 SDK 未封装 WS 下单**，下单仍走 REST。MCP/CLI 亦无 WS 通道。

### 5.3 错误返回的可机读性（Agent 场景关键）

MCP 报错返回结构化块（含 `code`/`endpoint`/`traceId`），CLI 报错带 `Hint` 与版本号：
```json
{"tool":"swap_place_order","error":true,"type":"OkxApiError","code":"51020",
 "message":"Order quantity invalid","endpoint":"POST /api/v5/trade/order","traceId":"...","serverVersion":"1.0.4"}
```
> 本项目已踩过 `51020`（数量非法）与 `510xx` 参数校验类错误 —— 这套结构化错误比裸 HTTP 响应好用得多，值得在自研告警里复用 `code + traceId` 双字段。

---

## 6. 前置查询能力（交易决策的输入）

**模块 `market`（20 工具，无需 API Key）** —— 下单前该查的都在这里：

| 用途 | MCP | CLI |
|---|---|---|
| 最新价 | `market_get_ticker` | `okx market ticker BTC-USDT` |
| 全市场报价 | `market_get_tickers` | `okx market tickers SPOT` |
| **盘口深度** | `market_get_orderbook` | `okx market orderbook BTC-USDT --sz 5` |
| K 线 / 历史K线 | `market_get_candles` | `okx market candles --bar 1H --limit 10` |
| **合约规格（`ctVal`/`minSz`/`lever`）** | `market_get_instruments` | `okx market instruments --instType SWAP` |
| 资金费率（当前/历史） | `market_get_funding_rate` | `okx market funding-rate BTC-USDT-SWAP [--history]` |
| 标记价格 | `market_get_mark_price` | `okx market mark-price --instType SWAP` |
| 逐笔成交 | `market_get_trades` | `okx market trades BTC-USDT` |
| 指数行情/K线 | `market_get_index_ticker` / `index_candles` | `okx market index-ticker/index-candles` |
| **涨跌价格限制** | `market_get_price_limit` | `okx market price-limit BTC-USDT-SWAP` |
| 持仓量 OI / 历史 / 变化榜 | `market_get_open_interest` / `oi_history` / `market_filter_oi_change` | `okx market open-interest` / `oi-history` / `oi-change` |
| **多维筛选器**（价/幅/市值/量/OI/费率） | `market_filter` | `okx market filter --instType SPOT ...` |
| 跨品种价差统计（含时点回溯） | `market_get_pair_spread` | `okx market pair-spread A B [--backtest-time <ms>]` |
| **70+ 技术指标（免鉴权）** | `market_get_indicator` / `market_list_indicators` | `okx market indicator rsi BTC-USDT-SWAP [--list --limit N]` |
| 股票通证 / 金属 / 商品 / 外汇 / 债券 | `market_get_stock_tokens`(已废弃) / `market_get_instruments_by_category` | `okx market instruments-by-category --instCategory 4\|5\|6\|7` |

> 💡 `market indicator` **服务端直接算指标且免鉴权**，还支持 `--backtest-time` 取"某历史时点当时可见的指标值"。这一点对**防未来函数回测**极有价值 —— 比本项目现在"拉 K 线 → Talib 本地算"的链路多了一条可交叉验证的独立口径。
> ⚠️ 官方说明：**指标接口不支持 `1m` 周期**。
> 本项目 `demo34` 已用 `get_orderbook` 做追逐价计算；`demo08/09/10/11/12` 覆盖 K 线与 Talib 本地指标。

**模块 `account`（14 工具，Skill `okx-cex-portfolio` 仅索引 13 命令）** —— 余额/持仓/账单：
`account_get_balance` / `balance_all`(一次性交易+资金+估值快照) / `asset_balance` / `positions` / `positions_history` / `bills`(+archive) / `trade_fee` / `config` / `max_size` / `max_avail_size` / `max_withdrawal` / `transfer` / `set_position_mode` / `trade_get_history`(审计)

**辅助情报模块**：`news`(12，含**宏观经济日历** GDP/CPI/NFP/FOMC)、`smartmoney`(10，聪明钱榜/共识信号/交易员持仓)、`skill`(7，市场搜索安装 Skill)、`earn`(26，余值资金)、`event`(9)、`option`(14)

---

## 7. ⚠️ Kit 能力缺口清单（必须走 SDK / 裸 REST）

这是本清单的**核心价值**：以下操作在 `okx-trade-mcp` / `okx-trade-cli` / Skills 里**根本不存在**，别在 Agent 提示词里承诺"用 MCP 就能做"。

| 缺口操作 | REST 端点 | Python SDK 方法 | 影响 |
|---|---|---|---|
| **定投计划 Recurring Buy** | `/api/v5/tradingBot/recurring/*` | `place_recurring_buy_order` 等 7 个 | 时间维度自动定额建仓只能自建或走 SDK |
| **手动加减保证金** | `/api/v5/account/position/margin-balance` | `Account.adjustment_margin` | 逐仓补保证金保持仓，Agent 做不了 |
| **撤回全部挂单** | v5 无此端点，Kit 亦无 | — | 应急清场只能"查列表 + 分批撤（≤20/批）" |
| 全仓隔离模式 | `/api/v5/account/set-isolated-mode` | `Account.set_isolated_mode` | 低频 |
| 网格保证金追加/提取 | `/api/v5/tradingBot/grid/margin-balance` | `Grid.grid_adjust_margin_balance` | 部分被 `grid amend --topUpAmt` 覆盖 |
| 批量设置杠杆（多品种一次） | `/api/v5/account/set-leverage` + `instIds` | `Account.set_leverage(instIds=...)` | 只能逐品种循环调用 |
| 借币 / 还币 / 一键还币 | `/api/v5/account/borrow-repay`、`/api/v5/trade/one-click-repay*` | `Account.borrow_repay`、`Trade.oneclick_repay_v2` | 低频 |
| 保证金试算 | `/api/v5/account/simulated_margin` | `Account.get_simulated_margin` | 组合保证金压力测试 |
| 希腊值(PM)设置 | `/api/v5/account/set-greeks` | `Account.set_greeks` | 期权 PM 账户 |
| 跟单交易 CopyTrading | `/api/v5/copytrading/*` | `CopyTrading.py` | Kit 未封装 |
| 价差交易 Spread Trading | `/api/v5/spread/*` | `SpreadTrading.py` | Kit 未封装 |
| 大宗交易 Block Trading | `/api/v5/blockTrading/*` | `BlockTrading.py` | Kit 未封装 |
| 兑换 Convert / 闪兑 | `/api/v5/asset/convert/*` | `Convert.py`、`Trade.easy_convert` | Kit 仅有现货/合约下单 |
| 子账户 / 资金资产 | `/api/v5/account/subaccount/*`、`/api/v5/asset/*` | `SubAccount.py`、`Funding.py` | Kit 仅有 `account transfer` |
| **WebSocket 直接下单** | WS `buy`/`sell` op | 未封装（仅有 `WsPrivateAsync` 订阅基类） | 低延迟路径需自研 |

**已确认的口径矛盾（用前须实测）**：
1. `list-tools` 实测 **166** 工具，README 宣称 **"167 tools across 11 modules"** —— 数字对不上，README 略旧。且 166 既不是命令数也不是工具名数：CLI 命令共 **178** 条，其中 12 条无 `toolName`（纯本地），剩下 **166** 条才是官方口径的"工具数"，而这 166 条去重后只对应 **160** 个不同 `toolName`。三个数的换算见 **附录 B.1**
2. `futures_place_algo_order` 的**参数表**与 swap 完全一致（7 种 ordType 齐备），但**工具描述**只写 "take-profit/stop-loss" —— 描述与 schema 不一致，交割单是否真支持 iceberg/twap 需小仓验
3. 期权有 `option algo place/amend/cancel`，但**普通期权单不允许附带 TP/SL**（官方 Skill 明写 "do NOT attach TP/SL"）；且期权 algo 参数**只有 TP/SL 一族**，无 trigger/chase/iceberg/twap
4. `okx swap amend` 在 schema 里 `toolName` 复用为 `spot_amend_order`（服务端别名），MCP 侧**不存在** `swap_amend_order` 这个名字 —— **别按产品线机械拼 MCP 工具名**

---

## 8. 意图 → 一条命令 速查

```bash
# —— 基础 ——
okx spot place --instId BTC-USDT --side buy --ordType market --sz 0.01
okx spot place --instId SOL-USDT --side buy --ordType market --sz 10 --tgtCcy quote_ccy   # 花 10 USDT
okx swap place --instId BTC-USDT-SWAP --side buy --ordType market --sz 1000 --tgtCcy quote_ccy --tdMode cross --posSide long
okx swap place --instId ETH-USDT-SWAP --side sell --ordType limit --sz 1 --px 3000 --tdMode isolated --posSide short

# —— 改 / 撤 / 查 ——
okx swap amend --instId BTC-USDT-SWAP --ordId <id> --newPx 95000
okx swap cancel --instId BTC-USDT-SWAP --ordId <id>
okx swap orders --status open --instId BTC-USDT-SWAP
okx swap fills --instId BTC-USDT-SWAP --json | jq '.[] | {px, sz, side}'

# —— 策略委托 ——
okx swap algo place --ordType conditional --tpTriggerPx 105000 --tpOrdPx=-1                # 单向止盈
okx swap algo place --ordType oco --tpTriggerPx 105000 --tpOrdPx=-1 --slTriggerPx 88000 --slOrdPx=-1
okx swap algo place --ordType trigger --triggerPx 100000 --orderPx=-1                      # 计划委托
okx swap algo trail --callbackRatio 0.02 --activePx 95000                                  # 移动止损
okx swap algo place --ordType chase --chaseType distance --chaseVal 0.5 --maxChaseVal 5    # 追逐限价
okx swap algo place --ordType iceberg --sz 5 --szLimit 0.5 --pxVar 0.001                   # 冰山/均匀梯度
okx swap algo place --ordType twap --sz 0.9 --szLimit 0.2 --timeInterval 120 --pxVar 0.001 --tdMode isolated
okx swap algo orders --status pending
okx swap algo cancel --orders '[{"instId":"BTC-USDT-SWAP","algoId":"590xxx"}]'

# —— 仓位 / 杠杆 / 账户 ——
okx swap close --instId BTC-USDT-SWAP --mgnMode cross --posSide long --autoCxl
okx swap leverage --instId BTC-USDT-SWAP --lever 10 --mgnMode cross
okx account set-position-mode --posMode long_short_mode

# —— 阶梯 / 机器人 ——
okx swap batch place ladder.json --json | jq '.[] | select(.sCode != "0")'
okx bot grid create --instId BTC-USDT-SWAP --algoOrdType contract_grid --maxPx 100000 --minPx 80000 --gridNum 10 --direction neutral --lever 3 --sz 100
okx bot grid liquidate-price --instId BTC-USDT-SWAP --sz 100 --lever 3 --direction long --maxPx 100000 --minPx 80000 --gridNum 10
okx bot dca create --instId BTC-USDT-SWAP --direction long --lever 3 --initOrdAmt 100 --maxSafetyOrds 3 --tpPct 0.03 --pxSteps 0.03 --pxStepsMult 1 --volMult 1 --safetyOrdAmt 50

# —— 下单前必查（防错） ——
okx market instruments --instType SWAP --instId BTC-USDT-SWAP   # ctVal / minSz / lever 上限
okx market price-limit BTC-USDT-SWAP
okx account balance-all
```

---

## 9. 安全护栏（写实盘之前必读）

| 机制 | 用法 | 说明 |
|---|---|---|
| 只读模式 | `okx-trade-mcp --read-only` | 只暴露查询工具，**写工具不注册** |
| 模块裁剪 | `okx-trade-mcp --modules market` / `spot,account` | 默认仅 `spot,swap,account`；不给 `bot` 就打不了机器人 |
| 模拟盘 | `okx --demo ...` 或 `--profile <demo>` | OAuth 用 `--demo`；API Key 用 demo profile（**两种认证切换方式不同**） |
| 限速 | 内建 rate limiter | 下单类 **60 ops / 2s / UID**；网格 `order-algo` 20 次/2s |
| 部分失败 | exit code 只有 `0`/`1` | 批量**任一笔**失败即 1，必须 parse JSON 看逐笔 `sCode` |
| 凭据 | 全本地 `~/.okx/config.toml`，keys 不出机 | **绝不在对话里接受凭据**（官方 Skill 明文规定） |

**官方 Skill 强制的两条铁律，建议直接吸收进本项目实盘闸门：**
1. **错误信息里"建议的补救写操作"不得自动执行** —— 必须先原文上报 → 只读诊断 → 呈现发现 → **等用户明确确认**。理由：错误文案会**笼统列出所有可能阻塞项**，真凶常只有一件，照抄会导致不必要的平仓/停机器人。
2. **写操作前一次性确认关键参数**（`instId/side/ordType/sz/tdMode`，金额语义还要额外确认 notional vs margin），**写后立即回读校验**。

**与本项目现状对接的提醒：**
- 本项目已有 `web_auth` 口令闸门（fail-safe：缺配置=关闭远程而非无防护）。MCP/CLI 是**绕过前端 Web、直连交易所**的新通道，权限模型完全独立 —— 引入前必须单独规划 API Key 权限位（建议先只读 Key）、profile 隔离、以及实盘/模拟盘默认档位。
- Kit 的 `--read-only` + `--modules` 正对应本项目"主交易循环与监控告警进程隔离"的思路：监控/分析型 Agent 只挂 `market,account`，执行型才挂 `spot,swap`。
- 实盘写通道接入 Agent 前，先把 `--demo` 全链路跑通（本项目 `flag` 配置已是这个语义）。

---

## 10. 本项目 demo 覆盖矩阵（已有实证 vs 清单）

| 能力 | 本项目实证文件 | 状态 |
|---|---|---|
| 限价 / 市价下单 | `demo03` / `demo04` | ✅ 已跑通 |
| 订单修改 / 撤销 | `demo05` / `demo06` | ✅ |
| 单笔查单 / 挂单列表 / 历史订单 | `demo22` / `demo18` / `demo19` | ✅ |
| 附带止盈止损下单 | `demo20_止盈止损交易.py` | ✅ |
| 减仓 / 市价平仓 | `demo26` / `demo27` | ✅ |
| 单向止盈止损 `conditional` | `demo32` | ✅ |
| 双向止盈止损 `oco` | `demo31` / `demo33` | ✅ |
| 计划委托 `trigger` | `demo35` | ✅ |
| 移动止盈止损 `move_order_stop` | `demo36` | ✅ |
| 追逐限价 `chase` | `demo34`（含 `get_orderbook` 算追价） | ✅ |
| 时间加权 `twap` | `demo37`（记录 TWAP 需 isolated） | ✅ |
| 网格 `grid` / `contract_grid` | `demo38`（注明 SDK 缺字段） | ✅ |
| 策略委托 查询+撤销 / 修改 | `demo39` / `demo40` | ✅ |
| 杠杆 单设 / 批量设 / 查询 | `demo24`×2 / `demo25` | ✅ |
| 持仓查询 / 历史持仓 | `demo07` / `demo21` | ✅ |
| 行情 / K线 / 指数 / 全市场 | `demo08` `demo15` `demo16` `demo41~44`(WebSocket) | ✅ |
| 技术指标 | `demo09`~`demo12`（Talib 本地算） | ✅ |
| 最大可开仓 | `demo14` | ✅ |
| 账户财产 / 余额 / 账单 / 计息 | `demo01` `demo02` `demo13` `demo17` | ✅ |
| **冰山委托 `iceberg`** | — | ❌ **缺口**（§3.2） |
| **DCA 马丁机器人** | — | ❌ **缺口**（§3.4） |
| **定投 Recurring Buy** | — | ❌ 缺口，且 **Kit 也无**（§3.6） |
| **batch 阶梯建仓/平仓** | SDK `place_multiple_orders` 可用但未写 demo | ❌ 缺口（§3.5） |
| **期权 / 事件合约** | — | ❌ 缺口（本项目以 USDT 永续为主，可评估是否引入） |
| **手动调保证金保持仓** | — | ❌ 缺口，**Kit 无、仅 SDK**（§4.3） |

> 若要补齐 `iceberg`，最省事的做法：复制 `demo37`(twap) 改 `ORDER_TYPE="iceberg"` —— 两者参数族**完全相同**（`szLimit/pxVar/pxSpread/pxLimit/timeInterval`）。

---

## 11. 选型建议（MCP / CLI / Skills 各干什么）

| 场景 | 选型 | 理由 |
|---|---|---|
| 交互式让 Agent 下单/复盘 | **MCP** + `--modules market,account`（先只读） | 自然语言，错误结构化，可 `--read-only` 收口 |
| 定时任务 / cron / 管道 / 自建脚本 | **CLI** + `--json` | 无需 AI 客户端，退出码 + jq 可编排；本项目 `task/` 体系天然契合 |
| 给 Agent 固化"操作纪律" | **Skills** | 价值在流程护栏（查 ctVal、写前确认、写后回读、禁自动补救），而非新能力 |
| 需要 Kit 未封装的接口 | **Python SDK** | `adjustment_margin` / `recurring` / `borrow_repay` / 批量杠杆 |
| 追求低延迟下单 | **裸 WebSocket 自研** | Kit 无 WS 下单，SDK 仅有订阅基类 |
| 回测时防未来函数 | `market indicator --backtest-time` | 服务端返回"当时可见"的指标值，可校验本地 Talib 口径 |

**推荐落地顺序**（若要把这套接进本项目）：
1. `okx config init` 配 **只读权限 Key** 的 profile → `okx --demo account balance-all` 验通
2. `okx-trade-mcp --read-only --modules market,account` 挂给分析型 Agent（零下单风险）
3. 装 `okx-cex-trade` Skill，把它的"写前确认 / 写后回读 / 禁止自动补救"三条纪律移植到本项目实盘闸门
4. 最后才开 `--modules spot,swap` 的写通道，且 profile 与主交易循环的凭据**物理隔离**（避免与 `DualPeriodStrategyAdapter` 全局状态互相踩）

---

## 附录 A：Skills 全集（实测仓库 `skills/` 目录）

你原始需求只列了 4 个，实际 `agent-trade-kit/skills/` 下有 **10 个**：

| Skill | 授权 | 覆盖 | 备注 |
|---|---|---|---|
| `okx-cex-market` | 不需要 | 行情/深度/K线/资金费率/指标 | 含 `price-data` `instrument` `derivatives` `indicator` 4 份 references |
| `okx-cex-trade` | 需要 | 现货/永续/交割/期权/事件合约 + TP/SL/trailing | **61 条命令**，含 `spot` `swap` `futures` `options` `event` 5 份 references |
| `okx-cex-portfolio` | 需要 | 余额/持仓/PnL/账单/划转 | 仅 **13 条命令**（2 写），不含事件合约 |
| `okx-cex-bot` | 需要 | Grid(9) + DCA(5) | **14 条命令** |
| `okx-cex-earn` | 需要 | Simple Earn / 链上质押 / 双币赢 / 闪赚 / 自动生息 | 5 份 references |
| `okx-cex-smartmoney` | 需要 | 聪明钱榜 / 共识信号 / 持仓分析 | 只读 |
| `okx-cex-auth` | — | 登录/鉴权流程 | 鉴权失败时由其他 Skill 转加载 |
| `okx-cex-skill-mp` | — | Skill 市场管理 | 对应 `okx skill *` |
| `okx-sentiment-tracker` | — | 舆情追踪 | — |
| `earn-hunter` | 需要 | 生息扫描（含 `scan.sh`、定时调度模板） | 唯一自带脚本与 Claude-Code/OpenClaw 配置模板的 Skill |

安装/管理：
```bash
okx skill search <keyword> ; okx skill categories
okx skill add okx-cex-trade          # 下载 + npx skills add 装到检测到的 Agent
okx skill list / check <name> / remove <name> / download <name> --dir
```
> 本地注册表：`~/.okx/skills/registry.json`（只记版本元数据，实际路径由 `npx skills add` 管）
> ⚠️ 官方声明：Skills 市场内容为**第三方开发者**提交，OKX 不审核 —— 装之前先读它自己的 `SKILL.md`。

## 附录 B：模块 / 读写命令统计（v1.4.7 实算）

> 统计口径：`powershell -File data\okx_rw_split.ps1`，规则 = "CLI 路径最后一段以写动词（place/cancel/amend/close/set/leverage/batch/create/stop/trail/transfer/…）开头"即计为写命令。**不是官方标注，是本项目推导口径**，但逐条命令名已列出，可复核。
> 阅读页 `/okx-capability` 用同一套规则实时解析 `data\_okx_list_tools.json`（实现在 `crypto/capability_routes.py`，规则共享在 `crypto/capability_diagrams.py::is_write_command`），改任一侧后跑 `python data\okx_capability_check.py` 即可确认文档与页面数字没分叉。

| 模块 | CLI 命令数 | **写命令** | 读命令 | 写命令明细 |
|---|---|---|---|---|
| `market` | 20 | **0** | 20 | 全只读，且无需 API Key |
| `account` | 14 | 2 | 12 | `set-position-mode`、`transfer` |
| `spot` | 13 | **9** | 4 | place / amend / cancel / batch / leverage / algo place / algo trail / algo amend / algo cancel |
| `swap` | 16 | **10** | 6 | spot 全部 + `close` |
| `futures` | 16 | **10** | 6 | 同 swap |
| `option` | 14 | 7 | 7 | place / cancel / amend / batch-cancel / algo place / algo amend / algo cancel |
| `event` | 9 | 3 | 6 | place / amend / cancel |
| `bot` | 14 | 6 | 8 | grid create/amend/stop/close-position、dca create/stop |
| `earn` | 26 | 7 | 19 | 规则未覆盖 `auto-earn on/off`、`dcd quote-and-buy`，实际 ≥10 |
| `news` | 12 | 0 | 12 | 含宏观经济日历 |
| `smartmoney` | 10 | **0** | 10 | 全只读 |
| `skill` | 7 | 0 | 7 | `skill add/remove` 改本地文件，不碰交易所 |
| `config` | 4 | 1 | 3 | `config set`（本地） |
| `pilot` | 3 | 0 | 3 | `pilot install/remove` 只动本机服务 |
| `setup` / `diagnose` / `upgrade` / `list-tools` | 0 | — | — | 18 个模块里这 4 个不挂 command，属 CLI 自身入口 |
| **合计** | **178** | **55** | **123** | 55 条写命令里只有 `config set` 1 条纯属本地；12 条纯本地命令的其余 11 条按规则判为读 |

### B.1 三个"总数"别混为一谈（口径对账）

同一份 `okx list-tools --json` 能数出三个不同的总数，页面顶部会同时展示，换算关系如下：

| 数 | 值 | 含义 |
|---|---|---|
| **CLI 命令数** | **178** | `modules[].commands[]` 的全部条目，含只存在于 CLI 的本地命令 |
| **官方 `totalTools`** | **166** | = 178 − 12，即"有 MCP 工具承载"的命令数 |
| **去重后 toolName 数** | **160** | 166 条命令里有 **6 组是别名**（同一工具挂两个命令名），去重后剩 160 个工具 |

**12 条纯本地命令**（`toolName` 为空 ⇒ MCP 侧根本不存在，只能走 CLI）：
`config init/show/set/setup-clients`、`pilot status/install/remove`、`skill add/remove/check/list`、`earn auto-earn status`。

**6 组别名**（一个工具 ↔ 多条命令，写 MCP 提示词时别机械拼名字）：

| toolName | 对应 CLI 命令 |
|---|---|
| `spot_amend_order` | `okx spot amend`、`okx swap amend` |
| `spot_place_algo_order` | `okx spot algo place`、`okx spot algo trail`（trail 只是 ordType=move_order_stop 的语法糖） |
| `earn_auto_set` | `okx earn auto-earn on`、`okx earn auto-earn off` |
| `news_get_latest` | `okx news latest`、`okx news important` |
| `news_search` | `okx news search`、`okx news by-sentiment` |
| `news_get_coin_sentiment` | `okx news coin-sentiment`、`okx news coin-trend` |

> 💡 这组数字给出了一个非常实用的**风险收口指标**：按上述规则判为"写"的命令共 **55 条**，其中只有 `config set` 1 条是纯本地配置，**其余 54 条都会真实打到交易所/钱包侧**；而这 54 条里又有 **45 条**集中在 `spot/swap/futures/option/bot/event` 六个模块。
> 因此 `--modules market,account,news,smartmoney` 的组合 = **零资金风险的全功能分析面**（一条交易写命令都不注册，account 的 2 条也只是仓位模式与划转），这应该是所有分析型 Agent 的默认挂载方式。
>
> ⚠️ 另需记住：MCP **默认挂载是 `spot,swap,account`** —— 即默认就已经带上了 21 条写命令（spot 9 + swap 10 + account 2）。"装上就只读"是不成立的，必须显式 `--read-only` 或裁剪 `--modules`。
