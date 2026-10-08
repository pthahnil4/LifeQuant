"""支出管理纯规则：整数分、业务日期、周期分段和预算分配，不访问数据库。"""
import calendar
import copy
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_MINOR = 9_999_999_999
UNCATEGORIZED = 'expense_uncategorized'
DEFAULTS = {
    'cycle_day': 1, 'week_start': 0, 'budget_minor': None,
    'warning': 80, 'critical': 90, 'pace_points': 10,
    'remind_days': 3, 'check_enabled': True, 'check_time': '21:00',
    'quick_entries': [], 'category_budgets': [],
    'payment_methods': ['现金', '微信', '支付宝', '银行卡', '信用卡'],
}
SEED_CATEGORIES = {
    '餐饮食品': '早餐 午餐 晚餐 外卖 饮品 零食 水果 生鲜食材',
    '交通出行': '公交地铁 打车 燃油充电 停车通行 车辆维护 长途交通',
    '居住账单': '房租 物业 水电燃气 通信网络 家政维修',
    '日用购物': '日用品 服饰鞋包 数码家电 家具家居 美容护理',
    '娱乐休闲': '影音会员 游戏 聚会 运动健身 线下娱乐',
    '医疗健康': '门诊 药品 体检 治疗',
    '学习成长': '图书 课程 考试 培训 学习工具',
    '工作项目': '软件订阅 AI工具 服务器 域名 办公用品 服务采购',
    '家庭照护': '家庭支持 育儿 养老 宠物',
    '人情社交': '礼物 红包 请客 捐赠',
    '旅行度假': '住宿 景点 旅行服务',
    '财务费用': '税费 保险 利息 手续费',
    '资金安排': '投入本金 偿还本金',
    '其他支出': '临时事项',
}
SEED_TAGS = '生活 工作 项目 家庭 出差 必要 可选 冲动 临时 大额 固定 订阅 一次性 可报销 投资本金 借款本金'.split()
COLORS = ['#3a7564', '#537cad', '#a77845', '#8973a8', '#b76669', '#688e80']


class ExpenseError(ValueError):
    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.status = status
        self.details = details


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def loads(value):
    return json.loads(value)


def text(value, limit=32, required=False):
    if not isinstance(value, str):
        raise ExpenseError('文本字段格式不正确')
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise ExpenseError(f'请填写 1～{limit} 字的内容' if required else f'内容不能超过 {limit} 字')
    return value


def integer(value, minimum, maximum):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ExpenseError('请输入有效整数')
    value = int(value)
    if not minimum <= value <= maximum:
        raise ExpenseError(f'数值范围应为 {minimum}～{maximum}')
    return value


def money(value, nullable=False, zero=False):
    if nullable and (value is None or value == ''):
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,8}(?:\.\d{1,2})?', value.strip()):
        raise ExpenseError('金额须为普通十进制字符串，最多两位小数')
    result = int(Decimal(value.strip()) * 100)
    if not (0 if zero else 1) <= result <= MAX_MINOR:
        raise ExpenseError('金额超出范围')
    return result


def amount(minor):
    if minor is None:
        return None
    minor = int(minor)
    return ('-' if minor < 0 else '') + f'{abs(minor) // 100}.{abs(minor) % 100:02d}'


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ExpenseError('日期格式须为 YYYY-MM-DD')
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise ExpenseError('日期不存在') from None
    if not 2000 <= result.year <= 2100:
        raise ExpenseError('日期范围为 2000～2100 年')
    return result


def tz(name):
    try:
        return ZoneInfo(text(name, 64, True))
    except (ZoneInfoNotFoundError, ValueError):
        raise ExpenseError('时区无效或缺少 tzdata 时区数据库') from None


def today(name='Asia/Shanghai'):
    return datetime.now(tz(name)).date()


def date_range(start, end):
    for offset in range(max(0, (end - start).days)):
        yield start + timedelta(days=offset)


def anchor(year, month, desired):
    return date(year, month, min(desired, calendar.monthrange(year, month)[1]))


