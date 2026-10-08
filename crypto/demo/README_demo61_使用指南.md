# Demo61 多币种量化数据管理系统 - 完整使用指南

## 📋 系统概述

**demo61_quant_db_manager.py** 是一个功能完整的多币种量化数据管理系统，支持15个主流币种的全流程数据管理。

### 🎯 核心功能

1. **多币种历史K线批量存储** - 批量获取和存储历史K线数据
2. **WebSocket多币种增量更新** - 实时接收和更新K线数据
3. **多币种历史指标批量计算** - 批量计算技术指标
4. **多币种数据同步校验** - 三级定时数据校验

### 💰 支持的15个币种

```python
SYMBOL_LIST = [  
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",  
    "DOGE-USDT-SWAP", "ADA-USDT-SWAP", "LTC-USDT-SWAP", "NEAR-USDT-SWAP", "TRX-USDT-SWAP",  
    "BCH-USDT-SWAP", "DOT-USDT-SWAP", "UNI-USDT-SWAP", "LINK-USDT-SWAP", "TRUMP-USDT-SWAP"  
]
```

### ⏰ 支持的时间周期

```python
TIME_PERIODS = ["5m", "15m", "1H", "4H", "1D", "1W"]
```

---

## 🚀 快速开始

### 环境要求

```bash
# Python依赖
pip install pandas numpy influxdb-client asyncio

# 确保InfluxDB配置正确
# 检查 influxdb_config.py 文件
```

### 基本命令结构

```bash
python demo61_quant_db_manager.py --mode <模式> [选项]
```

---

## 📖 详细使用说明

### 1. 历史数据同步模式 (history)

**功能：** 批量获取历史K线数据并存储到InfluxDB

#### 🔹 同步所有币种的历史K线（最近30天）
```bash
python demo61_quant_db_manager.py --mode history --symbols all
```

#### 🔹 同步指定币种的历史K线
```bash
python demo61_quant_db_manager.py --mode history --symbols BTC-USDT-SWAP,ETH-USDT-SWAP
```

#### 🔹 同步指定时间范围的数据
```bash
python demo61_quant_db_manager.py --mode history --symbols all --start 2024-01-01 --end 2024-08-29
```

#### 🔹 同步指定周期的数据
```bash
python demo61_quant_db_manager.py --mode history --symbols BTC-USDT-SWAP --periods 5m,1H
```

**预期输出示例：**
```
📥 开始同步 BTC-USDT-SWAP 5m 周期历史K线（2024-08-01 ~ 2024-08-29）...  
✅ 分页查询：共获取 362 条数据（分 4 页，每页100条，最后一页62条）  
⚡ 去重跳过：已存在 12 条（时间戳冲突）  
✅ 成功写入：350 条（耗时 4.2s）
```

---

### 2. WebSocket增量更新模式 (ws)

**功能：** 实时订阅币种K线数据，增量更新到InfluxDB

#### 🔹 启动WebSocket订阅（所有币种）
```bash
python demo61_quant_db_manager.py --mode ws --symbols all
```

#### 🔹 订阅指定币种
```bash
python demo61_quant_db_manager.py --mode ws --symbols BTC-USDT-SWAP,ETH-USDT-SWAP
```

#### 🔹 订阅指定周期
```bash
python demo61_quant_db_manager.py --mode ws --symbols BTC-USDT-SWAP --periods 5m,15m
```

**预期输出示例：**
```
🔌 WebSocket已连接（服务器时间：2024-08-29 15:30:00）  
↓ 接收数据：BTC-USDT-SWAP 5m K线（时间：2024-08-29 15:25:00，状态：新增）  
✅ 写入成功：1 条（成交量：1234.56）  
↓ 接收数据：ETH-USDT-SWAP 15m K线（时间：2024-08-29 15:15:00，状态：更新）  
✅ 更新成功：1 条（字段变化：volume）
```

**停止方式：** 按 `Ctrl+C` 优雅退出

---

### 3. 技术指标计算模式 (indicator)

**功能：** 从K线数据计算技术指标并存储

#### 🔹 计算所有币种的技术指标
```bash
python demo61_quant_db_manager.py --mode indicator --symbols all
```

#### 🔹 计算指定币种和周期的指标
```bash
python demo61_quant_db_manager.py --mode indicator --symbols NEAR-USDT-SWAP --periods 5m,1H
```

**计算的技术指标：**
- MACD-EMA (自定义指标)
- MA12, MA26 (移动平均线)
- MACD, DIF, DEA (MACD指标组合)
- BOLL (布林带：上轨/中轨/下轨)
- RSI (相对强弱指标)
- SAR (抛物转向指标)
- KDJ (随机指标：K/D/J)

**预期输出示例：**
```
📊 开始计算 BTC-USDT-SWAP 1H 周期指标...  
✅ 读取K线数据：120 条（2024-08-01 ~ 2024-08-29）  
⚡ 指标计算：MA12/MA26（耗时 0.3s）、MACD（0.2s）、KDJ（0.1s）...  
✅ 成功写入指标：120 条（无重复数据）
```

---

### 4. 数据校验模式 (validate)

**功能：** 多维度校验数据完整性、连续性和准确性

#### 🔹 高频校验（最近30分钟的5m/15m数据）
```bash
python demo61_quant_db_manager.py --mode validate --level high
```

#### 🔹 中频校验（最近2小时的1H/4H数据）
```bash
python demo61_quant_db_manager.py --mode validate --level medium
```

#### 🔹 全量校验（前1天的1D/1W数据）
```bash
python demo61_quant_db_manager.py --mode validate --level full
```

