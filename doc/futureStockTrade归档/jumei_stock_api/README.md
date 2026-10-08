# 聚美智数 · 股票行情 API —— 数据源档案

> 本目录用于集中存放「聚美智数」股票行情 API 的全部资料：接口清单、字段字典、
> 实测限制、真实返回样例。官方文档到齐后统一校准。
>
> 状态：**v0（预整理）** —— 已由 MCP `tools/list` + 直连 REST 实测 + 厂商公开汇总文章
> 交叉确认核心信息；标注 `⏳待文档确认` 的项目需以官方接口文档为准。

## 1. 数据源概况

| 项 | 值 |
|----|----|
| 名称 | 【聚美智数】股票行情查询 |
| 来源 | 阿里云市场（Cloud Market） |
| 直连 REST 网关 | `https://jmgphqcxhs.market.alicloudapi.com` |
| MCP 网关 | `http://mcpservergateway.market.alicloudapi.com/mcp/cmapi00067199/...` |
| 协议 | POST + `application/x-www-form-urlencoded; charset=UTF-8` |
| 鉴权 | 请求头 `Authorization: APPCODE <appcode>` |
| 覆盖 | A股 / 沪深（股票、板块、排行、报价、K线、分时成交、F10 等）|
| 不含 | ❌ 期货（本档案仅股票）|

## 2. 凭据管理（重要）

- AppCode **不写进代码、不写进本档案**。统一放在根级 `.env`：
  ```
  JUMEI_API_HOST=https://jmgphqcxhs.market.alicloudapi.com
  JUMEI_APPCODE=<你的AppCode>
  JUMEI_TIMEOUT=15
  ```
- 由 `config.py` 以「环境变量优先」加载；客户端 `stocks/jumei_api.py` 从 `config` 读取。
- `.env` 已被 `.gitignore` 忽略，勿提交。

## 3. 两条通道的关键差异（实测结论）

| 通道 | 控制面 | 数据面（取数） | 可用性 |
|------|--------|----------------|--------|
| **直连 REST** | 秒回 | 快、稳定 | ✅ 推荐使用 |
| MCP 网关 | `initialize`/`tools/list` 秒回 200 | 某段时间所有 `tools/call` 恒 502（上游超时）| ⚠️ 不稳定 |

→ **落地一律走直连 REST**；MCP 仅用于快速枚举工具与入参 schema。

## 4. 文件索引

- `endpoints.md` —— 接口清单：REST 路径 · 入参 · 必填 · 备注
- `response_fields.md` —— 返回体结构 + 字段字典 + 返回码表
- `limitations.md` —— 实测坑（K线最近100根 / 不可翻页 / 覆盖时长换算）
- `samples/` —— 各接口真实返回 JSON 样例
- 关联代码：`stocks/jumei_api.py`（工具类，含 `__main__` 自测）

## 5. 官方文档补全进度

据离线文档包 `cmapi00067199`（README 清单 + `_raw/*.json`）校准：

1. ✅ **全部 14 条 REST path/参数已确认**，客户端 `stocks/jumei_api.py` 全覆盖并实测通过（见 `endpoints.md`）。
2. ✅ **停牌信息 `/stock/a/stop`、大盘涨跌数 `/stock/hs/overview`** path/参数已确认（均无入参）。
3. ✅ **不存在"按日期/翻页取任意历史K线"的端点**，但三条接口根数上限不同（实测）：
   `a_kline`/`hs_kline` 硬顶 **100 根**；而 **`hs_mink`（分钟K含均线）不受此限，单次可回补 1000~1800 根**
   （60分钟≈近 22 个月）。三者都只到「当前」、无历史翻页 → 近2年1H可用 mink 直接回补，更早须定时采集入库。
   （详见 `limitations.md` 第1节；mink 高频大请求有专属限流坑。）
4. ⏳ 各接口 **QPS / 日配额 / 计费**（`charge` 字段确切含义）文档未列，需以购买套餐/实际返回为准。
5. ✅ **返回码表已确认**（全接口统一）：`200` 成功 / `400` 参数错误 / `500` 系统维护 / `999` 其他（以实际返回为准）；`ServiceTimeout=10000ms`。
6. ⏳ K线 `volume`（股/手）与 `amount` 单位口径：文档未注明，建议下单/回测前用日线对照交易所数据校准。
