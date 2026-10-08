#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Web 访问闸门（审计问题#1）
==========================

为什么要这一层
--------------
本项目 Flask 监听 0.0.0.0:7777 且**原先完全没有鉴权**，而 /api 下面挂着真实
下单、强平、启停实盘调度、改策略配置的接口（POST /api/task/trading/force-close
之类）。宝塔部署 + 手机访问意味着这些接口对整个网段可达：任何人打开
http://你的IP:7777 就能替你平仓、替你下单，页面上一句确认都不需要。

判定口径（重要，别改错方向）
---------------------------
口令来源二选一，优先级：环境变量 CRYPTO_WEB_TOKEN → 数据目录 web_token.txt
（与 db_url.txt 同一套外置目录约定，见 data_paths.resolve_data_file）。

  * **没配口令**：只放行回环地址（127.0.0.1/::1），其余一律 403。
    即"忘配口令"的默认结果是远程不可用，而不是远程无防护 —— fail-safe。
  * **配了口令**：所有来源（含回环）都要过口令。不对回环网开一面，是因为
    宝塔/nginx 反代下所有请求的 remote_addr 都是 127.0.0.1，放行回环等于
    形同虚设。

口令怎么带（三种都支持，为的是"人肉用着不别扭"）
  1. 浏览器首次访问被拦 → 内置登录页输一次 → 下发签名 cookie（30 天）；
  2. 网址后加 ?token=口令 → 校验通过后 302 去掉 query 并顺带发 cookie（手机
     不想打字时用）；
  3. Authorization: Bearer <口令> → 脚本/CLI 用，不走 cookie。

cookie 里存的是 HMAC(salt, 口令) 而不是口令本身：口令泄漏面只剩"配置文件 + 传输"，
被翻到 cookie 也还原不出口令。SameSite=Lax 是为了挡 CSRF —— 否则有了 cookie 之后，
别的网站可以拿用户的浏览器发跨站 POST 替她下单。

边界
----
- 只管 HTTP 入口。进程内部直接调函数（调度器/监控线程）不经过这里，不受影响。
- 不做账号体系、不做权限分级：这是单人试验场，一把口令足够。
- 不改动 OKX 侧凭据，两回事。
"""

import hashlib
import hmac
import ipaddress
import os

# ---------------------------------------------------------------- 常量与开关

COOKIE_NAME = 'ct_web_gate'
COOKIE_TTL_SECONDS = 30 * 24 * 3600
_SALT = b'cryptoTrade-web-gate-v1'
_TRUE = ('1', 'true', 'yes', 'on')

# 免鉴权路径：静态资源、图标、登录处理本身（登录页不能要求先登录）
_ALWAYS_OPEN = {'/favicon.ico', '/robots.txt', '/auth/gate', '/auth/logout'}

_token_cache = {'value': None}


def configured_token(refresh: bool = False) -> str:
    """读取访问口令：环境变量优先，其次数据目录 web_token.txt；都没有返回 ''。

    结果按进程缓存（改口令后重启生效）；文件读取失败也按"未配置"处理，
    宁可远程打不开，也不要因为读文件报错就把接口敞开。
    """
    if refresh:
        _token_cache['value'] = None
    if _token_cache['value'] is not None:
        return _token_cache['value']
    token = str(os.environ.get('CRYPTO_WEB_TOKEN', '') or '').strip()
    if not token:
        try:
            path = _token_file_path()
            if os.path.isfile(path):
                with open(path, encoding='utf-8') as f:
                    token = f.readline().strip()
        except OSError:
            token = ''
    _token_cache['value'] = token
    return token


def _token_file_path() -> str:
    """口令文件路径：走项目统一的外置数据目录约定（与 db_url.txt 一致）"""
    try:
        from .data_paths import resolve_data_file
    except ImportError:  # 脚本身份直跑
        from data_paths import resolve_data_file
    return resolve_data_file('web_token.txt')


def expected_cookie(token: str) -> str:
    """口令对应的 cookie 值（HMAC，不回显口令本身）"""
    return hmac.new(_SALT, token.encode('utf-8'), hashlib.sha256).hexdigest()


def is_loopback(ip: str) -> bool:
    """是否回环地址；解析不出来一律算"不是本机"（偏保守）"""
    try:
        return ipaddress.ip_address((ip or '').strip()).is_loopback
    except ValueError:
        return False


def _bearer(header: str) -> str:
    header = str(header or '').strip()
    if header[:7].lower() == 'bearer ':
        return header[7:].strip()
    return ''


def check_access(token: str, ip: str, path: str, cookie: str = '',
                 auth_header: str = '', query_token: str = '') -> tuple:
    """纯判定函数（不依赖 Flask，便于离线冒烟）。

    返回 (allow, mode)：
      mode=''          放行
      mode='need_token'需要口令且本次请求没带对 → 出登录页 / 401
      mode='bootstrap'  ?token= 带对了 → 发 cookie 后 302 去掉 query
      mode='no_remote' 未配口令且来源非本机 → 403
    """
    path = path or '/'
    if path in _ALWAYS_OPEN or path.startswith('/static/'):
        return True, ''

    presented = _bearer(auth_header) or (query_token or '').strip()
    if not token:
        # 未配置口令：只允许本机
        return (True, '') if is_loopback(ip) else (False, 'no_remote')

    if presented == token:
        # query 方式要落 cookie（否则每次点链接都得带口令，也难记）
        return (True, 'bootstrap') if (query_token or '').strip() == token \
            else (True, '')
    if cookie and hmac.compare_digest(cookie, expected_cookie(token)):
        return True, ''
    return False, 'need_token'


def safe_next(raw: str) -> str:
    """登录后要跳回的地址：只接受站内相对路径，挡开放重定向"""
    nxt = str(raw or '').strip()
    if not nxt or not nxt.startswith('/') or nxt.startswith('//'):
        return '/'
    return nxt


def _wants_json(path: str, accept: str) -> bool:
    """/api 下、或明确只收 JSON 的请求 → 返回 JSON；否则返回 HTML 登录页"""
    if path.startswith('/api/'):
        return True
    return 'text/html' not in (accept or '') and '*/*' not in (accept or '')


_DENY_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>访问验证 · LifeQuant</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1419;color:#d7dde3;font:14px/1.7 "Microsoft YaHei",system-ui,sans-serif}}
.card{{background:#171d24;border:1px solid #26313b;border-radius:12px;padding:28px 32px;
width:min(90vw,380px);box-shadow:0 12px 40px rgba(0,0,0,.45)}}
h1{{margin:0 0 6px;font-size:17px;color:#fff}}
p{{margin:0 0 18px;color:#8b98a5;font-size:12.5px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;
border:1px solid #2f3d49;background:#0f1419;color:#e6ebf0;font-size:14px}}
button{{width:100%;margin-top:12px;padding:10px;border:0;border-radius:8px;
background:#2f81f7;color:#fff;font-size:14px;cursor:pointer}}
.err{{color:#f85149;font-size:12.5px;margin-top:12px;min-height:18px}}
</style></head><body>
<div class="card">
  <h1>🔒 {title}</h1>
  <p>{hint}</p>
  {form}
  <div class="err">{err}</div>
</div>
</body></html>
"""


