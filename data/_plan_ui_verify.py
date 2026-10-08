# -*- coding: utf-8 -*-
"""任务卡交互回归验证（Playwright + 本地 mock 响应，只读、零写库）

覆盖那些「真实库里恰好没有样本、但代码已经实现」的分支：
  1) 任务树「完成依据第 N 格」chip 与点击跳转（现网任务全部来自旧数据惰性迁移，
     没有 completed_by_slot，故给 学习任务2 的响应注入一个）；
  2) 打卡表单快捷录入正路径：插入常用段落 / 复制上一条（给 学习任务3 注入
     2 条小记 + 1 条历史打卡）；
  3) 关联任务「只看可关联」过滤条（含吸顶条横向铺满、未关联记录的 is-pending 着色）；
  4) 范围补录逐日内容行；
  5) 窄屏 480×900 下表单列数与提交按钮可见性；
  6) 结算 Tab 复盘容量角标「打字即刷新」（只改前端输入框，绝不点保存）。

铁律：全程只点前端控件，绝不点「勾选/提交/保存/添加/删除/结算」；
脚本会记录任何非 GET 请求，跑完必须是空列表。
用法：先起 python data/_plan_ui_preview.py，再跑本脚本。
"""
import os
import json

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, '.qoder')
URL = 'http://127.0.0.1:7799/plan'

writes, errs = [], []
MOCK_SLOT = 86


def inject_done_by(nodes):
    """给第一个已完成任务补一个「完成依据格子」"""
    for n in nodes or []:
        if n.get('status') == 'done':
            n['completed_by_slot'] = MOCK_SLOT
            return True
        if inject_done_by(n.get('children')):
            return True
    return False


def inject_history(card):
    """补 2 条小记 + 第 1 格打卡记录（快捷录入的数据来源）"""
    # 顺手把复盘列灌到 58.6KB（> 80% 阈值），用来验容量角标的橙色告警分支
    card['review'] = '复' * 20000          # UTF-8 三字节 → 60000B
    card['notes'] = [
        {'time': '2026-09-15 20:10', 'content': '固定句式：今天完成域名解析配置，验证 HTTPS 跳转'},
        {'time': '2026-09-16 09:30', 'content': '复盘：部署环节耗时超出预估，下次先本地跑通'},
    ]
    slots = card.get('slots') or []
    if slots:
        slots[0].update({'filled': True, 'has_record': True, 'filled_at': '2026-09-15 20:00',
                         'record': {'content': '今天完成域名解析配置，验证 HTTPS 跳转',
                                    'duration_minutes': 90, 'task_links': []}})
    return True


def make_handler():
    def handle(route):
        resp = route.fetch()
        try:
            body = json.loads(resp.text())
            card = body.get('data') or {}
            title = card.get('title') or ''
            if '学习任务2' in title:
                inject_done_by(card.get('tasks'))
            elif '学习任务3' in title:
                inject_history(card)
            route.fulfill(response=resp, body=json.dumps(body, ensure_ascii=False))
        except Exception as exc:
            print('[mock skip]', exc, flush=True)
            route.fallback()
    return handle


def launch(p):
    for try_launch in (
        lambda: p.chromium.launch(channel='msedge'),
        lambda: p.chromium.launch(executable_path=os.path.expandvars(
            r'%LOCALAPPDATA%\ms-playwright\chromium-1187\chrome-win\chrome.exe')),
    ):
        try:
            return try_launch()
        except Exception as exc:
            print('[launch skip]', str(exc).splitlines()[0], flush=True)
    raise SystemExit('无可用浏览器')


def open_card(page, keyword, tab_key=None):
    page.click('.task-card:has-text("%s")' % keyword)
    page.wait_for_selector('#cm-tabs .cm-tab', timeout=30000)
    page.wait_for_timeout(1000)
    if tab_key:
        page.click('#cm-tabs [data-tab="%s"]' % tab_key)
        page.wait_for_timeout(600)


