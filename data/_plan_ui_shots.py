# -*- coding: utf-8 -*-
"""任务卡弹窗改版后的视觉验证（Playwright 无头截图 + 几何量测）

只做只读浏览：切 Tab、展开任务树、打开打卡表单后直接关闭，全程不点任何写操作按钮。
脚本会监听网络请求，一旦出现非 GET 就打印告警，用来兜底确认没写库。

用法：python data/_plan_ui_shots.py
输出：.qoder/ 下若干 png + 控制台量测结论
"""
import os
import json

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, '.qoder')
URL = 'http://127.0.0.1:7799/plan'

writes = []


def on_request(req):
    if req.method.upper() != 'GET':
        writes.append(req.method + ' ' + req.url)


def shot(page, name):
    path = os.path.join(OUT, name + '.png')
    page.screenshot(path=path)
    print('[shot]', path, flush=True)


def tab(page, key, wait=1200):
    page.click('#cm-tabs [data-tab="%s"]' % key)
    if key == 'slots':
        page.wait_for_selector('.slot-grid', timeout=20000)
    page.wait_for_timeout(wait)


def ev(page, expr):
    return page.evaluate(expr)


GEO_MODAL = """() => {
    const m = document.querySelector('.card-modal');
    const body = document.querySelector('#cm-body');
    const r = m.getBoundingClientRect();
    return {modal:[Math.round(r.width), Math.round(r.height)],
            bodyH: body ? body.clientHeight : 0,
            bodyScrollH: body ? body.scrollHeight : 0,
            tabsScrollW: (()=>{const t=document.querySelector('#cm-tabs'); return t?[t.scrollWidth,t.clientWidth]:null})()};
}"""

GEO_GRID = """() => {
    const g = document.querySelector('.slot-grid');
    if (!g) return null;
    const r = g.getBoundingClientRect();
    const cells = g.querySelectorAll('.slot-cell');
    const last = cells.length ? cells[cells.length-1].getBoundingClientRect() : null;
    const body = document.querySelector('#cm-body').getBoundingClientRect();
    return {grid:[Math.round(r.width), Math.round(r.height)], cells:cells.length,
            lastCellTop: last ? Math.round(last.top) : null,
            bodyBottom: Math.round(body.bottom),
            lastVisible: last ? (last.bottom <= body.bottom + 1) : null,
            cols: getComputedStyle(g).gridTemplateColumns.split(' ').length};
}"""

GEO_FEED = """() => {
    const b = document.querySelector('.srl-body');
    if (!b) return null;
    const cs = getComputedStyle(b);
    return {clientH:b.clientHeight, scrollH:b.scrollHeight, overflowY:cs.overflowY,
            items: b.querySelectorAll('.slot-record-item').length,
            tools: cs.position};
}"""

GEO_NOTES = """() => {
    const l = document.querySelector('.notes-list');
    const t = document.querySelector('#note-input');
    return {cols: l?getComputedStyle(l).gridTemplateColumns.split(' ').length:null,
            items: l?l.querySelectorAll('.note-item').length:null,
            tag: t?t.tagName:null,
            cap: (document.querySelector('.ni-cap')||{}).textContent};
}"""

GEO_SETTLE = """() => {
    const c = document.querySelector('.settle-cols');
    const rv = document.querySelector('#sr-review');
    return {cols: c?getComputedStyle(c).gridTemplateColumns.split(' ').length:null,
            review: rv?rv.tagName:null,
            reviewLen: rv?rv.value.length:null,
            cap: (document.querySelector('.settle-review .sf-tip')||{}).textContent};
}"""

