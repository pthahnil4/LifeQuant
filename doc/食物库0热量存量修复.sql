-- =====================================================================
-- 修复食物库中 0 热量存量食物（2026-09-22 回归修复配套）
-- ---------------------------------------------------------------------
-- 背景：旧版 POST /calorie/api/record 的新食物自动入库只传名称、
-- 恒写 calories=0，污染了 calorie_foods 表；这些 0 热量食物在搜索
-- 建议中被选中时，会把热量输入框回填为 0（本次代码已修复入库逻辑，
-- 但存量 0 热量行需要一次性回填）。
--
-- 回填口径：以三餐明细表 calorie_meal_items 中同名食物的单位热量
-- 均值为准（明细 calories 语义 = 单位热量；quantity=1 的旧行即绝对
-- 热量，均值同样成立）。仅更新当前值为 0 且未被禁用（0 卡司）的行。
-- 注意：矿泉水/黑咖啡等本就 0 热量的预置食物若无同名非零明细引用，
-- 不会被本脚本误改（子查询只包含 calories > 0 的明细）。
--
-- 执行方式：mysql -u<用户> -p <库名> < 本文件
-- 建议先跑文末的 SELECT 预览受影响行，再执行 UPDATE。
-- =====================================================================

-- 预览：将被修复的食物
-- SELECT f.id, f.name, f.calories AS old_calories, m.unit_cal AS new_calories
-- FROM calorie_foods f
-- JOIN (
--   SELECT name, AVG(calories) AS unit_cal
--   FROM calorie_meal_items
--   WHERE calories > 0
--   GROUP BY name
-- ) m ON m.name = f.name
-- WHERE f.calories = 0;

UPDATE calorie_foods f
JOIN (
  SELECT name, AVG(calories) AS unit_cal
  FROM calorie_meal_items
  WHERE calories > 0
  GROUP BY name
) m ON m.name = f.name
SET f.calories = m.unit_cal
WHERE f.calories = 0;

-- 顺带单位修复（可选）：0 热量入库的食物单位曾被硬编码为 '100克'，
-- 若明细里存有该食物真实单位快照，可按明细单位回填：
-- UPDATE calorie_foods f
-- JOIN (
--   SELECT name, unit FROM calorie_meal_items WHERE unit <> '' GROUP BY name, unit
-- ) m ON m.name = f.name
-- SET f.unit = m.unit
-- WHERE f.unit = '100克';
-- 注意：同名食物若有多种单位快照，上面按任意一行取值，执行前请先用
-- SELECT 检查 GROUP BY name, unit 的重复情况，必要时手工处理。
