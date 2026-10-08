# crypto 数据库全量备份与验证报告

- 源库：`hunter@49.51.136.88:3306/crypto` (MySQL 8.0.45)
- 备份 SQL 文件：`D:\python\cryptoTrade\data\crypto_backup.sql`
- 备份大小：699,210 bytes
- 备份 SHA256：`9a267367694438e874a610ad973c68dbd1141f2839203fa3f9cea91e5ed22d85`
- 本地验证用 SQLite：`D:\python\cryptoTrade\data\crypto_backup_verify.sqlite`
- 导入开始时间：2026-10-08 20:26:59 +0800
- 导入结束时间：2026-10-08 20:30:26 +0800

## 表级行数核对（源 COUNT(*) vs dump 解析 vs 本地 SQLite）

| # | 表名 | 源库行数 | dump 解析(近似) | 本地 SQLite 行数 | 一致 |
|---|------|---------|---------------|----------------|------|
| 1 | alert_log | 157 | 157 | 157 | OK |
| 2 | analysis_reminder_log | 0 | 0 | 0 | OK |
| 3 | balance_history | 86 | 86 | 86 | OK |
| 4 | calorie_config | 1 | 1 | 1 | OK |
| 5 | calorie_food_spot_photos | 0 | 0 | 0 | OK |
| 6 | calorie_food_spots | 0 | 0 | 0 | OK |
| 7 | calorie_foods | 154 | 154 | 154 | OK |
| 8 | calorie_meal_items | 15 | 15 | 15 | OK |
| 9 | calorie_records | 6 | 6 | 6 | OK |
| 10 | crypto_coins | 56 | 56 | 56 | OK |
| 11 | daily_checkins | 0 | 0 | 0 | OK |
| 12 | diary_entries | 0 | 0 | 0 | OK |
| 13 | diary_goals | 0 | 0 | 0 | OK |
| 14 | diary_settings | 1 | 1 | 1 | OK |
| 15 | expense_bill_occurrences | 0 | 0 | 0 | OK |
| 16 | expense_bill_templates | 0 | 0 | 0 | OK |
| 17 | expense_categories | 91 | 91 | 91 | OK |
| 18 | expense_change_logs | 17 | 17 | 17 | OK |
| 19 | expense_daily_checks | 0 | 0 | 0 | OK |
| 20 | expense_periods | 1 | 1 | 1 | OK |
| 21 | expense_record_tags | 9 | 9 | 9 | OK |
| 22 | expense_records | 9 | 9 | 9 | OK |
| 23 | expense_settings | 1 | 1 | 1 | OK |
| 24 | expense_tags | 19 | 19 | 19 | OK |
| 25 | instinct_corpus | 65 | 65 | 65 | OK |
| 26 | instinct_embeddings | 0 | 0 | 0 | OK |
| 27 | instinct_predictions | 0 | 0 | 0 | OK |
| 28 | instinct_wiki_rules | 1 | 1 | 1 | OK |
| 29 | journal_notes | 26 | 26 | 26 | OK |
| 30 | journal_tags | 7 | 7 | 7 | OK |
| 31 | kv_store | 44 | 44 | 44 | OK |
| 32 | manual_pause | 12 | 12 | 12 | OK |
| 33 | meta | 2 | 2 | 2 | OK |
| 34 | note_tags | 24 | 24 | 24 | OK |
| 35 | plan_cards | 20 | 20 | 20 | OK |
| 36 | plan_plans | 2 | 2 | 2 | OK |
| 37 | plan_slots | 2000 | 2000 | 2000 | OK |
| 38 | pos_algo | 0 | 0 | 0 | OK |
| 39 | pos_book | 44 | 44 | 44 | OK |
| 40 | pos_lev | 22 | 22 | 22 | OK |
| 41 | pos_slot | 88 | 88 | 88 | OK |
| 42 | reverse_guard | 0 | 0 | 0 | OK |
| 43 | star_market | 10 | 10 | 10 | OK |
| 44 | task_analysis_records | 0 | 0 | 0 | OK |
| 45 | tp_runtime_state | 0 | 0 | 0 | OK |
| 46 | trade_journal | 921 | 921 | 921 | OK |
| 47 | trader_directions | 22 | 22 | 22 | OK |
| 48 | trading_daily_snapshots | 0 | 0 | 0 | OK |

## 抽样查询（本地 SQLite 各表前 3 行）

### trade_journal

| id | ts | run_id | inst_id | bucket | direction | action | price | amount | ord_id | reason |
|---|---|---|---|---|---|---|---|---|---|---|
| 359 | 2026-07-29 14:24:32 | SMOKE | NEAR-USDT-SWAP | trend | long | open | 100.0 | 0.2 | O1 | signal |
| 360 | 2026-07-29 14:24:32 | SMOKE | NEAR-USDT-SWAP | trend | long | close | 110.0 | 0.2 | O2 | signal |
| 361 | 2026-07-29 14:24:32 | SMOKE | NEAR-USDT-SWAP | range | long | open | 96.5 | 0.3 | O1 | signal |

