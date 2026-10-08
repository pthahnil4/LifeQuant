# futureStockTrade → cryptoTrade 合并迁移方案与清单

> 状态：**已定稿，待执行**（本文件只做方案，不含任何代码改动）
> 调研基准：2026-10-08 两仓库磁盘现状
> 目标：把 `D:\python\futureStockTrade` 的期货监控 / 股票监控 / K线训练三模块，并入 `D:\python\cryptoTrade` 的 `crypto/` 应用，做到"一个进程、一个端口、一套鉴权、一套导航"。

## 决策结论（2026-10-08 与用户确认）

| 决策 | 结论 |
|---|---|
| D1 融合深度 | **同 app、同端口 7777、三个子包蓝图**。`/futures`、`/stocks`、`/kline` 由 `crypto/web_auth.py` 的 app 级闸门统一保护，导航与邮件复用宿主。 |
| D2 K线训练账号 | **保留独立 `users` 表**，session 键改名为 `kline_uid`；两层鉴权共存（闸门管远程访问，账号管训练数据归属）。对战/成绩数据零风险。 |
| D3 数据落库 | **先原样搬**（CSV/JSON/SQLite 照旧迁入宿主 `data/`），`tools/migrate_phase1~4` 留档不执行，MySQL 统一留待后续迭代。 |

对应调整：Phase 5 的依赖与密钥收口照常执行；Phase 7 的 MySQL 迁移脚本项转为"留档不执行"。

---

## 一、双方现状（实测数据）

### cryptoTrade（宿主）

| 项 | 现状 |
|---|---|
| 入口 | 根 `app.py` → `from crypto.app import app`，`crypto/app.py` 约 3439 行 |
| 端口 | `0.0.0.0:7777`（`CRYPTO_WEB_HOST/CRYPTO_WEB_PORT` 可覆盖） |
| 路由组织 | 页面路由内联 + 12 个 `*_routes.py` 蓝图；蓝图**统一回落** `crypto/templates/`、`crypto/static/`（不设 `template_folder`） |
| 鉴权 | `crypto/web_auth.py` app 级 `before_request` 全局闸门，**必须在所有蓝图注册之后**初始化（`crypto/app.py:145-147`） |
| 存储 | MySQL 权威源（SQLAlchemy+pymysql，连接串取 `CRYPTO_DB_URL` → `data/db_url.txt`）+ JSON/CSV 兜底；建表权威 = 根 `db_schema.sql` |
| 调度 | `crypto/task/scheduler.py` APScheduler 单例，`register_default_jobs()` 逐个 try-import 挂载 |
| 运维中心 | `opscenter/` 是**独立 Flask 应用**、独立 6002 进程 —— 本次不采用这个样式（见下） |
| 冲突预埋 | 已有 `crypto/star_market.py`、`crypto/version_close_open.py`、`crypto/strategy/pro3_dualtimeframe.py`、`pro3_singletimeframe.py`、`multi_period.html`、`star_market.html`、`nav.html`、`kline_fetcher/`、`kline_live_routes.py` |

### futureStockTrade（被并入方，无 git 仓库）

| 模块 | 蓝图行数 | 路由数 | 内部 py 文件 | 说明 |
|---|---|---|---|---|
| `futures/` | 686 | 26 | 16 | akshare 期货行情 + pro3 双周期 |
| `stocks/` | 324 | 9 | 7 | akshare 股票行情，最独立 |
| `kline/` | 1649 | 53 | 22 | K线训练/对战/PK，**最重且不独立**（registry 运行时聚合 futures+stocks+crypto 三适配器） |
| `strategies/` | — | — | 13 | futures+stocks 共用 pro3 核心与 `indicators.py` |
| 合计 | 约 21,300 行 Python | 88 路由 | ~58 py | 另有 `tools/`（MySQL 迁移脚本 6 个）、`doc/`（9 文件） |

入口把 root/futures/stocks/kline 四个目录全塞进 `sys.path`，之后**全部裸导入**（`import db` / `import config` / `from pro3_strategy import ...`）。这是本次迁移最大的技术风险源。

### 环境实测（关键，省掉大量工作）

```
python-okx 0.4.3   akshare 1.18.64   taospy 2.8.9   backtrader 1.9.78.123
Flask 3.1.3        pandas 2.3.3      argon2-cffi 25.1.0   ccxt 4.5.15
```

被并入方 `requirements.txt` 所要求的包在本机 **全部已装且版本满足**，宿主 `requirements.txt` 只是没声明 akshare/taospy/Werkzeug/dotenv。→ **依赖层面无需取舍，只需补声明**，不存在"合并把一个项目跑坏"的版本冲突。

---

## 二、目标架构（推荐方案）

**同进程、同 app、三个子包蓝图**，而不是仿 opscenter 再开一个独立服务。

理由：① 只有挂进同一个 `app`，`web_auth` 的 app 级 `before_request` 闸门才会自动覆盖 `/futures /stocks /kline`（实盘接口裸奔的历史问题不再重演）；② 用户要的"合二为一"包含统一导航与一次登录；③ 独立端口方案（5000 与 7777 并存）改动最小但等于没合并，且要复制一套鉴权与邮件配置。

合并后目录（新增部分用 ★）：

```
cryptoTrade/
├── app.py                        入口不变（7777）
├── crypto/
│   ├── app.py                    +3 段蓝图注册（闸门之前）
│   ├── futures/              ★   蓝图+适配器+akshare_api+star_market(期货版)+templates/+static/
│   ├── stocks/               ★   蓝图+适配器+stock_akshare_api+jumei_api+templates/+static/
│   ├── kline_training/       ★   训练/PK/账号（重命名，避开宿主 kline_fetcher/kline_live）
│   └── market_strategy/      ★   原 strategies/ 公共层（indicators/pro3_*/boll_limit/version_close_open）
├── doc/                          + 被并入方文档
└── db_schema.sql                 + 新增表 DDL（如走 MySQL 统一）
```

命名要点（都为了不与宿主撞名）：`kline/` → **`kline_training/`**；`strategies/` → **`market_strategy/`**；根 `config.py` → 拆进各包内 `settings.py`；`kline/db.py` → `kline_training/store.py`。宿主已有 `star_market.py`/`version_close_open.py`/`pro3_dualtimeframe.py` 与被并入方重名，但**包路径天然隔离**，不做内容合并。

---

## 三、决策记录（已全部确认，见文首"决策结论"）

| # | 决策 | 选项 A（我的默认建议） | 选项 B | 影响 |
|---|---|---|---|---|
| D1 | 融合深度 | 同 app 同端口 7777，三蓝图 | opscenter 式独立进程独立端口 | B 省 1~2 天工，但闸门/导航/邮件要复制两份，实盘暴露面翻倍 |
| D2 | K线训练账号体系 | 保留独立 `users` 表，session 键改 `kline_uid`（与宿主闸门口令分层共存） | 废除独立账号，统一走 `web_auth` 口令 | B 要迁 `data/training.db` 里的对战/成绩归属，风险高；A 只改键名 |
| D3 | 行情数据落库 | 先原样搬（CSV/JSON/SQLite 照旧），MySQL 统一留到后续迭代 | 同步执行 `tools/migrate_phase1~4` 灌进新库 | B 增加 1~2 天且引入数据一致性风险；建议 A，迁移期"能跑"优先 |

---

## 四、冲突与风险全景（逐条已核实）

| 风险 | 具体表现 | 处置 |
|---|---|---|
| 裸导入撞名 | 宿主顶层已有 `star_market.py`、`version_close_open.py`；`config.py`/`db.py`/`registry.py`/`mailer.py`/`auth_service.py`/`indicators.py` 名字通用，一旦 `sys.path` 顺序变化会被错误解析 | 取消 `sys.path` 拼接，全部改**显式包导入** `from crypto.futures.futures_adapter import ...` |
| 跨包耦合 | `futures/cme_api.py:43 from stocks.jumei_api import ...`（期货依赖股票包）；`kline/registry.py` 6 处依赖 futures+stocks 适配器 | 同规则改为包路径导入 |
| shim 遮蔽 | `futures/pro3_strategy.py` 是重导出 shim，`from pro3_strategy import` 会命中 futures 版而非公共版 | 统一指向 `crypto.market_strategy.pro3_strategy` |
| 模板同名 | 双方都有 `nav.html`、`multi_period.html`、`star_market.html`；被并入方还有通用的 `auth.html`/`profile.html` | 各蓝图**自带** `template_folder`（`crypto/futures/templates` 等），不并入 `crypto/templates/` |
| session/SECRET_KEY | 宿主 `crypto/app.py` **完全没有** `session` 与 `app.secret_key`；kline 依赖 `session['uid']` + `SECRET_KEY` 签名 cookie | 加 `app.secret_key`（走环境变量/`data/`，勿硬编码），session 键命名空间化 `kline_uid` |
| 闸门顺序 | `crypto/app.py:145-147`：`_init_web_auth(app)` 必须在所有 `register_blueprint` 之后 | 三处注册插在 128 行之后、145 行之前；**不要**往 `web_auth._ALWAYS_OPEN` 加白名单 |
| SMTP 重复 | 被并入方 `config.py:157-164` 有**硬编码邮箱授权码** | 删除明文，改用宿主邮件通道 |
| akshare 网络 | 期货/股票实时行情依赖国内网络（既有记忆：akshare 依赖国内网络、`futures_hist_em` 周月线接口不可用） | 蓝图注册包 try/except + 开关 `FST_ENABLE_FUTURES/STOCKS/KLINE`，缺库或无网时降级为提示页，不拖垮主应用 |
| OKX SDK 行为差异 | 宿主按 python-okx 0.4.1 的 HTTP/2 长连接与显式超时做过专项修复，现装 0.4.3 | 回归重点验证 `kline/kline_adapter.py` 与 `providers/crypto_provider.py` 的取数；必要时对齐宿主的超时/复用姿势 |
| SQLite WAL | `data/training.db-wal` 已达 **4MB**（未合并回主库） | 拷贝前**先** `PRAGMA wal_checkpoint(TRUNCATE)`，否则复制出的 db 丢最近数据 |
| TDengine | 默认开关关闭，历史 K 线缓存可选 | 保持关闭，不引入运维负担 |
| 明文密钥 | 宿主 `crypto/api_config.py:58-60` 有 DeepSeek 明文 key（已入库） | 顺手提醒：不在本次范围，建议单独处理 |

