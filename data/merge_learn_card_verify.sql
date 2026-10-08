-- =============================================================================
-- 学习任务卡「顺延合并」迁移后核对 SQL
-- 配套脚本：data/merge_learn_card.py（迁移） / data/merge_learn_card_verify.py（执行本文件）
-- 迁移内容：学习任务3（round=3, learn_252c445e）→ 学习任务2（round=2, learn_545dc97c）
--           打卡格子重新编号（起点 = 目标卡最大已用 slot_index + 1），小记直接追加末尾，
--           两张卡 goal 不动，来源卡清成空壳（status=pending / 0 格）。
-- 实测口径：目标卡 63 + 来源卡 24 = 87 格（不是 86；来源卡实际有 24 条打卡）
-- 说明：每张卡的 plan_slots 恒为 100 行（空格子也落库），所以"打卡数"= SUM(filled)。
-- 只读核对，不含任何 UPDATE/DELETE；如需回滚请用 --restore，不要手工改库。
--
-- ⚠️ 本文件刻意不使用 SET @var 变量：库表是 utf8mb4_general_ci，而用户变量带
--    连接层 collation（utf8mb4_0900_ai_ci），变量与列比较会报
--    1267/1270 Illegal mix of collations。字面量可被强制降级到列的 collation，
--    所以这里一律直接写常量。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- ① 两张卡格数 / 学习分钟数 / 打卡时间跨度 对账
--    期望：dst filled_count=87、src filled_count=0
-- -----------------------------------------------------------------------------
SELECT c.round,
       c.id                                                              AS card_id,
       c.title,
       c.status,
       COUNT(*)                                                          AS slot_rows,     -- 应为 100
       SUM(s.filled)                                                     AS filled_count,
       SUM(CASE WHEN s.filled THEN COALESCE(s.duration_minutes, 0) END)  AS total_minutes,
       MIN(CASE WHEN s.filled THEN NULLIF(s.filled_at, '') END)          AS first_at,
       MAX(CASE WHEN s.filled THEN NULLIF(s.filled_at, '') END)          AS last_at,
       COUNT(DISTINCT CASE WHEN s.filled THEN LEFT(NULLIF(s.filled_at, ''), 10) END)
                                                                         AS presence_days
FROM plan_cards c
JOIN plan_slots s ON s.card_id = c.id
WHERE c.id IN ('learn_545dc97c', 'learn_252c445e')
GROUP BY c.round, c.id, c.title, c.status
ORDER BY c.round;

-- -----------------------------------------------------------------------------
-- ② slot_index 主键完整性：格子行数必须 100、编号必须 0..99 无重复无缺失
--    期望：dup_idx = 0、missing_idx = 0
-- -----------------------------------------------------------------------------
SELECT c.round,
       c.id AS card_id,
       COUNT(*)                          AS slot_rows,
       COUNT(DISTINCT s.slot_index)      AS uniq_idx,
       100 - COUNT(DISTINCT s.slot_index) AS missing_idx,
       COUNT(*) - COUNT(DISTINCT s.slot_index) AS dup_idx
FROM plan_cards c
JOIN plan_slots s ON s.card_id = c.id
WHERE c.id IN ('learn_545dc97c', 'learn_252c445e')
GROUP BY c.round, c.id
ORDER BY c.round;

-- -----------------------------------------------------------------------------
-- ③ 全局重复编号兜底（跨这两张卡再查一次，确认迁移没写出重复主键行）
--    期望：空结果集
-- -----------------------------------------------------------------------------
SELECT card_id, slot_index, COUNT(*) AS n
FROM plan_slots
WHERE card_id IN ('learn_545dc97c', 'learn_252c445e')
GROUP BY card_id, slot_index
HAVING COUNT(*) > 1;

-- -----------------------------------------------------------------------------
-- ④ 目标卡已用编号是否连续（0..86 连续 = 重编号起点正确、无空洞）
--    期望：filled=87、min_idx=0、max_idx=86、span=87、kept_from_dst=63、moved_from_src=24
-- -----------------------------------------------------------------------------
SELECT COUNT(*)            AS filled,
       MIN(s.slot_index)   AS min_idx,
       MAX(s.slot_index)   AS max_idx,
       MAX(s.slot_index) - MIN(s.slot_index) + 1 AS span,
       SUM(CASE WHEN s.slot_index < 63 THEN 1 ELSE 0 END)  AS kept_from_dst,   -- 原卡2 63 格
       SUM(CASE WHEN s.slot_index >= 63 THEN 1 ELSE 0 END) AS moved_from_src   -- 搬入 24 格
