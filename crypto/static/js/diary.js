/**
 * 日记本 - 前端交互脚本
 * ==========================
 * 功能：按天记录 / 日历导航 / 分页时间线 / 公开·隐私双模式 /
 *       访问密码（argon2 后端哈希，正文脱敏在服务端完成）/
 *       人生计数器 / 目标倒计时器 / JSON 导出·导入 / 跨模块日期跳转
 *
 * 隐私口径：未解锁时接口返回的隐私条目 content 恒为空串（服务端脱敏），
 * 前端只负责把空正文渲染成"🔒 隐私日记"占位卡，不做任何本地加解密。
 */
(function () {
    'use strict';

    // =========================================================================
    // 全局状态
    // =========================================================================
    var API = '/diary/api';
    var PAGE_SIZE = 30;
    var state = {
        settings: { visibility: 'public', has_password: false, birth_date: '' },
        unlocked: true,
        locked_mode: 'unlocked',   // unlocked / locked / partial
        entries: [],               // 已加载页拼接（日期倒序）
        has_more: false,
        goals: [],
        life_age: null,            // {days, hours, total_days_float}
        life_ref_ts: 0,            // 服务器返回 life_age 的时刻（本地插值用）
        cal_month: '',             // YYYY-MM
        selected_date: '',         // 日历选中的日期（''=时间线模式）
        day_entries: null,         // 指定日期模式下的当日条目
        mood: '',
        private_flag: false,
        linked_from: '',
        stats: { total_entries: 0 },
        loading: false
    };

    var MOODS = ['😄', '🙂', '😐', '😢', '😡', '🤩', '😴', '🌈'];

    // =========================================================================
    // 工具
    // =========================================================================
    function $(id) { return document.getElementById(id); }

    function escapeHtml(str) {
        return String(str == null ? '' : str)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function toast(msg, type) {
        var el = document.createElement('div');
        el.className = 'journal-toast' + (type === 'error' ? ' journal-toast-error' : '');
        el.textContent = msg;
        document.body.appendChild(el);
        requestAnimationFrame(function () { el.classList.add('show'); });
        setTimeout(function () {
            el.classList.remove('show');
            setTimeout(function () { el.remove(); }, 300);
        }, 1800);
    }

    function _pad2(n) { return String(n).padStart(2, '0'); }
    function _ymd(d) { return d.getFullYear() + '-' + _pad2(d.getMonth() + 1) + '-' + _pad2(d.getDate()); }

    function todayStr() { return _ymd(new Date()); }

    function dateLabel(ds) {
        if (ds === todayStr()) return '今天';
        var y = new Date(Date.now() - 86400000);
        if (ds === _ymd(y)) return '昨天';
        var parts = ds.split('-');
        return parts[0] + '年' + parseInt(parts[1], 10) + '月' + parseInt(parts[2], 10) + '日';
    }

    function timePart(datetimeStr) { return (datetimeStr || '').substring(11, 16); }

    function postJson(path, body) {
        return fetch(API + path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body || {})
        }).then(function (r) { return r.json(); });
    }

    function getJson(path) {
        return fetch(API + path, { credentials: 'same-origin' })
            .then(function (r) { return r.json(); });
    }

    // =========================================================================
    // 首屏加载
    // =========================================================================
    function bootstrap() {
        var params = new URLSearchParams(window.location.search);
        var wantDate = (params.get('date') || '').trim();
        var linked = (params.get('link') || params.get('linked') || '').trim();
        if (linked) state.linked_from = linked;
        getJson('/bootstrap?limit=' + PAGE_SIZE + (wantDate ? '&date=' + encodeURIComponent(wantDate) : ''))
            .then(function (res) {
                if (res.code !== 200) { toast('加载失败：' + res.message, 'error'); return; }
                var d = res.data || {};
                state.settings = d.settings || state.settings;
                state.unlocked = !!d.unlocked;
                state.locked_mode = d.locked_mode || 'unlocked';
                state.entries = d.entries || [];
                state.has_more = !!d.has_more;
                state.goals = d.goals || [];
                state.life_age = d.life_age || null;
                state.life_ref_ts = Date.now();
                state.stats = d.stats || state.stats;
                if (d.day) { state.selected_date = d.day; state.day_entries = d.day_entries || []; }
                var cur = new Date();
                state.cal_month = (state.selected_date || todayStr()).substring(0, 7);
                renderAll();
                loadCalendar();
            })
            .catch(function (e) { toast('网络错误，加载失败', 'error'); console.error(e); });
    }

    function reloadKeepFeed() {
        bootstrap();
    }

    // =========================================================================
    // 渲染总控
    // =========================================================================
    function renderAll() {
        renderLockBanner();
        renderHeroButtons();
        renderLifeCard();
        renderGoals();
        renderComposer();
        renderFeed();
    }

    function renderHeroButtons() {
        // 上锁按钮只在"设了密码且当前已解锁"时出现
        var lockBtn = $('diary-btn-lock');
        lockBtn.style.display = (state.settings.has_password && state.unlocked) ? '' : 'none';
    }

    // ----------------------------------------------------------- 锁定横幅
    function renderLockBanner() {
        var banner = $('diary-lock-banner');
        banner.style.display = (state.locked_mode === 'locked') ? 'flex' : 'none';
        // 部分锁定（public+有密码）：隐私条目卡内自带解锁入口，横幅不常驻
    }

    // ----------------------------------------------------------- 人生计数器
    function renderLifeCard() {
        var box = $('diary-life-card');
        if (!state.settings.birth_date) {
            box.innerHTML =
                '<div class="dlc-empty">🎂 还没有设置出生日期' +
                '<button class="m-btn m-btn-sm" id="dlc-set-birth">去设置</button></div>';
            var btn = $('dlc-set-birth');
            if (btn) btn.addEventListener('click', openSettings);
            return;
        }
        tickLife();
    }

    function tickLife() {
        var box = $('diary-life-card');
        if (!state.life_age || !state.settings.birth_date) return;
        // 以接口返回值为基准，按本地时钟插值出当前小时数（小时级精度）
        var elapsed = (Date.now() - state.life_ref_ts) / 3600000;   // 小时
        var totalHours = state.life_age.days * 24 + state.life_age.hours + elapsed;
        var dayNo = Math.floor(totalHours / 24) + 1;   // 出生当天即人生第 1 天
        var minutesIntoDay = Math.floor((totalHours % 24) * 60);   // 本日已过分钟数
        var livedYears = state.life_age.total_days_float
            ? (state.life_age.total_days_float + elapsed / 24) / 365.2425 : 0;
        var expectDays = Math.round(80 * 365.2425);
        var pct = Math.min(100, (state.life_age.days / expectDays) * 100);
        box.innerHTML =
            '<div class="dlc-label">🧬 人生进度</div>' +
            '<div class="dlc-main">人生第 <b>' + dayNo + '</b> 天' +
            '<span class="dlc-hours">本日已过 ' + minutesIntoDay + ' 分钟</span></div>' +
            '<div class="dlc-bar"><span style="width:' + pct.toFixed(2) + '%"></span></div>' +
            '<div class="dlc-sub">已度过 ' + livedYears.toFixed(2) + ' 年 · ' +
            (livedYears / 80 * 100).toFixed(1) + '% 的 80 年人生假设</div>';
    }

    // ----------------------------------------------------------- 目标倒计时
    function renderGoals() {
        var box = $('diary-goals-list');
        var pending = state.goals.filter(function (g) { return !g.done; });
        var done = state.goals.filter(function (g) { return g.done; });
        if (!state.goals.length) {
            box.innerHTML = '<div class="dgc-empty">还没有目标，立一个 🎯（如"掌握Python技能" · 截止 10月5日）</div>';
            return;
        }
        box.innerHTML = pending.map(goalRowHtml).join('') +
            done.map(goalRowHtml).join('');
        Array.prototype.forEach.call(box.querySelectorAll('[data-goal-act]'), function (b) {
            b.addEventListener('click', function () {
                var id = b.getAttribute('data-goal-id');
                var act = b.getAttribute('data-goal-act');
                if (act === 'edit') openGoalEdit(id);
                else if (act === 'del') removeGoal(id);
                else if (act === 'done') finishGoal(id);
            });
        });
    }

    function goalRowHtml(g) {
        var cd = countdownOf(g.due_date);
        var cls = 'dgoal' + (g.done ? ' done' : '') + (cd.overdue ? ' overdue' : '');
        var left = g.done ? '已完成 ✓'
            : (cd.overdue ? '已到期' : '<b>' + cd.days + '</b> 天 ' + cd.hours + ' 小时');
        return '<div class="' + cls + '" data-goal-id="' + escapeHtml(g.id) + '">' +
            '<div class="dgoal-main"><span class="dgoal-title">' + escapeHtml(g.title) + '</span>' +
            '<span class="dgoal-due">📅 ' + escapeHtml(g.due_date) + '</span></div>' +
            '<div class="dgoal-count">' + left + '</div>' +
            '<div class="dgoal-ops">' +
            (g.done ? '' : '<button class="dgoal-op" data-goal-act="done" data-goal-id="' + escapeHtml(g.id) + '" title="标记完成">✓</button>') +
            '<button class="dgoal-op" data-goal-act="edit" data-goal-id="' + escapeHtml(g.id) + '" title="编辑">✎</button>' +
            '<button class="dgoal-op" data-goal-act="del" data-goal-id="' + escapeHtml(g.id) + '" title="删除">🗑</button>' +
            '</div></div>';
    }

    function countdownOf(dueDate) {
        var parts = String(dueDate || '').split('-');
        var deadline = new Date(+parts[0], +parts[1] - 1, +parts[2], 23, 59, 59);
        var ms = deadline.getTime() - Date.now();
        if (ms <= 0) return { days: 0, hours: 0, overdue: true };
        return { days: Math.floor(ms / 86400000), hours: Math.floor((ms % 86400000) / 3600000), overdue: false };
    }

    function tickCountdowns() {
        // 每秒轻量刷新：只重写未完成目标的倒计时文本，不重建 DOM
        Array.prototype.forEach.call(document.querySelectorAll('.dgoal:not(.done)'), function (row) {
            var id = row.getAttribute('data-goal-id');
            var g = null;
            for (var i = 0; i < state.goals.length; i++) { if (state.goals[i].id === id) { g = state.goals[i]; break; } }
            if (!g) return;
            var cd = countdownOf(g.due_date);
            var cell = row.querySelector('.dgoal-count');
            if (cell) cell.innerHTML = cd.overdue ? '已到期' : '<b>' + cd.days + '</b> 天 ' + cd.hours + ' 小时';
        });
        tickLife();
    }

    function goalFormHtml(g) {
        g = g || {};
        return '<div class="diary-form">' +
            '<label>目标标题<input type="text" id="dg-title" maxlength="120" placeholder="如：掌握Python技能" value="' + escapeHtml(g.title || '') + '"></label>' +
            '<label>截止日期<input type="date" id="dg-due" value="' + escapeHtml(g.due_date || '') + '"></label>' +
            '<label>备注（可选）<textarea id="dg-note" rows="2" placeholder="做到什么程度算完成？">' + escapeHtml(g.note || '') + '</textarea></label>' +
            '</div>';
    }

    function openGoalAdd() {
        MDialog.show({
            title: '🎯 立个目标', type: 'primary', message: goalFormHtml(),
            showCancel: true, okText: '立下', width: '460px',
            onOk: function () {
                var title = $('dg-title').value.trim();
                var due = $('dg-due').value;
                if (!title) { toast('目标标题不能为空', 'error'); return false; }
                if (!due) { toast('请选择截止日期', 'error'); return false; }
                postJson('/goal-add', { title: title, due_date: due, note: $('dg-note').value.trim() })
                    .then(function (res) {
                        if (res.code !== 200) { toast(res.message || '失败', 'error'); return false; }
                        toast(res.message); reloadKeepFeed();
                    });
            }
        });
    }

    function findGoal(id) {
        for (var i = 0; i < state.goals.length; i++) { if (state.goals[i].id === id) return state.goals[i]; }
        return null;
    }

    function openGoalEdit(id) {
        var g = findGoal(id);
        if (!g) return;
        MDialog.show({
            title: '✎ 编辑目标', type: 'primary', message: goalFormHtml(g),
            showCancel: true, okText: '保存', width: '460px',
            onOk: function () {
                postJson('/goal-update', {
                    id: id, fields: {
                        title: $('dg-title').value.trim(),
                        due_date: $('dg-due').value,
                        note: $('dg-note').value.trim()
                    }
                }).then(function (res) {
                    if (res.code !== 200) { toast(res.message || '失败', 'error'); return false; }
                    toast(res.message); reloadKeepFeed();
                });
            }
        });
    }

    function finishGoal(id) {
        postJson('/goal-update', { id: id, fields: { done: true } })
            .then(function (res) {
                if (res.code !== 200) { toast(res.message || '失败', 'error'); return; }
                toast('目标达成 🎉'); reloadKeepFeed();
            });
    }

    function removeGoal(id) {
        MDialog.confirm({
            message: '删除这个目标？', type: 'warning', okText: '删除',
            onOk: function () {
                postJson('/goal-delete', { id: id }).then(function (res) {
                    if (res.code !== 200) { toast(res.message || '失败', 'error'); return; }
                    toast(res.message); reloadKeepFeed();
                });
            }
        });
    }

    // =========================================================================
    // 日历
    // =========================================================================
    function loadCalendar() {
        getJson('/calendar?month=' + encodeURIComponent(state.cal_month))
            .then(function (res) {
                if (res.code !== 200) return;
                renderCalendar(res.data.days || []);
            });
    }

    function renderCalendar(days) {
        var title = $('diary-cal-title');
        var parts = state.cal_month.split('-');
        title.textContent = parts[0] + ' 年 ' + parseInt(parts[1], 10) + ' 月';
        var byDate = {};
        days.forEach(function (d) { byDate[d.date] = d; });
        var y = +parts[0], m = +parts[1];
        var first = new Date(y, m - 1, 1);
        var lead = (first.getDay() + 6) % 7;            // 周一为一周起点
        var dim = new Date(y, m, 0).getDate();
        var today = todayStr();
        var html = '';
        for (var i = 0; i < lead; i++) html += '<span class="dcc-cell blank"></span>';
        for (var day = 1; day <= dim; day++) {
            var ds = y + '-' + _pad2(m) + '-' + _pad2(day);
            var info = byDate[ds];
            var cls = 'dcc-cell';
            if (info && info.total > 0) cls += info.private > 0 ? ' has has-private' : ' has';
            if (ds === today) cls += ' today';
            if (ds === state.selected_date) cls += ' selected';
            if (ds > today) cls += ' future';
            html += '<button type="button" class="' + cls + '" data-date="' + ds + '">' +
                day + (info && info.total > 0 ? '<i></i>' : '') + '</button>';
        }
        var grid = $('diary-cal-grid');
        grid.innerHTML = html;
        Array.prototype.forEach.call(grid.querySelectorAll('[data-date]'), function (c) {
            if (c.classList.contains('future')) return;
            c.addEventListener('click', function () { selectDate(c.getAttribute('data-date')); });
        });
    }

    function shiftMonth(delta) {
        var parts = state.cal_month.split('-');
        var d = new Date(+parts[0], +parts[1] - 1 + delta, 1);
        state.cal_month = d.getFullYear() + '-' + _pad2(d.getMonth() + 1);
        loadCalendar();
    }

    function selectDate(ds) {
        if (state.selected_date === ds) { clearDateFocus(); return; }
        state.selected_date = ds;
        getJson('/day?date=' + encodeURIComponent(ds)).then(function (res) {
            if (res.code !== 200) { toast(res.message || '加载失败', 'error'); return; }
            state.day_entries = res.data.entries || [];
            renderFeed();
            renderSideTip();
            renderComposer();
            loadCalendar();
        });
    }

    function clearDateFocus() {
        state.selected_date = '';
        state.day_entries = null;
        renderFeed();
        renderSideTip();
        renderComposer();
        loadCalendar();
    }

    function renderSideTip() {
        var tip = $('diary-side-date');
        var back = $('diary-btn-today');
        if (state.selected_date) {
            tip.textContent = '正在查看：' + dateLabel(state.selected_date);
            back.style.display = '';
        } else {
            tip.textContent = '';
            back.style.display = 'none';
        }
    }

    // =========================================================================
    // 写日记
    // =========================================================================
    function renderComposer() {
        var dateLabelEl = $('diary-composer-date');
        var target = state.selected_date || todayStr();
        dateLabelEl.textContent = dateLabel(target);
        // 心情 chips
        var box = $('diary-mood-chips');
        box.innerHTML = MOODS.map(function (mo) {
            return '<button type="button" class="dyc-mood' + (state.mood === mo ? ' on' : '') +
                '" data-mood="' + mo + '">' + mo + '</button>';
        }).join('');
        Array.prototype.forEach.call(box.querySelectorAll('[data-mood]'), function (b) {
            b.addEventListener('click', function () {
                state.mood = state.mood === b.getAttribute('data-mood') ? '' : b.getAttribute('data-mood');
                renderComposer();
            });
        });
        $('diary-private-toggle').checked = state.private_flag;
        // 关联来源横幅（跨模块跳转携带 ?linked=calorie:2026-09-26）
        var banner = $('diary-link-banner');
        if (state.linked_from) {
            banner.style.display = 'flex';
            $('diary-link-title').textContent = state.linked_from;
        } else {
            banner.style.display = 'none';
        }
    }

    function submitEntry() {
        if (!state.unlocked && state.locked_mode === 'locked') {
            toast('隐私模式下请先解锁', 'error');
            return;
        }
        var content = $('diary-input').value.trim();
        if (!content) { toast('日记内容不能为空', 'error'); return; }
        var body = {
            content: content,
            date: state.selected_date || todayStr(),
            mood: state.mood,
            private: !!state.private_flag,
            linked_from: state.linked_from
        };
        postJson('/add', body).then(function (res) {
            if (res.code !== 200) { toast(res.message || '写入失败', 'error'); return; }
            toast(res.message);
            $('diary-input').value = '';
            state.mood = '';
            state.linked_from = '';
            bootstrap();
        });
    }

    // =========================================================================
    // 时间线
    // =========================================================================
    function renderFeed() {
        var feed = $('diary-feed');
        var list, foot = $('diary-feed-foot');
        if (state.selected_date) {
            // 单日模式：按当天书写顺序正排（接口本就连序返回）
            list = (state.day_entries || []).slice();
            foot.style.display = 'none';
        } else {
            list = state.entries;
            foot.style.display = state.has_more ? '' : 'none';
        }
        if (!list.length) {
            feed.innerHTML = '<div class="diary-empty">📭 ' +
                (state.selected_date ? '这一天还没有日记，写点什么吧' : '还没有日记，从今天开始') + '</div>';
            return;
        }
        // 按天分组
        var groups = [];
        var index = {};
        list.forEach(function (e) {
            var ds = e.date || (e.created_at || '').substring(0, 10);
            if (index[ds] === undefined) { index[ds] = groups.length; groups.push({ date: ds, items: [] }); }
            groups[index[ds]].items.push(e);
        });
        feed.innerHTML = groups.map(function (g) {
            return '<section class="diary-day">' +
                '<div class="diary-day-head"><span>' + dateLabel(g.date) + '</span>' +
                '<small>' + g.date + ' · ' + g.items.length + ' 条</small>' +
                '<a class="diary-day-rate" href="/checkin?date=' + encodeURIComponent(g.date) +
                '" title="查看该日每日评级总控台">📋 查看今日评级</a></div>' +
                g.items.map(entryCardHtml).join('') + '</section>';
        }).join('');
        bindFeedOps(feed);
    }

    function entryCardHtml(e) {
        var locked = !!e.locked;
        if (locked) {
            return '<div class="dentry locked"><span class="dentry-time">--:--</span>' +
                '<div class="dentry-body"><div class="dentry-lock-text">🔒 隐私日记 · 未解锁' +
                (state.locked_mode === 'partial'
                    ? '（本书设了密码，解锁后可见）<button class="dentry-inline-unlock" id="dentry-unlock-here">输入密码</button>'
                    : '') +
                '</div></div></div>';
        }
        var ops =
            '<button class="dentry-op" data-op="edit" data-id="' + escapeHtml(e.id) + '" title="编辑">✎</button>' +
            '<button class="dentry-op" data-op="del" data-id="' + escapeHtml(e.id) + '" title="删除">🗑</button>';
        var link = e.linked_from ? linkedHtml(e.linked_from) : '';
        return '<div class="dentry' + (e.private ? ' is-private' : '') + '" data-id="' + escapeHtml(e.id) + '">' +
            '<span class="dentry-time">' + escapeHtml(timePart(e.created_at)) + '</span>' +
            '<div class="dentry-body">' +
            '<div class="dentry-content">' + (e.mood ? escapeHtml(e.mood) + ' ' : '') +
            escapeHtml(e.content).replace(/\n/g, '<br>') + '</div>' +
            link +
            '</div>' +
            '<div class="dentry-side">' + (e.private ? '<span class="dentry-badge">🔏 隐私</span>' : '') + ops + '</div>' +
            '</div>';
    }

    function linkedHtml(ref) {
        // 跨模块关联：'calorie:日期' / 'journal:xxx' / 'checkin:日期' 跳转到对应模块
        if (ref.indexOf('calorie:') === 0) {
            return '<div class="dentry-link" data-href="/calorie">🔗 关联热量记录 · ' +
                escapeHtml(ref.substring(8)) + ' →</div>';
        }
        if (ref.indexOf('checkin:') === 0) {
            var day = ref.substring(8);
            return '<div class="dentry-link" data-href="/checkin?date=' + encodeURIComponent(day) +
                '">🔗 关联每日评级 · ' + escapeHtml(day) + ' →</div>';
        }
        if (ref.indexOf('journal:') === 0) {
            return '<div class="dentry-link" data-href="/journal">🔗 关联随笔 →</div>';
        }
        return '';
    }

    function bindFeedOps(feed) {
        Array.prototype.forEach.call(feed.querySelectorAll('.dentry-op'), function (b) {
            b.addEventListener('click', function () {
                var id = b.getAttribute('data-id');
                if (b.getAttribute('data-op') === 'edit') openEntryEdit(id);
                else removeEntry(id);
            });
        });
        Array.prototype.forEach.call(feed.querySelectorAll('.dentry-link'), function (a) {
            a.addEventListener('click', function () { window.location.href = a.getAttribute('data-href'); });
        });
        var inlineUnlock = $('dentry-unlock-here');
        if (inlineUnlock) inlineUnlock.addEventListener('click', openUnlockDialog);
    }

    function findEntry(id) {
        var lists = [state.entries, state.day_entries || []];
        for (var k = 0; k < lists.length; k++) {
            for (var i = 0; i < lists[k].length; i++) {
                if (lists[k][i].id === id) return lists[k][i];
            }
        }
        return null;
    }

    function openEntryEdit(id) {
        var e = findEntry(id);
        if (!e) return;
        if (e.locked) { toast('解锁后才能查看和编辑', 'error'); return; }
        MDialog.show({
            title: '✎ 编辑日记', type: 'primary', width: '560px',
            showCancel: true, okText: '保存',
            message: '<div class="diary-form">' +
                '<label>日期<input type="date" id="de-date" value="' + escapeHtml(e.date) + '"></label>' +
                '<label>正文<textarea id="de-content" rows="6">' + escapeHtml(e.content) + '</textarea></label>' +
                '<label class="diary-check"><input type="checkbox" id="de-private"' +
                (e.private ? ' checked' : '') + '> 隐私条目（未解锁不可见）</label>' +
                '</div>',
            onOk: function () {
                var content = $('de-content').value.trim();
                if (!content) { toast('内容不能为空', 'error'); return false; }
                postJson('/update', {
                    id: id, fields: {
                        content: content,
                        date: $('de-date').value,
                        private: $('de-private').checked
                    }
                }).then(function (res) {
                    if (res.code !== 200) { toast(res.message || '失败', 'error'); return false; }
                    toast(res.message); bootstrap();
                });
            }
        });
    }

    function removeEntry(id) {
        MDialog.confirm({
            message: '删除这条日记？删除后不可恢复。', type: 'warning', okText: '删除',
            onOk: function () {
                postJson('/delete', { id: id }).then(function (res) {
                    if (res.code !== 200) { toast(res.message || '失败', 'error'); return; }
                    toast(res.message); bootstrap();
                });
            }
        });
    }

    function loadMore() {
        if (state.loading || !state.has_more || !state.entries.length) return;
        state.loading = true;
        var anchor = state.entries[state.entries.length - 1].id;
        getJson('/bootstrap?limit=' + PAGE_SIZE + '&before=' + encodeURIComponent(anchor))
            .then(function (res) {
                state.loading = false;
                if (res.code !== 200) { toast(res.message || '加载失败', 'error'); return; }
                var more = res.data.entries || [];
                // 同 id 幂等去重（锚点页边界重叠时）
                var seen = {};
                state.entries.forEach(function (e) { seen[e.id] = true; });
                more = more.filter(function (e) { return !seen[e.id]; });
                state.entries = state.entries.concat(more);
                state.has_more = !!res.data.has_more && more.length > 0;
                renderFeed();
            })
            .catch(function () { state.loading = false; });
    }

    // =========================================================================
    // 密码与可见性
    // =========================================================================
    function openUnlockDialog() {
        MDialog.show({
            title: '🔓 输入访问密码', type: 'info', width: '400px', showCancel: true, okText: '解锁',
            message: '<div class="diary-form"><label>密码' +
                '<input type="password" id="du-pwd" autocomplete="current-password" placeholder="日记本访问密码"></label></div>',
            onOk: function () {
                var pwd = $('du-pwd').value;
                if (!pwd) { toast('请输入密码', 'error'); return false; }
                doUnlock(pwd);
            }
        });
        setTimeout(function () { var el = $('du-pwd'); if (el) el.focus(); }, 60);
    }

    function doUnlock(pwd) {
        postJson('/unlock', { password: pwd }).then(function (res) {
            if (res.code !== 200) { toast(res.message || '解锁失败', 'error'); return false; }
            toast(res.message); bootstrap();
        });
    }

    function openPasswordDialog() {
        var has = !!state.settings.has_password;
        MDialog.show({
            title: has ? '🔑 修改访问密码' : '🔑 设置访问密码', type: 'primary',
            width: '420px', showCancel: true, okText: '保存',
            message: '<div class="diary-form">' +
                (has ? '<label>原密码<input type="password" id="dp-old" autocomplete="current-password"></label>' : '') +
                '<label>新密码<input type="password" id="dp-new" placeholder="至少 4 位"></label>' +
                '<label>确认新密码<input type="password" id="dp-re" ></label>' +
                '</div>',
            onOk: function () {
                var np = $('dp-new').value;
                if (np !== $('dp-re').value) { toast('两次输入不一致', 'error'); return false; }
                if (np.length < 4) { toast('密码至少 4 位', 'error'); return false; }
                var body = { password: np };
                if (has) body.old_password = $('dp-old').value;
                postJson('/password-set', body).then(function (res) {
                    if (res.code !== 200) { toast(res.message || '失败', 'error'); return false; }
                    toast(res.message); bootstrap();
                });
            }
        });
    }

    function removePassword() {
        if (!state.settings.has_password) return;
        MDialog.show({
            title: '🗑 清除访问密码', type: 'warning', width: '420px',
            showCancel: true, okText: '清除',
            message: '<div class="diary-form"><p>清除后日记本将转为公开模式，所有隐私条目将对能打开本页面的人可见。</p>' +
                '<label>当前密码<input type="password" id="dr-pwd" autocomplete="current-password"></label></div>',
            onOk: function () {
                postJson('/password-remove', { password: $('dr-pwd').value }).then(function (res) {
                    if (res.code !== 200) { toast(res.message || '失败', 'error'); return false; }
                    toast(res.message); bootstrap();
                });
            }
        });
    }

    function lockNow() {
        postJson('/lock', {}).then(function (res) {
            if (res.code !== 200) { toast(res.message || '失败', 'error'); return; }
            toast(res.message); bootstrap();
        });
    }

    // ----------------------------------------------------------- 设置弹窗
    function openSettings() {
        var s = state.settings;
        var visHtml =
            '<div class="diary-vis-switch">' +
            '<label class="dvs-opt' + (s.visibility === 'public' ? ' on' : '') + '">' +
            '<input type="radio" name="dvs-vis" value="public"' + (s.visibility === 'public' ? ' checked' : '') + '> 🌐 公开可浏览</label>' +
            '<label class="dvs-opt' + (s.visibility === 'private' ? ' on' : '') + '">' +
            '<input type="radio" name="dvs-vis" value="private"' + (s.visibility === 'private' ? ' checked' : '') + '> 🔒 隐私需密码</label>' +
            '</div>';
        MDialog.show({
            title: '⚙ 日记本设置', type: 'primary', width: '520px',
            showCancel: true, okText: '保存设置',
            message: '<div class="diary-form diary-settings">' +
                '<div class="ds-block"><div class="ds-label">可见性</div>' + visHtml +
                '<div class="ds-help">未设置密码时无法开启隐私模式；切换即时生效。</div></div>' +
                '<div class="ds-block"><div class="ds-label">访问密码</div>' +
                '<div class="ds-row">' +
                '<button type="button" class="m-btn m-btn-sm" id="ds-btn-pwd">' +
                (s.has_password ? '🔑 修改密码' : '➕ 设置密码') + '</button>' +
                (s.has_password ? '<button type="button" class="m-btn m-btn-sm m-btn-danger" id="ds-btn-rmpwd">清除密码</button>' : '') +
                '</div><div class="ds-help">密码只保存哈希值（argon2/bcrypt），服务器上也看不到明文；忘记密码无法找回。</div></div>' +
                '<div class="ds-block"><div class="ds-label">出生日期（人生计数器）</div>' +
                '<input type="date" id="ds-birth" value="' + escapeHtml(s.birth_date || '') + '">' +
                '<button type="button" class="m-btn m-btn-sm" id="ds-btn-clear-birth">清除</button></div>' +
                '<div class="ds-block"><div class="ds-label">数据</div><div class="ds-row">' +
                '<button type="button" class="m-btn m-btn-sm" id="ds-btn-export">⬇ 导出 JSON</button>' +
                '<button type="button" class="m-btn m-btn-sm" id="ds-btn-import">⬆ 导入 JSON</button>' +
                '</div><div class="ds-help">共 ' + (state.stats.total_entries || 0) + ' 条日记。导出不含密码；导入按条目 id 幂等去重。</div></div>' +
                '</div>',
            onOk: function () {
                var radios = document.getElementsByName('dvs-vis');
                var vis = 'public';
                for (var i = 0; i < radios.length; i++) { if (radios[i].checked) vis = radios[i].value; }
                var birth = $('ds-birth').value || '';
                return saveSettings(vis, birth);
            }
        });
        var bPwd = $('ds-btn-pwd'); if (bPwd) bPwd.addEventListener('click', openPasswordDialog);
        var bRm = $('ds-btn-rmpwd'); if (bRm) bRm.addEventListener('click', removePassword);
        var bEx = $('ds-btn-export'); if (bEx) bEx.addEventListener('click', exportDiary);
        var bIm = $('ds-btn-import'); if (bIm) bIm.addEventListener('click', importDiary);
        var bCl = $('ds-btn-clear-birth');
        if (bCl) bCl.addEventListener('click', function () { $('ds-birth').value = ''; });
    }

    function saveSettings(vis, birth) {
        // 有密码才允许开隐私：客户端先拦一道，防弹窗被无效提交关掉
        if (vis === 'private' && !state.settings.has_password) {
            toast('请先在上方设置访问密码，再开启隐私模式', 'error');
            return false;
        }
        // 只在有变化时提交对应字段，避免把未解锁的 birth 空值写回
        var body = {};
        if (vis !== state.settings.visibility) body.visibility = vis;
        if ((birth || '') !== (state.settings.birth_date || '')) body.birth_date = birth || '';
        if (!body.visibility && !('birth_date' in body)) return true;
        postJson('/settings', body).then(function (res) {
            if (res.code !== 200) { toast(res.message || '保存失败', 'error'); return; }
            toast(res.message); bootstrap();
        });
        return true;
    }

    // ----------------------------------------------------------- 导出 / 导入
    function exportDiary() {
        window.location.href = API + '/export';
    }

    function importDiary() {
        var input = document.createElement('input');
        input.type = 'file';
        input.accept = 'application/json,.json';
        input.addEventListener('change', function () {
            var f = input.files && input.files[0];
            if (!f) return;
            var reader = new FileReader();
            reader.onload = function () {
                var payload;
                try { payload = JSON.parse(reader.result); }
                catch (e) { toast('文件不是合法 JSON', 'error'); return; }
                MDialog.confirm({
                    message: '将导入 ' + ((payload.entries || []).length) + ' 条日记 / ' +
                        ((payload.goals || []).length) + ' 个目标（同 id 自动跳过）。',
                    type: 'warning', okText: '导入',
                    onOk: function () {
                        postJson('/import', { payload: payload }).then(function (res) {
                            if (res.code !== 200) { toast(res.message || '导入失败', 'error'); return; }
                            toast(res.message); bootstrap();
                        });
                    }
                });
            };
            reader.readAsText(f, 'utf-8');
        });
        input.click();
    }

    // =========================================================================
    // 事件绑定与启动
    // =========================================================================
    function bindStaticEvents() {
        $('diary-btn-settings').addEventListener('click', openSettings);
        $('diary-btn-export').addEventListener('click', exportDiary);
        $('diary-btn-import').addEventListener('click', importDiary);
        $('diary-btn-lock').addEventListener('click', lockNow);
        $('diary-btn-add-goal').addEventListener('click', openGoalAdd);
        $('diary-cal-prev').addEventListener('click', function () { shiftMonth(-1); });
        $('diary-cal-next').addEventListener('click', function () { shiftMonth(1); });
        $('diary-btn-today').addEventListener('click', clearDateFocus);
        $('diary-btn-more').addEventListener('click', loadMore);
        $('diary-submit').addEventListener('click', submitEntry);
        $('diary-private-toggle').addEventListener('change', function () {
            state.private_flag = this.checked;
        });
        $('diary-link-cancel').addEventListener('click', function () {
            state.linked_from = '';
            renderComposer();
        });
        $('diary-input').addEventListener('keydown', function (ev) {
            if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') { ev.preventDefault(); submitEntry(); }
        });
        $('diary-unlock-form').addEventListener('submit', function (ev) {
            ev.preventDefault();
            var pwd = $('diary-unlock-pwd').value;
            if (!pwd) { toast('请输入密码', 'error'); return; }
            $('diary-unlock-pwd').value = '';
            doUnlock(pwd);
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        bindStaticEvents();
        bootstrap();
        setInterval(tickCountdowns, 1000);
    });
})();
