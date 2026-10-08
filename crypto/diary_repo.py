#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日记本模块数据访问层（MySQL 版）
================================
职责：
- 密码哈希：优先 argon2（argon2-cffi），其次 bcrypt，两者都缺失时退到
  PBKDF2-HMAC-SHA256（哈希串带算法前缀，验证时按前缀分派，永不含明文）
- diary_settings 单行设置的读取/惰性建行/更新（可见性、密码、生日）
- diary_entries 按天 CRUD、分页列表、日历聚合
- diary_goals 目标倒计时 CRUD

口径与 journal_repo 一致：全部函数接收外部 session，不自开事务；
对外序列化统一 'YYYY-MM-DD HH:MM:SS' 字符串。
"""

import hashlib
import hmac
import logging
import math
import secrets
import uuid
from datetime import datetime, date

from sqlalchemy import select, func

from .models import DiarySetting, DiaryEntry, DiaryGoal

logger = logging.getLogger(__name__)

# 解锁凭据 cookie 的签名盐（与 web_auth 的闸门 cookie 无关，只管日记隐私态）
_UNLOCK_SALT = b'cryptoTrade-diary-unlock-v1'
UNLOCK_COOKIE = 'ct_diary_unlock'
UNLOCK_TTL_SECONDS = 2 * 3600   # 解锁有效 2 小时，过期需重新输入密码

# 密码哈希算法前缀
_PREFIX_ARGON2 = 'argon2id$'
_PREFIX_BCRYPT = 'bcrypt$'
_PREFIX_PBKDF2 = 'pbkdf2$'
_PBKDF2_ROUNDS = 240000

# 密码长度上下限（防误设空串把门焊死，也防超长 DoS）
PASSWORD_MIN_CHARS = 4
PASSWORD_MAX_CHARS = 128


class DiaryPasswordError(ValueError):
    """密码设置/校验失败；调用方据此返回 400/401。"""


# =============================================================================
# 密码哈希（纯计算，不碰数据库，可离线冒烟）
# =============================================================================

def _hashlib_pbkdf2(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, _PBKDF2_ROUNDS)
    return f'{_PREFIX_PBKDF2}{salt.hex()}${_PBKDF2_ROUNDS}${dk.hex()}'


def hash_password(password: str) -> str:
    """生成带算法前缀的密码哈希串（argon2 > bcrypt > pbkdf2 依次探测）。"""
    pwd = str(password or '')
    if not (PASSWORD_MIN_CHARS <= len(pwd) <= PASSWORD_MAX_CHARS):
        raise DiaryPasswordError(f'密码长度需在 {PASSWORD_MIN_CHARS}~{PASSWORD_MAX_CHARS} 字符之间')
    try:
        from argon2 import PasswordHasher
        return _PREFIX_ARGON2 + PasswordHasher().hash(pwd)
    except ImportError:
        pass
    except Exception as e:  # argon2 装了但底层库异常（如缺 ffi），退到下一档
        logger.warning('[Diary] argon2 不可用，回退其它哈希算法: %s', e)
    try:
        import bcrypt
        return _PREFIX_BCRYPT + bcrypt.hashpw(pwd.encode('utf-8'), bcrypt.gensalt()).decode('ascii')
    except ImportError:
        pass
    except Exception as e:
        logger.warning('[Diary] bcrypt 不可用，回退 pbkdf2: %s', e)
    return _hashlib_pbkdf2(pwd)


def _pbkdf2_verify(password: str, body: str) -> bool:
    try:
        salt_hex, rounds_s, dk_hex = body.split('$', 2)
        salt = bytes.fromhex(salt_hex)
        rounds = int(rounds_s)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, rounds)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


def verify_password(password: str, stored: str) -> bool:
    """按哈希串前缀分派校验；任何异常都保守判为校验失败。"""
    stored = str(stored or '')
    pwd = str(password or '')
    if not stored or not pwd:
        return False
    try:
        if stored.startswith(_PREFIX_ARGON2):
            from argon2 import PasswordHasher
            from argon2.exceptions import VerifyMismatchError
            try:
                PasswordHasher().verify(stored[len(_PREFIX_ARGON2):], pwd)
                return True
            except VerifyMismatchError:
                return False
        if stored.startswith(_PREFIX_BCRYPT):
            import bcrypt
            return bcrypt.checkpw(pwd.encode('utf-8'), stored[len(_PREFIX_BCRYPT):].encode('ascii'))
        if stored.startswith(_PREFIX_PBKDF2):
            return _pbkdf2_verify(pwd, stored[len(_PREFIX_PBKDF2):])
    except Exception as e:
        logger.warning('[Diary] 密码校验异常（按失败处理）: %s', e)
        return False
    # 未知前缀（手工改库/降级残留）：无法校验，一律失败，绝不放行
    logger.warning('[Diary] 未知密码哈希前缀，拒绝解锁')
    return False


# =============================================================================
# 解锁凭据 cookie（HMAC 签名，不含口令/哈希本身）
# =============================================================================

def unlock_cookie_value(password_hash: str, expires_ts: int) -> str:
    """解锁成功后写入浏览器的 cookie 值：sig.expires，签名覆盖哈希+过期点。"""
    msg = f'{password_hash}|{expires_ts}'.encode('utf-8')
    sig = hmac.new(_UNLOCK_SALT, msg, hashlib.sha256).hexdigest()
    return f'{sig}.{expires_ts}'


def unlock_cookie_ok(cookie_val: str, password_hash: str, now_ts: int) -> bool:
    """校验解锁 cookie：签名对 + 未过期 + 哈希未变更（改/删密码即全体失效）。"""
    raw = str(cookie_val or '')
    if not raw or not password_hash or '.' not in raw:
        return False
    sig, _, expires_s = raw.rpartition('.')
    try:
        expires_ts = int(expires_s)
    except ValueError:
        return False
    if expires_ts <= now_ts:
        return False
    expect = unlock_cookie_value(password_hash, expires_ts).rpartition('.')[0]
    return hmac.compare_digest(sig, expect)


def new_unlock_expiry(now_ts: int) -> int:
    return now_ts + UNLOCK_TTL_SECONDS


# =============================================================================
# 设置（diary_settings 单行，惰性建行）
# =============================================================================

def get_or_create_settings(session) -> DiarySetting:
    """读取单行设置；不存在则就地创建默认行（public / 无密码 / 无生日）。"""
    row = session.get(DiarySetting, 1)
    if row is None:
        row = DiarySetting(id=1)
        session.add(row)
        session.flush()
    return row


def update_settings_fields(session, birth_date=None, visibility=None) -> DiarySetting:
    """更新非密码类设置字段（None 表示不改）；返回更新后的单行。

    密码相关变更一律走 set_password/clear_password，禁止从这里旁路。
    """
    row = get_or_create_settings(session)
    if visibility is not None:
        row.visibility = visibility
    if birth_date is not None:
        row.birth_date = birth_date
    row.updated_at = datetime.now()
    return row


def set_password(session, password: str, old_password: str = None) -> DiarySetting:
    """设置/更新访问密码。已有密码时必须提供旧密码才能更换（防丢锁改密）。"""
    row = get_or_create_settings(session)
    if row.password_hash:
        if not old_password or not verify_password(old_password, row.password_hash):
            raise DiaryPasswordError('原密码不正确')
    row.password_hash = hash_password(password)
    row.updated_at = datetime.now()
    return row


def clear_password(session, password: str) -> DiarySetting:
    """校验密码后清除（同时调用方应把可见性退回 public，防无门可解）。"""
    row = get_or_create_settings(session)
    if row.password_hash and not verify_password(password, row.password_hash):
        raise DiaryPasswordError('密码不正确')
    row.password_hash = ''
    row.updated_at = datetime.now()
    return row


# =============================================================================
# 日期工具
# =============================================================================

_DATE_RE_FMT = '%Y-%m-%d'


def valid_date_str(raw) -> str:
    """校验 'YYYY-MM-DD' 日期串；非法抛 ValueError（调用方转 400）。"""
    s = str(raw or '').strip()
    try:
        return date.fromisoformat(s).isoformat()
    except (ValueError, TypeError):
        raise ValueError(f'日期格式非法（应为 YYYY-MM-DD）: {raw!r}')


def month_str(raw) -> str:
    """校验 'YYYY-MM' 月份串；非法抛 ValueError。"""
    s = str(raw or '').strip()
    try:
        d = datetime.strptime(s, '%Y-%m')
        return d.strftime('%Y-%m')
    except (ValueError, TypeError):
        raise ValueError(f'月份格式非法（应为 YYYY-MM）: {raw!r}')


# =============================================================================
# 条目（diary_entries）
# =============================================================================

def new_entry_id() -> str:
    return 'dentry_' + uuid.uuid4().hex[:8]


def add_entry(session, entry: dict) -> dict:
    """写入一条日记；entry 需含 date/content，可选 mood/private/linked_from 与时间。"""
    now = datetime.now()
    row = DiaryEntry(
        id=entry.get('id') or new_entry_id(),
        date=valid_date_str(entry['date']),
        content=str(entry.get('content') or '').strip(),
        mood=str(entry.get('mood') or '')[:16],
        private=bool(entry.get('private')),
        linked_from=str(entry.get('linked_from') or '')[:64],
        created_at=_parse_ts(entry.get('created_at')) or now,
        updated_at=_parse_ts(entry.get('updated_at')) or now,
    )
    if not row.content:
        raise ValueError('日记内容不能为空')
    session.add(row)
    session.flush()
    return row.to_dict()


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def get_entry(session, entry_id: str):
    if not entry_id:
        return None
    return session.get(DiaryEntry, entry_id)


def update_entry(session, entry_id: str, content=None, mood=None,
                 private=None, date=None) -> dict:
    """编辑条目字段（None 表示不改）；条目不存在返回 None。"""
    row = session.get(DiaryEntry, entry_id)
    if row is None:
        return None
    if content is not None:
        c = str(content).strip()
        if not c:
            raise ValueError('日记内容不能为空')
        row.content = c
    if mood is not None:
        row.mood = str(mood)[:16]
    if private is not None:
        row.private = bool(private)
    if date is not None:
        row.date = valid_date_str(date)
    row.updated_at = datetime.now()
    return row.to_dict()


def delete_entry(session, entry_id: str) -> bool:
    row = session.get(DiaryEntry, entry_id)
    if row is None:
        return False
    session.delete(row)
    return True


def list_entries(session, limit: int = 60, before_id: str = None):
    """按（日期倒序，创建时间倒序，id 倒序）分页拉取条目，返回 dict 列表。

    before_id 指定锚点：只返回排序上位于该条目之后的（更早的）页。
    """
    stmt = select(DiaryEntry).order_by(DiaryEntry.date.desc(),
                                       DiaryEntry.created_at.desc(),
                                       DiaryEntry.id.desc())
    if before_id:
        anchor = session.get(DiaryEntry, before_id)
        if anchor is not None:
            stmt = stmt.where(
                (DiaryEntry.date < anchor.date)
                | ((DiaryEntry.date == anchor.date)
                   & ((DiaryEntry.created_at < anchor.created_at)
                      | ((DiaryEntry.created_at == anchor.created_at)
                         & (DiaryEntry.id < anchor.id)))))
    rows = session.execute(stmt.limit(max(1, min(int(limit), 500)))).scalars().all()
    return [r.to_dict() for r in rows]


def entries_for_date(session, day: str):
    """某天的全部条目（时间正序）。"""
    day = valid_date_str(day)
    rows = session.execute(
        select(DiaryEntry).where(DiaryEntry.date == day)
        .order_by(DiaryEntry.created_at, DiaryEntry.id)
    ).scalars().all()
    return [r.to_dict() for r in rows]


def month_calendar(session, month: str):
    """某月日历聚合：返回 [{'date', 'total', 'private'}]（按日期升序）。"""
    month = month_str(month)
    rows = session.execute(
        select(DiaryEntry.date, func.count(), func.sum(DiaryEntry.private))
        .where(DiaryEntry.date.like(f'{month}-%'))
        .group_by(DiaryEntry.date)
        .order_by(DiaryEntry.date)
    ).all()
    return [{'date': r[0], 'total': int(r[1]), 'private': int(r[2] or 0)} for r in rows]


def total_entry_count(session) -> int:
    return int(session.execute(select(func.count()).select_from(DiaryEntry)).scalar() or 0)


def all_entries(session):
    """导出用：全部条目（日期升序，同日按创建升序）。"""
    rows = session.execute(
        select(DiaryEntry).order_by(DiaryEntry.date, DiaryEntry.created_at, DiaryEntry.id)
    ).scalars().all()
    return [r.to_dict() for r in rows]


# =============================================================================
# 目标倒计时（diary_goals）
# =============================================================================

def new_goal_id() -> str:
    return 'dgoal_' + uuid.uuid4().hex[:8]


def add_goal(session, title: str, due_date: str, note: str = '') -> dict:
    title = str(title or '').strip()
    if not title:
        raise ValueError('目标标题不能为空')
    if len(title) > 120:
        raise ValueError('目标标题不能超过 120 字符')
    row = DiaryGoal(id=new_goal_id(), title=title, due_date=valid_date_str(due_date),
                    note=str(note or '').strip(), done=False)
    session.add(row)
    session.flush()
    return row.to_dict()


def list_goals(session):
    """全部目标：未完成在前，按截止日期升序。"""
    rows = session.execute(
        select(DiaryGoal).order_by(DiaryGoal.done, DiaryGoal.due_date, DiaryGoal.id)
    ).scalars().all()
    return [r.to_dict() for r in rows]


def update_goal(session, goal_id: str, title=None, due_date=None, note=None,
                done=None) -> dict:
    row = session.get(DiaryGoal, goal_id)
    if row is None:
        return None
    if title is not None:
        t = str(title).strip()
        if not t:
            raise ValueError('目标标题不能为空')
        if len(t) > 120:
            raise ValueError('目标标题不能超过 120 字符')
        row.title = t
    if due_date is not None:
        row.due_date = valid_date_str(due_date)
    if note is not None:
        row.note = str(note).strip()
    if done is not None:
        row.done = bool(done)
    row.updated_at = datetime.now()
    return row.to_dict()


def delete_goal(session, goal_id: str) -> bool:
    row = session.get(DiaryGoal, goal_id)
    if row is None:
        return False
    session.delete(row)
    return True


# =============================================================================
# 人生计数器（纯计算，前后端同一口径，供接口直出兜底）
# =============================================================================

def life_day_age(birth_date: str, now: datetime = None):
    """返回 {'days': int, 'hours': int, 'total_days_float': float}；生日非法返回 None。

    口径：从出生日 00:00:00 起到 now 的整天数与剩余小时数（人生第 N 天从 1 起算）。
    """
    try:
        b = date.fromisoformat(str(birth_date or '').strip())
    except (ValueError, TypeError):
        return None
    now = now or datetime.now()
    birth_dt = datetime.combine(b, datetime.min.time())
    if now < birth_dt:
        return None
    delta_seconds = (now - birth_dt).total_seconds()
    total_days = math.floor(delta_seconds / 86400)
    return {
        'days': total_days + 1,                       # 出生当天即人生第 1 天
        'hours': math.floor((delta_seconds - total_days * 86400) / 3600),
        'total_days_float': round(delta_seconds / 86400, 4),
    }


def goal_countdown(due_date: str, now: datetime = None):
    """截止日期倒计时（截止日当天 23:59:59 前都算未过期）。

    返回 {'days_left': int, 'hours_left': int, 'overdue': bool}；日期非法返回 None。
    """
    try:
        d = date.fromisoformat(str(due_date or '').strip())
    except (ValueError, TypeError):
        return None
    now = now or datetime.now()
    deadline = datetime.combine(d, datetime.max.time()).replace(microsecond=0)
    delta_seconds = (deadline - now).total_seconds()
    if delta_seconds < 0:
        return {'days_left': 0, 'hours_left': 0, 'overdue': True}
    return {
        'days_left': math.floor(delta_seconds / 86400),
        'hours_left': math.floor((delta_seconds % 86400) / 3600),
        'overdue': False,
    }