def _gate_page(err: str = '', next_path: str = '/', token_configured: bool = True):
    """内置登录页（不建模板文件，闸门自包含）。带口令时渲染表单，否则只给提示。"""
    if token_configured:
        form = ('<form method="post" action="/auth/gate">'
                '<input name="token" type="password" placeholder="访问口令" autofocus>'
                '<input name="next" type="hidden" value="'
                + safe_next(next_path).replace('"', '&quot;') + '">'
                '<button type="submit">进入</button></form>')
        title, hint = '访问验证', '这是实盘交易面板，需要口令。'
        status = 401
    else:
        form = ''
        title = '该服务只允许本机访问'
        hint = ('服务器没有配置访问口令，因此从其它设备（手机/局域网）打不开。'
                '想远程用：在服务器上设置环境变量 CRYPTO_WEB_TOKEN，'
                '或把口令写进 data/web_token.txt，然后重启进程。')
        status = 403
    return _DENY_PAGE.format(title=title, hint=hint, form=form, err=err), status


def init_app(app):
    """把闸门挂到 Flask 应用上：一个 before_request + 登录/登出两个路由。

    必须在所有蓝图注册之后调用，保证 /api、/plan、/calorie 等全部入口都在
    覆盖范围内（before_request 对蓝图路由同样生效）。
    """
    from flask import request, redirect, make_response, jsonify

    @app.before_request
    def _web_gate():
        token = configured_token()
        allow, mode = check_access(
            token=token,
            ip=request.remote_addr,
            path=request.path,
            cookie=request.cookies.get(COOKIE_NAME, ''),
            auth_header=request.headers.get('Authorization', ''),
            query_token=request.args.get('token', ''),
        )
        if allow and mode != 'bootstrap':
            return None
        if allow:  # ?token= 校验通过 → 发 cookie 并把口令从地址栏抹掉
            resp = make_response(redirect(safe_next(request.args.get('next'))))
            resp.set_cookie(COOKIE_NAME, expected_cookie(token),
                            max_age=COOKIE_TTL_SECONDS, httponly=True,
                            samesite='Lax')
            return resp
        want_json = _wants_json(request.path, request.headers.get('Accept', ''))
        if want_json:
            if mode == 'no_remote':
                return jsonify({'code': 403, 'data': None, 'message':
                                '服务端未配置访问口令，只允许本机访问（详见 '
                                'crypto/web_auth.py 说明）'}), 403
            return jsonify({'code': 401, 'data': None,
                            'message': '需要访问口令：请在浏览器打开首页登录，'
                                       '或带上 Authorization: Bearer <口令>'}), 401
        page, status = _gate_page(err='', next_path=request.path or '/',
                                  token_configured=bool(token))
        return make_response(page), status

    @app.route('/auth/gate', methods=['POST'])
    def _web_gate_login():
        form = request.form if request.form else request.get_json(silent=True)
        token = configured_token()
        nxt = safe_next((form or {}).get('next'))
        if not token:
            page, status = _gate_page(err='服务端未配置口令', next_path=nxt,
                                      token_configured=False)
            return make_response(page), status
        if not hmac.compare_digest(str((form or {}).get('token') or '').strip(), token):
            page, status = _gate_page(err='口令不对', next_path=nxt)
            return make_response(page), status
        resp = make_response(redirect(nxt))
        resp.set_cookie(COOKIE_NAME, expected_cookie(token),
                        max_age=COOKIE_TTL_SECONDS, httponly=True, samesite='Lax')
        return resp

    @app.route('/auth/logout', methods=['GET', 'POST'])
    def _web_gate_logout():
        resp = make_response(redirect('/'))
        resp.delete_cookie(COOKIE_NAME)
        return resp