FROM plan_slots s
WHERE s.card_id = 'learn_545dc97c' AND s.filled;

-- -----------------------------------------------------------------------------
-- ⑤ 合并前后格数守恒（目标卡原有 + 来源卡搬入 = 原两卡合计，不增不减）
--    期望：dst_filled=87、src_filled=0、sum_filled=87
-- -----------------------------------------------------------------------------
SELECT SUM(CASE WHEN card_id = 'learn_545dc97c' THEN filled ELSE 0 END) AS dst_filled,
       SUM(CASE WHEN card_id = 'learn_252c445e' THEN filled ELSE 0 END) AS src_filled,
       SUM(filled)                                                      AS sum_filled
FROM plan_slots
WHERE card_id IN ('learn_545dc97c', 'learn_252c445e');

-- -----------------------------------------------------------------------------
-- ⑥ 搬入格子的时间序是否单调（重编号按 filled_at 升序分配，编号递增则时间不减）
--    期望：bad_order = 0
-- -----------------------------------------------------------------------------
SELECT COUNT(*) AS bad_order
FROM (
    SELECT s.slot_index,
           s.filled_at,
           LAG(s.slot_index) OVER (ORDER BY s.filled_at, s.slot_index) AS prev_idx
    FROM plan_slots s
    WHERE s.card_id = 'learn_545dc97c' AND s.filled
) t
WHERE t.prev_idx IS NOT NULL AND t.slot_index < t.prev_idx;

-- -----------------------------------------------------------------------------
-- ⑦ 卡级字段核对：notes 条数（目标 32+11=43、来源 0）、review、settlement、goal 未被改动
--    期望：两卡 settlement 均为 NULL（目标卡结算已撤销）、两卡 goal 原样保留
-- -----------------------------------------------------------------------------
SELECT c.round,
       c.id                                   AS card_id,
       c.status,
       JSON_LENGTH(NULLIF(c.notes, ''))       AS notes_count,
       CHAR_LENGTH(c.review)                  AS review_chars,
       JSON_LENGTH(NULLIF(c.tasks, ''))       AS task_roots,
       c.settlement                           AS settlement_json,
       LEFT(c.goal, 40)                       AS goal_head,
       CHAR_LENGTH(c.goal)                    AS goal_chars,
       c.reward, c.base_reward, c.hourly_rate,
       c.start_time, c.end_time, c.updated_at
FROM plan_cards c
WHERE c.id IN ('learn_545dc97c', 'learn_252c445e')
ORDER BY c.round;

-- -----------------------------------------------------------------------------
-- ⑧ 孤儿 task_links 检测（格子关联的 task_id 必须能在【本卡】任务树里找到）
--    需要 MySQL 8.0.4+（JSON_TABLE）。期望：空结果集
--    非空 = 前端该格会显示「已删除任务」，任务进度与实际投入归因静默丢失
-- -----------------------------------------------------------------------------
SELECT s.card_id,
       s.slot_index,
       jt.task_id,
       jt.state
FROM plan_slots s
JOIN plan_cards c ON c.id = s.card_id
JOIN JSON_TABLE(
        IFNULL(NULLIF(s.task_links, ''), '[]'),
        '$[*]' COLUMNS (task_id VARCHAR(64) PATH '$.task_id',
                        state   VARCHAR(16) PATH '$.state')) AS jt
LEFT JOIN JSON_TABLE(
        IFNULL(NULLIF(c.tasks, '[]'), '[]'),
        '$[*]' COLUMNS (root_id VARCHAR(64) PATH '$.id')) tr
       ON tr.root_id = jt.task_id
LEFT JOIN JSON_TABLE(
        IFNULL(NULLIF(c.tasks, '[]'), '[]'),
        '$[*].children[*]' COLUMNS (ch_id VARCHAR(64) PATH '$.id')) tc
       ON tc.ch_id = jt.task_id
WHERE s.card_id IN ('learn_545dc97c', 'learn_252c445e')
  AND s.filled
  AND tr.root_id IS NULL
  AND tc.ch_id IS NULL;   -- 根层与子层（本计划任务树最深 2 层）都没命中才算孤儿

