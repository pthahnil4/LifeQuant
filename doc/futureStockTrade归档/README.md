# futureStockTrade 归档文档

本目录是 **2026-10-08 合并迁移**（`futureStockTrade` → `cryptoTrade`）时从原项目
原样复制过来的历史文档，**只作参考，不再是运行口径**。迁移后的模块边界、路由前缀、
数据落点以 `doc/futureStockTrade合并迁移方案与清单.md` 与根目录 `项目说明.txt` 为准。

| 文件 | 原路径 | 内容 | 今天的可用性 |
|---|---|---|---|
| `数据存储审计报告.md` | `futureStockTrade/doc/` | 原项目的 CSV/JSON/SQLite/MySQL 落点清单 | 仅作历史对照；合并后数据已按决策 D3 原样搬进宿主 `data/` 与各模块目录，路径变了 |
| `futures模块代码汇总.md` | `futureStockTrade/futures/` | 期货监控模块的结构与接口汇总 | 代码现居 `crypto/futures/`，路由挂 `/futures`；汇总里的相对路径需自行换算 |
| `股票模块复刻分析.md` | `futureStockTrade/` | A 股模块的复刻思路 | 代码现居 `crypto/stocks/`，路由挂 `/stocks`；**股票模块只读行情，零交易端点**是硬红线 |
| `jumei_stock_api/` | `futureStockTrade/doc/jumei_stock_api/` | 聚美数据（JumData）行情接口笔记：约定、端点、返回字段、限制、样例响应 | 仍然有效，是 `crypto/stocks/` 数据源的接口说明 |

## 关于凭据

这批文档里**没有任何真实密钥**（复扫过 `AppCode`/`AppKey`/`secret`/`授权码` 等形态，
命中 0 处真实值）。`jumei_stock_api/README.md` 明确要求 AppCode 不写进代码与文档，
运行时取值口径是「环境变量 `JUMEI_APPCODE` → `data/jumei_appcode.txt` → 空」，
对应实现见 `crypto/ApiUtils/`（Phase 5 已把原先写死的明文常量改掉）。

`jumei_stock_api/api.md` 在原项目里就是 0 字节空文件，这里保留原样未删。

## 原项目

`D:\python\futureStockTrade` 在整个迁移过程中保持只读，未做任何删除或改写。
Phase 7 验收通过后才会改名为 `futureStockTrade_DEPRECATED` 冻结两周，再决定是否移除。
