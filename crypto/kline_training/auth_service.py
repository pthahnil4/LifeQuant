#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/auth_service.py  →  现位置: crypto/kline_training/auth_service.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
轻量鉴权业务层
==============
邮箱验证码注册 / 多标识登录 / 改密 / 备注。口令哈希用 ``werkzeug.security``
（Flask 自带），会话态由 Flask session 承载（密钥见 ``config.SECRET_KEY``）。

登录模型（按产品确认）
----------------------
- 首次：邮箱 + 邮箱验证码 → 注册即登录；系统自动生成默认口令
  = 邮箱「@」前前 6 个字母/数字（不足则全取），用户随后可自行改密。
- 再次：用 邮箱 / 手机号 / 昵称 任一 + 口令 登录。
- 忘记口令：可用「邮箱验证码」直接登录（purpose='reset'），顺带可设新口令。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import logging
import random
import re
import sys
import os


from crypto.kline_training import settings as config
from crypto.kline_training import store as db
from crypto.kline_training import mailer

try:
    from werkzeug.security import generate_password_hash, check_password_hash
except Exception:                          # 极端兜底：werkzeug 不可用时用 pbkdf2
    import hashlib
    import secrets as _secrets

    def generate_password_hash(pw):
        salt = _secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'),
                                 salt.encode('utf-8'), 120000)
        return 'pbk2$' + salt + '$' + dk.hex()

    def check_password_hash(stored, pw):
        try:
            _, salt, digest = stored.split('$', 2)
            dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'),
                                     salt.encode('utf-8'), 120000)
            return dk.hex() == digest
        except Exception:
            return False

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r'^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$')
_PHONE_RE = re.compile(r'^1[3-9]\d{9}$')


# ====================================================================
#  校验 / 工具
# ====================================================================

def valid_email(email):
    return bool(_EMAIL_RE.match((email or '').strip()))


def valid_phone(phone):
    phone = (phone or '').strip()
    return (not phone) or bool(_PHONE_RE.match(phone))     # 手机号可选，空即合法


def norm_email(email):
    return (email or '').strip().lower()


def default_password(email):
    """从邮箱派生默认口令：@ 前的前 6 个字母/数字（不足则全取，空则回退）。"""
    local = norm_email(email).split('@')[0]
    alnum = [c for c in local if c.isalnum()]
    pw = ''.join(alnum[:6])
    if len(pw) < 4:                        # 过短则用邮箱哈希补稳定口令
        pw = (pw + 'kline' + str(abs(hash(local)) % 1000))[:8]
    return pw


def gen_code():
    return '%06d' % random.randint(0, 999999)


def public_user(row):
    """剥离敏感字段，返回可下发前端的用户信息。"""
    if not row:
        return None
    return {'id': row['id'], 'email': row.get('email'),
            'nickname': row.get('nickname'), 'phone': row.get('phone')}


# ====================================================================
#  验证码
# ====================================================================

def send_code(email, purpose='register'):
    """生成并发送验证码。返回 dict：{ok, err, dev_code}。

    失败但 EMAIL_DEV_FALLBACK 为真时，dev_code 回填以便本地自测。
    """
    email = norm_email(email)
    if not valid_email(email):
        raise ValueError('邮箱格式不正确')
    code = gen_code()
    db.save_verify_code(email, code, purpose=purpose, ttl=config.EMAIL_CODE_TTL)
    ok, err, dev_code = mailer.send_verify_code(email, code)
    return {'ok': ok, 'err': err, 'dev_code': dev_code}


def _verify_code(email, code, purpose):
    rec = db.get_verify_code(norm_email(email), purpose)
    if not rec:
        return False, '验证码已过期，请重新获取'
    if rec['code'] != (code or '').strip():
        return False, '验证码不正确'
    db.delete_verify_code(norm_email(email), purpose)   # 用后即焚
    return True, None


# ====================================================================
#  注册 / 登录
# ====================================================================

def register(email, code, nickname, phone=None):
    """邮箱验证码注册（注册即登录）。返回 (user_public, default_password)。"""
    email = norm_email(email)
    if not valid_email(email):
        raise ValueError('邮箱格式不正确')
    nickname = (nickname or '').strip()
    if not nickname:
        raise ValueError('昵称不能为空')
    if len(nickname) > 24:
        raise ValueError('昵称过长（最多 24 字）')
    if not valid_phone(phone):
        raise ValueError('手机号格式不正确')
    if db.get_user_by_email(email):
        raise ValueError('该邮箱已注册，请直接登录')

    ok, err = _verify_code(email, code, 'register')
    if not ok:
        raise ValueError(err)

    pw = default_password(email)
    pw_hash = generate_password_hash(pw)
    try:
        uid = db.create_user(email, pw_hash, nickname,
                             (phone or '').strip() or None)
    except Exception as e:
        msg = str(e)
        if 'nickname' in msg.lower() or 'users.nickname' in msg:
            raise ValueError('该昵称已被占用')
        if 'phone' in msg.lower():
            raise ValueError('该手机号已被占用')
        raise

    # 核对用：首次注册只落盘单向哈希，此处仅打印前 8 位摘要（非明文）
    logger.info('[register] new user id=%s email=%s hash_prefix=%s',
                uid, email, pw_hash[:8])
    user = db.get_user_by_id(uid)
    return public_user(user), pw


def login(identifier, password):
    """邮箱/手机号/昵称 + 口令登录。

    返回 ``(user_public, err_key)``：
      - 成功：user_public 为用户信息，err_key=None。
      - 失败：user_public=None，err_key ∈
          'empty'        未提供登录标识；
          'not_found'    账号不存在（邮箱/手机号/昵称都匹配不到）；
          'bad_password' 账号存在但口令不正确。
    区分「账号不存在」与「密码错误」，避免混为一谈、便于定位。
    """
    identifier = (identifier or '').strip()
    if not identifier:
        return None, 'empty'
    user = db.get_user_by_login(identifier)
    if not user:
        return None, 'not_found'
    if not check_password_hash(user['password_hash'] or '', password or ''):
        return None, 'bad_password'
    return public_user(user), None


def login_by_code(email, code):
    """邮箱验证码直接登录（找回密码入口）。成功返回 user_public。"""
    email = norm_email(email)
    ok, err = _verify_code(email, code, 'reset')
    if not ok:
        raise ValueError(err)
    user = db.get_user_by_email(email)
    if not user:
        raise ValueError('该邮箱尚未注册')
    return public_user(user)


def change_password(user_id, new_password):
    """登录用户改密。返回 bool。"""
    new_password = (new_password or '').strip()
    if len(new_password) < 4:
        raise ValueError('新密码至少 4 位')
    return db.update_password(user_id, generate_password_hash(new_password))


def update_nickname(user_id, nickname):
    """改昵称（非空、≤ 24、全局唯一，校验下沉 db.update_nickname）。"""
    return db.update_nickname(user_id, nickname)


def update_phone(user_id, phone):
    """改手机号（选填，非空需格式合法且全局唯一）。"""
    phone = (phone or '').strip()
    if phone and not valid_phone(phone):
        raise ValueError('手机号格式不正确')
    return db.update_phone(user_id, phone)


def verify_password(user_id, password):
    """校验当前用户口令（供改密前验证 old_password）。"""
    user = db.get_user_by_id(user_id)
    if not user:
        return False
    return check_password_hash(user['password_hash'] or '', password or '')
