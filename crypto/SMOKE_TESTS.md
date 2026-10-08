# 冒烟脚本索引

本文件是仓库内所有 `_smoke_*.py` 脚本的统一索引。**下次做任务前，先查这里**——
避免重复造脚本、避免误跑会写库/发信/调真实 API 的脚本。

> 技术文档 `crypto/task/定时任务量化交易系统技术文档.md` 第 532 行已把
> `_smoke_rg2.py` / `_smoke_dual_position.py` / `_smoke_fix_regression.py` 列为
> 「改动交易链路后应全部重跑」的常驻回归脚本。本索引补全其余 30 个。

## 安全等级

| 等级 | 含义 | 能否随便跑 |
|---|---|---|
| 🔒 纯离线 | 不连 DB、不调 API、不发邮件（内存沙箱 / 临时文件 / 日志文件） | ✅ 随时 |
| 🛡️ 写库 + 还原 | 写 DB，但 finally 无条件恢复快照（或自清理插入的行） | ✅ 可随时（生产调度器并发可能干扰断言） |
| ⚠️ 写库 + 租约 | 写 DB/KV，需取维护租约暂停生产巡检 | ⚠️ 生产环境跑要避开巡检高峰 |
| 🌐 连真实 API | 调 OKX 接口 | ❌ 实盘时段慎跑 |

## 🔒 纯离线（31 个）