---

## 五、迁移阶段清单（每阶段都可独立验收、可回退）

### Phase 0 · 基线与快照（0.5 天）✅ 已完成
- [x] futureStockTrade 先 `git init` + 全量提交一次，形成可追溯基线（基线 commit `016c001`，并补 `.gitignore` 排除 `data/`、`*.db*`、`.env`、`*.log`、截图）
- [x] 复制一份 zip 快照到 `D:\python\futureStockTrade_premerge_20261008.zip`（延续你现有 `cryptoTrade (N).zip` 习惯）
- [x] cryptoTrade 从 `main` 切 `feat/merge-futurestock` 分支；确认工作区干净
- [x] **WAL 归零后再复制**：改用 `migrate_00_baseline.py` 内的 `PRAGMA wal_checkpoint(TRUNCATE)` + `integrity_check` + 表清单打印（比 shutdown hook 更直接，不依赖停机窗口）
- [x] **隔离验证准备**：迁移期每次起服务验证都带 `CRYPTO_NO_BACKGROUND=1`，绝不让交易调度器随合并中的半成品代码重启（历史事故记忆：重启需归因+时间窗口）

### Phase 1 · 公共策略层（0.5~1 天）✅ 已完成（commit `56188ca`）
- [x] `strategies/{indicators,pro3_strategy,pro3_dualtimeframe,pro3_singletimeframe,futures_boll_limit_dualtimeframe,version_close_open}.py` → `crypto/market_strategy/`
- [x] `strategies/futuresStrategy/*` → `crypto/market_strategy/futures_strategy/`
- [x] **不迁** `strategies/cryptoStrategy/*`（缺 `api_config`、`trend_strategy_boll_limit_scheduler_like`，属跨项目残留，宿主已有对应实现）
- [x] 全部 import 改包路径；`futures/pro3_strategy.py`、`stocks/pro3_strategy.py` 两个 shim **不复制**（原项目保持不动，合并后统一由 `crypto.market_strategy` 提供）
- [x] 验收：`python -c` 逐个 import 通过 + 一次 pro3 回测冒烟；**补测**：真实数据 A/B（akshare 豆粕 M0，908 根日线）期货版与股票版逐字段一致
- [x] **计划修正**：`strategies/*` 直接依赖 `futures.akshare_api`，故期货数据层 `akshare_api.py` 提前到 Phase 1 一并迁入；`cme_api.py` 依赖 `stocks.jumei_api`，推迟到 Phase 3

### Phase 2 · 期货模块（1 天）✅ 已完成
- [x] `futures/` → `crypto/futures/`（含 `templates/` 4 个 html、`static/futures.js`、`future.csv`、`star品种行情.csv`、`futures_config.json`，以及批量趋势输出 `trend_atr_all_result.csv`）
- [x] `futures_blueprint.py` → `crypto/futures/routes.py`；构造 `Blueprint('futures', url_prefix='/futures', template_folder='templates', static_folder='static')`
- [x] 期货配置随模块走（`futures_config.json` 原样保留）。**未新建 `settings.py`**：该模块不含 SMTP/密钥类配置，敏感项收口统一放到 Phase 5
- [x] `cme_api.py` 不在 Phase 2 迁入（依赖 `stocks.jumei_api`），与 Phase 3 股票模块一起收敛
- [x] 在 `crypto/app.py` 注册（必须在 `_init_web_auth(app)` 之前；try/except 包裹 + `FST_ENABLE_FUTURES=0` 可显式关闭）
- [x] 导航项：`crypto/navigation_service.py` 新增顶层分组「多市场」（order 5，`工具与系统` 后移到 6）+ `link-futures`、`link-futures-star`，`ACTIVE_PAGE_ALIASES` 同步补两个别名
- [x] 验收：7777 下路由与原项目 **25 条完全对齐**（缺失/新增/方法不一致均为 0），4 个页面全 200 且套用宿主导航与样式；未配口令时远程 403。详见「九、执行进度与实测结论」

### Phase 3 · 股票模块（0.5~1 天）✅ 已完成
- [x] `stocks/` → `crypto/stocks/`（`stocks_blueprint.py` → `routes.py`，`url_prefix='/stocks'`，模板/静态目录随蓝图）
- [x] `jumei_api.py` 迁入并标注「未接线」；`cme_api.py`（Phase 2 推迟项）一并迁入 `crypto/futures/cme_api.py`，导入收敛为 `from crypto.stocks.jumei_api import ...`
- [x] **新建 `crypto/stocks/gateway_config.py` 取代根级 `config.py`**：只暴露 `JUMEI_API_HOST / JUMEI_APPCODE / JUMEI_TIMEOUT / JUMEI_CME_*` 六个名字，凭据按「环境变量 → 外置 `data/jumei*_appcode.txt` → 空」解析。原 `config.py` 里的 `SECRET_KEY='fst-dev-insecure-change-me'`、SMTP 授权码明文、TDengine 口令、MySQL 连接串**一律未进入** cryptoTrade（脚本内置凭据体检，命中 0）
- [x] **不迁** `stocks/stock_pro3_strategy.py`（纯兼容 shim，除自身文档外无引用）、`stocks/demo_kline.py`（示例脚本，无人 import）
- [x] 在 `crypto/app.py` 注册（闸门之前，try/except + `FST_ENABLE_STOCKS=0` 开关）；导航「多市场」分组下新增 `link-stocks`
- [x] 验收：9 条路由 + 静态 = 10 条与原项目**完全对齐**；3 页面全 200 并套用宿主导航；`api/config` 严格相等，5 个实时接口结构一致；89 项站内引用失败 0；CSS 回归 0。详见「9.6」

### Phase 4 · K线训练模块（2~3 天，主战场）✅ 已完成（验收见 §9.9）
- [x] `kline/` → `crypto/kline_training/`；`db.py` → `store.py`，`registry.py`/`mailer.py`/`auth_service.py`/`training_service.py`/`chart_serialize.py`/`kline_adapter.py` 保留原名（包内已隔离）
- [x] `providers/*` 与 `registry.py` 的 6 处跨模块导入改包路径
- [x] `kline/database/`（TDengine 缓存）→ `crypto/kline_training/td_cache/`，默认关闭
- [x] `mailer.py` → 优先复用宿主既有邮件通道，仅保留 kline 专属模板
      （取值改为引用宿主唯一发件配置 `crypto/task/config/email_config.py`，键名 `SMTP_CONFIG` 不变，
      `mailer.py` 零语义改动；`EMAIL_DEV_FALLBACK` 默认由「开」改「关」）
- [x] `session['uid']` → `session['kline_uid']`（routes.py 5 处；`current_user` 注入由 app 级收窄为蓝图级）
- [x] `app.secret_key` 落到宿主配置（环境变量 → `data/app_secret_key.txt` → 首次自动生成并落盘，
      无任何代码内默认值）；`SESSION_COOKIE_SECURE`/30 天生命周期一并搬
- [x] 数据：`training.db` 先 checkpoint 再复制 → `cryptoTrade/data/kline_training.db`，路径走 `data_paths.py` 机制（勿硬编码，遵守跨平台 JSON/DB 落盘规范）
- [x] **不迁**：`kline_trainer/`（demo）、`db_probe_login.py`、`verify_auth_login.py`、`test_fetch.py`
- [x] 验收：登录/注册、训练开局、三市场 provider、对战与成绩读写

