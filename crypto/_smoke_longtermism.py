# -*- coding: utf-8 -*-
"""长期主义 v1.1 改造后端逻辑验证脚本（临时测试，不触碰真实数据）

安全等级：🔒 纯离线 —— 只跑 plan_routes 里的纯计算函数（_calc_* / _settle_card
不写库不写文件）。导入时必须走包名 `crypto.plan_routes`：该模块已迁进 crypto
包并改用相对导入（from .database import ...），再用旧的 spec_from_file_location
按顶层模块加载会直接撞上 ImportError。所验函数均为纯函数，无需再重定向数据文件。
"""
import sys
import os
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crypto.plan_routes as pr  # noqa: E402

failures = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)

# ---------- 1. 在场天数 ----------
slots = []
for i in range(5):
    slots.append({"filled": True, "filled_at": f"2026-08-0{i+1} 09:00", "record": None})
# 同一天两次打卡（跨两张卡合计去重场景）
slots.append({"filled": True, "filled_at": "2026-08-02 20:00", "record": None})
check("在场天数按日期去重", pr._calc_presence_days(slots) == 5,
      f"got={pr._calc_presence_days(slots)} (expected 5)")

# ---------- 2. 历史最佳连续在场天数（v1.1） ----------
all_slots = []
# 场景：8-01~8-04 连续 4 天，8-05 断，8-06 在场 → 最佳连续 = 4（断档不归零）
for d in ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]:
    all_slots.append([{"filled": True, "filled_at": f"{d} 10:00", "record": None}])
all_slots.append([{"filled": True, "filled_at": "2026-08-06 10:00", "record": None}])
best = pr._calc_best_streak_days(all_slots)
check("最佳连续在场（断档取历史最长段）", best == 4, f"got={best}")

# 场景：8-01,8-02 在场，8-03 断，8-04,8-05,8-06 在场 → 最佳 = 3
all_slots3 = []
for d in ["2026-08-01", "2026-08-02", "2026-08-04", "2026-08-05", "2026-08-06"]:
    all_slots3.append([{"filled": True, "filled_at": f"{d} 10:00", "record": None}])
check("最佳连续在场（重新开始计数）", pr._calc_best_streak_days(all_slots3) == 3,
      f"got={pr._calc_best_streak_days(all_slots3)}")

# 场景：今天（真实系统时间）在场
today = datetime.date.today().isoformat()
all_slots2 = [[{"filled": True, "filled_at": f"{today} 10:00", "record": None}]]
check("最佳连续在场（仅今天）", pr._calc_best_streak_days(all_slots2) == 1, f"got={pr._calc_best_streak_days(all_slots2)}")

# 无任何打卡 → 0
check("最佳连续在场（无记录为 0）", pr._calc_best_streak_days([]) == 0)

# ---------- 3. 任务树完成 / 提前通关资格（旧 milestones 口径已迁任务树 v2） ----------
card_learn = {
    "type": "learn", "reward": 2000, "status": "in_progress",
    "tasks": [{"id": "a", "title": "a", "status": "done", "children": []},
              {"id": "b", "title": "b", "status": "done", "children": []}],
    "slots": [{"filled": True, "filled_at": "2026-08-07 10:00", "record": {"duration_minutes": 30}}] * 1
}
check("任务树全部完成", pr._calc_tasks_all_done(card_learn))
card_learn["tasks"][1]["status"] = "doing"
check("任务树未全部完成", not pr._calc_tasks_all_done(card_learn))
card_learn["tasks"][1]["status"] = "done"
check("学习卡提前资格判定", pr._calc_early_eligible(card_learn))
card_trade = {"type": "trade", "base_reward": 2000, "hourly_rate": 20,
              "tasks": [], "slots": []}
check("交易卡无任务树时不具提前资格（与学习卡同口径）",
      not pr._calc_early_eligible(card_trade))

