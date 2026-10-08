/* 自定义筛选引擎离线回归：node crypto/static/js/_smoke_batch_filter.js
 * 从 main.js 抽取纯求值函数（不触发 DOM），验证多周期组合、指标阈值、区间、
 * 跨周期趋势相等、空值/无效数据处理与预设条件。
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');
function slice(from, to) {
    const a = source.indexOf(from);
    const b = source.indexOf(to, a + from.length);
    assert(a > 0 && b > a, '抽取边界缺失: ' + from);
    return source.slice(a, b);
}
const code =
    slice("const FILTER_PERIODS = ['15m', '1H', '4H', '1D'];", 'let currentView') +
    slice('// ---- 自定义筛选：条件模型 ----', '// ---- 自定义筛选：构建器渲染 ----') +
    slice('function filterOptions', 'function renderFilterBuilder') +
    'globalThis.api = { evalCondition, matchesFilters, makeCondition, filterColumn, ' +
    'setConds: (c) => { customConditions = c; }, addCond: (c) => customConditions.push(c) };';

const context = vm.createContext({ customConditions: [], condSeq: 0 });
vm.runInContext(code, context);
const api = context.api;

// 构造一行测试数据（列名与 read_csv_for_display 一致）
function row(t, opts) {
    opts = opts || {};
    return Object.assign({
        '15m_趋势': t[0], '1H_趋势': t[1], '4H_趋势': t[2], '1D_趋势': t[3],
        'ADX_4H': opts.adx4h, 'ADX_15m': opts.adx15m, 'ATR_4H': opts.atr4h,
        'MACD_1D': opts.macd1d, 'DIF_4H': opts.dif4h, 'ER_4H': opts.er4h,
        'SAR_1D': opts.sar1d, '4H_盈亏%': opts.profit4h
    }, opts.extra || {});
}
const cond = (field, period, op, value, value2) => ({ field, period, op, value, value2: value2 || '' });

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

test('跨周期组合：4H上涨且15m下跌', () => {
    const cs = [cond('趋势', '4H', '等于', '上涨'), cond('趋势', '15m', '等于', '下跌')];
    const hit = row(['下跌', '上涨', '上涨', '观望']);
    const miss = row(['上涨', '上涨', '上涨', '上涨']);
    assert.equal(cs.every(c => api.evalCondition(hit, c)), true);
    assert.equal(cs.every(c => api.evalCondition(miss, c)), false);
});

test('趋势不等于：排除上涨但保留下跌/观望', () => {
    const c = cond('趋势', '4H', '不等于', '上涨');
    assert.equal(api.evalCondition(row(['', '', '下跌', '']), { ...c }), true);
    assert.equal(api.evalCondition(row(['', '', '观望', '']), c), true);
    assert.equal(api.evalCondition(row(['', '', '上涨', '']), c), false);
});

test('跨周期一致（同1D）：都为上涨/下跌且相同才命中', () => {
    const c = cond('趋势', '4H', '等于', '同1D');
    assert.equal(api.evalCondition(row(['', '', '上涨', '上涨']), c), true);
    assert.equal(api.evalCondition(row(['', '', '下跌', '下跌']), c), true);
    assert.equal(api.evalCondition(row(['', '', '上涨', '下跌']), c), false);
    assert.equal(api.evalCondition(row(['', '', '观望', '观望']), c), false, '两者观望不算一致');
    assert.equal(api.evalCondition(row(['', '', '上涨', '']), c), false, '空值不算一致');
});

test('跨周期不一致（不等于 同1D）', () => {
    const c = cond('趋势', '4H', '不等于', '同1D');
    assert.equal(api.evalCondition(row(['', '', '上涨', '下跌']), c), true);
    assert.equal(api.evalCondition(row(['', '', '上涨', '上涨']), c), false);
});

test('数值阈值 >,>=,<,<=,=', () => {
    const r = row(['', '', '', ''], { adx4h: '30' });
    assert.equal(api.evalCondition(r, cond('ADX', '4H', '>', '25')), true);
    assert.equal(api.evalCondition(r, cond('ADX', '4H', '>=', '30')), true);
    assert.equal(api.evalCondition(r, cond('ADX', '4H', '<', '30')), false);
    assert.equal(api.evalCondition(r, cond('ADX', '4H', '<=', '30')), true);
    assert.equal(api.evalCondition(r, cond('ADX', '4H', '=', '30')), true);
    assert.equal(api.evalCondition(r, cond('ADX', '4H', '=', '29')), false);
});

test('区间：双界/单边/未填都不越界误判', () => {
    const r = row(['', '', '', ''], { macd1d: '5' });
    assert.equal(api.evalCondition(r, cond('MACD', '1D', '区间', '0', '10')), true);
    assert.equal(api.evalCondition(r, cond('MACD', '1D', '区间', '6', '10')), false);
    assert.equal(api.evalCondition(r, cond('MACD', '1D', '区间', '', '10')), true, '只填上界');
    assert.equal(api.evalCondition(r, cond('MACD', '1D', '区间', '0', '')), true, '只填下界');
    assert.equal(api.evalCondition(r, cond('MACD', '1D', '区间', '', '')), true, '两端空=不限制');
});

test('空值/无效数据：数值条件不命中，未填阈值则不限制', () => {
    for (const bad of ['', '--', 'abc', null, undefined]) {
        const r = row(['', '', '', ''], { atr4h: bad });
        assert.equal(api.evalCondition(r, cond('ATR', '4H', '>', '1')), false, '无效值不应命中: ' + bad);
        assert.equal(api.evalCondition(r, cond('ATR', '4H', '>', '')), true, '未填阈值=不限制');
    }
});

test('matchesFilters 读取全局条件数组（AND 组合）', () => {
    api.setConds([]);
    api.addCond(cond('趋势', '4H', '等于', '上涨'));
    api.addCond(cond('ADX', '4H', '>=', '25'));
    assert.equal(api.matchesFilters(row(['', '', '上涨', ''], { adx4h: '40' })), true);
    assert.equal(api.matchesFilters(row(['', '', '上涨', ''], { adx4h: '10' })), false);
    assert.equal(api.matchesFilters(row(['', '', '下跌', ''], { adx4h: '40' })), false);
});

test('预设条件与列名映射', () => {
    const c1 = api.makeCondition('consistent');
    assert.deepEqual([c1.field, c1.period, c1.op, c1.value], ['趋势', '4H', '等于', '同1D']);
    const c2 = api.makeCondition('strong');
    assert.deepEqual([c2.field, c2.period, c2.op, c2.value], ['ADX', '4H', '>=', '25']);
    assert.equal(api.filterColumn('盈亏', '4H'), '4H_盈亏%');
    assert.equal(api.filterColumn('趋势', '15m'), '15m_趋势');
    assert.equal(api.filterColumn('ADX', '1D'), 'ADX_1D');
});

let failed = 0;
tests.forEach(({ name, fn }) => {
    try { fn(); console.log('通过：' + name); }
    catch (e) { failed++; console.error('失败：' + name + '\n  ' + e.message); }
});
console.log(failed ? (failed + ' 项失败') : ('全部 ' + tests.length + ' 项筛选引擎回归通过'));
process.exitCode = failed ? 1 : 0;