### Phase 5 · 配置、密钥与依赖收口（0.5 天）
- [x] `requirements.txt` 补：`akshare>=1.18,<2.0`、`Werkzeug>=3.0,<4.0`、`cryptography>=41.0`
      （实测按代码 import 反查后调整：**不加** `python-dotenv`——合并后无任何 `.env` 读取点；
      `taospy` 以注释形式声明为可选，`td_cache` 默认关闭；详见 §9.12）
- [x] 顺带补齐宿主原有缺口：`requests` / `psutil` / `Markdown`（都不是合并引入，
      但按「代码有 import、清单没声明」的口径属于同一类事故）
- [x] 被并入方 `.env` 语义 → 统一为宿主的环境变量 + `data/*.txt` 模式；
      **硬编码 SMTP 授权码与默认 `SECRET_KEY` 已在 Phase 4 随 `settings.py` 一并挡在门外**，
      本轮用脚本复扫确认（含「环境变量 or 写死缺省」这种后门形态）
- [x] `api_config`（OKX 密钥）不引入新副本 —— 实测 K 线训练的 crypto provider 走的是
      OKX **公开**行情接口（`/api/v5/market/*`，无需 API Key），比"复用"更彻底：
      合并链路 0 处引用 `api_config`，宿主那份密钥文件一行未动
- [x] 全仓库 grep 明文密钥，确认没带进 git —— 新增 `verify_05_secrets_and_deps.py`，
      扫 512 个 git 可见文件：高危 0 / 中危 0；**过程中抓出 1 处真泄露**
      （`crypto/ApiUtils/` 三份工具类把阿里云 AppCode 写成明文常量，注释里还有
      AppKey/AppSecret；这三份文件当时处于「未跟踪但也未 ignore」状态，
      Phase 7 的 `git add -A` 会直接把凭据推进公开仓库）→ 已改为
      环境变量 / `data/jumei_appcode.txt` 解析，凭据移出源码，git 历史确认干净
- [x] 验收：`python -X utf8 verify_05_secrets_and_deps.py` 退出码 0

### Phase 6 · 部署、入口与前端收口（1 天）
- [x] 单实例守护：`app.py` 的 `connect_ex` 探端口、拒绝双实例思路合并进宿主根 `app.py`，防 Windows 端口复用劫持（旧 5000 进程的停止见下面单独一条，需用户点头）
- [~] 导航统一：`/`、`/futures/`、`/stocks/`、`/kline/` 互通，active 态与返回链接检查
      —— 代码侧已就绪（三个模块页都 include 宿主 `nav.html`，导航默认配置与
      `CATALOG_PAGES` 已含四项）；**生产导航是 `source=db` 的用户自定义配置**，
      新条目不会自动出现在他的导航里，需要一次 `PUT /api/navigation/config`
      或在「导航配置」页手工添加（待用户确认，见 11.5）
- [x] 开关：`FST_ENABLE_*` 三个环境变量 + 缺依赖时的降级提示页
- [~] 回归测试：跑宿主既有 Python 回归脚本（结果见 11.4）
- [x] 冒烟：新增 `_smoke_merge_futures.py` / `_smoke_merge_stocks.py` / `_smoke_merge_kline.py`，纳入 `_run_smokes.py`
- [x] 接线入库：`crypto/app.py` 的三个蓝图注册块补进版本库（Phase 1~5 一直漏了它，
      干净检出挂不上模块）；与宿主在制代码同 hunk，用 `data/_stage_fst_wiring.py`
      只写索引拆分提交，并在 `git worktree` 的纯仓库检出里复验通过（见 11.6）
- [ ] **A/B 验证**：合并后同一时刻对同一批期货/股票品种跑新旧两套逻辑比对结果（记忆教训：冒烟全绿证明不了任何事，判定逻辑必须用生产真实数据 A/B）
- [ ] 生产切换：低峰窗口（避开开仓窗口）重启 + 重启归因日志
- [ ] 端口 5000 进程退役（守护已就位，但停掉旧实例要用户点头；PID 43060）

> Phase 6 实测记录见「十一、Phase 6 实测记录」。其中**导航统一**这一项在核查时挖出
> 一个 Phase 2 遗留的真实缺陷（期货星标页被宿主同名模板遮蔽），已修复并补了专项冒烟。

### Phase 7 · 收尾（0.5 天）
- [ ] `tools/migrate_phase1~4.py`（CSV/JSON→MySQL）留档不执行（决策 D3=B 时才执行，且需先对齐 `db_schema.sql`）
- [x] 文档：被并入方 `doc/数据存储审计报告.md`、`doc/jumei_stock_api/*`、`futures模块代码汇总.md`、`股票模块复刻分析.md` → `cryptoTrade/doc/futureStockTrade归档/`（附 `README.md` 标注来源与可用性；凭据复扫 0 处真实值，`verify_05` 退出码 0）
- [x] 更新 `项目说明.txt` / `数据存储说明.md` 的模块边界章节（新增【多市场模块边界】与存储表第 8 节，密钥文件清单补 `app_secret_key.txt` / `jumei_appcode.txt`）
- [ ] 合并分支、打 tag `merge-futurestock-202610xx`、原目录改名 `futureStockTrade_DEPRECATED` 保留 2 周（**均需用户点头**：涉及生产分支与原项目目录）

**总工作量估算：6~9 人日**（Phase 4 的 K线训练占一半以上）。可以按 Phase 2/3/4 分三次交付，每次上线一个市场模块。

---

## 六、明确不迁移的文件（清单）

| 类别 | 文件 | 原因 |
|---|---|---|
| 跨项目残留 | `strategies/cryptoStrategy/`（4 项） | 引用本仓库不存在的 `api_config`、`okx.MarketData`、`trend_strategy_boll_limit_scheduler_like`；宿主已有对应实现 |
| 一次性脚本 | `calc_today.py`、`verify_indicators.py`、`run_strategy_comparison.py`、`run_batch_backtest.py`、`run_full_backtest.py` | 无任何蓝图/服务 import；回测脚本可选择性保留在 `tools/` |
| 调试残留 | `_kline_server.py`、`_audit_dbs.py`、`_diag_okx.py`、`_start_test.py`、`kline/db_probe_login.py`、`kline/verify_auth_login.py`、`kline/test_fetch.py`、`kline/kline_trainer/` | 冒烟/调试用，与运行无关 |
| 产物与噪声 | `screenshot_*.{png,jpeg}`（8 个）、`*.log`（10 个）、`server_out.txt/server_err.txt`、`flask*.log`、`results/*.csv`、`lc_indicators.csv` | 生成物，可留档在 zip 快照里 |
| 重复副本 | `futures/pro3_strategy.py`、`stocks/pro3_strategy.py`（shim）、`templates/nav.html`、`static/css/` | 由宿主统一 |

---

## 七、回滚方案

1. 代码：merge 分支不合 `main` 即无影响；生产环境保留上一版 zip + `git revert` 或切回原目录启动。
2. 数据：合并**只复制不删除**，原 `futureStockTrade/data/`、CSV、SQLite 全程保持不动，直到 Phase 7 验收通过才改名冻结。
3. 端口：原 5000 服务在验收期内保持可独立启动（不注册进 7777 的那份代码不受影响），双轨观察 3 天。
4. 交易安全：整个过程宿主调度器用 `CRYPTO_NO_BACKGROUND=1` 隔离验证，确认无误后才在正式重启时启用；不改任何下单/仓位逻辑。

---

## 八、评审后我要做的第一件事

按你选定的 D1/D2/D3 结论，从 Phase 0+1 开始（基线 + 公共策略层），完成后给你一次可 import 通过的验收报告，再进 Phase 2 期货模块。

---

## 九、执行进度与实测结论（Phase 0~2，2026-10-08）

### 9.1 交付物

| 类型 | 文件 | 说明 |
|---|---|---|
| 搬运脚本 | `migrate_00_baseline.py` | WAL checkpoint(TRUNCATE) + 原项目 zip 快照 + `integrity_check`/表清单 |
| 搬运脚本 | `migrate_01_strategy_layer.py` | 策略层搬迁，`SUBS` 规则表 + akshare try 兜底/裸 sys.path 正则清理 + PROVENANCE 头 + 残留体检 |
| 搬运脚本 | `migrate_02_futures.py` | 期货模块搬迁（含「明确不迁」清单与 cme_api 推迟说明） |
| 验收脚本 | `verify_01_strategy_layer.py` | AST 逻辑等价 / 数值 A/B / 宿主未被污染 三道验收 |
| 验收脚本 | `verify_02_futures.py` | dump-old / dump-new / compare / http 四模式，带 `gate_opener()` 走真实口令流程 |
| 验收脚本 | `verify_02_css_coverage.py` | CSS 回归核算（只取静态 `class=""`，算「原样式表有而宿主无」） |
| 验收脚本 | `verify_02_assets.py` | 页面资源真实请求回环（复现浏览器请求路径，非拼绝对 URL） |
| 迁入代码 | `crypto/market_strategy/`（11 文件） | 公共指标 + Pro3 系列 + 期货 Boll 限价双周期 + 期货版策略子包 |
| 迁入代码 | `crypto/futures/`（7 py + 4 html + 1 js + 4 数据） | 路由、适配器、星标行情、批量趋势分析、akshare 数据层与模板静态 |

