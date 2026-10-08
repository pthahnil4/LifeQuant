# Demo API 封装进度追踪

> 记录 `demo/` 目录下各脚本的 API 化封装状态
> 更新日期: 2026-05-12 (第二轮封装)

---

## 📈 市场数据类

| 文件 | 状态 | 对应接口 | 说明 |
|------|------|---------|------|
| demo08_获取指数K线数据.py | ✅ 已封装 | `GET /api/market/index_kline` | 指数K线数据 |
| demo15_获取所有产品行情信息.py | ✅ 已封装 | `GET /api/market/ticker` | 产品行情（单币种/全量） |
| demo16_获取交易产品历史K线数据.py | ✅ 已封装 | `GET /api/market/history_kline` | 历史K线(不含最新) |
| demo23_K线数据对比工具.py | ✅ 已封装 | `GET /api/market/kline_compare` | 4种K线方法对比 |

## 👤 账户类

| 文件 | 状态 | 对应接口 | 说明 |
|------|------|---------|------|
| demo02_账户余额查询.py | ✅ 已封装 | `GET /api/account/info` | 账户余额/总权益 |
| demo07_持仓查询.py | ✅ 已封装 | `GET /api/account/positions` | 当前持仓 |
| demo14_获取最大可开仓数量.py | ✅ 已封装 | `GET /api/account/max_order_size` | 最大可开仓数量 |
| demo17_获取账户计息记录.py | ✅ 已封装 | `GET /api/account/interest` | 计息记录 |
| demo21_历史持仓查询.py | ✅ 已封装 | `GET /api/account/positions_history` | 历史持仓 |
| demo25_查询杠杆倍数.py | ✅ 已封装 | `GET /api/account/leverage` | 杠杆倍数查询 |

## 💰 资金类

| 文件 | 状态 | 对应接口 | 说明 |
|------|------|---------|------|
| demo01_账户财产估值.py | ✅ 已封装 | `GET /api/funds/balance` | 资产估值 + 余额 |
| demo13_查看账户账单详情.py | ✅ 已封装 | `GET /api/funds/history` | 资金流水/账单 |

## 🔄 基础交易类

| 文件 | 状态 | 对应接口 | 说明 |
|------|------|---------|------|
| demo03_限价单交易.py | ❌ 下单操作 | — | 下限价单（未封装，仅保留查询） |
| demo04_市价单交易.py | ❌ 下单操作 | — | 下市价单（未封装，仅保留查询） |
| demo05_订单修改.py | ❌ 操作类 | — | 修改订单（未封装，仅保留查询） |
| demo06_订单撤销.py | ❌ 操作类 | — | 撤销订单（未封装，仅保留查询） |
| demo18_获取未成交订单列表.py | ✅ 已封装 | `GET /api/trade/open_orders` | 当前委托列表 |
| demo19_查询历史订单.py | ✅ 已封装 | `GET /api/trade/history` | 历史委托记录 |
| demo20_止盈止损交易.py | ❌ 下单操作 | — | 止盈止损（未封装，仅保留查询） |
| demo22_订单查询.py | ✅ 已封装 | `GET /api/trade/order_info` | 订单详情查询 |
| demo26_减仓平仓交易.py | ❌ 操作类 | — | 减仓平仓（未封装，仅保留查询） |
| demo27_市价减仓平仓交易.py | ❌ 操作类 | — | 市价减仓平仓（未封装，仅保留查询） |

## 🔄 高级策略交易类

| 文件 | 状态 | 对应接口 | 说明 |
|------|------|---------|------|
| demo31_单向止盈止损.py | ❌ 未封装 | — | 单向止盈止损 |
| demo32_单向止盈止损(conditional).py | ❌ 未封装 | — | 条件止盈止损 |
| demo33_双向止盈止损(oco).py | ❌ 未封装 | — | OCO双向止盈止损 |
| demo34_追逐限价委托(chase).py | ❌ 未封装 | — | 追逐限价委托 |
| demo35_计划委托(trigger).py | ❌ 未封装 | — | 计划委托 |
| demo36_移动止盈止损(move_order_stop).py | ❌ 未封装 | — | 移动止盈止损 |
| demo37_时间加权委托(twap).py | ❌ 未封装 | — | TWAP时间加权委托 |
| demo38_网格策略委托下单(grid).py | ❌ 未封装 | — | 网格策略委托 |
| demo39_查询并撤销策略委托订单.py | ❌ 未封装 | — | 策略委托管理 |
| demo40_修改策略委托订单.py | ❌ 未封装 | — | 修改策略委托 |