GEO_FORM = """() => {
    const d = document.querySelector('.m-dialog');
    if (!d) return null;
    const r = d.getBoundingClientRect();
    const cols = d.querySelector('.sf-cols');
    const ok = d.querySelector('.m-dialog-ok-btn');
    const okR = ok ? ok.getBoundingClientRect() : null;
    const ta = d.querySelector('textarea');
    return {dlg:[Math.round(r.width), Math.round(r.height)],
            cls: d.className,
            cols: cols?getComputedStyle(cols).gridTemplateColumns.split(' ').length:null,
            bodyH: (d.querySelector('.m-dialog-body')||{clientHeight:0}).clientHeight,
            bodyScrollH: (d.querySelector('.m-dialog-body')||{scrollHeight:0}).scrollHeight,
            okVisible: okR ? (okR.top >= 0 && okR.bottom <= window.innerHeight + 1) : null,
            taH: ta ? Math.round(ta.getBoundingClientRect().height) : null,
            overflowX: document.documentElement.scrollWidth > window.innerWidth};
}"""


def open_card(page, keyword):
    page.click('.task-card:has-text("%s")' % keyword)
    page.wait_for_selector('#card-modal', state='visible')
    # 卡片详情是异步拉的，等 Tab 与正文真的渲染出来再量，否则量到的是加载中的空壳
    page.wait_for_selector('#cm-tabs .cm-tab', timeout=30000)
    page.wait_for_timeout(1200)


def close_form(page):
    btn = page.query_selector('.m-dialog-close')
    if btn:
        btn.click()
        page.wait_for_timeout(500)