### 9.2 Phase 1 实测（策略层）

- **AST 逻辑等价**：11 个文件剥离 import/docstring/sys.path 兜底并塌缩空 try 后比对 `ast.dump`，**逻辑不等价文件数 0** —— 证明「只改导入、没改逻辑」。
- **数值 A/B**：合成数据 + 真实生产数据（akshare 豆粕 M0，908 根日线）双路径逐字段比对，**全部一致**。
- **宿主未被污染**：`crypto/strategy/`（加密版 pro3 / boll_limit）与被并入策略为两套实现，包路径隔离、互不引用，实测零改动。
- **残留体检**：无裸 `from strategies import` / `sys.path.insert` 兜底 / 原项目绝对路径残留。

### 9.3 Phase 2 实测（期货模块并入 7777）

- **路由表 A/B**：原项目 25 条 / 合并后 25 条，缺失 0、新增 0、方法不一致 0 → **完全对齐**。
- **页面**：`/futures/`、`/futures/detail/M`、`/futures/multi/M`、`/futures/star-market` 全 200，且已套用宿主导航与样式（首页 100,710 → 110,244 字节，增量即宿主 `nav.html` + `navigation.css`）。
- **静态**：`/futures/static/futures.js` 200（蓝图 static 解析正确）；`/static/css/style.css` 200 / 101,604 字节（落到宿主）。
- **接口**：4 个文件驱动接口（`api/config`、`api/csv/data`、`api/csv/filtered`、`api/starred`）新旧响应**严格相等**；9 个实时接口（strategy/data、detail、single、multi、boll、dualpro3、star-market/data、batch/trend/status、refresh-status）结构一致。
- **样式统一**：宿主 `style.css` 是原项目样式的**超集**（原项目独有类 0 个）。同名类声明差异仅 2 个 —— `.active`（宿主为超集）、`.multi-period-grid`（宿主 `repeat(7,1fr)`，恰好匹配该页「七周期趋势方向」标题，原基样式为 4 列）。**迁移回归 0**。
- **资源回环**：4 个页面共 118 项站内引用，失败 0（1 个 POST 端点被 GET 打返回 405，属预期）。
- **访问闸门**：`/futures/` 与 `/` 在无口令远程请求下同为 401/403 → 证明蓝图确实注册在闸门**之前**，未出现绕过。
- **宿主回归**：`crypto/task/_smoke_fix_regression.py` → **ALL 37 CHECKS PASSED**；`crypto/_smoke_web_auth.py` → 全部通过。
- **导航**：`resolve_active` 对 `/futures/ → link-futures`、`/futures/star-market → link-futures-star` 命中正确；顶层顺序 监控台0 / 分析记录1 / 行情2 / 交易3 / 个人管理4 / **多市场5** / 工具与系统6。
- **实盘隔离**：全程 `CRYPTO_NO_BACKGROUND=1`，未启动任何交易调度器与监控线程，未触达下单/强平接口。

### 9.4 需要你知道的两件事

1. **`crypto/app.py` 与 `crypto/navigation_service.py` 我没有提交**。这两份是你的在途工作（`app.py` 里混着 expense/diary/checkin/indicator-nav/kline-live 五个蓝图的未提交改动，`navigation_service.py` 整个文件还没入库）。把它们塞进迁移 commit 会把你的未成型改动一起带进去。所以 Phase 2 的 commit 只含迁移自有文件（越界项 0），这两个文件的改动留在工作区，由你按自己的节奏提交。
2. **原项目全程只读**。合并采用「复制 + 改写」，`D:\python\futureStockTrade\` 下任何文件都未被修改或删除，Phase 7 验收通过前保持双轨可独立启动。

### 9.5 一个必须说清楚的导航事实（对 Phase 2 验收口径的修正）

`crypto/navigation_service.py` 的 `DEFAULT_ITEMS` 只在**数据库无导航记录**（`source=default`）时生效。
你这套环境是 `source=db`（23 项、5 个顶层分组，你在「导航配置」里调过），而宿主的既定策略是
「升级不会强塞」，所以新增的「多市场」分组与期货/股票入口**不会自动出现**在你的导航条上。
Phase 2 当时只验了 `resolve_active` 对 DEFAULT_ITEMS 的命中，口径不够，这里补正：

- 页面本身始终可达：直接访问 `/futures/`、`/stocks/` 实测 200；
- 入口一键可加：三项已登记进 `CATALOG_PAGES`，在「导航配置 → 从内置目录添加」点一次即进导航
  （期货监控台 / 期货星标行情 / 股票监控台）；实测 `/api/navigation/catalog` 返回 6 项且含这三项；
- 全新部署（清库或换机）自带完整「多市场」分组，无需手动添加。

我没有代你改写库里的导航配置——那是你手工维护的数据，且宿主策略明确不强塞。要我用一次
`PUT /api/navigation/config` 把这三项直接写进去也可以，你说一声就行。

### 9.6 Phase 3 实测（股票模块并入 7777）

- **AST 逻辑等价**：股票 6 文件 + 期货 4 文件共 10 对，归一化后比对 `ast.dump`，**逻辑不等价 0**。
  其中 `jumei_api.py` / `cme_api.py` 的 `_ROOT = os.path.dirname(...)` 只服务于被删掉的 sys.path 兜底，
  审计里按「整句精确匹配」从两侧同时剔除（不做模糊前缀匹配，避免过度剔除掩盖真实改动）。
- **路由表 A/B**：原项目 10 条 / 合并后 10 条（9 业务路由 + 蓝图 static），缺失 0、新增 0 → **完全对齐**。
- **页面**：`/stocks/`、`/stocks/detail/600519`、`/stocks/multi/600519` 全 200 并套用宿主导航与样式
  （21,898 → 31,823 字节，增量即宿主 `nav.html` + `navigation.css`）。
- **接口**：`api/config` 新旧**严格相等**；5 个实时接口结构一致（strategy/data 2 项、single 7 项、
  detail 11 项、multi 6 个周期、detail?strategy=traditional 11 项）。多周期接口单次实测 54~59 秒
  （串行拉 5m/15m/30m/1H/4H/1D），故 `verify_03_stocks.py` 把超时放到 300s 并对实时接口重试一次 ——
  首轮那次 `新=None timed out` 是上游限流而非迁移缺陷，重试后 200 且与原服务**字节数完全相同**（1,798 = 1,798）。
- **样式**：3 个模板的静态类共 107 个，迁移回归 **0**；同名类声明差异 4 个 —— `.active`（宿主为超集）、
  `.dot-up/.dot-down`（宿主 `#27ae60`/`#e74c3c`，原 `#4caf50`/`#f44336`）、`.dot-wait`
  （宿主灰 `#ccc`，原橙 `#ff9800`）。属设计语言统一的**预期差异**，要还原原视觉需给蓝图自带样式表。
- **资源回环**：3 页面 89 项站内引用，失败 0。
- **凭据体检**：迁入文件中明文 AppCode / SMTP 授权码 / `SECRET_KEY` 默认值 / DB 口令命中 **0** 处。
- **宿主回归**：`_smoke_fix_regression.py` → ALL 37 PASSED；`_smoke_web_auth.py` → 全部通过；
  `/`、`/futures/`、`/futures/star-market`、`/navigation-settings` 合并后仍全 200。

### 9.7 Phase 3 交付物补充

| 类型 | 文件 | 说明 |
|---|---|---|
| 搬运脚本 | `migrate_03_stocks.py` | 含 Step 3 凭据泄漏体检（只报文件与行号，不打印内容） |
| 验收脚本 | `verify_03_stocks.py` | ast / dump-old / dump-new / compare / http / assets 六模式 |
| 修正脚本 | `fix_03_encoding_headers.py` | 把被迁移说明顶下去的 `# -*- coding -*-` 归位到第 2 行（PEP 263），Phase 1 的 3 个文件同批修正；migrate_01/02/03 的插入逻辑一并改为 `header_insert_pos()` |
| 验收脚本 | `verify_02_css_coverage.py`（升级） | 改为可传 `futures/stocks` 参数，默认仍为期货口径 |
| 新建模块 | `crypto/stocks/gateway_config.py` | 六个 `JUMEI_*` 名字的凭据解析，无明文密钥 |
| 迁入代码 | `crypto/stocks/`（6 py + 3 html + stocks.js + stock_config.json）、`crypto/futures/cme_api.py` | 股票监控台 + 两个云市场行情客户端 |

### 9.8 下一步

Phase 4 K 线训练模块（主战场 1649 行 / 53 路由）：`kline/ → crypto/kline_training/`、
`db.py → store.py`、`session['uid'] → session['kline_uid']`、补宿主 `app.secret_key`、复用宿主邮件通道，
并把原 `config.py` 的硬编码 SMTP 授权码与写死在代码里的 `SECRET_KEY` 开发默认值（一个明文占位串，
不在此复述）彻底挡在门外。

