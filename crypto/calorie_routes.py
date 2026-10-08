#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热量缺口管理系统 - Flask 蓝图
=============================
所有 /calorie 和 /calorie/api/* 路由
包括：每日记录管理、食物热量库管理、计算引擎、统计看板

【存储】MySQL（迁移批次2）：数据访问统一走 calorie_repo，
连接配置见 database.py。对外 API 契约与 JSON 文件版完全一致。
"""

import datetime
import logging
import math
import os
import re

from flask import Blueprint, jsonify, request, render_template, send_from_directory

from .database import session_scope
from .data_paths import resolve_data_dir
from . import calorie_repo as repo

logger = logging.getLogger(__name__)

calorie_bp = Blueprint('calorie_bp', __name__)

# =============================================================================
# 美食打卡点：照片存储常量
# -----------------------------------------------------------------------------
# 照片文件落盘 data/food_spot_photos/（跟随 CRYPTO_PLAN_DATA_DIR 外置目录口径），
# 数据库只存服务端重命名后的文件名索引；经 /calorie/api/food-spot/photo/<id>
# 回显（send_from_directory 防路径穿越，且自动处于 web_auth 闸门覆盖内）。
# =============================================================================
_SPOT_PHOTO_SUBDIR = 'food_spot_photos'
_SPOT_PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
_SPOT_PHOTO_MAX_SIZE = 8 * 1024 * 1024      # 单张 ≤ 8MB
_SPOT_PHOTO_MAX_COUNT = 9                    # 单次上传最多张数
_SPOT_PHOTO_NAME_RE = re.compile(r'^[a-z0-9_]+\.[a-z0-9]{3,4}$')


def _spot_photo_dir() -> str:
    """解析照片目录绝对路径并确保存在；创建失败时返回空串由调用方降级处理"""
    base = resolve_data_dir()
    if not base:
        return ''
    path = os.path.join(base, _SPOT_PHOTO_SUBDIR)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.warning('[FoodSpot] 照片目录创建失败(%s): %s', path, e)
        return ''
    return path


def _save_spot_photo_files(files, spot_id):
    """把上传的图片逐个落盘并登记索引，返回 (照片dict列表, 错误信息)。

    仅接受白名单扩展名；文件名由服务端生成（fsp_xxx.ext，绝不采用原始文件名），
    从源头杜绝路径穿越与同名覆盖。单个文件校验失败跳过不整体报错，
    全部无效才返回错误。
    """
    photo_dir = _spot_photo_dir()
    if not photo_dir:
        return [], '照片存储目录不可用'
    saved, errors = [], []
    position = 0
    with session_scope() as session:
        existing = repo.get_spot(session, spot_id) or {'photos': []}
        position = len(existing.get('photos') or [])
    for f in list(files)[:_SPOT_PHOTO_MAX_COUNT]:
        original = (f.filename or '').strip()
        ext = os.path.splitext(original)[1].lower()
        if ext not in _SPOT_PHOTO_EXTS:
            errors.append(f'不支持的图片格式: {original or "(未命名)"}')
            continue
        photo_id = repo.next_photo_id()
        filename = f'{photo_id}{ext}'
        abs_path = os.path.join(photo_dir, filename)
        try:
            f.save(abs_path)
            size = os.path.getsize(abs_path)
        except OSError:
            errors.append(f'照片保存失败: {original}')
            continue
        if size > _SPOT_PHOTO_MAX_SIZE:
            _try_remove_photo(abs_path)
            errors.append(f'单张照片超过 8MB 上限: {original}')
            continue
        with session_scope() as session:
            saved.append(repo.add_spot_photo(session, {
                'id': photo_id, 'spot_id': spot_id, 'position': position,
                'filename': filename, 'original_name': original,
                'file_size': size}))
        position += 1
    if not saved and errors:
        return [], '；'.join(errors)
    return saved, ''


def _try_remove_photo(abs_path):
    """尽力删除磁盘照片文件（事务提交后调用）；失败仅记日志，不影响 API 结果"""
    try:
        if abs_path and os.path.isfile(abs_path):
            os.remove(abs_path)
    except OSError as e:
        logger.warning('[FoodSpot] 照片文件清理失败(%s): %s', abs_path, e)


def _remove_spot_photo_by_filename(filename):
    """按库中记录的磁盘文件名删除照片文件；非白名单格式直接拒绝（防穿越）"""
    if not filename or not _SPOT_PHOTO_NAME_RE.match(filename):
        return
    photo_dir = _spot_photo_dir()
    if photo_dir:
        _try_remove_photo(os.path.join(photo_dir, filename))

# =============================================================================
# 数据库初始化已收敛到 database.init_db() 进程内单例，由应用启动时后台
# 预热（warmup_async）完成，蓝图导入期不再同步建表，避免阻塞启动。
# =============================================================================

# =============================================================================
# 默认配置参数
# =============================================================================
_DEFAULT_CONFIG = {
    'height': 169,           # 身高（cm）
    'age': 29,               # 年龄
    'step_frequency': 0.7,   # 步频系数
    'weight_factor': 55,     # 体重系数（kg）
    'target_deficit': 100000  # 总目标缺口（kcal），约13kg脂肪
}

# =============================================================================
# 预置食物数据
# =============================================================================
_DEFAULT_FOODS = [
    {'name': '白米饭', 'unit': '100克', 'calories': 116, 'category': '主食'},
    {'name': '馒头', 'unit': '1个', 'calories': 223, 'category': '主食'},
    {'name': '面条(煮)', 'unit': '100克', 'calories': 110, 'category': '主食'},
    {'name': '全麦面包', 'unit': '1片', 'calories': 78, 'category': '主食'},
    {'name': '燕麦片', 'unit': '100克', 'calories': 367, 'category': '主食'},
    {'name': '红薯', 'unit': '100克', 'calories': 86, 'category': '主食'},
    {'name': '玉米', 'unit': '1根', 'calories': 132, 'category': '主食'},
    {'name': '鸡蛋(煮)', 'unit': '1个', 'calories': 71, 'category': '肉蛋'},
    {'name': '鸡胸肉', 'unit': '100克', 'calories': 133, 'category': '肉蛋'},
    {'name': '瘦牛肉', 'unit': '100克', 'calories': 125, 'category': '肉蛋'},
    {'name': '瘦猪肉', 'unit': '100克', 'calories': 143, 'category': '肉蛋'},
    {'name': '三文鱼', 'unit': '100克', 'calories': 208, 'category': '肉蛋'},
    {'name': '虾仁', 'unit': '100克', 'calories': 93, 'category': '肉蛋'},
    {'name': '豆腐', 'unit': '100克', 'calories': 81, 'category': '肉蛋'},
    {'name': '西兰花', 'unit': '100克', 'calories': 34, 'category': '蔬菜'},
    {'name': '菠菜', 'unit': '100克', 'calories': 23, 'category': '蔬菜'},
    {'name': '番茄', 'unit': '1个', 'calories': 22, 'category': '蔬菜'},
    {'name': '黄瓜', 'unit': '1根', 'calories': 16, 'category': '蔬菜'},
    {'name': '胡萝卜', 'unit': '100克', 'calories': 41, 'category': '蔬菜'},
    {'name': '生菜', 'unit': '100克', 'calories': 15, 'category': '蔬菜'},
    {'name': '白菜', 'unit': '100克', 'calories': 13, 'category': '蔬菜'},
    {'name': '苹果', 'unit': '1个', 'calories': 95, 'category': '水果'},
    {'name': '香蕉', 'unit': '1根', 'calories': 105, 'category': '水果'},
    {'name': '橙子', 'unit': '1个', 'calories': 62, 'category': '水果'},
    {'name': '葡萄', 'unit': '100克', 'calories': 69, 'category': '水果'},
    {'name': '蓝莓', 'unit': '100克', 'calories': 57, 'category': '水果'},
    {'name': '牛奶(全脂)', 'unit': '250毫升', 'calories': 161, 'category': '乳制品'},
    {'name': '酸奶(原味)', 'unit': '100克', 'calories': 72, 'category': '乳制品'},
    {'name': '奶酪', 'unit': '10克', 'calories': 35, 'category': '乳制品'},
    {'name': '核桃', 'unit': '10克', 'calories': 65, 'category': '零食'},
    {'name': '杏仁', 'unit': '10克', 'calories': 58, 'category': '零食'},
    {'name': '花生', 'unit': '10克', 'calories': 57, 'category': '零食'},
    {'name': '黑巧克力', 'unit': '10克', 'calories': 55, 'category': '零食'},
    {'name': '橄榄油', 'unit': '10克', 'calories': 88, 'category': '饮品'},
    {'name': '蜂蜜', 'unit': '10克', 'calories': 30, 'category': '饮品'},
    {'name': '黑咖啡', 'unit': '1杯', 'calories': 5, 'category': '饮品'},
    {'name': '矿泉水', 'unit': '500毫升', 'calories': 0, 'category': '饮品'},
    {'name': '老乡鸡米饭', 'unit': '200克', 'calories': 338, 'category': '主食'},
    {'name': '桃酥', 'unit': '30克', 'calories': 168, 'category': '主食'},
    {'name': '粥料', 'unit': '100克', 'calories': 350, 'category': '主食'},
    {'name': '水饺(袋)', 'unit': '22个', 'calories': 867, 'category': '主食'},
    {'name': '挂面', 'unit': '100克', 'calories': 354, 'category': '主食'},
    {'name': '生米', 'unit': '100克', 'calories': 336, 'category': '主食'},
    {'name': '手擀面', 'unit': '100克', 'calories': 240, 'category': '主食'},
    {'name': '玉米(100克)', 'unit': '100克', 'calories': 100, 'category': '主食'},
    {'name': '饭团', 'unit': '100克', 'calories': 134, 'category': '主食'},
    {'name': '枣糕', 'unit': '100克', 'calories': 350, 'category': '主食'},
    {'name': '燕麦麸皮', 'unit': '28克', 'calories': 100, 'category': '主食'},
    {'name': '魔芋面', 'unit': '100克', 'calories': 249, 'category': '主食'},
    {'name': '魔芋凉皮', 'unit': '100克', 'calories': 176, 'category': '主食'},
    {'name': '魔芋爆肚', 'unit': '100克', 'calories': 211, 'category': '主食'},
    {'name': '魔芋面(即食)', 'unit': '1份', 'calories': 12, 'category': '主食'},
    {'name': '牛肉板面', 'unit': '1份', 'calories': 710, 'category': '主食'},
    {'name': '香辣鸡杂面', 'unit': '1份', 'calories': 683, 'category': '主食'},
    {'name': '香辣牛肉面', 'unit': '1份', 'calories': 450, 'category': '主食'},
    {'name': '老鸡扬米面', 'unit': '1份', 'calories': 468, 'category': '主食'},
    {'name': '香菇鸡汤面', 'unit': '1份', 'calories': 420, 'category': '主食'},
    {'name': '绿豆饼', 'unit': '100克', 'calories': 200, 'category': '主食'},
    {'name': '绿豆饼(30克)', 'unit': '30克', 'calories': 95, 'category': '主食'},
    {'name': '泡芙', 'unit': '100克', 'calories': 200, 'category': '主食'},
    {'name': '海苔小贝', 'unit': '100克', 'calories': 300, 'category': '主食'},
    {'name': '肥叔锅贴', 'unit': '15个', 'calories': 402, 'category': '主食'},
    {'name': '西红柿炒蛋', 'unit': '100克', 'calories': 163, 'category': '肉蛋'},
    {'name': '梅菜扣肉', 'unit': '100克', 'calories': 455, 'category': '肉蛋'},
    {'name': '毛豆烧鸡', 'unit': '100克', 'calories': 463, 'category': '肉蛋'},
    {'name': '香辣鸡杂', 'unit': '100克', 'calories': 350, 'category': '肉蛋'},
    {'name': '鸡汁辣鱼', 'unit': '100克', 'calories': 272, 'category': '肉蛋'},
    {'name': '葱油菜苔', 'unit': '100克', 'calories': 141, 'category': '肉蛋'},
    {'name': '老母鸡汤', 'unit': '100克', 'calories': 175, 'category': '肉蛋'},
    {'name': '葱油鸡', 'unit': '100克', 'calories': 366, 'category': '肉蛋'},
    {'name': '板栗烧鸡', 'unit': '100克', 'calories': 400, 'category': '肉蛋'},
    {'name': '酸菜鱼', 'unit': '100克', 'calories': 270, 'category': '肉蛋'},
    {'name': '小炒肉', 'unit': '100克', 'calories': 354, 'category': '肉蛋'},
    {'name': '梅干菜凤爪翅', 'unit': '100克', 'calories': 416, 'category': '肉蛋'},
    {'name': '蒜蓉粉丝虾', 'unit': '100克', 'calories': 92, 'category': '肉蛋'},
    {'name': '菠萝咕咾肉', 'unit': '100克', 'calories': 456, 'category': '肉蛋'},
    {'name': '番茄菌菇肉丸', 'unit': '100克', 'calories': 188, 'category': '肉蛋'},
    {'name': '竹笋蒸鸡翅', 'unit': '100克', 'calories': 213, 'category': '肉蛋'},
    {'name': '乌鸡', 'unit': '100克', 'calories': 150, 'category': '肉蛋'},
    {'name': '卤肉肠', 'unit': '1份', 'calories': 210, 'category': '肉蛋'},
    {'name': '牛肉馅饼', 'unit': '1份', 'calories': 298, 'category': '肉蛋'},
    {'name': '鸡排', 'unit': '100克', 'calories': 200, 'category': '肉蛋'},
    {'name': '卤牛肉', 'unit': '100克', 'calories': 150, 'category': '肉蛋'},
    {'name': '卤鸭肫', 'unit': '100克', 'calories': 50, 'category': '肉蛋'},
    {'name': '鸡胸肉炒西蓝花', 'unit': '100克', 'calories': 180, 'category': '肉蛋'},
    {'name': '牛肉串', 'unit': '5串', 'calories': 119, 'category': '肉蛋'},
    {'name': '羊肉串', 'unit': '5串', 'calories': 297, 'category': '肉蛋'},
    {'name': '鱼香肉丝', 'unit': '100克', 'calories': 292, 'category': '肉蛋'},
    {'name': '莴笋丝炒蛋', 'unit': '100克', 'calories': 259, 'category': '肉蛋'},
    {'name': '白菜豆腐', 'unit': '100克', 'calories': 100, 'category': '蔬菜'},
    {'name': '小炒脆笋', 'unit': '100克', 'calories': 252, 'category': '蔬菜'},
    {'name': '香菇', 'unit': '100克', 'calories': 28, 'category': '蔬菜'},
    {'name': '茄子', 'unit': '100克', 'calories': 25, 'category': '蔬菜'},
    {'name': '土豆', 'unit': '100克', 'calories': 77, 'category': '蔬菜'},
    {'name': '莲藕', 'unit': '100克', 'calories': 70, 'category': '蔬菜'},
    {'name': '青菜', 'unit': '100克', 'calories': 14, 'category': '蔬菜'},
    {'name': '西兰花(炒)', 'unit': '100克', 'calories': 27, 'category': '蔬菜'},
    {'name': '香椿', 'unit': '100克', 'calories': 50, 'category': '蔬菜'},
    {'name': '豆腐(炒)', 'unit': '100克', 'calories': 87, 'category': '蔬菜'},
    {'name': '大蒜', 'unit': '100克', 'calories': 138, 'category': '蔬菜'},
    {'name': '草莓', 'unit': '100克', 'calories': 96, 'category': '水果'},
    {'name': '香蕉(100克)', 'unit': '100克', 'calories': 93, 'category': '水果'},
    {'name': '橘子', 'unit': '100克', 'calories': 44, 'category': '水果'},
    {'name': '西瓜', 'unit': '100克', 'calories': 28, 'category': '水果'},
    {'name': '特仑苏纯牛奶', 'unit': '250毫升', 'calories': 184, 'category': '乳制品'},
    {'name': '脱脂奶粉', 'unit': '25克', 'calories': 93, 'category': '乳制品'},
    {'name': '腰果', 'unit': '20克', 'calories': 116, 'category': '零食'},
    {'name': '带壳瓜子', 'unit': '10克', 'calories': 30, 'category': '零食'},
    {'name': '可可粉', 'unit': '5克', 'calories': 17, 'category': '零食'},
    {'name': '花生(颗)', 'unit': '10颗', 'calories': 60, 'category': '零食'},
    {'name': '米露', 'unit': '1杯', 'calories': 50, 'category': '零食'},
    {'name': '老干妈(沥油)', 'unit': '10克', 'calories': 52, 'category': '零食'},
    {'name': '拌饭酱', 'unit': '10克', 'calories': 48, 'category': '零食'},
    {'name': '油壶喷雾', 'unit': '10下', 'calories': 18, 'category': '零食'},
    {'name': '洋车前子壳', 'unit': '6克', 'calories': 10, 'category': '零食'},
]

# 预置食物按名称索引：新食物自动入库时，请求未带热量可按种子默认值兜底
_DEFAULT_FOOD_MAP = {f['name']: f for f in _DEFAULT_FOODS}

# =============================================================================
# 计算引擎
# =============================================================================

class CalorieCalculator:
    """热量缺口计算引擎"""

    def __init__(self, config=None):
        cfg = config or _DEFAULT_CONFIG
        self.height = cfg.get('height', 175)
        self.age = cfg.get('age', 29)
        self.step_frequency = cfg.get('step_frequency', 0.7)
        self.weight_factor = cfg.get('weight_factor', 55)
        self.target_deficit = cfg.get('target_deficit', 100000)

    def calc_bmr(self, morning_weight):
        """计算基础代谢（Mifflin-St Jeor 男性版）"""
        if not morning_weight or morning_weight <= 0:
            return 0
        return round((10 * morning_weight / 2) + (6.25 * self.height) - (5 * self.age) + 5, 2)

    def calc_intake_deficit(self, bmr, breakfast, lunch, dinner):
        """计算摄入缺口 = 基础代谢 - 三餐总摄入"""
        total_intake = (breakfast or 0) + (lunch or 0) + (dinner or 0)
        return round(bmr - total_intake, 2)

    def calc_exercise_calories(self, daily_steps):
        """计算运动消耗 = 步数 × 0.7 × 55 / 1000"""
        if not daily_steps or daily_steps <= 0:
            return 0.0
        return round(daily_steps * self.step_frequency * self.weight_factor / 1000, 2)

    def calc_calorie_deficit(self, intake_deficit, exercise_calories):
        """计算热量缺口 = 摄入缺口 + 运动消耗"""
        return round((intake_deficit or 0) + (exercise_calories or 0), 2)

    def calc_total_intake(self, breakfast, lunch, dinner):
        """计算三餐总摄入"""
        return round((breakfast or 0) + (lunch or 0) + (dinner or 0), 2)

    def calc_completion_estimate(self, cumulative_deficit, avg_calorie_deficit):
        """推算目标达成日期：剩余缺口按历史日均缺口匀速补齐，天数向上取整后从今天顺延。

        口径：剩余缺口 = target_deficit - 已累积缺口（不足 0 按 0 计，即已达成）；
        日均缺口取全部历史记录的 calorie_deficit 均值。日均 ≤0（无记录或整体净盈余）
        时不具备推算条件，天数与日期返回 None 由前端显示占位文案。
        """
        target = float(self.target_deficit or 0)
        remaining = round(max(target - float(cumulative_deficit or 0), 0.0), 2)
        estimate = {'remaining_deficit': remaining}
        if remaining <= 0:
            estimate['est_days_remaining'] = 0
            estimate['est_completion_date'] = datetime.date.today().isoformat()
            return estimate
        avg = float(avg_calorie_deficit or 0)
        if avg <= 0:
            estimate['est_days_remaining'] = None
            estimate['est_completion_date'] = None
            return estimate
        # 减一个浮点容差，避免商恰为整数时被进位误差多算一天
        days = math.ceil(remaining / avg - 1e-9)
        estimate['est_days_remaining'] = days
        estimate['est_completion_date'] = (
            datetime.date.today() + datetime.timedelta(days=days)).isoformat()
        return estimate

    def calc_dashboard(self, records):
        """计算统计看板数据"""
        if not records:
            dashboard = {
                'total_calorie_deficit': 0.0,
                'cumulative_deficit': 0.0,
                'current_progress': 0.0,
                'record_count': 0,
                'avg_calorie_deficit': 0.0,
                'avg_total_intake': 0.0,
                'avg_intake_deficit': 0.0,
                'avg_daily_steps': 0,
                'avg_exercise_calories': 0.0
            }
            # 空库无可推算依据：剩余缺口即总目标，天数/日期为 None
            dashboard.update(self.calc_completion_estimate(0.0, 0.0))
            return dashboard

        total_deficit = sum(r.get('calorie_deficit', 0) or 0 for r in records)
        total_intake = sum(
            (r.get('breakfast_calories', 0) or 0) +
            (r.get('lunch_calories', 0) or 0) +
            (r.get('dinner_calories', 0) or 0)
            for r in records
        )
        total_intake_deficit = sum(r.get('intake_deficit', 0) or 0 for r in records)
        total_steps = sum(r.get('daily_steps', 0) or 0 for r in records)
        total_exercise = sum(r.get('exercise_calories', 0) or 0 for r in records)
        count = len(records)

        # 累积缺口：所有记录的热量缺口之和
        cumulative_deficit = total_deficit

        # 当前进度
        progress = round(cumulative_deficit / self.target_deficit * 100, 2) if self.target_deficit > 0 else 0.0

        # 脂肪消耗总量（每3850大卡对应1斤脂肪）
        total_fat_loss = round(total_deficit / 3850, 4)

        return {
            'total_calorie_deficit': round(total_deficit, 2),
            'cumulative_deficit': round(cumulative_deficit, 2),
            'current_progress': progress,
            'record_count': count,
            'total_fat_loss': total_fat_loss,
            'avg_calorie_deficit': round(total_deficit / count, 2) if count else 0.0,
            'avg_total_intake': round(total_intake / count, 2) if count else 0.0,
            'avg_intake_deficit': round(total_intake_deficit / count, 2) if count else 0.0,
            'avg_daily_steps': round(total_steps / count) if count else 0,
            'avg_exercise_calories': round(total_exercise / count, 2) if count else 0.0,
            **self.calc_completion_estimate(cumulative_deficit, total_deficit / count)
        }

# =============================================================================
# 数据操作层
# =============================================================================

def _ensure_food_seed(session):
    """食物库空表时补种子预置数据（对齐 JSON 版 _init_food_db 的兜底行为）"""
    foods = repo.load_foods(session)
    if foods:
        return foods
    repo.lock_config(session)
    foods = repo.load_foods(session, for_update=True)
    if foods:
        return foods
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for i, item in enumerate(_DEFAULT_FOODS, 1):
        repo.add_food(session, {
            'id': f'food_{i:03d}',
            'name': item['name'],
            'unit': item['unit'],
            'calories': item['calories'],
            'category': item['category'],
            'created_at': now
        }, flush=False)
    session.flush()
    return repo.load_foods(session, for_update=True)


def _parse_food_names(food_str):
    """解析逗号分隔的食物名称"""
    if not food_str or not food_str.strip():
        return []
    return [f.strip() for f in food_str.split(',') if f.strip()]


def _meal_item_total(item):
    """单条明细的摄入量 = 单位热量 × 数量。

    优先采用前端传来的 total_calories；quantity 缺失/非法时按 1 处理，
    兼容旧契约（calories 即该行绝对热量）。
    """
    total = item.get('total_calories')
    if total is not None:
        try:
            return round(float(total), 2)
        except (ValueError, TypeError):
            pass
    try:
        unit_cal = float(item.get('calories', 0) or 0)
    except (ValueError, TypeError):
        unit_cal = 0.0
    try:
        qty = float(item.get('quantity', 1) or 1)
    except (ValueError, TypeError):
        qty = 1.0
    if qty <= 0:
        qty = 1.0
    return round(unit_cal * qty, 2)


def _normalize_meal_foods(foods):
    """规整明细数组：补齐 quantity/unit/total_calories，保证存储结构与 API 回显一致"""
    normalized = []
    for item in foods:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        try:
            qty = float(entry.get('quantity', 1) or 1)
        except (ValueError, TypeError):
            qty = 1.0
        if qty <= 0:
            qty = 1.0
        entry['quantity'] = round(qty, 3)
        entry['unit'] = str(entry.get('unit', '') or '')[:32]
        entry['total_calories'] = _meal_item_total(entry)
        normalized.append(entry)
    return normalized


def _collect_food_registration_info(meal_foods, legacy_pairs):
    """收集记录内各食物名的入库信息（单位热量/单位快照）。

    优先级：新格式明细（calories 即单位热量）> 旧格式字符串单食物时
    按该餐总热量计 > 热量缺失时按预置种子同名默认值补齐。
    保证自动入库的新食物不再恒为 0 热量（入库为 0 会反过来导致
    后续搜索建议选中时热量回填为 0）。
    """
    info = {}
    for foods in meal_foods:
        for f in foods or []:
            name = str(f.get('name', '') or '').strip()
            if not name or name in info:
                continue
            try:
                cal = float(f.get('calories', 0) or 0)
            except (ValueError, TypeError):
                cal = 0.0
            info[name] = {'calories': cal, 'unit': str(f.get('unit', '') or '').strip()[:32]}
    for food_str, meal_cal in legacy_pairs:
        names = _parse_food_names(food_str)
        # 旧格式仅单食物时能把该餐热量归属到食物；多食物逗号串无法拆分，不归属
        if len(names) == 1 and names[0] not in info:
            try:
                cal = float(meal_cal or 0)
            except (ValueError, TypeError):
                cal = 0.0
            info[names[0]] = {'calories': cal, 'unit': ''}
    for name, entry in info.items():
        if entry['calories'] <= 0:
            seed = _DEFAULT_FOOD_MAP.get(name)
            if seed:
                entry['calories'] = float(seed['calories'])
                entry['unit'] = entry['unit'] or str(seed['unit'])
    return info


def _recalc_cumulative(records):
    """重新计算所有记录的累积热量缺口"""
    cumulative = 0.0
    # 按日期排序
    sorted_records = sorted(records, key=lambda r: r.get('date', ''))
    for record in sorted_records:
        deficit = record.get('calorie_deficit', 0) or 0
        cumulative += deficit
        record['cumulative_deficit'] = round(cumulative, 2)
    return sorted_records


# =============================================================================
# 页面路由
# =============================================================================

@calorie_bp.route('/calorie')
def calorie_page():
    """渲染热量缺口管理页面"""
    return render_template('calorie.html')


# =============================================================================
# API 路由 - 每日记录
# =============================================================================

@calorie_bp.route('/calorie/api/records', methods=['GET'])
def api_get_records():
    """获取所有记录 + 统计看板"""
    with session_scope() as session:
        config = repo.load_config(session)
        records = repo.load_records(session)

    # 按日期降序排列
    records_sorted = sorted(records, key=lambda r: r.get('date', ''), reverse=True)

    calculator = CalorieCalculator(config)
    dashboard = calculator.calc_dashboard(records)

    return jsonify({
        'success': True,
        'records': records_sorted,
        'dashboard': dashboard,
        'config': config
    })


@calorie_bp.route('/calorie/api/record', methods=['POST'])
def api_save_record():
    """保存或更新一条每日记录"""
    try:
        req = request.get_json(force=True)
    except Exception:
        return jsonify({'success': False, 'error': '无效的请求数据'}), 400

    date = req.get('date', '').strip()
    if not date:
        return jsonify({'success': False, 'error': '日期不能为空'}), 400

    morning_weight = req.get('morning_weight')
    evening_weight = req.get('evening_weight')
    daily_steps = req.get('daily_steps', 0)

    # 处理多食物数组（支持新格式：foods 数组，明细带 quantity/unit/total_calories；
    # 也兼容旧格式：无 quantity 的明细按 quantity=1 计）
    breakfast_foods = req.get('breakfast_foods', [])
    lunch_foods = req.get('lunch_foods', [])
    dinner_foods = req.get('dinner_foods', [])

    if breakfast_foods and isinstance(breakfast_foods, list):
        breakfast_foods = _normalize_meal_foods(breakfast_foods)
        breakfast_calories = round(sum(_meal_item_total(f) for f in breakfast_foods), 2)
        breakfast_food = ', '.join(f.get('name', '') for f in breakfast_foods if f.get('name'))
    else:
        breakfast_foods = []
        breakfast_food = req.get('breakfast_food', '').strip()
        breakfast_calories = req.get('breakfast_calories', 0)

    if lunch_foods and isinstance(lunch_foods, list):
        lunch_foods = _normalize_meal_foods(lunch_foods)
        lunch_calories = round(sum(_meal_item_total(f) for f in lunch_foods), 2)
        lunch_food = ', '.join(f.get('name', '') for f in lunch_foods if f.get('name'))
    else:
        lunch_foods = []
        lunch_food = req.get('lunch_food', '').strip()
        lunch_calories = req.get('lunch_calories', 0)

    if dinner_foods and isinstance(dinner_foods, list):
        dinner_foods = _normalize_meal_foods(dinner_foods)
        dinner_calories = round(sum(_meal_item_total(f) for f in dinner_foods), 2)
        dinner_food = ', '.join(f.get('name', '') for f in dinner_foods if f.get('name'))
    else:
        dinner_foods = []
        dinner_food = req.get('dinner_food', '').strip()
        dinner_calories = req.get('dinner_calories', 0)

    # 类型转换与验证
    try:
        morning_weight = float(morning_weight) if morning_weight is not None and morning_weight != '' else None
    except (ValueError, TypeError):
        morning_weight = None

    try:
        evening_weight = float(evening_weight) if evening_weight is not None and evening_weight != '' else None
    except (ValueError, TypeError):
        evening_weight = None

    try:
        breakfast_calories = float(breakfast_calories) if breakfast_calories else 0
    except (ValueError, TypeError):
        breakfast_calories = 0

    try:
        lunch_calories = float(lunch_calories) if lunch_calories else 0
    except (ValueError, TypeError):
        lunch_calories = 0

    try:
        dinner_calories = float(dinner_calories) if dinner_calories else 0
    except (ValueError, TypeError):
        dinner_calories = 0

    try:
        daily_steps = int(daily_steps) if daily_steps else 0
    except (ValueError, TypeError):
        daily_steps = 0

    # 先锁定累计值写入作用域，再读取配置；避免并发补录互相覆盖累计值。
    with session_scope() as session:
        config = repo.lock_config(session)

        # 计算各项指标
        calculator = CalorieCalculator(config)
        bmr = calculator.calc_bmr(morning_weight)
        intake_deficit = calculator.calc_intake_deficit(bmr, breakfast_calories, lunch_calories, dinner_calories)
        exercise_calories = calculator.calc_exercise_calories(daily_steps)
        calorie_deficit = calculator.calc_calorie_deficit(intake_deficit, exercise_calories)

        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        record = {
            'id': date,
            'date': date,
            'morning_weight': morning_weight,
            'evening_weight': evening_weight,
            'bmr': bmr,
            'breakfast_foods': breakfast_foods,
            'breakfast_food': breakfast_food,
            'breakfast_calories': breakfast_calories,
            'lunch_foods': lunch_foods,
            'lunch_food': lunch_food,
            'lunch_calories': lunch_calories,
            'dinner_foods': dinner_foods,
            'dinner_food': dinner_food,
            'dinner_calories': dinner_calories,
            'intake_deficit': intake_deficit,
            'daily_steps': daily_steps,
            'exercise_calories': exercise_calories,
            'calorie_deficit': calorie_deficit,
            'cumulative_deficit': 0.0,
            'created_at': now,
            'updated_at': now
        }

        # upsert_record 已保留 created_at，无需提前再读一遍主记录和三餐。
        repo.upsert_record(session, record)

        before = repo.load_record_metrics(session)
        records = _recalc_cumulative([dict(r) for r in before])
        repo.bulk_update_records(session, repo.changed_metrics(before, records))

        # 处理食物自动入库（new_foods 仅返回本次新建的，对齐 JSON 版契约）；
        # 入库信息以请求明细的单位热量/单位快照为准，热量缺失时取预置种子默认值
        reg_info = _collect_food_registration_info(
            (breakfast_foods, lunch_foods, dinner_foods),
            ((breakfast_food, breakfast_calories) if not breakfast_foods else ('', 0),
             (lunch_food, lunch_calories) if not lunch_foods else ('', 0),
             (dinner_food, dinner_calories) if not dinner_foods else ('', 0)))
        all_food_names = (
            _parse_food_names(breakfast_food) +
            _parse_food_names(lunch_food) +
            _parse_food_names(dinner_food)
        )
        new_foods = repo.add_missing_foods(session, all_food_names, defaults=reg_info)

        # 更新后的记录（含重算后的累计值）与统计看板
        updated_record = repo.get_record(session, date)
        dashboard = calculator.calc_dashboard(records)

    return jsonify({
        'success': True,
        'record': updated_record,
        'dashboard': dashboard,
        'new_foods': new_foods
    })


@calorie_bp.route('/calorie/api/record/<record_id>', methods=['DELETE'])
def api_delete_record(record_id):
    """删除一条记录"""
    with session_scope() as session:
        config = repo.lock_config(session)
        if not repo.delete_record(session, record_id):
            return jsonify({'success': False, 'error': '记录不存在'}), 404

        before = repo.load_record_metrics(session)
        records = _recalc_cumulative([dict(r) for r in before])
        repo.bulk_update_records(session, repo.changed_metrics(before, records))

    calculator = CalorieCalculator(config)
    dashboard = calculator.calc_dashboard(records)

    return jsonify({
        'success': True,
        'dashboard': dashboard
    })


# =============================================================================
# API 路由 - 食物热量库
# =============================================================================

@calorie_bp.route('/calorie/api/foods', methods=['GET'])
def api_get_foods():
    """获取食物热量库列表"""
    with session_scope() as session:
        foods = _ensure_food_seed(session)

    # 支持搜索
    search = request.args.get('search', '').strip().lower()
    category = request.args.get('category', '').strip()

    if search:
        foods = [f for f in foods if search in f['name'].lower()]
    if category:
        foods = [f for f in foods if f.get('category') == category]

    # 按名称排序
    foods.sort(key=lambda f: f['name'])

    return jsonify({
        'success': True,
        'foods': foods,
        'total': len(foods)
    })


@calorie_bp.route('/calorie/api/food', methods=['POST'])
def api_add_food():
    """新增食物到热量库"""
    try:
        req = request.get_json(force=True)
    except Exception:
        return jsonify({'success': False, 'error': '无效的请求数据'}), 400

    name = req.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': '食物名称不能为空'}), 400

    unit = req.get('unit', '100克').strip()
    try:
        calories = float(req.get('calories', 0))
    except (ValueError, TypeError):
        calories = 0
    category = req.get('category', '其他').strip()

    food_db = None  # 兼容占位：查重与写入均在事务内完成
    with session_scope() as session:
        repo.lock_config(session)
        _ensure_food_seed(session)

        # 检查是否已存在（不区分大小写）
        if repo.find_food_by_name(session, name):
            return jsonify({'success': False, 'error': f'食物"{name}"已存在'}), 409

        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        new_food = {
            'id': repo.next_food_id(session),
            'name': name,
            'unit': unit,
            'calories': calories,
            'category': category if category else '其他',
            'created_at': now
        }
        repo.add_food(session, new_food)

    return jsonify({'success': True, 'food': new_food})


@calorie_bp.route('/calorie/api/food/<food_id>', methods=['PUT'])
def api_update_food(food_id):
    """更新食物信息"""
    try:
        req = request.get_json(force=True)
    except Exception:
        return jsonify({'success': False, 'error': '无效的请求数据'}), 400

    with session_scope() as session:
        repo.lock_config(session)
        target = repo.get_food(session, food_id)
        if not target:
            return jsonify({'success': False, 'error': '食物不存在'}), 404

        name = req.get('name', '').strip()
        fields = {}
        if name:
            # 检查是否与其他食物重名
            if repo.find_food_by_name_excluding(session, name, food_id):
                return jsonify({'success': False, 'error': f'食物"{name}"已存在'}), 409
            fields['name'] = name

        if 'unit' in req:
            fields['unit'] = req['unit'].strip()
        if 'calories' in req:
            try:
                fields['calories'] = float(req['calories'])
            except (ValueError, TypeError):
                pass
        if 'category' in req:
            fields['category'] = req['category'].strip() if req['category'].strip() else '其他'

        target = repo.update_food_fields(session, food_id, **fields) or target

    return jsonify({'success': True, 'food': target})


@calorie_bp.route('/calorie/api/food/<food_id>', methods=['DELETE'])
def api_delete_food(food_id):
    """删除食物"""
    with session_scope() as session:
        repo.lock_config(session)
        if not repo.delete_food(session, food_id):
            return jsonify({'success': False, 'error': '食物不存在'}), 404

    return jsonify({'success': True})


# =============================================================================
# API 路由 - 配置
# =============================================================================

@calorie_bp.route('/calorie/api/config', methods=['GET'])
def api_get_config():
    """获取配置参数"""
    with session_scope() as session:
        config = repo.load_config(session)
    return jsonify({'success': True, 'config': config})


@calorie_bp.route('/calorie/api/config', methods=['PUT'])
def api_update_config():
    """更新配置参数"""
    try:
        req = request.get_json(force=True)
    except Exception:
        return jsonify({'success': False, 'error': '无效的请求数据'}), 400

    with session_scope() as session:
        config = repo.lock_config(session)

        # 更新可配置项
        for key in ('height', 'age', 'step_frequency', 'weight_factor', 'target_deficit'):
            if key in req:
                try:
                    val = float(req[key])
                    if key == 'target_deficit' and val <= 0:
                        continue
                    config[key] = val
                except (ValueError, TypeError):
                    pass

        repo.save_config(session, config)

        # 配置变化后重新计算所有记录：先在内存算好每条的新值，再合并为一条
        # 批量 UPDATE 写回，避免逐条 UPDATE 在远端 MySQL 上产生 N 次网络往返
        calculator = CalorieCalculator(config)
        before = repo.load_record_metrics(session)
        raw_records = [dict(r) for r in before]
        for record in raw_records:
            mw = record.get('morning_weight')
            bmr = calculator.calc_bmr(mw)
            bf = record.get('breakfast_calories', 0) or 0
            lf = record.get('lunch_calories', 0) or 0
            df = record.get('dinner_calories', 0) or 0
            steps = record.get('daily_steps', 0) or 0
            intake_deficit = calculator.calc_intake_deficit(bmr, bf, lf, df)
            exercise_calories = calculator.calc_exercise_calories(steps)
            calorie_deficit = calculator.calc_calorie_deficit(intake_deficit, exercise_calories)
            record['bmr'] = bmr
            record['intake_deficit'] = intake_deficit
            record['exercise_calories'] = exercise_calories
            record['calorie_deficit'] = calorie_deficit

        records = _recalc_cumulative(raw_records)
        repo.bulk_update_records(session, repo.changed_metrics(before, records))

    dashboard = calculator.calc_dashboard(records)

    return jsonify({
        'success': True,
        'config': config,
        'dashboard': dashboard
    })


# =============================================================================
# API 路由 - 食物分类列表（用于前端筛选）
# =============================================================================

@calorie_bp.route('/calorie/api/categories', methods=['GET'])
def api_get_categories():
    """获取食物分类列表"""
    with session_scope() as session:
        foods = _ensure_food_seed(session)
    categories = set()
    for f in foods:
        cat = f.get('category', '其他')
        if cat:
            categories.add(cat)
    return jsonify({
        'success': True,
        'categories': sorted(categories)
    })


# =============================================================================
# API 路由 - 美食打卡点（探店记录 + 照片）
# -----------------------------------------------------------------------------
# 创建/更新走 multipart/form-data：文本字段与 images[] 文件一次提交；
# 照片回显走 /calorie/api/food-spot/photo/<photo_id>（send_from_directory）。
# =============================================================================

@calorie_bp.route('/calorie/api/food-spots', methods=['GET'])
def api_list_food_spots():
    """获取全部美食打卡点（含照片索引），按创建时间倒序"""
    with session_scope() as session:
        spots = repo.list_spots(session)
    return jsonify({'success': True, 'spots': spots, 'total': len(spots)})


@calorie_bp.route('/calorie/api/food-spot', methods=['POST'])
def api_save_food_spot():
    """创建或更新一条美食打卡点（带 id 为更新）。

    表单字段：shop_name(必填)/address/category/review；
    可选 id 与 files 里的 images[] 图片（多张，美食特写/环境/地图截图等）。
    """
    shop_name = (request.form.get('shop_name') or '').strip()
    spot_id = (request.form.get('id') or '').strip()
    # 店铺名必填仅在新建时强校验；更新时缺省视为不改名
    if not shop_name and not spot_id:
        return jsonify({'success': False, 'error': '店铺名称不能为空'}), 400
    if len(shop_name) > 128:
        return jsonify({'success': False, 'error': '店铺名称过长（≤128字）'}), 400

    fields = {
        'address': (request.form.get('address') or '').strip()[:255],
        'category': (request.form.get('category') or '').strip()[:64],
        'review': (request.form.get('review') or '').strip(),
    }
    if shop_name:
        fields['shop_name'] = shop_name

    files = [f for f in request.files.getlist('images[]') if f and f.filename]
    if len(files) > _SPOT_PHOTO_MAX_COUNT:
        return jsonify({'success': False,
                        'error': f'单次最多上传 {_SPOT_PHOTO_MAX_COUNT} 张照片'}), 400

    with session_scope() as session:
        if spot_id:
            if not repo.get_spot(session, spot_id):
                return jsonify({'success': False, 'error': '打卡点不存在'}), 404
        else:
            spot_id = repo.next_spot_id()
        # 先提交主表（新建时照片外键才有落点；更新时文本字段即时生效）
        repo.upsert_spot(session, spot_id, fields)

    photos_err = ''
    if files:
        _, photos_err = _save_spot_photo_files(files, spot_id)

    with session_scope() as session:
        spot = repo.get_spot(session, spot_id)

    if photos_err:
        return jsonify({'success': False, 'error': photos_err, 'spot': spot}), 400
    return jsonify({'success': True, 'spot': spot})


@calorie_bp.route('/calorie/api/food-spot/<spot_id>', methods=['DELETE'])
def api_delete_food_spot(spot_id):
    """删除打卡点及其全部照片（先删库确认成功，再尽力清理磁盘文件）"""
    with session_scope() as session:
        filenames = repo.delete_spot(session, spot_id)
    if filenames is None:
        return jsonify({'success': False, 'error': '打卡点不存在'}), 404
    for name in filenames:
        _remove_spot_photo_by_filename(name)
    return jsonify({'success': True})


@calorie_bp.route('/calorie/api/food-spot/photo/<photo_id>', methods=['GET'])
def api_get_food_spot_photo(photo_id):
    """回显单张打卡点照片（按库中索引定位磁盘文件，文件名白名单校验防穿越）"""
    if not re.match(r'^fsp_[a-z0-9]{1,40}$', photo_id or ''):
        return jsonify({'success': False, 'error': '无效的照片编号'}), 400
    with session_scope() as session:
        filename = repo.get_spot_photo_filename(session, photo_id)
    if not filename:
        return jsonify({'success': False, 'error': '照片不存在'}), 404
    photo_dir = _spot_photo_dir()
    if not photo_dir or not _SPOT_PHOTO_NAME_RE.match(filename):
        return jsonify({'success': False, 'error': '照片存储不可用'}), 500
    return send_from_directory(photo_dir, filename)


@calorie_bp.route('/calorie/api/food-spot/photo/<photo_id>', methods=['DELETE'])
def api_delete_food_spot_photo(photo_id):
    """删除单张照片（编辑打卡记录时移除已上传照片）"""
    if not re.match(r'^fsp_[a-z0-9]{1,40}$', photo_id or ''):
        return jsonify({'success': False, 'error': '无效的照片编号'}), 400
    with session_scope() as session:
        ok, filename = repo.delete_spot_photo(session, photo_id)
    if not ok:
        return jsonify({'success': False, 'error': '照片不存在'}), 404
    _remove_spot_photo_by_filename(filename)
    return jsonify({'success': True})