| 脚本 | 覆盖模块 | 何时重跑 |
|---|---|---|
| `_smoke_strategy_gate.py`（根目录） | 策略计算锁（`crypto/task/strategy_gate.py`）—— 两种导入身份共用同一把锁、两种降级方式 | 改 `strategy_gate.py` 或 `builtins` 传递方式 |
| `crypto/_smoke_config_save_flag.py` | 配置写库失败如实上报（问题#7）—— `save_config` 返回 DB 主存结果、`last_save_db_ok()` 一次性消费、失败时文件仍落盘 | 改 `real_strategy_adapter.save_config` 或 `app._coin_cfg_warn()` |
| `crypto/_smoke_fixed_coins.py` | 固定币种动态管理 + crypto_coins 宇宙一致性（监控台「移除选中/提升为固定/CSV同步固定」）—— `remove_fixed_coins`/`promote_floating_to_fixed`/`sync_fixed_from_csv` 及 `_universe_remove_coins`/`_universe_add_coins`：移除同步剔除选中/星标/浮动 + 从宇宙(DB表+CSV)彻底删除、CSV同步不复活下架币；提升写入宇宙(DB+CSV)、CSV同步不丢弃已提升币；至少保留一个/空宇宙护栏、去重、CSV表头列数不漂移(DB宽表头 vs CSV窄表头) | 改这三个函数、`_universe_*` helper、`get_csv_coins` 语义，或 `load_config`/`save_config`/`market_data_repo` 契约时 |
| `crypto/_smoke_longtermism.py` | 长期主义 v1.1（`plan_routes._calc_presence_days`）—— 在场天数去重、最佳连续 | 改 `plan_routes.py` 在场/连续天数逻辑 |
| `crypto/_smoke_okx_capability.py` | OKX 能力清单阅读页 `/okx-capability` —— 62 场景覆盖：蓝图/模板/nav.html **及全站 15 个模板**可编译（nav 被每页 include，改它等于改全站）、页面 200、渲染结构与 `doc/OKX交易操作能力清单.md` 逐项对账（表格数=源分隔行数、代码围栏配对、小节数=h2+1、顶部 chip 三数=实际 DOM 数、tag 闭合、转义竖线已还原、图示 10/10 注入且 svg 可读）、TOC 锚点与正文 id 一一对应且唯一、窄屏可读性四条硬约束（目录收浮层/flex 不压扁/长串可断行/高亮跟随不劫持整页滚动）、工具矩阵三数自洽（178 CLI − 12 仅本地 = 166 = 官方 totalTools、去重工具数 = 166 − 6）、页面内嵌 JSON 与 `/api/matrix` 同源、导航入口注册 + 当前页高亮 + 其它入口不误高亮、文档 mtime 变化必须重渲染、`markdown` 库缺失时降级为纯文本+告警条不 500、证据文件缺失仍 200 且明确告知 | 改 `capability_routes.py`、`capability_diagrams.py`、`templates/okx_capability.html`、`static/js/okx_capability.js`、`templates/nav.html` 增删入口，或改了 `doc/` 清单/`data/_okx_list_tools.json` 后 |
| `crypto/_smoke_sysmon.py` | 内存自监控 + 系统监控（`memory_watchdog` / `system_monitor`）—— RSS 读取、JSONL 写入、状态字段 | 改 `memory_watchdog.py` 或 `system_monitor.py` |
| `crypto/_smoke_task_switch.py` | 定时任务统一开关 —— `scheduler.task_switch` 两层合并（kv 持久开关 load_switch_state 打桩不碰 DB + config.json 文件急停只能单向关）；文件层语义（缺省=开/模块级/全局/非法任务名容错）、fail-open（文件缺/JSON坏/块非 dict 均按开）、mtime 缓存免重启生效、分析纪律 email.enabled 总开关与任务开关接线（_email_send_ok 三态、register 不注册分支）、每日归零开关分支 | 改 `scheduler.py` 的 `task_switch`/`_switch_store_on`/`_switch_file_on`/`SWITCHABLE_JOB_LABELS`/`register_job` 守卫、`task/monitor/analysis_discipline.py` 的 `_module_switch_on`/`_email_send_ok`/`register_discipline_job`、`task/memory_daily_reset.py` 的注册分支，或 `discipline_repo.py`/`discipline_routes.py` 的 email 配置字段；开关文件路径与 kv 读层都在脚本内打桩，不碰真 config.json/数据库。接口层（/api/task/job/<id>/switch 等）用临时隔离实例 curl 验过，尚未进常驻冒烟 |
| `crypto/_smoke_web_auth.py` | Web 访问闸门（问题#1）—— 41 场景覆盖判定矩阵（未配口令=只放行回环、配口令=回环也要过口令）、cookie 存 HMAC 而非口令原文、口令来源优先级与文件回退、`safe_next` 挡开放重定向，并用自建极小 Flask app 验真接线（403/401 JSON与登录页、不发/发 cookie、HttpOnly+SameSite=Lax、`?token=` 引导后抹掉地址栏口令、Bearer、登出、静态资源免鉴权） | 改 `crypto/web_auth.py` 任何判定分支或 `init_app` 里的钩子；**注意它不导入 `crypto.app`**，闸门在真 app 上的接线要手工在线验（启服务后跑一轮 HTTP，见《闸门改动的在线验证步骤》） |
| `crypto/task/_smoke_boot_resume.py` | 启动归因 + 自愈决策矩阵 + 每日内存归零 + 启动期 DB 预热重试 —— 71 场景覆盖退出标记原子写/六态归因（first_run / mem_restart / daily_restart / abnormal_death / cold_start / **parallel_instance 并发实例**）/SIGTERM 捕获（写标记 + 不吞停止请求 + 不抢别人处理器 + 非主线程安全回退）/真探活与 PID 复用（起真子进程、用真已回收 pid，不写死 999）/**最新一条是「不带 pid 的 embedded」这一生产常态**/决策矩阵全组合（夜间接回、白天预告+否决窗、熔断、并发实例不占额度、窗口外不熔断、计划内不占额度、熔断信带排障指引、台账留心跳作者 pid）/worker 集成/归零四条拒绝执行条件/预热退避重试（不阻塞主线程、中途成功即停、默认不打栈、开 `CRYPTO_DB_WARM_TRACE` 必给栈） | 改 `crypto/process_lifecycle.py` 的判定或策略、改 `scheduler.py` 的 `schedule_auto_resume`/`_auto_resume_worker`、改 `task/memory_daily_reset.py`、改 `crypto/database.py` 的 `warmup_async`、或改 `app.py` 里 `install_sigterm_marker()` 的接线位置（必须是主线程导入阶段）（状态文件靠 `CRYPTO_LIFECYCLE_DIR` 指到临时目录，**必须在 import 被测模块之前设置**；它还会桩掉 `crypto.task.trend_range_trader`，改成模块级 import 会失效） |
| `crypto/task/_smoke_cl_ord_id.py` | 下单幂等打标与「结果未知」反查（问题#4）—— 38 场景覆盖 clOrdId 格式（**口径来自 OKX 实测：纯字母数字 ≤32**，并按源码扫出全部下单前缀逐一验）、打标全覆盖、异常后 found 不重发、absent 可重发、unknown 停手需人工核实、OKX 客户端 HTTP 超时已抬高 | 改 `trade_executor.py` 的 `gen_cl_ord_id` / `probe_*_by_*_id` / `execute_trade` / `execute_reduce_only_order` / `execute_chase_limit_order` / `_apply_http_timeout`，或新增下单出口时；客户号字符集存疑先跑 `crypto/task/_diag_cl_ord_id_charset.py` |
| `crypto/task/_smoke_dir_lock.py` | 人工方向锁定语义（2026-09-01 事故修复）—— 8 个场景覆盖 dual/single/页面锁定 | 改 `trend_range_trader.py` 方向锁定分支 |
| `crypto/task/_smoke_dual_position.py` | 双仓位架构离线（**常驻**）—— 趋势/区间/减仓/对账/配置校验 | 改 `position_order_manager.py` 或 `trend_range_trader.py` 挂单/账本链路 |
| `crypto/task/_smoke_fix_regression.py` | 审计修复回归（**常驻**）—— fx01~fx16 撤单竞态/仓位上限/人工强平/粉尘清零 | 改挂单管理器或交易器任何方法 |
| `crypto/task/_smoke_leverage_guard.py` | 杠杆交易所回核（2026-09-01）—— 8 个场景覆盖缓存/回核/重设/cross-isolated 分键 | 改 `_set_leverage_if_needed` |
| `crypto/task/_smoke_manual_close_detect.py` | 手动平仓检测 `_detect_external_close`（临时） | 改 `trend_range_trader._detect_external_close` |
| `crypto/task/_smoke_manual_override_regression.py` | 人工干预三问题回归 —— 幽灵挂单/强制方向持续期/加仓吸收/对账缩减 | 改挂单管理器 `reconcile` 或 `_reconcile_slot` |
| `crypto/task/_smoke_okx_ratelimit.py` | 只读接口限频/退避（问题#8）—— 41 场景覆盖开关直通、令牌桶、50011 退避、网络抖动重试、本地 bug 不重试、等不到令牌抛 RateLimited、环境变量覆盖，并静态核对 10 个文件（1 个自身除外）“只读已接线 / 下单未被包” | 改 `task/utils/okx_ratelimit.py`，或改 `trade_executor` / `api_routes` / `market_scanner` / `star_market` / `batch_trend_updater` / `instrument_spec` / `alert_monitor` / `app.py` / `plan_routes` 里 OKX 接口调用方式（尤其新增只读接口、或给下单加包层时）；**新增调用 OKX 的模块必须加进 `_TARGETS`** |
| `crypto/task/_smoke_order_step.py` | 合约下单步长三档（2026-09-01 实盘故障）—— XRP/NEAR/POL 三档规格 | 改 `InstrumentSpecCache.steps()`/`quantize()` 或挂单 `_q()` |
| `crypto/task/_smoke_order_ttl.py` | 挂单 TTL 到期撤销（临时）—— **TTL 只约束趋势跟踪仓；区间波动仓是随 BOLL 边界改单追价的持久挂单，永不受 TTL 撤单重挂**（2026-09-28 用户明确纠正，原实现错误地对两个篮子都套 TTL）；覆盖趋势仓超龄撤单/未超龄保留/ttl=0 关闭/部分成交先入账再撤/撤单未确认不误复位、区间仓超龄不撤 | 改 `poll_fills` 的 TTL 分支（必须只对 `BUCKET_TREND` 触发 `_expire_ttl`）或 `_expire_ttl`。**V2 账本改完后裸桩必须同步升级**：`_save` 桩返回 True（【2026-09-28 移除 G3 后】落库失败已不再拒发请求，桩返回 True 只为保持“落库成功”常态路径、让用例聚焦撤单逻辑本身）、补 `account/environment/_entry_blocked/_round_place_failures` 等属性、且 `FakeExec` 必须在 cancel 后把 `probe_order` 改口回 `canceled`（V2 不信撤单受理、只信终态），并把 `pom.journal_fill` 桩成空函数防止往业务库写成交流水 |
| `crypto/task/_smoke_trader_lease.py` | 跨进程调度单实例租约（防「两个写入者」同写一份账本导致 `_revision` 版本冲突，2026-09-28 实盘事故；【移除 G3 后】冲突不再全局停摆，但双写仍会互相覆盖账本，故租约保留）—— 7 场景验纯决策 `_lease_decide`：空闲抢占/本进程续约保留 acquired_ts/其他存活持有者→`LeaseRefusedError`（携对方 holder）/超 TTL 陈旧租约可接管/过期边界(renewed+ttl==now)判过期、`_lease_key` ≤64 且按 (account,environment) 分区、`make_lease_holder_id` 唯一。原子写由 `patch_json_config` 保证（其正确性见 `_smoke_config_store`），故本冒烟不连库 | 改 `trader_state_repo` 的 `acquire/renew/release_trader_lease`、`_lease_decide`/`_lease_key`/`LeaseRefusedError`/`LEASE_DEFAULT_TTL`，或改 `trend_range_trader._acquire/_renew/_release_trader_lease`、`run_real_trading_batch` 轮首续约、`stop()`/`scheduler.stop_trading_scheduler` 释放接线时 |
| `crypto/task/_smoke_rg2.py` | 反向持仓风控强平（临时）—— 成功/失败分支、通知计数 | 改反向持仓风控分支 |
| `crypto/task/_smoke_risk_alerts.py` | 双仓位·风控告警 P0 全项 + P1 盈亏双向/账户余额保证金/调度轮存活/插针禁开仓/发信失败兜底 + P2 账本背离/兜底委托不在位（纯内存桩不连 DB/邮件/API；【2026-09-28 移除 G3】已删除 P2-c 持久化降级/P2-d 链路健康聚合两组用例）—— 下单失败暂存/排空/`set_context` 复位、P0-1 连续拒单达阈值升级（可配轮数）+ 干净轮复位、P0-3 need_verify 孤儿单首轮即发且不推进被拒计数、`enabled=False` 不发信仍排空、P0-2 止盈止损平仓失败连续 N 轮升级、P0-4 `_position_liq_info` 距离/方向计算与缺数据回退、`_classify_liq_level` 分级、`_transition` 强平距离复用级别状态机、P1 `_classify_profit_level` 正阈值分级/盈亏互斥/盈利侧独立状态机键位、P1 `_classify_margin_level`/`_classify_avail_level` 账户余额保证金分级与独立键位、P1 `_liveness_thresholds`/`_classify_liveness_level` 调度轮存活分级（线程死亡/停滞/停止/未出轮）与 `TaskScheduler.get_loop_liveness` 裸桩只读、P1 BOLL 出轨分级器 `_classify_boll_level`（越界幅度/band、逆势破轨升 critical、轨内不命中；用户选跳过接线仅保留分级器备用）、P1 插针 spike `_classify_spike` 单轮环比判定（无基准/间隔过小/未达阈值不判，宁漏不误伤）+ `_note_spike_guard` 开关×发信×暂停×延长 + `_spike_pause_remaining` 到期自动清除、P1 发信失败兜底死信 `_dead_letter_store` 落盘/指纹去重/超限裁剪 + `flush_dead_letters` SMTP仍不通积压保留·恢复后按时间正序补发·节流skip·enabled关闭不落盘 + `email_channel_health` 积压可见（假 email_tool + 临时目录，不连 SMTP）、P2-a `reconcile` 缩减/吸收记 `_round_divergences` + `_handle_ledger_divergence` 按 shrink/absorb 阈值分级告警（缩减critical·吸收warning·从零吸收不发避重启误报·子开关/总开关）、P2-b `_note_algo_backup`+`algo_in_place` 兜底委托该挂在位却缺失连续 N 轮升级 critical·恢复在位复位·无静态触发价不判 | 改 `position_order_manager._note_place_failure`/`drain_place_failures`/`drain_divergences`/`reconcile` 背离记账/`set_context`/`algo_in_place`、`trend_range_trader._handle_place_failures`/`_handle_ledger_divergence`/`_note_algo_backup`/`_sync_trend_algo`/`run_real_trading_batch` P2 旁路钩子/`_alert_sl_tp_fail`/`_risk_alerts_cfg`/`_safe_risk_alert`/`_classify_spike`/`_note_spike_guard`/`_spike_pause_remaining`/`_spike_guard_cfg`（step 5a 调用/step 5d 复用 allow_entry 禁开仓闸门）、`database.session_scope`/`_db_note_*`/`_is_conn_error`/`db_health`、`message_notifier.send_risk_alert`/`send_liq_distance_alert`/`send_profit_alert`/`_dispatch_email` 死信钩子/`_dead_letter_store`/`flush_dead_letters`/`email_channel_health`/`_dl_cfg`/`_dead_letter_path`/`_dl_read`/`_dl_write`，或 `alert_monitor` 的 `_position_liq_info`/`_classify_liq_level`/`_classify_profit_level`/`_classify_margin_level`/`_classify_avail_level`/`_liveness_thresholds`/`_classify_liveness_level`/`_classify_boll_level`/`_transition`/liq·profit·acct·liveness 检测分支/run_alert_check 死信补发钩子，或 `scheduler.get_loop_liveness` |
| `crypto/task/_smoke_smart_reduce.py` | 智能减仓 —— `classify_reason` 优先级、0.5U 折算、`_close_bucket_smart` 分支 | 改 `_close_bucket_smart` 或 `classify_reason` |
| `crypto/task/_smoke_sync_scope.py` | 轮初对账范围划分（幽灵合约冻结全账户事故的回归，详见 `crypto/幽灵合约冻结开仓事故修复说明.md`）—— 26 场景：配置内/交易所目录内无条件同步；账本记着持仓·在途委托·兜底委托则**绝不跳过**（哪怕是幽灵合约）；FILLED/EXPIRED **终态残留不算敞口**（已移除币种的常态），但带 unknown/cancel_requested 或非终态状态就算；粉尘级残渣不算敞口；仅带风险标记 + 交易所明确无此合约才跳过并从内存账本丢弃；账本干净的空条目跳过且**不联网问存在性**；存在性查询失败/抛异常/桩缺 `spec_cache` 一律按未知保守同步；`pause_until` 未消化不放行、已过期不拦；跳过日志按集合签名去重；`_is_not_exist_response` 只认 51001/"doesn't exist"，限频·空响应·非 dict 全不算否定 | 改 `trend_range_trader._partition_sync_scope`/`_ledger_open_interest`/`_ledger_risk_flags`/`_slot_unsettled`/`_spec_instrument_exists`/`_SLOT_SETTLED`、`run_real_trading_batch` 轮初预对账循环，或 `instrument_spec._is_not_exist_response`/`instrument_exists` 的判定口径时；**改终态口径必须同步改 `_cleanup_ghost_ledger.has_open_interest`，两边必须同标准** |
| `crypto/task/_smoke_trading_runtime.py` | 实盘调度器启停竞态（#6）+ 重启自愈记账（#5）—— 18 场景覆盖收尾窗口拒启、finishing 三态、开关×期望状态组合、不传 `boot_info` 走老语义 | 改 `scheduler.py` 的 start/stop/get_trading_status/schedule_auto_resume，或改 `trading_runtime_repo.py`（桩靠 `sys.modules` 顶掉 `trend_range_trader`，一旦改成模块级 import 桩会失效；通知函数已从 staticmethod 改实例方法，桩签名要带 `self` 与 `why=''`） |
| `crypto/task/_smoke_round_snapshot.py` | 执行器轮初全账户快照缓存（Option B 性能优化「改动1」，把 4 币种整轮压进 20s）—— 10 场景：快照激活后按 `instId` 读持仓/挂单/账户模式全部本地命中·零跨境 API；无该币事实返回空视图仍零 API；`invalidate_snapshot(inst)` 只让该币回落 live 其它币续命中；`inst_id=None`（全账户）读永远走 live；`_write` 写操作按 `instId`/批量 list 参数自动失效相关币、取不到 `instId` 时保守停用整轮快照；`end_round_snapshot`/`begin(None)` 后读路径全部回落 live；从未 begin 时裸实例照常 live 不报错（裸 `__new__` 跳过 `__init__`，`_rl`/`_get_positions_raw`/`trade_api`/`account_api` 全替身计数，不联网） | 改 `trade_executor.py` 的 `begin_round_snapshot`/`invalidate_snapshot`/`end_round_snapshot`/`_snapshot_*`/`_write` 失效钩子、`try_get_positions_by_mode`/`try_get_open_orders`/`try_get_position_mode` 的快照命中分支，或改 `trend_range_trader.run_real_trading_batch` 的快照注入/`finally` 清理接线时 |
| `crypto/task/_smoke_watch_mode.py` | 观察模式（`trade_enabled=False`）写操作闸门 —— 禁联网桩下跑真实函数体：`watch_mode`/`watch_mode_active` 上下文与 `trade_executor.watch_mode_check` 前置拦截；只读/对账/日志照做，不下单不撤单不发信；与人工干预、风险闸门、持久化的交叉场景 | 改 `crypto/task/utils/watch_mode.py`、`trade_executor` 的写操作闸门接线、或 `trend_range_trader._is_watch_mode`（含新增「自动降级观察」这类旁路判定）时 |
| `crypto/_smoke_task_switch.py` | 定时任务统一开关（kv task_switches + config.json 急停）—— 32 场景：文件层语义/fail-open/mtime 缓存/kv×文件合并（文件只能单向关）/分析纪律邮件开关/巡检与每日归零注册闸/模块总闸 `discipline_repo.module_enabled`（业务 kv enabled × 任务开关，任一层关→闸门与网页提醒静默） | 改 `scheduler.py` 的 task_switch/load_switch_state/set_job_switch/register_job 守卫、`discipline_repo.py` 的 module_enabled、或 `memory_daily_reset.py`/`analysis_discipline.py` 的注册分支 |