### 9.9 Phase 4 实测（K 线训练并入，2026-10-08）

这一层是合并里最重的一块（蓝图 1649 行 / 54 条路由 / 19 个 py / 9 个模板 / 三市场 provider /
SQLite 账号-成绩-对战），所以验收分四层做，每层都对齐前面 Phase 的口径。

- **① AST 逻辑等价**：13 对文件（routes/store/auth_service/mailer/registry/training_service/
  chart_serialize/kline_adapter + providers 5 个）归一化后比对 `ast.dump`，**逻辑不等价 0**。
  审计口径在 verify_01 的归一化器之上补了三件事，都是为了「只允许声明过的差异」：
  ① 按精确整句从新侧还原 `kline_uid → uid`（5 处）与「蓝图级 `context_processor` → app 级」
  （1 处）；② 新增 `_drop_bootstrap_nodes()`，两侧同规则剔除「算目录常量 + 塞 sys.path」样板
  （如 `providers/base.py` 源侧 10 条 / 新侧 1 条，verify_01 原 token 清单认不住 `_THIS_DIR/_KLINE_DIR`
  这类名字，不补就会假报不等价）；③ 源文件带 UTF-8 BOM（`kline_adapter.py`），统一按 `utf-8-sig`
  读入，否则 `ast.parse` 直接报非法字符。
  `td_cache/` 4 个文件属**声明式偏离**（凭据收口 + 未配置早退），不断言等价，逐行打印差异供复核：
  `__init__.py` 21 行、`import_csv.py` 18 行、`test_connection.py` 1 行、`test_crud.py` 1 行。
- **② 路由表 A/B**：两个独立进程各 dump 一次，原项目 54 条 / 合并后 **54 条**，
  按「URL 规则 + 允许方法 + 端点名」比对，缺失 0、新增 0 —— 两侧蓝图名都保持 `kline`，
  所以 `url_for('kline.xxx')` 与 `/kline/static/…` 端点原样可用。
- **③ 业务流程 A/B（55 步，test_client + 各自一次性临时 SQLite）**：这是 Phase 4 验收口径的正面回答。
  两侧跑同一条流程：三品类清单 → 三市场取数 → 自由训练/盲测开局 → 匿名会话与榜单 → 未登录门槛 →
  发码-注册（甲/乙）→ 重复注册 400 → 退出 → 邮箱登录/昵称登录/错密码 401/不存在 404 → 改密 → 改昵称 →
  心跳 → 成绩落库 → 对战建房/加入/图表/双方提交/查看/对比/我的战绩 → 榜单 → 互相备注 →
  给自己备注 400 → 非管理员重置密码 403 → 6 个页面渲染。
  结果：**共同步骤 55 / 仅原项目 0 / 仅合并后 0，严格比对 50 步逐字段相等**（状态码、响应结构、
  归一化后的数据全一致），另外 5 步按结构比对（4 个 akshare 实时接口会漂移 + 盲选用的是无种子
  `random.Random()`，两次运行本就不同题）。对战码（`store._gen_match_code` 的 6 位大写字母数字）、
  验证码、随机 id、时间戳都由 `normalize()` 压成占位符，占位规则要求「至少含一个字母」，
  免得把 `600519` 这类纯数字股票代码也吞掉。
  临时库路径 `data/tmp_isolated_smoke/kline_flow/{old,new}.db`，**你真实的 5 个账号与 18 条成绩/对战
  记录没有被这个流程写过一行**；SMTP 指向 `127.0.0.1:1` 让发信必然失败，从而走开发兜底把验证码回传，
  全程不发真实邮件、不下单。
- **④ 在跑服务的 HTTP A/B + 资源回环**：原服务 5000 对临时新实例 7788（`CRYPTO_NO_BACKGROUND=1`，
  没有动你 7777 那个在跑的进程）。6 个页面全 200 且同时具备宿主导航、宿主样式与 **K 线登录工具条**
  （`/kline/` 10,449 → 22,534 字节，增量即宿主 `nav.html`+`navigation.css`+工具条）；
  4 个静态资源全 200（`/static/js/navigation.js` 原服务 404 属宿主独有，预期）；
  9 个只读接口里 `api/categories` **严格相等**，其余 8 个结构一致（含 stock/futures 两个实时取数）；
  鉴权门槛两侧同状态（`api/pk/mine` 都 401）；6 页面 145 项站内引用，**失败 0**，15 项是 POST-only
  端点（GET 探到 405 说明路由已解析）。
- **⑤ 样式**：`verify_02_css_coverage.py` 升级后支持 `kline`（并修了两处口径：模块名 → 目录名要用
  `kline_training`；原项目基准样式要按选中模块取并集，否则只读根级 `style.css` 会漏判）。
  9 个模板共 293 个静态类，**迁移回归 0**；同名类声明差异 1 个（`.active`，宿主为超集），
  属设计语言统一的预期差异。`futures stocks` 重跑仍 0 回归。
- **⑥ 数据**：`training.db`（172,032 字节）经 sqlite 备份 API 复制为 `data/kline_training.db`，
  8 张数据表行数逐项相等（users 5 / email_verify_codes 4 / user_notes 0 / sessions 8 / matches 6 /
  results 10 / blind_sessions 0 / match_players 3）；该路径已被根 `.gitignore` 的 `*.db` 覆盖，账号数据不入库。
- **⑦ 凭据**：`scan_secret_leak()` 对 18 个迁入/新建文件命中 **0**；全仓库再扫三个已知明文
  （原 SMTP 授权码、TDengine 出厂口令、原 `SECRET_KEY` 默认值）——代码里 0 处，
  仅剩宿主那份 gitignore 内的 `crypto/task/config/email_config.py`（同一发件账号，本来就在用）
  和本文档 §9.8 的描述（已把明文值改为文字说明，注释里的出厂账号口令也一并抹掉）。
  `.gitignore` 补了 `data/app_secret_key.txt`、`data/kline_admin_emails.txt`、`data/jumei_appcode.txt`、
  `data/jumei_cme_appcode.txt`、`data/*.bak`（后两项是 Phase 3 欠的：`gateway_config.py` 当时声称已忽略）。
- **⑧ 宿主回归**：`_run_smokes.py` 16 个 task 层冒烟 **15 通过**，唯一失败 `_smoke_watch_mode.py`
  （4 项，报 `'TrendRangeTrader' object has no attribute 'spec_cache'`）与本次迁移无关：
  用 `FST_ENABLE_KLINE=0` 关掉 K 线蓝图重跑，失败项与报错逐字相同；该文件与 `utils/watch_mode.py`
  都还是未入库的在途改动。`_smoke_fix_regression.py` → ALL 37 PASSED，`/`、`/futures/`、`/stocks/`、
  `/navigation-settings`、`/kline/` 合并后全 200。

### 9.10 Phase 4 交付物

| 类型 | 文件 | 说明 |
|---|---|---|
| 搬运脚本 | `migrate_04_kline_training.py` | 17 项 py 映射 + 模板/静态/数据复制 + sqlite 备份式迁库（带行数校验）+ 凭据体检；uid→kline_uid 改名、sys.path 兜底按行分类删除（有「别处仍在用则保留」安全阀） |
| 验收脚本 | `verify_04_kline_training.py` | ast / dump-old / dump-new / compare / flow-old / flow-new / flow-compare / http / assets 九模式；`VERIFY_NEW_BASE`、`VERIFY_OLD_BASE` 可切端口 |
| 验收脚本 | `verify_02_css_coverage.py`（升级） | 支持 `kline` 参数、模块名→目录名映射、原项目基准样式取并集 |
| 新建模块 | `crypto/kline_training/settings.py` | K 线专用配置：SQLite 路径、`SECRET_KEY` 三级解析、会话策略、管理员名单、验证码策略、`SMTP_CONFIG`（引用宿主唯一发件配置）、TDengine 只读环境变量、`state()` 脱敏自检；无任何代码内明文凭据 |
| 新建模板 | `crypto/kline_training/templates/kline_account_bar.html` | 宿主导航是数据驱动的、没有用户区，故按宿主设计语言补 K 线蓝图专属登录工具条（昵称/个人中心/登出 + 15s 在线心跳），随 8 个模板 include |
| 迁入代码 | `crypto/kline_training/`（19 py + 8 原模板 + 2 js）、`crypto/kline_training/td_cache/` | 训练台/盲测/对战/战绩/账号 + 默认关闭的 TDengine 缓存工具 |
| 迁入数据 | `data/kline_training.db`（gitignore） | 5 账号 / 8 训练会话 / 6 对战 / 10 成绩，行数与源库逐项相等 |
| 宿主接线 | `crypto/app.py`（未提交）、`crypto/navigation_service.py`（未提交） | 蓝图注册落在 Web 闸门之前 + `app.secret_key`/会话参数；导航加「K线训练台」与 catalog 三项（kline / kline-pk / kline-blind） |

