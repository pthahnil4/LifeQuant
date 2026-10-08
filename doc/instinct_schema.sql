-- =============================================================================
-- 盘感模拟模块（RAG + LLM Wiki）建表 SQL
-- 权威来源约定与 db_schema.sql 一致：本文件为 P1 阶段增量建表脚本，
-- 经 migrate 流程确认后并入 db_schema.sql。
-- 字符集 utf8mb4，全部 InnoDB。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. instinct_corpus —— 标准化语料库（情景记忆层）
--    三个来源统一压缩成"一行一样本"：
--      analysis_record: task_analysis_records（判断+近/远窗口回填）
--      trade_slot:      plan_slots × plan_cards(type='trade')（预测+结算+行情文本）
--      journal_review:  journal_notes(type='review')（复盘四格 lesson）
--    ctx_* 列 = 当时可见字段（进检索与 prompt）；
--    outcome_* 列 = 事后才知字段（只做统计计分，永不进 prompt，防泄漏）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `instinct_corpus` (
  `id`            BIGINT NOT NULL AUTO_INCREMENT,
  `source`        VARCHAR(16)  NOT NULL COMMENT 'analysis_record/trade_slot/journal_review',
  `source_ref`    VARCHAR(64)  NOT NULL COMMENT '源记录唯一键，如 tar:123 / slot:cardid_3 / note:xxxx',
  `ts`            VARCHAR(19)  NOT NULL COMMENT '决策时刻 YYYY-MM-DD HH:MM:SS',
  `inst_id`       VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '币种（lesson 类可为空=全局）',
  `short_period`  VARCHAR(8)   NOT NULL DEFAULT '',
  `long_period`   VARCHAR(8)   NOT NULL DEFAULT '',
  -- ▼ 当时可见上下文（防泄漏边界：只有这些字段允许进入检索与 prompt）
  `ctx_short_dir`   VARCHAR(8)  NOT NULL DEFAULT '' COMMENT 'short/long/flat，未知为空',
  `ctx_long_dir`    VARCHAR(8)  NOT NULL DEFAULT '',
  `ctx_long_dir_prev` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '实际决策方向',
  `ctx_atr_pct`     DOUBLE      NOT NULL DEFAULT 0   COMMENT '当时 ATR%',
  `ctx_atr_pctile`  DOUBLE      NOT NULL DEFAULT -1  COMMENT '该币滚动窗口内 ATR 分位 0~1，-1=未知',
  `ctx_dir_flipped` TINYINT(1)  NOT NULL DEFAULT 0   COMMENT 'long_dir 是否刚翻转(prev<>cur)',
  `ctx_price`       DOUBLE      NOT NULL DEFAULT 0,
  `ctx_text`        TEXT        NULL COMMENT '当时 written 的自由文本(分析原因/行情分析)',
  -- ▼ 当时的决策
  `judgment`      VARCHAR(8)   NOT NULL DEFAULT '' COMMENT 'rise/watch/fall/predict 归一三分类',
  `decision_text` TEXT         NULL COMMENT '决策描述(lesson 类存结论)',
  -- ▼ 事后结果（永不进 prompt；仅统计与展示标签）
  `outcome_near`  VARCHAR(8)   NOT NULL DEFAULT '' COMMENT 'up/flat/down，未回填为空',
  `outcome_far`   VARCHAR(8)   NOT NULL DEFAULT '',
  `chg_near_pct`  DOUBLE       NOT NULL DEFAULT 0,
  `chg_far_pct`   DOUBLE       NOT NULL DEFAULT 0,
  `hit_near`      TINYINT(1)   NULL COMMENT '三分类口径命中（与 classify_move 对齐）',
  `hit_far`       TINYINT(1)   NULL,
  `labeled`       TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=已有远窗口结果，可用于 A/B',
  `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ic_ref` (`source`, `source_ref`),
  KEY `idx_ic_inst_ts` (`inst_id`, `ts`),
  KEY `idx_ic_labeled` (`labeled`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='盘感语料库（情景记忆）';

-- -----------------------------------------------------------------------------
-- 2. instinct_wiki_rules —— 语义记忆层（蒸馏规则卡）
--    candidate=蒸馏任务产出的候选（待用户确认），active=生效进 prompt，
--    retired=被新规则取代/失效（保留追溯，不物理删除）。
--    合并策略沿用学习任务卡顺延合并经验：同 scenario 的新证据更新既有条目，
--    而不是插新卡造成规则矛盾。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `instinct_wiki_rules` (
  `id`          BIGINT NOT NULL AUTO_INCREMENT,
  `rule_key`    VARCHAR(64)  NOT NULL COMMENT '稳定标识：scenario 短码，用于合并去重',
  `statement`   TEXT         NOT NULL COMMENT '规则正文（给用户和 LLM 看的一句话）',
  `kind`        VARCHAR(16)  NOT NULL DEFAULT 'scenario' COMMENT 'meta/scenario/prohibition',
  `condition_json` TEXT      NULL COMMENT '触发条件（机器可读）：{dir_combo,atr_pctile_range,inst,...}',
  `stat_basis`  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '统计依据，如 远窗口命中33.3%(n=15)',
  `evidence_refs` TEXT       NULL COMMENT '证据语料 id JSON 数组 [corpus_id,...]',
  `status`      VARCHAR(16)  NOT NULL DEFAULT 'candidate' COMMENT 'candidate/active/retired',
  `created_by`  VARCHAR(16)  NOT NULL DEFAULT 'distiller' COMMENT 'distiller/user',
  `supersedes_id` BIGINT     NULL COMMENT '取代哪条旧规则（合并链）',
  `valid_until` VARCHAR(19)  NULL COMMENT '有效期（盘感规则会过期，过期自动降级 candidate）',
  `created_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_wiki_key` (`rule_key`),
  KEY `idx_wiki_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='盘感 Wiki 规则卡（语义记忆）';

-- -----------------------------------------------------------------------------
-- 3. instinct_predictions —— LLM 影子预测流水
--    每次生成一条，pending 状态由回填任务按近/远窗口结算为 scored。
--    retrieved_refs_json 完整留存当次检索证据，供逐案复盘"它引用得对不对"。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `instinct_predictions` (
  `id`            BIGINT NOT NULL AUTO_INCREMENT,
  `ts`            VARCHAR(19)  NOT NULL COMMENT '预测生成时刻',
  `inst_id`       VARCHAR(32)  NOT NULL,
  `ctx_snapshot_json` TEXT     NOT NULL COMMENT '当时可见上下文快照（含检索入参）',
  `judgment`      VARCHAR(8)   NOT NULL DEFAULT '' COMMENT 'rise/watch/fall',
  `confidence`    DOUBLE       NOT NULL DEFAULT 0 COMMENT '0~1',
  `rationale`     TEXT         NULL COMMENT 'LLM 理由（须引用规则/案例编号）',
  `cited_rule_ids`    VARCHAR(255) NOT NULL DEFAULT '' COMMENT '逗号分隔 instinct_wiki_rules.id',
  `cited_corpus_ids`  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '逗号分隔 instinct_corpus.id',
  `wiki_ids`      VARCHAR(255) NOT NULL DEFAULT '' COMMENT '本次注入的 active 规则 id',
  `retrieved_refs_json` TEXT   NULL COMMENT '本次 topK 检索结果与得分（审计用）',
  `model`         VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '模型标识',
  `latency_ms`    INT          NOT NULL DEFAULT 0,
  `price_at_pred` DOUBLE       NOT NULL DEFAULT 0,
  -- ▼ 结算列（由回填任务写，口径 = analysis_record_repo.classify_move）
  `status`        VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending/scored/error',
  `price_near`    DOUBLE       NULL,
  `price_far`     DOUBLE       NULL,
  `actual_near`   VARCHAR(8)   NOT NULL DEFAULT '',
  `actual_far`    VARCHAR(8)   NOT NULL DEFAULT '',
  `hit_near`      TINYINT(1)   NULL,
  `hit_far`       TINYINT(1)   NULL,
  `user_trusted`  TINYINT(1)   NULL COMMENT '用户反馈：当时会选择相信系统判断吗（1/0/NULL）',
  `user_agree`    TINYINT(1)   NULL COMMENT '用户反馈：系统判断与你自己一致吗',
  `created_at`    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ip_inst_ts` (`inst_id`, `ts`),
  KEY `idx_ip_status` (`status`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='LLM 影子预测流水与结算';

-- -----------------------------------------------------------------------------
-- 4. instinct_embeddings —— 文本向量表（v2 启用，v1 可建空表）
--    远端 MySQL 不依赖向量类型：向量 JSON 存 TEXT，
--    相似度在 Python 侧用 numpy 余弦计算；量大后换本地 faiss（data/instinct/）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `instinct_embeddings` (
  `corpus_id`  BIGINT      NOT NULL COMMENT 'instinct_corpus.id',
  `model`      VARCHAR(64) NOT NULL COMMENT 'embedding 模型标识，换模型全量重刷',
  `vec_json`   MEDIUMTEXT  NOT NULL COMMENT 'JSON 数组（float 列表）',
  `created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`corpus_id`, `model`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='语料文本向量（hybrid 检索文本路）';
