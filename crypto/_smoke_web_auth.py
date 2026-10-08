#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：Web 访问闸门 crypto/web_auth.py（审计问题#1）
======================================================

覆盖点：
1. check_access 判定矩阵（未配口令=只放行回环；配口令=回环也要过口令，
   因为 nginx 反代下 remote_addr 全是 127.0.0.1，豁免回环等于没闸门）
2. cookie 值是 HMAC 而非口令本身，且跨进程稳定（重启不掉登录）
3. is_loopback / safe_next（开放重定向）/ 口令来源优先级与文件回退
4. 真接线：自建极小 Flask app + test_client，验 403/401/登录页/发 cookie/
   SameSite=Lax/CSRF 面、?token= 引导后从地址栏抹掉口令、Bearer 头、登出

安全边界：全程不导入 crypto.app（那会拉起调度器与 DB 预热），口令用临时目录
与环境变量喂进去，不读写仓库里真实的 data/web_token.txt。

约定：本脚本用「收集全部失败再退出」的 check()，不用首次失败即 sys.exit 的
ok() —— 后者会让后面的用例从没跑到过（SMOKE_TESTS.md 里记过这个坑）。
"""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from crypto import web_auth as wa  # noqa: E402

TMP_DIR = tempfile.mkdtemp(prefix='smoke_web_auth_')
failures = []


def check(name, cond, detail=''):
    if not cond:
        failures.append(name)
    print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


def set_token(token):
    """把口令设成指定值：走真实解析路径（环境变量优先，其次外置数据目录）"""
    if token:
        os.environ['CRYPTO_WEB_TOKEN'] = token
    else:
        os.environ.pop('CRYPTO_WEB_TOKEN', None)
    os.environ['CRYPTO_PLAN_DATA_DIR'] = TMP_DIR  # 空目录 → 文件回退一定读不到
    return wa.configured_token(refresh=True)


# ============ 1. 判定矩阵（纯函数，不碰 Flask） ============
print('\n=== 1. check_access 判定矩阵 ===')
set_token('')
check('1a 未配口令+本机 → 放行', wa.check_access('', '127.0.0.1', '/') == (True, ''))
check('1b 未配口令+远程 → 403', wa.check_access('', '10.1.2.3', '/')[1] == 'no_remote')
check('1c 未配口令+地址解析不了 → 当远程',
      wa.check_access('', '', '/api/task/trading/start')[1] == 'no_remote')

set_token('s3cret-token')
check('1d 配口令+无凭证 → 要口令', wa.check_access('s3cret-token', '1.1.1.1', '/')[1] == 'need_token')
check('1e 配口令+本机+无凭证 → 仍要口令（回环不豁免）',
      wa.check_access('s3cret-token', '127.0.0.1', '/')[1] == 'need_token')
good_cookie = wa.expected_cookie('s3cret-token')
check('1f cookie 对 → 放行', wa.check_access('s3cret-token', '1.1.1.1', '/', cookie=good_cookie) == (True, ''))
check('1g cookie 错 → 要口令',
      wa.check_access('s3cret-token', '1.1.1.1', '/', cookie='deadbeef')[1] == 'need_token')
check('1h cookie 里塞口令原文也不认（必须是 HMAC 值）',
      wa.check_access('s3cret-token', '1.1.1.1', '/', cookie='s3cret-token')[1] == 'need_token')
check('1i Bearer 头对 → 放行',
      wa.check_access('s3cret-token', '1.1.1.1', '/api/x',
                      auth_header='Bearer s3cret-token') == (True, ''))
check('1j Bearer 大小写混用也认',
      wa.check_access('s3cret-token', '1.1.1.1', '/api/x',
                      auth_header='bearer s3cret-token') == (True, ''))
check('1k ?token= 对 → bootstrap（要落 cookie）',
      wa.check_access('s3cret-token', '1.1.1.1', '/', query_token='s3cret-token')[1] == 'bootstrap')
check('1l ?token= 错 → 要口令',
      wa.check_access('s3cret-token', '1.1.1.1', '/', query_token='nope')[1] == 'need_token')
check('1m 静态资源免鉴权（远程未配口令也放行）',
      set_token('') or wa.check_access('', '10.0.0.9', '/static/js/main.js') == (True, ''))
check('1n 登录入口本身免鉴权（否则永远进不去）',
      wa.check_access('', '10.0.0.9', '/auth/gate') == (True, ''))

# ============ 2. cookie 值与口令解析 ============
print('\n=== 2. cookie 派生与口令来源 ===')
check('2a cookie 值不等于口令本身', wa.expected_cookie('abc') != 'abc')
check('2b 同口令派生稳定（重启不掉登录）', wa.expected_cookie('abc') == wa.expected_cookie('abc'))
check('2c 不同口令派生不同', wa.expected_cookie('abc') != wa.expected_cookie('abd'))
check('2d 环境变量优先于文件', (
    os.environ.update({'CRYPTO_WEB_TOKEN': 'from-env'}),
    open(wa._token_file_path(), 'w', encoding='utf-8').write('from-file\n'),
    wa.configured_token(refresh=True) == 'from-env')[-1])
check('2e 无环境变量时回落 web_token.txt', (
    os.environ.pop('CRYPTO_WEB_TOKEN', None),
    wa.configured_token(refresh=True) == 'from-file')[-1])
os.remove(wa._token_file_path())
check('2f 都没有 → 空串（进而走"只放行本机"）', wa.configured_token(refresh=True) == '')
check('2g 回环判定 127.x/::1', wa.is_loopback('127.0.0.1') and wa.is_loopback('::1'))
check('2h 局域网/公网不当本机',
      not wa.is_loopback('192.168.1.7') and not wa.is_loopback('8.8.8.8'))
check('2i 垃圾地址不当本机', not wa.is_loopback('not-an-ip') and not wa.is_loopback(None))
check('2j 开放重定向只认站内相对路径', wa.safe_next('//evil.com') == '/'
      and wa.safe_next('http://evil.com') == '/' and wa.safe_next('') == '/')
check('2k 正常 next 保留', wa.safe_next('/plan?tab=1') == '/plan?tab=1')

# ============ 3. 真接线（极小 Flask app，不碰 crypto.app） ============
print('\n=== 3. Flask 接线行为 ===')
from flask import Flask, jsonify  # noqa: E402

app = Flask(__name__)


@app.route('/')
def _page():
    return 'INDEX'


@app.route('/api/task/trading/force-close', methods=['POST', 'GET'])
def _money():
    return jsonify({'code': 200, 'message': '这里会真的下单', 'data': None})


wa.init_app(app)
c = app.test_client()

# 3-1 未配口令：远程访问下单接口必须被挡
set_token('')
r = c.get('/api/task/trading/force-close', environ_base={'REMOTE_ADDR': '203.0.113.9'})
check('3a 未配口令+远程 → 403 且没进业务函数', r.status_code == 403, f'code={r.status_code}')
check('3b 403 响应里没执行下单', b'\xe7\x9c\x9f\xe7\x9a\x84\xe4\xb8\x8b\xe5\x8d\x95' not in r.data)
r = c.get('/', environ_base={'REMOTE_ADDR': '203.0.113.9', 'HTTP_ACCEPT': 'text/html'})
check('3c 未配口令+远程页面 → 403 带指路文案',
      r.status_code == 403 and 'CRYPTO_WEB_TOKEN' in r.get_data(as_text=True))
r = c.get('/', environ_base={'REMOTE_ADDR': '127.0.0.1'})
check('3d 未配口令+本机 → 照常用', r.status_code == 200 and r.data == b'INDEX')

# 3-2 配口令：登录流程
set_token('gate-pass')
r = c.get('/api/task/trading/force-close', environ_base={'REMOTE_ADDR': '203.0.113.9'})
check('3e 配口令+远程无凭证 → 401 JSON', r.status_code == 401 and r.get_json()['code'] == 401)
r = c.get('/', environ_base={'REMOTE_ADDR': '203.0.113.9', 'HTTP_ACCEPT': 'text/html'})
check('3f 配口令+浏览器未登录 → 401 出登录页',
      r.status_code == 401 and 'action="/auth/gate"' in r.get_data(as_text=True))
r = c.post('/auth/gate', data={'token': 'wrong', 'next': '/'},
           environ_base={'REMOTE_ADDR': '203.0.113.9'})
check('3g 口令错 → 401 提示且不发 cookie',
      r.status_code == 401 and '口令不对' in r.get_data(as_text=True)
      and wa.COOKIE_NAME not in (r.headers.get('Set-Cookie') or ''))
r = c.post('/auth/gate', data={'token': 'gate-pass', 'next': '/api/x'},
           environ_base={'REMOTE_ADDR': '203.0.113.9'})
sc = r.headers.get('Set-Cookie') or ''
check('3h 口令对 → 302 跳回原页并下发 cookie',
      r.status_code == 302 and r.headers.get('Location') == '/api/x', sc[:60])
check('3i cookie 为 HttpOnly + SameSite=Lax（挡 CSRF 跨站 POST）',
      'httponly' in sc.lower() and 'samesite=lax' in sc.lower())
check('3j cookie 值是 HMAC 不含口令原文',
      'gate-pass' not in sc and wa.expected_cookie('gate-pass') in sc)
c.delete_cookie(wa.COOKIE_NAME)  # test_client 有 cookie jar，不清就会带着上一步的登录态
r = c.get('/api/task/trading/force-close', environ_base={'REMOTE_ADDR': '203.0.113.9'})
check('3k 登录后新会话不带 cookie 仍被挡（放行只认 cookie，不是来过一次就放开）',
      r.status_code == 401, f'code={r.status_code}')
c.set_cookie(wa.COOKIE_NAME, wa.expected_cookie('gate-pass'))
r = c.get('/api/task/trading/force-close', environ_base={'REMOTE_ADDR': '203.0.113.9'})
check('3l 带正确 cookie → 放行', r.status_code == 200, f'code={r.status_code}')
r = c.get('/', environ_base={'REMOTE_ADDR': '203.0.113.9'})
check('3m cookie 有效时页面也直接进（不再弹登录页）', r.status_code == 200)

# 3-3 ?token= 引导与登出
c.delete_cookie(wa.COOKIE_NAME)
r = c.get('/?token=gate-pass', environ_base={'REMOTE_ADDR': '203.0.113.9'})
sc = r.headers.get('Set-Cookie') or ''
check('3n ?token= 引导 → 302 且把口令从地址栏抹掉',
      r.status_code == 302 and 'token=' not in (r.headers.get('Location') or ''),
      r.headers.get('Location'))
check('3o 引导同时下发 cookie（之后不用带口令）', wa.COOKIE_NAME in sc)
c.delete_cookie(wa.COOKIE_NAME)
r = c.get('/?token=nope', environ_base={'REMOTE_ADDR': '203.0.113.9',
                                        'HTTP_ACCEPT': 'text/html'})
check('3p 口令给错在 query 里也不会放行', r.status_code == 401,
      f'code={r.status_code}')
# 已有有效 cookie 时，多余的错口令 query 不该把已登录用户踢回登录页
c.set_cookie(wa.COOKIE_NAME, wa.expected_cookie('gate-pass'))
r = c.get('/?token=nope', environ_base={'REMOTE_ADDR': '203.0.113.9',
                                        'HTTP_ACCEPT': 'text/html'})
check('3p2 已登录 + 错口令 query → 凭 cookie 照进', r.status_code == 200,
      f'code={r.status_code}')
r = c.get('/auth/logout', environ_base={'REMOTE_ADDR': '203.0.113.9'})
check('3q 登出接口可访问且清 cookie', r.status_code in (302, 200)
      and 'Max-Age=0' in (r.headers.get('Set-Cookie') or ''))

# 3-4 静态资源必须免鉴权，否则登录页样式/前端 JS 加载不出来
r = c.get('/static/nope.js', environ_base={'REMOTE_ADDR': '203.0.113.9'})
check('3r 静态资源不被闸门 401/403（Flask 自己 404 是正常的）',
      r.status_code not in (401, 403), f'code={r.status_code}')

# ============ 汇总 ============
print(f"\n{'全部通过' if not failures else '失败 ' + str(len(failures)) + ' 项: ' + ', '.join(failures)}")
try:
    os.environ.pop('CRYPTO_WEB_TOKEN', None)
    os.environ.pop('CRYPTO_PLAN_DATA_DIR', None)
    for f in os.listdir(TMP_DIR):
        os.remove(os.path.join(TMP_DIR, f))
    os.rmdir(TMP_DIR)
except OSError:
    pass
sys.exit(1 if failures else 0)