### 9.11 Phase 4 留下的一句话结论与下一步

- 结论：**合并后的 `/kline` 与原 5000 端口的 `/kline` 在业务行为上逐步一致**（55 步流程 + 54 条路由 +
  13 对文件 AST 等价），差异只有三类且都是有意为之：宿主访问闸门叠加、宿主设计语言与登录工具条、
  凭据全部外置。
- 待你确认（与 §9.5 同一件事）：你的导航是 `source=db`（revision 6，23 项手工调过），
  所以 `/futures/`、`/stocks/`、`/kline/` 三个入口都只在「导航配置 → 从内置目录添加」里，
  不会自己冒出来。要我发一次 `PUT /api/navigation/config` 把它们写进你的库，说一声即可。
- 另一件要提醒的：合并后的 7777 进程还是 Phase 3 时代码在跑，**`/kline` 目前只有临时实例（7788）验过**，
  正式生效要等 Phase 6 的低峰重启（原 5000 仍在，所以现在什么也没丢）。
- 下一步：Phase 5 —— `requirements.txt` 收口（akshare/Werkzeug/python-dotenv/taospy/cryptography，
  `python-okx` 下限抬到 `>=0.3.0`）+ 全仓库明文密钥复扫。

---

## 十、Phase 5 实测记录（配置、密钥与依赖收口，2026-10-08）

验收脚本：`verify_05_secrets_and_deps.py`（两项检查 + 一个自检），退出码 **0**。

### 10.1 依赖收口：把清单和代码对齐

原计划要补的五项，实测下来只有三项成立，另两项按「代码里到底有没有 import」判掉：

| 包 | 结论 | 依据（实测） |
|---|---|---|
| `akshare>=1.18,<2.0` | **补**，本机 1.18.64 符合 | `crypto/futures/akshare_api.py`、`crypto/stocks/stock_akshare_api.py`、`crypto/market_strategy/futures_boll_limit_dualtimeframe.py`、`crypto/kline_training/providers/{stock,futures}_provider.py` 共 4 处运行链路引用 |
| `Werkzeug>=3.0,<4.0` | **补**，本机 3.1.3 符合 | `kline_training/auth_service.py`、`store.py` 用 `werkzeug.security` 做口令哈希；`expense_routes.py` 用 `werkzeug.exceptions`。锁上限是为了默认哈希算法不漂移，否则老 `password_hash` 会校验不上 |
| `cryptography>=41.0` | **补**，本机 46.0.3 符合 | 业务代码 0 处 import，但 PyMySQL 连 MySQL 8 的 `caching_sha2_password` 握手必须有它；宿主一直连 MySQL，之前只在服务器上手工装过 |
| `python-dotenv` | **不补** | 全仓库（排除原项目）0 处 `load_dotenv`。原项目 `config.py` 读 `.env`，合并后 `settings.py`/`gateway_config.py` 走「环境变量 + `data/*.txt`」，加进来是假依赖 |
| `taospy` | **注释形式声明为可选** | 唯一使用方 `crypto/kline_training/td_cache/__init__.py` 用 `try/except` 包住 `from taosrest import connect`，不装也能启动；该缓存本就闲置（宿主有自己的 1 分钟链路） |

顺带抓出三处**宿主原有**缺口（不是合并引入，但同一口径）：
`requests`（`market_cap_updater.py` 顶层硬 import，以前靠 akshare 传递依赖救的）、
`psutil`（`process_lifecycle.py` 的进程存活判定，缺包时自愈归因会静默失真）、
`Markdown`（`capability_routes.py` 的「OKX 能力」页渲染）。
`bcrypt` 与 `taospy` 一样按可选注释处理——日记本口令是 argon2 → bcrypt → 标准库 pbkdf2 三档探测。

三轮判定，避免「看着齐了其实没齐」：
1. **正向**：代码 import → 清单声明。演示/调试脚本（`crypto/demo/`、`_probe_*`、`data/_*`）
   单独列为 `[仅演示]`，不参与硬性判定，否则 `influxdb_client`/`playwright`/`websocket` 会一直刷屏。
2. **反向**：清单声明 → 代码是否有人用。`tzdata`、`cryptography` 属「无 import 但必须有」，
   走带理由的白名单，不当冗余。
3. **交叉核对**：不信名字，直接问解释器。对每个 import 名跑 `importlib.util.find_spec`，
   加载路径落在仓库之外就是真三方依赖。这一轮才把「目录名恰好和某个包同名」的盲区堵住
   ——`crypto/task/notification/` 这种靠 `sys.path` 裸导入的自家包，之前会被误报成缺声明。

`python-okx` 保持 `>=0.3.0`（本机 0.4.3），宿主实盘链路一直在用这条线，不去动它。

### 10.2 明文密钥复扫：抓到一处真会进公开仓库的凭据

扫描范围用 `git ls-files --cached --others --exclude-standard` 取「git 看得见的文件」，
所以 **`.gitignore` 掉的 `data/` 凭据不在范围内，也不该在**；被扫到的 512 个文件里
包含未跟踪但也未忽略的文件——这正是漏网凭据的藏身处。

- **命中并已修复**：`crypto/ApiUtils/alicloud_market.py`、`cme_futures_utils.py`、
  `jumdata_kline_utils.py` 三份工具类都把阿里云 API 市场 **AppCode 明文**写成模块常量，
  `alicloud_market.py` 的注释里还额外写着 AppKey + AppSecret。这三份文件当时是
  `?? untracked`（未被 ignore），Phase 7 一句 `git add -A` 就会把它们推上公开仓库。
  处理方式：新增 `crypto/ApiUtils/appcode.py`，按「环境变量 → `data/jumei_appcode.txt` →
  空串并明确抛错」解析；AppCode 与 AppKey/AppSecret 移到
  `data/jumei_appcode.txt` / `data/jumei_appkey_secret.txt`（后者补进 `.gitignore`）。
  三种运行模式实测都能取到凭据：包内导入、脚本直跑（cwd 在 `ApiUtils/`）、
  异地 cwd（按 `__file__` 反推项目根的 `data/`）。
  **git 历史确认干净**：`git log --all -S <AppCode>`、`-S <AppSecret>` 均无输出，
  `git grep <AppCode> HEAD` 无输出 → 凭据从未进过版本库，不需要因此轮换密钥。
- **最终结果**：高危 0、中危 0，降级为提示 17 条（全是冒烟/演示脚本里的假口令与测试 IP）。
- **判定口径**（都是为了不把真东西漏掉同时不刷屏）：
  注释行同样要扫（原项目的事故之一就是凭据进注释）；但「读环境变量 *or* 写死缺省」
  这种后门形态不接受"安全形态"豁免，且只认赋值目标本身就像凭据名的行；
  环境变量名（全大写）、`{占位}` 模板、含中文的说明、`'api_key' in argv` 这类成员判断
  一律视为非凭据；输出只回显前 2 字符与长度，脚本自身被提交也不会变成泄密载体。
- **脚本自检**（冒烟全绿证明不了任何事，反向也要证）：临时埋 5 种真实泄露写法
  ——硬编码口令、`os.environ.get(...) or 'fallback'` 后门、32 位 AppCode 明文、
  带账号口令的连接串、16 位授权码形状。结果 5 条全部命中高危，连接串那行还额外
  命中公网 IP → 判定链没被降噪规则削弱，验完即删该临时文件。

### 10.3 凭据语义收口的两处核对

- **`.env` 语义**：合并进来的模块不再有 `.env` 读取点。K 线侧凭据三级解析在
  `crypto/kline_training/settings.py`（环境变量 → `data/` 外置文件 → 现场随机并落盘），
  行情网关侧在 `crypto/stocks/gateway_config.py`，本轮再加 `crypto/ApiUtils/appcode.py`，
  三处同一口径。原 `config.py` 里的 SMTP 授权码与默认 `SECRET_KEY` 在 Phase 4 就已挡在门外。
- **OKX 密钥**：`api_config` 在 `crypto/kline_training/`、`crypto/futures/`、`crypto/stocks/`
  三个目录里 **0 处引用**。原因是 K 线训练的 crypto provider 打的是 OKX 公开行情接口
  （`/api/v5/market/candles`、`/history-candles`、`/public/instruments`、`/market/tickers`），
  不需要签名也不需要 Key。所以「不引入新副本」这条不是靠复用做到的，是根本没用上——
  宿主 `crypto/api_config.py` 一行未动，仍是 gitignore 状态。

### 10.4 回归与遗留

- 临时实例（`CRYPTO_WEB_PORT=7789`，`CRYPTO_NO_BACKGROUND=1`）起来后复跑：
  `/kline` 资源回环 145 项引用 0 失败；`/futures` 4 页 0 失败；`/stocks` 3 页 0 失败，
  且 Phase 3 的 HTTP A/B（对 5000）全部通过。带闸门的实测对照：
  7777（Phase 3 代码）`/kline/=404`，7789（含 Phase 4 接线）`/kline/=200 22,534B`
  → 再次印证 `/kline` 要等 Phase 6 重启才在正式端口生效。临时实例用完即停，
  7788/7789 均已无监听。
