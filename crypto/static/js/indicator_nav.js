/* ============================================================================
 * 指标导航 · 趋势方法可视化回测页（/indicator-nav）
 * --------------------------------------------------------------------------
 * 控制区（币种/周期/指标/高级参数） → 调 /indicator-nav/api/backtest 一次性
 * 取回图表载荷、统计、逐笔交易与资金曲线；ECharts 渲染 K 线 + BOLL 轨道 +
 * 买卖点标记 + 持仓区间着色。指标横向对比表来自 /api/compare（点击行即切换
 * 指标重跑）。本文件只做展示与请求编排，所有计算在服务端完成。
 * ==========================================================================*/
(function () {
    'use strict';

    var $ = function (id) { return document.getElementById(id); };

    // 币种池：与 kline_fetcher/symbols.py 的 COINS 配置保持一致
    var SYMBOLS = ['near', 'btc', 'eth', 'sol', 'xrp', 'ada', 'doge', 'dot', 'link', 'ltc'];
    var DEFAULTS = { symbol: 'near', short_bar: '15m', long_bar: '4H', indicator: 'keltner' };

    var kChart = null;        // K 线主图 ECharts 实例
    var moneyChart = null;    // 资金曲线 ECharts 实例
    var lastResult = null;    // 最近一次回测结果（行点击定位用；含 chart/chart_long 两份载荷）
    var cmpLoadedKey = '';    // 已加载对比表的 symbol|long_bar
    var running = false;
    var curView = 'short';    // 图表视图：short=短周期持仓区间，long=长周期趋势方向段

    // ---------------------------------------------------------------- 工具
    function setMsg(text, cls) {
        var el = $('in-msg');
        el.className = 'in-msg' + (cls ? ' ' + cls : '');
        el.textContent = text || '';
    }

    function fillSelect(sel, options, selected) {
        sel.innerHTML = '';
        options.forEach(function (opt) {
            var value = (typeof opt === 'string') ? opt : opt.value;
            var label = (typeof opt === 'string') ? opt : opt.label;
            var o = document.createElement('option');
            o.value = value;
            o.textContent = label;
            if (value === selected) o.selected = true;
            sel.appendChild(o);
        });
    }

    function fmtPct(v, digits) {
        if (v === null || v === undefined || isNaN(v)) return '--';
        return (v >= 0 ? '+' : '') + Number(v).toFixed(digits === undefined ? 2 : digits) + '%';
    }

    function fmtNum(v, digits) {
        if (v === null || v === undefined || isNaN(v)) return '--';
        return Number(v).toFixed(digits === undefined ? 2 : digits);
    }

    // 服务端字符串（指标名/错误信息）入 innerHTML 前先转义，避免标记注入
    function esc(s) {
        return String(s === null || s === undefined ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function currentParams() {
        return {
            symbol: $('in-symbol').value,
            short_bar: $('in-short-bar').value,
            long_bar: $('in-long-bar').value,
            indicator: $('in-indicator').value,
            view: curView,
            leverage_times: $('in-leverage').value || '10',
            entry_atr_mult: $('in-entry-atr').value || '0.7',
            exit_atr_mult: $('in-exit-atr').value || '0.3',
            chart_bars: $('in-chart-bars').value || '2000'
        };
    }

    function buildQuery(params) {
        return Object.keys(params).map(function (k) {
            return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
        }).join('&');
    }

    // ---------------------------------------------------------------- K 线主图
    // 图例文案随视图切换：短周期=实盘持仓区间+买卖点；长周期=趋势指标直接切出的多空方向段
    function renderChartTitle(view, p) {
        var el = $('in-chart-title');
        if (view === 'long') {
            el.innerHTML =
                '<span><span class="in-legend-dot" style="background:#d32f2f"></span>看多方向段（' + esc(p.indicator) + ' @' + esc(p.long_bar) + '）</span>' +
                '<span><span class="in-legend-dot" style="background:#2e7d32"></span>看空方向段</span>' +
                '<span><span class="in-legend-dot" style="background:#4356c0"></span>BOLL(20,2) 轨道（长周期）</span>' +
                '<span>▲▼ 长周期回测买卖点</span>' +
                '<span style="color:#999">底色=长周期多空方向段；统计/资金曲线/逐笔均为长周期回测口径</span>';
        } else {
            el.innerHTML =
                '<span><span class="in-legend-dot" style="background:#d32f2f"></span>做多持仓区间 / ▲ 开多</span>' +
                '<span><span class="in-legend-dot" style="background:#2e7d32"></span>做空持仓区间 / ▼ 开空</span>' +
                '<span><span class="in-legend-dot" style="background:#4356c0"></span>BOLL(20,2) 轨道</span>' +
                '<span style="color:#999">滚轮缩放 · 拖动平移 · 点击交易行可定位</span>';
        }
    }

    function renderKline(chart, view, p) {
        var el = $('in-kline');
        if (!kChart) kChart = echarts.init(el);
        var isLong = view === 'long';

        // 持仓区间 markArea 数据：多=红底、空=绿底（对齐国内涨红跌绿习惯）；
        // 长周期视图下 zones 即趋势多空方向段
        var zones = (chart.zones || []).map(function (z) {
            return [{
                xAxis: z[0],
                itemStyle: { color: z[2] === 'rise' ? 'rgba(211,47,47,0.07)' : 'rgba(46,125,50,0.07)' }
            }, { xAxis: z[1] }];
        });

        // 买卖点标记 → 四个散点系列（开多/开空/平多/平空）
        function scatter(side, dir) {
            return (chart.markers || []).filter(function (m) {
                return m.side === side && m.dir === dir;
            }).map(function (m) {
                return {
                    value: [m.idx, m.price],
                    excluded: !!m.excluded,
                    time: m.time
                };
            });
        }

        var series = [
            {
                name: 'K线', type: 'candlestick', data: chart.candles,
                itemStyle: {
                    color: '#ef5350', color0: '#26a69a',
                    borderColor: '#ef5350', borderColor0: '#26a69a'
                },
                markArea: {
                    silent: true,
                    data: zones
                }
            },
            { name: 'BOLL上轨', type: 'line', data: chart.boll_top, showSymbol: false,
              lineStyle: { width: 1, color: '#7d8cff' }, emphasis: { disabled: true } },
            { name: 'BOLL中轨', type: 'line', data: chart.boll_mid, showSymbol: false,
              lineStyle: { width: 1, color: '#b3bcf5', type: 'dashed' }, emphasis: { disabled: true } },
            { name: 'BOLL下轨', type: 'line', data: chart.boll_bot, showSymbol: false,
              lineStyle: { width: 1, color: '#7d8cff' }, emphasis: { disabled: true } },
            { name: '开多', type: 'scatter', data: scatter('entry', 'rise'), symbol: 'triangle',
              symbolSize: 11, itemStyle: { color: '#d32f2f' } },
            { name: '开空', type: 'scatter', data: scatter('entry', 'fall'), symbol: 'triangle',
              symbolRotate: 180, symbolSize: 11, itemStyle: { color: '#2e7d32' } },
            { name: '平多', type: 'scatter', data: scatter('exit', 'rise'), symbol: 'pin',
              symbolSize: 13, itemStyle: { color: '#ff8a65' } },
            { name: '平空', type: 'scatter', data: scatter('exit', 'fall'), symbol: 'pin',
              symbolSize: 13, itemStyle: { color: '#81c784' } }
        ];

        kChart.setOption({
            animation: false,
            tooltip: {
                trigger: 'axis',
                axisPointer: { type: 'cross' },
                confine: true,
                formatter: function (ps) {
                    if (!ps || !ps.length) return '';
                    var idx = ps[0].dataIndex;
                    var t = chart.times[idx] || '';
                    var lines = ['<b>' + t + '</b>'];
                    ps.forEach(function (p) {
                        if (p.seriesType === 'candlestick') {
                            var v = p.data;   // [open, close, low, high]
                            lines.push('开 ' + v[1] + '　收 ' + v[2]);
                            lines.push('低 ' + v[3] + '　高 ' + v[4]);
                        } else if (p.seriesType === 'line' && p.data != null) {
                            lines.push(p.seriesName + ' ' + Number(p.data).toFixed(4));
                        } else if (p.seriesType === 'scatter') {
                            var d = p.data || {};
                            lines.push(p.seriesName + ' @ ' + (d.value ? d.value[1] : '') +
                                (d.excluded ? '（剔除单）' : ''));
                        }
                    });
                    var ld = chart.long_dir ? chart.long_dir[idx] : 0;
                    var dirLabel = ld >= 0.5 ? '多 ↑' : (ld <= -0.5 ? '空 ↓' : '中性');
                    lines.push((isLong ? '长周期方向段: ' : '长周期方向: ') + dirLabel);
                    return lines.join('<br>');
                }
            },
            legend: { top: 0, type: 'scroll', data: ['BOLL上轨', 'BOLL中轨', 'BOLL下轨', '开多', '开空', '平多', '平空'] },
            grid: { left: 58, right: 20, top: 30, bottom: 58 },
            xAxis: {
                type: 'category', data: chart.times, boundaryGap: true,
                axisLine: { lineStyle: { color: '#999' } },
                axisLabel: { fontSize: 10 }
            },
            yAxis: { scale: true, axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: '#f0f2f4' } } },
            dataZoom: [
                { type: 'inside', start: 60, end: 100 },
                { type: 'slider', start: 60, end: 100, height: 20, bottom: 8 }
            ],
            series: series
        }, true);
    }

    // 顶部角标：随视图展示当前周期的根数/时间跨度
    function renderHeader(data) {
        var isLong = curView === 'long';
        var chart = isLong ? data.chart_long : data.chart;
        var period = isLong ? data.params.long_bar : data.params.short_bar;
        var totalBars = isLong ? data.data_range.long_bars : data.data_range.total_bars;
        var t = (chart && chart.times) || [];
        var start = t.length ? t[0] : (data.data_range.start || '--');
        var end = t.length ? t[t.length - 1] : (data.data_range.end || '--');
        var srcText = data.source === 'csv' ? '数据源：本地 1m CSV（全量历史）' : '数据源：OKX 公共行情（近120天）';
        $('in-src-badge').textContent = srcText + ' · ' + (isLong ? '长周期 ' : '短周期 ') +
            totalBars + ' 根 ' + period + ' · ' + (start || '--') + ' ~ ' + (end || '--') +
            ' · 耗时 ' + data.elapsed_ms + 'ms';
    }

    // 按当前视图渲染全部（图表+图例+角标+统计+资金曲线+逐笔）；
    // 长/短两套数据在同一次回测响应里已一并返回，切换视图无需重跑
    function renderResult() {
        if (!lastResult) return;
        var d = lastResult;
        var isLong = curView === 'long';
        var chart = isLong ? d.chart_long : d.chart;
        if (!chart) return;
        renderKline(chart, curView, d.params);
        renderChartTitle(curView, d.params);
        renderHeader(d);
        renderStats(d);
        renderMoney((isLong ? d.money_curves_long : d.money_curves) || {});
        renderTrades((isLong ? d.trades_long : d.trades) || []);
    }

    // 定位到某笔交易（表行点击）：把 dataZoom 窗口挪到该区间。
    // 逐笔表与主图均按当前视图取对应周期数据，交易索引与偏移在同一周期空间内，无需切视图
    function focusTrade(tr) {
        if (!kChart || !lastResult) return;
        var chart = curView === 'long' ? lastResult.chart_long : lastResult.chart;
        if (!chart) return;
        var lo = chart.offset || 0;
        var s = tr.entry_idx - lo;
        var e = tr.exit_idx - lo;
        var total = chart.times.length;
        if (e < 0 || s >= total) {
            setMsg('该笔交易不在当前图表窗口内（图表仅展示最近 ' + total + ' 根 bar），可调大「图表 bar 数」', 'note');
            return;
        }
        s = Math.max(s, 0);
        var pad = Math.max(Math.round((e - s) * 0.6), 30);
        var a = Math.max(0, s - pad);
        var b = Math.min(total - 1, e + pad);
        kChart.dispatchAction({
            type: 'dataZoom',
            startValue: a,
            endValue: b
        });
    }

    // ---------------------------------------------------------------- 统计卡片
    function statCard(k, v, cls) {
        return '<div class="in-stat"><div class="k">' + k + '</div><div class="v' +
            (cls ? ' ' + cls : '') + '">' + v + '</div></div>';
    }

    function renderStats(r) {
        // 视图联动：长周期视图取 stats_long（长周期回测口径），否则取 stats
        var s = curView === 'long' ? (r.stats_long || r.stats) : r.stats;
        var seg = r.segments || {};   // 趋势段特征本就基于长周期数据（两视图共用）
        var p = r.params;
        var html = '';
        html += statCard('总交易次数', s.total_trades + ' <small>笔</small>');
        html += statCard('胜率', fmtNum(s.win_rate * 100) + ' <small>%（' + s.winning_trades + '胜/' + s.losing_trades + '负）</small>');
        html += statCard('盈亏比（均值比）', fmtNum(s.final_mean_pl_ratio));
        html += statCard('盈利因子', fmtNum(s.profit_factor));
        html += statCard('定投收益', fmtNum(s.fix_money) + ' <small>起点100</small>',
            s.fix_money >= 100 ? 'pos' : 'neg');
        html += statCard('复投收益（复利）', fmtNum(s.contract_money) + ' <small>起点100</small>',
            s.contract_money >= 100 ? 'pos' : 'neg');
        html += statCard('混合收益', fmtNum(s.mix_money) + ' <small>起点100</small>',
            s.mix_money >= 100 ? 'pos' : 'neg');
        html += statCard('真实定投（含剔除亏）', fmtNum(s.fix_real_money));
        html += statCard('定投最大回撤', fmtNum(s.fix_drawdown) + ' <small>%</small>');
        html += statCard('复投最大回撤', fmtNum(s.contract_drawdown) + ' <small>%</small>');
        html += statCard('日均盈利', fmtPct(s.daily_profit));
        html += statCard('周均盈利', fmtPct(s.weekly_profit));
        html += statCard('趋势段平均持续', fmtNum(seg.avg_dur_hours, 1) + ' <small>h（' + esc(p.indicator) + ' @' + esc(p.long_bar) + '）</small>');
        html += statCard('趋势段日均反转', fmtNum(seg.reversals_per_day, 3));
        html += statCard('平均持仓时长', fmtNum(s.avg_hold_hours, 1) + ' <small>h</small>');
        html += statCard('剔除交易', s.excluded_trades + ' <small>笔（' + fmtNum(s.excluded_ratio * 100, 1) + '%）</small>');
        $('in-stats').innerHTML = html;
    }

    // ---------------------------------------------------------------- 资金曲线
    function renderMoney(mc) {
        var el = $('in-money');
        if (!moneyChart) moneyChart = echarts.init(el);
        var xs = ['起点'].concat(mc.times || []);
        function line(name, vals, color) {
            return { name: name, type: 'line', showSymbol: false, data: [100].concat(vals || []),
                     lineStyle: { width: 1.6, color: color }, itemStyle: { color: color },
                     emphasis: { disabled: true } };
        }
        moneyChart.setOption({
            animation: false,
            tooltip: { trigger: 'axis', confine: true },
            legend: { top: 0, type: 'scroll' },
            grid: { left: 52, right: 14, top: 28, bottom: 40 },
            xAxis: { type: 'category', data: xs, axisLabel: { fontSize: 9 } },
            yAxis: { scale: true, axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: '#f0f2f4' } } },
            dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 6 }],
            series: [
                line('定投', mc.fix, '#4356c0'),
                line('复投', mc.contract, '#e53935'),
                line('混投', mc.mix, '#fb8c00'),
                line('真实定投', mc.fix_real, '#8e9aaf')
            ]
        }, true);
    }

    // ---------------------------------------------------------------- 交易记录
    function renderTrades(trades) {
        var tbody = $('in-trade-table').querySelector('tbody');
        tbody.innerHTML = '';
        $('in-trade-empty').style.display = trades.length ? 'none' : 'block';
        $('in-trade-count').textContent = '共 ' + trades.length + ' 笔已平仓';
        trades.forEach(function (t) {
            var tr = document.createElement('tr');
            tr.className = 'in-trade-row';
            var dirHtml = '<span class="in-dir-badge ' + t.dir + '">' + (t.dir === 'rise' ? '做多' : '做空') + '</span>' +
                (t.excluded ? '<span class="in-excl-badge">剔除</span>' : '');
            var pnlCls = t.profit > 0 ? 'pos' : (t.profit < 0 ? 'neg' : '');
            tr.innerHTML = '<td>' + t.no + '</td>' +
                '<td>' + dirHtml + '</td>' +
                '<td>' + t.entry_time + '</td>' +
                '<td>' + t.open_price + '</td>' +
                '<td>' + t.exit_time + '</td>' +
                '<td>' + t.close_price + '</td>' +
                '<td>' + fmtNum(t.hold_hours, 1) + '</td>' +
                '<td class="in-pnl ' + pnlCls + '">' + fmtPct(t.profit) + '</td>' +
                '<td>' + fmtPct(t.max_win) + '</td>' +
                '<td>' + fmtPct(t.max_loss) + '</td>' +
                '<td>' + fmtNum(t.fix_after, 0) + '</td>' +
                '<td>' + fmtNum(t.contract_after, 0) + '</td>' +
                '<td>' + fmtNum(t.mix_after, 0) + '</td>';
            tr.addEventListener('click', function () { focusTrade(t); });
            tbody.appendChild(tr);
        });
    }

    // ---------------------------------------------------------------- 指标对比表
    function renderCompare(rows, activeIndicator) {
        var tbody = $('in-cmp-table').querySelector('tbody');
        tbody.innerHTML = '';
        rows.forEach(function (r) {
            var tr = document.createElement('tr');
            tr.setAttribute('data-indicator', r.indicator);
            tr.className = 'in-cmp-row' + (r.indicator === activeIndicator ? ' sel' : '');
            if (r.error) {
                tr.innerHTML = '<td>' + esc(r.indicator) + '</td><td colspan="6" style="color:#c62828">计算失败: ' + esc(r.error) + '</td>';
            } else {
                tr.innerHTML = '<td>' + esc(r.indicator) + '</td>' +
                    '<td>' + r.num_segments + '</td>' +
                    '<td>' + r.reversals + '</td>' +
                    '<td>' + fmtNum(r.avg_dur_hours, 1) + '</td>' +
                    '<td>' + fmtNum(r.median_dur_hours, 1) + '</td>' +
                    '<td>' + fmtNum(r.coverage * 100, 1) + '%</td>' +
                    '<td>' + fmtNum(r.reversals_per_day, 3) + '</td>';
                tr.addEventListener('click', function () {
                    if ($('in-indicator').value === r.indicator) return;
                    $('in-indicator').value = r.indicator;
                    runBacktest();
                });
            }
            tbody.appendChild(tr);
        });
    }

    // 切换指标后让对比表选中行高亮跟随当前指标（renderCompare 仅在重新拉取时打标）
    function syncCompareSelection() {
        var active = $('in-indicator').value;
        var tbody = $('in-cmp-table').querySelector('tbody');
        if (!tbody) return;
        var trs = tbody.querySelectorAll('tr[data-indicator]');
        for (var i = 0; i < trs.length; i++) {
            trs[i].classList.toggle('sel', trs[i].getAttribute('data-indicator') === active);
        }
    }

    function loadCompare(force) {
        var symbol = $('in-symbol').value;
        var longBar = $('in-long-bar').value;
        var key = symbol + '|' + longBar;
        if (!force && key === cmpLoadedKey) return;
        cmpLoadedKey = key;
        var tbody = $('in-cmp-table').querySelector('tbody');
        tbody.innerHTML = '<tr><td colspan="7" style="color:#999">对比计算中（全部指标 × ' + longBar + ' 长周期数据，首次约需数秒）…</td></tr>';
        fetch('/indicator-nav/api/compare?' + buildQuery({ symbol: symbol, long_bar: longBar }))
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (!data.success) {
                    tbody.innerHTML = '<tr><td colspan="7" style="color:#c62828">' + (data.error || '加载失败') + '</td></tr>';
                    return;
                }
                renderCompare(data.rows || [], $('in-indicator').value);
            })
            .catch(function (e) {
                tbody.innerHTML = '<tr><td colspan="7" style="color:#c62828">请求失败: ' + e + '</td></tr>';
            });
    }

    // ---------------------------------------------------------------- 回测主流程
    function runBacktest() {
        if (running) return;
        running = true;
        var btn = $('in-run');
        btn.disabled = true;
        var p = currentParams();
        setMsg('回测计算中…（' + p.symbol.toUpperCase() + ' ' + p.short_bar + ' × ' + p.long_bar + ' × ' + p.indicator + '）', 'busy');
        fetch('/indicator-nav/api/backtest?' + buildQuery(p))
            .then(function (resp) { return resp.json().then(function (d) { return { ok: resp.ok, body: d }; }); })
            .then(function (r) {
                if (!r.body.success) {
                    setMsg(r.body.error || ('请求失败（HTTP ' + (r.ok ? '?' : '错误') + '）'), 'err');
                    return;
                }
                var data = r.body;
                lastResult = data;
                // 后端按 params.view 回显视图（前端切换视图不重跑，仅重跑时使用当前视图）
                curView = (data.params && data.params.view === 'long') ? 'long' : 'short';
                $('in-view').value = curView;
                var noteMsg = (data.notes || []).filter(function (x) { return x && x.trim(); }).join('；');
                setMsg(noteMsg, noteMsg ? 'note' : '');
                renderResult();
                // 对比表：同 symbol+long_bar 只拉一次；指标下拉高亮随行选择
                loadCompare(false);
                var sel = $('in-indicator');
                if (data.indicators && data.indicators.length && sel.options.length !== data.indicators.length) {
                    fillSelect(sel, data.indicators, data.params.indicator);
                }
                syncCompareSelection();
            })
            .catch(function (e) {
                setMsg('网络请求失败: ' + e, 'err');
            })
            .finally(function () {
                running = false;
                btn.disabled = false;
            });
    }

    // ---------------------------------------------------------------- 初始化
    function initMeta() {
        fillSelect($('in-symbol'), SYMBOLS, DEFAULTS.symbol);
        fetch('/indicator-nav/api/meta')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var shorts = (data.short_bar_options && data.short_bar_options.length) ? data.short_bar_options : ['15m', '1H', '4H'];
                var longs = (data.long_bar_options && data.long_bar_options.length) ? data.long_bar_options : ['4H', '1D'];
                var inds = (data.indicators && data.indicators.length) ? data.indicators : ['keltner', 'trix_ma'];
                fillSelect($('in-short-bar'), shorts, DEFAULTS.short_bar);
                fillSelect($('in-long-bar'), longs, DEFAULTS.long_bar);
                fillSelect($('in-indicator'), inds, DEFAULTS.indicator);
            })
            .catch(function () {
                // meta 失败也保底可用（浏览器离线时给出最小档位）
                fillSelect($('in-short-bar'), ['15m', '1H', '4H'], DEFAULTS.short_bar);
                fillSelect($('in-long-bar'), ['4H', '1D'], DEFAULTS.long_bar);
                fillSelect($('in-indicator'), ['keltner', 'trix_ma'], DEFAULTS.indicator);
            })
            .finally(function () {
                runBacktest();
            });
    }

    function bindEvents() {
        $('in-run').addEventListener('click', runBacktest);
        // 周期 / 指标 / 币种切换即自动刷新（需求：切换时自动刷新所有数据和图表）
        ['in-symbol', 'in-short-bar', 'in-long-bar', 'in-indicator'].forEach(function (id) {
            $(id).addEventListener('change', function () {
                runBacktest();
                if (id === 'in-symbol' || id === 'in-long-bar') loadCompare(false);
            });
        });
        // 视图切换：长/短两套图表+统计+资金曲线+逐笔在同一次回测结果里已一并返回，
        // 本地全量重渲染即可（统计口径随之切到长/短周期），不重跑回测
        $('in-view').addEventListener('change', function () {
            curView = this.value === 'long' ? 'long' : 'short';
            if (lastResult) renderResult();
            else runBacktest();
        });
        $('in-adv-toggle').addEventListener('click', function () {
            var adv = $('in-adv');
            adv.classList.toggle('show');
            this.textContent = adv.classList.contains('show') ? '收起高级参数 ▴' : '高级参数 ▾';
        });
        window.addEventListener('resize', function () {
            if (kChart) kChart.resize();
            if (moneyChart) moneyChart.resize();
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        bindEvents();
        initMeta();
    });
})();
