-- ============================================================
-- 隔离测试库一次性授权（需 root 或具备 CREATE DATABASE + GRANT 的账号）
-- ============================================================
-- 为什么必须由管理员执行：应用账号 hunter@'%' 的实测权限是
--   GRANT USAGE ON *.* TO `hunter`@`%`
--   GRANT ALL PRIVILEGES ON `crypto`.* TO `hunter`@`%`
-- 即只有业务库 crypto 的操作权，没有 CREATE DATABASE，也无法带库名以外的
-- 连接（不带库名连接实测报 1045）。因此清表冒烟所需的独立 schema 只能
-- 由管理员建一次，之后所有读写冒烟都由脚本自动指向它。
--
-- 执行方式（任选其一）：
--   1) 宝塔面板 → 数据库 → phpMyAdmin → 用 root 登录 → SQL 窗口粘贴本文件
--   2) 服务器上：mysql -u root -p < 隔离测试库授权.sql
--
-- 本文件不接触 crypto 业务库：只新建一个空库并授权，不复制、不迁移、不读取
-- 业务数据；表结构随后由 data/make_test_schema.py 按 crypto/models.py 建出来。

CREATE DATABASE IF NOT EXISTS `crypto_test`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

-- 字符集/排序规则与业务库保持一致（实测 crypto = utf8mb4 / utf8mb4_general_ci），
-- 否则测试库上的 LIKE / 等值比较行为不能外推到线上。

GRANT ALL PRIVILEGES ON `crypto_test`.* TO 'hunter'@'%';
FLUSH PRIVILEGES;

-- ------------------------------------------------------------
-- 自检（执行完应看到下面两项预期结果）
-- ------------------------------------------------------------
-- 1) 能看到 crypto_test，且字符集/排序规则为 utf8mb4 / utf8mb4_general_ci
SELECT SCHEMA_NAME, DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME
FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME = 'crypto_test';

-- 2) 授权行里出现 `crypto_test`.*
SHOW GRANTS FOR 'hunter'@'%';

-- ------------------------------------------------------------
-- 回滚（不再需要测试库时执行；库名带 test 标记，不可能是业务库）
-- ------------------------------------------------------------
-- DROP DATABASE IF EXISTS `crypto_test`;
-- REVOKE ALL PRIVILEGES ON `crypto_test`.* FROM 'hunter'@'%';
-- FLUSH PRIVILEGES;