- 本轮**没有**重跑 `_run_smokes.py`（16 个 task 层冒烟，Phase 4 已跑过 15/16，
  唯一失败是用户在途的 `_smoke_watch_mode.py`）：Phase 5 只改依赖清单、忽略规则、
  `ApiUtils/` 三份无人 import 的工具类和两个新脚本，交易运行链路 0 改动。
  全量回归按计划留在 Phase 6。
- 顺手让 `verify_02_assets.py` / `verify_03_stocks.py` 支持 `VERIFY_NEW_BASE` 环境变量，
  和 `verify_04` 对齐，验收时不必拿用户的 7777 当试验田。

### 10.5 交付物与下一步

| 类型 | 文件 | 说明 |
|---|---|---|
| 验收脚本 | `verify_05_secrets_and_deps.py` | 依赖收口（正向/反向/按加载位置交叉核对 + 版本区间实测）与明文密钥复扫（含后门形态识别、夹具降级、占位排除）；支持 `--deps` / `--secrets` |
| 新建模块 | `crypto/ApiUtils/appcode.py` | 云市场 AppCode 三级解析 + `require_appcode()` 明确报错；未跟踪文件，Phase 7 一并决定去留 |
| 凭据改造 | `crypto/ApiUtils/{alicloud_market,cme_futures_utils,jumdata_kline_utils}.py` | 明文常量 → 解析函数，实例化时缺凭据直接抛错而非带空 header 去打接口 |
| 凭据外置 | `data/jumei_appcode.txt`、`data/jumei_appkey_secret.txt` | 均在 `.gitignore` 内（后者本轮补入） |
| 清单 | `requirements.txt` | +`Werkzeug`/`akshare`/`cryptography`/`requests`/`psutil`/`Markdown`；`taospy`/`bcrypt` 以注释说明为可选；`.env`/dotenv 的排除理由写进文件 |
| 忽略规则 | `.gitignore` | 补 `data/jumei_appkey_secret.txt` |

下一步：Phase 6 —— 端口 5000 退役与单实例守护并入宿主根 `app.py`、`FST_ENABLE_*` 三个开关
落地、新增 `_smoke_merge_{futures,stocks,kline}.py` 纳入 `_run_smokes.py`、
**用生产真实数据做合并前后 A/B**（K 线的 crypto 分支必须在能连 OKX 的环境复验一次，
Phase 4 那轮是合成行情），最后低峰重启 7777 并留下重启归因日志。

---

## 十一、Phase 6 实测记录（部署入口与前端收口，2026-10-08）

### 11.1 单实例守护并入宿主根 `app.py`

原 `futureStockTrade/app.py` 的 `connect_ex` 探端口逻辑迁到宿主根 `app.py`，
但**位置**与原方案不同，这一步是刻意的：

| 口径 | 原项目 | 合并后 |
|---|---|---|
| 探测时机 | `app.run()` 之前 | `from crypto.app import app` **之前** |
| 原因 | 原项目 import 阶段无副作用 | 宿主 `crypto/app.py` 在 import 阶段就拉起 DB 预热与调度线程（`_boot_scheduler_background`），那是一个**会下单**的调度器 |

若把守护留在 `app.run()` 前，第二个实例会先把调度器跑起来才被拦下。实测：
7777 在跑时执行 `python app.py` → 直接打印排查指引并 `exit 1`，
日志里没有任何 `[Boot]`/`[SCHEDULER]` 行，即 import 阶段未被触达。

三条兼容口径：
- Gunicorn/uWSGI 以 `app:app` 加载走 import，不经过 `__main__`，守护不生效（实测
  `import app` 正常拿到 `app.app`，多 worker 部署不受影响）。
- 并存第二个验证实例：`CRYPTO_ALLOW_DUP_PORT=1`（提示里写明双实例的调度器会重复下单），
  更推荐 `CRYPTO_WEB_PORT=7789` 换端口。
- 探测本身出错（端口越界等 `OSError`）不拦启动，避免守护反过来变成故障源。

启动横幅同步补齐三个合并模块的入口链接，并按 **blueprint 是否真挂上**
（`app.blueprints`）打印——开关关掉或注册降级时不会给出死链。

### 11.2 一个 Phase 2 遗留的真实缺陷：模板跨目录遮蔽（已修）

合并后期货蓝图的 `template_folder` 与宿主 `crypto/templates` 同处一条 Jinja 搜索链，
而两侧都有裸名 `star_market.html`：

| 侧 | 文件 | 行数 | 页面 |
|---|---|---|---|
| 宿主 | `crypto/templates/star_market.html` | 650 | `/star-market` 加密货币星标币种行情 |
| 期货 | `crypto/futures/templates/star_market.html` | 908 | `/futures/star-market` 期货星标品种行情 |

**宿主目录优先解析**，因此 `/futures/star-market` 一直渲染的是加密货币那个模板：
HTTP 200、页头写着「星标币种行情」、前端 fetch 打的是 `/api/star-market/*`（宿主的
接口），期货星标页实际上从未真正上线过。Phase 2 的验收只比过状态码与「是否套用宿主
导航/样式」，所以两轮 A/B 全绿也没抓到——正是记忆里那条
「冒烟全绿可能证明不了任何事」。

修复（只做命名收敛，不动宿主页面）：
- `crypto/futures/templates/star_market.html` → `futures_star_market.html`（`git mv`），
  与同目录 `futures_index/futures_detail/futures_multi_period` 的前缀约定一致；
- `crypto/futures/routes.py` 的 `star_market_page()` 改渲染新名，并就地写清为什么必须带前缀。

新建核查脚本 `verify_06_template_collisions.py`（退出码 0/1/2）：
① 从**运行时注册状态**枚举 Jinja 搜索目录（`app.template_folder` + 每个蓝图
`template_folder`，新挂蓝图不需改脚本），②扫 `.py` 的 `render_template` 与 `.html` 的
`{% include/extends %}` 求同名交集，③实测 `jinja_env.get_template()` 的解析归属并打印
「被遮蔽」的一侧，④另列「生产代码引用但文件不存在」的缺失模板。
演示/自检脚本（`verify_*`、`_smoke*`、`*demo*`、`tools/`）不参与判定，避免自指噪音。

**自证有效性**：往宿主目录临时植入一个假的 `futures_index.html`，脚本如实报出
「冲突 + 蓝图侧被遮蔽」并返回 1；删除后回到 0。当前实测：4 个搜索目录、41 个模板名、
0 冲突、0 缺失。

### 11.3 `FST_ENABLE_*` 开关与降级提示页

三个模块的注册收口到 `_register_fst_module()`，状态记进 `_FST_MODULES`
（`ok` / `disabled` / `failed` + 异常原文）：

| 开关 | 关掉的模块 | 关闭原因的典型场景 |
|---|---|---|
| `FST_ENABLE_FUTURES=0` | `/futures` | 服务器无国内网络出口，akshare 取不到数 |
| `FST_ENABLE_STOCKS=0` | `/stocks` | 同上 |
| `FST_ENABLE_KLINE=0` | `/kline` | 训练库/会话密钥未就绪，或只想跑交易主功能 |

模块没挂上时，前缀不再回落成裸 404，而是渲染新增的降级提示页
`crypto/templates/fst_unavailable.html`（503）：状态、原因、访问路径、恢复步骤
（含该模块的环境变量名），JSON 请求回 503 结构化体。样式沿用宿主 `style.css` 的
CSS 变量与卡片体系，并 include 宿主 `nav.html`——按「以目标站设计语言为准」。
前缀兜底路由只在模块**确实未挂载**时注册，挂载成功完全不拦截；
`/kline/` 的兜底不会误吞宿主的 `/kline-live`（按路径段匹配，不按字符串前缀）。

### 11.4 合并模块冒烟与全量回归

新增（均可 `python -m crypto._smoke_merge_xxx` 直跑，`--off` 跑关闭态）：

| 脚本 | 断言要点 |
|---|---|
| `_smoke_merge_helper.py` | 公共夹具：`CRYPTO_NO_BACKGROUND=1`、测试口令走 `CRYPTO_WEB_TOKEN`、`CRYPTO_PLAN_DATA_DIR`/`CRYPTO_KLINE_SQLITE_PATH` 指向临时目录；`under_prefix()` 做**路径段级**匹配 |
| `_smoke_merge_futures.py` | 蓝图先于闸门（未登录 401）、27 条路由与 Phase 2 基线一致、`futures_star_market.html` 解析归属、期货页渲染期货模板而宿主页未被反向影响、宿主导航与 style.css 注入、关闭态 503 提示页 |
| `_smoke_merge_stocks.py` | 闸门顺序、10 条路由基线、模板归属、2 个页面可达、**零交易端点红线**（路由名不得含 place/cancel order、/trade、/buy、/sell、liquidat） |
| `_smoke_merge_kline.py` | 54 条路由基线、模板归属、`app.secret_key` 长度达标且 `settings.py` 无写死字面量、密钥落在临时目录、SQLite 指向临时库并建 9 张表（**未打开真实 `data/kline_training.db`**）、闸门 cookie `ct_web_gate` 与 Flask `session` 不同名、会话键已收口为 `kline_uid`、K 线 crypto provider 不引用 `api_config` |

