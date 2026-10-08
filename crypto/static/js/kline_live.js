/* eslint-env browser */
/**
 * 监控台币种 1 分钟 K 线实时拉取 —— /kline-live 页面逻辑
 * ==================================================
 * 轮询 /api/status 渲染「币种总览 + 当前币种明细卡」，按当前币种×类型拉 /api/bars 渲染明细表。
 * 币种范国内嵌在 kv 配置里（coins 白名单，空=跟随监控台全部币种）；ccxt 无数据源的币标灰展示而不报错。
 * 页面本身绝不触发网络抓取（除了「立即拉取」与「全量回填」两个按钮），避免刷新页面＝打爆交易所。
 */
(function () {
    'use strict';

    var API = '/kline-live/api';
    var STATUS_MS = 5000;      // 进度轮询间隔
    var BARS_MS = 15000;       // 明细表刷新间隔（比进度慢，减少 CSV 尾读）

    var el = {
        tabs: document.getElementById('kl-tabs'),
        cards: document.getElementById('kl-cards'),
        feed: document.getElementById('kl-feed-list'),
        tbody: document.querySelector('#kl-table tbody'),
        tableEmpty: document.getElementById('kl-table-empty'),
        barsMeta: document.getElementById('kl-bars-meta'),
        stateBadge: document.getElementById('kl-state-badge'),
        serverTime: document.getElementById('kl-server-time'),
        intervalTxt: document.getElementById('kl-interval-txt'),
        nextRun: document.getElementById('kl-next-run'),
        auto: document.getElementById('kl-auto'),
        maxInput: document.getElementById('kl-max'),
        sinceInput: document.getElementById('kl-since'),
        modeSel: document.getElementById('kl-mode'),
        intervalMsInput: document.getElementById('kl-interval-ms'),
        budgetInput: document.getElementById('kl-budget'),
        pullBtn: document.getElementById('kl-pull'),
        bfStartBtn: document.getElementById('kl-bf-start'),
        bfStopBtn: document.getElementById('kl-bf-stop'),
        auditBtn: document.getElementById('kl-audit'),
        auditOut: document.getElementById('kl-audit-out'),
        saveBtn: document.getElementById('kl-save'),
        toggleBtn: document.getElementById('kl-toggle'),
        limitSel: document.getElementById('kl-limit'),
        coinSel: document.getElementById('kl-coin'),
        coinEditBtn: document.getElementById('kl-coin-edit'),
        coinPick: document.getElementById('kl-coin-pick'),
        coinCount: document.getElementById('kl-coin-count'),
        coinBody: document.querySelector('#kl-coin-table tbody'),
        coinMeta: document.getElementById('kl-coin-meta'),
        nosrcMeta: document.getElementById('kl-nosrc-meta'),
        msg: document.getElementById('kl-msg')
    };

    var state = {
        streams: [],          // meta 下发的流清单 [{key,label,symbol,coin,stream_type,available,enabled}]
        types: [],            // [{value,label}] 永续/现货
        coins: [],            // 监控台币种 base 清单（按市值降序）
        enabledCoins: [],     // 配置里的币种白名单（空=全部）
        activeCoin: 'BTC',    // 当前查看的币种（'*' = 全部）
        activeType: 'swap',   // 当前查看的流类型
        status: null,
        offsetMs: 0,          // 服务器墙钟与本地时钟的偏差（倒计时用）
        lastBarsTs: 0,        // 表格首行时间戳，用于「本轮新增」高亮
        editingConfig: false  // 用户正在改配置时不覆盖输入框
    };

    // ---------------------------------------------------------------- 当前查看的流
    function coinStreamKey(coin, type) {
        return String(coin || 'BTC').toLowerCase() + ':' + (type || state.activeType || 'swap');
    }

    // 选了「全部币种」时明细表没得选，退到第一条已有台账的流（空台账时看第一条）
    function barsStreamKey() {
        if (state.activeCoin !== '*') return coinStreamKey(state.activeCoin);
        var list = (state.status && state.status.streams) || [];
        for (var i = 0; i < list.length; i++) {
            if (list[i].rows_total > 0) return list[i].key;
        }
        return list.length ? list[0].key : coinStreamKey('BTC');
    }

    // ---------------------------------------------------------------- 工具
    function parseWallClock(s) {
        // 'YYYY-MM-DD HH:MM:SS'（服务器本地墙钟）→ Date（按浏览器本地时区解释）
        if (!s) return null;
        var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(String(s));
        if (!m) return null;
        return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
    }

    function fmtNum(v, digits) {
        if (v === null || v === undefined || v === '' || isNaN(Number(v))) return '--';
        return Number(v).toLocaleString('zh-CN', {
            minimumFractionDigits: digits === undefined ? 1 : digits,
            maximumFractionDigits: digits === undefined ? 1 : digits
        });
    }

    function fmtInt(v) {
        if (v === null || v === undefined || isNaN(Number(v))) return '--';
        return Number(v).toLocaleString('zh-CN');
    }

    function fmtBytes(n) {
        if (!n) return '--';
        if (n < 1024) return n + ' B';
        if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
        return (n / 1048576).toFixed(2) + ' MB';
    }

    function hhmmss(d) {
        function p(x) { return (x < 10 ? '0' : '') + x; }
        return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    }

    function shortTime(s) {
        var d = parseWallClock(s);
        return d ? d.toLocaleTimeString('zh-CN', { hour12: false }) : (s || '--');
    }

    function dayTime(s) {
        // 地板时间/回填游标都是「几年前」，只显示时分不够用，给到分钟
        var d = parseWallClock(s);
        if (!d) return s || '--';
        function p(x) { return (x < 10 ? '0' : '') + x; }
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
            ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    }

    function fmtDur(sec) {
        if (sec === null || sec === undefined || isNaN(Number(sec))) return '--';
        var s = Math.max(0, Math.round(Number(sec)));
        if (s < 90) return s + ' 秒';
        var m = Math.round(s / 60);
        if (m < 90) return m + ' 分钟';
        var h = Math.floor(m / 60);
        if (h < 48) return h + ' 小时' + (m % 60 < 10 ? '0' : '') + (m % 60) + ' 分';
        return Math.round(h / 24) + ' 天';
    }

    function showMsg(text, kind) {
        el.msg.textContent = text;
        el.msg.className = 'kl-msg ' + (kind || 'note');
        if (kind !== 'err') setTimeout(function () { el.msg.className = 'kl-msg'; }, 4200);
    }

    function get(url, opts) {
        return fetch(url, opts).then(function (r) {
            return r.json().catch(function () { throw new Error('HTTP ' + r.status + ' 返回非 JSON'); })
                .then(function (j) {
                    if (!j || j.success !== true) throw new Error((j && j.error) || ('HTTP ' + r.status));
                    return j;
                });
        });
    }

    function postJson(url, body) {
        return get(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
    }

    // ---------------------------------------------------------------- 币种与类型切换
    function renderTabs() {
        el.tabs.innerHTML = '';
        state.types.forEach(function (t) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'kl-tab' + (t.value === state.activeType ? ' on' : '');
            b.textContent = t.label;
            b.onclick = function () {
                state.activeType = t.value;
                renderTabs();
                renderCards();
                loadBars(true);
            };
            el.tabs.appendChild(b);
        });
    }

    function renderCoinSelect() {
        var st = state.status || {};
        var coins = (st.coins && st.coins.length ? st.coins : state.coins) || [];
        var known = coins.map(function (c) { return c.coin || c; });
        if (known.indexOf(state.activeCoin) < 0 && state.activeCoin !== '*') {
            state.activeCoin = known.length ? known[0] : 'BTC';
        }
        var opts = ['<option value="*">全部币种</option>'].concat(known.map(function (c) {
            return '<option value="' + c + '">' + c + '</option>';
        }));
        el.coinSel.innerHTML = opts.join('');
        el.coinSel.value = state.activeCoin;
    }

    // ---------------------------------------------------------------- 进度看板
    function metric(k, v, cls, small) {
        return '<div class="kl-m"><div class="k">' + k + '</div><div class="v' +
            (cls ? ' ' + cls : '') + (small ? ' sm' : '') + '">' + v + '</div></div>';
    }

    function renderCards() {
        var st = state.status;
        if (!st) return;
        el.cards.innerHTML = '';
        var list = (st.streams || []).filter(function (s) {
            return state.activeCoin === '*' || s.coin === state.activeCoin;
        });
        if (!list.length) {
            el.cards.innerHTML = '<div class="kl-empty">该币种当前没有可用的数据流（类型未启用或不在清单里）</div>';
            return;
        }
        list.forEach(function (s) {
            if (s.available === false) {
                el.cards.appendChild(nosrcCard(s));
                return;
            }
            var bar = s.latest_bar || {};
            var chg = (bar.close != null && bar.open) ? (bar.close - bar.open) : null;
            var card = document.createElement('div');
            card.className = 'kl-card';

            var priceCls = chg === null ? '' : (chg >= 0 ? ' up' : ' down');
            var head = '<div class="kl-card-top"><h2>' + escapeHtml(s.label || s.key) + '</h2>' +
                '<span class="kl-symbol">' + escapeHtml(s.symbol || '') + '</span></div>' +
                '<div class="kl-desc">' + escapeHtml(s.sub || '') + '</div>';

            var priceRow = '<div class="kl-price-row">' +
                '<div class="kl-price' + priceCls + '">' + fmtNum(bar.close, 1) + '</div>' +
                '<div class="kl-price-tag">最新已收盘 1m 收盘价</div>' +
                (s.live_price != null
                    ? '<div class="kl-live">当前分钟实时价 <b>' + fmtNum(s.live_price, 1) +
                      '</b> <span>' + shortTime(s.live_price_at) + '</span></div>'
                    : '<div class="kl-live">实时价 <b>--</b>（ticker 未取到）</div>') +
                '</div>';

            var pct = s.backfill_pct == null ? null : Number(s.backfill_pct);
            var running = st.backfill_running || s.backfill_paused;
            var prog = '<div class="kl-prog">' +
                '<div class="kl-prog-txt"><span>全量回填进度 ' +
                (pct == null ? '目标未知（尚未探测数据地板）'
                    : pct.toFixed(2) + '%') +
                (s.backfill_done ? '（地板→现在已全部到位，转为持续增量）'
                    : pct == null ? ''
                        : '，尚差 ' + fmtInt(s.backfill_remaining_rows) + ' 根') +
                (s.backfill_paused ? '（已手动暂停）' : running ? '（回填运行中…）' : '') +
                '</span><span>到位 ' + fmtInt(s.backfill_covered_rows) + ' / 应有 ' +
                fmtInt(s.backfill_need_rows) + ' 根（台账 ' + fmtInt(s.rows_total) +
                ' 根，交易所侧缺 ' + fmtInt(s.backfill_gap_rows) + ' 根）</span></div>' +
                '<div class="kl-prog-track"><div class="kl-prog-fill' + (pct != null && pct >= 100 ? ' full' : '') +
                '" style="width:' + Math.min(pct == null ? 0 : pct, 100) + '%"></div></div></div>';

            var lagTxt = s.lag_seconds == null ? '--'
                : (s.lag_seconds < 120 ? s.lag_seconds + ' 秒' : Math.round(s.lag_seconds / 60) + ' 分钟');
            var metrics = '<div class="kl-metrics">' +
                metric('末根时间（UTC+8）', s.latest_bar ? shortTime(s.latest_bar.time_local) : '--', null, true) +
                metric('末根 UTC', s.latest_bar ? shortTime(s.latest_bar.time_utc) : '--', null, true) +
                metric('数据滞后', lagTxt, s.lag_seconds != null && s.lag_seconds > 180 ? 'bad' : 'good') +
                metric('末根涨跌', chg === null ? '--' : (chg >= 0 ? '+' : '') + fmtNum(chg, 1),
                    chg === null ? '' : (chg >= 0 ? 'bad' : 'good')) +
                metric('末根振幅', (bar.high != null && bar.low != null && bar.high) ?
                    fmtNum(bar.high - bar.low, 1) : '--') +
                metric('末根成交量', fmtNum(bar.volume, 2), null, true) +
                metric('覆盖区间', s.coverage_days ? s.coverage_days + ' 天' : '--', null, true) +
                metric('数据地板', s.earliest_time_local ? dayTime(s.earliest_time_local) +
                    (s.floor_source === 'probe' ? '（探测）' : '（N天窗口）') : '未探测', null, true) +
                metric('回填游标', s.backfill_done ? '已追平' : (s.head_cursor_time_local
                    ? dayTime(s.head_cursor_time_local) : '--'), null, true) +
                metric('实测速率', s.backfill_rate ? fmtNum(s.backfill_rate, 0) + ' 根/秒' : '--', null, true) +
                metric('预计剩余', s.backfill_done ? '已全量到位'
                    : (s.backfill_eta_sec ? fmtDur(s.backfill_eta_sec) : '--'), null, true) +
                metric('本轮新增', s.last_appended == null ? '--' : fmtInt(s.last_appended) + ' 根', null, true) +
                metric('其中历史回填', s.last_backfilled == null ? '--' : fmtInt(s.last_backfilled) + ' 根', null, true) +
                metric('本轮耗时', s.last_elapsed_sec == null ? '--' : s.last_elapsed_sec + 's', null, true) +
                metric('最近运行', s.last_run_at ? shortTime(s.last_run_at) : '未运行', null, true) +
                metric('成功/失败轮', (s.rounds_ok || 0) + ' / ' + (s.rounds_fail || 0),
                    s.rounds_fail ? 'bad' : 'good', true) +
                metric('跳过轮', s.skipped || 0, null, true) +
                '</div>';

            var err = s.last_error
                ? '<div class="kl-file" style="color:#c62828;border-top-color:#f5c6c0">最近错误：' +
                  escapeHtml(s.last_error) + '</div>'
                : '';
            var file = '<div class="kl-file">CSV：' + escapeHtml(s.csv_path || '--') +
                ' · ' + fmtBytes(s.csv_bytes) + '</div>';

            card.innerHTML = head + priceRow + prog + metrics + err + file;
            el.cards.appendChild(card);
        });
    }

    // ccxt 无数据源：只给一张灰卡写清原因，不编造进度（这一档本来就没得拉）
    function nosrcCard(s) {
        var card = document.createElement('div');
        card.className = 'kl-card nosrc';
        card.innerHTML =
            '<div class="kl-card-top"><h2>' + escapeHtml(s.label || s.key) + '</h2>' +
            '<span class="kl-symbol">' + escapeHtml(s.market_id || s.symbol || '') + '</span></div>' +
            '<div class="kl-nosrc-note">⛔ ' + escapeHtml(s.market_note || 'ccxt 无此数据源') +
            '，已自动跳过（不建文件、不报错）</div>' +
            '<div class="kl-metrics">' +
            metric('市场类型', escapeHtml(s.stream_type || '--'), null, true) +
            metric('预期 CSV', escapeHtml((s.csv_path || '').split(/[\\/]/).pop() || '--'), null, true) +
            metric('校验时间', s.market_checked_at ? shortTime(s.market_checked_at) : '--', null, true) +
            '</div>';
        return card;
    }

    // ---------------------------------------------------------------- 币种总览表
    function pctCell(pct) {
        if (pct === null || pct === undefined || isNaN(Number(pct))) {
            return '<span class="kl-pct"><span>目标未知</span></span>';
        }
        var v = Math.min(Number(pct), 100);
        return '<span class="kl-pct"><span class="kl-prog-track"><span class="kl-prog-fill' +
            (v >= 100 ? ' full' : '') + '" style="width:' + v + '%"></span></span>' +
            '<span>' + v.toFixed(2) + '%</span></span>';
    }

    function typeChip(s) {
        if (!s) return '<span class="kl-chip off">未启用</span>';
        if (s.available === false) return '<span class="kl-chip na" title="' +
            escapeHtml(s.market_note || 'ccxt 无此数据源') + '">无数据源</span>';
        if (!s.rows_total) return '<span class="kl-chip">待拉取</span>';
        if (s.backfill_done) return '<span class="kl-chip on">已追平</span>';
        return '<span class="kl-chip on">' + fmtInt(s.rows_total) + ' 根</span>';
    }

    function renderCoinTable() {
        var st = state.status;
        if (!st || !el.coinBody) return;
        var streams = st.streams || [];
        var byKey = {};
        streams.forEach(function (s) { byKey[s.key] = s; });
        var rows = (st.coins || []).map(function (c) {
            var swap = null, spot = null;
            (c.streams || []).forEach(function (k) {
                var s = byKey[k];
                if (!s) return;
                if (s.stream_type === 'swap') swap = s;
                if (s.stream_type === 'spot') spot = s;
            });
            var cls = 'kl-coin-row' + (c.available === false ? ' nosrc' : '');
            return '<tr class="' + cls + '" data-coin="' + escapeHtml(c.coin) + '" ' +
                'style="cursor:pointer">' +
                '<td><b>' + escapeHtml(c.coin) + '</b>' +
                (c.available === false ? ' <span class="kl-chip na">无数据源</span>' : '') + '</td>' +
                '<td>' + typeChip(swap) + '</td>' +
                '<td>' + typeChip(spot) + '</td>' +
                '<td>' + fmtInt(c.rows_total) + '</td>' +
                '<td>' + pctCell(c.backfill_pct) + '</td>' +
                '<td>' + fmtInt(c.backfill_remaining_rows) + '</td>' +
                '<td>' + escapeHtml(c.latest_time_local || '--') + '</td>' +
                '<td>' + escapeHtml(c.last_run_at ? shortTime(c.last_run_at) : '未运行') + '</td>' +
                '<td style="text-align:left">' + escapeHtml(c.market_note || '') + '</td>' +
                '</tr>';
        }).join('');
        el.coinBody.innerHTML = rows ||
            '<tr><td colspan="9" class="kl-empty">暂无币种（监控台清单读不到）</td></tr>';
        Array.prototype.forEach.call(el.coinBody.querySelectorAll('tr[data-coin]'), function (tr) {
            tr.onclick = function () {
                state.activeCoin = tr.getAttribute('data-coin');
                el.coinSel.value = state.activeCoin;
                renderCards();
                loadBars(true);
            };
        });
    }

    // ---------------------------------------------------------------- 状态头部
    function renderHeader() {
        var st = state.status;
        if (!st) return;

        var b = el.stateBadge;
        b.className = 'kl-badge';
        if (!st.enabled) {
            b.textContent = '任务已停用';
            b.classList.add('warn');
        } else if (st.running) {
            b.textContent = '抓取中…';
            b.classList.add('run');
        } else if ((st.streams || []).some(function (s) { return s.last_error; })) {
            b.textContent = '最近一轮有失败';
            b.classList.add('err');
        } else if (st.backfill_running) {
            b.textContent = '全量回填运行中…（实时尾段会暂时滞后）';
            b.classList.add('run');
        } else {
            // rounds 是从 data/ 快照恢复的累计轮次（跨进程重启），别写成「本次」
            b.textContent = '定时拉取运行中 · 累计 ' + fmtInt(st.rounds) + ' 轮'
                + (st.last_round_at ? '（最近 ' + shortTime(st.last_round_at) + '）' : '');
            b.classList.add('ok');
        }

        el.coinCount.textContent = (st.coins_enabled && st.coins_enabled.length)
            ? st.coins_enabled.length + ' / ' + fmtInt(st.coins_total) + ' 个（已收窄）'
            : fmtInt(st.coins_total) + ' 个（跟随监控台）';
        el.coinMeta.textContent = '共 ' + fmtInt(st.coins_total) + ' 个币种 · '
            + fmtInt(st.coins_available) + ' 个有数据源 · '
            + fmtInt(st.streams_total) + ' 条流 · 台账合计 ' +
            fmtInt((st.coins || []).reduce(function (a, c) { return a + (c.rows_total || 0); }, 0)) + ' 根';
        el.nosrcMeta.textContent = fmtInt(st.coins_missing || 0) + ' 个币种无数据源'
            + (st.coins_unchecked ? ' · ' + fmtInt(st.coins_unchecked) + ' 个未校验' : '');
        el.nosrcMeta.style.display = (st.coins_missing || st.coins_unchecked) ? '' : 'none';

        el.serverTime.textContent = '服务器时间 ' + shortTime(st.server_time);
        el.offsetMs = (parseWallClock(st.server_time) || new Date()).getTime() - Date.now();
        el.intervalTxt.textContent = '每 ' + st.interval_seconds + ' 秒';

        el.toggleBtn.textContent = st.enabled ? '停用任务' : '启用任务';
        el.toggleBtn.className = 'kl-btn ' + (st.enabled ? 'danger' : '');

        if (!state.editingConfig) {
            el.maxInput.value = st.max_candles_per_stream;
            el.sinceInput.value = st.since_days;
            el.intervalMsInput.value = st.request_interval_ms;
            el.budgetInput.value = st.round_budget_sec;
            if (el.modeSel.value !== st.backfill_mode) el.modeSel.value = st.backfill_mode;
        }
        el.bfStartBtn.disabled = !!st.backfill_running;
        el.bfStopBtn.disabled = !st.backfill_running &&
            !(st.streams || []).some(function (s) { return s.backfill_paused; });
        tickCountdown();
    }

    function tickCountdown() {
        var st = state.status;
        if (!st) return;
        var next = parseWallClock(st.next_run_at);
        if (!next) {
            el.nextRun.textContent = st.enabled ? '未入队（调度器未启动或任务未注册）' : '已停用';
            return;
        }
        var remain = Math.round((next.getTime() - (Date.now() + el.offsetMs)) / 1000);
        el.nextRun.textContent = remain > 0
            ? shortTime(st.next_run_at) + '（' + remain + 's 后）'
            : shortTime(st.next_run_at) + '（即将触发）';
    }

    // ---------------------------------------------------------------- 运行流水
    function renderFeed() {
        var st = state.status;
        var evts = (st && st.events) || [];
        if (!evts.length) {
            el.feed.innerHTML = '<div class="kl-empty">暂无运行记录</div>';
            return;
        }
        el.feed.innerHTML = evts.slice(0, 40).map(function (e) {
            return '<div class="kl-feed-item ' + (e.level || 'info') + '">' +
                '<span class="t">' + shortTime(e.ts) + '</span>' +
                '<span class="m">' + escapeHtml(e.msg || '') + '</span></div>';
        }).join('');
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
    }

    // ---------------------------------------------------------------- 明细表
    function loadBars(reset) {
        var limit = el.limitSel.value || '60';
        return get(API + '/bars?stream=' + encodeURIComponent(barsStreamKey()) + '&limit=' + limit)
            .then(function (j) {
                var bars = j.bars || [];
                el.barsMeta.textContent = (j.label || '') + ' · ' + j.symbol + ' · 末 ' + bars.length + ' 根';
                if (!bars.length) {
                    el.tbody.innerHTML = '';
                    el.tableEmpty.style.display = 'block';
                    state.lastBarsTs = 0;
                    return;
                }
                var freshCut = reset ? 0 : state.lastBarsTs;
                var rowsHtml = bars.map(function (b) {
                    var chg = (b.close != null && b.open) ? (b.close - b.open) : null;
                    var pct = (chg !== null && b.open) ? (chg / b.open * 100) : null;
                    var isNew = b.timestamp > freshCut;
                    return '<tr' + (isNew ? ' class="fresh"' : '') + '>' +
                        '<td>' + escapeHtml(b.time_local || '--') + (isNew ? '<span class="kl-new">新增</span>' : '') + '</td>' +
                        '<td>' + escapeHtml((b.datetime || '').replace('T', ' ').replace('+00:00', '')) + '</td>' +
                        '<td>' + fmtNum(b.open, 1) + '</td>' +
                        '<td>' + fmtNum(b.high, 1) + '</td>' +
                        '<td>' + fmtNum(b.low, 1) + '</td>' +
                        '<td>' + fmtNum(b.close, 1) + '</td>' +
                        '<td class="kl-chg ' + (chg === null ? '' : (chg >= 0 ? 'up' : 'down')) + '">' +
                        (chg === null ? '--' : (chg >= 0 ? '+' : '') + fmtNum(chg, 1) +
                            '（' + (chg >= 0 ? '+' : '') + fmtNum(pct, 3) + '%）') + '</td>' +
                        '<td>' + fmtNum(b.volume, 3) + '</td>' +
                        '</tr>';
                }).join('');
                el.tbody.innerHTML = rowsHtml;
                el.tableEmpty.style.display = 'none';
                state.lastBarsTs = bars[0].timestamp;
            })
            .catch(function (e) { showMsg('明细读取失败：' + e.message, 'err'); });
    }

    // ---------------------------------------------------------------- 轮询
    function loadStatus() {
        return get(API + '/status')
            .then(function (j) {
                state.status = j;
                if (j.coins_enabled) state.enabledCoins = j.coins_enabled;
                renderHeader();
                renderCoinSelect();
                renderCoinTable();
                renderCards();
                renderFeed();
            })
            .catch(function (e) { showMsg('状态读取失败：' + e.message, 'err'); });
    }

    function loop() {
        setInterval(function () {
            if (!el.auto.checked) return;
            loadStatus();
        }, STATUS_MS);
        setInterval(function () {
            if (!el.auto.checked) return;
            loadBars(false);
        }, BARS_MS);
        setInterval(tickCountdown, 1000);
    }

    // ---------------------------------------------------------------- 交互
    el.pullBtn.onclick = function () {
        el.pullBtn.disabled = true;
        postJson(API + '/pull', {})
            .then(function (j) {
                showMsg(j.message + '（后台执行，进度稍后刷新）', 'note');
                setTimeout(loadStatus, 2500);
                setTimeout(function () { loadBars(false); }, 6000);
            })
            .catch(function (e) { showMsg('手动拉取启动失败：' + e.message, 'err'); })
            .then(function () { el.pullBtn.disabled = false; });
    };

    el.saveBtn.onclick = function () {
        var patch = {
            max_candles_per_stream: Math.max(0, parseInt(el.maxInput.value, 10) || 0),
            since_days: Math.min(36500, Math.max(1, parseInt(el.sinceInput.value, 10) || 30)),
            backfill_mode: el.modeSel.value || 'earliest',
            request_interval_ms: Math.min(5000, Math.max(0, parseInt(el.intervalMsInput.value, 10) || 0)),
            round_budget_sec: Math.min(3000, Math.max(5, parseInt(el.budgetInput.value, 10) || 40))
        };
        el.saveBtn.disabled = true;
        postJson(API + '/config', patch)
            .then(function (j) {
                showMsg('已保存：口径 ' +
                    (j.config.backfill_mode === 'earliest' ? '全量（地板→现在）'
                        : '最近 ' + j.config.since_days + ' 天') +
                    ' · 批间隔 ' + j.config.request_interval_ms + 'ms · 单轮预算 ' +
                    j.config.round_budget_sec + 's。' + j.message, 'note');
                return loadStatus();
            })
            .catch(function (e) { showMsg('配置保存失败：' + e.message, 'err'); })
            .then(function () { el.saveBtn.disabled = false; });
    };

    el.bfStartBtn.onclick = function () {
        el.bfStartBtn.disabled = true;
        // 只回填当前选中的币种（两个启用类型都排）；选了「全部币种」就传 all
        var scope = state.activeCoin === '*' ? 'all' : state.activeCoin;
        postJson(API + '/backfill/start', { coins: scope })
            .then(function (j) {
                showMsg(j.message + '（同一份 CSV 只允许一个写入者，回填期间定时轮会跳过，停批后自动追平）', 'note');
                return loadStatus();
            })
            .catch(function (e) {
                el.bfStartBtn.disabled = false;
                showMsg('全量回填启动失败：' + e.message, 'err');
            });
    };

    el.bfStopBtn.onclick = function () {
        el.bfStopBtn.disabled = true;
        postJson(API + '/backfill/stop',
            state.activeCoin === '*' ? {} : { coins: state.activeCoin })
            .then(function (j) {
                showMsg(j.message, 'note');
                return loadStatus();
            })
            .catch(function (e) { showMsg('停止信号发送失败：' + e.message, 'err'); });
    };

    el.auditBtn.onclick = function () {
        el.auditBtn.disabled = true;
        el.auditOut.textContent = '台账体检中（整扫文件，可能十几秒）…';
        get(API + '/audit?stream=' + encodeURIComponent(barsStreamKey()))
            .then(function (j) {
                var d = j.data || {};
                el.auditOut.textContent = '精确台账 ' + fmtInt(d.rows_exact) + ' 根 · 首根 ' +
                    (d.first_ts ? new Date(d.first_ts).toISOString().slice(0, 16).replace('T', ' ') : '--') +
                    ' · 末根 ' + (d.last_ts ? new Date(d.last_ts).toISOString().slice(0, 16).replace('T', ' ') : '--') +
                    ' · 台账内缺 ' + fmtInt((d.main || {}).missing_rows) + ' 根 · 重复 ' +
                    fmtInt(d.dup_rows) + ' 根（UTC）';
            })
            .catch(function (e) { el.auditOut.textContent = '体检失败：' + e.message; })
            .then(function () { el.auditBtn.disabled = false; });
    };

    el.toggleBtn.onclick = function () {
        var st = state.status || {};
        var to = !st.enabled;
        el.toggleBtn.disabled = true;
        postJson(API + '/config', { enabled: to })
            .then(function () {
                showMsg(to ? '任务已启用（调度器已重新注册）' : '任务已停用（调度器已移除任务，CSV 保留）', 'note');
                return loadStatus();
            })
            .catch(function (e) { showMsg('开关切换失败：' + e.message, 'err'); })
            .then(function () { el.toggleBtn.disabled = false; });
    };

    [el.maxInput, el.sinceInput, el.intervalMsInput, el.budgetInput, el.modeSel].forEach(function (i) {
        if (!i) return;
        i.addEventListener('focus', function () { state.editingConfig = true; });
        i.addEventListener('blur', function () { state.editingConfig = false; });
        i.addEventListener('change', function () { state.editingConfig = true; });
    });
    el.limitSel.onchange = function () { loadBars(true); };
    el.coinSel.onchange = function () {
        state.activeCoin = el.coinSel.value;
        renderCards();
        loadBars(true);
    };

    // ---------------------------------------------------------------- 币种范围面板
    function renderCoinPick() {
        var st = state.status || {};
        // 候选用监控台全集（而不是已收窄的注册表），否则收窄到只剩 BTC 后就再也选不回其他币
        var pool = (st.watch_coins && st.watch_coins.length)
            ? st.watch_coins.map(function (c) { return { coin: c, available: null }; })
            : (st.coins || []);
        var en = state.enabledCoins || [];
        var followAll = !en.length;
        var html = '<div class="kl-coin-pick-head">' +
            '<label class="kl-ck"><input type="checkbox" id="kl-ck-all"' +
            (followAll ? ' checked' : '') + '> 跟随默认：固定币种按市值前 12</label>' +
            '<span>已选 <b id="kl-pick-count">' + (followAll ? (st.coins_total || pool.length) : en.length) + '</b> 个；' +
            '取消左边勾选可手动指定。无数据源的币即使勾上也会被 ccxt 校验跳过</span>' +
            '<button type="button" class="kl-btn" id="kl-pick-save">保存币种范围</button>' +
            '<button type="button" class="kl-btn ghost" id="kl-pick-close">收起</button>' +
            '</div>';
        html += pool.map(function (c) {
            var on = followAll ? false : en.indexOf(c.coin) >= 0;
            var na = c.available === false;
            return '<label class="kl-ck' + (na ? ' na' : '') + '" title="' +
                escapeHtml(na ? (c.market_note || 'ccxt 无此数据源') : c.coin) + '">' +
                '<input type="checkbox" value="' + escapeHtml(c.coin) + '"' +
                (on ? ' checked' : '') + (followAll ? ' disabled' : '') + '> ' +
                escapeHtml(c.coin) + (na ? ' ⚠' : '') + '</label>';
        }).join('');
        el.coinPick.innerHTML = html;
        var allChk = document.getElementById('kl-ck-all');
        var count = document.getElementById('kl-pick-count');
        function boxes() {
            return Array.prototype.slice.call(el.coinPick.querySelectorAll('input[value]'));
        }
        function refreshCount() {
            var chk = boxes().filter(function (i) { return i.checked; });
            count.textContent = allChk.checked ? (st.coins_total || pool.length) : chk.length;
        }
        allChk.onchange = function () {
            boxes().forEach(function (i) {
                i.disabled = allChk.checked;
                if (allChk.checked) i.checked = false;
            });
            refreshCount();
        };
        boxes().forEach(function (i) { i.onchange = refreshCount; });
        document.getElementById('kl-pick-close').onclick = function () {
            el.coinPick.classList.remove('open');
        };
        document.getElementById('kl-pick-save').onclick = function () {
            var btn = this;
            var list = allChk.checked ? [] : boxes().filter(function (i) { return i.checked; })
                .map(function (i) { return i.value; });
            if (!allChk.checked && !list.length) {
                showMsg('一个币都没勾：要么改回「跟随监控台全部币种」，要么至少选一个', 'err');
                return;
            }
            btn.disabled = true;
            postJson(API + '/config', { coins: list })
                .then(function (j) {
                    state.enabledCoins = (j.config && j.config.coins) || [];
                    showMsg('币种范围已保存：' +
                        (state.enabledCoins.length ? state.enabledCoins.join('、')
                            : '跟随默认（固定币种按市值前 12，下一轮生效）'), 'note');
                    el.coinPick.classList.remove('open');
                    return loadStatus();
                })
                .catch(function (e) { showMsg('币种范围保存失败：' + e.message, 'err'); })
                .then(function () { btn.disabled = false; });
        };
    }

    el.coinEditBtn.onclick = function () {
        if (el.coinPick.classList.contains('open')) {
            el.coinPick.classList.remove('open');
            return;
        }
        renderCoinPick();
        el.coinPick.classList.add('open');
    };

    // ---------------------------------------------------------------- 启动
    get(API + '/meta')
        .then(function (m) {
            state.streams = m.streams || [];
            var selected = (m.config && m.config.streams) || [];
            state.types = (m.stream_types || []).filter(function (t) {
                return !selected.length || selected.indexOf(t.value) >= 0;
            });
            if (!state.types.length) {
                state.types = [{ value: 'swap', label: '永续合约' }, { value: 'spot', label: '现货' }];
            }
            state.coins = (m.coins || []).map(function (c) { return c.coin || c; });
            state.enabledCoins = (m.config && m.config.coins) || [];
            if (state.types.map(function (t) { return t.value; }).indexOf(state.activeType) < 0) {
                state.activeType = state.types[0].value;
            }
            renderTabs();
            renderCoinSelect();
        })
        .catch(function (e) { showMsg('元信息读取失败：' + e.message, 'err'); })
        .then(function () { return loadStatus(); })
        .then(function () { return loadBars(true); })
        .then(loop);
})();
