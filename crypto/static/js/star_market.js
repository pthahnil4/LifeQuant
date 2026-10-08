/**
 * 星标币种行情 — 前端交互脚本
 * ============================
 * 功能：数据加载、表格渲染、排序、筛选、刷新、导出
 */

document.addEventListener('DOMContentLoaded', function () {
    // ================================================================
    // DOM 元素引用
    // ================================================================
    const tableBody = document.getElementById('star-table-body');
    const tableContainer = document.getElementById('star-table-container');
    const tableEmpty = document.getElementById('star-table-empty');
    const tableLoading = document.getElementById('star-table-loading');
    const refreshBtn = document.getElementById('star-refresh-btn');
    const refreshStatus = document.getElementById('star-refresh-status');
    const lastUpdateEl = document.getElementById('star-last-update');
    const totalCountEl = document.getElementById('star-total-count');
    const filterTrend = document.getElementById('star-filter-trend');
    const filterDirection = document.getElementById('star-filter-direction');
    const filterBtn = document.getElementById('star-filter-btn');
    const clearFilterBtn = document.getElementById('star-filter-clear-btn');
    const exportCsvBtn = document.getElementById('star-export-csv');
    const exportXlsxBtn = document.getElementById('star-export-xlsx');

    // 进度条 DOM 引用
    const progressArea = document.getElementById('star-progress');
    const progressBar = document.getElementById('star-progress-bar');
    const progressMsg = document.getElementById('star-progress-msg');

    // ================================================================
    // 状态变量
    // ================================================================
    let allRecords = [];        // 原始数据缓存
    let displayRecords = [];    // 当前显示数据
    let sortCol = null;         // 当前排序列索引
    let sortDir = 'asc';        // 排序方向
    let isRefreshing = false;   // 是否正在刷新
    let filterActive = false;   // 是否有激活的筛选

    // 定时刷新状态
    let timerEnabled = false;
    let timerId = null;
    let trendRefreshing = false;  // 防止趋势刷新重入

    // 拖拽排序状态
    let dragSrcCode = null;

    // ================================================================
    // 列定义（用于表格渲染）
    // ================================================================
    const COLUMNS = [
        { key: '名称', label: '名称', type: 'text', width: 'auto' },
        { key: '代码', label: '代码', type: 'text', width: 'auto' },
        { key: '现价', label: '现价', type: 'price', width: 'auto' },
        { key: '15分钟', label: '15分钟', type: 'trend', width: 'auto' },
        { key: '60分钟', label: '60分钟', type: 'trend', width: 'auto' },
        { key: '4小时', label: '4小时', type: 'trend', width: 'auto' },
        { key: '日线', label: '日线', type: 'trend', width: 'auto' },
        { key: '上次交易时间(1H)', label: '交易时间', type: 'text', width: 'auto' },
        { key: '方向(1H)', label: '方向', type: 'direction', width: 'auto' },
        { key: '上次交易价格(1H)', label: '交易价格', type: 'number', width: 'auto' },
        { key: '策略盈亏(1H)', label: '策略盈亏', type: 'profit', width: 'auto' },
        { key: '持仓时间', label: '持仓时间', type: 'text', width: 'auto' },
        { key: '预测涨跌', label: '预测涨跌', type: 'editable', width: 'auto' },
        { key: '建议操作', label: '建议操作', type: 'editable', width: 'auto' },
    ];

    // 可编辑字段的列名
    const EDITABLE_KEYS = ['预测涨跌', '建议操作'];

    // ================================================================
    // 工具函数
    // ================================================================

    /** 获取趋势颜色类名 */
    function trendClass(val) {
        if (val === '上涨' || val === '上涨趋势') return 'td-up';
        if (val === '下跌' || val === '下跌趋势') return 'td-down';
        return 'td-wait';
    }

    /** 获取方向标签 */
    function directionHtml(val) {
        if (val === '做多') return '<span class="tag tag-long">📈 做多</span>';
        if (val === '做空') return '<span class="tag tag-short">📉 做空</span>';
        return '<span class="td-wait">' + (val || '--') + '</span>';
    }

    /** 获取盈亏颜色 */
    function profitClass(val) {
        if (!val || val === '') return '';
        var n = parseFloat(val);
        if (isNaN(n)) return '';
        return n >= 0 ? 'td-profit-positive' : 'td-profit-negative';
    }

    /** 格式化数字 */
    function fmtNum(val, decimals) {
        if (val === undefined || val === null || val === '') return '--';
        var n = parseFloat(val);
        if (isNaN(n)) return val;
        decimals = decimals || 4;
        return n.toFixed(decimals);
    }

    /** 高亮现价 */
    function priceHtml(val) {
        if (!val || val === '') return '--';
        return '<span class="price-value" style="font-size:1rem;">' + val + '</span>';
    }

    /** 获取预测/建议颜色 */
    function suggestionClass(val) {
        if (!val) return '';
        if (val.indexOf('做多') >= 0 || val.indexOf('买入') >= 0 || val.indexOf('上涨') >= 0) return 'td-up';
        if (val.indexOf('做空') >= 0 || val.indexOf('卖出') >= 0 || val.indexOf('下跌') >= 0) return 'td-down';
        return '';
    }

    /** HTML 编码 */
    function htmlEncode(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    // ================================================================
    // 核心渲染函数
    // ================================================================

    function renderTable(records) {
        if (!records || records.length === 0) {
            tableContainer.style.display = 'none';
            tableEmpty.style.display = 'block';
            tableLoading.style.display = 'none';
            return;
        }

        tableContainer.style.display = 'block';
        tableEmpty.style.display = 'none';
        tableLoading.style.display = 'none';

        var html = '';
        records.forEach(function (r) {
            var code = r['代码'] ? htmlEncode(r['代码']) : '';
            html += '<tr draggable="true" data-code="' + code + '">';
            // 拖拽手柄列
            html += '<td><span class="drag-handle" title="拖拽排序">⠿</span></td>';
            COLUMNS.forEach(function (col) {
                var val = r[col.key] || '';
                var tdClass = '';
                var content = '';

                switch (col.type) {
                    case 'trend':
                        tdClass = trendClass(val);
                        content = val || '--';
                        break;
                    case 'direction':
                        content = directionHtml(val);
                        break;
                    case 'profit':
                        tdClass = profitClass(val);
                        content = val ? val + (isNaN(parseFloat(val)) ? '' : '') : '--';
                        if (content !== '--' && !isNaN(parseFloat(val))) {
                            var n = parseFloat(val);
                            content = (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
                        }
                        break;
                    case 'price':
                        content = priceHtml(val);
                        break;
                    case 'number':
                        content = fmtNum(val, 4);
                        break;
                    case 'editable':
                        tdClass = 'td-editable';
                        content = '<span class="editable-field" data-code="' + htmlEncode(r['代码']) + '" data-col="' + col.key + '">' + (val || '') + '</span>';
                        break;
                    default:
                        content = val || '--';
                }

                html += '<td class="' + tdClass + '">' + content + '</td>';
            });
            html += '</tr>';
        });

        tableBody.innerHTML = html;
        updateStats(records.length);
    }

    /** 更新统计信息 */
    function updateStats(count) {
        if (totalCountEl) {
            totalCountEl.textContent = count;
        }
    }

    // ================================================================
    // 排序功能
    // ================================================================

    function doSort(colIndex) {
        if (sortCol === colIndex) {
            sortDir = sortDir === 'asc' ? 'desc' : 'asc';
        } else {
            sortCol = colIndex;
            sortDir = 'asc';
        }

        var records = filterActive ? [...displayRecords] : [...allRecords];
        var col = COLUMNS[colIndex];
        var key = col.key;
        var type = col.type;

        records.sort(function (a, b) {
            var va = a[key] || '';
            var vb = b[key] || '';

            // 数值比较
            if (type === 'number' || type === 'price' || type === 'profit') {
                var na = parseFloat(va);
                var nb = parseFloat(vb);
                if (!isNaN(na) && !isNaN(nb)) {
                    return sortDir === 'asc' ? na - nb : nb - na;
                }
            }

            // 文本比较
            if (va === '' && vb === '') return 0;
            if (va === '') return 1;
            if (vb === '') return -1;
            var cmp = va.localeCompare(vb, 'zh-CN');
            return sortDir === 'asc' ? cmp : -cmp;
        });

        if (filterActive) {
            displayRecords = records;
        } else {
            allRecords = records;
        }

        updateSortUI(colIndex);
        renderTable(records, filterActive ? displayRecords : allRecords);
    }

    function updateSortUI(colIndex) {
        document.querySelectorAll('#star-data-table th').forEach(function (th) {
            var idx = parseInt(th.getAttribute('data-col-index'));
            th.classList.remove('sort-asc', 'sort-desc');
            if (idx === colIndex) {
                th.classList.add(sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
            }
        });
    }

    // 表头点击排序
    document.getElementById('star-data-table').addEventListener('click', function (e) {
        var th = e.target.closest('th');
        if (!th) return;
        var idx = th.getAttribute('data-col-index');
        if (idx === null || idx === undefined) return;
        doSort(parseInt(idx));
    });

    // ================================================================
    // 筛选功能
    // ================================================================

    function applyFilters() {
        var trendVal = filterTrend ? filterTrend.value : '';
        var dirVal = filterDirection ? filterDirection.value : '';

        filterActive = !!(trendVal || dirVal);

        if (!filterActive) {
            displayRecords = [];
            renderTable(allRecords);
            return;
        }

        displayRecords = allRecords.filter(function (r) {
            // 周期趋势筛选
            if (trendVal) {
                var parts = trendVal.split('_');
                var colName = parts[0];
                var expected = parts[1];
                if ((r[colName] || '') !== expected) return false;
            }

            // 方向筛选
            if (dirVal) {
                if ((r['方向(1H)'] || '') !== dirVal) return false;
            }

            return true;
        });

        sortCol = null;
        sortDir = 'asc';
        updateSortUI(-1);
        renderTable(displayRecords);
    }

    function clearFilters() {
        if (filterTrend) filterTrend.value = '';
        if (filterDirection) filterDirection.value = '';
        filterActive = false;
        displayRecords = [];
        sortCol = null;
        sortDir = 'asc';
        updateSortUI(-1);
        renderTable(allRecords);
    }

    /** 清除全部数据（除名称、代码外所有列），并保存回 CSV */
    async function clearAllData() {
        MDialog.danger('确定要清空所有币种的行情数据吗？（仅保留名称和代码列）', async function() {
            try {
                var resp = await fetch('/api/star-market/clear-data', { method: 'POST' });
                var result = await resp.json();

                if (result.code === 200) {
                    if (refreshStatus) {
                        refreshStatus.textContent = '✅ ' + (result.message || '数据已清空');
                        refreshStatus.className = 'refresh-status success';
                    }
                    // 重新加载数据
                    await loadData();
                } else {
                    MDialog.alert({ message: '清除失败: ' + (result.message || '未知错误'), type: 'danger' });
                }
            } catch (e) {
                console.error('清除数据失败:', e);
                MDialog.alert({ message: '清除数据失败: ' + e.message, type: 'danger' });
            }
        });
    }

    if (filterBtn) filterBtn.addEventListener('click', applyFilters);
    if (clearFilterBtn) clearFilterBtn.addEventListener('click', clearAllData);

    // ================================================================
    // 数据加载
    // ================================================================

    async function loadData() {
        tableLoading.style.display = 'block';
        tableContainer.style.display = 'none';
        tableEmpty.style.display = 'none';

        try {
            var resp = await fetch('/api/star-market/data');
            var result = await resp.json();

            if (result.code === 200 && result.data) {
                allRecords = result.data.records || [];
                sortCol = null;
                sortDir = 'asc';
                filterActive = false;
                displayRecords = [];

                // 清空筛选
                if (filterTrend) filterTrend.value = '';
                if (filterDirection) filterDirection.value = '';

                updateSortUI(-1);
                renderTable(allRecords);

                // 更新时间
                if (lastUpdateEl) {
                    var refreshTime = result.data.last_refresh_time;
                    if (refreshTime) {
                        lastUpdateEl.textContent = refreshTime;
                    } else {
                        lastUpdateEl.textContent = '--';
                    }
                }
            } else {
                renderTable([]);
            }
        } catch (e) {
            console.error('加载星标行情数据失败:', e);
            tableLoading.textContent = '数据加载失败: ' + e.message;
            tableContainer.style.display = 'none';
            tableEmpty.style.display = 'block';
            tableEmpty.innerHTML = '数据加载失败，请检查网络后重试';
        }
        tableLoading.style.display = 'none';
    }

    // ================================================================
    // 刷新行情（后台异步 + 前端轮询进度）
    // ================================================================

    var progressTimer = null;

    function stopProgressPolling() {
        if (progressTimer) {
            clearInterval(progressTimer);
            progressTimer = null;
        }
    }

    function startProgressPolling() {
        stopProgressPolling();
        // 每 1.5 秒查询一次进度
        progressTimer = setInterval(pollProgress, 1500);
    }

    async function pollProgress() {
        try {
            var resp = await fetch('/api/star-market/refresh-progress');
            var result = await resp.json();

            if (result.code === 200 && result.data) {
                var p = result.data;
                updateProgressUI(p);

                // 更新状态显示
                if (refreshStatus) {
                    var symbolInfo = p.current_symbol ? ' [' + p.current_symbol + ']' : '';
                    if (p.status === 'running') {
                        refreshStatus.textContent = '⏳ ' + p.message + ' (' + p.updated + '/' + p.total + ')';
                        refreshStatus.className = 'refresh-status refreshing';
                    } else if (p.status === 'completed') {
                        refreshStatus.textContent = '✅ ' + p.message;
                        refreshStatus.className = 'refresh-status success';
                        stopProgressPolling();
                        finishRefresh();
                    } else if (p.status === 'error') {
                        refreshStatus.textContent = '❌ ' + p.message;
                        refreshStatus.className = 'refresh-status error';
                        stopProgressPolling();
                        finishRefresh();
                    }
                }
            }
        } catch (e) {
            console.error('查询刷新进度失败:', e);
        }
    }

    function updateProgressUI(p) {
        if (!progressArea || !progressBar || !progressMsg) return;
        progressArea.classList.add('active');
        var pct = p.progress_pct || 0;
        progressBar.style.width = pct + '%';
        progressBar.textContent = pct >= 5 ? pct + '%' : '';

        if (p.status === 'running') {
            progressMsg.textContent = p.message || ('处理中: ' + p.current + '/' + p.total);
        } else if (p.status === 'completed') {
            progressMsg.textContent = p.message || '刷新完成!';
            progressBar.style.background = 'linear-gradient(90deg, #5cb85c, #5cb85c)';
        } else if (p.status === 'error') {
            progressMsg.textContent = '错误: ' + (p.message || '未知错误');
            progressBar.style.background = '#e74c3c';
        }
    }

    async function finishRefresh() {
        isRefreshing = false;
        refreshBtn.disabled = false;
        refreshBtn.textContent = '🔄 刷新行情';
        // 重新加载数据
        await loadData();
    }

    async function refreshPrices() {
        if (isRefreshing) return;

        isRefreshing = true;
        refreshBtn.disabled = true;
        refreshBtn.textContent = '⏳ 刷新中...';
        if (refreshStatus) {
            refreshStatus.textContent = '正在启动刷新任务...';
            refreshStatus.className = 'refresh-status refreshing';
        }

        // 显示进度条
        if (progressArea) {
            progressArea.classList.add('active');
            progressBar.style.width = '0%';
            progressBar.style.background = 'linear-gradient(90deg, #4a90d9, #5cb85c)';
            progressBar.textContent = '';
            progressMsg.textContent = '正在启动刷新任务...';
        }

        try {
            var resp = await fetch('/api/star-market/refresh', { method: 'POST' });
            var result = await resp.json();

            if (result.code === 200) {
                var p = result.data;
                // 刷新请求已发送，只要不是终态（completed/error）就启动轮询
                // 兼容后端可能返回 running / started / idle 等过渡状态
                if (p && p.status !== 'completed' && p.status !== 'error') {
                    // 开始轮询进度（轮询函数内部会自动判断终态并停止）
                    startProgressPolling();
                } else if (p && (p.status === 'completed' || p.status === 'error')) {
                    // 任务已瞬间完成或失败（罕见情况）
                    if (refreshStatus) {
                        refreshStatus.textContent = (p.status === 'completed' ? '✅ ' : '❌ ') + p.message;
                        refreshStatus.className = 'refresh-status ' + (p.status === 'completed' ? 'success' : 'error');
                    }
                    await finishRefresh();
                }
            } else {
                if (refreshStatus) {
                    refreshStatus.textContent = '❌ 刷新失败: ' + (result.message || '未知错误');
                    refreshStatus.className = 'refresh-status error';
                }
                finishRefresh();
            }
        } catch (e) {
            console.error('刷新价格失败:', e);
            if (refreshStatus) {
                refreshStatus.textContent = '❌ 网络请求失败: ' + e.message;
                refreshStatus.className = 'refresh-status error';
            }
            finishRefresh();
        }
    }

    if (refreshBtn) refreshBtn.addEventListener('click', refreshPrices);

    // ================================================================
    // 导出功能
    // ================================================================

    if (exportCsvBtn) {
        exportCsvBtn.addEventListener('click', function () {
            window.location.href = '/api/star-market/export/csv';
        });
    }

    if (exportXlsxBtn) {
        exportXlsxBtn.addEventListener('click', function () {
            window.location.href = '/api/star-market/export/xlsx';
        });
    }

    // ================================================================
    // 同步星标按钮
    // ================================================================

    const syncBtn = document.getElementById('star-sync-btn');
    if (syncBtn) {
        syncBtn.addEventListener('click', async function () {
            syncBtn.textContent = '⏳ 同步中...';
            syncBtn.disabled = true;
            try {
                var resp = await fetch('/api/star-market/sync', { method: 'POST' });
                var result = await resp.json();
                if (result.code === 200) {
                    if (refreshStatus) {
                        refreshStatus.textContent = '✅ ' + (result.message || '同步完成');
                        refreshStatus.className = 'refresh-status success';
                    }
                    // 重新加载数据
                    await loadData();
                } else {
                    MDialog.alert({ message: '同步失败: ' + (result.message || '未知错误'), type: 'danger' });
                }
            } catch (e) {
                console.error('同步星标失败:', e);
                MDialog.alert({ message: '同步星标失败: ' + e.message, type: 'danger' });
            } finally {
                syncBtn.textContent = '🔄 同步星标';
                syncBtn.disabled = false;
            }
        });
    }

    // ================================================================
    // 趋势方向刷新（15分钟 / 60分钟）
    // ================================================================

    /**
     * 刷新指定周期的趋势方向
     */
    async function refreshTrendColumn(bar, colName, btnId) {
        if (trendRefreshing) return;  // 防止重入
        var btn = document.getElementById(btnId);

        trendRefreshing = true;
        var originalText = btn ? btn.textContent : '';
        if (btn) {
            btn.disabled = true;
            btn.textContent = '⏳ 刷新中...';
        }
        if (refreshStatus) {
            refreshStatus.textContent = '正在刷新 ' + colName + ' 趋势...';
            refreshStatus.className = 'refresh-status refreshing';
        }

        try {
            var resp = await fetch('/api/star-market/refresh-trend', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ bar: bar, col: colName })
            });
            var result = await resp.json();

            if (result.code === 200 && result.data) {
                var d = result.data;
                var msg = d.message || '刷新完成';
                if (refreshStatus) {
                    refreshStatus.textContent = '✅ ' + msg;
                    refreshStatus.className = 'refresh-status success';
                }
                // 重新加载数据
                await loadData();
            } else {
                if (refreshStatus) {
                    refreshStatus.textContent = '❌ 刷新失败: ' + (result.message || '未知错误');
                    refreshStatus.className = 'refresh-status error';
                }
            }
        } catch (e) {
            console.error('刷新趋势失败:', e);
            if (refreshStatus) {
                refreshStatus.textContent = '❌ 网络请求失败: ' + e.message;
                refreshStatus.className = 'refresh-status error';
            }
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = originalText;
            }
            trendRefreshing = false;
        }
    }

    // 15分钟趋势刷新按钮
    var trend15mBtn = document.getElementById('star-trend-15m');
    if (trend15mBtn) {
        trend15mBtn.addEventListener('click', function () {
            refreshTrendColumn('15m', '15分钟', 'star-trend-15m');
        });
    }

    // 60分钟趋势刷新按钮
    var trend1hBtn = document.getElementById('star-trend-1h');
    if (trend1hBtn) {
        trend1hBtn.addEventListener('click', function () {
            refreshTrendColumn('1H', '60分钟', 'star-trend-1h');
        });
    }

    // ================================================================
    // 定时自动刷新
    // ================================================================

    var timerToggle = document.getElementById('star-timer-toggle');
    var timerInterval = document.getElementById('star-timer-interval');
    var timerBar = document.getElementById('star-timer-bar');

    function stopTimer() {
        if (timerId) {
            clearInterval(timerId);
            timerId = null;
        }
        timerEnabled = false;
        if (timerToggle) {
            timerToggle.textContent = '▶ 开启';
            timerToggle.className = 'm-btn m-btn-success m-btn-sm m-btn-outline inactive';
        }
    }

    function startTimer() {
        if (timerId) stopTimer();
        var minutes = parseInt(timerInterval ? timerInterval.value : 5);
        var ms = minutes * 60 * 1000;
        var barLabel = timerBar ? timerBar.options[timerBar.selectedIndex].text : '15分钟';

        timerEnabled = true;
        if (timerToggle) {
            timerToggle.textContent = '⏸ ' + barLabel + ' (' + minutes + 'min)';
            timerToggle.className = 'm-btn m-btn-success m-btn-sm active';
        }

        timerId = setInterval(async function () {
            // 页面可见时才执行
            if (document.hidden) return;

            var bar = timerBar ? timerBar.value : '15m';
            var colName = bar === '15m' ? '15分钟' : '60分钟';
            var btnId = bar === '15m' ? 'star-trend-15m' : 'star-trend-1h';

            if (refreshStatus) {
                refreshStatus.textContent = '⏰ 定时刷新 ' + colName + ' 趋势...';
                refreshStatus.className = 'refresh-status refreshing';
            }
            await refreshTrendColumn(bar, colName, btnId);
        }, ms);
    }

    if (timerToggle) {
        timerToggle.addEventListener('click', function () {
            if (timerEnabled) {
                stopTimer();
            } else {
                startTimer();
            }
        });
    }

    // 定时器运行中切换周期时，实时更新按钮文字
    if (timerBar) {
        timerBar.addEventListener('change', function () {
            if (timerEnabled) {
                var minutes = parseInt(timerInterval ? timerInterval.value : 5);
                var barLabel = timerBar.options[timerBar.selectedIndex].text;
                if (timerToggle) {
                    timerToggle.textContent = '⏸ ' + barLabel + ' (' + minutes + 'min)';
                }
            }
        });
    }

    // ================================================================
    // 拖拽排序
    // ================================================================

    var tableBodyEl = document.getElementById('star-table-body');

    function onDragStart(e) {
        var tr = e.target.closest('tr');
        if (!tr) return;
        dragSrcCode = tr.getAttribute('data-code');
        tr.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', dragSrcCode || '');
    }

    function onDragEnd(e) {
        var tr = e.target.closest('tr');
        if (tr) tr.classList.remove('dragging');
        document.querySelectorAll('#star-table-body tr').forEach(function (r) {
            r.classList.remove('drag-over', 'drag-over-top');
        });
    }

    function onDragOver(e) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';

        var targetTr = e.target.closest('tr');
        if (!targetTr || targetTr.getAttribute('data-code') === dragSrcCode) return;

        // 清除其他高亮
        document.querySelectorAll('#star-table-body tr').forEach(function (r) {
            r.classList.remove('drag-over', 'drag-over-top');
        });

        // 判断拖拽目标是在上方还是下方
        var rect = targetTr.getBoundingClientRect();
        var midY = rect.top + rect.height / 2;
        if (e.clientY < midY) {
            targetTr.classList.add('drag-over-top');
        } else {
            targetTr.classList.add('drag-over');
        }
    }

    function onDrop(e) {
        e.preventDefault();
        var targetTr = e.target.closest('tr');
        if (!targetTr || !dragSrcCode) return;

        var targetCode = targetTr.getAttribute('data-code');
        if (!targetCode || targetCode === dragSrcCode) return;

        // 从数组中获取当前顺序
        var order = allRecords.map(function (r) { return r['代码'] ? r['代码'].trim() : ''; });

        var srcIdx = order.indexOf(dragSrcCode);
        var tgtIdx = order.indexOf(targetCode);
        if (srcIdx === -1 || tgtIdx === -1) return;

        // 判断插入位置
        var rect = targetTr.getBoundingClientRect();
        var midY = rect.top + rect.height / 2;
        var insertBefore = e.clientY < midY;

        // 从数组中移除源元素
        order.splice(srcIdx, 1);
        // 重新计算目标索引（因为移除了一个元素）
        tgtIdx = order.indexOf(targetCode);
        // 插入到目标位置
        var insertAt = insertBefore ? tgtIdx : tgtIdx + 1;
        order.splice(insertAt, 0, dragSrcCode);

        // 保存排序到后端
        saveOrder(order);

        // 清除高亮
        document.querySelectorAll('#star-table-body tr').forEach(function (r) {
            r.classList.remove('drag-over', 'drag-over-top');
        });
    }

    async function saveOrder(orderedCodes) {
        var feedback = document.getElementById('star-order-feedback');
        if (feedback) {
            feedback.textContent = '⏳ 保存顺序...';
            feedback.className = 'order-save-feedback show';
        }

        try {
            var resp = await fetch('/api/star-market/reorder', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ordered_codes: orderedCodes })
            });
            var result = await resp.json();

            if (result.code === 200) {
                if (feedback) {
                    feedback.textContent = '✅ 顺序已保存';
                }
                // 重新加载数据获取新的顺序
                await loadData();
            } else {
                if (feedback) {
                    feedback.textContent = '❌ 保存失败';
                }
            }
        } catch (e) {
            console.error('保存排序失败:', e);
            if (feedback) {
                feedback.textContent = '❌ 网络错误';
            }
        }

        // 3 秒后隐藏反馈
        if (feedback) {
            setTimeout(function () {
                feedback.className = 'order-save-feedback';
            }, 3000);
        }
    }

    // 绑定拖拽事件（使用事件委托）
    if (tableBodyEl) {
        tableBodyEl.addEventListener('dragstart', onDragStart);
        tableBodyEl.addEventListener('dragend', onDragEnd);
        tableBodyEl.addEventListener('dragover', onDragOver);
        tableBodyEl.addEventListener('drop', onDrop);
    }

    // ================================================================
    // 内联编辑功能
    // ================================================================

    var currentlyEditing = null;  // 当前正在编辑的 span 元素

    /** 进入编辑模式 */
    function enterEditMode(span) {
        // 如果已有正在编辑的，先取消
        if (currentlyEditing && currentlyEditing !== span) {
            cancelEditMode(currentlyEditing);
        }

        if (span.classList.contains('editing')) return; // 已在编辑

        var oldVal = span.textContent;
        var code = span.getAttribute('data-code');
        var col = span.getAttribute('data-col');

        span.classList.add('editing');
        span.innerHTML = '<input class="editable-input" type="text" value="' + htmlEncode(oldVal) + '" data-old="' + htmlEncode(oldVal) + '">'
            + '<button class="btn-save-field" onclick="event.stopPropagation();saveField(this)">保存</button>'
            + '<button class="btn-cancel-field" onclick="event.stopPropagation();cancelEdit(this)">取消</button>'
            + '<span class="save-feedback" id="save-feedback-' + code + '-' + col + '"></span>';

        currentlyEditing = span;

        // 自动聚焦输入框
        var input = span.querySelector('.editable-input');
        if (input) {
            setTimeout(function () { input.focus(); input.select(); }, 50);
        }
    }

    /** 保存字段 */
    window.saveField = async function (btn) {
        var span = btn.closest('.editable-field');
        if (!span) return;

        var input = span.querySelector('.editable-input');
        if (!input) return;

        var code = span.getAttribute('data-code');
        var col = span.getAttribute('data-col');
        var newVal = input.value.trim();
        var oldVal = input.getAttribute('data-old') || '';

        // 如果值没变化，直接取消编辑
        if (newVal === oldVal) {
            cancelEditMode(span);
            return;
        }

        // 禁用按钮防止重复提交
        btn.disabled = true;
        var feedback = document.getElementById('save-feedback-' + code + '-' + col);
        if (feedback) feedback.textContent = '保存中...';

        try {
            // 构建 payload
            var payload = { code: code, updates: {} };
            payload.updates[col] = newVal;

            var resp = await fetch('/api/star-market/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            var result = await resp.json();

            if (result.code === 200) {
                if (feedback) {
                    feedback.textContent = '✅ 已保存';
                    feedback.className = 'save-feedback success';
                }
                // 更新内部数据
                for (var i = 0; i < allRecords.length; i++) {
                    if (allRecords[i]['代码'] === code) {
                        allRecords[i][col] = newVal;
                        break;
                    }
                }
                // 更新显示数据（如果筛选激活）
                if (filterActive) {
                    for (var i = 0; i < displayRecords.length; i++) {
                        if (displayRecords[i]['代码'] === code) {
                            displayRecords[i][col] = newVal;
                            break;
                        }
                    }
                }
                // 退出编辑模式显示新值
                setTimeout(function () {
                    span.classList.remove('editing');
                    span.innerHTML = htmlEncode(newVal || '');
                    currentlyEditing = null;
                }, 800);
            } else {
                if (feedback) {
                    feedback.textContent = '❌ ' + (result.message || '保存失败');
                    feedback.className = 'save-feedback error';
                }
                btn.disabled = false;
            }
        } catch (e) {
            console.error('保存字段失败:', e);
            if (feedback) {
                feedback.textContent = '❌ 网络错误';
                feedback.className = 'save-feedback error';
            }
            btn.disabled = false;
        }
    };

    /** 取消编辑 */
    window.cancelEdit = function (btn) {
        var span = btn.closest('.editable-field');
        if (span) cancelEditMode(span);
    };

    function cancelEditMode(span) {
        if (!span) return;
        var oldVal = span.querySelector('.editable-input')
            ? span.querySelector('.editable-input').getAttribute('data-old') || ''
            : span.textContent;
        span.classList.remove('editing');
        span.innerHTML = htmlEncode(oldVal);
        if (currentlyEditing === span) {
            currentlyEditing = null;
        }
    }

    // 点击 editable-field 进入编辑模式（事件委托）
    document.getElementById('star-data-table').addEventListener('click', function (e) {
        var span = e.target.closest('.editable-field');
        if (!span) return;
        if (span.classList.contains('editing')) return; // 已在编辑模式
        // 如果点击的是已编辑模式内部元素，忽略
        if (e.target.closest('.btn-save-field') || e.target.closest('.btn-cancel-field') || e.target.closest('.editable-input')) {
            return;
        }
        enterEditMode(span);
    });

    // 键盘事件：Enter 保存，Escape 取消
    document.getElementById('star-data-table').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
            var input = e.target.closest('.editable-input');
            if (input) {
                e.preventDefault();
                var span = input.closest('.editable-field');
                if (span) {
                    var saveBtn = span.querySelector('.btn-save-field');
                    if (saveBtn) saveBtn.click();
                }
            }
        }
        if (e.key === 'Escape') {
            var input = e.target.closest('.editable-input');
            if (input) {
                e.preventDefault();
                var span = input.closest('.editable-field');
                if (span) {
                    var cancelBtn = span.querySelector('.btn-cancel-field');
                    if (cancelBtn) cancelBtn.click();
                }
            }
        }
    });

    // ================================================================
    // 自动刷新（每 5 分钟）
    // ================================================================

    setInterval(function () {
        // 只在页面可见时自动刷新数据表格
        if (!document.hidden) {
            loadData();
        }
    }, 300000); // 5 分钟

    // ================================================================
    // 初始化
    // ================================================================

    loadData();
});
