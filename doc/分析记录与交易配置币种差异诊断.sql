-- =============================================================================
-- 诊断：对比「定时任务交易配置币种」与「实盘分析记录币种」
-- 目的：定位分析记录页与交易配置页币种不一致的根因（账号 key 错配）
-- 背景：交易配置按账号存于 kv_store，key = 'strategy_config:{account}'；
--       未指定账号时网页默认口径 = DEFAULT_ACCOUNT（当前为 'main'）。
--       历史遗留的全局 key 'strategy_config' 可能与账号专属 key 内容不同，
--       若某端回退读全局就会看到不同币种列表。
-- 运行： mysql -h 49.51.136.88 -P 3306 -u hunter -p crypto < 本文件
-- =============================================================================

-- ① 各账号 / 全局的交易配置币种（DB 权威值）
SELECT `key`,
       JSON_LENGTH(JSON_EXTRACT(value, '$.currencies')) AS coin_cnt,
       JSON_EXTRACT(value, '$.currencies[*].instId')    AS coins,
       updated_at
FROM kv_store
WHERE `key` LIKE 'strategy_config%'
ORDER BY `key`;

-- ② 实盘分析记录里出现过的币种（含记录数与时间范围）
SELECT inst_id,
       COUNT(*) AS records,
       MIN(ts)  AS first_ts,
       MAX(ts)  AS last_ts
FROM task_analysis_records
GROUP BY inst_id
ORDER BY inst_id;

-- ③ 差异：分析记录里有、但默认账号(main)交易配置已不含的历史币种
--    （这些行在前端「每次进入自动同步币种下拉」后不会再出现在选择器里，
--     但历史分析记录仍保留，可用列表「批量删除」清理）
SELECT DISTINCT t.inst_id
FROM task_analysis_records t
WHERE t.inst_id NOT IN (
    SELECT JSON_UNQUOTE(JSON_EXTRACT(jt.coin, '$.instId'))
    FROM kv_store k
    JOIN JSON_TABLE(k.value, '$.currencies[*]'
         COLUMNS (coin JSON PATH '$')) jt
    WHERE k.`key` = 'strategy_config:main'
)
ORDER BY t.inst_id;