### plan_slots

| card_id | slot_index | filled | filled_at | has_record | content | duration_minutes | prediction | actual | hit | market_analysis | action_advice | account_balance | analysis_ids | analysis_hour | bypass_analysis | task_links |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| learn_1440374c | 0 | 0 |  | 0 |  | 0 |  |  | NULL |  |  |  |  |  | 0 | [] |
| learn_1440374c | 1 | 0 |  | 0 |  | 0 |  |  | NULL |  |  |  |  |  | 0 | [] |
| learn_1440374c | 2 | 0 |  | 0 |  | 0 |  |  | NULL |  |  |  |  |  | 0 | [] |

### balance_history

| account_key | ts | balance | source |
|---|---|---|---|
| main | 1786879636241 | 135.3709 | backfill |
| main | 1786966036241 | 142.4957 | backfill |
| main | 1787052436241 | 149.9954 | backfill |

### crypto_coins

| id | rank_no | symbol | inst_id | name_cn | h1_trend | h1_price | h1_time | h1_profit | h1_close | h1_macd | h1_dif | h1_adx | h1_atr | h1_sar | h1_sar_color | h4_trend | h4_price | h4_time | h4_profit | h4_close | h4_macd | h4_dif | h4_adx | h4_atr | h4_sar | h4_sar_color | d1_trend | d1_price | d1_time | d1_profit | d1_close | d1_macd | d1_dif | d1_adx | d1_atr | d1_sar | d1_sar_color | m15_trend | m15_price | m15_time | m15_profit | m15_close | m15_macd | m15_dif | m15_adx | m15_atr | m15_sar | m15_sar_color | h1_er | h4_er | d1_er | m15_er |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 32217 | 1 | BTC | BTC-USDT-SWAP | 比特币 | 上涨 | 84087.4 | 2026-10-07 15:00:00 | -21.2 | 82304.7 | 16.9069 | -376.2744 | 47.14 | 4.8938 | 82277.9 | green | 下跌 | 85221.3 | 2026-10-05 20:00:00 | 34.22 | 82304.7 | -591.6296 | -687.1843 | 30.69 | 9.1172 | 82277.9 | green | 下跌 | 83045.8 | 2026-09-29 00:00:00 | 8.92 | 82304.7 | -861.0058 | 1366.1196 | 42.56 | 26.0174 | 82185.7 | green | 下跌 | 82751.9 | 2026-10-08 18:15:00 | 5.4 | 82304.7 | -111.7114 | -128.7515 | 29.65 | 2.763 | 82304.0 | green | 0.2192 | 0.7534 | 0.3118 | 0.5942 |
| 32218 | 2 | ETH | ETH-USDT-SWAP | 以太坊 | 上涨 | 2568.2 | 2026-10-07 21:00:00 | -14.86 | 2530.04 | -0.1517 | -16.9003 | 51.51 | 6.1881 | 2526.17 | green | 下跌 | 2695.97 | 2026-10-05 20:00:00 | 61.78 | 2529.4 | -24.2095 | -41.3396 | 37.48 | 11.5971 | 2526.17 | green | 下跌 | 2677.02 | 2026-09-28 00:00:00 | 55.14 | 2529.4 | -52.6467 | 31.7738 | 37.81 | 32.6163 | 2526.17 | green | 下跌 | 2555.92 | 2026-10-08 18:15:00 | 10.35 | 2529.46 | -5.9921 | -8.2765 | 33.21 | 3.7067 | 2529.05 | green | 0.5144 | 0.7814 | 0.6784 | 0.8167 |
| 32219 | 3 | XRP | XRP-USDT-SWAP | 瑞波币 | 上涨 | 1.4247 | 2026-10-08 02:00:00 | -21.41 | 1.3942 | 0.0012 | -0.0137 | 53.05 | 8.3202 | 1.3929 | green | 下跌 | 1.492 | 2026-10-05 20:00:00 | 65.55 | 1.3942 | -0.0177 | -0.0265 | 32.09 | 15.5645 | 1.3929 | green | 下跌 | 1.5173 | 2026-09-29 00:00:00 | 81.13 | 1.3942 | -0.0337 | 0.0153 | 27.59 | 51.0687 | 1.3881 | green | 下跌 | 1.4064 | 2026-10-08 18:15:00 | 8.67 | 1.3942 | -0.0027 | -0.0037 | 28.67 | 4.3753 | 1.3935 | green | 0.124 | 0.8967 | 0.5097 | 0.7176 |

## 结论

- **全部 48 张业务表** 在源库、dump 文件、本地 SQLite 三方一致，导入无误。
- dump 解析行数使用 '),(' 粗略分割，仅在字段值恰好包含该子串时可能虚高；正式对账以「源 COUNT(*) == 本地 SQLite COUNT(*)」为准。
