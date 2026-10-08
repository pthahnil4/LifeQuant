# RAG + LLM Wiki 盘感模块 使用手册

> 版本 v1.1 ｜ 2026-09-17 ｜ 适用代码：批次12（P1~P3 全部机械部分 + D6/D7 上线 + **U1 LLM key 已配置，真实网关验证通过**）
> 技术方案与统计口径的"为什么"见姊妹文档：`doc/RAG_LLM_Wiki模拟盘感落地方案.md`（本手册只讲"怎么用"）

---

## 1. 系统架构概览

### 1.1 数据流（单向，永不回流交易链路）

```
 三源历史数据                     语料层                        消费层
┌─────────────────┐   ┌──────────────────────┐   ┌─────────────────────────┐
│ task_analysis_   │   │ instinct_corpus       │   │ retriever  hybrid 检索   │
│ records (判断快照)├──▶│ (ctx_* 当时可见信息   │──▶│ (方向形似3.0 + ATR分位2.0 │
│ plan_slots (交易 │   │  + outcome_* 事后结算  │   │  + 周期1.0 + 衰减1.5,    │
│ 卡结算样本)      │   │  严格二分, 幂等upsert) │   │  同币种×3.0)             │
│ journal_notes    │   └──────────┬───────────┘   └───────────┬─────────────┘
│ (复盘, 暂无)     │              │                           │
└─────────────────┘   ┌──────────▼───────────┐   ┌──────────▼─────────────┐
                       │ wiki_distiller 统计蒸馏│   │ prompts 四段式渲染      │
                       │ instinct_wiki_rules   │──▶│ SYSTEM/WIKI/CASES/NOW  │
                       │ (规则卡 candidate→    │   │ (outcome 物理隔离,      │
                       │  active→retired)      │   │  fail-closed 拦截)      │
                       └──────────────────────┘   └──────────┬─────────────┘
                                                              │
                       ┌──────────────────────────┐  ┌───────▼──────────────┐
                       │ settle_pending 满窗惰性结算│◀─│ llm_gateway → LLM     │
                       │ (与复盘口径同源 classify_  │  │ instinct_predictions  │
                       │  move; 只统计, 永不进prompt)│  │ (影子预测流水)        │
                       └──────────────────────────┘  └──────────────────────┘
```

### 1.2 三层记忆模型

| 层 | 表 | 内容 | 进 prompt？ |
|---|---|---|---|
| **情景记忆** (episodic) | `instinct_corpus` | 每次决策的"当时可见上下文 + 判断 + 事后对错"，1:1 样本 | 仅 `ctx_*` 列（结果注记 v1 不注入） |
| **语义记忆** (semantic) | `instinct_wiki_rules` | 蒸馏出的显式规则卡（分桶统计路 + 个人vs策略种子路） | `status='active'` 且条件匹配的卡 |
| **工作记忆** (working) | `instinct_predictions` | 每次推理的输入快照、引用、输出、结算 | 不进，纯审计与 A/B 统计 |

### 1.3 防泄漏铁律（违反视为缺陷，冒烟有负例断言）

1. `ctx_*` 与 `outcome_*`/`hit_*` 严格二分：检索相似度、prompt 渲染只触碰 `ctx_*`；
2. 三层闸门：`sanitize_case_for_prompt`（键级剥离）→ `render_prompt(strict)`（入口抛错）→ `assert_no_future_leak`（文本级数值扫描）；
3. 历史回灌防"未来规则评历史题"：只注入 `created_at ≤ anchor` 且当时未过期的 active 规则；
4. **instinct 包与路由永不 import 任何下单/交易执行模块**——整点任务只写 `instinct_*` 四张表。

---

## 2. 页面功能详解与操作指南

### 2.1 /instinct 盘感模拟台（日常快览）

导航「🧠 盘感模拟」进入。五个区块：

| 区块 | 用途 | 说明 |
|---|---|---|
| 📍 当前局面 | 选币种 → 刷新快照 | 走实盘同源 `DualPeriodStrategyAdapter`（参数与该币交易配置一致），数秒级 |
| 🗂️ 相似历史 | 自动随快照展示 | topK 邻居仅含当时可见信息（页面即声明防泄漏口径） |
| 📜 Wiki 规则卡 | 快速确认/下线 | 完整治理在 Wiki 管理台（下表） |
| 🤖 影子预测流水 | 查看/反馈 | 👍可信/👎存疑/🤝一致/🙅相反 = 元认知校准数据源 |
| 📊 三感对比 | 你 vs 策略 vs LLM | LLM scored < 60 时显式标注"样本不足，仅供参考" |

「⚡ 来一发影子预测」：未配 LLM key 时返回明确 503 指引（不假成功）；配好后点击即产生一条 pending 预测，窗口满后自动结算。

### 2.2 /instinct/wiki 盘感 Wiki 管理台（治理/调试）

从模拟台顶部「📚 Wiki 管理台」链接进入。四个标签页：

