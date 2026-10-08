/* 批量控制器离线回归：node crypto/static/js/_smoke_batch_trend.js
 * 直接加载 main.js 被测片段；模拟网络、时钟与 DOM，不访问生产服务。
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');
const start = source.indexOf('    // ---- SSE 实时推送与持续轮询兜底');
const end = source.indexOf('    // ---- 初始加载表格数据 ----', start);
assert(start > 0 && end > start, '控制器边界必须存在');
const controller = source.slice(start, end);

class Element {
    constructor() {
        this.style = {}; this.children = []; this.events = {}; this.attrs = {};
        this.clientHeight = 120; this.top = 0; this.offsetHeight = 20;
        this.classList = {add() {}, remove() {}};
    }
    set textContent(text) { this.text = text; this.children = []; this.top = 0; }
    get textContent() { return this.text || ''; }
    get childElementCount() { return this.children.length; }
    get firstElementChild() { return this.children[0]; }
    get scrollHeight() { return this.children.length * 20; }
    get scrollTop() { return this.top; }
    set scrollTop(value) { this.top = Math.min(Math.max(0, value), Math.max(0, this.scrollHeight - this.clientHeight)); }
    appendChild(child) { this.children.push(child); }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
    setAttribute(name, value) { this.attrs[name] = value; }
    addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
    async emit(name) { for (const fn of this.events[name] || []) await fn({}); }
}

function state(status = 'running', extra = {}) {
    return Object.assign({status, run_id: 'run1', progress_pct: 20, elapsed_seconds: 2,
        message: '测试状态', coin_total: 2, current: 2, total: 10, success: 0, error: 0,
        logs: [], cursor: 0, dropped_logs: 0}, extra);
}
function line(seq, run_id = 'run1') { return {seq, run_id, msg: '日志 ' + seq, ts: '12:00', level: 'info'}; }

function harness(withSSE = true) {
    const elements = {}, sources = [], timers = new Map(), events = {};
    let timerID = 0, now = 100000, reloads = 0, confirmation;
    let responder = async () => ({code: 200, data: state()});
    const get = id => (elements[id] ||= new Element());
    class EventSource {
        constructor(url) { this.url = url; this.listeners = {}; sources.push(this); }
        close() { this.closed = true; }
        addEventListener(name, fn) { this.listeners[name] = fn; }
        emit(name, value) { this.listeners[name]({data: JSON.stringify(value)}); }
    }
    const context = vm.createContext({
        console, AbortController, Date: {now: () => now},
        document: {getElementById: get, createElement: () => new Element()},
        window: {EventSource: withSSE ? EventSource : undefined,
            addEventListener(name, fn) { events[name] = fn; }},
        EventSource, setTimeout(fn, delay) { timers.set(++timerID, {fn, delay}); return timerID; },
        clearTimeout(id) { timers.delete(id); },
        fetch: async (url, options) => {
            const result = await responder(url, options);
            return {ok: result.code === 200, status: result.code, json: async () => result};
        },
        MDialog: {confirm(options) { confirmation = options; }},
        loadTableData() { reloads++; },
        batchStartBtn: get('batch-start-btn'), batchProgress: get('batch-progress'),
        progressBar: get('progress-bar'), progressMsg: get('progress-msg'),
        isRunning: false, currentView: 'full'
    });
    vm.runInContext(controller + `
        globalThis.api = {syncBatchState, openBatchSSE, appendLog, adoptBatch, updateProgressUI,
            scheduleBatchSync, setBatchBusy, getState: () => ({activeRunId, logCursor,
                isRunning, followLogs, logCollapsed, nextSSEAttempt, monitorEpoch})};`, context);
    return {api: context.api, get, sources, timers, events,
        set responder(fn) { responder = fn; },
        get reloads() { return reloads; }, get confirmation() { return confirmation; },
        advance(ms) { now += ms; }};
}

const tests = [];
function test(name, fn) { tests.push({name, fn}); }

test('页面恢复运行任务、接入 SSE、按游标去重', async () => {
    const h = harness();
    h.responder = async () => ({code: 200, data: state('running', {logs: [line(1)]})});
    await h.api.syncBatchState();
    assert.equal(h.get('batch-start-btn').disabled, true);
    assert.equal(h.sources.length, 1);
    assert.match(h.sources[0].url, /cursor=1&run_id=run1/);
    h.sources[0].emit('log', line(1));
    h.sources[0].emit('log', line(2));
    assert.equal(h.get('batch-log-body').childElementCount, 2);
    h.sources[0].emit('progress', state('running', {progress_pct: 42.5}));
    assert.equal(h.get('progress-bar').style.width, '42.5%');
});

test('上翻不抢滚动、折叠展开保留位置、回到最新', async () => {
    const h = harness();
    h.api.adoptBatch(state());
    for (let i = 1; i <= 80; i++) h.api.appendLog(line(i));
    const body = h.get('batch-log-body');
    body.scrollTop = 100;
    await body.emit('scroll');
    h.api.appendLog(line(81));
    assert.equal(body.scrollTop, 100);
    await h.get('batch-log-toggle').emit('click');
    h.api.appendLog(line(82));
    await h.get('batch-log-toggle').emit('click');
    assert.equal(body.scrollTop, 100);
    await h.get('batch-log-follow').emit('click');
    assert.equal(body.scrollTop, body.scrollHeight - body.clientHeight);
    await h.get('batch-log-toggle').emit('click');
    h.api.appendLog(line(83));
    await h.get('batch-log-toggle').emit('click');
    assert.equal(body.scrollTop, body.scrollHeight - body.clientHeight);
});

test('DOM 日志缓冲有界、跨任务重置游标和颜色', async () => {
    const h = harness();
    h.api.adoptBatch(state());
    for (let i = 1; i <= 5010; i++) h.api.appendLog(line(i));
    assert.equal(h.get('batch-log-body').childElementCount, 5000);
    assert.match(h.get('batch-log-count').textContent, /较早 10 行/);
    h.api.updateProgressUI(state('error'));
    h.api.adoptBatch(state('running', {run_id: 'run2'}));
    h.api.appendLog(line(1, 'run2'));
    h.api.appendLog(line(5011));
    h.api.updateProgressUI(state('running', {run_id: 'run2'}));
    assert.equal(h.get('batch-log-body').childElementCount, 1);
    assert.notEqual(h.get('progress-bar').style.background, '#e74c3c');
});

test('SSE 与 HTTP 同时断网仍禁用启动并持续重试', async () => {
    const h = harness();
    await h.api.syncBatchState();
    const old = h.sources[0];
    old.onerror();
    h.responder = async () => { throw new Error('断网'); };
    await h.api.syncBatchState();
    assert.equal(h.get('batch-start-btn').disabled, true);
    assert.equal(old.closed, true);
    assert.match(h.get('batch-connection-msg').textContent, /自动重试/);
    assert(h.timers.size > 0);
    old.emit('done', state('completed'));
    assert.equal(h.get('batch-start-btn').disabled, true);
    h.responder = async () => ({code: 200, data: state('running', {logs: [line(1)]})});
    await h.api.syncBatchState();
    assert.equal(h.get('batch-log-body').childElementCount, 1);
});

test('无 EventSource 轮询也有日志和统一完成收尾', async () => {
    const h = harness(false);
    await h.api.syncBatchState();
    assert.equal(h.sources.length, 0);
    h.responder = async () => ({code: 200, data: state('completed', {progress_pct: 100, logs: [line(1)]})});
    await h.api.syncBatchState();
    assert.equal(h.get('batch-start-btn').disabled, false);
    assert.equal(h.get('progress-bar').style.width, '100%');
    assert.equal(h.reloads, 1);
    await h.api.syncBatchState();
    assert.equal(h.reloads, 1);
});

test('终态先接收尾日志再解锁按钮，失败终态也刷新', async () => {
    const h = harness();
    await h.api.syncBatchState();
    const stream = h.sources[0];
    stream.emit('progress', state('error'));
    assert.equal(h.get('batch-start-btn').disabled, true);
    stream.emit('log', line(1));
    stream.emit('done', state('error'));
    assert.equal(h.get('batch-log-body').childElementCount, 1);
    assert.equal(h.get('batch-start-btn').disabled, false);
    assert.equal(h.reloads, 1);
});

test('SSE 无响应或非法 JSON 自动降级而非永久卡住', async () => {
    const h = harness();
    await h.api.syncBatchState();
    h.advance(9000);
    await h.api.syncBatchState();
    assert.equal(h.sources[0].closed, true);
    h.advance(16000);
    await h.api.syncBatchState();
    assert.equal(h.sources.length, 2);
    h.sources[1].listeners.progress({data: '{坏数据'});
    assert.equal(h.sources[1].closed, true);
    assert(h.timers.size > 0);
});

test('并发启动冲突恢复已有任务，POST 超时只查询不重发', async () => {
    const h = harness();
    let posts = 0;
    h.responder = async (url) => {
        if (url === '/api/batch/update') { posts++; return {code: 409, data: state()}; }
        return {code: 200, data: state()};
    };
    await h.get('batch-start-btn').emit('click');
    await h.confirmation.onOk();
    await h.api.syncBatchState();
    assert.equal(posts, 1);
    assert.equal(h.sources.length, 1);
    const h2 = harness();
    h2.responder = async (url) => {
        if (url === '/api/batch/update') { posts++; throw new Error('超时'); }
        return {code: 200, data: state()};
    };
    await h2.get('batch-start-btn').emit('click');
    await h2.confirmation.onOk();
    assert.equal(h2.get('batch-start-btn').disabled, true);
    await h2.api.syncBatchState();
    assert.equal(posts, 2);
    assert.equal(h2.sources.length, 1);
});

test('旧查询响应不能覆盖新提交的任务', async () => {
    const h = harness();
    let resolveOld;
    h.responder = async url => url === '/api/batch/update'
        ? {code: 200, data: state('running', {run_id: 'new'})}
        : new Promise(resolve => { resolveOld = resolve; });
    const pending = h.api.syncBatchState();
    await h.get('batch-start-btn').emit('click');
    await h.confirmation.onOk();
    resolveOld({code: 200, data: state('completed', {run_id: 'old'})});
    await pending;
    assert.equal(h.api.getState().activeRunId, 'new');
    assert.equal(h.get('batch-start-btn').disabled, true);
});

test('页面离开清理连接，返回后重新同步', async () => {
    const h = harness();
    await h.api.syncBatchState();
    h.events.pagehide();
    assert.equal(h.sources[0].closed, true);
    assert.equal(h.timers.size, 0);
    h.events.pageshow();
    assert(h.timers.size > 0);
    await h.api.syncBatchState();
    assert.equal(h.sources.length, 2);
});

(async () => {
    for (const {name, fn} of tests) {
        await fn();
        console.log('通过：' + name);
    }
    console.log('全部 ' + tests.length + ' 项前端回归通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