## 🛡️ 写库 + 还原（11 个）

所有脚本都遵循「快照→清场→finally 无条件恢复」的隔离策略。前置条件：`CRYPTO_DB_URL` 环境变量。

| 脚本 | 覆盖模块 | 何时重跑 |
|---|---|---|
| `crypto/_smoke_analysis_record.py` | 实盘分析记录（批次10）—— 建表 + CRUD + 复盘回填 + 统计 | 改 `analysis_record_repo.py` 或 `TaskAnalysisRecord` 模型 |
| `crypto/_smoke_balance.py` | 账户余额历史（批次4）—— `balance_repo` + `api_routes` 快照/回溯辅助函数 | 改 `balance_repo.py` 或 `BalanceHistory` 模型 |
| `crypto/_smoke_checkin_reminder.py` | 打卡倒计时提醒 —— `_evaluate` 纯函数判定（到点/未到期/去重/防轰炸/回溯窗）+ kv 配置状态回环 + 巡检入口打桩 | 改 `task/monitor/checkin_reminder.py` 或其调度接线 |
| `crypto/_smoke_calorie.py` | 热量模块 HTTP API —— `calorie_bp` 全部路由 | 改 `calorie_routes.py` 或 `CalorieFood/Record/MealItem/Config` 模型 |
| `crypto/_smoke_food_spot.py` | 美食打卡点（批次14，热量页内嵌）—— 创建/多图上传/格式白名单拒收/照片回显与防穿越/编辑回填/删单照/删点级联清盘/GET 零写入/增量建表接线；照片落盘跟随 `CRYPTO_PLAN_DATA_DIR` 隔离到临时目录 | 改 `calorie_routes.py` 的 food-spot 路由、`calorie_repo.py` 的 `FoodSpot*` 函数、`FoodSpot/FoodSpotPhoto` 模型、calorie 网页打卡区段或 `db_schema.sql` 批次14 段 |
| `crypto/_smoke_config_store.py` | 策略配置/币种自选 kv_store（批次7a）—— round-trip/缺省/损坏/覆盖/删除 + 切库兜底 | 改 `config_store_repo.py` 或 `real_strategy_adapter` 切库逻辑 |
| `crypto/_smoke_journal.py` | 随笔复盘 HTTP API —— `journal_bp` 全部路由 | 改 `journal_routes.py` 或 `JournalTag/Note/NoteTag` 模型 |
| `crypto/_smoke_instinct.py` | 盘感语料+检索+Wiki+预测管线+/instinct 页面+Wiki 管理台（批次12 P1~P3、D6/D7/D10）—— 106 场景：instinct_* 四表存在性；防泄漏结构断言（ctx 列与 outcome 列不相交、`ctx_dict()` 物理给不出 outcome、样本键无野字段）；抽取口径（judgment 归一、labeled↔outcome_far、hit/outcome 与 `classify_move` 重算逐条一致、`ctx_dir_flipped` 语义、两次抽取确定性）；`--verify` 基线三方核对（源表全量/P0 窗口/语料表 vs P0 冻结 20.0/33.3/26.7/53.3）；沙箱幂等（清表全量入库、重跑 0 新增 0 更新、篡改值被 update 分支还原、源新增行触发 insert、finally 快照恢复含 id）；P2 检索打分原语（dir/atr/period/decay/同币种×3 精确值断言、topK 确定性、LOO 排除自身、sanitize 剥离 outcome 与内部键、`assert_no_future_leak` 负例）；P2 Wiki 状态机（rule_key created→merged 不重复建卡、candidate→active→retired、retired 拒绝激活/再蒸馏复活、条件 DSL fail-closed、过期自动降级、规则表快照-还原）；P2 蒸馏（两次候选一致、stat:* 候选逐条复算过门槛、种子路与冻结基线方向一致）；P3 管线（prompt 四段结构、输入含 outcome 键必须抛 ValueError、strict=False 答案值仍不进文本、网关 schema 七类拒绝+围栏容忍+`C1/R2/数字串`引用归一化正例（DeepSeek 真机行为回归）、越界引用拒绝、mock 确定性、回灌 15 样本全管线零失败、防未来规则 anchor 过滤、无 mock 残留行）；D6/D7 HTTP 组（真 app test_client 且 `CRYPTO_NO_BACKGROUND=1`：页面 200、currencies/rules/stats/now 缺参/settle 结构、无 key 时 predict 明确 503 指引且调度任务不注册、不存在资源 404、配口令时未带凭证 401）；D10 Wiki 管理台组（/instinct/wiki 页 200 + iwk- CSS 隔离 + 四区块标记、search_test 三模式：manual 打分明细 breakdown 齐全且 final==score 逐条与生产总分一致、corpus LOO 不含自身/404、live 缺参 400、trend 事件流字段、distill 干跑前后规则数不变、expire、llm_configured 布尔） | 改 `crypto/instinct/` 包任意文件（corpus_builder/retriever/wiki_repo/wiki_distiller/prompts/llm_gateway/predict_service）、`crypto/instinct_routes.py`、`templates/instinct.html`、`templates/instinct_wiki.html`、`static/css/instinct_wiki.css`、`task/scheduler.py` 的 register_instinct_jobs 挂载、`Instinct*` 批次12模型、`analysis_record_repo` 计分口径常量（`classify_move/_JUDGMENT_EXPECT/REVIEW_*/_review_price_at`），或上游三源表（task_analysis_records/plan_slots/journal_notes）结构变化时 |
| `crypto/_smoke_kv_cache.py` | 缓存类 kv_store（批次8）—— `instrument_spec_cache` / `market_scan_cache` 切库 + 双写 | 改 `config_store_repo.py` 缓存键、`instrument_spec.py`、`market_scanner.py` |
| `crypto/_smoke_market_csv.py` | 行情 CSV（批次7b）—— `crypto_coins` / `star_market` 整表/排序/中文键/文件兜底 | 改 `market_data_repo.py` 或 `CryptoCoin/StarMarketRow` 模型 |
| `crypto/_smoke_plan.py` | 任务计划 HTTP API —— `plan_bp` 全部路由 | 改 `plan_routes.py` 或 `PlanPlan/Card/Slot` 模型 |
| `crypto/_smoke_trade_journal.py` | 结构化成交流水（批次6）—— `record_fill` / `read_journal` / `classify_reason` | 改 `trade_journal_repo.py` 或 `TradeJournal` 模型 |
| `crypto/_smoke_trader_state.py` | 交易运行时状态（批次5）—— `trader_state_repo` + `DualPositionOrderManager` / `TakeProfitEngine` 持久化链路 | 改 `trader_state_repo.py` 或 8 张状态表模型 |

