# -*- coding: utf-8 -*-
"""
合并模块冒烟公共夹具（futureStockTrade 合并迁移 Phase 6）
========================================================
供 ``crypto/_smoke_merge_{futures,stocks,kline}.py`` 复用。

为什么单独一个夹具：三个模块并入的是**同一个宿主进程**，冒烟必须在「宿主已
import 完成」的状态下检查蓝图注册、路由前缀、模板解析与闸门覆盖；而宿主
``crypto.app`` 在 import 阶段就会拉起调度器、监控线程并读真实 OKX 密钥。三个脚本
各起一次进程又都必须先做同一套隔离设置，逻辑重复且容易漏，因此收口到这里。

安全边界（三条硬约束，改动本文件前先读）：
1. ``CRYPTO_NO_BACKGROUND=1`` —— 绝不拉起交易调度器与告警监控（真下单、真发信）。
2. ``CRYPTO_WEB_TOKEN`` 用测试口令覆盖环境变量，且 ``CRYPTO_DATA_DIR`` 指向临时目录，
   冒烟不读写仓库里真实的 ``data/web_token.txt``、``data/app_secret_key.txt``、
   ``kline_training.db``。
3. 只发 GET 与显式列出的安全 POST，绝不碰下单/撤单/改配置类接口。

注意：``CRYPTO_*`` 这些环境变量必须在 **import crypto.app 之前** 设置——模块级常量
（SQLite 路径、口令、开关）都在导入期求值，晚了就改不动。
"""
import os
import sys
import tempfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

TEST_TOKEN = 'merge-smoke-token'


def bootstrap_env(extra_env=None, fresh_data_dir=True):
    """设置隔离环境变量（必须在导入 crypto.app 之前调用）。"""
    os.environ['CRYPTO_NO_BACKGROUND'] = '1'
    os.environ['CRYPTO_WEB_TOKEN'] = TEST_TOKEN
    if fresh_data_dir:
        # 外置数据目录指向临时目录：app_secret_key.txt / kline_training.db
        # 都会在这里新建，绝不碰真实 data/
        tmp = tempfile.mkdtemp(prefix='fst_merge_smoke_')
        os.environ['CRYPTO_DATA_DIR'] = tmp
        os.environ['CRYPTO_PLAN_DATA_DIR'] = tmp
        os.environ['CRYPTO_KLINE_SQLITE_PATH'] = os.path.join(tmp, 'kline_smoke.db')
    for k, v in (extra_env or {}).items():
        os.environ[k] = v


def load_app():
    """导入宿主 app（此时三个合并模块已按当前环境变量注册/降级）。"""
    from crypto.app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app


def client(app):
    """返回已通过访问闸门的 test_client（?token= 引导，与真实浏览器同路径）。"""
    c = app.test_client()
    r = c.get('/?token=' + TEST_TOKEN, follow_redirects=True)
    if r.status_code != 200 or '访问验证' in r.get_data(as_text=True):
        raise AssertionError(f'冒烟无法通过访问闸门: status={r.status_code}')
    return c


def ck(cond, label, detail=''):
    if not cond:
        raise AssertionError(f'{label} 失败: {detail}')
    print(f'  [ok] {label}')


def under_prefix(rule_str, prefix):
    """路径段级前缀判断。

    必须按「段」而不是按字符串 startswith：宿主的 ``/kline-live`` 与合并进来的
    ``/kline/`` 只差一个连字符，裸 startswith 会把前者算进后者，冒烟因此误报
    「关掉 K 线训练后仍有 /kline 残留路由」。
    """
    p = prefix.rstrip('/')
    return rule_str == p or rule_str.startswith(p + '/')


def rules_for(app, prefix):
    """列出某前缀下的全部路由规则（排序后便于比对）。"""
    return sorted(str(r) for r in app.url_map.iter_rules()
                  if under_prefix(str(r), prefix))


def has_rule(app, rule_str, endpoint=None):
    for r in app.url_map.iter_rules():
        if str(r) == rule_str and (endpoint is None or r.endpoint == endpoint):
            return True
    return False


def resolve_template_dir(app, name):
    """模板名实际解析到的目录（用于抓跨目录遮蔽）。"""
    t = app.jinja_env.get_template(name)
    return os.path.normpath(os.path.dirname(getattr(t, 'filename', '') or ''))


def section(title):
    print('\n' + '=' * 70)
    print(title)
    print('=' * 70)
