# -*- coding: utf-8 -*-
"""
冒烟测试：期货模块并入宿主应用（/futures 蓝图）
=============================================
覆盖场景（全部离线，不访问 akshare，不触发任何写操作）：
  1. 蓝图在 web 闸门之前注册：闸门开启时 /futures/** 未登录一律 401
  2. 路由前缀完整：/futures 下的页面与 API 规则条数达标且带 /futures 前缀
  3. 模板解析归属：futures_star_market.html 解析到期货目录（遮蔽回归专项）
  4. 宿主 /star-market 仍解析宿主模板：合并不能反向吃掉原页面
  5. 页面可达 + 宿主 nav 注入：/futures/ 与 /futures/star-market 返回 200 且含导航
  6. 行情依赖未就绪时页面不 500（离线环境 akshare 取数失败要优雅降级）
  7. FST_ENABLE_FUTURES=0 时：蓝图不注册 + 前缀返回 503 提示页
运行：python -m crypto._smoke_merge_futures
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from crypto import _smoke_merge_helper as H


def main_off():
    """关闭态：必须在导入 crypto.app 之前把开关设成 0（模块级求值）。"""
    H.bootstrap_env()          # 只补隔离变量，不覆盖 FST_ENABLE_FUTURES
    H.ck(os.environ.get('FST_ENABLE_FUTURES') == '0', '开关已置于关闭态')
    app = H.load_app()
    client = H.client(app)

    H.section('关闭态断言（FST_ENABLE_FUTURES=0）')
    H.ck('futures' not in app.blueprints, '期货蓝图未注册')
    st = __import__('crypto.app', fromlist=['_FST_MODULES'])._FST_MODULES['futures']
    H.ck(st['status'] == 'disabled', f"注册状态记为 disabled（实际 {st['status']}）")
    r = client.get('/futures/')
    H.ck(r.status_code == 503, f'关闭态访问 /futures/ 返回 503（{r.status_code}）')
    body = r.get_data(as_text=True)
    H.ck('已按配置关闭' in body, '提示页写明「已按配置关闭」')
    H.ck('FST_ENABLE_FUTURES' in body, '提示页给出恢复用的环境变量名')
    ra = client.get('/futures/api/config')
    H.ck(ra.status_code == 503 and 'code' in ra.get_data(as_text=True),
         '关闭态 API 也回 503 JSON，不给裸 404')
    rmain = client.get('/')
    H.ck(rmain.status_code == 200, '关闭期货模块不影响宿主首页')
    print('\n[PASS] 期货模块关闭态冒烟通过')


def main():
    H.bootstrap_env({'FST_ENABLE_FUTURES': '1'})
    app = H.load_app()
    client = H.client(app)

    H.section('1. 蓝图注册与闸门顺序')
    H.ck('futures' in app.blueprints, '期货蓝图已注册')
    rules = H.rules_for(app, '/futures')
    # 27 = Phase 2 路由表 A/B 的基线（原项目 27 条 / 合并后 27 条，见
    # data/_fst_route_table/{old,new}.json）。条数只允许增加，减少即路由丢失。
    H.ck(len(rules) >= 27, f'路由条数不低于基线 27（实际 {len(rules)} 条）')
    H.ck(all(r.startswith('/futures') for r in rules), '路由全部带 /futures 前缀')
    for must in ('/futures/', '/futures/star-market', '/futures/detail/<symbol>'):
        H.ck(H.has_rule(app, must), f'关键路由存在: {must}')

    # 未登录必须被闸门拦下（用不带 cookie 的新 client）
    anon = app.test_client()
    r = anon.get('/futures/')
    H.ck(r.status_code in (401, 403), f'未过闸门访问 /futures/ 被拦（{r.status_code}）')

    H.section('2. 模板解析归属（跨目录遮蔽回归专项）')
    d = H.resolve_template_dir(app, 'futures_star_market.html')
    H.ck(d.replace('\\', '/').endswith('crypto/futures/templates'),
         f'futures_star_market.html 解析到期货目录（{d}）')
    d2 = H.resolve_template_dir(app, 'star_market.html')
    H.ck(d2.replace('\\', '/').endswith('crypto/templates'),
         f'宿主 star_market.html 仍解析宿主目录（{d2}）')
    H.ck('futures' not in d2, '宿主星标页没有被期货模板反向遮蔽')

    H.section('3. 页面可达与宿主导航注入')
    for path, marker in (('/futures/', '期货'),
                         ('/futures/star-market', '星标品种行情')):
        r = client.get(path)
        body = r.get_data(as_text=True)
        H.ck(r.status_code == 200, f'{path} 返回 200', f'status={r.status_code}')
        H.ck(marker in body, f'{path} 含预期标题文案「{marker}」')
        H.ck('navLinks' in body or 'main-nav' in body, f'{path} 已注入宿主公共导航')
        H.ck('/futures/static' not in body or 'css/style.css' in body,
             f'{path} 样式走宿主 style.css')

    r1 = client.get('/futures/star-market')
    r2 = client.get('/star-market')
    H.ck('星标品种行情' in r1.get_data(as_text=True)
         and '星标币种行情' not in r1.get_data(as_text=True),
         '期货星标页渲染的是期货模板，没有串到宿主模板')
    H.ck('星标币种行情' in r2.get_data(as_text=True), '宿主星标页内容未被期货模块影响')

    H.section('4. 行情依赖未就绪时的页面韧性')
    # 离线/无国内网络时 akshare 取数会失败，页面本身仍须 200（数据由前端异步补）
    r = client.get('/futures/detail/M')
    H.ck(r.status_code == 200, '期货详情页不依赖实时行情即可渲染')

    print('\n[PASS] 期货模块合并冒烟全部通过')


if __name__ == '__main__':
    if '--off' in sys.argv:
        # 开关必须在导入 crypto.app 之前落定：模块级注册逻辑只跑一次
        os.environ['FST_ENABLE_FUTURES'] = '0'
        main_off()
    else:
        main()