## 🛡️ 隔离空库：支出管理（1 个）

| 脚本 | 覆盖模块 | 何时重跑 |
|---|---|---|
| `crypto/_smoke_expense.py` | 手动支出/回款/划转、金额与周期、幂等与版本、退款上限、分类标签与审计、月/周/分类预算、固定账单与预留、每日核对、只读 GET、预览、CSV、安全闸门、网页契约、1 万笔分页聚合、模型/SQL/增量建表接线 | 改 `expense_*.py`、`Expense*` 模型、支出网页及 JS/CSS、`db_schema.sql`、增量建表清单或共享导航接线 |

此脚本**不适用上节历史脚本的业务库快照还原方式**。启动前经过 `test_isolation.require_isolated_test_db()`：只接受 `CRYPTO_TEST_DB_URL` 或守卫支持的 `CRYPTO_TEST_DB_SCHEMA`，库名必须含测试标记且不能与业务库同名；未配置立即退出，不回退业务库。只补建 `expense_*` 表，已有任何支出数据即拒绝运行；普通测试结束清理本次测试行，不删表。中断留下数据时改用新的空测试库，不能为重跑清理用户账本。

## 🛡️ 隔离空库：日记本（1 个）

| 脚本 | 覆盖模块 | 何时重跑 |
|---|---|---|
| `crypto/_smoke_diary.py` | 日记本（批次13）12 组场景：密码哈希三算法回退与往返校验、解锁 cookie HMAC 签名（过期/篡改/改密失效）、人生天数与目标倒计时数学、页面渲染+导航入口接线、按天记录 CRUD 与校验、日历聚合与分页、隐私双模式（服务端脱敏/解锁流程/换密旧凭据失效/清除退回公开）、解锁失败限流、目标生命周期、生日设置校验、导出导入回环（幂等/脏数据/锁定拒导）、模型/DDL/增量建表接线自检 | 改 `diary_*.py`、`Diary*` 模型、`templates/diary.html`、`static/js/diary.js`、style.css 日记区段、`db_schema.sql` diary 段、增量建表清单或共享导航接线 |

