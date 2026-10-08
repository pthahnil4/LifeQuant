# -*- coding: utf-8 -*-
"""市值榜功能路由冒烟测试：/monitor-guide、fixed-coins/market-detail、starred 回归。

用法：python -X utf8 data/_smoke_marketcap_routes.py
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, 'crypto'), os.path.join(ROOT, 'crypto', 'strategy'), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from crypto.app import app  # noqa: E402

def _auth_headers():
    """携带 web 登录口令（Bearer 方式，见 web_auth.py 口令怎么带）。"""
    tok = os.environ.get('CRYPTO_WEB_TOKEN', '').strip()
    tok_file = os.path.join(ROOT, 'data', 'web_token.txt')
    if not tok and os.path.exists(tok_file):
        tok = open(tok_file, encoding='utf-8').read().strip()
    if tok:
        return {'Authorization': f'Bearer {tok}'}
    return {}

def main():
    client = app.test_client()
    hdr = _auth_headers()
    ok = True

    # 1) 路由注册检查
    rules = {r.rule for r in app.url_map.iter_rules()}
    for need in ('/monitor-guide',
                 '/top-coins',
                 '/api/strategy/fixed-coins/refresh-marketcap',
                 '/api/strategy/fixed-coins/market-detail',
                 '/api/strategy/starred'):
        hit = need in rules
        ok &= hit
        print(('✅' if hit else '❌'), '路由注册', need)

    # 2) 操作手册页渲染
    r = client.get('/monitor-guide', headers=hdr)
    body = r.get_data(as_text=True)
    good = r.status_code == 200 and '固定币种' in body
    ok &= good
    print(('✅' if good else '❌'), f'/monitor-guide status={r.status_code} len={len(body)}')

    # 2b) Top50 币种导览页渲染
    r = client.get('/top-coins', headers=hdr)
    body = r.get_data(as_text=True)
    good = r.status_code == 200 and '币种导览' in body and 'COIN_ABOUT' in body
    ok &= good
    print(('✅' if good else '❌'), f'/top-coins status={r.status_code} len={len(body)}')

    # 3) 固定币种详情接口
    r = client.get('/api/strategy/fixed-coins/market-detail', headers=hdr)
    try:
        data = r.get_json()
    except Exception as e:
        data = None
        print('❌ market-detail 非 JSON:', e, r.status_code, r.get_data(as_text=True)[:200])
    if data:
        d = data.get('data') or {}
        rows = d.get('coins') or []
        good = data.get('code') == 200 and len(rows) >= 40
        ok &= good
        print(('✅' if good else '❌'),
              f"market-detail code={data.get('code')} coins={len(rows)} stale={d.get('stale')} "
              f"fetched_at={d.get('fetched_at')}")
        if rows:
            print('   首行:', json.dumps(rows[0], ensure_ascii=False)[:160])
            print('   末行:', json.dumps(rows[-1], ensure_ascii=False)[:160])

    # 4) starred 回归（装饰器曾误删已修复）
    r = client.get('/api/strategy/starred', headers=hdr)
    good = r.status_code != 404
    ok &= good
    print(('✅' if good else '❌'), '/api/strategy/starred status=', r.status_code)

    print('\n== SMOKE_MARKETCAP_ROUTES', 'PASS ==' if ok else 'FAIL ==')
    sys.exit(0 if ok else 1)

if __name__ == '__main__':
    main()
