# futures 模块代码汇总

## 一、核心代码（系统运行必须）

| 文件 | 用途 |
|------|------|
| `__init__.py` | 模块初始化文件，负责导入和导出核心类（FuturesAdapter、FuturesPro3Strategy 等），使 `futures` 目录成为一个标准的 Python 包 |
| `akshare_api.py` | 统一的 akshare 期货数据接口工具类。封装了通过 `akshare` 库获取期货行情数据的各种方法，包括获取实时行情、历史K线、品种列表等 |
| `pro3_strategy.py` | Pro3 趋势策略核心引擎。基于 MACD 平滑 + ADX 自适应算法，通过多周期K线数据计算趋势信号（signal），输出多/空方向判断，是期货和股票模块共用的策略实现 |
| `futures_adapter.py` | 策略适配器。负责连接数据层（akshare_api）、策略层（pro3_strategy）与前端展示层，实现数据获取→策略计算→结果组装→返回前端的完整流程 |
| `futures_blueprint.py` | Flask 蓝图定义。注册期货模块的所有页面路由和 REST API 接口，包括品种列表页、详情页、多周期分析页的路由 |
| `futures_config.json` | 自选品种配置文件。JSON 格式，存放用户关注的期货品种代码列表 |
| `future.csv` | 期货品种基础数据表。CSV 格式，包含所有可交易期货品种的名称、代码等信息 |
| `templates/` | HTML 前端模板目录，包含 3 个页面模板：`futures_index.html`（品种列表页）、`futures_detail.html`（品种详情页）、`futures_multi_period.html`（多周期分析页） |
| `static/` | 静态资源目录，包含前端 JS 文件 `futures.js`，负责页面交互逻辑、Ajax 请求和图表渲染 |

## 二、分析工具（非系统运行必须）

| 文件 | 用途 |
|------|------|
| `diagnose_trend.py` | 多周期趋势诊断工具。对单个期货品种，逐周期输出 Pro3 策略的信号详情（MACD值、ADX值、signal 方向等），用于排查策略信号是否符合预期 |
| `filter_trend_atr.py` | 趋势筛选 + ATR 排序工具。批量扫描所有期货品种，按 Pro3 策略的 signal 字段判断趋势方向，结合 ATR 指标排序输出，辅助筛选交易标的 |

## 三、依赖关系

```
app.py (主应用)
  └── futures_blueprint.py (蓝图注册)
        └── futures_adapter.py (适配器)
              ├── akshare_api.py (数据获取)
              ├── pro3_strategy.py (策略计算)
              └── futures_config.json / future.csv (配置)
```