与 `_smoke_expense.py` 同一套隔离口径：`require_isolated_test_db()` + 只补建 `diary_*` 三表 + 表非空即拒绝运行；SQLite 全绿不能外推 MySQL 的 DDL/锁结论。

### 自动化验收

在项目根目录使用独立空 SQLite 文件（Windows PowerShell）：

```powershell
$env:CRYPTO_TEST_DB_URL='sqlite:///D:/python/cryptoTrade/data/expense_0922_smoke.db'
$env:CRYPTO_PLAN_DATA_DIR='D:/python/cryptoTrade/data/expense_test_data'
python -B -m crypto._smoke_expense
node --check crypto/static/js/expense.js
git diff --check
```

- 2026-09-22 实测：20 组通过，用时 6.707 秒，无跳过；Node 前端单元契约、JS 语法及 diff 空白检查通过。
- 1 万笔隔离数据：20 次“50 条分页列表 + 总览”合并耗时采样，P95 为 225.4ms，最后一次共 23 条 SQL。结果仅属于当次本地 SQLite 环境，不是生产性能保证。
- 覆盖精确整数分、月末/闰年/跨年、过渡预算保留、历史预算基准不重写、未来已准备周期账单调整、标签变更审计、并发退款限额、事务回滚及“已保存但统计待刷新”。监听读取接口确认无 DML/DDL；验证无预算/零预算、停用与恢复、核对失效、CSV 公式防护及访问闸门。
- 前端契约使用 Node VM 执行真实 JS 辅助逻辑，验证金额、转义、分类候选、同步阻止表单提交、请求头与冲突透传；全部 Jinja 模板可编译，支出页可渲染，静态元素 ID 对齐。这些不能替代真实浏览器布局与交互验收。
- SQL 文件与模型列名核对、MySQL 方言 DDL 编译、SQLite 幂等建表及增量补建调用接线已验；**独立 MySQL 的实际 DDL、索引、锁与并发尚未验证**，当前未配置独立 MySQL 测试目标。
- 脚本不导入 `crypto.app`，不启动交易调度器，不调用交易、付款、邮件或外部分析服务。本轮未运行生产迁移、未部署。

### 隔离网页入口与待验项

用另一个空测试库启动最小 Flask 页面，勿与自动化测试共用账本：

```powershell
$env:CRYPTO_TEST_DB_URL='sqlite:///D:/python/cryptoTrade/data/expense_ui_0922_smoke.db'
$env:CRYPTO_PLAN_DATA_DIR='D:/python/cryptoTrade/data/expense_ui_test_data'
python -B -m crypto._smoke_expense --serve
```

打开 `http://127.0.0.1:5091/expense`。仅监听回环地址，沿用 Web 访问闸门；若本地已配置口令，使用现有登录流程，不关闭闸门。共享导航只注入默认配置和只读桩，不开放导航写入，也不加载其他业务页面。服务重启仍要求空账本；已有验收数据时使用新的空测试文件，不清掉原数据。

当前自动浏览器视图未挂载（`visible=false / attached=false`），表单输入超时，尚未提交网页测试账目；没有网页交互通过或布局截图的证据。需打开并聚焦 IDE 的 Browser 视图后补验：

1. 首次设置预算 1000；记录午餐 12.34，编辑为 20，关联回款 5，核对净支出 15；作废、恢复后核对所有展示一致。
2. 新建一次性账单 50，手动支付 20；应显示待付预留 30、周期净支出 35。确认结清、跳过及重开时核对预留与周预算影响。
3. 验四页签、分类标签增改停用、筛选分页、详情审计、每日核对与失效、导出、金额隐私、近 30 天图与刷新、特殊文本按普通文字显示。
4. 在测试环境验证重复点击、响应丢失后原请求重试、版本冲突、未保存离页提示、跨午夜与预算草稿保留；不操作交易导航或真实账本。

## 🛡️ 隔离空库：每日评级总控台（1 个）

| 脚本 | 覆盖模块 | 何时重跑 |
|---|---|---|
| `crypto/_smoke_checkin.py` | 每日评级总控台（批次15，原"每日多维打卡"）12 组场景：/checkin 页与三模板编译、JS↔页面元素 id 契约（含日记按钮 ck-diary-tip/jump/entry、评级框 ck-rating）、首页状态条接线、日记双向关联前端契约（diary.js 识别 checkin: 回指、/checkin?date= 深链）；**红线**=GET api/today（缺缓存/过期缓存）与历史补录全程零 OKX 调用、GET 零写入；热量/学习采集口径（跨日干扰、trade 卡默认不计、done 任务去重）；refresh-trade 实拉+净入金剔除+按日缓存 TTL 与缓存命中；提交服务端重算、覆盖保留 created_at、评级归一、评价截断/超长拒收、未来日期拒收；日记状态读取（has_entry/entry_count/entry_id 优先回指条目）与提交定格 diary_entry_id；历史补录纯本地差分且不污染按日缓存；history 区间校验；repo 单测（快照新鲜度/裁剪/upsert）；改名（导航种子项=每日评级总控台）+ 23:30 收尾提醒任务接线（checkin_diary_reminder job_id/纯函数判定/SWITCHABLE_JOB_LABELS/注册）+ diary_entry_id 三处一致（模型列/增量加列清单/DDL）自检 | 改 `checkin_routes.py`、`checkin_repo.py`、`DailyCheckin/TradingDailySnapshot` 模型、`plan_repo.load_daily_learn_stats`、`diary_repo/DiaryEntry` 读法、`task/monitor/checkin_diary_reminder.py`、`templates/checkin.html`、`static/js/checkin.js`、`static/js/diary.js`、index.html 状态条区段、`db_schema.sql` 批次15 段、增量建表/加列清单或共享导航接线 |