**预期输出示例（表格化）：**
```
╔══════════════════╦════════╦════════════════╦══════════╦════════════╦════════════╗  
║ 币种+周期        ║ 时间窗口  ║ 实际条数      ║ 理论条数  ║ 连续性     ║ 关联性     ║  
╠══════════════════╬════════╬════════════════╬══════════╬════════════╬════════════╣  
║ BTC 5m          ║ 最近30分 ║ 6              ║ 6        ║ ✔️ 连续     ║ ✔️ 关联     ║  
║ ETH 1H          ║ 最近2小时 ║ 2              ║ 2        ║ ✔️ 连续     ║ ✔️ 关联     ║  
║ NEAR 1D         ║ 前1天    ║ 1              ║ 1        ║ ✔️ 连续     ║ ✔️ 关联     ║  
╚══════════════════╩════════╩════════════════╩══════════╩════════════╩════════════╝
```

---

## 🎛️ 高级选项

### 详细日志模式
```bash
python demo61_quant_db_manager.py --mode history --symbols all --verbose
```

### 自定义时间范围
```bash
python demo61_quant_db_manager.py --mode history --symbols BTC-USDT-SWAP --start 2024-06-01 --end 2024-08-31
```

### 混合操作示例
```bash
# 1. 先同步历史数据
python demo61_quant_db_manager.py --mode history --symbols BTC-USDT-SWAP,ETH-USDT-SWAP

# 2. 计算技术指标
python demo61_quant_db_manager.py --mode indicator --symbols BTC-USDT-SWAP,ETH-USDT-SWAP

# 3. 启动实时更新
python demo61_quant_db_manager.py --mode ws --symbols BTC-USDT-SWAP,ETH-USDT-SWAP
```

---

## 📊 数据存储结构

### K线数据表命名规则
```
{币种}_kline
例如：btc_usdt_swap_kline, eth_usdt_swap_kline
```

### 技术指标表命名规则
```
{币种}_indicators  
例如：btc_usdt_swap_indicators, eth_usdt_swap_indicators
```

### 主要字段说明

**K线表字段：**
- timestamp: 时间戳
- symbol: 币种标识
- period: 时间周期
- open/high/low/close: OHLC价格
- volume/vol_ccy: 成交量/成交额
- confirm: 确认状态

**指标表字段：**
- timestamp: 时间戳
- symbol: 币种标识  
- period: 时间周期
- macd_ema: 自定义MACD-EMA指标
- ma12/ma26: 移动平均线
- dif/dea/macd: MACD指标组合
- boll_upper/middle/lower: 布林带
- rsi: 相对强弱指标
- sar: 抛物转向指标
- k/d/j: KDJ指标
- direction: 方向标志
- current_profit: 当前持仓盈亏
- zero1/zero2: MACD临界值

---

## ⚠️ 注意事项

### 1. 去重机制
- 系统自动检查重复数据，避免重复插入
- 支持增量更新和数据修正

### 2. 错误处理
- 网络断开自动重连（WebSocket）
- API限流自动重试
- 详细的错误日志记录

### 3. 性能优化
- 分页获取历史数据
- 批量写入InfluxDB
- 异步处理WebSocket消息

### 4. 数据安全
- 写入前数据校验
- 时间戳去重检查
- 完整性校验机制

---

## 🔧 定时任务配置

### Windows计划任务示例

```batch
# 高频校验（每10分钟）
schtasks /create /tn "QuantDB_HighFreq_Validation" /tr "python D:\path\to\demo61_quant_db_manager.py --mode validate --level high" /sc minute /mo 10

# 中频校验（每1小时）  
schtasks /create /tn "QuantDB_MediumFreq_Validation" /tr "python D:\path\to\demo61_quant_db_manager.py --mode validate --level medium" /sc hourly

# 全量校验（每天凌晨3点）
schtasks /create /tn "QuantDB_Full_Validation" /tr "python D:\path\to\demo61_quant_db_manager.py --mode validate --level full" /sc daily /st 03:00
```

### Linux Cron任务示例

```bash
# 编辑crontab
crontab -e

# 添加定时任务
*/10 * * * * cd /path/to/project && python demo61_quant_db_manager.py --mode validate --level high
0 */1 * * * cd /path/to/project && python demo61_quant_db_manager.py --mode validate --level medium  
0 3 * * * cd /path/to/project && python demo61_quant_db_manager.py --mode validate --level full
```

---

## 🆘 故障排除

### 常见问题

**1. InfluxDB连接失败**
- 检查 `influxdb_config.py` 配置
- 确认InfluxDB服务运行状态
- 验证网络连接

**2. WebSocket连接超时**
- 检查网络连接
- 确认OKX API访问权限
- 重启程序自动重连

**3. 数据获取失败**
- 检查币种代码是否正确
- 确认时间范围合理
- 查看API调用限制

**4. 指标计算错误**
- 确保K线数据充足（≥100条）
- 检查数据质量
- 查看详细错误日志

---

## 📞 技术支持

如有问题，请查看：
1. 程序运行日志：`quant_db_manager.log`
2. InfluxDB连接状态
3. 系统资源使用情况
4. 网络连接状态

---

## 🎉 总结

demo61_quant_db_manager.py 提供了一个完整的多币种量化数据管理解决方案，支持：

✅ **15个主流币种** 的全覆盖  
✅ **6个时间周期** 的多维度分析  
✅ **4种核心功能** 的完整流程  
✅ **3级数据校验** 的质量保证  
✅ **命令行界面** 的灵活控制  
✅ **实时增量更新** 的高效同步  
✅ **完整日志系统** 的运维支持  

通过合理配置和使用，可以构建一个稳定、高效的量化交易数据基础设施。