## 📊 技术指标类

| 文件 | 状态 | 对应接口 | 说明 |
|------|------|---------|------|
| demo09_用talib计算技术指标.py | ❌ 未封装 | — | TA-Lib指标计算 |
| demo10_用基础方法计算技术指标.py | ❌ 未封装 | — | 基础方法指标计算 |
| demo11_RSI技术指标.py | ❌ 未封装 | — | RSI指标 |
| demo12_MACD-EMA技术指标.py | ❌ 未封装 | — | MACD-EMA指标 |
| getBOLL.py | ❌ 未封装 | — | BOLL指标 |
| getEMA.py | ❌ 未封装 | — | EMA指标 |
| getRSI.py | ❌ 未封装 | — | RSI计算 |
| getRSI_RMI.py | ❌ 未封装 | — | RSI/RMI计算 |
| getSAR.py | ❌ 未封装 | — | SAR指标 |

## 🌐 WebSocket 类

| 文件 | 状态 | 对应接口 | 说明 |
|------|------|---------|------|
| demo41_标记价格WebSocket订阅.py | ❌ 未封装 | — | WebSocket不适合REST API |
| demo42_标记价格K线WebSocket订阅.py | ❌ 未封装 | — | 同上 |
| demo43_普通K线WebSocket订阅.py | ❌ 未封装 | — | 同上 |
| demo44_指数K线WebSocket订阅.py | ❌ 未封装 | — | 同上 |

## 🗄️ InfluxDB / 量化数据管理类

| 文件 | 状态 | 对应接口 | 说明 |
|------|------|---------|------|
| demo51_InfluxDB时序数据库操作.py | ❌ 未封装 | — | InfluxDB基础操作 |
| demo52_InfluxDB存储K线数据.py | ❌ 未封装 | — | K线数据存储 |
| demo53_InfluxDB技术指标计算.py | ❌ 未封装 | — | 指标计算+存储 |
| demo54_InfluxDB数据清理工具.py | ❌ 未封装 | — | 数据清理 |
| demo55_InfluxDB_Data_Viewer.py | ❌ 未封装 | — | 数据查看器 |
| demo55_InfluxDB数据查询检查工具.py | ❌ 未封装 | — | 数据查询检查 |
| demo56_测试增量更新.py | ❌ 未封装 | — | 增量更新测试 |
| demo61_quant_db_manager.py | ❌ 未封装 | — | 量化数据库管理 |
| demo62_multi_symbol_kline_storage.py | ❌ 未封装 | — | 多币种K线存储 |
| demo63_websocket_incremental_update.py | ❌ 未封装 | — | WebSocket增量更新 |
| demo64_multi_symbol_indicators.py | ❌ 未封装 | — | 多币种指标计算 |
| demo65_multi_symbol_validation.py | ❌ 未封装 | — | 数据验证 |

## 🔧 工具类

| 文件 | 状态 | 对应接口 | 说明 |
|------|------|---------|------|
| advanced_strategy_demo.py | ❌ 未封装 | — | 高级策略演示 |
| demo_runner.py | ❌ 未封装 | — | Demo运行器 |
| install_dependencies.py | ❌ 未封装 | — | 依赖安装 |
| setup_wizard.py | ❌ 未封装 | — | 安装向导 |
| test_api_call.py | ❌ 未封装 | — | API调用测试 |

---

## 统计汇总

| 类别 | 总数 | 已封装 | 未封装 |
|------|:---:|:------:|:------:|
| 市场数据 | 4 | 4 | 0 |
| 账户类 | 6 | 6 | 0 |
| 资金类 | 2 | 2 | 0 |
| 基础交易类（仅查询） | 10 | 3 | 7（下单/操作类） |
| 高级策略交易类 | 10 | 0 | 10 |
| 技术指标类 | 9 | 0 | 9 |
| WebSocket类 | 4 | 0 | 4 |
| InfluxDB/数据管理 | 12 | 0 | 12 |
| 工具类 | 5 | 0 | 5 |
| **合计** | **62** | **15** | **47（含下单/操作未封装）** |