与 `_smoke_diary.py` 同一套隔离口径：`require_isolated_test_db()` + 只补建打卡与依赖表（calorie/plan/balance_history/diary_entries），表非空即拒绝运行。脚本用 `sys.modules` 桩顶掉 `api_config` 与 `crypto.api_routes`、整体替换 `_okx_client_and_call`/`_resolve_account`，**全程不触真 OKX、不导入 crypto.app**。注意：PlanPlan/PlanCard/PlanSlot 之间是裸 FK 无 relationship()，同 flush 插入顺序不保证，种子数据必须分段 flush（2026-09-26 冒烟首跑就是这么炸的）。SQLite 全绿不能外推 MySQL 的 DDL/锁结论。改名后导航是 kv_store 整份快照，须另跑 `data/_nav_rename_checkin.py --apply` 迁移存量快照（种子静态断言不能替代快照迁移）。

## ⚠️ 写库 + 租约（3 个）

| 脚本 | 覆盖模块 | 何时重跑 | 关键约束 |
|---|---|---|---|
| `crypto/_smoke_discipline.py` | 分析纪律（批次11）—— DB 增量迁移 + 配置读写 + 闸门判定 + 看板只读 | 改 `discipline_repo.py` 或 `task_analysis_records` 表结构 | 唯一写 kv_store 配置，测完还原 |
| `crypto/_smoke_discipline_http.py` | 分析纪律 HTTP 全部蓝图路由 + [C2] 任务开关→闸门/网页提醒静默联动（页面开关/全局开关/文件急停三层都要让 status・gate 返回 disabled） | 改 `discipline_routes.py` 或闸门/台账/豁免逻辑，或改 task_switches↔module_enabled 联动 | 必须 `CRYPTO_NO_BACKGROUND=1`（后台线程会真发信）；取巡检维护租约；冒烟期间强制打开 analysis_discipline 任务开关、测完还原用户基准（否则用户已在 /task 页关巡检时，“闸门必须拦 403”类用例会联动性误挂） |
| `crypto/_smoke_discipline_mail.py` | 分析纪律邮件链路 —— 缺口信/断档汇总/日报/策略行情表 | 改 `task/monitor/analysis_discipline.py` 或邮件渲染 | 接管发信出口落盘到 `data/mail_preview/`，**脚本内无真发分支**；取巡检租约 |

## 🌐 连真实 API（5 个）

| 脚本 | 覆盖模块 | 何时重跑 | 备注 |
|---|---|---|---|
| `crypto/_smoke_pos_history.py` | 主账号持仓盈亏查询（OKX 官方接口） | 改 `ApiUtils/account_query_utils.py` 或分页逻辑 | **查询工具，非回归测试**；调真实 OKX API |
| `crypto/task/_diag_cl_ord_id_charset.py` | OKX 客户号字符集与长度口径，**两个接口各一组**：普通委托 `clOrdId`（`get_order`）+ 策略委托 `algoClOrdId`（`get_algo_order_details`），另含 `get_instruments` 链路自检 | 改 `gen_cl_ord_id` 前、或怀疑交易所改了客户号规则时 | **定性探针，不下单不撤单**；实测结论=两处同为「纯字母数字 ≤32」。结尾口径由 `_verdict()` 按实测结果现场生成，不许硬编码 |
| `crypto/task/_diag_open_orders_clordid.py` | 交易所侧挂单与 clOrdId 对账（纯只读：`get_order_list` / `get_orders_history`） | 怀疑幽灵挂单、漏撤、或想确认某笔挂单是"哪一轮哪个篮子"下的 | **不下单不撤单**；`python .../xxx.py stageone hist`。51000 事故复盘中确认「打标真的写进了交易所」就是用它 |
| `crypto/task/_diag_process_instances.py` | 本机 app.py 实例数 / Web 端口归属 / 启动台账 / 自愈熔断余额与**解封时刻**；`ab` 子命令=拿**生产真实心跳**跑归因 A/B（新口径 vs 把 `_pid_alive` 短路掉的旧口径） | 日志出现 `abnormal_death` 但没崩、自愈被熔断、怀疑有人起了第二个实例、想知道"还要等多久才会自动接回"、改归因逻辑后 | 纯只读，不 kill 进程、不改状态文件（`ab` 只把心跳拷进临时目录）；解封时刻按 `plan_resume` 的 `count >= RESUME_MAX（含本次）` 口径算，不是"最早那条掉出窗口"；**改归因必跑 `ab`**——该修复的 22 个冒烟全绿而修复本身是废的，就是它抓出来的 |
| `crypto/task/_diag_margin_state.py` | 账户保证金实况（纯只读 OKX：余额/可用/挂单冻结/负债/账户等级 + 逐笔持仓保证金占用 + 按配置杠杆的各币种**最大可开**） | 出现 `51008 可用 USDT 不足`、或想知道"还能不能再开一单、钱被什么占着" | **必须在能出到交易所的终端里跑**：本机靠代理接管海外流量，而并非每个进程都被接管——开头会先做 DNS/TCP 体检，若解析到 `169.254.x.x` 这类 fake-IP 就直接退出码 2 并说明"你换了个没被接管的终端"，**别把这当成"账户没余额"**。`python .../_diag_margin_state.py stageone` |

## 🧰 运维清理脚本（不是回归，会写库）

| 脚本 | 作用 | 关键约束 |
|---|---|---|
| `crypto/task/_cleanup_ghost_ledger.py` | 清理 V2 持仓账本（`kv_store` 的 `pos_v2:*` 行）里“已不在配置且无事实可核对”的幽灵/空账本条目（误配 instId、已移除币种留下的残留会让轮初对账白跑并冻结开仓额度）；**默认只读体检**，逐条打 `PROTECTED / GHOST / IDLE` 三分类 | `python -B crypto/task/_cleanup_ghost_ledger.py --account quantlimit` 先看体检报告；**只有加 `--apply --scheduler-stopped` 两个旗标才真删**（只给 `--apply` 不给 `--scheduler-stopped` 直接退出码 2）——账本靠 `_revision` 乐观锁，实盘进程在跑时改库会触发版本冲突 latch 并**永久停单**；有持仓/在途委托/风险标记的条目一律 `PROTECTED` 不删（在途判定与调度器 `_slot_unsettled` 同口径：FILLED/EXPIRED 终态残留不算敞口）；`--include-idle` 才动“合约存在但账本已空”的条目；`--detail` 会逐条打印持仓量/快照/风险标记与被保留原因（区分"真敞口要平仓"与"残留脏数据要清账本"）。与根目录 `_cleanup_ledger.py`（清分析缺档台账）不是同一个东西 |

## 🔒 脚本的四类假失败（一次性修掉，别再犯）

2026-09-09 排查，四个 🔒 脚本因历史迁移而长期红/静默失效，均为**脚本自身**问题，
生产代码零改动：

- `crypto/_smoke_longtermism.py`：用 `spec_from_file_location` 顶层加载 `plan_routes`，
  而它内部是 `from .database import ...` 包相对导入 → 必 ImportError。改 `import crypto.plan_routes`。
- `crypto/task/_smoke_order_ttl.py`：`M.__new__(M)` 手工桩漏了 `spec_cache`，被测函数
  访 `self.spec_cache` 直接 AttributeError。桩的接口面要跟着被测代码的依赖清单走。
