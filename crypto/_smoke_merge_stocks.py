# -*- coding: utf-8 -*-
"""
冒烟测试：股票模块并入宿主应用（/stocks 蓝图）
============================================
覆盖场景（全部离线，不访问 akshare，不触发任何写操作）：
  1. 蓝图在 web 闸门之前注册：未登录访问 /stocks/** 一律 401
  2. 路由前缀完整：/stocks 下规则条数达基线且全带前缀
  3. 模板名不与宿主冲突（股票模板一律 stocks_ 前缀），解析归属正确
  4. 页面可达 + 宿主 nav 注入 + style.css
  5. 股票模块零交易接口：路由里不得出现下单/撤单类端点
  6. FST_ENABLE_STOCKS=0：蓝图不注册 + 前缀 503 提示页 + 宿主主功能不受影响
运行：python -m crypto._smoke_merge_stocks
      python -m crypto._smoke_merge_stocks --off
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from crypto import _smoke_merge_helper as H

# 原项目 /stocks 路由基线：9 条业务路由 + 1 条蓝图静态 = 10 条
#（见 doc/futureStockTrade合并迁移方案与清单.md「9.6」与原项目路由表 A/B 结论）
BASELINE_RULES = 10
# 交易类端点黑名单：股票模块设计上是只读行情 + 本地自选清单
TRADE_WORDS = re.compile(r'(?i)(place[-_]?order|cancel[-_]?order|submit[-_]?order'
                         r'|/trade|/buy|/sell|liquidat)')


def main_off():
    H.bootstrap_env()
    H.ck(os.environ.get('FST_ENABLE_STOCKS') == '0', '开关已置于关闭态')
    app = H.load_app()
    client = H.client(app)

    H.section('关闭态断言（FST_ENABLE_STOCKS=0）')
    H.ck('stocks' not in app.blueprints, '股票蓝图未注册')
    from crypto.app import _FST_MODULES
    H.ck(_FST_MODULES['stocks']['status'] == 'disabled', '注册状态记为 disabled')
    r = client.get('/stocks/')
    H.ck(r.status_code == 503, f'关闭态访问 /stocks/ 返回 503（{r.status_code}）')
    H.ck('FST_ENABLE_STOCKS' in r.get_data(as_text=True), '提示页给出环境变量名')
    H.ck(client.get('/').status_code == 200, '宿主首页不受股票模块关闭影响')
    H.ck(client.get('/futures/').status_code == 200, '期货模块不受股票开关牵连')
    print('\n[PASS] 股票模块关闭态冒烟通过')


def main():
    H.bootstrap_env({'FST_ENABLE_STOCKS': '1'})
    app = H.load_app()
    client = H.client(app)

    H.section('1. 蓝图注册与闸门顺序')
    H.ck('stocks' in app.blueprints, '股票蓝图已注册')
    rules = H.rules_for(app, '/stocks')
    H.ck(len(rules) >= BASELINE_RULES,
         f'路由条数不低于基线 {BASELINE_RULES}（实际 {len(rules)} 条）')
    H.ck(all(r.startswith('/stocks') for r in rules), '路由全部带 /stocks 前缀')
    anon = app.test_client()
    H.ck(anon.get('/stocks/').status_code in (401, 403), '未过闸门访问 /stocks/ 被拦')

    H.section('2. 模板命名与解析归属')
    for name in ('stocks_index.html', 'stocks_detail.html',
                 'stocks_multi_period.html'):
        d = H.resolve_template_dir(app, name)
        H.ck(d.replace('\\', '/').endswith('crypto/stocks/templates'),
             f'{name} 解析到股票目录（{d}）')

    H.section('3. 页面可达与宿主导航注入')
    for path, marker in (('/stocks/', '股票'), ('/stocks/detail/600519', '600519')):
        r = client.get(path)
        body = r.get_data(as_text=True)
        H.ck(r.status_code == 200, f'{path} 返回 200', f'status={r.status_code}')
        H.ck(marker in body, f'{path} 含预期文案「{marker}」')
        H.ck('main-nav' in body or 'navLinks' in body, f'{path} 已注入宿主公共导航')
        H.ck('css/style.css' in body, f'{path} 样式走宿主 style.css')

    H.section('4. 零交易接口红线（股票模块只读）')
    bad = [r for r in rules if TRADE_WORDS.search(r)]
    H.ck(not bad, f'路由中不含交易类端点（命中：{bad}）')

    print('\n[PASS] 股票模块合并冒烟全部通过')


if __name__ == '__main__':
    if '--off' in sys.argv:
        os.environ['FST_ENABLE_STOCKS'] = '0'
        main_off()
    else:
        main()