def main():
    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('request', lambda r: writes.append(r.method + ' ' + r.url)
                if r.method.upper() != 'GET' else None)
        page.on('pageerror', lambda e: errs.append(str(e)))
        page.on('console', lambda m: errs.append('console: ' + m.text) if m.type == 'error' else None)
        page.route('**/plan/api/card-detail**', make_handler())

        # ---- 1. 完成依据 chip + 跳转 ----
        page.goto(URL, wait_until='networkidle')
        page.wait_for_selector('.task-card')
        open_card(page, '学习任务2', 'tasks')
        chips = page.evaluate("""() => [...document.querySelectorAll('.tt-doneby')].map(c =>
            ({text:c.textContent, slot:c.dataset.slot, visible:c.offsetParent!==null}))""")
        print('[1 完成依据chip]', json.dumps(chips, ensure_ascii=False))
        if chips:
            page.click('.tt-doneby')
            page.wait_for_timeout(800)
            print('[1 跳转]', json.dumps(page.evaluate("""() => {
                const cur = document.querySelector('.slot-cell.cur');
                const act = document.querySelector('#cm-tabs .cm-tab.active');
                const r = cur ? cur.getBoundingClientRect() : null;
                return {tab: act?act.dataset.tab:null, slot: cur?cur.dataset.index:null,
                        inView: r ? (r.top>=0 && r.bottom<=window.innerHeight) : null};
            }"""), ensure_ascii=False))
        page.screenshot(path=os.path.join(OUT, 'v8_doneby_jump.png'))
        page.evaluate("TaskPlan.closeCardModal()")
        page.wait_for_timeout(400)

        # ---- 2~4. 学习卡填写表单：快捷录入 / 过滤条 / 范围补录 ----
        page.goto(URL, wait_until='networkidle')
        page.wait_for_selector('.task-card')
        open_card(page, '学习任务3', 'slots')
        print('[2 只看未关联]', json.dumps(page.evaluate("""() => {
            const pend = document.querySelector('#srl-only-pending');
            if (!pend) return {rendered:false};
            const before = document.querySelectorAll('.srl-body .slot-record-item').length;
            pend.checked = true; pend.dispatchEvent(new Event('change',{bubbles:true}));
            // repaint 会整块重画，样式必须在重画后的节点上量（旧节点已脱离文档流）
            const row0 = document.querySelector('.srl-body .slot-record-item');
            const cs = row0 ? getComputedStyle(row0) : null;
            return {rendered:true, before:before,
                    after: document.querySelectorAll('.srl-body .slot-record-item').length,
                    chip: !!document.querySelector('.srl-pending-link'),
                    pendCls: row0 ? row0.className : null,
                    pendBorder: cs ? cs.borderLeftColor + '/' + cs.borderLeftWidth : null};
        }"""), ensure_ascii=False))
        # 过滤完记得取消勾选，否则后面的断言看到的是被筛掉的列表
        page.evaluate("""() => {const p=document.querySelector('#srl-only-pending');
            if(p&&p.checked){p.checked=false;p.dispatchEvent(new Event('change',{bubbles:true}));}}""")

        page.query_selector('.slot-cell:not(.filled):not(.locked)').click()
        page.wait_for_timeout(1100)
        print('[2 快捷按钮]', json.dumps(page.evaluate("""() => ({
            copy: getComputedStyle(document.querySelector('#sf-copy-last')).display,
            snips: getComputedStyle(document.querySelector('#sf-snips-toggle')).display
        })"""), ensure_ascii=False))
        page.click('#sf-snips-toggle')
        page.wait_for_timeout(400)
        page.click('.sf-snip')
        page.wait_for_timeout(400)
        print('[2 插入段落]', json.dumps(page.evaluate("""() => ({
            items: document.querySelectorAll('.sf-snip').length,
            value: document.querySelector('#sf-content').value.slice(0,30),
            note: (document.querySelector('#sf-quick-note')||{}).textContent
        })"""), ensure_ascii=False))
        page.click('#sf-copy-last')
        page.wait_for_timeout(400)
        print('[2 复制上一条]', json.dumps(page.evaluate("""() => ({
            value: document.querySelector('#sf-content').value.slice(0,30),
            duration: document.querySelector('#sf-duration').value,
            note: (document.querySelector('#sf-quick-note')||{}).textContent
        })"""), ensure_ascii=False))

        print('[3 过滤条]', json.dumps(page.evaluate("""() => {
            const t = document.querySelector('.lp-tools');
            const lb = t.querySelector('label'), cb = lb.querySelector('input');
            const total = document.querySelectorAll('.lp-row').length;
            cb.checked = true; cb.dispatchEvent(new Event('change',{bubbles:true}));
            const vis = [...document.querySelectorAll('.lp-row')].filter(r => r.offsetParent !== null);
            // 吸顶条必须横向铺满 .link-picker 的内容区，否则滚动行的字会从左右 8px 缝隙里透出来
            const box = t.getBoundingClientRect(), pick = document.querySelector('.link-picker');
            return {labelDisplay:getComputedStyle(lb).display, cbW: Math.round(cb.getBoundingClientRect().width),
                    total: total, visible: vis.length, stat: t.querySelector('.lp-stat').textContent,
                    spanGap: +(box.width - pick.clientWidth).toFixed(1)};
        }"""), ensure_ascii=False))
        page.screenshot(path=os.path.join(OUT, 'v8_lp_filter.png'))

        page.click('#sf-mode-backfill')
        page.wait_for_timeout(300)
        page.click('#sf-bf-range')
        page.wait_for_timeout(300)
        page.evaluate("""() => {
            const s=document.querySelector('#sf-bf-start'), e=document.querySelector('#sf-bf-end');
            s.value='2026-09-10'; e.value='2026-09-13';
            s.dispatchEvent(new Event('change',{bubbles:true}));
            e.dispatchEvent(new Event('change',{bubbles:true}));
        }""")
        page.wait_for_timeout(900)
        print('[4 范围补录]', json.dumps(page.evaluate("""() => {
            const first = document.querySelector('.bf-row');
            return {rows: document.querySelectorAll('.bf-row').length,
                    perDayText: first?!!first.querySelector('.sf-bf-text'):null,
                    tools: !!document.querySelector('.bf-tools')};
        }"""), ensure_ascii=False))
        page.click('.m-dialog-close')
        page.wait_for_timeout(400)

        # ---- 6. 结算 Tab：复盘角标就地刷新（打字即更新，不整块重渲染） ----
        page.click('#cm-tabs [data-tab="settle"]')
        page.wait_for_timeout(700)
        print('[6 复盘角标-初始]', json.dumps(page.evaluate("""() => {
            const cap = document.querySelector('#sr-review-cap');
            const title = document.querySelector('.settle-review .sr-title');
            if (!cap || !title) return {rendered:false};
            const cs = getComputedStyle(cap);
            return {text: cap.textContent, titleDisplay: getComputedStyle(title).display,
                    marginLeft: cs.marginLeft, weight: cs.fontWeight, hi: cap.classList.contains('hi')};
        }"""), ensure_ascii=False))
        page.evaluate("""() => {
            const b = document.querySelector('#sr-review');
            b.value = 'X'.repeat(1200);            // 1200 字节 ≈ 1.2KB
            b.dispatchEvent(new Event('input', {bubbles:true}));
        }""")
        page.wait_for_timeout(200)
        print('[6 复盘角标-打字后]', json.dumps(page.evaluate("""() => {
            const cap = document.querySelector('#sr-review-cap');
            return {text: cap.textContent, hi: cap.classList.contains('hi'),
                    note: (document.querySelector('#sr-review-note')||{}).textContent || '(空)'};
        }"""), ensure_ascii=False))
        # 只清前端内存里的输入框，绝不点保存
        page.evaluate("() => { document.querySelector('#sr-review').value = ''; }")
        page.screenshot(path=os.path.join(OUT, 'v8_settle_review.png'))
        page.evaluate("TaskPlan.closeCardModal()")
        page.wait_for_timeout(400)

        # ---- 5. 窄屏表单 ----
        page.set_viewport_size({'width': 480, 'height': 900})
        page.goto(URL, wait_until='networkidle')
        page.wait_for_selector('.task-card')
        open_card(page, '学习任务3', 'slots')
        page.query_selector('.slot-cell:not(.filled):not(.locked)').click()
        page.wait_for_timeout(1100)
        print('[5 窄屏表单]', json.dumps(page.evaluate("""() => {
            const d = document.querySelector('.m-dialog');
            const r = d.getBoundingClientRect();
            const ok = d.querySelector('.m-dialog-ok-btn').getBoundingClientRect();
            return {dlg:[Math.round(r.width), Math.round(r.height)],
                    cols: getComputedStyle(d.querySelector('.sf-cols')).gridTemplateColumns.split(' ').length,
                    okInView: ok.bottom <= window.innerHeight + 1,
                    overflowX: document.documentElement.scrollWidth > window.innerWidth};
        }"""), ensure_ascii=False))
        page.screenshot(path=os.path.join(OUT, 'v8_narrow_fill.png'))
        page.click('.m-dialog-close')

        print('[errors]', json.dumps(errs, ensure_ascii=False))
        print('[非 GET]', json.dumps(writes, ensure_ascii=False))
        browser.close()