- `crypto/_smoke_sysmon.py`：读/写真实告警冷却状态文件 —— 30 分钟内真发过告警则「应发
  邮件」必假失败，更严重的是脚本 stub 会写脏真文件、把消费端真告警压掉 30 分钟。改指
  临时路径；并在顶部 reconfigure stdout 为 UTF-8（告警主题带 🚨，GBK 控制台直接崩）。
- `crypto/task/_smoke_smart_reduce.py`：拿本地 `config_trend_range.json` 的内容做断言，
  但策略配置已迁 MySQL kv_store，快照文件早不是真相 → 改验代码层缺省行为。另一坑：
  `FakeSpecCache` 只实现了 `get_spec`/`usd_to_contracts`，而生产代码走 `steps()/quantize()`，
  缺方法被 `except` 吞成兜底 (0.1, 0.1) —— 只要被测函数用 try 包着，桩必须实现**真实接口**
 而不是“够用”，否则测的是兜底路径而不是目标分支。

> 通则：这些脚本都用了「首次失败即 `sys.exit(1)`」的 `ok()`，一旦前面卡住，后面的
> 用例**从未跑到**——修好第一个坏点后必须复跑全量，才会暴露被掩盖的第二个。

### 同一类在 2026-09-27 又复发一次（原因换成 V2 账本 + 下单打标）

两个常驻/临时脚本长期红，都是**桩没跟上生产代码**，生产代码零改动：

- `_smoke_order_ttl.py`：三处不同步——`_save` 桩返回 None（V2 把"下单意图先落库"当发送
  前置条件，挂单必被拒）、缺 `account/environment/_entry_blocked/_round_place_failures`
  等新属性、`FakeExec` 撤单后仍回 `live`（V2 不信撤单受理、只信终态，槽位永停 PENDING）。
  另：该桩会把成交流水写进**业务库**，已把 `pom.journal_fill` 桩成空函数。
- `_smoke_dual_position.py`：`MockExecutor.execute_trade/execute_reduce_only_order` 不接
  `cl_ord_id`（问题#4 上线后未同步）→ 场景 1 就 `KeyError: None` 崩掉整个脚本，后面的
  场景全部未跑到。已补参数与 `probe_order_by_cl_id`；修完全量复跑后暴露出**两项真红**：
  `真实持仓缩减后区间仓平仓单改量为 0.2`（实测 0.3）与 `clear_bucket 撤单并清空账本`
  （`n=0` 但持仓/均价已归零、挂单已 `canceled`，像是前一步 `reconcile` 已把单撤掉、
  脚本断言的 `n>=1` 过期）。**这两项尚未定性**：要改必须先确认 V2 的
  `reconcile`/`clear_bucket` 职责边界，不得为了凑绿灯直接放宽断言。

### 还有一类更危险：假完备（绿灯不等于验全了）

`_smoke_okx_ratelimit.py` 的静态核对报 41/41 全绿，事后发现 `alert_monitor.py` 与
`crypto/app.py` 共四处只读接口**完全没接线**：因为 `_TARGETS` 只列了 7 个文件，而且
`_CLIENT` 只认变量名（`account_api.get_positions(`），认不出链式直调
（`MarketData.MarketAPI(flag=flag).get_ticker(`、`get_account_api(a).get_positions(`）。
2026-09-10 补了清单与正则，并用一个临时脚本做**反向验证**（把六种裸调用形状喂给
正则，确认逐个能咬、四种已包形式不误报）才收工——静态扫到 0 命中与根本没扫到，
在输出上长得一模一样。

**绿灯不等于验全了**的另一例（2026-09-11 实盘 51000 事故）：`_smoke_cl_ord_id.py`
第 1 组断言写的是 `re.fullmatch(r'[A-Za-z0-9_]+')` + 「长度 ≤64」，实现照此生成
`ct_<token>_<hash>`，两边**错得一模一样**，所以 29/29 全绿；而 OKX 的 clOrdId 实测
只收「纯字母数字 ≤32 位」，结果打标一上线，实盘每一笔委托都被拒
`51000: Parameter clOrdId error`，双仓位一笔都挂不出去。教训：**凡"合法性"断言，
口径必须来自交易所实测**，不能来自文档印象或别的交易所的习惯；探针脚本
`crypto/task/_diag_cl_ord_id_charset.py`（只查单不下单）就是这条口径的出处，
改 `gen_cl_ord_id` 前先跑它，结论回写到函数注释。

这次事故还有个**二阶杀伤**值得单独记住：非法客户号不只让下单被拒，还顺带废掉了
「结果未知」的定性能力——`probe_order_by_cl_id` 反查用的也是同一个 clOrdId，
`GET /api/v5/trade/order` 同样回 51000，被判定逻辑当成"未识别错误码"而归成
`unknown`，于是日志里成片出现「反查同样失败，请人工核实」。也就是说**打标格式
出错时，安全网和被保护的动作一起失效**，看起来像"网络极差"，其实是同一个根因。
修复后同一时刻的 SSL 断连只留下 `absent`（可安全重发）而不再有 `unknown`，
`need_verify` 计数归零，这一点才算被真正验证。

**同一个坑当天又踩了一次（结论写死在输出里）**：上面那个探针只调了 `get_order`
（普通委托的 `clOrdId`），结尾却硬编码打印"OKX clOrdId/algoClOrdId 口径已确认"，
索引文档也跟着抄——**algoClOrdId（策略委托：追逐限价/TWAP/移动止盈止损）从头到尾
没测过**。这不是小事：实盘当天只跑到普通委托路径，策略委托要等仓位真的建立、
触发止盈止损才会走到；万一两处规则不同，雷会埋在"最该成交的那一刻"。补测后
两处口径确实相同（12/12 与预期一致），但这是运气，不是验证。已改为两个接口各跑
一组，且结论由 `_verdict()` 按实测结果现场生成，鉴权失败/限频一类答不出来的
响应判为 `未知` 并明说"本次结论不完整"——**探针能打印的口径，只能是它真查过的**。

同类还有：`_diag_open_orders_clordid.py` 首版把 SDK 方法名写成
`get_order_history`（实际是 `get_orders_history`，多个 s），`hist` 分支一跑就
`AttributeError`。只跑主分支、不跑冷门分支，等于没测那条分支。

**同一天第三次：修复本身是假的，只有拿真实数据跑才知道。** 给启动归因加
「上个进程还活着 ⇒ 并发实例，不是暴死」时，代码取的是**最新一条心跳**的 pid。
可生产心跳文件里最新一条几乎永远是 `source: embedded`（应用内系统监控写的内存
水位，**它不带 pid**），带 pid 的 `app` 行每分钟都比它早几秒。于是这条修复
在真实数据上**一次都不会触发**，而 22 个新增冒烟场景照样全绿——因为伪造数据时
我习惯性给每条都写了 pid。改成"判活去看最近一条**带 pid** 的心跳"之后，拿生产
`memory_history.jsonl` 做 A/B 才看到 `parallel_instance` 真的出来了。三条教训：

1. 造测试数据要照**真实数据形状**造，不能照"我以为的形状"造 —— 真实文件里那一行
   少个字段，往往就是它自己的坑；有生产数据可拿时，务必再跑一次真实数据 A/B。