def month_add(value, count, desired=None):
    number = value.year * 12 + value.month - 1 + count
    return anchor(number // 12, number % 12 + 1, desired or value.day)


def ordinary_bounds(value, cycle_day):
    start = anchor(value.year, value.month, cycle_day)
    if start > value:
        start = month_add(start, -1, cycle_day)
    return start, month_add(start, 1, cycle_day)


def period_rule(value, rules):
    """规则生效日切割周期，过渡段连续、无缺口，旧记录不迁移。"""
    applicable = None
    following = None
    for rule in sorted(rules, key=lambda r: r['effective_from']):
        effective = day(rule['effective_from'])
        if effective <= value:
            applicable = rule
        else:
            following = effective
            break
    config = copy.deepcopy(DEFAULTS)
    if applicable:
        config.update(applicable)
    start, end = ordinary_bounds(value, config['cycle_day'])
    if applicable:
        start = max(start, day(applicable['effective_from']))
    if following:
        end = min(end, following)
    if applicable and start == day(applicable['effective_from']) and 'transition_budget_minor' in applicable:
        config['budget_minor'] = applicable['transition_budget_minor']
    return start, end, config


def weekly_allocation(start, end, budget, fixed, week_start=0):
    pool = None if budget is None else max(budget - fixed, 0)
    length = (end - start).days
    base, extra = divmod(pool or 0, length)
    weeks = []
    for i, current in enumerate(date_range(start, end)):
        if not weeks or current.weekday() == week_start:
            weeks.append({'start': current.isoformat(), 'end': current.isoformat(), 'budget_minor': None if pool is None else 0})
        weeks[-1]['end'] = (current + timedelta(days=1)).isoformat()
        if pool is not None:
            weeks[-1]['budget_minor'] += base + (i < extra)
    return weeks


def budget_status(spent, budget, warning=80, critical=90):
    if budget is None:
        return {'level': 'unset', 'label': '未设置预算', 'percent': None}
    if spent > budget:
        level, label = 'over', '已超支'
    elif spent == budget:
        level, label = 'exhausted', '预算用尽'
    elif budget and spent * 100 >= budget * critical:
        level, label = 'critical', '接近上限'
    elif budget and spent * 100 >= budget * warning:
        level, label = 'warning', '请留意额度'
    else:
        level, label = 'normal', '预算正常'
    percent = round(max(spent, 0) * 100 / budget, 2) if budget else None
    return {'level': level, 'label': label, 'percent': percent}


def validate_preferences(body, previous=None):
    config = copy.deepcopy(previous or DEFAULTS)
    for key, lo, hi in (('cycle_day', 1, 31), ('week_start', 0, 6), ('warning', 1, 99),
                        ('critical', 1, 99), ('pace_points', 0, 99), ('remind_days', 0, 30)):
        if key in body:
            config[key] = integer(body[key], lo, hi)
    if config['week_start'] not in (0, 6):
        raise ExpenseError('周起始日仅支持周一或周日')
    if config['warning'] >= config['critical']:
        raise ExpenseError('提醒阈值必须小于强提醒阈值，且都小于 100%')
    if 'budget' in body:
        config['budget_minor'] = money(body['budget'], nullable=True, zero=True)
    if 'check_time' in body:
        if not isinstance(body['check_time'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', body['check_time']):
            raise ExpenseError('核对提醒时间须为 HH:MM')
        config['check_time'] = body['check_time']
    if 'check_enabled' in body:
        if not isinstance(body['check_enabled'], bool):
            raise ExpenseError('提醒开关须为布尔值')
        config['check_enabled'] = body['check_enabled']
    if 'payment_methods' in body:
        values = body['payment_methods']
        if not isinstance(values, list) or len(values) > 30:
            raise ExpenseError('支付方式最多 30 项')
        config['payment_methods'] = list(dict.fromkeys(text(v, 64, True) for v in values))
    return config


def bill_dates(first, frequency, end):
    if frequency not in ('once', 'monthly', 'yearly'):
        raise ExpenseError('账单频率须为一次性、每月或每年')
    current, index = first, 0
    while current < end:
        yield current
        if frequency == 'once':
            break
        index += 1
        current = month_add(first, index * (12 if frequency == 'yearly' else 1), first.day)


def safe_csv(value):
    value = str(value or '')
    if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')) or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value