def history_offline():
    """历史时间回归：--history-offline 无需服务，全量拦截网络，允许提交到内存桩。"""
    import re
    from pathlib import Path

    root = Path(ROOT) / 'crypto'
    script = (root / 'static/js/task_plan.js').read_text(encoding='utf-8')
    script = script.replace("document.addEventListener('DOMContentLoaded', TaskPlan.init);", '')
    # 仅暴露浏览器内副本的测试入口，磁盘上的业务代码不变。
    script = script.replace('init: init,', 'init: init, testFill: openSlotFill, testDetail: openSlotDetail, testCss: FORM_CSS,')
    template = (root / 'templates/task_plan.html').read_text(encoding='utf-8')
    css = (root / 'static/css/style.css').read_text(encoding='utf-8')
    css += '\n'.join(re.findall(r'<style>(.*?)</style>', template, re.S))
    checks, unexpected, errors = [], [], []

    def check(name, ok):
        assert ok, name
        checks.append(name)

    with sync_playwright() as p:
        browser = launch(p)
        context = browser.new_context(viewport={'width': 1440, 'height': 700})
        def abort(route):
            unexpected.append(route.request.url)
            route.abort()
        context.route('**/*', abort)
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content('<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>')
        page.add_style_tag(content=css)
        page.add_script_tag(content=(root / 'static/js/mdialog.js').read_text(encoding='utf-8'))
        page.add_script_tag(content=script)
        page.add_style_tag(content=page.evaluate('TaskPlan.testCss'))
        page.evaluate("""() => {
            window.calls = []; window.pending = []; window.holdGate = false;
            window.fetch = (url, opts={}) => {
                const data = opts.body ? JSON.parse(opts.body) : null;
                calls.push({url, data});
                const reply = body => ({json: () => Promise.resolve(body)});
                if(url.startsWith('/plan/api/analysis-gate')) {
                    const target = new URL(url, 'http://test.invalid').searchParams.get('filled_at') || '2026-09-22 10:00:00';
                    const response = {code:200,data:{enabled:true,allowed:false,mode:'strict_block',
                        hour_slot:target.slice(0,13),actual:0,required:1,records:[]}};
                    if(holdGate) return new Promise(resolve => pending.push(() => resolve(reply(response))));
                    return Promise.resolve(reply(response));
                }
                if(url === '/plan/api/account-balance') return Promise.resolve(reply({code:200,data:{totalEq:'100',updateTime:'测试'}}));
                if(url.startsWith('/plan/api/backfill-balances')) {
                    const params = new URL(url,'http://test.invalid').searchParams;
                    let day = params.get('start'); const end = params.get('end'), days=[];
                    while(day <= end && days.length < 62) {
                        days.push({date:day,balance:100+days.length});
                        const date = new Date(day+'T12:00:00Z'); date.setUTCDate(date.getUTCDate()+1);
                        day = date.toISOString().slice(0,10);
                    }
                    return Promise.resolve(reply({code:200,data:{days}}));
                }
                if(['/plan/api/fill-slot','/plan/api/update-slot','/plan/api/backfill-batch'].includes(url)) {
                    if(window.writeResponse) return Promise.resolve(reply(window.writeResponse));
                    return Promise.resolve(reply(window.testCard.type === 'trade' && !url.endsWith('backfill-batch')
                        ? {code:403,message:'测试：目标历史小时缺分析',data:{need_analysis:true}}
                        : {code:400,message:'测试：保留表单检查请求'}));
                }
                throw Error('非预期接口：'+url);
            };
            window.openHistoryTest = (type, detail, status) => {
                window.calls=[]; window.holdGate=false; window.pending=[]; window.writeResponse=null;
                const card={id:'test',plan_id:'test_plan',type,status:status||'in_progress',notes:[],
                    tasks:Array.from({length:12},(_,i)=>({id:'t'+i,title:'任务'+i,status:'todo',estimated_minutes:60,children:[]})),
                    slots:Array.from({length:100},(_,i)=>({slot_index:i,filled:false,filled_at:'',record:null}))};
                card.slots[0]={slot_index:0,filled:true,filled_at:'2026-09-08 10:00:27',
                    record:{content:'原学习记录',prediction:'涨',actual:'跌',duration_minutes:30,task_links:[]}};
                window.testCard=card;
                if(detail) TaskPlan.testDetail(card,card.slots[0],0); else TaskPlan.testFill(card,1);
            };
        }""")

        def open_form(kind, detail=False, status='in_progress'):
            page.evaluate("() => document.querySelectorAll('.m-dialog-overlay').forEach(o=>MDialog.close(o))")
            page.wait_for_timeout(320)
            page.evaluate('([kind,detail,status])=>openHistoryTest(kind,detail,status)', [kind, detail, status])
            page.wait_for_timeout(320)

        def writes_to(path):
            return page.evaluate('(path)=>calls.filter(c=>c.url===path)', '/plan/api/' + path)

        def change(selector, value):
            page.locator(selector).fill(value)
            page.locator(selector).dispatch_event('change')

        def submit():
            page.locator('.m-dialog-ok-btn').click()
            page.wait_for_timeout(80)

        for width in (1440, 375):
            page.set_viewport_size({'width': width, 'height': 700})
            for kind in ('learn', 'trade'):
                label = f'{width}/{kind}'
                open_form(kind)
                check(label + ' 模式入口首屏可见', page.locator('#sf-mode-backfill').evaluate('e=>{const r=e.getBoundingClientRect(),b=e.closest(".m-dialog-body").getBoundingClientRect();return r.top>=b.top && r.bottom<=b.bottom && r.bottom<=innerHeight}'))
                page.click('#sf-mode-backfill')
                change('#sf-filled-at', '')
                content = '#sf-content' if kind == 'learn' else '#sf-market-analysis'
                page.locator(content).fill('表单不能丢失')
                submit()
                check(label + ' 空时间拒绝且表单保留', not writes_to('fill-slot') and page.locator(content).input_value() == '表单不能丢失')
                change('#sf-filled-at', '2026-09-10T09:35:27')
                check(label + ' 历史输入未禁用', page.locator('#sf-filled-at').is_enabled() and not page.locator('#sf-filled-at').get_attribute('min'))
                page.screenshot(path=os.path.join(OUT, f'history_{width}_{kind}.png'))
                submit()
                posted = writes_to('fill-slot')[-1]['data']
                check(label + ' 单点日期秒正确', posted['filled_at'] == '2026-09-10 09:35:27' and posted['fill_mode'] == 'backfill')
                if kind == 'trade':
                    check(label + ' 403后时间和内容保留', page.locator(content).input_value() == '表单不能丢失' and '2026-09-10 09' in page.locator('#sf-gate').inner_text())
                page.click('#sf-mode-today')
                submit()
                check(label + ' 今日不带历史参数', 'filled_at' not in writes_to('fill-slot')[-1]['data'])

                open_form(kind)
                page.click('#sf-mode-backfill')
                page.click('#sf-bf-range')
                change('#sf-bf-start', '2026-09-10')
                change('#sf-bf-end', '2026-09-11')
                change('#sf-bf-time', '09:35')
                page.wait_for_timeout(80)
                if kind == 'learn':
                    page.locator('.sf-bf-text').nth(0).fill('第一天')
                    page.locator('.sf-bf-text').nth(1).fill('第二天')
                submit()
                entries = writes_to('backfill-batch')[-1]['data']['entries']
                check(label + ' 批量历史时间正确', [e['filled_at'] for e in entries] == ['2026-09-10 09:35','2026-09-11 09:35'])
                check(label + ' 逐日内容或余额正确', [e['record']['content'] for e in entries] == ['第一天','第二天'] if kind == 'learn' else [e['record']['account_balance'] for e in entries] == ['100','101'])
                count = len(writes_to('backfill-batch'))
                change('#sf-bf-time', '')
                submit()
                check(label + ' 空每日时刻拒绝', len(writes_to('backfill-batch')) == count)
                change('#sf-bf-time', '09:35')
                change('#sf-bf-start', '')
                submit()
                check(label + ' 空日期范围拒绝', len(writes_to('backfill-batch')) == count)

                open_form(kind, True)
                check(label + ' 详情保留原秒数', page.locator('#sd-filled-at').input_value() == '2026-09-08T10:00:27')
                submit()
                check(label + ' 详情未改时间不提交日期', 'filled_at' not in writes_to('update-slot')[-1]['data'])
                change('#sd-filled-at', '2026-09-10T09:35:27')
                submit()
                check(label + ' 详情改期参数正确', writes_to('update-slot')[-1]['data']['filled_at'] == '2026-09-10 09:35:27')
                check(label + ' 详情失败保留输入', page.locator('#sd-filled-at').input_value() == '2026-09-10T09:35:27')
                count = len(writes_to('update-slot'))
                change('#sd-filled-at', '')
                submit()
                check(label + ' 详情空时间拒绝', len(writes_to('update-slot')) == count)
                open_form(kind, True, 'completed')
                check(label + ' 结束卡时间只读', page.locator('#sd-filled-at').count() == 0)

        page.set_viewport_size({'width': 1440, 'height': 700})
        for target in ('range', 'empty', 'another'):
            open_form('trade')
            page.click('#sf-mode-backfill')
            page.evaluate('window.holdGate=true')
            change('#sf-filled-at', '2026-09-10T09:35')
            page.evaluate('window.holdGate=false')
            if target == 'range':
                page.click('#sf-bf-range')
            else:
                change('#sf-filled-at', '' if target == 'empty' else '2026-09-11T10:00')
            page.wait_for_timeout(80)
            expected = page.locator('#sf-gate').inner_text()
            page.evaluate('() => pending.splice(0).forEach(release=>release())')
            page.wait_for_timeout(80)
            check('旧闸门响应失效/' + target, page.locator('#sf-gate').inner_text() == expected)
        for kind in ('learn', 'trade'):
            for operation in ('single', 'range', 'edit'):
                open_form(kind, operation == 'edit')
                if operation == 'edit':
                    change('#sd-filled-at', '2026-09-10T09:35:27')
                else:
                    page.click('#sf-mode-backfill')
                    if operation == 'range':
                        page.click('#sf-bf-range')
                    else:
                        change('#sf-filled-at', '2026-09-10T09:35:27')
                page.evaluate('window.writeResponse={code:200,data:{filled:1,filled_count:2,refresh:{}}}')
                submit()
                page.wait_for_timeout(320)
                check('成功响应关闭表单/' + kind + '/' + operation,
                      page.locator('.slot-form').count() == 0 and '成功' in page.locator('.m-dialog-body').inner_text())
        check('浏览器无脚本错误', not errors)
        check('无真实网络请求', not unexpected)
        browser.close()
    print(json.dumps({'passed': len(checks), 'checks': checks, 'errors': errors, 'network': unexpected}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    import sys
    if '--history-offline' in sys.argv:
        history_offline()
    else:
        main()