**① 📜 规则卡治理**
- 状态过滤（candidate/active/retired）+ 关键词过滤；
- 每卡展开**证据链**（来源样本引用）与条件 DSL、统计依据（命中 n/N vs 同源基线）；
- 操作：`✅ 确认生效`（candidate→active，90 天有效）、`🚫 否决`/`⏸ 下线`（→retired，可附原因写进证据链）；
- `🧪 蒸馏复算（只出报告）`：全量重算显著候选，**不写库**，结果与报告路径回显；
- `📥 蒸馏并入库候选`：幂等 upsert（同 rule_key 合并证据不重复建卡；retired 被再蒸馏命中会复活为 candidate）；
- `⏰ 执行过期降级检查`：valid_until 过期的 active 卡降回 candidate 待复检（整点任务也会自动做）。
- **铁律：任何蒸馏/入库都不会自动激活规则——激活必须人工逐条点确认。**

**② 🤖 预测审计**
- 全字段流水：判断/置信/快照价/近远窗实际涨跌与命中/引用规则与语料 id/延迟；mock 行不展示；
- 用户反馈四键直接在表内点选（当前选中态高亮）；
- `🧮 立即结算到期项`：手动触发满窗口回填（平时由 10 分钟结算任务自动跑）。

**③ 📈 校准仪表盘**
- 六卡片：你/策略/LLM × 近/远窗的**当前累计命中率**与样本数；
- 折线图：累计命中率随样本推移的演化（后端给逐条 0/1 事件流，前端累计，不做平滑不藏样本）；
- 阅读提示：LLM 曲线要等 key 到位产生 scored 行后才会出现；<60 样本只看走势别看结论。

**④ 🔍 检索测试**
- 三种查询构造：**手工参数**（方向/翻转/ATR分位/周期/币种）· **按语料 id**（leave-one-out，自动排除自身）· **实时快照**（现拉行情）；
- 结果逐条给**打分明细条形分解**：dir/atr/period/decay 原始相似度 → 加权分 → base × 同币种 boost = 总分；
- 明细与生产检索总分逐位一致（冒烟断言保证），调权重前先在这里看直觉对不对；
- 本页展示"事后涨跌"列仅供人工核对；LLM 的 prompt 通道物理拿不到这些列。

---

## 3. CLI 工具速查

```powershell
# 语料重建/核对（幂等，可重复跑）
python -m crypto.instinct.corpus_builder --build
python -m crypto.instinct.corpus_builder --verify     # 与 P0 冻结基线三方核对

# 检索自检（结构门强制；统计门 n≥30 才出判定）
python -m crypto.instinct.retriever --self-check
python -m crypto.instinct.retriever --demo <corpus_id>

# 统计蒸馏（无需 LLM）
python -m crypto.instinct.wiki_distiller --report     # 只出报告
python -m crypto.instinct.wiki_distiller --apply      # 报告 + candidate 入库
python -m crypto.instinct.wiki_distiller --list

# 影子预测历史回灌（mock 验证管线；真实网关重跑即 D5 全验）
python -m crypto.instinct.predict_service --backfill --limit 15 --mock
python -m crypto.instinct.predict_service --backfill --persist   # 验证行请自行清理，防污染 A/B

# 规则激活/下线（页面同等能力，CLI 供脚本化）
python -c "from crypto.instinct import wiki_repo as w; print(w.activate_rule(1, confirmed_by='user'))"
```

---

## 4. 配置说明

### 4.1 LLM 网关（OpenAI 兼容，DeepSeek/DashScope 等皆可）

三个环境变量（或写入 `crypto/api_config.py` 的 `CRYPTO_LLM_BASE_URL / CRYPTO_LLM_API_KEY / CRYPTO_LLM_MODEL`，读取优先级：环境变量 > api_config）：

| 变量 | 例 | 说明 |
|---|---|---|
| `CRYPTO_LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容 base（不含 `/chat/completions`） |
| `CRYPTO_LLM_API_KEY` | `sk-...` | **不要提交进仓库**；`api_config.py` 已在 .gitignore |
| `CRYPTO_LLM_MODEL` | `deepseek-chat` | 模型名 |

**未配置时系统的行为（全部显式，绝不静默假成功）**：
- 调度任务不注册（任务列表干净缺席，日志会打"盘感影子预测未注册"）；
- 页面/接口 `predict` 返回 503 + 指引文案；
- CLI 真实网关调用抛 `GatewayConfigError`（mock 网关不受影响）。

> **当前状态（2026-09-17）**：三项已按上表写入 `crypto/api_config.py`（DeepSeek，真实回灌 15/15 验证通过）。宝塔侧**重启 app 后**整点影子预测与结算任务自动注册——无需再改任何代码。
> 兼容性说明：真实模型（DeepSeek 实测）会把 prompt 中 `[C1]/[R2]` 编号标签以字符串形式填进 `cited_*` 数组，网关解析层已归一化为整数（野格式与越界引用仍硬拒绝），不影响引用真实性统计。

### 4.2 开关

| 变量 | 默认 | 作用 |
|---|---|---|
| `CRYPTO_INSTINCT_AUTO` | `1` | 置 `0`/`false`/`off`：即使有 key 也不注册整点影子预测/结算任务 |
| `CRYPTO_NO_BACKGROUND` | 未设 | 通用开关：`1` 时 app 启动不拉调度器（冒烟/调试用） |

### 4.3 访问鉴权

`/instinct` 与 `/instinct/wiki` 全部路径由全局 web_auth 闸门覆盖（fail-safe：配了口令则所有来源含回环都要过口令；未配口令只放行本机）。无需为本模块单独配置。

### 4.4 定时任务（配置 key 重启后自动出现）

| job_id | 周期 | 动作 |
|---|---|---|
| `instinct_shadow_predict` | 整点 cron | 串行逐币影子预测（规避策略适配器全局状态竞争）+ 规则过期降级 |
| `instinct_settle` | 10 分钟 | pending 预测满窗结算（bar 未收线静默重试） |

---

## 5. 规则卡生命周期（治理流程图）

```
 蒸馏 --新建--> candidate --人工确认(页面/CLI)--> active --90天到期--> candidate(降级复检)
                    ^                                │
                    │ 再蒸馏命中(复活/合并证据)          │ 人工下线/否决(附原因入证据链)
                    +------------ retired <-----------+
 未知条件键 → 规则整条不匹配（fail-closed，宁可不注入）