`_run_smokes.py` 扩展：task 层 + `crypto/_smoke_merge_*`（开启态与 `--off` 各一轮），
统一 `-X utf8`，失败列表非空时以退出码 1 结束，可直接当 CI 门。

本轮实测：**25 通过 / 1 失败**（19 个 task 层 + 6 个合并模块轮次：三模块开启态与
`--off` 关闭态各一轮；夹具文件 `_smoke_merge_helper.py` 已从 runner 的 glob 里排除）。
唯一失败是宿主既有的 `_smoke_watch_mode.py`
（4 例，`'TrendRangeTrader' object has no attribute 'spec_cache'`）：生产代码
`_resolve_position_size()` 已改为直接用 `self.spec_cache`，而该冒烟用
`cls.__new__(cls)` 手工搭夹具，桩了 `_steps`/`_resolve_size` 却没注入 `spec_cache`。
属既有测试夹具缺口，与本次合并无关（`git log` 显示 `trend_range_trader.py` 最近改动是
`fe98e01`，合并分支未碰它），修法是夹具里补一行 `self.t.spec_cache = …`，
留待单独处理，不混进迁移提交。

### 11.5 交付物与待用户决策项

| 类型 | 文件 | 说明 |
|---|---|---|
| 入口 | `app.py` | 单实例守护前置 + 三模块入口链接按注册状态打印 |
| 注册收口 | `crypto/app.py` | `_register_fst_module()` / `_FST_MODULES` / 前缀兜底路由 |
| 降级页 | `crypto/templates/fst_unavailable.html` | 状态 + 原因 + 恢复步骤，宿主设计语言 |
| 缺陷修复 | `crypto/futures/templates/futures_star_market.html`、`crypto/futures/routes.py` | 模板改名 + 渲染改名 |
| 验收脚本 | `verify_06_template_collisions.py` | 模板冲突/遮蔽/缺失核查，含植入自证 |
| 冒烟 | `crypto/_smoke_merge_{helper,futures,stocks,kline}.py`、`_run_smokes.py` | 三模块开启态/关闭态各一轮，纳入统一 runner |

仍需用户拍板（Phase 6 收尾两问 + 两项可选）：
1. **7777 低峰重启窗口**：`/futures`、`/stocks`、`/kline` 与本轮全部修复都要重启才在
   正式端口生效（5000 仍在跑，所以现在没丢东西）；重启同时留重启归因日志。
2. **生产真实数据 A/B**：期货/股票同一批品种新旧比对，K 线 crypto 分支必须在**能连
   OKX** 的环境复验（Phase 4 那轮用的是确定性合成行情，证明不了真实链路）。
3. 导航是否合并：用户线上导航是 `source=db` 的自定义配置，新模块不会自动出现，
   可一次性 `PUT /api/navigation/config` 加 `/futures/`、`/stocks/`、`/kline/`。
   已备好工具并**只跑过 dry-run**（未写入，等用户点头）：
   `python -X utf8 tools/nav_add_merged_modules.py [--apply]` —— 幂等补齐
   「多市场」分组与四条入口，已有同 URL 条目自动跳过，走 `repo.save_full()` 的
   `base_revision` 乐观锁，读不到库时直接中止（绝不把用户配置覆盖成默认）。
   当前实测输出：`source=db revision=6，条目 23 个`，需补 1 个分组 + 4 条入口，
   导航改动即时生效、不需要重启 7777。
4. 阿里云 AppKey/AppSecret 是否轮换（明文凭据已移出仓库，git 历史干净，但曾在本机
   源码里存过）。

### 11.6 提交拆分：合并接线终于进了版本库（2026-10-08 补）

Phase 6 落地时踩到一个更早的坑：**`crypto/app.py` 在 Phase 1~5 的任何一次提交里都
没被改过**。用 `git show HEAD:crypto/app.py | grep -E "futures|stocks|kline_training"`
验证是 0 命中 —— 也就是说三个蓝图的注册代码一直只存在于我的工作区，干净检出的
分支根本挂不上 `/futures`、`/stocks`、`/kline`，前面所有验收跑的其实都是「未提交状态」。

不能简单 `git add crypto/app.py`：该文件工作区改动是 552 增 / 58 删，我的合并块
（140 行）与**用户自己的在制功能**（每日多维打卡、指标导航、BTC 1 分钟 K 线、
导航管理，依赖的 `crypto/checkin_routes.py` 等还是未跟踪文件）落在同一个
`@@ -96,0 +108,171 @@` hunk 里，整文件提交会把别人没写完的代码一起钉进历史。

拆法见 `data/_stage_fst_wiring.py`：取 HEAD 原文，只把合并块插到
`app.register_blueprint(instinct_bp)` 之后，`hash-object -w --path` + `update-index`
**只写索引不动工作区**。安全闸是脚本自带的四条自检：锚点在 HEAD 必须唯一、块内
必须含 `_FST_MODULES`/三个 `FST_ENABLE_*`/`fst_unavailable.html`、块内不得混入
`checkin_bp` 等宿主 WIP、结果 `ast.parse` 通过。索引内容相对 HEAD 是纯 +141 行，
工作区文件提交前后 md5 一致（`bf49db23…`），用户 WIP 一行未少。

拆成两个提交：`06d0a97`（守护/模板遮蔽/降级页/冒烟/工具/文档，13 个文件）与
`43a4279`（仅合并接线 + 拆分脚本）。

**干净检出复验**（这一步才是「迁移完成」的真凭据）：`git worktree add --detach
.wt_head HEAD` 在没有一行未提交改动的检出里跑，工作区里那些未跟踪的宿主文件
不参与，所以跑的就是纯仓库状态：

| 项 | 结果 |
|---|---|
| `_smoke_merge_kline.py` | PASS（54 路由基线、临时 SQLite 建 9 表、provider 无密钥） |
| `_smoke_merge_futures.py` / `_smoke_merge_stocks.py` | PASS（27 / 10 路由基线、股票零交易端点红线） |
| 三模块 `--off` 关闭态 | PASS ×3（503 降级页而非 404） |
| `verify_06_template_collisions.py` | 退出 0（0 冲突 / 0 缺失） |

验完 `git worktree remove --force .wt_head` + `worktree prune`，目录已确认删除，
`git worktree list` 只剩主检出。仍未做且必须用户点头的：生产真实数据 A/B、低峰
重启 7777、端口 5000 退役、`nav_add_merged_modules.py --apply`、AppKey 轮换。

---

## 十二、Phase 7 实测记录（文档归档与模块边界，2026-10-08）

被并入方的四份文档已按原样复制进 `doc/futureStockTrade归档/`（原项目仍只读、未删），
并补了一份 `README.md` 写清每份的来源路径与「今天还能不能用」：

| 归档文件 | 状态说明 |
|---|---|
| `数据存储审计报告.md` | 历史对照用；合并后数据落点已变，运行口径看 `数据存储说明.md` 第 8 节 |
| `futures模块代码汇总.md` | 代码现居 `crypto/futures/`，文档内相对路径需换算 |
| `股票模块复刻分析.md` | 代码现居 `crypto/stocks/`；「只读行情、零交易端点」红线仍有效 |
| `jumei_stock_api/`（5 份 + 3 个样例 JSON） | 仍有效，是股票模块数据源的接口笔记；`api.md` 在原项目就是 0 字节，保留原样 |

复制前后各扫一遍凭据：源文档里 6 处 `appcode/token/password` 命中全是**字段名与
`<你的AppCode>` 占位符**，无真实值；归档后再跑 `verify_05_secrets_and_deps.py`
（扫 520 个 git 可见文件）高危 0 / 中危 0，退出码 0。`.gitignore` 已确认覆盖
`data/kline_training.db`（`*.db`）、`data/app_secret_key.txt`、`data/jumei_appcode.txt`，
归档动作不会把凭据或真实成绩库带进提交。

模块边界文档两处收口：`项目说明.txt` 新增【多市场模块边界】（四个模块的路由、代码
目录、数据落点 + 归档文档指路），`数据存储说明.md` 新增第三节第 8 小节（三个合并
模块的数据表与 D3「先原样搬、格式后续统一」的现状说明），密钥文件清单补上
`data/app_secret_key.txt` 与 `data/jumei_appcode.txt` 两项。

Phase 7 剩下的都是要用户点头的动作：分支合并与打 tag、原目录改名冻结、
`tools/migrate_phase1~4.py` 是否执行（D3 后续统一格式时才动，且需先对齐 `db_schema.sql`）。
