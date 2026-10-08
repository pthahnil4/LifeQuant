# 接口清单（endpoints）

> 来源：阿里云市场离线文档包 `cmapi00067199`（README 接口清单 + `_raw/*.json` 权威参数）。
> ✅ = 已直连 REST 实测通过。全部 14 条 path 均已确认，客户端 `stocks/jumei_api.py` 已全覆盖。
> 通用约定：`POST`，头 `Authorization: APPCODE <appcode>`、`Content-Type: application/x-www-form-urlencoded; charset=UTF-8`；
> `ServiceTimeout=10000ms`；返回信封 `{code,msg,taskNo,charge?,data}`；业务码 400 参数错误 / 500 系统维护 / 999 其他。

## A. K线类（核心）

### ✅ A股K线 —— `/stock/a/kline`　方法 `a_kline()`
| 参数 | 必填 | 说明 |
|------|------|------|
| symbol | 是 | 品种代码，如 `sh000001` / `sz000001` |
| type | 是 | `1`/`5`/`15`/`30`/`60`/`120` 分钟，`240` 日，`1200` 周，`7200` 月，`86400` 年 |
| pageSize | 否 | 默认 10；**实测上限 100，恒为最近 100 根** |
| ma | 否 | 均线 `5,10,15,20,25,30`，传了返回 `ma_priceN` / `ma_volumeN` |

返回 `data.list[]`（dict）：`{day, open, high, low, close, volume, amount, [ma_*]}`。
⚠️ **无日期区间 / 无翻页参数** —— 官方文档已确认，只能取最近 N 根。

### ✅ 沪深K线 —— `/stock/hs/kline`　方法 `hs_kline()`
| 参数 | 必填 | 说明 |
|------|------|------|
| code | 是 | 如 `000001.SZ`（从排行/板块接口获取）|
| period | 是 | `1`/`5`/`15`/`30`/`60` 分钟，`101` 日，`102` 周，`103` 月，`104` 季，`105` 半年，`106` 年 |
| fuquan | 是 | `0` 不复权 / `1` 前复权 / `2` 后复权 |
| pageSize | 否 | **实测上限 100，恒为最近 100 根** |

返回 `data.candle[]`（数组）：`[ts(秒), open, close, high, low, volume, amount, change, changeRate]`。
⚠️ 同样**无日期 / 无翻页**。

### ✅ 沪深分钟K含均线 —— `/stock/hs/mink`　方法 `hs_mink()`
| 参数 | 必填 | 说明 |
|------|------|------|
| code | 是 | 如 `000001.SZ` |
| period | 否 | `1`/`5`/`15`/`30`/`60`/`120` 分钟 |
| pageSize | 否 | **★不受100限制**：实测单次可返回 1000~1800 根（60分钟≈近22个月）；≥2000 触发 code=502 |

⚠️ 高频大 pageSize 连打会触发本接口**专属限流**（之后任意大小返回空 candle），需冷却数分钟。
仍只到「当前」、无历史翻页 —— 更早历史须定时采集入库。

## B. 排行 / 行情 / 信息类

### ✅ A股排行 —— `/stock/a/rank`　方法 `a_rank()`
| 参数 | 必填 | 说明 |
|------|------|------|
| market | 是 | `hs_a`/`hs_b`/`hs_bjs`/`kcb`/`cyb`/`hs` |
| sort | 否 | `changeRate`(默认)/`volume`/`value`/`amplitude`/`turnOver`/`volumeRatio`/`pe`/`pb`/`totalShare`；含 `_5m`/`_5d`/`_20d` 系列 |
| asc | 否 | `0` 倒序(默认)/`1` 正序 |
| pageNo | 否 | 默认 1 |
| pageSize | 否 | 默认 10，最大 100 |

返回 `data.list[]`：`symbol,name,price,change,changeRate,open,high,low,preclose,volume,value,update_time` 等。

### ✅ 沪深股票排行 —— `/stock/hs/rank`　方法 `hs_rank()`
`type`(1全市场/2主板/3上证A/4深证A/5创业板/6中小板/7科创板) · `sortField`(px_change_rate默认/fundflow/turnover_ratio/volume_ratio/turnover_value) · `orderBy`(desc默认/asc) · `pageNo` · `pageSize`，均可选。

### ✅ A股报价 —— `/stock/a/price`　方法 `a_price()`
`symbol`(必)：多只逗号分隔，如 `sh000001,sz000002`。

### ✅ 沪深股票信息 —— `/stock/hs/info`　方法 `hs_info()`
`code`(必)：如 `000001.SZ`。

### ✅ A股F10公司信息 —— `/stock/a/f10-company`　方法 `a_f10_company()`
`symbol`(必)：**纯数字**，如 `000002`。

### ✅ A股信息拓展 —— `/stock/a/extra`　方法 `a_extra()`
`symbol`(必)：如 `sh600519`，可逗号分隔。

### ✅ 沪深分时成交 —— `/stock/hs/tick`　方法 `hs_tick()`
`code`(必)：如 `000001.SZ`。

### ✅ A股停牌信息 —— `/stock/a/stop`　方法 `a_stop()`
无入参。

### ✅ 沪深大盘涨跌数 —— `/stock/hs/overview`　方法 `hs_overview()`
无入参。

## C. 板块类

### ✅ 沪深板块排行 —— `/stock/hs/blockrank`　方法 `hs_blockrank()`
`type`(必)：1地域/2行业/3概念。`pageNo`/`pageSize` 可选。返回含 `blockCode`。

### ✅ 沪深板块成分股排行 —— `/stock/hs/blocklist`　方法 `hs_blocklist()`
`blockCode`(必)：从板块排行接口获取。`pageNo`/`pageSize` 可选。

---
_更新历史：v1（2026-09-18）据官方离线文档包 `cmapi00067199` 补全全部 14 条 path/参数，并直连实测通过。_
