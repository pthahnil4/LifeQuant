-- =====================================================================
-- cryptoTrade MySQL 建表脚本留档（与 crypto/models.py 保持同步）
-- 数据库: crypto  字符集: utf8mb4
-- 执行方式: 本脚本为建表权威来源；应用启动时 database.init_db()
--           仅做连通性探活与种子补全（不再逐表 create_all），
--           全新部署/迁移时用 migrate_json_to_db.py 触发完整建表。
--
-- 迁移批次记录：
--   批次1（2026-08-23）: meta + 随笔模块（journal_tags/journal_notes/note_tags）
--   批次2（2026-08-23）: 热量模块（calorie_foods/calorie_records/calorie_meal_items/calorie_config）+ kv_store
--   批次3（2026-08-23）: 任务计划模块（plan_plans/plan_cards/plan_slots）
--   批次4（2026-08-23）: 账户余额历史（balance_history）
--   批次5（2026-08-23）: 交易运行时状态（trader_directions/reverse_guard/manual_pause/
--                        tp_runtime_state/pos_book/pos_slot/pos_algo/pos_lev）
--   批次6（2026-08-23）: 结构化成交流水（trade_journal）
--   批次7a（2026-08-23）: 策略配置/币种自选整份存入 kv_store
--                        （key: strategy_config / coin_selection，无新表）
--   批次7b（2026-08-23）: 行情 CSV（crypto_coins / star_market，全列字符串）
--   批次8（2026-08-23）: 缓存类整份存入 kv_store
--                        （key: instrument_spec_cache / market_scan_cache，无新表）
--   批次9（2026-08-30）: 监控告警历史（alert_log）
--   批次10（2026-09-01）: 实盘分析记录（task_analysis_records）
--   批次11（2026-09-07）: 分析纪律（analysis_reminder_log 新表；
--                        task_analysis_records 增 hour_slot/source；
--                        plan_slots 增 analysis_ids/analysis_hour/bypass_analysis；
--                        配置整份存 kv_store key='analysis_discipline_config'）
--   批次12（2026-09-17）: 盘感模拟模块（instinct_corpus/instinct_wiki_rules/
--                        instinct_predictions/instinct_embeddings，
--                        详见 doc/RAG_LLM_Wiki模拟盘感落地方案.md）
--   批次13（2026-09-26）: 日记本模块（diary_settings/diary_entries/diary_goals，
--                        密码只存 argon2/bcrypt/pbkdf2 哈希串，永不存明文）
--   批次14（2026-09-26）: 美食打卡点模块（calorie_food_spots/calorie_food_spot_photos，
--                        探店记录挂在热量缺口页面下；照片文件落盘 data/food_spot_photos/，
--                        表内只存文件名索引，不存二进制）
--   增量（2026-09-17）:  热量明细结构扩展 calorie_meal_items 增 quantity/unit
--                        （数量 × 单位热量 = 总热量；存量库由 init_db 自动补列，
--                        手工迁移见 doc/热量明细数量字段迁移.sql）
-- =====================================================================

