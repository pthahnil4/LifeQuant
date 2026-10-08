#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读 HTTP 客户端（P3 · 运行态全景）
======================================
调度可视化 / 实盘只读监控的数据源是**交易系统 Web 进程**本身，只能经 HTTP 只读
获取。本模块是运维站访问这些接口的唯一出口，严守红线：

- 只发 GET，绝不触碰任何下单/平仓/启停（POST）接口；
- 绝不 import crypto 任何模块（口令解析在本模块用 stdlib 自行完成，口径与
  crypto/web_auth.py 同构：环境变量优先 → 数据目录 web_token.txt 首行）；
- 任何异常都不外抛，统一收敛成结构化结果，页面据此优雅降级；
- 超时短、结果由上层 TTL 缓存，克制对 OKX 的读频。

返回统一信封 dict：
  {ok, status, kind, data, message, endpoint, elapsed_ms, http_code}
  kind ∈ 'ok' 取到 data
       'offline' 连不上（交易 Web 未启动 / 端口不对 / 超时）
       'auth'    被访问闸门拦下（401/403：口令缺失或不符，或远程且未配口令）
       'error'   连上了但接口回了非 200（业务错误 / 解析失败）
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config as C

_UA = 'OpsCenter-readonly-monitor/1.0'


def read_token():
    """读取访问口令：环境变量 CRYPTO_WEB_TOKEN 优先，其次数据目录 web_token.txt 首行。

    与交易系统 web_auth.configured_token() 同构，但**不 import 它**——运维站与交易
    系统物理隔离，只共享同一份外置配置文件。取不到返回 ''（未配口令）。
    """
    token = str(os.environ.get(C.WEB_TOKEN_ENV, '') or '').strip()
    if token:
        return token
    try:
        if os.path.isfile(C.WEB_TOKEN_FILE):
            with open(C.WEB_TOKEN_FILE, encoding='utf-8') as f:
                return f.readline().strip()
    except OSError:
        pass
    return ''


def _envelope(endpoint, ok, kind, http_code=None, data=None, message='', started=None):
    return {
        'ok': ok,
        'kind': kind,
        'status': kind,                       # 语义别名，模板读起来更直观
        'data': data,
        'message': message,
        'endpoint': endpoint,
        'http_code': http_code,
        'elapsed_ms': int((time.time() - started) * 1000) if started else 0,
    }


def api_get(name, params=None):
    """按端点名（C.API_ENDPOINTS 的 key）发起一次只读 GET，返回统一信封，绝不抛。"""
    path = C.API_ENDPOINTS.get(name, name if str(name).startswith('/') else '')
    endpoint = path or str(name)
    started = time.time()
    if not path:
        return _envelope(endpoint, False, 'error', message='未知端点：%s' % name, started=started)

    url = C.TRADING_API_BASE + path
    if params:
        url += '?' + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})

    headers = {'Accept': 'application/json', 'User-Agent': _UA}
    token = read_token()
    if token:
        headers['Authorization'] = 'Bearer ' + token        # 配了口令时，回环也要过闸门

    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=C.API_TIMEOUT_SEC) as resp:
            body = resp.read()
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        # 4xx/5xx：web_auth 拦下会是 401/403（JSON），交易系统内部错误是 500
        try:
            err_body = e.read().decode('utf-8', errors='replace')
        except Exception:
            err_body = ''
        parsed = None
        try:
            parsed = json.loads(err_body) if err_body else None
        except ValueError:
            parsed = None
        msg = (parsed or {}).get('message') if isinstance(parsed, dict) else (err_body[:200] or str(e))
        kind = 'auth' if e.code in (401, 403) else 'error'
        return _envelope(endpoint, False, kind, http_code=e.code, message=msg or ('HTTP %s' % e.code),
                         started=started)
    except urllib.error.URLError as e:
        reason = getattr(e, 'reason', e)
        return _envelope(endpoint, False, 'offline', message='无法连接 %s（%s）' % (C.TRADING_API_BASE, reason),
                         started=started)
    except Exception as e:                              # socket.timeout / 其它，一律兜住
        return _envelope(endpoint, False, 'offline', message='请求异常：%s' % e, started=started)

    try:
        payload = json.loads(body.decode('utf-8', errors='replace'))
    except ValueError:
        return _envelope(endpoint, False, 'error', http_code=code, message='响应不是合法 JSON', started=started)

    if not isinstance(payload, dict):
        return _envelope(endpoint, False, 'error', http_code=code, message='响应结构异常', started=started)

    # 交易系统统一信封 {code, message, data}；成功 code==200
    api_code = payload.get('code')
    if api_code == 200:
        return _envelope(endpoint, True, 'ok', http_code=code, data=payload.get('data'), started=started)
    return _envelope(endpoint, False, 'error', http_code=code, data=payload.get('data'),
                     message=str(payload.get('message') or ('接口返回 code=%s' % api_code)), started=started)


def probe():
    """轻量探活：只判断交易系统 Web 是否可达（供页面顶部一致性提示，带缓存）。"""
    return api_get('account_info')
