#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/mailer.py  →  现位置: crypto/kline_training/mailer.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
邮件发送工具（邮箱验证码）
==========================
用 stdlib ``smtplib`` 发送验证码邮件，配置取自根级 ``config.SMTP_CONFIG``
（环境变量优先）。自托管小范围场景足够，无第三方依赖。

设计
----
- 支持 SSL(465) 与 STARTTLS(587) 两种连接方式（由 ``use_ssl`` 决定）。
- ``send_verify_code(email, code)`` 返回 ``(ok, err, dev_code)``：
  - ok=True  表示已成功投递到对方邮箱；dev_code 为 None。
  - ok=False 表示发送失败；当 ``config.EMAIL_DEV_FALLBACK`` 为真时，
    dev_code 回填验证码并打印到日志，便于本地无 SMTP 时仍能自测注册流程；
    否则 dev_code=None，调用方应提示「邮件发送失败」。
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import logging
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from crypto.kline_training import settings as config

logger = logging.getLogger(__name__)

_SUBJECT = 'K线训练营 · 邮箱验证码'


def _build_html(code, ttl_min):
    return (
        '<div style="font-family:-apple-system,Segoe UI,Microsoft YaHei,'
        'sans-serif;max-width:480px;margin:0 auto;padding:24px;'
        'background:#0f1420;border-radius:10px;color:#e6e8ee">'
        '<h2 style="margin:0 0 8px;color:#4fc3f7">K线训练营</h2>'
        '<p style="margin:0 0 16px;color:#9aa0aa">你正在注册 / 登录，'
        '本次验证码为：</p>'
        '<div style="font-size:32px;letter-spacing:6px;font-weight:700;'
        'text-align:center;color:#26a69a;background:#1a1e2e;'
        'border-radius:8px;padding:14px 0;margin:0 0 16px">'
        '{code}</div>'
        '<p style="margin:0;color:#9aa0aa;font-size:13px">'
        '验证码 <strong>{ttl}</strong> 分钟内有效，请勿泄露给他人。'
        '若非本人操作，请忽略本邮件。</p>'
        '</div>'
    ).format(code=code, ttl=ttl_min)


def send_verify_code(to_email, code):
    """发送验证码邮件。返回 (ok, err, dev_code)。"""
    cfg = config.SMTP_CONFIG
    from_email = cfg['from_email']
    password = cfg['password']
    host = cfg['smtp_host']
    port = cfg['smtp_port']
    use_ssl = cfg['use_ssl']
    ttl_min = max(1, int(config.EMAIL_CODE_TTL // 60))

    msg = MIMEText(_build_html(code, ttl_min), 'html', 'utf-8')
    msg['Subject'] = Header(_SUBJECT, 'utf-8')
    msg['From'] = formataddr((str(Header('K线训练营', 'utf-8')), from_email))
    msg['To'] = to_email

    def _dev_fallback(err):
        logger.warning('邮件发送失败(%s)：code=%s -> %s', err, code, to_email)
        if config.EMAIL_DEV_FALLBACK:
            return False, str(err), code     # 开发兜底：回传验证码
        return False, str(err), None

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls(context=ssl.create_default_context())
        try:
            server.login(from_email, password)
            server.sendmail(from_email, [to_email], msg.as_string())
        finally:
            server.quit()
        return True, None, None
    except Exception as e:                    # 任何 SMTP 异常都走兜底
        # 完整堆栈落日志（stderr 重定向至 flask_err.log），便于定位 SSL/授权码/频控
        logger.exception('[SMTP] 发送验证码异常 to=%s host=%s port=%s ssl=%s',
                         to_email, host, port, use_ssl)
        return _dev_fallback('%s: %s' % (type(e).__name__, e))