-- ---------------------------------------------------------------------
-- 元信息表：schema 版本 / 初始化时间
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `meta` (
  `key` VARCHAR(64) NOT NULL COMMENT '元信息键',
  `value` TEXT NOT NULL COMMENT '元信息值',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='元信息表（schema版本/初始化时间）';

-- ---------------------------------------------------------------------
-- 随笔标签字典（name 即主键；created_at 保持原 JSON 数组顺序）
-- 来源: journal_notes.json -> tags[]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `journal_tags` (
  `name` VARCHAR(32) NOT NULL COMMENT '标签名（≤8字，路由层校验）',
  `color` VARCHAR(16) NOT NULL DEFAULT '#999' COMMENT '标签色值',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='随笔标签字典';

-- ---------------------------------------------------------------------
-- 随笔 / 复盘条目（复盘四格拆为独立列，支持按字段检索）
-- 来源: journal_notes.json -> notes[]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `journal_notes` (
  `id` VARCHAR(32) NOT NULL COMMENT '条目ID（note_xxxxxxxx）',
  `type` VARCHAR(16) NOT NULL DEFAULT 'note' COMMENT 'note=随笔 / review=复盘',
  `content` TEXT NOT NULL COMMENT '正文',
  `review_subject` TEXT NULL COMMENT '复盘四格：对象',
  `review_decision` TEXT NULL COMMENT '复盘四格：当时决策',
  `review_outcome` TEXT NULL COMMENT '复盘四格：实际结果',
  `review_lesson` TEXT NULL COMMENT '复盘四格：经验教训',
  `pinned` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否置顶',
  `distilled` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已提炼为经验',
  `linked_from` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '关联来源条目ID',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_journal_notes_created` (`created_at`),
  KEY `idx_journal_notes_type` (`type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='随笔与复盘条目';

-- ---------------------------------------------------------------------
-- 笔记 ↔ 标签关联表（级联删除：删笔记清关联、删标签摘引用）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `note_tags` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `note_id` VARCHAR(32) NOT NULL COMMENT '条目ID',
  `tag_name` VARCHAR(32) NOT NULL COMMENT '标签名',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_note_tag` (`note_id`, `tag_name`),
  KEY `idx_note_tags_note` (`note_id`),
  KEY `idx_note_tags_tag` (`tag_name`),
  CONSTRAINT `fk_note_tags_note` FOREIGN KEY (`note_id`)
    REFERENCES `journal_notes` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_note_tags_tag` FOREIGN KEY (`tag_name`)
    REFERENCES `journal_tags` (`name`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='笔记-标签关联';

-- ---------------------------------------------------------------------
-- 食物热量库
-- 来源: calorie_food_db.json -> foods[]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `calorie_foods` (
  `id` VARCHAR(16) NOT NULL COMMENT '食物ID（food_xxx）',
  `name` VARCHAR(64) NOT NULL COMMENT '食物名称',
  `unit` VARCHAR(32) NOT NULL DEFAULT '100克' COMMENT '计量单位',
  `calories` FLOAT NOT NULL DEFAULT 0 COMMENT '热量(kcal)',
  `category` VARCHAR(32) NOT NULL DEFAULT '其他' COMMENT '分类',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  KEY `idx_calorie_foods_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='食物热量库';

-- ---------------------------------------------------------------------
-- 每日热量记录（id 即日期）
-- 来源: calorie_records.json -> records[]
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `calorie_records` (
  `id` VARCHAR(10) NOT NULL COMMENT '记录ID（= 日期 YYYY-MM-DD）',
  `date` VARCHAR(10) NOT NULL COMMENT '日期',
  `morning_weight` FLOAT NULL COMMENT '晨重(kg)',
  `evening_weight` FLOAT NULL COMMENT '晚重(kg)',
  `bmr` FLOAT NOT NULL DEFAULT 0 COMMENT '基础代谢',
  `breakfast_food` TEXT NOT NULL COMMENT '早餐食物名（逗号分隔）',
  `breakfast_calories` FLOAT NOT NULL DEFAULT 0 COMMENT '早餐热量',
  `lunch_food` TEXT NOT NULL COMMENT '午餐食物名',
  `lunch_calories` FLOAT NOT NULL DEFAULT 0 COMMENT '午餐热量',
  `dinner_food` TEXT NOT NULL COMMENT '晚餐食物名',
  `dinner_calories` FLOAT NOT NULL DEFAULT 0 COMMENT '晚餐热量',
  `intake_deficit` FLOAT NOT NULL DEFAULT 0 COMMENT '摄入缺口',
  `daily_steps` INT NOT NULL DEFAULT 0 COMMENT '步数',
  `exercise_calories` FLOAT NOT NULL DEFAULT 0 COMMENT '运动消耗',
  `calorie_deficit` FLOAT NOT NULL DEFAULT 0 COMMENT '当日热量缺口',
  `cumulative_deficit` FLOAT NOT NULL DEFAULT 0 COMMENT '累计缺口',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_calorie_records_date` (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='每日热量记录';

-- ---------------------------------------------------------------------
-- 三餐食物明细（原记录内 *_foods JSON 数组的关系化拆分）
-- 明细结构 {name, calories(单位热量), quantity(数量), unit(单位快照)}，
-- 该行总摄入 = calories × quantity；存量数据 quantity=1 与原绝对热量兼容
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `calorie_meal_items` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `record_id` VARCHAR(10) NOT NULL COMMENT '所属记录（日期）',
  `meal` VARCHAR(10) NOT NULL COMMENT 'breakfast/lunch/dinner',
  `position` INT NOT NULL DEFAULT 0 COMMENT '餐内顺序',
  `name` VARCHAR(64) NOT NULL COMMENT '食物名',
  `calories` FLOAT NOT NULL DEFAULT 0 COMMENT '单位热量(kcal/单位)',
  `quantity` FLOAT NOT NULL DEFAULT 1 COMMENT '数量（支持小数，如 0.8/3）',
  `unit` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '单位快照（如 100克/1个）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_meal_item` (`record_id`, `meal`, `position`),
  KEY `idx_meal_items_record` (`record_id`),
  CONSTRAINT `fk_meal_items_record` FOREIGN KEY (`record_id`)
    REFERENCES `calorie_records` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='三餐食物明细';

-- ---------------------------------------------------------------------
-- 热量模块配置（原 calorie_records.json 内嵌 config，单行表）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `calorie_config` (
  `id` INT NOT NULL PRIMARY KEY COMMENT '固定为 1（单行表）',
  `height` FLOAT NOT NULL DEFAULT 169 COMMENT '身高(cm)',
  `age` FLOAT NOT NULL DEFAULT 29 COMMENT '年龄',
  `step_frequency` FLOAT NOT NULL DEFAULT 0.7 COMMENT '步频系数',
  `weight_factor` FLOAT NOT NULL DEFAULT 55 COMMENT '体重系数(kg)',
  `target_deficit` FLOAT NOT NULL DEFAULT 100000 COMMENT '总目标缺口(kcal)'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='热量模块配置';

-- =====================================================================
-- 批次3：任务计划模块（task_plans.json → plan_plans / plan_cards / plan_slots）
-- 语义：整树读写；时间字段为业务字符串；小结构（daily_rule/round_config/
--       milestones/todos/notes/settlement）存 JSON 文本列
-- =====================================================================

CREATE TABLE IF NOT EXISTS `plan_plans` (
  `id` VARCHAR(32) NOT NULL COMMENT '计划ID（如 plan_learn_1000）',
  `sort_order` INT NOT NULL DEFAULT 0 COMMENT '原 plans 数组顺序',
  `type` VARCHAR(16) NOT NULL DEFAULT 'custom' COMMENT 'learn/trade/custom',
  `name` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '计划名称',
  `grand_goal` TEXT NOT NULL COMMENT '总目标描述',
  `total_hours` INT NOT NULL DEFAULT 100 COMMENT '总小时数',
  `round_count` INT NOT NULL DEFAULT 1 COMMENT '轮次数',
  `per_round_hours` INT NOT NULL DEFAULT 100 COMMENT '每轮小时数',
  `daily_rule` TEXT NOT NULL COMMENT '每日规则 JSON',
  `round_config` TEXT NULL COMMENT '轮次配置 JSON（交易计划）',
  `created_at` VARCHAR(19) NOT NULL DEFAULT '' COMMENT '创建时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='任务计划';

CREATE TABLE IF NOT EXISTS `plan_cards` (
  `id` VARCHAR(32) NOT NULL COMMENT '卡片ID',
  `plan_id` VARCHAR(32) NOT NULL COMMENT '所属计划ID',
  `sort_order` INT NOT NULL DEFAULT 0 COMMENT '原 cards 数组顺序',
  `type` VARCHAR(16) NOT NULL DEFAULT 'learn' COMMENT 'learn/trade',
  `round` INT NOT NULL DEFAULT 0 COMMENT '轮次序号',
  `title` VARCHAR(128) NOT NULL DEFAULT '' COMMENT '卡片标题',
  `goal` TEXT NOT NULL COMMENT '目标描述',
  `reward` INT NOT NULL DEFAULT 0 COMMENT '奖励',
  `base_reward` INT NULL COMMENT '基础奖励（交易卡）',
  `hourly_rate` INT NULL COMMENT '时薪（交易卡）',
  `status` VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/in_progress/completed/failed/abandoned',
  `start_time` VARCHAR(19) NOT NULL DEFAULT '' COMMENT '开始时间',
  `end_time` VARCHAR(19) NOT NULL DEFAULT '' COMMENT '结束时间',
  `milestones` TEXT NOT NULL COMMENT '子目标 JSON 数组',
  `todos` TEXT NOT NULL COMMENT 'TodoList JSON 数组',
  `tasks` TEXT NULL COMMENT '任务树 JSON（任务管理 v2；NULL=旧数据未迁移）',
  `notes` TEXT NOT NULL COMMENT '过程小记 JSON 数组',
  `review` TEXT NOT NULL COMMENT '复盘文字',
  `settlement` TEXT NULL COMMENT '结算结果 JSON',
  `created_at` VARCHAR(19) NOT NULL DEFAULT '' COMMENT '创建时间',
  `updated_at` VARCHAR(19) NOT NULL DEFAULT '' COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_cards_plan` (`plan_id`),
  CONSTRAINT `fk_plan_cards_plan` FOREIGN KEY (`plan_id`)
    REFERENCES `plan_plans` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='任务卡';

CREATE TABLE IF NOT EXISTS `plan_slots` (
  `card_id` VARCHAR(32) NOT NULL COMMENT '所属卡片ID',
  `slot_index` INT NOT NULL COMMENT '格子序号(0~99)',
  `filled` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已打卡',
  `filled_at` VARCHAR(19) NOT NULL DEFAULT '' COMMENT '打卡时间',
  `has_record` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否有记录体（区分 None/{}）',
  `content` TEXT NOT NULL COMMENT '学习内容（学习卡）',
  `duration_minutes` INT NOT NULL DEFAULT 0 COMMENT '时长分钟',
  `prediction` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '预测涨跌（交易卡）',
  `actual` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '实际涨跌（交易卡）',
  `hit` TINYINT(1) NULL COMMENT '预测是否命中（NULL=未计算）',
  `market_analysis` TEXT NOT NULL COMMENT '行情分析（交易卡）',
  `action_advice` TEXT NOT NULL COMMENT '操作建议（交易卡）',
  `account_balance` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '账户金额（交易卡）',
  `analysis_ids` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '本次打卡依据的分析记录ID（逗号分隔，批次11）',
  `analysis_hour` VARCHAR(13) NOT NULL DEFAULT '' COMMENT '打卡对应的小时槽 YYYY-MM-DD HH（批次11）',
  `bypass_analysis` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否无分析支撑放行（soft 模式留痕，批次11）',
  `task_links` TEXT NULL COMMENT '任务树关联 [{task_id,state}]（任务管理 v2；NULL/[]=待关联）',
  PRIMARY KEY (`card_id`, `slot_index`),
  CONSTRAINT `fk_plan_slots_card` FOREIGN KEY (`card_id`)
    REFERENCES `plan_cards` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='打卡格子';

-- ---------------------------------------------------------------------
-- 批次4：账户余额历史快照（来源: account_balance_history.json）
-- PK (account_key, ts) 天然对同 ts 去重；source 区分真实快照与回溯点
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `balance_history` (
  `account_key` VARCHAR(32) NOT NULL COMMENT '账号标识（api_config 的 account 字段）',
  `ts` BIGINT NOT NULL COMMENT '快照时间戳（UTC 毫秒）',
  `balance` DOUBLE NOT NULL DEFAULT 0 COMMENT '总权益（USD 估值，4位小数）',
  `source` VARCHAR(16) NOT NULL DEFAULT 'snapshot'
    COMMENT '来源: snapshot=真实快照 / backfill=回溯倒推',
  PRIMARY KEY (`account_key`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='账户余额历史快照';

-- ---------------------------------------------------------------------
-- 通用 KV 存储（配置/缓存类整体读写型数据，value 为 JSON 文本）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `kv_store` (
  `key` VARCHAR(64) NOT NULL COMMENT '键名',
  `value` TEXT NOT NULL COMMENT 'JSON 文本值',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间（缓存 TTL 判断用）',
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='通用KV存储';

-- ---------------------------------------------------------------------
-- 批次5：交易运行时状态（定时任务调度器持久化状态）
-- 语义：内存字典缓存 + 变更立即落库；落库失败仅告警不中断交易。
-- 金额/价格/时间戳小数列一律 DOUBLE（避免 FLOAT 单精度舍入）。
-- ---------------------------------------------------------------------

-- 各币种长短周期方向记录（来源: scheduler_state.json）
CREATE TABLE IF NOT EXISTS `trader_directions` (
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `short_dir` VARCHAR(8) NOT NULL COMMENT '短周期方向 long/short',
  `long_dir` VARCHAR(8) NOT NULL COMMENT '长周期方向 long/short',
  PRIMARY KEY (`inst_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='币种长短周期方向记录';

-- 反向持仓风控计时器（来源: reverse_guard_state.json）
CREATE TABLE IF NOT EXISTS `reverse_guard` (
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `detected_ts` DOUBLE NOT NULL DEFAULT 0 COMMENT '首次检测时间戳（epoch秒）',
  `long_direction` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '长周期方向',
  `reverse_side` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '反向持仓方向',
  `reverse_mode` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '反向持仓保证金模式',
  `reverse_amount` DOUBLE NOT NULL DEFAULT 0 COMMENT '反向持仓张数',
  `warned` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已发预警邮件',
  PRIMARY KEY (`inst_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='反向持仓风控计时器';

-- 人工强平冷却（来源: manual_pause_state.json）
CREATE TABLE IF NOT EXISTS `manual_pause` (
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `resume_ts` DOUBLE NOT NULL DEFAULT 0 COMMENT '恢复自动开仓的时间戳（epoch秒）',
  PRIMARY KEY (`inst_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='人工强平冷却';

-- 止盈引擎运行时状态（来源: tp_runtime_state.json）
CREATE TABLE IF NOT EXISTS `tp_runtime_state` (
  `state_key` VARCHAR(64) NOT NULL COMMENT '状态键 {inst_id}:{long|short}',
  `entry_ts` DOUBLE NOT NULL DEFAULT 0 COMMENT '入场时间戳（epoch秒）',
  `peak` DOUBLE NOT NULL DEFAULT 0 COMMENT '持仓期间峰值价',
  `trough` DOUBLE NOT NULL DEFAULT 0 COMMENT '持仓期间谷值价',
  `avg_px` DOUBLE NOT NULL DEFAULT 0 COMMENT '入场时持仓均价快照',
  `ladder_done` TEXT NOT NULL COMMENT '已触发分批档位 JSON 数组',
  `extra` TEXT NULL COMMENT '扩展状态 JSON（combo 的 P0/ATR0/R0/protect/partial_done 等）',
  PRIMARY KEY (`state_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='止盈引擎运行时状态';

-- 双仓位本地账本·篮子级（来源: position_order_state.json 拆表）
CREATE TABLE IF NOT EXISTS `pos_book` (
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `bucket` VARCHAR(8) NOT NULL COMMENT '篮子 trend/range',
  `held_long` DOUBLE NOT NULL DEFAULT 0 COMMENT '多头持仓张数（本地账本）',
  `held_short` DOUBLE NOT NULL DEFAULT 0 COMMENT '空头持仓张数（本地账本）',
  `avg_px_long` DOUBLE NOT NULL DEFAULT 0 COMMENT '多头加权入场均价',
  `avg_px_short` DOUBLE NOT NULL DEFAULT 0 COMMENT '空头加权入场均价',
  `prev_open_confirmed` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '上轮开仓窗口标识',
  `prev_close_confirmed` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '上轮平仓窗口标识',
  `last_desired` VARCHAR(8) NULL COMMENT '上一轮期望方向',
  PRIMARY KEY (`inst_id`, `bucket`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='双仓位本地账本（篮子级）';

-- 双仓位本地账本·挂单槽位（来源: position_order_state.json 拆表）
CREATE TABLE IF NOT EXISTS `pos_slot` (
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `bucket` VARCHAR(8) NOT NULL COMMENT '篮子 trend/range',
  `slot` VARCHAR(8) NOT NULL COMMENT '槽位 entry/exit',
  `state` VARCHAR(16) NOT NULL DEFAULT 'IDLE' COMMENT 'IDLE/PENDING/FILLED/EXPIRED',
  `ord_id` VARCHAR(40) NULL COMMENT '委托单ID',
  `price` DOUBLE NOT NULL DEFAULT 0 COMMENT '委托价',
  `amount` DOUBLE NOT NULL DEFAULT 0 COMMENT '委托张数',
  `placed_ts` DOUBLE NOT NULL DEFAULT 0 COMMENT '挂单时间戳（epoch秒）',
  `acc_filled` DOUBLE NOT NULL DEFAULT 0 COMMENT '累计已成交张数',
  `dir` VARCHAR(8) NULL COMMENT '持仓方向 long/short',
  `qfail_logged` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '查单失败日志已去重标记',
  PRIMARY KEY (`inst_id`, `bucket`, `slot`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='双仓位本地账本（挂单槽位）';

-- 双仓位本地账本·交易所侧兜底委托（来源: position_order_state.json 拆表）
CREATE TABLE IF NOT EXISTS `pos_algo` (
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `bucket` VARCHAR(8) NOT NULL COMMENT '篮子 trend/range',
  `direction` VARCHAR(8) NOT NULL COMMENT '方向 long/short',
  `algo_id` VARCHAR(40) NULL COMMENT '兜底委托ID',
  `amount` DOUBLE NOT NULL DEFAULT 0 COMMENT '委托张数',
  `sl` DOUBLE NOT NULL DEFAULT 0 COMMENT '止损触发价',
  `tp` DOUBLE NOT NULL DEFAULT 0 COMMENT '止盈触发价',
  `ts` DOUBLE NOT NULL DEFAULT 0 COMMENT '挂单时间戳（epoch秒）',
  PRIMARY KEY (`inst_id`, `bucket`, `direction`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='双仓位本地账本（兜底委托）';

-- 双仓位本地账本·杠杆设置缓存（来源: position_order_state.json 拆表）
-- 行存在即 lev_set 为 dict（两列均空=空dict）；行不存在即 lev_set=null
CREATE TABLE IF NOT EXISTS `pos_lev` (
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `cross_lev` DOUBLE NULL COMMENT '全仓杠杆（多头）',
  `isolated_lev` DOUBLE NULL COMMENT '逐仓杠杆（空头）',
  PRIMARY KEY (`inst_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='双仓位本地账本（杠杆缓存）';

-- ---------------------------------------------------------------------
-- 批次6：结构化成交流水（来源: logs/trade_journal.jsonl，append-only）
-- 自增 id 保留 JSONL 写入顺序（同一 ts 可能多条）；ts 保持业务字符串
-- （读取端字符串闭区间筛选）；旁路记录，落库失败不阻断交易。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `trade_journal` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增序号（保留写入顺序）',
  `ts` VARCHAR(19) NOT NULL COMMENT '成交确认时间 YYYY-MM-DD HH:MM:SS（本地时钟）',
  `run_id` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '调度轮次ID（可为空）',
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `bucket` VARCHAR(16) NOT NULL DEFAULT '' COMMENT 'trend/range/account(跨仓位强平)',
  `direction` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '持仓方向 long/short',
  `action` VARCHAR(8) NOT NULL DEFAULT '' COMMENT 'open/close',
  `price` DOUBLE NOT NULL DEFAULT 0 COMMENT '成交价（市价单为下单时最新价，近似值）',
  `amount` DOUBLE NOT NULL DEFAULT 0 COMMENT '成交张数',
  `ord_id` VARCHAR(40) NOT NULL DEFAULT '' COMMENT '交易所订单ID（可为空）',
  `reason` TEXT NOT NULL COMMENT 'signal=信号成交；其余为平仓原因原文',
  PRIMARY KEY (`id`),
  KEY `idx_trade_journal_ts` (`ts`),
  KEY `idx_trade_journal_inst` (`inst_id`),
  KEY `idx_tj_inst_ts` (`inst_id`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='结构化成交流水（实盘vs策略理论对比引擎数据源）';

-- ---------------------------------------------------------------------
-- 批次7b：行情 CSV（全列字符串，与 CSV 单元格契约一致，空值保持空串）
-- crypto_coins ← crypto_coins.csv（批量趋势分析结果，整表覆盖写）
-- star_market  ← star币种行情.csv（星标行情，id 顺序即拖拽排序后行序）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `crypto_coins` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `rank_no` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '排名（CSV列 rank）',
  `symbol` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '币种代码',
  `inst_id` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '合约ID',
  `name_cn` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '中文名称',
  `h1_trend` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '1H_趋势',
  `h1_price` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '1H_交易价格',
  `h1_time` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '1H_交易时间',
  `h1_profit` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '1H_盈亏%',
  `h1_close` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '1H_收盘价',
  `h1_macd` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'MACD_1H',
  `h1_dif` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'DIF_1H',
  `h1_adx` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ADX_1H',
  `h1_atr` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ATR_1H',
  `h1_sar` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'SAR_1H',
  `h1_sar_color` VARCHAR(8) NOT NULL DEFAULT '' COMMENT 'SAR颜色_1H',
  `h1_er` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ER_1H（Kaufman效率系数）',
  `h4_trend` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '4H_趋势',
  `h4_price` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '4H_交易价格',
  `h4_time` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '4H_交易时间',
  `h4_profit` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '4H_盈亏%',
  `h4_close` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '4H_收盘价',
  `h4_macd` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'MACD_4H',
  `h4_dif` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'DIF_4H',
  `h4_adx` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ADX_4H',
  `h4_atr` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ATR_4H',
  `h4_sar` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'SAR_4H',
  `h4_sar_color` VARCHAR(8) NOT NULL DEFAULT '' COMMENT 'SAR颜色_4H',
  `h4_er` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ER_4H（Kaufman效率系数）',
  `d1_trend` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '1D_趋势',
  `d1_price` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '1D_交易价格',
  `d1_time` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '1D_交易时间',
  `d1_profit` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '1D_盈亏%',
  `d1_close` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '1D_收盘价',
  `d1_macd` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'MACD_1D',
  `d1_dif` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'DIF_1D',
  `d1_adx` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ADX_1D',
  `d1_atr` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ATR_1D',
  `d1_sar` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'SAR_1D',
  `d1_sar_color` VARCHAR(8) NOT NULL DEFAULT '' COMMENT 'SAR颜色_1D',
  `d1_er` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ER_1D（Kaufman效率系数）',
  `m15_trend` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '15m_趋势',
  `m15_price` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '15m_交易价格',
  `m15_time` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '15m_交易时间',
  `m15_profit` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '15m_盈亏%',
  `m15_close` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '15m_收盘价',
  `m15_macd` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'MACD_15m',
  `m15_dif` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'DIF_15m',
  `m15_adx` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ADX_15m',
  `m15_atr` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ATR_15m',
  `m15_sar` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'SAR_15m',
  `m15_sar_color` VARCHAR(8) NOT NULL DEFAULT '' COMMENT 'SAR颜色_15m',
  `m15_er` VARCHAR(24) NOT NULL DEFAULT '' COMMENT 'ER_15m（Kaufman效率系数）',
  PRIMARY KEY (`id`),
  KEY `idx_crypto_coins_inst` (`inst_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='全币种多周期行情快照（迁移自 crypto_coins.csv）';

CREATE TABLE IF NOT EXISTS `star_market` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '行序（拖拽排序后整表重写重建）',
  `name` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '名称',
  `code` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '代码',
  `cur_price` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '现价',
  `t_15m` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '15分钟趋势',
  `t_60m` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '60分钟趋势',
  `t_4h` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '4小时趋势',
  `t_1d` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '日线趋势',
  `last_trade_time` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '上次交易时间(1H)',
  `direction` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '方向(1H) 做多/做空',
  `last_trade_price` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '上次交易价格(1H)',
  `profit_1h` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '策略盈亏(1H) 如 +14.41%',
  `hold_time` VARCHAR(24) NOT NULL DEFAULT '' COMMENT '持仓时间 如 12小时17分',
  `predict` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '预测涨跌（用户手填）',
  `advice` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '建议操作（用户手填）',
  PRIMARY KEY (`id`),
  KEY `idx_star_market_code` (`code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='星标币种行情（迁移自 star币种行情.csv）';

-- =============================================================================
-- 批次9：监控告警历史（异常行情与持仓盈亏监控报警系统）
-- =============================================================================
-- 旁路留档：落库失败只记日志，不阻断监控主循环与交易主流程。
-- metric_value / threshold 用 DOUBLE（遵循批次5约定，避免 FLOAT 单精度舍入）。
CREATE TABLE IF NOT EXISTS `alert_log` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `alert_type` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '告警类型 price/pnl',
  `inst_id` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '合约ID',
  `level` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '级别 warning/critical/recover',
  `metric_value` DOUBLE NOT NULL DEFAULT 0 COMMENT '触发时指标值（涨跌幅/盈亏率 %）',
  `threshold` DOUBLE NOT NULL DEFAULT 0 COMMENT '对应级别的触发阈值',
  `message` TEXT COMMENT '告警描述文本',
  `notify_sent` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否实际发出（被冷却/递进抑制为0）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '触发时间',
  PRIMARY KEY (`id`),
  KEY `idx_alert_time` (`created_at`),
  KEY `idx_alert_inst` (`inst_id`),
  KEY `idx_alert_inst_time` (`inst_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='监控告警历史（异常行情/持仓盈亏极端值）';

-- =============================================================================
-- 批次10：定时任务实盘分析记录（手动快照 + 个人判断 + 事后复盘回填）
-- 语义：用户手动触发快照（实时价格/长短周期方向）后填写个人涨跌判断与
-- 分析原因；记录满 1H/4H 后由查询端惰性回填后续价格（K线 close），
-- 用于个人判断 vs 策略方向命中率复盘。金额/价格列 DOUBLE（批次5约定）。
-- =============================================================================
CREATE TABLE IF NOT EXISTS `task_analysis_records` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `ts` VARCHAR(19) NOT NULL COMMENT '记录时间 YYYY-MM-DD HH:MM:SS（本地时钟）',
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `price` DOUBLE NOT NULL DEFAULT 0 COMMENT '快照时实时价格',
  `short_period` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '快照时短周期（如 5m）',
  `long_period` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '快照时长周期（如 4H）',
  `short_dir` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '短周期方向 long/short',
  `long_dir` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '长周期方向（当前时段原始方向）',
  `long_dir_prev` VARCHAR(8) NULL COMMENT '长周期上一时段方向（实际决策方向，可为空）',
  `atr_pct` DOUBLE NOT NULL DEFAULT 0 COMMENT 'ATR 百分比（辅助参考）',
  `user_judgment` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '个人判断 rise/fall/watch',
  `user_reason` TEXT COMMENT '分析原因（用户手填）',
  `hour_slot` VARCHAR(13) NOT NULL DEFAULT '' COMMENT '所属小时槽 YYYY-MM-DD HH（冗余，避免对 ts 做函数运算，批次11）',
  `source` VARCHAR(16) NOT NULL DEFAULT 'live' COMMENT '来源 live=当时记录/backfill=事后补记（服务端判定，批次11）',
  `price_1h` DOUBLE NULL COMMENT '记录后 1 小时价格（复盘回填）',
  `ts_1h` VARCHAR(19) NULL COMMENT '1H 回填取价对应的K线时间',
  `price_4h` DOUBLE NULL COMMENT '记录后 4 小时价格（复盘回填）',
  `ts_4h` VARCHAR(19) NULL COMMENT '4H 回填取价对应的K线时间',
  -- 批次16-A：先触碰判定（短周期 K 线逐根扫描，谁先触 ±θ 带判谁；端点列保留为参考）
  `touch_near` VARCHAR(8) NULL COMMENT '近窗口先触碰 up/down/none/conflict（NULL=待回填）',
  `touch_near_ts` VARCHAR(19) NULL COMMENT '近窗口首触(或冲突)bar起点时间',
  `touch_far` VARCHAR(8) NULL COMMENT '远窗口先触碰 up/down/none/conflict',
  `touch_far_ts` VARCHAR(19) NULL COMMENT '远窗口首触(或冲突)bar起点时间',
  -- 批次16-B：结构价计划（仅 rise/fall；无固定截止窗，结构自证 win/loss/open/gap）
  `target_price` DOUBLE NULL COMMENT '目标价（先到=win，可空=未挂计划）',
  `invalid_price` DOUBLE NULL COMMENT '失效价（先到=loss，分析作废）',
  `plan_result` VARCHAR(8) NULL COMMENT '计划终局 win/loss，未终局 open(观察中)/gap(超K线深度无法观测)',
  `plan_r` DOUBLE NULL COMMENT '兑现盈亏比 R（win时>0，loss时固定-1）',
  `plan_touch_ts` VARCHAR(19) NULL COMMENT '计划终局触发bar起点时间',
  PRIMARY KEY (`id`),
  KEY `idx_tar_inst_ts` (`inst_id`, `ts`),
  KEY `idx_tar_slot` (`hour_slot`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='定时任务实盘分析记录（手动快照+个人判断+复盘统计）';

-- =============================================================================
-- 批次11：分析纪律小时槽台账（Analysis Discipline）
-- -----------------------------------------------------------------------------
-- 巡检 job 每 5 分钟按 hour_slot 幂等 upsert：
--   既是邮件防重发依据（notified），也是看板合规率/断档热力/streak/
--   补记率的唯一数据源。hour_slot 唯一键保证多进程/重跑不重复计数。
-- status: satisfied（槽内当时就合格）/ missing（过宽限期仍缺）/
--         satisfied_later（先缺后补记补齐）/ exempt（人工豁免）
-- =============================================================================
CREATE TABLE IF NOT EXISTS `analysis_reminder_log` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `hour_slot` VARCHAR(13) NOT NULL COMMENT '小时槽 YYYY-MM-DD HH（幂等唯一键）',
  `stat_date` VARCHAR(10) NOT NULL DEFAULT '' COMMENT '所属日期 YYYY-MM-DD（按日聚合）',
  `required_count` INT NOT NULL DEFAULT 1 COMMENT '判定时要求的分析记录条数',
  `actual_count` INT NOT NULL DEFAULT 0 COMMENT '判定时实际的分析记录条数',
  `missing_insts` TEXT COMMENT '未覆盖币种 JSON 数组文本（require_cover_tracked 开启时有值）',
  `status` VARCHAR(16) NOT NULL DEFAULT 'missing' COMMENT 'satisfied/missing/satisfied_later/exempt',
  `notified` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已发缺口邮件（防重发）',
  `notified_at` VARCHAR(19) NOT NULL DEFAULT '' COMMENT '邮件发出时刻',
  `resolved_at` VARCHAR(19) NOT NULL DEFAULT '' COMMENT '补齐时刻（衡量“忘了多久”）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '台账创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_arl_hour_slot` (`hour_slot`),
  KEY `idx_arl_date` (`stat_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='分析纪律小时槽合格台账（闸门/巡检/看板共用）';

-- =============================================================================
-- 批次12：盘感模拟模块（RAG + LLM Wiki）
-- -----------------------------------------------------------------------------
-- 防泄漏核心不变式：ctx_* 列 = 决策当时可见字段（允许进检索与 prompt）；
-- outcome_*/hit_*/chg_* 列 = 事后才知字段（只做统计计分，永不进 prompt）。
-- 语料行由 crypto/instinct/corpus_builder.py 幂等生成（uk source+source_ref）。
-- =============================================================================
CREATE TABLE IF NOT EXISTS `instinct_corpus` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `source` VARCHAR(16) NOT NULL COMMENT 'analysis_record/trade_slot/journal_review',
  `source_ref` VARCHAR(64) NOT NULL COMMENT '源记录唯一键 tar:123/slot:card_3/note:xxx',
  `ts` VARCHAR(19) NOT NULL COMMENT '决策时刻 YYYY-MM-DD HH:MM:SS',
  `inst_id` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '币种（lesson 类为空=全局）',
  `short_period` VARCHAR(8) NOT NULL DEFAULT '',
  `long_period` VARCHAR(8) NOT NULL DEFAULT '',
  `ctx_short_dir` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '当时短周期方向 long/short',
  `ctx_long_dir` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '当时长周期方向',
  `ctx_long_dir_prev` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '长周期上一时段方向（实际决策方向）',
  `ctx_atr_pct` DOUBLE NOT NULL DEFAULT 0 COMMENT '当时 ATR%',
  `ctx_atr_pctile` DOUBLE NOT NULL DEFAULT -1 COMMENT '该币滚动窗口 ATR 分位 0~1，-1=未知',
  `ctx_dir_flipped` TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'long_dir 是否刚翻转(prev<>cur)',
  `ctx_price` DOUBLE NOT NULL DEFAULT 0 COMMENT '快照价（当时可见）',
  `ctx_text` TEXT COMMENT '当时写下的自由文本（分析原因/行情分析）',
  `judgment` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '当时决策归一三分类 rise/watch/fall',
  `decision_text` TEXT COMMENT '决策描述（lesson 类存结论）',
  `outcome_near` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '近窗口实际 up/flat/down，未回填为空',
  `outcome_far` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '远窗口实际',
  `chg_near_pct` DOUBLE NOT NULL DEFAULT 0 COMMENT '近窗口涨跌幅%',
  `chg_far_pct` DOUBLE NOT NULL DEFAULT 0 COMMENT '远窗口涨跌幅%',
  `hit_near` TINYINT(1) NULL COMMENT '近窗口命中（与 classify_move 口径对齐）',
  `hit_far` TINYINT(1) NULL COMMENT '远窗口命中',
  `labeled` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '1=已有远窗口结果，可用于 A/B',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ic_ref` (`source`, `source_ref`),
  KEY `idx_ic_inst_ts` (`inst_id`, `ts`),
  KEY `idx_ic_labeled` (`labeled`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='盘感语料库（情景记忆，ctx/outcome 防泄漏二分）';

CREATE TABLE IF NOT EXISTS `instinct_wiki_rules` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `rule_key` VARCHAR(64) NOT NULL COMMENT '稳定标识：scenario 短码，用于合并去重',
  `statement` TEXT NOT NULL COMMENT '规则正文（给用户和 LLM 看的一句话）',
  `kind` VARCHAR(16) NOT NULL DEFAULT 'scenario' COMMENT 'meta/scenario/prohibition',
  `condition_json` TEXT COMMENT '触发条件（机器可读）{dir_combo,atr_pctile_range,inst}',
  `stat_basis` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '统计依据，如 远窗口命中33.3%(n=15)',
  `evidence_refs` TEXT COMMENT '证据语料 id JSON 数组 [corpus_id,...]',
  `status` VARCHAR(16) NOT NULL DEFAULT 'candidate' COMMENT 'candidate/active/retired',
  `created_by` VARCHAR(16) NOT NULL DEFAULT 'distiller' COMMENT 'distiller/user',
  `supersedes_id` BIGINT NULL COMMENT '取代哪条旧规则（合并链）',
  `valid_until` VARCHAR(19) NULL COMMENT '有效期（过期自动降级 candidate）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_wiki_key` (`rule_key`),
  KEY `idx_wiki_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='盘感 Wiki 规则卡（语义记忆，candidate→active 状态机）';

CREATE TABLE IF NOT EXISTS `instinct_predictions` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `ts` VARCHAR(19) NOT NULL COMMENT '预测生成时刻',
  `inst_id` VARCHAR(32) NOT NULL COMMENT '合约ID',
  `ctx_snapshot_json` TEXT NOT NULL COMMENT '当时可见上下文快照（含检索入参）',
  `judgment` VARCHAR(8) NOT NULL DEFAULT '' COMMENT 'rise/watch/fall',
  `confidence` DOUBLE NOT NULL DEFAULT 0 COMMENT '0~1',
  `rationale` TEXT COMMENT 'LLM 理由（须引用规则/案例编号）',
  `cited_rule_ids` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '逗号分隔 instinct_wiki_rules.id',
  `cited_corpus_ids` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '逗号分隔 instinct_corpus.id',
  `wiki_ids` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '本次注入的 active 规则 id',
  `retrieved_refs_json` TEXT COMMENT '本次 topK 检索结果与得分（审计用）',
  `model` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '模型标识',
  `latency_ms` INT NOT NULL DEFAULT 0 COMMENT 'LLM 调用耗时',
  `price_at_pred` DOUBLE NOT NULL DEFAULT 0 COMMENT '生成预测时的价格（结算基准）',
  `status` VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/scored/error',
  `price_near` DOUBLE NULL COMMENT '近窗口结算价',
  `price_far` DOUBLE NULL COMMENT '远窗口结算价',
  `actual_near` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '近窗口实际 up/flat/down',
  `actual_far` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '远窗口实际',
  `hit_near` TINYINT(1) NULL COMMENT '近窗口命中（classify_move 口径）',
  `hit_far` TINYINT(1) NULL COMMENT '远窗口命中',
  `user_trusted` TINYINT(1) NULL COMMENT '用户反馈：当时会信吗（1/0/NULL）',
  `user_agree` TINYINT(1) NULL COMMENT '用户反馈：与你自己判断一致吗',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_ip_inst_ts` (`inst_id`, `ts`),
  KEY `idx_ip_status` (`status`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='LLM 影子预测流水与结算';

CREATE TABLE IF NOT EXISTS `instinct_embeddings` (
  `corpus_id` BIGINT NOT NULL COMMENT 'instinct_corpus.id',
  `model` VARCHAR(64) NOT NULL COMMENT 'embedding 模型标识，换模型全量重刷',
  `vec_json` MEDIUMTEXT NOT NULL COMMENT 'JSON 数组（float 列表）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  PRIMARY KEY (`corpus_id`, `model`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='语料文本向量（hybrid 检索文本路，v2 启用）';

-- =============================================================================
-- 支出管理：整数分、日期快照、独立单账本；审计时间由应用写入 UTC。
-- =============================================================================
CREATE TABLE IF NOT EXISTS `expense_settings` (
  `id` INT NOT NULL PRIMARY KEY,
  `currency` VARCHAR(3) NOT NULL,
  `timezone` VARCHAR(64) NOT NULL,
  `tracking_start_date` DATE NOT NULL,
  `initialized_at` DATETIME NULL,
  `seed_version` INT NOT NULL,
  `settings_json` TEXT NOT NULL,
  `version` INT NOT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `expense_categories` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY,
  `parent_id` VARCHAR(32) NULL,
  `parent_key` VARCHAR(32) NOT NULL,
  `name` VARCHAR(32) NOT NULL,
  `normalized_name` VARCHAR(64) NOT NULL,
  `color` VARCHAR(7) NOT NULL,
  `sort_order` INT NOT NULL,
  `is_active` BOOL NOT NULL,
  `is_system` BOOL NOT NULL,
  `version` INT NOT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,
  CONSTRAINT `uk_exp_category_name` UNIQUE (`parent_key`, `normalized_name`),
  FOREIGN KEY (`parent_id`) REFERENCES `expense_categories` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `expense_tags` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY,
  `name` VARCHAR(32) NOT NULL,
  `normalized_name` VARCHAR(64) NOT NULL UNIQUE,
  `color` VARCHAR(7) NOT NULL,
  `sort_order` INT NOT NULL,
  `is_favorite` BOOL NOT NULL,
  `is_active` BOOL NOT NULL,
  `version` INT NOT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `expense_periods` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY,
  `start_date` DATE NOT NULL UNIQUE,
  `end_date_exclusive` DATE NOT NULL,
  `budget_minor` BIGINT NULL,
  `rule_snapshot_json` TEXT NOT NULL,
  `version` INT NOT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `expense_bill_templates` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY,
  `name` VARCHAR(128) NOT NULL,
  `amount_minor` BIGINT NOT NULL,
  `category_id` VARCHAR(32) NOT NULL,
  `tag_ids_json` TEXT NOT NULL,
  `repeat_rule_json` TEXT NOT NULL,
  `effective_from` DATE NOT NULL,
  `is_active` BOOL NOT NULL,
  `version` INT NOT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,
  FOREIGN KEY (`category_id`) REFERENCES `expense_categories` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `expense_bill_occurrences` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY,
  `template_id` VARCHAR(32) NULL,
  `due_date` DATE NOT NULL,
  `expected_minor` BIGINT NOT NULL,
  `status` VARCHAR(16) NOT NULL,
  `closed_at` DATETIME NULL,
  `snapshot_json` TEXT NOT NULL,
  `version` INT NOT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,
  CONSTRAINT `uk_exp_bill_due` UNIQUE (`template_id`, `due_date`),
  KEY `idx_exp_bill_due_status` (`due_date`, `status`),
  FOREIGN KEY (`template_id`) REFERENCES `expense_bill_templates` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `expense_records` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY,
  `kind` VARCHAR(16) NOT NULL,
  `amount_minor` BIGINT NOT NULL,
  `business_date` DATE NOT NULL,
  `category_id` VARCHAR(32) NOT NULL,
  `title` VARCHAR(120) NOT NULL,
  `merchant` VARCHAR(128) NOT NULL,
  `note` TEXT NOT NULL,
  `payment_method` VARCHAR(64) NOT NULL,
  `original_record_id` VARCHAR(32) NULL,
  `unlinked_refund_reason` VARCHAR(255) NOT NULL,
  `bill_occurrence_id` VARCHAR(32) NULL,
  `client_request_id` VARCHAR(64) NOT NULL UNIQUE,
  `request_payload_hash` VARCHAR(64) NOT NULL,
  `deleted_at` DATETIME NULL,
  `version` INT NOT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,
  CONSTRAINT `ck_exp_positive` CHECK (`amount_minor` > 0),
  KEY `idx_exp_date_deleted` (`business_date`, `deleted_at`, `id`),
  KEY `idx_exp_category_date` (`category_id`, `business_date`),
  KEY `ix_expense_records_original_record_id` (`original_record_id`),
  KEY `ix_expense_records_bill_occurrence_id` (`bill_occurrence_id`),
  FOREIGN KEY (`category_id`) REFERENCES `expense_categories` (`id`),
  FOREIGN KEY (`original_record_id`) REFERENCES `expense_records` (`id`),
  FOREIGN KEY (`bill_occurrence_id`) REFERENCES `expense_bill_occurrences` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `expense_record_tags` (
  `record_id` VARCHAR(32) NOT NULL,
  `tag_id` VARCHAR(32) NOT NULL,
  PRIMARY KEY (`record_id`, `tag_id`),
  KEY `idx_exp_tag_record` (`tag_id`, `record_id`),
  FOREIGN KEY (`record_id`) REFERENCES `expense_records` (`id`),
  FOREIGN KEY (`tag_id`) REFERENCES `expense_tags` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `expense_daily_checks` (
  `business_date` DATE NOT NULL PRIMARY KEY,
  `confirmed_at` DATETIME NULL,
  `status` VARCHAR(16) NOT NULL,
  `version` INT NOT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `expense_change_logs` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY,
  `entity_type` VARCHAR(32) NOT NULL,
  `entity_id` VARCHAR(32) NOT NULL,
  `action` VARCHAR(32) NOT NULL,
  `before_json` TEXT NOT NULL,
  `after_json` TEXT NOT NULL,
  `reason` VARCHAR(255) NOT NULL,
  `created_at` DATETIME NOT NULL,
  KEY `idx_exp_log_entity` (`entity_type`, `entity_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- =============================================================================
-- 批次13：日记本模块（diary_settings / diary_entries / diary_goals）
-- 密码保护：password_hash 存 argon2/bcrypt/pbkdf2 前缀哈希串（如 'argon2id$...'），
-- 明文永不落库；可见性 public=公开浏览 / private=隐私（全部条目需解锁）。
-- =============================================================================

-- ---------------------------------------------------------------------
-- 日记本全局设置（单行 id=1）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `diary_settings` (
  `id` INT NOT NULL PRIMARY KEY COMMENT '固定单行 id=1',
  `visibility` VARCHAR(16) NOT NULL DEFAULT 'public' COMMENT 'public=公开 / private=隐私（需密码）',
  `password_hash` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '访问密码哈希（含算法前缀）；空=未设置',
  `birth_date` VARCHAR(10) NOT NULL DEFAULT '' COMMENT '出生年月日 YYYY-MM-DD（人生计数器）；空=未设置',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='日记本全局设置';

-- ---------------------------------------------------------------------
-- 日记条目（按天记录，一天可多条；private=1 的单条隐私需解锁才可见正文）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `diary_entries` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY COMMENT '条目ID（dentry_xxxxxxxx）',
  `date` VARCHAR(10) NOT NULL COMMENT '记录日期 YYYY-MM-DD（按天组织的维度）',
  `content` TEXT NOT NULL COMMENT '日记正文',
  `mood` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '心情标记（emoji 文本，可空）',
  `private` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '单条隐私标记：1=需解锁查看',
  `linked_from` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '跨模块关联来源（如 calorie:YYYY-MM-DD）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  KEY `idx_diary_entries_date` (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='日记条目（按天记录）';

-- ---------------------------------------------------------------------
-- 目标倒计时器（标题 + 截止日期，可多个并存）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `diary_goals` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY COMMENT '目标ID（dgoal_xxxxxxxx）',
  `title` VARCHAR(120) NOT NULL COMMENT '目标标题（如：掌握Python技能）',
  `due_date` VARCHAR(10) NOT NULL COMMENT '截止日期 YYYY-MM-DD（如：2026-10-05）',
  `note` TEXT NOT NULL COMMENT '备注（可空串）',
  `done` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已完成',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  KEY `idx_diary_goals_due` (`due_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='日记本目标倒计时';

-- =============================================================================
-- 批次14：美食打卡点模块（calorie_food_spots / calorie_food_spot_photos）
-- 探店记录挂在热量缺口页面下；照片文件本体落盘 data/food_spot_photos/，
-- 表内只存文件名索引（filename 为服务端生成的纯字母数字+扩展名），不存二进制。
-- =============================================================================

-- ---------------------------------------------------------------------
-- 美食打卡点主表（店铺/地址/种类/点评）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `calorie_food_spots` (
  `id` VARCHAR(32) NOT NULL PRIMARY KEY COMMENT '打卡点ID（fspot_xxxxxxxx）',
  `shop_name` VARCHAR(128) NOT NULL COMMENT '店铺名称',
  `address` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '详细地址',
  `category` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '美食种类/分类（如火锅/日料/小吃）',
  `review` TEXT NOT NULL COMMENT '个人点评/心得',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  KEY `idx_food_spot_create` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='美食打卡点（探店记录）';

-- ---------------------------------------------------------------------
-- 打卡点照片索引（美食特写/店铺环境/地图截图等，多张）
-- filename 为服务端重命名后的磁盘文件名，经 /calorie/api/food-spot/photo/<id> 访问
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `calorie_food_spot_photos` (
  `id` VARCHAR(48) NOT NULL PRIMARY KEY COMMENT '照片ID（fsp_<uuid16>）',
  `spot_id` VARCHAR(32) NOT NULL COMMENT '所属打卡点',
  `position` INT NOT NULL DEFAULT 0 COMMENT '上传顺序',
  `filename` VARCHAR(128) NOT NULL COMMENT '磁盘文件名（服务端生成，防穿越）',
  `original_name` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '上传时的原始文件名（仅展示用）',
  `file_size` INT NOT NULL DEFAULT 0 COMMENT '文件字节数',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  KEY `idx_food_spot_photo_spot` (`spot_id`),
  KEY `idx_food_spot_photo_pos` (`spot_id`, `position`),
  CONSTRAINT `fk_spot_photo_spot` FOREIGN KEY (`spot_id`)
    REFERENCES `calorie_food_spots` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='美食打卡点照片索引';

-- =============================================================================
-- 批次15：每日多维打卡（daily_checkins / trading_daily_snapshots）
-- 一天一条记录：热量/交易/学习三项自动指标提交时定格，外加自我评级(E~S可空)
-- 与纯文本评价。本模块对 calorie/plan/balance 表零写入。
-- 详见 doc/每日多维打卡设计文档.md
-- =============================================================================

-- ---------------------------------------------------------------------
-- 每日打卡记录（date 即主键，当天重复提交覆盖）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `daily_checkins` (
  `date` VARCHAR(10) NOT NULL PRIMARY KEY COMMENT '打卡日期 YYYY-MM-DD（本地日）',
  `cal_value` FLOAT NULL COMMENT '当日热量缺口定格(kcal)；NULL=未录入',
  `cal_passed` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '热量项达标（≥阈值）',
  `cal_status` VARCHAR(16) NOT NULL DEFAULT 'no_data' COMMENT 'ok / no_data',
  `trade_base` DOUBLE NULL COMMENT '基准权益（基准日最后快照，USDT）',
  `trade_base_date` VARCHAR(10) NOT NULL DEFAULT '' COMMENT '基准快照所属日期（回退时≠前一日）',
  `trade_live` DOUBLE NULL COMMENT '提交时实时总权益（USDT）',
  `trade_transfer` DOUBLE NULL COMMENT '当日净入金（剔除用）；NULL=账单未取到（未修正）',
  `trade_pct` DOUBLE NULL COMMENT '当日总权益涨幅 %（已剔除净入金）',
  `trade_passed` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '交易项达标（≥阈值）',
  `trade_status` VARCHAR(16) NOT NULL DEFAULT 'no_data' COMMENT 'ok / no_base / api_error / no_data',
  `trade_src` VARCHAR(16) NOT NULL DEFAULT '' COMMENT 'live=实拉 / cache=按日缓存',
  `learn_hours` FLOAT NULL COMMENT '当日学习时长定格（小时，learn 卡）',
  `task_done_count` INT NOT NULL DEFAULT 0 COMMENT '当日打卡关联 done 状态的去重任务数',
  `learn_passed` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '学习项达标（时长与任务数双阈值）',
  `learn_status` VARCHAR(16) NOT NULL DEFAULT 'ok' COMMENT 'ok / no_data',
  `rating` VARCHAR(2) NULL COMMENT '自我评级 E/D/C/B/A/S；NULL=未评',
  `comment` TEXT NOT NULL COMMENT '评价（纯文本）',
  `all_passed` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '三项全达标（提交时计算定格）',
  `account_key` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '交易项取数账号（审计）',
  `diary_entry_id` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '关联日记条目id（可空，提交时定格回填）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='每日评级总控台记录（热量/交易/学习+评级评价+日记关联）';

-- ---------------------------------------------------------------------
-- 交易打卡指标按日缓存（OKX 拉取结果，控制只读限频；同账号同日覆盖）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `trading_daily_snapshots` (
  `account_key` VARCHAR(32) NOT NULL COMMENT '账号标识（api_config account）',
  `date` VARCHAR(10) NOT NULL COMMENT '日期 YYYY-MM-DD',
  `base_balance` DOUBLE NULL COMMENT '基准权益',
  `base_date` VARCHAR(10) NOT NULL DEFAULT '' COMMENT '基准快照所属日期',
  `live_balance` DOUBLE NULL COMMENT '最近一次实时权益',
  `net_transfer` DOUBLE NULL COMMENT '当日净入金；NULL=账单未取到',
  `pct` DOUBLE NULL COMMENT '修正后当日收益率 %',
  `status` VARCHAR(16) NOT NULL DEFAULT '' COMMENT 'ok / no_base / api_error',
  `fetched_at` VARCHAR(19) NOT NULL DEFAULT '' COMMENT '最近实拉时间（TTL 判断）',
  PRIMARY KEY (`account_key`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='交易打卡指标按日缓存';

-- =============================================================================
-- 增量索引/列补丁（全新建库由上方 CREATE 语句包含；存量库由应用侧
-- database.init_db() 首次启动时自动检查补齐，无需手工执行；
-- 若需手工补建，可执行以下语句）
-- =============================================================================
-- ALTER TABLE `trade_journal` ADD INDEX `idx_tj_inst_ts` (`inst_id`, `ts`);
-- ALTER TABLE `alert_log`     ADD INDEX `idx_alert_inst_time` (`inst_id`, `created_at`);
-- 批次11（分析纪律）：
-- ALTER TABLE `task_analysis_records` ADD COLUMN `hour_slot` VARCHAR(13) NOT NULL DEFAULT '';
-- ALTER TABLE `task_analysis_records` ADD COLUMN `source`    VARCHAR(16) NOT NULL DEFAULT 'live';
-- ALTER TABLE `task_analysis_records` ADD INDEX  `idx_tar_slot` (`hour_slot`);
-- ALTER TABLE `plan_slots` ADD COLUMN `analysis_ids`    VARCHAR(255) NOT NULL DEFAULT '';
-- ALTER TABLE `plan_slots` ADD COLUMN `analysis_hour`   VARCHAR(13)  NOT NULL DEFAULT '';
-- ALTER TABLE `plan_slots` ADD COLUMN `bypass_analysis` TINYINT(1)   NOT NULL DEFAULT 0;