def run():
    with sync_playwright() as p:
        browser = None
        # 环境里 playwright 期望的 chromium 构建没装，退而用系统 Edge / 已有 chromium
        for try_launch in (
            lambda: p.chromium.launch(channel='msedge'),
            lambda: p.chromium.launch(channel='chrome'),
            lambda: p.chromium.launch(executable_path=os.path.expandvars(
                r'%LOCALAPPDATA%\ms-playwright\chromium-1187\chrome-win\chrome.exe')),
        ):
            try:
                browser = try_launch()
                break
            except Exception as exc:
                print('[launch skip]', str(exc).splitlines()[0], flush=True)
        if browser is None:
            raise SystemExit('无可用浏览器，跳过视觉验证')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        console_errs = []
        bad_res = []
        page.on('request', on_request)
        page.on('response', lambda r: bad_res.append(str(r.status) + ' ' + r.url) if r.status >= 400 else None)
        page.on('console', lambda m: console_errs.append(m.text) if m.type == 'error' else None)
        page.on('pageerror', lambda e: console_errs.append('pageerror: ' + str(e)))

        page.goto(URL, wait_until='networkidle')
        page.wait_for_selector('.task-card')

        print('\n===== 桌面 1440x900 =====')
        open_card(page, '学习任务2')
        print('[概览]', json.dumps(ev(page, GEO_MODAL), ensure_ascii=False))
        print('[概览复盘域]', ev(page, "!!document.querySelector('#ov-review')"))
        shot(page, 'v2_d1_overview')

        tab(page, 'slots')
        print('[打卡 网格]', json.dumps(ev(page, GEO_GRID), ensure_ascii=False))
        print('[打卡 记录列]', json.dumps(ev(page, GEO_FEED), ensure_ascii=False))
        # 记录列表：关键字过滤 + 分段加载（纯前端）
        page.fill('#srl-search', 'halo')
        page.wait_for_timeout(400)
        filtered = ev(page, "document.querySelectorAll('.srl-body .slot-record-item').length")
        page.fill('#srl-search', '')
        page.wait_for_timeout(400)
        before = ev(page, "document.querySelectorAll('.srl-body .slot-record-item').length")
        page.click('#srl-more')
        page.wait_for_timeout(400)
        after = ev(page, "document.querySelectorAll('.srl-body .slot-record-item').length")
        print('[记录过滤/分段]', json.dumps({'搜索halo': filtered, '清空': before, '继续显示后': after},
                                          ensure_ascii=False))
        shot(page, 'v2_d1_slots')

        tab(page, 'tasks')
        page.click('#tt-tools-expand') if page.query_selector('#tt-tools-expand') else None
        exp = page.query_selector('.tt-tools button:has-text("全部展开")')
        if exp:
            exp.click()
            page.wait_for_timeout(500)
        print('[任务 节点行数]', ev(page, "document.querySelectorAll('.tt-row').length"))
        shot(page, 'v2_d1_tasks')

        tab(page, 'checkin')
        print('[图表]', ev(page, "(()=>{const c=document.querySelector('.ccb-canvas');return c?[c.clientWidth,c.clientHeight,!!c.querySelector('canvas')]:null})()"))
        shot(page, 'v2_d1_checkin')

        tab(page, 'notes')
        print('[小记]', json.dumps(ev(page, GEO_NOTES), ensure_ascii=False))
        shot(page, 'v2_d1_notes')

        tab(page, 'settle')
        print('[结算]', json.dumps(ev(page, GEO_SETTLE), ensure_ascii=False))
        shot(page, 'v2_d1_settle')

        # 打卡表单几何：换到进行中的交易卡（学习卡已结束，格子只读）
        page.evaluate("TaskPlan.closeCardModal()")
        page.wait_for_timeout(400)
        open_card(page, '第2轮')
        tab(page, 'slots')
        print('[交易卡 网格]', json.dumps(ev(page, GEO_GRID), ensure_ascii=False))
        filled = page.query_selector('.slot-cell.filled')
        if filled:
            filled.click()
            page.wait_for_timeout(900)
            print('[记录弹窗]', json.dumps(ev(page, GEO_FORM), ensure_ascii=False))
            shot(page, 'v2_d1_slot_detail')
            close_form(page)
        empty = page.query_selector('.slot-cell:not(.filled):not(.locked)')
        if empty:
            empty.click()
            page.wait_for_timeout(900)
            print('[填写弹窗]', json.dumps(ev(page, GEO_FORM), ensure_ascii=False))
            shot(page, 'v2_d1_slot_fill')
            close_form(page)
        page.evaluate("TaskPlan.closeCardModal()")
        page.wait_for_timeout(400)

        print('\n===== 窄屏 480x900 =====')
        page.set_viewport_size({'width': 480, 'height': 900})
        page.goto(URL, wait_until='networkidle')
        page.wait_for_selector('.task-card')
        open_card(page, '学习任务2')
        print('[概览]', json.dumps(ev(page, GEO_MODAL), ensure_ascii=False))
        shot(page, 'v2_n1_overview')

        # Tab 栏横向滚动可达性
        scroll_info = ev(page, """() => {
            const t = document.querySelector('#cm-tabs');
            t.scrollLeft = 9999;
            return {scrollW:t.scrollWidth, clientW:t.clientWidth, after:t.scrollLeft,
                    tabs:[...t.querySelectorAll('.cm-tab')].map(b=>b.dataset.tab)};
        }""")
        print('[Tab栏]', json.dumps(scroll_info, ensure_ascii=False))
        shot(page, 'v2_n1_tabs_scrolled')

        for key, nm in [('slots', 'slots'), ('tasks', 'tasks'), ('checkin', 'checkin'),
                        ('notes', 'notes'), ('settle', 'settle')]:
            tab(page, key, 700)
            if key == 'slots':
                print('[打卡 网格]', json.dumps(ev(page, GEO_GRID), ensure_ascii=False))
                print('[打卡 记录列]', json.dumps(ev(page, GEO_FEED), ensure_ascii=False))
            if key == 'notes':
                print('[小记]', json.dumps(ev(page, GEO_NOTES), ensure_ascii=False))
            if key == 'settle':
                print('[结算]', json.dumps(ev(page, GEO_SETTLE), ensure_ascii=False))
            print('  [%s 横向溢出]' % key, ev(page, "document.documentElement.scrollWidth > window.innerWidth"))
            shot(page, 'v2_n1_' + nm)

        print('\n[console errors]', json.dumps(console_errs, ensure_ascii=False))
        print('[HTTP>=400]', json.dumps(bad_res, ensure_ascii=False))
        print('[非 GET 请求]', json.dumps(writes, ensure_ascii=False) or '无')
        browser.close()


if __name__ == '__main__':
    run()
