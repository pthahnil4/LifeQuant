# -*- coding: utf-8 -*-
"""
冒烟测试：K 线训练模块并入宿主应用（/kline 蓝图）
==============================================
K 线训练是三个合并模块里唯一带**自有登录态与自有数据库**的：
宿主鉴权走 web_auth 的签名无关 Cookie，K 线训练走 Flask 签名 session
（键名 ``kline_uid``）。两套身份体系在同一进程里共存（决策 D2），因此本冒烟
除了路由/模板，还专门验「会话键不互相污染」和「密钥不写死」。

覆盖场景（全程离线，数据目录与 SQLite 都指向临时目录，绝不碰真实 data/）：
  1. 蓝图注册与闸门顺序：/kline/** 受同一访问闸门保护
  2. 路由条数与 /kline 前缀
  3. 模板解析归属（kline_* / pk_* / auth.html 落在训练目录）
  4. app.secret_key 非空、来自外置文件/环境变量，且**不是**代码里的写死串
  5. 会话键隔离：session['kline_uid'] 不影响宿主 web_auth 判定
  6. SQLite 隔离：DB_PATH 落在临时目录，init_db 建表成功，真实库未被打开
  7. 页面可达：/kline/ 未登录给登录入口，已登录给训练台
  8. 加密货币 provider 走 OKX **公共**行情（链路里不得出现 api_config 密钥引用）
运行：python -m crypto._smoke_merge_kline
      python -m crypto._smoke_merge_kline --off
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from crypto import _smoke_merge_helper as H

# 原项目 /kline 路由基线：54 条（见迁移文档「Phase 4 · 路由表 A/B」）
BASELINE_RULES = 54


def main_off():
    H.bootstrap_env()
    H.ck(os.environ.get('FST_ENABLE_KLINE') == '0', '开关已置于关闭态')
    app = H.load_app()
    client = H.client(app)

    H.section('关闭态断言（FST_ENABLE_KLINE=0）')
    H.ck('kline' not in app.blueprints, 'K 线训练蓝图未注册')
    from crypto.app import _FST_MODULES
    H.ck(_FST_MODULES['kline']['status'] == 'disabled', '注册状态记为 disabled')
    r = client.get('/kline/')
    H.ck(r.status_code == 503, f'关闭态访问 /kline/ 返回 503（{r.status_code}）')
    H.ck('FST_ENABLE_KLINE' in r.get_data(as_text=True), '提示页给出环境变量名')
    # 关掉训练模块后，宿主不应残留任何 /kline 路由（避免半挂载）
    H.ck(len(H.rules_for(app, '/kline')) <= 2,
         f'无残留 /kline 业务路由（实际 {H.rules_for(app, "/kline")}）')
    H.ck(client.get('/').status_code == 200, '宿主首页正常')
    print('\n[PASS] K 线训练关闭态冒烟通过')


def main():
    H.bootstrap_env({'FST_ENABLE_KLINE': '1'})
    app = H.load_app()
    client = H.client(app)

    H.section('1. 蓝图注册与闸门顺序')
    H.ck('kline' in app.blueprints, 'K 线训练蓝图已注册')
    rules = H.rules_for(app, '/kline')
    H.ck(len(rules) >= BASELINE_RULES,
         f'路由条数不低于基线 {BASELINE_RULES}（实际 {len(rules)} 条）')
    H.ck(all(r.startswith('/kline') for r in rules), '路由全部带 /kline 前缀')
    anon = app.test_client()
    H.ck(anon.get('/kline/').status_code in (401, 403), '未过闸门访问 /kline/ 被拦')

    H.section('2. 模板解析归属')
    for name in ('kline_index.html', 'kline_train.html', 'auth.html', 'pk_index.html'):
        d = H.resolve_template_dir(app, name)
        H.ck(d.replace('\\', '/').endswith('crypto/kline_training/templates'),
             f'{name} 解析到训练模块目录（{d}）')

    H.section('3. 会话签名密钥：必须有值，且不是写死的字符串')
    from crypto.kline_training import settings as ks
    H.ck(bool(app.secret_key) and len(str(app.secret_key)) >= 32,
         'app.secret_key 已配置且长度足够')
    H.ck(str(app.secret_key) == str(ks.SECRET_KEY), '蓝图密钥与 settings 取值一致')
    settings_src = open(os.path.join(os.path.dirname(__file__), 'kline_training',
                                     'settings.py'), encoding='utf-8').read()
    H.ck(not re.search(r"SECRET_KEY\s*=\s*['\"][^'\"]{3,}['\"]", settings_src),
         'settings.py 内不存在写死的 SECRET_KEY 字面量')
    key_file = os.path.join(os.environ['CRYPTO_PLAN_DATA_DIR'], 'app_secret_key.txt')
    H.ck(os.path.isfile(key_file), '会话密钥落在本次临时数据目录（未读写真实 data/）')

    H.section('4. SQLite 隔离与建表')
    from crypto.kline_training import store as kstore
    H.ck(str(os.environ['CRYPTO_KLINE_SQLITE_PATH']) == str(kstore.DB_PATH),
         f'SQLite 指向临时目录（{kstore.DB_PATH}）')
    H.ck('kline_training.db' not in str(kstore.DB_PATH),
         '未打开仓库里真实的 kline_training.db')
    kstore.init_db()
    H.ck(os.path.isfile(kstore.DB_PATH), '临时库已建表')
    conn = kstore.get_conn()
    n = conn.execute(
        "select count(*) from sqlite_master where type='table'").fetchone()[0]
    H.ck(n >= 8, f'表数量达标（{n} 张）')

    H.section('5. 会话键隔离（kline_uid 不污染宿主闸门）')
    from crypto import web_auth as wa
    H.ck(wa.COOKIE_NAME != 'session',
         f'闸门 cookie（{wa.COOKIE_NAME}）与 Flask session 不同名，两套身份互不覆盖')
    routes_src = open(os.path.join(os.path.dirname(__file__), 'kline_training',
                                   'routes.py'), encoding='utf-8').read()
    H.ck('kline_uid' in routes_src and "session['uid']" not in routes_src,
         '训练模块会话键已收口为 kline_uid，不再有裸 uid')
    with client.session_transaction() as sess:
        sess['kline_uid'] = 999001
        sess['kline_username'] = 'merge_smoke'
    H.ck(client.get('/').status_code == 200, '带 kline_uid 的会话仍可访问宿主首页')
    # 反向也要成立：只过宿主闸门（无 kline_uid）不应被当成 K 线已登录
    fresh = app.test_client()
    fresh.get('/?token=' + H.TEST_TOKEN, follow_redirects=True)
    r_k = fresh.get('/kline/')
    H.ck(r_k.status_code in (200, 302), f'宿主身份访问 /kline 不报 5xx（{r_k.status_code}）')
    body_k = r_k.get_data(as_text=True)
    H.ck('登录' in body_k or 'login' in body_k.lower() or r_k.status_code == 302,
         '未登录 K 线时页面给出登录入口而非训练台')

    H.section('6. 页面可达')
    r = client.get('/kline/')
    body = r.get_data(as_text=True)
    H.ck(r.status_code == 200, f'/kline/ 返回 200（{r.status_code}）')
    H.ck('main-nav' in body or 'navLinks' in body, '/kline/ 已注入宿主公共导航')
    H.ck('css/style.css' in body, '/kline/ 样式走宿主 style.css')

    H.section('7. 加密货币 provider 只走 OKX 公共行情')
    _kd = os.path.join(os.path.dirname(__file__), 'kline_training')
    for fname in ('providers/crypto_provider.py', 'kline_adapter.py'):
        p = os.path.join(_kd, fname)
        if not os.path.isfile(p):
            H.ck(False, f'{fname} 存在')
            continue
        text = open(p, encoding='utf-8').read()
        H.ck('api_config' not in text, f'{fname} 不引用宿主实盘密钥模块 api_config')
    adapter = open(os.path.join(_kd, 'kline_adapter.py'), encoding='utf-8').read()
    H.ck('market/candles' in adapter or 'public' in adapter,
         'kline_adapter 走 OKX 公共行情端点（无需密钥）')

    print('\n[PASS] K 线训练模块合并冒烟全部通过')


if __name__ == '__main__':
    if '--off' in sys.argv:
        os.environ['FST_ENABLE_KLINE'] = '0'
        main_off()
    else:
        main()