# ---------- 4. 提前奖励倍率（v1.1：默认 ×1，不翻倍） ----------
check("提前奖励倍率默认 1", pr._early_multiplier({}) == 1)
check("提前奖励倍率读 daily_rule", pr._early_multiplier({"daily_rule": {"early_bonus_multiplier": 1}}) == 1)
check("提前奖励倍率读 round_config", pr._early_multiplier({"round_config": {"early_bonus_multiplier": 1}}) == 1)

# ---------- 5. 学习卡奖励（无罚款，提前奖励 ×1） ----------
plan = {"daily_rule": {"mode": "longtermism"}}
card_learn["slots"] = [{"filled": True, "filled_at": "2026-08-07 10:00",
                        "record": {"duration_minutes": 60}}]
ri = pr._calc_card_reward(plan, card_learn)
check("学习卡提前奖励 = 99h×20×1（v1.1 不翻倍）", ri["early_bonus"] == 1980, f"got={ri['early_bonus']}")
check("学习卡最终奖励 = 2000+1980", ri["final_reward"] == 3980, f"got={ri['final_reward']}")
check("学习卡在场天数", ri["presence_days"] == 1, f"got={ri['presence_days']}")
check("奖励信息不含罚款字段", "total_penalty" not in ri)

# 任务树未完成 → 无提前奖励
card_learn["tasks"][1]["status"] = "doing"
ri2 = pr._calc_card_reward(plan, card_learn)
check("未达标不显示提前奖", ri2["early_bonus"] == 0 and ri2["final_reward"] == 2000,
      f"got early={ri2['early_bonus']} final={ri2['final_reward']}")
card_learn["tasks"][1]["status"] = "done"

# ---------- 6. 学习卡提前结算（settle） ----------
card_learn["slots"] = [{"filled": True, "filled_at": "2026-08-07 10:00",
                        "record": {"duration_minutes": 60}}]
pr._settle_card(plan, card_learn)
check("学习卡提前通关状态", card_learn["status"] == "completed", f"got={card_learn['status']}")
check("结算包含提前奖励", card_learn["settlement"]["early_bonus"] > 0,
      f"got={card_learn['settlement']['early_bonus']}")
check("结算无罚款字段", "penalty_total" not in card_learn["settlement"])

# ---------- 7. 交易卡结算（通关与学习卡同口径：满100格或任务树全部完成；
#            8% 增长线仅作每日达标评级与记录快照，不参与通关判定） ----------
card_trade["tasks"] = [{"id": "n1", "title": "A", "status": "done", "children": []}]
card_trade["slots"] = [
    {"filled": True, "filled_at": "2026-08-07 10:00",
     "record": {"duration_minutes": 60, "account_balance": "1000"}},
    {"filled": True, "filled_at": "2026-08-08 10:00",
     "record": {"duration_minutes": 60, "account_balance": "1100"}},
]
pr._settle_card(plan, card_trade)
check("交易卡任务树全完成 → 通关", card_trade["status"] == "completed", f"got={card_trade['status']}")
check("交易卡结算记录资金增长快照", card_trade["settlement"].get("growth_pct") == 10.0,
      f"got={card_trade['settlement'].get('growth_pct')}")
check("交易卡结算无罚款字段", "penalty_total" not in card_trade["settlement"])

# 亏钱（增长 -5%）但任务树全完成 → 仍通关（增长不再判死）
card_trade2 = {"type": "trade", "base_reward": 2000, "hourly_rate": 20, "status": "in_progress",
               "tasks": [{"id": "n1", "title": "A", "status": "done", "children": []}],
               "slots": [
                   {"filled": True, "filled_at": "2026-08-07 10:00",
                    "record": {"duration_minutes": 60, "account_balance": "1000"}},
                   {"filled": True, "filled_at": "2026-08-08 10:00",
                    "record": {"duration_minutes": 60, "account_balance": "950"}},
               ]}
pr._settle_card(plan, card_trade2)
check("交易卡亏钱但树全完成仍通关", card_trade2["status"] == "completed", f"got={card_trade2['status']}")
check("亏损增长仅作快照记录", card_trade2["settlement"]["growth_pct"] == -5.0,
      f"got={card_trade2['settlement']['growth_pct']}")

