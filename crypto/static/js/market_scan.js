/**
 * 全市场趋势扫描页脚本
 * =====================
 * - 拉取 /api/market-scan/data 缓存结果，无数据时自动触发后台扫描并轮询进度
 * - 四个榜单（明确趋势 / 涨幅 / 跌幅 / 成交额）以币种卡片展示
 * - 每张卡片支持：勾选（多选）、星标（复用监控台 starred_coins）、点击选中
 * - 支持定时自动重新扫描
 */
document.addEventListener('DOMContentLoaded', function () {
    var gridEl = document.getElementById('ms-grid');
    var loadingEl = document.getElementById('ms-loading');
    var emptyEl = document.getElementById('ms-empty');
    var scanBtn = document.getElementById('ms-scan-btn');
    var fetchBtn = document.getElementById('ms-fetch-btn');
    var infoBtn = document.getElementById('ms-info-btn');
    var metaEl = document.getElementById('scan-meta');
    var progressEl = document.getElementById('ms-progress');
    var progressBar = document.getElementById('ms-progress-bar');
    var progressMsg = document.getElementById('ms-progress-msg');
    var batchStarBtn = document.getElementById('ms-batch-star-btn');
    var addMonitorBtn = document.getElementById('ms-add-monitor-btn');
    var clearCheckBtn = document.getElementById('ms-clear-check-btn');
    var applyIntervalBtn = document.getElementById('ms-apply-interval-btn');
    var intervalInput = document.getElementById('ms-refresh-interval');
    var refreshStatus = document.getElementById('ms-refresh-status');

    var STARRED_COINS = [];         // instId 列表（与监控台共享）
    var CHECKED = {};               // instId -> true，本页勾选状态
    var SCAN_DATA = null;           // 最近一次扫描结果
    var CURRENT_VIEW = 'trending';  // 当前榜单
    var pollTimer = null;
    var autoTimer = null;

    var VIEW_LABELS = {
        trending: '明确趋势币种',
        gainers: '24H 涨幅榜',
        losers: '24H 跌幅榜',
        by_volume: '24H 成交额榜'
    };

    // ---------------------------------------------------------------
    // 工具
    // ---------------------------------------------------------------
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function fmtPrice(v) {
        var n = Number(v);
        if (!isFinite(n) || n <= 0) return '--';
        if (n >= 1) return n.toLocaleString('en-US', { maximumFractionDigits: 4 });
        return n.toPrecision(4);
    }

    function fmtPct(v) {
        if (v === null || v === undefined || v === '') return '--';
        var n = Number(v);
        if (!isFinite(n)) return '--';
        return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
    }

    function pctClass(v) {
        var n = Number(v);
        if (!isFinite(n) || n === 0) return 'val-yellow';
        return n > 0 ? 'val-green' : 'val-red';
    }

    function trendBadge(label, dir) {
        if (!label) return '<span class="ms-badge ms-badge-none">未分析</span>';
        var cls = 'ms-badge-range';
        var icon = '⚪';
        if (label === '强趋势') { cls = 'ms-badge-strong'; icon = '🚀'; }
        else if (label === '温和趋势') { cls = 'ms-badge-mild'; icon = '📈'; }
        var d = dir ? ('·' + dir) : '';
        return '<span class="ms-badge ' + cls + '">' + icon + label + d + '</span>';
    }

    // ---------------------------------------------------------------
    // 星标
    // ---------------------------------------------------------------
    function loadStarred() {
        return fetch('/api/strategy/starred')
            .then(function (r) { return r.json(); })
            .then(function (res) {
                if (res.code === 200) STARRED_COINS = res.data || [];
            })
            .catch(function () { STARRED_COINS = []; });
    }

    function saveStarred() {
        return fetch('/api/strategy/starred', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ starred_coins: STARRED_COINS })
        }).catch(function () {});
    }

    function toggleStar(instId) {
        var idx = STARRED_COINS.indexOf(instId);
        if (idx >= 0) STARRED_COINS.splice(idx, 1);
        else STARRED_COINS.push(instId);
        updateCardState(instId);
        saveStarred();
    }

    // ---------------------------------------------------------------
    // 勾选
    // ---------------------------------------------------------------
    function toggleCheck(instId) {
        if (CHECKED[instId]) delete CHECKED[instId];
        else CHECKED[instId] = true;
        updateCardState(instId);
    }

    function checkedList() {
        return Object.keys(CHECKED);
    }

    // 同步某个 instId 对应的所有卡片的星标/勾选视觉状态（可能多个榜单出现同一币种）
    function updateCardState(instId) {
        var cards = gridEl.querySelectorAll('.ms-card[data-inst="' + CSS.escape(instId) + '"]');
        cards.forEach(function (card) {
            var starred = STARRED_COINS.indexOf(instId) >= 0;
            var checked = !!CHECKED[instId];
            var star = card.querySelector('.ms-star');
            var cb = card.querySelector('.ms-check');
            if (star) {
                star.textContent = starred ? '★' : '☆';
                star.classList.toggle('starred', starred);
            }
            if (cb) cb.checked = checked;
            card.classList.toggle('star-highlight', starred);
            card.classList.toggle('ms-checked', checked);
        });
    }

    // ---------------------------------------------------------------
    // 渲染
    // ---------------------------------------------------------------
    function buildCard(c) {
        var starred = STARRED_COINS.indexOf(c.instId) >= 0;
        var checked = !!CHECKED[c.instId];
        var card = document.createElement('div');
        card.className = 'card ms-card' + (starred ? ' star-highlight' : '') + (checked ? ' ms-checked' : '');
        card.dataset.inst = c.instId;

        var er = (c.er === null || c.er === undefined) ? '--' : Number(c.er).toFixed(2);

        card.innerHTML =
            '<div class="ms-card-head">' +
                '<label class="ms-check-wrap" title="勾选（可批量星标）">' +
                    '<input type="checkbox" class="ms-check"' + (checked ? ' checked' : '') + '>' +
                '</label>' +
                '<span class="ms-symbol">' + esc(c.symbol) + '</span>' +
                '<span class="ms-star' + (starred ? ' starred' : '') + '" title="加入/取消星标">' + (starred ? '★' : '☆') + '</span>' +
            '</div>' +
            '<div class="ms-price">' + fmtPrice(c.last) + '<span class="ms-price-unit">USDT</span></div>' +
            '<div class="ms-trend-row">' + trendBadge(c.trend_label, c.trend_dir) + '</div>' +
            '<div class="ms-metrics">' +
                metric('24H涨跌', fmtPct(c.change_24h), pctClass(c.change_24h)) +
                metric('7日涨跌', fmtPct(c.chg_7d), pctClass(c.chg_7d)) +
                metric('成交额', esc(c.turnover_text), 'val-blue') +
                metric('效率ER', er, 'val-yellow') +
            '</div>';

        // 事件绑定
        var cb = card.querySelector('.ms-check');
        cb.addEventListener('click', function (e) {
            e.stopPropagation();
            toggleCheck(c.instId);
        });
        var star = card.querySelector('.ms-star');
        star.addEventListener('click', function (e) {
            e.stopPropagation();
            toggleStar(c.instId);
        });
        // 点击卡片主体 = 勾选切换（类似其他币种卡片的选中方式）
        card.addEventListener('click', function () {
            toggleCheck(c.instId);
        });

        return card;
    }

    function metric(label, value, cls) {
        return '<div class="ms-metric">' +
            '<span class="ms-metric-label">' + label + '</span>' +
            '<span class="ms-metric-value ' + (cls || '') + '">' + value + '</span>' +
            '</div>';
    }

    function currentRows() {
        if (!SCAN_DATA) return [];
        if (CURRENT_VIEW === 'trending') return SCAN_DATA.trending || [];
        return (SCAN_DATA.rankings && SCAN_DATA.rankings[CURRENT_VIEW]) || [];
    }

    function render() {
        // badge 数量
        setBadge('trending', SCAN_DATA ? (SCAN_DATA.trending || []).length : 0);
        ['gainers', 'losers', 'by_volume'].forEach(function (k) {
            var n = (SCAN_DATA && SCAN_DATA.rankings && SCAN_DATA.rankings[k]) ? SCAN_DATA.rankings[k].length : 0;
            setBadge(k, n);
        });

        var rows = currentRows();
        gridEl.innerHTML = '';

        if (!SCAN_DATA) {
            gridEl.style.display = 'none';
            emptyEl.style.display = 'block';
            return;
        }
        emptyEl.style.display = 'none';

        if (!rows.length) {
            gridEl.style.display = 'none';
            emptyEl.style.display = 'block';
            emptyEl.textContent = CURRENT_VIEW === 'trending'
                ? ((SCAN_DATA && SCAN_DATA.scan_mode === 'rankings')
                    ? '当前为「直接获取」榜单（未做趋势分析），如需明确趋势币种请点击「🔍 分析」。'
                    : '当前候选中没有满足强/温和趋势标准的币种（市场整体偏震荡）。')
                : '该榜单暂无数据。';
            return;
        }

        gridEl.style.display = 'grid';
        var frag = document.createDocumentFragment();
        rows.forEach(function (c) { frag.appendChild(buildCard(c)); });
        gridEl.appendChild(frag);
    }

    function setBadge(view, n) {
        var el = document.getElementById('badge-' + view);
        if (el) el.textContent = n;
    }

    function renderMeta() {
        if (!SCAN_DATA) { metaEl.textContent = ''; return; }
        var t = SCAN_DATA.thresholds || {};
        metaEl.innerHTML =
            '<span class="ms-meta-env">' + esc(SCAN_DATA.env || '') + '</span> · ' +
            '合约 ' + (SCAN_DATA.ticker_count || 0) + ' · ' +
            '震荡剔除 ' + (SCAN_DATA.ranging_count || 0) + ' · ' +
            esc(SCAN_DATA.generated_at || '');
    }

    // ---------------------------------------------------------------
    // 数据加载 / 扫描
    // ---------------------------------------------------------------
    function loadData(triggerIfEmpty) {
        return fetch('/api/market-scan/data')
            .then(function (r) { return r.json(); })
            .then(function (res) {
                if (res.code !== 200 || !res.data) return;
                var d = res.data;
                if (d.has_data && d.result) {
                    SCAN_DATA = d.result;
                    loadingEl.style.display = 'none';
                    renderMeta();
                    render();
                }
                // 若正在扫描，接管进度轮询
                if (d.progress && d.progress.status === 'running') {
                    startPolling();
                } else if (!d.has_data && triggerIfEmpty) {
                    startScan();
                } else if (!d.has_data) {
                    loadingEl.style.display = 'none';
                    render();
                }
            })
            .catch(function () {
                loadingEl.style.display = 'none';
            });
    }

    function startScan() {
        scanBtn.disabled = true;
        scanBtn.textContent = '⏳ 分析中...';
        progressEl.style.display = 'block';
        progressBar.style.width = '0%';
        progressBar.textContent = '0%';
        progressMsg.textContent = '正在启动分析...';

        fetch('/api/market-scan/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
            .then(function (r) { return r.json(); })
            .then(function () { startPolling(); })
            .catch(function () {
                scanBtn.disabled = false;
                scanBtn.textContent = '🔍 分析';
                progressMsg.textContent = '启动分析失败';
            });
    }

    function startPolling() {
        if (pollTimer) clearInterval(pollTimer);
        progressEl.style.display = 'block';
        scanBtn.disabled = true;
        scanBtn.textContent = '⏳ 分析中...';
        pollTimer = setInterval(pollProgress, 1200);
        pollProgress();
    }

    function pollProgress() {
        fetch('/api/market-scan/progress')
            .then(function (r) { return r.json(); })
            .then(function (res) {
                if (res.code !== 200 || !res.data) return;
                var p = res.data;
                var pct = p.progress_pct || 0;
                progressBar.style.width = pct + '%';
                progressBar.textContent = pct + '%';
                progressMsg.textContent = p.message || '';

                if (p.status === 'completed') {
                    finishScan();
                } else if (p.status === 'error') {
                    if (pollTimer) clearInterval(pollTimer);
                    pollTimer = null;
                    scanBtn.disabled = false;
                    scanBtn.textContent = '🔍 分析';
                    progressMsg.textContent = p.message || '分析失败';
                }
            })
            .catch(function () {});
    }

    function finishScan() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
        scanBtn.disabled = false;
        scanBtn.textContent = '🔍 分析';
        setTimeout(function () { progressEl.style.display = 'none'; }, 800);
        loadData(false);
    }

    // ---------------------------------------------------------------
    // 直接获取（仅三大榜单，不做趋势扫描）
    // ---------------------------------------------------------------
    function fetchRankings() {
        if (pollTimer) {
            MDialog.alert({ message: '分析正在进行中，请稍后再试', type: 'warning' });
            return;
        }
        fetchBtn.disabled = true;
        scanBtn.disabled = true;
        fetchBtn.textContent = '⏳ 获取中...';
        loadingEl.style.display = 'block';

        fetch('/api/market-scan/rankings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
            .then(function (r) { return r.json(); })
            .then(function (res) {
                if (res.code === 200 && res.data) {
                    SCAN_DATA = res.data;
                    renderMeta();
                    // 仅榜单模式下「明确趋势」无数据，自动切到涨幅榜便于查看
                    if (CURRENT_VIEW === 'trending') switchView('gainers');
                    else render();
                } else {
                    MDialog.alert({ message: '获取失败: ' + (res.message || ''), type: 'danger' });
                }
            })
            .catch(function () {
                MDialog.alert({ message: '获取失败：网络错误', type: 'danger' });
            })
            .finally(function () {
                loadingEl.style.display = 'none';
                fetchBtn.disabled = false;
                scanBtn.disabled = false;
                fetchBtn.textContent = '⚡ 直接获取';
            });
    }

    // ---------------------------------------------------------------
    // 筛选条件说明弹窗
    // ---------------------------------------------------------------
    function showThresholds() {
        fetch('/api/market-scan/thresholds')
            .then(function (r) { return r.json(); })
            .then(function (res) {
                if (res.code !== 200 || !res.data) {
                    MDialog.alert({ message: '获取筛选条件失败', type: 'danger' });
                    return;
                }
                var t = res.data;
                var html =
                    '<div class="ms-info-list" style="text-align:left;line-height:1.9;">' +
                        '<div><b>💧 流动性门槛：</b>24H 估算成交额 ≥ ' + esc(t.min_turnover_text) + ' USDT</div>' +
                        '<div><b>🔢 候选数量限制：</b>最多 ' + t.candidate_limit + ' 个候选币种</div>' +
                        '<div><b>🚀 强趋势阈值：</b>ER ≥ ' + t.er_strong + '</div>' +
                        '<div><b>📈 温和趋势阈值：</b>ER ≥ ' + t.er_mild + '</div>' +
                        '<div><b>📊 7日幅度门槛：</b>≥ ' + t.chg7d_min + '%</div>' +
                        '<div><b>📅 日线回溯天数：</b>' + t.daily_lookback + ' 天</div>' +
                    '</div>' +
                    '<div style="margin-top:10px;padding-top:8px;border-top:1px dashed #ddd;color:#888;font-size:0.85rem;text-align:left;">' +
                        'ER（Kaufman 效率系数）= |净变动| / Σ|逐日变动|，越接近 1 越单边，越接近 0 越震荡。' +
                    '</div>';
                MDialog.alert({ title: '趋势扫描筛选条件', message: html, type: 'info' });
            })
            .catch(function () {
                MDialog.alert({ message: '获取筛选条件失败：网络错误', type: 'danger' });
            });
    }

    // 切换榜单视图（与标签点击一致）
    function switchView(view) {
        var tabs = document.getElementById('ms-tabs');
        tabs.querySelectorAll('.tab-btn').forEach(function (b) {
            b.classList.toggle('active', b.dataset.view === view);
        });
        CURRENT_VIEW = view;
        render();
    }

    // ---------------------------------------------------------------
    // 批量星标 / 清除勾选
    // ---------------------------------------------------------------
    function batchStar() {
        var list = checkedList();
        if (!list.length) {
            MDialog.alert({ message: '请先勾选至少一个币种', type: 'warning' });
            return;
        }
        var added = 0;
        list.forEach(function (instId) {
            if (STARRED_COINS.indexOf(instId) < 0) { STARRED_COINS.push(instId); added++; }
        });
        saveStarred().then(function () {
            list.forEach(updateCardState);
            MDialog.alert({ message: '已将 ' + added + ' 个币种加入星标（另有 ' + (list.length - added) + ' 个已在星标中）', type: 'success' });
        });
    }

    function clearCheck() {
        var list = checkedList();
        CHECKED = {};
        list.forEach(updateCardState);
    }

    // 将勾选币种加入监控台（作为浮动币种）
    function addToMonitor() {
        var list = checkedList();
        if (!list.length) {
            MDialog.alert({ message: '请先勾选至少一个币种', type: 'warning' });
            return;
        }
        addMonitorBtn.disabled = true;
        fetch('/api/strategy/floating-coins/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ coins: list })
        }).then(function (r) { return r.json(); }).then(function (res) {
            if (res.code === 200) {
                MDialog.alert({ message: res.message + '，可在监控台“监控币种选择”中管理', type: 'success' });
            } else {
                MDialog.alert({ message: '添加失败: ' + (res.message || ''), type: 'danger' });
            }
        }).catch(function () {
            MDialog.alert({ message: '添加失败: 网络错误', type: 'danger' });
        }).finally(function () {
            addMonitorBtn.disabled = false;
        });
    }

    // ---------------------------------------------------------------
    // 自动刷新
    // ---------------------------------------------------------------
    function applyInterval() {
        if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
        var mins = parseInt(intervalInput.value, 10);
        refreshStatus.style.display = 'block';
        if (!mins || mins <= 0) {
            refreshStatus.textContent = '⏸ 自动刷新已关闭';
            return;
        }
        autoTimer = setInterval(function () {
            // 仅在没有扫描进行时触发
            if (!pollTimer) startScan();
        }, mins * 60 * 1000);
        refreshStatus.textContent = '✅ 每 ' + mins + ' 分钟自动重新扫描一次';
    }

    // ---------------------------------------------------------------
    // 事件绑定
    // ---------------------------------------------------------------
    scanBtn.addEventListener('click', startScan);
    fetchBtn.addEventListener('click', fetchRankings);
    infoBtn.addEventListener('click', showThresholds);
    batchStarBtn.addEventListener('click', batchStar);
    addMonitorBtn.addEventListener('click', addToMonitor);
    clearCheckBtn.addEventListener('click', clearCheck);
    applyIntervalBtn.addEventListener('click', applyInterval);

    document.getElementById('ms-tabs').querySelectorAll('.tab-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            document.getElementById('ms-tabs').querySelectorAll('.tab-btn').forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            CURRENT_VIEW = btn.dataset.view;
            emptyEl.textContent = '暂无扫描数据，点击右上角「🔍 分析」拉取全市场行情。';
            render();
        });
    });

    // ---------------------------------------------------------------
    // 初始化
    // ---------------------------------------------------------------
    loadingEl.style.display = 'block';
    loadStarred().then(function () {
        loadData(false);  // 默认只展示上次缓存，不自动重新分析
    });
});
