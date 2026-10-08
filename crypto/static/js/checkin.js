/**
 * 每日多维打卡 —— 前端交互层（批次15）
 * =====================================
 * 同一文件服务两个挂载点（渐进增强、零依赖）：
 *   1. /checkin 打卡页（#checkin-page）：三项指标卡 + 评级/评价 + 提交 + 近30天历史
 *   2. 监控台首页状态条（#checkin-bar）：只读展示今日达标情况
 * 数据全部来自 /checkin/api/*；页面自动加载只读缓存，OKX 实拉仅由
 * 「刷新交易数据」按钮（POST refresh-trade）或服务端提交时触发。
 */
(function () {
    'use strict';

    function $(sel, root) { return (root || document).querySelector(sel); }
    function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
    function escapeHtml(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }
    function fmt(v, digits) {
        if (v === null || v === undefined || isNaN(v)) return '--';
        return Number(v).toFixed(digits === undefined ? 1 : digits).replace(/\.0+$/, '');
    }
    function fmtMoney(v) {
        if (v === null || v === undefined || isNaN(v)) return '--';
        return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    function fmtPct(v) {
        if (v === null || v === undefined || isNaN(v)) return '--';
        var n = Number(v);
        return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
    }
    function toast(msg, kind) {
        var t = $('#ck-toast');
        if (!t) return;
        t.textContent = msg;
        t.className = 'task-toast show toast-' + (kind || 'info');
        clearTimeout(t._h);
        t._h = setTimeout(function () { t.className = 'task-toast'; }, 2200);
    }
    function apiGet(url) {
        return fetch(url).then(function (r) { return r.json(); });
    }
    function apiPost(url, body) {
        return fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {})
        }).then(function (r) { return r.json(); });
    }
    function todayStr() {
        var d = new Date();
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    }

    /* 达标徽章文案 */
    function badgeHtml(passed, status) {
        if (status && status !== 'ok') return '<span class="ck-badge na">无数据</span>';
        return passed ? '<span class="ck-badge pass">达标</span>' : '<span class="ck-badge fail">未达标</span>';
    }

    /* 交易项明细行（含各类降级状态的用户可读提示） */
    function tradeDetailHtml(t) {
        if (t.status === 'no_data') return '尚无可用数据（历史日期需当日有余额快照才能计算）';
        if (t.status === 'api_error') return 'OKX 接口异常，请稍后重试';
        if (t.status === 'no_base') return '缺少基准快照（balance_history 无更早记录），无法计算涨幅';
        var parts = [];
        parts.push('基准 ' + escapeHtml(t.base_date || '--') + '：' + fmtMoney(t.base));
        parts.push('当前权益：' + fmtMoney(t.live));
        if (t.transfer !== null && t.transfer !== undefined) {
            parts.push('当日净入金已剔除：' + fmtMoney(t.transfer));
        }
        if (t.fetched_at) {
            parts.push('取数 ' + escapeHtml(String(t.fetched_at).slice(11, 19) || t.fetched_at) +
                (t.src === 'cache' || t.src === 'cache_stale' ? '（缓存）' : ''));
        }
        return parts.join('<br>');
    }

    /* =====================================================================
     * 打卡页（/checkin）
     * ===================================================================== */
    function initPage() {
        var dateInput = $('#ck-date');
        var notice = $('#ck-notice');
        var refreshBtn = $('#ck-refresh-trade');
        var submitBtn = $('#ck-submit');
        var commentEl = $('#ck-comment');
        var rating = '';        // 当前选中评级（'' = 未选）
        var current = null;     // 最近一次 api/today 返回的 data

        // 初始日期：支持 ?date=YYYY-MM-DD 深链（从日记本「查看今日评级」跳回）
        var urlDate = (new URLSearchParams(window.location.search).get('date') || '').trim();
        var initDate = /^\d{4}-\d{2}-\d{2}$/.test(urlDate) && urlDate <= todayStr()
            ? urlDate : todayStr();
        dateInput.value = initDate;
        dateInput.max = todayStr();

        function diaryUrl(ds) {
            // 跳日记本并自动关联当日：date + link=checkin:YYYY-MM-DD 回指格式
            return '/diary?date=' + encodeURIComponent(ds) +
                '&link=' + encodeURIComponent('checkin:' + ds);
        }

        function setNotice(text) {
            notice.textContent = text || '';
            notice.style.display = text ? 'block' : 'none';
        }

        // 日记关联提示 + 未评级高亮（依 api/today 返回的 diary 与 saved 状态）
        function renderDiary(data) {
            var date = data.date;
            var saved = data.saved;
            var diary = data.diary || { has_entry: false, entry_count: 0 };
            var entryBtn = $('#ck-diary-entry');
            if (entryBtn) {
                entryBtn.href = diaryUrl(date);
                entryBtn.textContent = diary.has_entry
                    ? '📔 今日日记（已写 ' + diary.entry_count + ' 条）'
                    : '📔 去写今日日记';
            }
            var tip = $('#ck-diary-tip');
            var jump = $('#ck-diary-jump');
            // 仅「当日已打卡但没写日记」时顶部提示（未打卡时页面主提示是引导打卡）
            if (tip && jump) {
                if (saved && !diary.has_entry) {
                    if (jump) jump.href = diaryUrl(date);
                    tip.classList.add('show');
                } else {
                    tip.classList.remove('show');
                }
            }
            // 已打卡但未评级 → 高亮评级选择区
            var ratingBox = $('#ck-rating');
            if (ratingBox) {
                ratingBox.classList.toggle('need', !!saved && !(saved.rating && saved.rating.length));
            }
        }

        function renderMetrics(data) {
            var m = data.metrics;
            // 热量
            var calCard = $('#ck-card-calorie');
            $('.ck-badge', calCard).outerHTML = badgeHtml(m.calorie.passed, m.calorie.status);
            $('.ck-card-value', calCard).innerHTML = fmt(m.calorie.value, 0) + ' <small>kcal</small>';
            $('.ck-card-target', calCard).textContent = '目标：缺口 ≥ ' + fmt(m.targets.calorie, 0) + ' kcal';
            // 交易
            var trCard = $('#ck-card-trade');
            $('.ck-badge', trCard).outerHTML = badgeHtml(m.trade.passed, m.trade.status);
            $('.ck-card-value', trCard).innerHTML = fmtPct(m.trade.pct) + ' <small>权益涨幅</small>';
            $('.ck-card-target', trCard).textContent = '目标：当日权益涨幅 ≥ ' + fmt(m.targets.trade_pct, 0) + '%';
            $('#ck-trade-detail').innerHTML = tradeDetailHtml(m.trade);
            // 学习
            var lnCard = $('#ck-card-learn');
            $('.ck-badge', lnCard).outerHTML = badgeHtml(m.learn.passed, m.learn.status);
            $('#ck-learn-value').innerHTML = fmt(m.learn.hours, 1) + ' <small>h · 完成任务 ' +
                (m.learn.task_done_count == null ? '--' : m.learn.task_done_count) + ' 个</small>';
            $('#ck-learn-target').textContent = '目标：≥ ' + fmt(m.targets.learn_hours, 0) +
                ' 小时 且 完成任务 ≥ ' + m.targets.task_count + ' 个';
            // 总达标
            var allBadge = $('#ck-all-badge');
            allBadge.className = 'ck-badge ' + (m.all_passed ? 'pass' : 'na');
            allBadge.textContent = '三项全达标：' + (m.all_passed ? '是' : '否');
            // 刷新按钮仅今日可用
            $('#ck-trade-actions').style.display = data.is_today ? '' : 'none';
        }

        function load(date) {
            setNotice('');
            apiGet('/checkin/api/today?date=' + encodeURIComponent(date))
                .then(function (res) {
                    if (res.code !== 200 || !res.data) { toast(res.message || '加载失败', 'error'); return; }
                    current = res.data;
                    renderMetrics(current);
                    renderDiary(current);
                    // 已存打卡回填评级与评价（覆盖提交前先看原内容）
                    var saved = current.saved;
                    if (saved) {
                        setRating(saved.rating || '');
                        commentEl.value = saved.comment || '';
                        setNotice(date === todayStr()
                            ? '今日已打卡（' + (saved.updated_at || saved.date) + '），重新提交将覆盖原记录'
                            : '该日已有打卡记录（' + saved.date + '），重新提交将覆盖');
                    } else {
                        setRating('');
                        commentEl.value = '';
                    }
                    if (!current.is_today) {
                        setNotice('补录模式：交易项按本地余额快照差分计算，不触达 OKX；缺快照的日期无法计算涨幅。' +
                            (saved ? ' 该日已有打卡记录，重新提交将覆盖。' : ''));
                    }
                })
                .catch(function (e) { toast('加载失败：' + e, 'error'); });
        }

        function setRating(val) {
            rating = val;
            $$('.ck-rating-chip').forEach(function (chip) {
                chip.classList.toggle('sel', chip.getAttribute('data-rating') === val);
            });
        }

        $$('.ck-rating-chip').forEach(function (chip) {
            chip.addEventListener('click', function () {
                var val = chip.getAttribute('data-rating');
                setRating(rating === val ? '' : val);
            });
        });

        dateInput.addEventListener('change', function () {
            if (dateInput.value) load(dateInput.value);
        });
        $('#ck-goto-today').addEventListener('click', function () {
            dateInput.value = todayStr();
            load(todayStr());
        });

        refreshBtn.addEventListener('click', function () {
            refreshBtn.disabled = true;
            refreshBtn.textContent = '⏳ 正在拉取…';
            apiPost('/checkin/api/refresh-trade', { date: dateInput.value })
                .then(function (res) {
                    if (res.code !== 200) { toast(res.message || '刷新失败', 'error'); return; }
                    toast(res.message === '缓存命中' ? '缓存仍新鲜（5 分钟内），如需强制重拉稍后再试' : '交易数据已更新', 'success');
                    load(dateInput.value);
                })
                .catch(function (e) { toast('刷新失败：' + e, 'error'); })
                .finally(function () {
                    refreshBtn.disabled = false;
                    refreshBtn.textContent = '🔄 刷新交易数据';
                });
        });

        submitBtn.addEventListener('click', function () {
            if (!current) { toast('数据尚未加载完成', 'error'); return; }
            submitBtn.disabled = true;
            submitBtn.textContent = '⏳ 提交中…';
            apiPost('/checkin/api/checkin', {
                date: dateInput.value,
                rating: rating,
                comment: commentEl.value
            })
                .then(function (res) {
                    if (res.code !== 200) { toast(res.message || '提交失败', 'error'); return; }
                    toast('打卡已保存（' + res.data.date + '）', 'success');
                    load(dateInput.value);
                    loadHistory();
                })
                .catch(function (e) { toast('提交失败：' + e, 'error'); })
                .finally(function () {
                    submitBtn.disabled = false;
                    submitBtn.textContent = '✅ 提交打卡';
                });
        });

        function loadHistory() {
            apiGet('/checkin/api/history')
                .then(function (res) {
                    var body = $('#ck-hist-body');
                    if (res.code !== 200 || !res.data) { return; }
                    var items = res.data.items || [];
                    if (!items.length) {
                        body.innerHTML = '<tr><td colspan="4" style="color:#999;">近 30 天暂无打卡记录</td></tr>';
                        return;
                    }
                    body.innerHTML = items.map(function (it) {
                        var dots = ['calorie', 'trade', 'learn'].map(function (k) {
                            return '<span class="ck-dot ' + (it[k] && it[k].passed ? 'y' : 'n') + '" title="' + k + '"></span>';
                        }).join('');
                        var comment = escapeHtml((it.comment || '').slice(0, 40)) +
                            ((it.comment || '').length > 40 ? '…' : '');
                        return '<tr class="ck-hist-row" data-date="' + escapeHtml(it.date) + '">' +
                            '<td>' + escapeHtml(it.date) + '</td>' +
                            '<td>' + dots + (it.all_passed ? ' <span class="ck-allpass" style="color:#2e7d32;">全达标</span>' : '') + '</td>' +
                            '<td>' + (it.rating ? escapeHtml(it.rating) : '--') + '</td>' +
                            '<td style="color:#888;">' + (comment || '--') + '</td>' +
                            '</tr>';
                    }).join('');
                    $$('.ck-hist-row', body).forEach(function (row) {
                        row.addEventListener('click', function () {
                            dateInput.value = row.getAttribute('data-date');
                            load(row.getAttribute('data-date'));
                            window.scrollTo({ top: 0, behavior: 'smooth' });
                        });
                    });
                });
        }

        load(dateInput.value);
        loadHistory();
    }

    /* =====================================================================
     * 监控台首页状态条（#checkin-bar）
     * 无论成败都保持可见：这是个人管理总控台的门面信息，静默隐藏等于没入口
     * ===================================================================== */
    function initBar() {
        var bar = $('#checkin-bar');
        if (!bar) return;
        function show(html) {
            bar.innerHTML = '<span class="ck-bar-title">📋 今日评级</span>' + html +
                '<span class="ck-bar-spacer"></span>' +
                '<a href="/checkin" class="m-btn m-btn-primary m-btn-xs">去打卡</a>';
            bar.style.display = 'flex';
        }
        apiGet('/checkin/api/today')
            .then(function (res) {
                if (res.code !== 200 || !res.data) {
                    show('<span class="ck-bar-item" style="color:#c62828;">加载失败：' +
                        escapeHtml(res.message || '接口异常') + '</span>');
                    return;
                }
                var m = res.data.metrics;
                function dot(passed) { return '<span class="ck-dot ' + (passed ? 'y' : 'n') + '"></span>'; }
                var saved = res.data.saved;
                var link = saved
                    ? '<a href="/checkin" class="m-btn m-btn-secondary m-btn-xs">查看/覆盖</a>'
                    : '<a href="/checkin" class="m-btn m-btn-primary m-btn-xs">去打卡</a>';
                // 已打卡但无评级 → 待评级徽章（引导补评）；有评级则直接展示等级
                var ratingCell;
                if (saved && saved.rating) {
                    ratingCell = '<span class="ck-bar-item">评级 <b>' + escapeHtml(saved.rating) + '</b></span>';
                } else if (saved) {
                    ratingCell = '<span class="ck-badge fail" style="font-size:.72rem;">待评级</span>';
                } else {
                    ratingCell = '<span class="ck-bar-item">评级 <b>--</b></span>';
                }
                bar.innerHTML =
                    '<span class="ck-bar-title">📋 今日评级</span>' +
                    '<span class="ck-bar-item">' + dot(m.calorie.passed) + '热量 <b>' + fmt(m.calorie.value, 0) + ' kcal</b></span>' +
                    '<span class="ck-bar-item">' + dot(m.trade.passed) + '交易 <b>' + fmtPct(m.trade.pct) + '</b></span>' +
                    '<span class="ck-bar-item">' + dot(m.learn.passed) + '学习 <b>' + fmt(m.learn.hours, 1) + 'h</b></span>' +
                    ratingCell +
                    (saved ? '<span class="ck-badge pass" style="font-size:.72rem;">已打卡</span>'
                           : '<span class="ck-badge fail" style="font-size:.72rem;">未打卡</span>') +
                    '<span class="ck-bar-spacer"></span>' + link;
                bar.style.display = 'flex';
            })
            .catch(function () {
                show('<span class="ck-bar-item" style="color:#c62828;">加载失败（服务未重启或接口不可达）</span>');
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        if ($('#checkin-page')) initPage();
        initBar();
    });
})();