2. 断言里"库里存的是什么"不能猜：`logger.error(..., exc_info=False)` 之后
   `record.exc_info` 是 `False` 不是 `None`，写 `is None` 的断言当场 FAIL（幸好
   是写反了方向暴露出来，而不是反过来静默通过）。
3. 常量/属性名要查不要编：诊断脚本里写了个不存在的 `psutil.LISTEN`（状态就是
   字符串 `'LISTEN'`），只在"多实例"分支才执行到，第一次跑就炸。

同类"冒烟自己造出假故障"还有一次：`_smoke_fix_regression.py` 用 `__new__` 造裸
`TrendRangeTrader` 桩（没 `spec_cache`），`_steps()` 把 AttributeError 当"读规格失败"
告警，每跑一次冒烟就往**实盘** `task_scheduler.log` 灌十几行"读取合约下单步长失败"，
排障时极易被误读成现网故障。2026-09-11 已改为：`spec_cache` 未注入 → 静默按兜底步长
返回；只有真注入了却读失败才告警。

## 闸门改动的在线验证步骤（改 `web_auth.py` 后补做一遍）

`_smoke_web_auth.py` 故意不导入 `crypto.app`（那会拉起调度器与 DB 预热），它只能证明
闸门自身逻辑对，证明不了“挂在真 app 上确实生效”。两边都验才算改完：

```powershell
$env:CRYPTO_NO_BACKGROUND='1'; $env:CRYPTO_WEB_PORT='5111'; python app.py   # 后台起临时实例
```

然后至少打四个点：未登录 `GET /` → 401 登录页；`POST /auth/gate` 带错口令 → 401 且不发
cookie；带对口令 → 302 到 `next` 且 `Set-Cookie` 含 HttpOnly；登录后 `GET /` 与
`/plan`、`/api-console`、`/api/task/status` 都不再是 401/403（验蓝图路由与模板/静态资源
没被误伤）。2026-09-10 就是这么跑过一轮 11 项全绿的。

## 历史打卡时间修复与自测（2026-09-22）

### 根因与改动

- `task_plan.js` 的「今日打卡 / 历史补录」和时间输入未被删除，也没有禁止选择过去日期；原位置在内容及关联任务列表之后，桌面和窄屏均可能滑出弹窗首屏。现已移到表单顶部。
- 原历史单点时间清空后仍提交 `fill-slot`，但不带 `filled_at`，后端按普通打卡使用当前时间。现前端必须选择完整时间；显式 `fill_mode: backfill` 缺时间时后端也拒绝，普通打卡省略时间仍兼容原行为。
- 原已打卡详情只有时间文本，`update-slot` 不处理 `filled_at`。现进行中卡片可编辑时间；未修改时间不重写、不丢秒数，接口仅传时间时保留正文、余额和任务关联。
- 交易改期重新走目标小时的服务端闸门，拒绝时不写入，放行后更新分析关联；未改期保留原分析关联。任务完成依据的时间通过原任务联动同步。
- 切换范围/清空时间会使旧单点闸门请求失效；范围余额请求也按序号丢弃过期响应。错误在当前表单内提示，避免弹提示时销毁待提交内容。
- 未改变新增打卡的进行中状态限制、历史日期可选范围、同日多条规则、批量 100 条上限、逐小时闸门/部分成功、批量关联固定 doing、局部加锁写回及学习卡结算规则。已结束卡片不开放改期。
- 模板更新静态脚本版本参数；未操作部署环境，也未重启实际服务。

### 自动化回归（无需真实数据库或服务）

在项目根目录运行：

```powershell
python -B -m unittest crypto._smoke_db_performance.PlanLocalWriteTests -v
python -B -X utf8 data/_plan_ui_verify.py --history-offline
node --check crypto/static/js/task_plan.js
git diff --check
```

- 后端：22 项通过（新增 9 项历史时间回归），使用内存 SQLite + 最小 Flask 蓝图，验证实际落库重读、交易真实闸门判定、拒绝时零修改、任务完成时间、当日与历史打卡、单条与批量、无关卡片不变以及学习卡满格结算/解锁。
- 前端：73 项断言通过，`--history-offline` 使用真实 JS、弹窗组件与 CSS，网络全拦截、接口以内存桩响应；不启动服务、不访问生产数据。覆盖 1440×700 / 375×700、学习/交易、空时间拒绝、秒数保留、范围逐日内容/余额、已打卡改期、过期闸门响应、失败表单保留及成功关闭。
- 完整 `crypto._smoke_db_performance` 共 58 项，57 项通过；原有 `test_whole_tree_write_is_limited_to_structural_entries` 失败：`api_settle_expired` 使用整树读写，但静态白名单未列入。用修复前 HEAD 源码重跑同一断言也失败，非本次引入；未为转绿改动该规则或结算实现。
- SQLite 验证不能代替 MySQL 并发锁验收；本次未连接真实数据库、未调用 OKX，也未做生产补录。

### 页面手工验收步骤

仅在测试环境创建/使用进行中的学习卡和交易卡：

1. 点空格 → 顶部「历史补录」→「单点补录」→ 选过去日期与具体时刻，填内容/关联任务后提交；重开记录，日期时间应保持不变。清空时间提交应拒绝，内容不丢。
2. 从历史切回「今日打卡」再提交，确认使用当前时间，不沿用刚才的历史日期。
3. 点已打卡格子 → 修改顶部时间 → 保存；确认格数未增加、内容和关联保留，若该格完成了任务，任务完成时间同步。原时间含秒但未修改时不能归零。
4. 学习卡和交易卡分别选择「范围补录」，设两个历史日期及每日时刻，确认预览后提交；学习卡逐日内容独立，交易卡逐日余额按原快照规则带入。空时刻/空日期不能提交。
5. 交易卡选缺分析的历史小时：单点或改期应被拦且表单保留；补齐目标小时分析后重试。批量中有缺分析的小时，应只保存合格条目并列出被拦小时。
6. 快速切换单点日期、范围、今日模式，状态条应始终对应当前模式；结束卡的时间只读。重复日期仍只提示，不新增禁止同日多次的限制。

## 已知缺口（待办）

- [ ] 公共断言 helper `ck()` / `_title()` / `FAILS` 在至少 4 个文件里各自复制
  （`_smoke_discipline_mail.py`、`_smoke_discipline_http.py`、`_smoke_strategy_gate.py`、
  `_smoke_discipline.py`），可考虑抽到 `crypto/_smoke_util.py`
- [ ] 根目录 `_run_smokes.py` 能一次跑完 `crypto/task/_smoke_*.py` 全部 18 个并
  打印每个的最后一行结论，但仍是临时脚本（未入库、不报错只列 FAILED）；可考虑
  收进 `crypto/_smoke_runner.py` 并支持只跑 🔒 组
- [ ] `_smoke_pos_history.py` 不是严格意义的冒烟测试，是查询工具，可考虑挪到 `demo/` 或重命名

## 维护约定

- 新增 `_smoke_*.py` 时，**必须**在对应分组表里加一行，至少写清"覆盖模块"和"何时重跑"
- 如果脚本会写库/发信/调真实 API，**必须**在 docstring 顶部写明，并在本索引放到对应分组
- docstring 里建议记录"曾经踩过的坑"（参考 `_smoke_discipline_mail.py` 第 3-21 行的事故记忆，
  以及 `_smoke_discipline_http.py` 第 22-24 行关于 `CRYPTO_NO_BACKGROUND` 的由来）