# 未满 100 格且任务树未完成 → 未通关，奖金全额熔断归零（即便增长 >8%）
card_trade3 = {"type": "trade", "base_reward": 2000, "hourly_rate": 20, "status": "in_progress",
               "tasks": [{"id": "n1", "title": "A", "status": "doing", "children": []}],
               "slots": [
                   {"filled": True, "filled_at": "2026-08-07 10:00",
                    "record": {"duration_minutes": 60, "account_balance": "1000"}},
                   {"filled": True, "filled_at": "2026-08-08 10:00",
                    "record": {"duration_minutes": 60, "account_balance": "1100"}},
               ]}
pr._settle_card(plan, card_trade3)
check("未满100格且树未完 → failed（增长>8%也不通关）", card_trade3["status"] == "failed", f"got={card_trade3['status']}")
check("交易卡未通关奖金归零（熔断）", card_trade3["settlement"]["final_reward"] == 0,
      f"got={card_trade3['settlement']['final_reward']}")

# ---------- 8. today-status 响应结构（无罚款字段 + 在场日历） ----------
from flask import Flask
app = Flask(__name__)
app.register_blueprint(pr.plan_bp)

with app.test_client() as client:
    resp = client.get('/plan/api/today-status')
    data = resp.get_json()
    if data and data.get("code") == 200 and data["data"]:
        t = data["data"][0]
        check("today-status 无罚款/达标字段",
              all(k not in t for k in ("learn_penalty", "trade_penalty", "learn_status", "trade_status")))
        check("today-status 含在场字段",
              "today_present" in t and "learn_presence_days" in t and "welcome" in t)
        cal = t.get("presence_calendar") or {}
        days = cal.get("days") or []
        check("today-status 含在场日历", "presence_calendar" in t and len(days) == 15 * 7,
              f"days={len(days)}")
        check("在场日历结构合法",
              all("date" in d and "minutes" in d and 0 <= d["level"] <= 4 for d in days))
    else:
        check("today-status 响应", False, str(data))

# ---------- 9. list 统计结构（在场天数 + 在场日历） ----------
with app.test_client() as client:
    resp = client.get('/plan/api/list')
    data = resp.get_json()
    if data and data.get("code") == 200 and data["data"]:
        s = data["data"][0]["stats"]
        check("list stats 含在场统计",
              "total_presence_days" in s and "best_streak_days" in s and "streak_days" not in s and "total_penalty" not in s)
        check("list stats 含在场日历",
              "presence_calendar" in s and len((s.get("presence_calendar") or {}).get("days") or []) == 15 * 7)
        # 卡片 reward_info 结构验证
        ri = data["data"][0]["stats"]["cards"][0]["reward_info"]
        check("card reward_info 无罚款字段",
              "total_penalty" not in ri and "presence_days" in ri and "early_eligible" in ri)
    else:
        check("list 响应", False, str(data))

# ---------- 10. 在场日历构建（level 分级） ----------
check("日历 level 分级 0", pr._calendar_level(0) == 0)
check("日历 level 分级 1", pr._calendar_level(15) == 1)
check("日历 level 分级 2", pr._calendar_level(45) == 2)
check("日历 level 分级 3", pr._calendar_level(90) == 3)
check("日历 level 分级 4", pr._calendar_level(150) == 4)
cal_all = pr._build_presence_calendar([
    {"filled": True, "filled_at": "2026-08-07 10:00", "record": {"duration_minutes": 90}}
])
check("日历 15 周 105 天", len(cal_all["days"]) == 105, f"got={len(cal_all['days'])}")
day_map = {d["date"]: d for d in cal_all["days"]}
if "2026-08-07" in day_map:
    check("日历记录当天在场时长", day_map["2026-08-07"]["minutes"] == 90 and day_map["2026-08-07"]["level"] == 3)

print()
if failures:
    print(f"FAILED: {len(failures)} 项未通过 -> {failures}")
    sys.exit(1)
print("ALL TESTS PASSED (longtermism backend logic verified)")