```

- `rule_key` 幂等：同 key 再蒸馏只合并证据/刷新文本，不产生第二张卡；active 不因再蒸馏回退状态；
- 现役：#1 `seed:user-vs-strategy-far`（meta，active，confirmed_by=user，至 2026-12-16）。

---

## 6. 常见问题排查

| 症状 | 原因与处置 |
|---|---|
| 点预测报 503 | 网关配置未生效：确认 `api_config.py` 三项在位后**重启服务端 app**（配置在进程启动时读取）；临时可先用 `--backfill --mock` 验证管线本身 |
| 任务列表没有 instinct 两项 | 双闸门：无 key 或 `CRYPTO_INSTINCT_AUTO=0` 不注册。key 已在位时看是否重启过 app；日志"盘感影子预测未注册（原因）" |
| 分析记录页的记录被误删了 | 源头 `task_analysis_records` 无自动删除路径，误删可用 `python data/_rebuild_tar_from_corpus.py`（先干跑核对，加 `--apply` 写库）从语料快照按原 id 反推重建；有 MySQL 备份时优先用备份换回 |
| 预测一直是"待结算" | 窗口未满（近/远窗 = 短周期×REVIEW_NEAR/FAR_MULT）或 bar 未收线；点「立即结算到期项」或等 10 分钟任务 |
| `/api/now` 或实时检索很慢/失败 | 走真实行情拉取 + 双周期引擎，数秒属正常；失败看 logs 中 strategy_adapter 报错（K线接口异常） |
| 蒸馏报告候选是 0 条 | 正常且预期：桶 n≥10 且偏离同源基线≥15pp 才出卡（宁缺毋滥）；等语料积累 |
| 自检统计门"不出判定" | n<30 时故意搁置（小样本假信号教训见方案 §4-P2），非故障 |
| A/B 三感对比 LLM 列空 | scored 且非 mock 的预测才计数；<60 会标注"样本不足" |
| 规则激活后 prompt 没变化 | 检查条件 DSL 是否匹配当前快照（未知键=整条不匹配）；检索测试页可模拟 |
| 端口/登录问题 | 与本模块无关，走全局 web_auth：见 `crypto/web_auth.py` docstring |

冒烟回归（改任何 instinct 文件后必跑）：

```powershell
python crypto/_smoke_instinct.py    # 期望 [RESULT] PASS=106 FAIL=0
```

---

## 7. 回滚预案

模块自治，回滚 = 摘线，数据保留：

1. **停自动任务**：设 `CRYPTO_INSTINCT_AUTO=0` 重启（或调度器页面移除两个 job）；
2. **下线页面**：`crypto/app.py` 注释 `app.register_blueprint(instinct_bp)` 一行即可，其余模块零影响；
3. **停用某条规则**：页面「⏸ 下线」或 `wiki_repo.retire_rule(id, reason=...)`；
4. **数据**：`instinct_*` 四张表独立命名空间，不参与交易链路任何读取，留着不碍事；确要清空按 `doc/instinct_schema.sql` 对应 DROP。

回滚验证：`/instinct` 404、任务列表无 instinct 项、实盘交易日志无新增相关记录。

---

## 8. 边界声明（写死在架构里）

- 本模块是**影子系统**：预测、规则、统计永不下单、永不进入任何委托/风控决策路径；
- `crypto/instinct/` 包与 `instinct_routes.py` 不 import `trade_executor` / `position_order_manager` / `trend_range_trader` 等交易执行模块（冒烟有结构断言）;
- 升级为"实盘参考"级需通过方案 §4-P4 的 A/B 验收（LLM 远窗命中 ≥ 你基线且 ≥ 策略-5pp，且元认知数据正相关），届时仍只是"参考展示"，不改铁律。