-- -----------------------------------------------------------------------------
-- ⑨ done 节点的「完成依据格子」必须指向本卡已勾选格子（否则删打卡回退会指错格）
--    期望：空结果集
-- -----------------------------------------------------------------------------
SELECT c.round,
       c.id AS card_id,
       jt.title,
       jt.completed_by_slot
FROM plan_cards c
JOIN JSON_TABLE(
        IFNULL(NULLIF(c.tasks, '[]'), '[]'),
        '$[*]' COLUMNS (title VARCHAR(128) PATH '$.title',
                        status VARCHAR(16) PATH '$.status',
                        completed_by_slot INT PATH '$.completed_by_slot')) jt
LEFT JOIN plan_slots s
       ON s.card_id = c.id AND s.slot_index = jt.completed_by_slot AND s.filled
WHERE c.id IN ('learn_545dc97c', 'learn_252c445e')
  AND jt.status = 'done'
  AND jt.completed_by_slot IS NOT NULL
  AND s.card_id IS NULL;

-- -----------------------------------------------------------------------------
-- ⑩ 来源卡必须是干净空壳（0 格 / 0 小记 / 无打卡关联 / status=pending / 无结算）
--    期望：一行，所有 chk_* 均为 OK（goal 保留 = OK(目标保留)）
-- -----------------------------------------------------------------------------
SELECT c.round,
       c.id AS card_id,
       c.status,
       CASE WHEN COALESCE(agg.f, 0) = 0 THEN 'OK' ELSE 'FAIL:残留打卡' END AS chk_filled,
       CASE WHEN JSON_LENGTH(NULLIF(c.notes, '')) = 0 THEN 'OK' ELSE 'FAIL:残留小记' END AS chk_notes,
       CASE WHEN COALESCE(agg.links, 0) = 0 THEN 'OK' ELSE 'FAIL:残留关联' END AS chk_links,
       CASE WHEN c.settlement IS NULL THEN 'OK' ELSE 'FAIL:残留结算' END AS chk_settle,
       CASE WHEN NULLIF(c.start_time, '') IS NULL AND NULLIF(c.end_time, '') IS NULL
            THEN 'OK' ELSE 'FAIL:残留起止时间' END AS chk_time,
       CASE WHEN CHAR_LENGTH(c.goal) > 0 THEN 'OK(目标保留)' ELSE 'INFO:目标为空' END AS chk_goal
FROM plan_cards c
LEFT JOIN (
    SELECT card_id,
           SUM(filled) AS f,
           SUM(CASE WHEN NULLIF(task_links, '') IS NOT NULL
                     AND JSON_LENGTH(task_links) > 0 THEN 1 ELSE 0 END) AS links
    FROM plan_slots
    GROUP BY card_id
) agg ON agg.card_id = c.id
WHERE c.id = 'learn_252c445e';

-- -----------------------------------------------------------------------------
-- ⑪ 整计划串行链视图（确认"当前可打卡卡"落在目标卡上，来源卡为 pending 锁定）
--    期望：round1 completed / round2 in_progress 87h / round3 pending 0h
-- -----------------------------------------------------------------------------
SELECT c.round,
       c.type,
       c.title,
       c.status,
       COALESCE(agg.f, 0) AS filled,
       CASE WHEN c.settlement IS NULL THEN NULL
            ELSE JSON_UNQUOTE(JSON_EXTRACT(c.settlement, '$.final_reward')) END AS settled_reward
FROM plan_cards c
LEFT JOIN (SELECT card_id, SUM(filled) AS f FROM plan_slots GROUP BY card_id) agg
       ON agg.card_id = c.id
WHERE c.plan_id = 'plan_learn_1000'
ORDER BY c.sort_order;

-- -----------------------------------------------------------------------------
-- ⑫ 看板"已获奖金"口径（= 该计划内 Σ 各卡 settlement.final_reward，按计划分组）
--    撤销目标卡结算后，plan_learn_1000 应只剩 round1 的 2760，与页面顶部数字一致
--    （注意：不按 plan 分组会把交易计划的结算也算进来，数字会对不上）
-- -----------------------------------------------------------------------------
SELECT c.plan_id,
       p.name,
       COUNT(c.settlement) AS settled_cards,
       SUM(CAST(JSON_UNQUOTE(JSON_EXTRACT(c.settlement, '$.final_reward')) AS DECIMAL(10, 2)))
        AS earned_reward
FROM plan_cards c
JOIN plan_plans p ON p.id = c.plan_id
WHERE c.settlement IS NOT NULL
GROUP BY c.plan_id, p.name;
