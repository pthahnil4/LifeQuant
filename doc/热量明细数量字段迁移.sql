-- =====================================================================
-- 热量模块明细结构扩展：calorie_meal_items 增加 quantity / unit（2026-09-17）
-- ---------------------------------------------------------------------
-- 背景：食物支持「数量 × 单位热量 = 总热量」建模。
--   - calories 语义调整为「单位热量」（kcal/单位）
--   - quantity 为食用数量（支持小数，如 0.8 / 3）
--   - unit 为选中时食物库单位的快照（如 100克 / 1个）
--   - 该行总摄入 = calories × quantity；存量行 quantity=1，
--     与原「calories 即绝对热量」语义完全兼容，无需数据回填。
--
-- 说明：应用启动时 database.init_db() 会按 _REQUIRED_COLUMNS 自动补列，
-- 通常无需手工执行；本文件留档，供特殊环境（如直接操作生产库）手工执行。
-- 执行方式：mysql -u<用户> -p <库名> < 本文件
-- =====================================================================

ALTER TABLE `calorie_meal_items`
  ADD COLUMN `quantity` FLOAT NOT NULL DEFAULT 1 COMMENT '数量（支持小数，如 0.8/3）' AFTER `calories`,
  ADD COLUMN `unit` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '单位快照（如 100克/1个）' AFTER `quantity`;

-- 验证：
--   SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_DEFAULT
--   FROM information_schema.COLUMNS
--   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'calorie_meal_items';
