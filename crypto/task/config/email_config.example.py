#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
邮箱配置模板 —— 真实文件 crypto/task/config/email_config.py 不入库（含 SMTP 授权码）

两种生成方式，任选其一：
  1. 推荐：启动 Web 后在「任务系统 → 邮件配置」页面填一次并保存，
     app.py 的 POST /api/task/config/email 会整体重写 email_config.py（含本模板全部函数）；
  2. 手工：把本文件复制为同目录 email_config.py，替换授权码占位符。

    copy email_config.example.py email_config.py
"""

EMAIL_CONFIG = {
    'from_email':  'your_smtp_account@example.com',   # 发件人邮箱
    'password':    'your_smtp_authorization_code',    # 邮箱授权码（QQ/163 均非登录密码）
    'smtp_host':   'smtp.qq.com',
    'smtp_port':   465,                               # 465=SSL, 587=TLS
    'use_ssl':     True,
}

# 收件人（各类告警、日报、复盘信都发这一个地址）
ADMIN_EMAIL = 'admin@example.com'


def get_email_config() -> dict:
    """返回 SMTP 配置副本"""
    return EMAIL_CONFIG.copy()


def get_admin_email() -> str:
    """返回管理员邮箱地址"""
    return ADMIN_EMAIL
