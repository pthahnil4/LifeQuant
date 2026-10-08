/**
 * 随笔与复盘 - 前端交互脚本
 * ==========================
 * 功能：时间线 / 快速记录 / 复盘四格 / 标签管理 / 筛选搜索 /
 *       置顶 / 提炼经验 / 补记关联 / 经验库 / 统计看板 / 导出
 */
(function () {
    'use strict';

    // =========================================================================
    // 全局状态
    // =========================================================================
    var API = '/journal/api';
    var state = {
        notes: [],            // 全部条目（后端原样返回）
        tags: [],             // 标签列表（含 count）
        stats: null,          // 统计看板数据
        loaded: false,
        // 筛选条件（前端本地过滤）
        filterTags: [],       // 选中标签名（多选并集）
        filterType: 'all',    // all / note / review
        filterDays: '0',      // 0 / 7 / 30 / month
        keyword: '',
        // 其他
        activeTab: 'timeline',
        composerTags: [],     // 随手记选中的标签
        linkedFrom: '',       // 补记关联的前因条目 id
        chartInstance: null
    };

    // =========================================================================
    // 工具函数
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

    function dateLabel(dateStr) {
        var today = new Date();
        var fmt = function (d) {
            return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
        };
        if (dateStr === fmt(today)) return '今天';
        var yest = new Date(today.getTime() - 86400000);
        if (dateStr === fmt(yest)) return '昨天';
        return dateStr;
    }

    function timePart(datetimeStr) {
        return (datetimeStr || '').substring(11, 16);
    }

    function findNote(id) {
        for (var i = 0; i < state.notes.length; i++) {
            if (state.notes[i].id === id) return state.notes[i];
        }
        return null;
    }

    function findTag(name) {
        for (var i = 0; i < state.tags.length; i++) {
            if (state.tags[i].name === name) return state.tags[i];
        }
        return null;
    }

    // =========================================================================
    // 数据加载与筛选
    // =========================================================================
    function loadAll(onDone) {
        fetch(API + '/list')
            .then(function (r) { return r.json(); })
            .then(function (res) {
                if (res.code !== 200) { toast('加载失败：' + res.message, 'error'); return; }
                state.notes = res.data.notes || [];
                state.tags = res.data.tags || [];
                state.stats = res.data.stats || null;
                state.loaded = true;
                renderAll();
                if (onDone) onDone();
            })
            .catch(function (e) { toast('网络错误，加载失败', 'error'); console.error(e); });
    }

    function daysFromFilter() {
        if (state.filterDays === 'month') return new Date().getDate();
        return parseInt(state.filterDays, 10) || 0;
    }

    function filteredNotes() {
        var list = state.notes.slice();
        if (state.filterTags.length) {
            list = list.filter(function (n) {
                return state.filterTags.some(function (t) { return (n.tags || []).indexOf(t) >= 0; });
            });
        }
        if (state.filterType !== 'all') {
            list = list.filter(function (n) { return n.type === state.filterType; });
        }
        var days = daysFromFilter();
        if (days > 0) {
            var cutoff = new Date(Date.now() - days * 86400000);
            var cutoffStr = cutoff.getFullYear() + '-' + String(cutoff.getMonth() + 1).padStart(2, '0') + '-' + String(cutoff.getDate()).padStart(2, '0');
            list = list.filter(function (n) { return (n.created_at || '').substring(0, 10) >= cutoffStr; });
        }
        if (state.keyword) {
            var kw = state.keyword.toLowerCase();
            list = list.filter(function (n) {
                if ((n.content || '').toLowerCase().indexOf(kw) >= 0) return true;
                var rv = n.review || {};
                return ['subject', 'decision', 'outcome', 'lesson'].some(function (k) {
                    return String(rv[k] || '').toLowerCase().indexOf(kw) >= 0;
                });
            });
        }
        return list;
    }

    // =========================================================================
    // 局部刷新：本地重算标签计数 / 统计看板，替代整表 loadAll 的网络往返
    // （严格镜像后端 journal_routes._build_stats / _calc_streak_days /
    //   _calc_best_streak_days / _build_calendar，仅依赖 state.notes + state.tags）
    // =========================================================================
    function _pad2(n) { return String(n).padStart(2, '0'); }
    function _ymd(d) { return d.getFullYear() + '-' + _pad2(d.getMonth() + 1) + '-' + _pad2(d.getDate()); }
    function _midnight(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }

    // 依据 state.notes 重算每个标签的引用计数（写回 state.tags[].count）
    function recomputeTagCounts() {
        var counts = {};
        state.notes.forEach(function (n) {
            (n.tags || []).forEach(function (t) { counts[t] = (counts[t] || 0) + 1; });
        });
        state.tags.forEach(function (t) { t.count = counts[t.name] || 0; });
    }

    // 本地重算统计看板（镜像后端 _build_stats）
    function computeStatsLocal() {
        var notes = state.notes;
        var monthPrefix = _ymd(new Date()).substring(0, 7);
        var countByDay = {};
        var review = 0, distilled = 0, month = 0;
        notes.forEach(function (n) {
            if (n.type === 'review') review++;
            if (n.distilled) distilled++;
            var d = (n.created_at || '').substring(0, 10);
            if (d) countByDay[d] = (countByDay[d] || 0) + 1;
            if ((n.created_at || '').substring(0, 7) === monthPrefix) month++;
        });
        // 连续记录天数（镜像 _calc_streak_days：今天无记录则从昨天起算）
        var today = _midnight(new Date());
        var cursor = countByDay[_ymd(today)] ? new Date(today) : new Date(today.getTime() - 86400000);
        var streak = 0;
        while (countByDay[_ymd(cursor)]) { streak++; cursor.setDate(cursor.getDate() - 1); }
        // 历史最佳连续（镜像 _calc_best_streak_days）
        var dayList = Object.keys(countByDay).sort();
        var best = 0, cur = 0, prev = null;
        dayList.forEach(function (ds) {
            var d = new Date(ds + 'T00:00:00');
            if (prev !== null && Math.round((d - prev) / 86400000) === 1) cur++;
            else cur = 1;
            if (cur > best) best = cur;
            prev = d;
        });
        // 近 15 周记录日历（镜像 _build_calendar，周一为一周起点）
        var weeks = 15;
        var monday = new Date(today);
        monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
        var start = new Date(monday);
        start.setDate(start.getDate() - (weeks - 1) * 7);
        var calDays = [];
        for (var i = 0; i < weeks * 7; i++) {
            var dd = new Date(start);
            dd.setDate(dd.getDate() + i);
            var key = _ymd(dd);
            var c = countByDay[key] || 0;
            calDays.push({ date: key, count: c, level: Math.min(c, 4), future: dd > today });
        }
        recomputeTagCounts();
        return {
            total: notes.length,
            review_count: review,
            note_count: notes.length - review,
            distilled_count: distilled,
            month_count: month,
            streak_days: streak,
            best_streak_days: best,
            tag_distribution: state.tags.map(function (t) {
                return { name: t.name, color: t.color || '#999', count: t.count || 0 };
            }),
            calendar: { start_date: _ymd(start), weeks: weeks, days: calDays }
        };
    }

    // 用后端返回的完整 note 就地替换本地副本（找不到则插到最前）
    function upsertNoteLocal(note) {
        if (!note || !note.id) return;
        for (var i = 0; i < state.notes.length; i++) {
            if (state.notes[i].id === note.id) { state.notes[i] = note; return; }
        }
        state.notes.unshift(note);
    }

    // 变更后的局部刷新：本地重算统计 + 重渲染，不再走 loadAll 网络往返；
    // 时间线以 animate=false 重绘，避免整屏卡片重放入场动画造成闪烁。
    function refreshAfterMutation() {
        state.stats = computeStatsLocal();
        renderOverview();
        renderSidebar();
        renderTimeline(false);
        if (state.activeTab === 'wisdom') renderWisdom();
        if (state.activeTab === 'stats') renderStats();
    }

    // =========================================================================
    // 渲染：总览卡
    // =========================================================================
    function renderOverview() {
        var s = state.stats || { total: 0, review_count: 0, distilled_count: 0, streak_days: 0 };
        var items = [
            { icon: '📝', num: s.total, label: '总记录' },
            { icon: '🔄', num: s.review_count, label: '复盘' },
            { icon: '💡', num: s.distilled_count, label: '经验' },
            { icon: '🔥', num: s.streak_days, label: '连续记录(天)' }
        ];
        $('journal-overview').innerHTML = items.map(function (it) {
            return '<div class="jov-item"><span class="jov-icon">' + it.icon + '</span>' +
                '<span class="jov-num">' + it.num + '</span>' +
                '<span class="jov-label">' + it.label + '</span></div>';
        }).join('');
    }

    // =========================================================================
    // 渲染：标签侧边栏 + 随手记标签选择
    // =========================================================================
    function renderSidebar() {
        var total = state.notes.length;
        var html = '<div class="jsb-item jsb-all' + (state.filterTags.length === 0 ? ' active' : '') + '" data-tag="">' +
            '<span class="jsb-dot" style="background:linear-gradient(135deg,#4a90d9,#5cb85c)"></span>' +
            '<span class="jsb-name">全部</span><span class="jsb-count">' + total + '</span></div>';

        state.tags.forEach(function (t) {
            var active = state.filterTags.indexOf(t.name) >= 0 ? ' active' : '';
            html += '<div class="jsb-item' + active + '" data-tag="' + escapeHtml(t.name) + '">' +
                '<span class="jsb-dot" style="background:' + escapeHtml(t.color) + '"></span>' +
                '<span class="jsb-name">' + escapeHtml(t.name) + '</span>' +
                '<span class="jsb-count">' + (t.count || 0) + '</span>' +
                '<span class="jsb-ops">' +
                '<button class="jsb-op jsb-recolor" title="修改颜色">🎨</button>' +
                '<button class="jsb-op jsb-del" title="删除标签">✕</button>' +
                '</span></div>';
        });
        html += '<button class="jsb-add" id="jsb-add-tag">＋ 新标签</button>';
        $('journal-sidebar').innerHTML = html;

        // 标签点击筛选
        Array.prototype.forEach.call($('journal-sidebar').querySelectorAll('.jsb-item'), function (el) {
            el.addEventListener('click', function (e) {
                if (e.target.closest('.jsb-op')) return;
                var tag = el.getAttribute('data-tag');
                if (!tag) { state.filterTags = []; }
                else {
                    var idx = state.filterTags.indexOf(tag);
                    if (idx >= 0) state.filterTags.splice(idx, 1);
                    else state.filterTags.push(tag);
                }
                renderSidebar();
                renderTimeline();
            });
        });
        // 改色 / 删除 / 新增
        Array.prototype.forEach.call($('journal-sidebar').querySelectorAll('.jsb-recolor'), function (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                openRecolorDialog(btn.closest('.jsb-item').getAttribute('data-tag'));
            });
        });
        Array.prototype.forEach.call($('journal-sidebar').querySelectorAll('.jsb-del'), function (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                deleteTag(btn.closest('.jsb-item').getAttribute('data-tag'));
            });
        });
        $('jsb-add-tag').addEventListener('click', openNewTagDialog);
    }

    function renderComposerTags() {
        var html = state.tags.map(function (t) {
            var on = state.composerTags.indexOf(t.name) >= 0;
            var style = on
                ? 'background:' + t.color + ';border-color:' + t.color + ';color:#fff;'
                : 'color:' + t.color + ';border-color:' + t.color + '55;';
            return '<button class="jct-chip' + (on ? ' on' : '') + '" data-tag="' + escapeHtml(t.name) + '" style="' + style + '">' +
                escapeHtml(t.name) + '</button>';
        }).join('');
        $('journal-composer-tags').innerHTML = html;
        Array.prototype.forEach.call($('journal-composer-tags').querySelectorAll('.jct-chip'), function (chip) {
            chip.addEventListener('click', function () {
                var tag = chip.getAttribute('data-tag');
                var idx = state.composerTags.indexOf(tag);
                if (idx >= 0) state.composerTags.splice(idx, 1);
                else state.composerTags.push(tag);
                renderComposerTags();
            });
        });
    }

    // =========================================================================
    // 渲染：时间线
    // =========================================================================
    function buildNoteCard(n, staggerIdx, animate) {
        var isReview = n.type === 'review';
        var cls = 'jnote-card' + (n.pinned ? ' pinned' : '') + (isReview ? ' is-review' : '');
        var delay = Math.min(staggerIdx * 60, 420);
        // animate===false：局部刷新重绘时关闭入场动画，避免整屏卡片重放造成闪烁
        var styleAttr = (animate === false) ? 'animation:none;' : ('animation-delay:' + delay + 'ms;');

        var tagHtml = (n.tags || []).map(function (t) {
            var tag = findTag(t);
            var color = tag ? tag.color : '#999';
            return '<span class="jnote-tag" style="color:' + color + ';background:' + color + '18;">#' + escapeHtml(t) + '</span>';
        }).join('');

        // 复盘四格摘要（只渲染有值的格子）
        var rvHtml = '';
        if (isReview && n.review) {
            var fields = [
                ['🎯 对象', n.review.subject], ['🧭 决策', n.review.decision],
                ['📈 结果', n.review.outcome], ['💡 教训', n.review.lesson]
            ];
            var cells = fields.filter(function (f) { return f[1]; }).map(function (f) {
                return '<div class="jrv-cell"><span class="jrv-label">' + f[0] + '</span>' +
                    '<span class="jrv-value">' + escapeHtml(f[1]) + '</span></div>';
            }).join('');
            if (cells) rvHtml = '<div class="jnote-review">' + cells + '</div>';
        }

        // 关联提示（支持条目间关联与跨模块关联：calorie:YYYY-MM-DD 回指热量记录、
        // diary:YYYY-MM-DD 跳转日记本对应日期）
        var linkHtml = '';
        if (n.linked_from) {
            if (n.linked_from.indexOf('calorie:') === 0) {
                var refDate = n.linked_from.substring(8);
                linkHtml = '<div class="jnote-link" data-href="/calorie" title="跳转到热量管理查看当日记录">🔗 关联热量记录 · ' + escapeHtml(refDate) + ' →</div>';
            } else if (n.linked_from.indexOf('diary:') === 0) {
                var dDate = n.linked_from.substring(6);
                linkHtml = '<div class="jnote-link" data-href="/diary?date=' + encodeURIComponent(dDate) + '" title="跳转到日记本查看当天日记">🔗 关联日记 · ' + escapeHtml(dDate) + ' →</div>';
            } else {
                var src = findNote(n.linked_from);
                var srcText = src ? (src.created_at.substring(0, 10) + ' ' + (src.content || '').substring(0, 20)) : '原条目已删除';
                linkHtml = '<div class="jnote-link" data-goto="' + escapeHtml(n.linked_from) + '">↳ 后续验证 · 前因：' + escapeHtml(srcText) + '…</div>';
            }
        }

        var badges = '';
        if (isReview) badges += '<span class="jnote-badge badge-review">复盘</span>';
        else badges += '<span class="jnote-badge badge-note">随笔</span>';
        if (n.distilled) badges += '<span class="jnote-badge badge-wisdom">💡 经验</span>';

        var ops = '<span class="jnote-ops">' +
            '<button class="jnote-op" data-act="edit" title="编辑">✏️</button>' +
            '<button class="jnote-op" data-act="pin" title="' + (n.pinned ? '取消置顶' : '置顶') + '">📌</button>';
        if (isReview) {
            ops += '<button class="jnote-op" data-act="distill" title="' + (n.distilled ? '移出经验库' : '提炼为经验') + '">💡</button>';
        }
        ops += '<button class="jnote-op" data-act="link" title="补记关联（后续验证）">🔗</button>' +
            '<button class="jnote-op jnote-op-danger" data-act="delete" title="删除">🗑</button></span>';

        return '<div class="' + cls + '" data-id="' + escapeHtml(n.id) + '" style="' + styleAttr + '">' +
            '<div class="jnote-head">' +
            '<span class="jnote-time">' + timePart(n.created_at) + '</span>' + badges +
            (n.pinned ? '<span class="jnote-pin-flag">📌</span>' : '') + ops +
            '</div>' +
            '<div class="jnote-content">' + escapeHtml(n.content) + '</div>' +
            rvHtml +
            '<div class="jnote-foot">' + tagHtml + '</div>' +
            linkHtml +
            '</div>';
    }

    function renderTimeline(animate) {
        var box = $('journal-timeline');
        if (!state.loaded) {
            box.innerHTML = '<div class="jnote-card skeleton"></div>'.repeat(3);
            return;
        }
        var list = filteredNotes();
        if (!list.length) {
            var emptyMsg = state.notes.length
                ? '<div class="journal-empty"><div class="je-icon">🔍</div><div>没有匹配的记录，换个筛选条件试试</div>' +
                  '<button class="m-btn m-btn-secondary m-btn-sm" id="je-clear-filter">清空筛选</button></div>'
                : '<div class="journal-empty"><div class="je-icon">✍️</div><div>还没有记录，写下此刻的想法吧</div>' +
                  '<button class="m-btn m-btn-primary m-btn-sm" id="je-focus-input">开始记录</button></div>';
            box.innerHTML = emptyMsg;
            var clearBtn = $('je-clear-filter');
            if (clearBtn) clearBtn.addEventListener('click', clearFilters);
            var focusBtn = $('je-focus-input');
            if (focusBtn) focusBtn.addEventListener('click', function () { $('journal-input').focus(); });
            return;
        }

        // 置顶在前，其余按时间倒序（后端已倒序）
        var pinned = list.filter(function (n) { return n.pinned; });
        var normal = list.filter(function (n) { return !n.pinned; });

        var idx = 0;
        var html = '';
        if (pinned.length) {
            html += '<div class="jtl-group-title">📌 置顶</div>';
            pinned.forEach(function (n) { html += buildNoteCard(n, idx++, animate); });
        }
        // 按日期分组
        var groups = {};
        var order = [];
        normal.forEach(function (n) {
            var d = (n.created_at || '').substring(0, 10);
            if (!groups[d]) { groups[d] = []; order.push(d); }
            groups[d].push(n);
        });
        order.forEach(function (d) {
            html += '<div class="jtl-group-title">' + dateLabel(d) + '</div>';
            groups[d].forEach(function (n) { html += buildNoteCard(n, idx++, animate); });
        });
        box.innerHTML = html;
        bindNoteCardEvents(box);
    }

    function bindNoteCardEvents(container) {
        Array.prototype.forEach.call(container.querySelectorAll('.jnote-op'), function (btn) {
            btn.addEventListener('click', function () {
                var card = btn.closest('.jnote-card');
                var id = card.getAttribute('data-id');
                var act = btn.getAttribute('data-act');
                if (act === 'edit') openEditDialog(id);
                else if (act === 'pin') togglePin(id);
                else if (act === 'distill') toggleDistill(id);
                else if (act === 'link') startLink(id);
                else if (act === 'delete') deleteNote(id);
            });
        });
        Array.prototype.forEach.call(container.querySelectorAll('.jnote-link'), function (el) {
            el.addEventListener('click', function () {
                var href = el.getAttribute('data-href');
                if (href) { window.location.href = href; return; }  // 跨模块关联跳转（如热量页）
                var target = container.querySelector('.jnote-card[data-id="' + el.getAttribute('data-goto') + '"]');
                if (target) {
                    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    target.classList.add('flash');
                    setTimeout(function () { target.classList.remove('flash'); }, 1000);
                } else toast('前因条目不在当前筛选结果中');
            });
        });
    }

    function clearFilters() {
        state.filterTags = [];
        state.filterType = 'all';
        state.filterDays = '0';
        state.keyword = '';
        $('journal-search').value = '';
        $('journal-days-filter').value = '0';
        Array.prototype.forEach.call($('journal-type-tabs').querySelectorAll('.jtt'), function (b) {
            b.classList.toggle('active', b.getAttribute('data-type') === 'all');
        });
        renderSidebar();
        renderTimeline();
    }

    // =========================================================================
    // 条目操作
    // =========================================================================
    function submitNote() {
        var content = $('journal-input').value.trim();
        if (!content) { toast('先写点什么吧 ✍️', 'error'); $('journal-input').focus(); return; }

        var isReview = $('journal-review-toggle').checked;
        var body = {
            content: content,
            type: isReview ? 'review' : 'note',
            tags: state.composerTags.slice(),
            linked_from: state.linkedFrom || ''
        };
        if (isReview) {
            body.review = {
                subject: $('jrg-subject').value.trim(),
                decision: $('jrg-decision').value.trim(),
                outcome: $('jrg-outcome').value.trim(),
                lesson: $('jrg-lesson').value.trim()
            };
        }

        var btn = $('journal-submit');
        btn.disabled = true;
        fetch(API + '/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
            .then(function (r) { return r.json(); })
            .then(function (res) {
                btn.disabled = false;
                if (res.code !== 200) { toast(res.message, 'error'); return; }
                // 清空输入区并保留焦点，方便连续速记
                $('journal-input').value = '';
                $('journal-input').style.height = 'auto';
                $('jrg-subject').value = '';
                $('jrg-decision').value = '';
                $('jrg-outcome').value = '';
                $('jrg-lesson').value = '';
                cancelLink();
                // 局部刷新：后端已返回完整 note，插入本地并就地重渲染（不再整表 loadAll）
                if (res.data) upsertNoteLocal(res.data);
                refreshAfterMutation();
                toast(res.message);
                $('journal-input').focus();
            })
            .catch(function () { btn.disabled = false; toast('提交失败，请重试', 'error'); });
    }

    function openEditDialog(id) {
        var n = findNote(id);
        if (!n) return;
        var isReview = n.type === 'review';

        var tagChips = state.tags.map(function (t) {
            var on = (n.tags || []).indexOf(t.name) >= 0;
            return '<button type="button" class="jed-chip' + (on ? ' on' : '') + '" data-tag="' + escapeHtml(t.name) + '" ' +
                'style="border-color:' + t.color + (on ? ';background:' + t.color + ';color:#fff;' : ';color:' + t.color + ';') + '">' +
                escapeHtml(t.name) + '</button>';
        }).join('');

        var reviewHtml = '';
        if (isReview) {
            var rv = n.review || {};
            reviewHtml = '<div class="jed-review">' +
                '<label>🎯 复盘对象</label><input type="text" id="jed-subject" value="' + escapeHtml(rv.subject || '') + '">' +
                '<label>🧭 当时怎么做的</label><input type="text" id="jed-decision" value="' + escapeHtml(rv.decision || '') + '">' +
                '<label>📈 实际结果</label><input type="text" id="jed-outcome" value="' + escapeHtml(rv.outcome || '') + '">' +
                '<label>💡 下次怎么办</label><input type="text" id="jed-lesson" value="' + escapeHtml(rv.lesson || '') + '">' +
                '</div>';
        }

        var html = '<div class="journal-edit-dialog">' +
            '<textarea id="jed-content" rows="4">' + escapeHtml(n.content) + '</textarea>' +
            '<div class="jed-tags">' + tagChips + '</div>' +
            reviewHtml + '</div>';

        var overlay = MDialog.show({
            title: isReview ? '编辑复盘' : '编辑随笔',
            type: 'primary',
            message: html,
            width: '560px',
            showCancel: true,
            okText: '保存',
            onOk: function () {
                var content = $('jed-content').value.trim();
                if (!content) { toast('内容不能为空', 'error'); return false; }
                var tags = [];
                Array.prototype.forEach.call(overlay.querySelectorAll('.jed-chip.on'), function (c) {
                    tags.push(c.getAttribute('data-tag'));
                });
                var fields = { content: content, tags: tags };
                if (isReview) {
                    fields.review = {
                        subject: $('jed-subject').value.trim(),
                        decision: $('jed-decision').value.trim(),
                        outcome: $('jed-outcome').value.trim(),
                        lesson: $('jed-lesson').value.trim()
                    };
                }
                fetch(API + '/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: id, fields: fields })
                })
                    .then(function (r) { return r.json(); })
                    .then(function (res) {
                        if (res.code !== 200) { toast(res.message, 'error'); return; }
                        toast('更新成功 ✅');
                        // 局部刷新：用返回的完整 note 就地替换本地副本
                        if (res.data) upsertNoteLocal(res.data);
                        refreshAfterMutation();
                    });
            }
        });

        // 标签 chips 切换
        Array.prototype.forEach.call(overlay.querySelectorAll('.jed-chip'), function (chip) {
            chip.addEventListener('click', function () { chip.classList.toggle('on'); });
        });
    }

    function deleteNote(id) {
        MDialog.danger('确定删除这条记录吗？此操作不可撤销。', function () {
            fetch(API + '/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: id })
            })
                .then(function (r) { return r.json(); })
                .then(function (res) {
                    if (res.code !== 200) { toast(res.message, 'error'); return; }
                    // 滑出动画后从本地移除并局部刷新（不再整表 loadAll）
                    var card = document.querySelector('.jnote-card[data-id="' + id + '"]');
                    var applyRemove = function () {
                        state.notes = state.notes.filter(function (n) { return n.id !== id; });
                        // 镜像后端 delete_note：清理其它条目对被删条目的 linked_from 引用
                        state.notes.forEach(function (n) { if (n.linked_from === id) n.linked_from = ''; });
                        refreshAfterMutation();
                    };
                    if (card) { card.classList.add('removing'); setTimeout(applyRemove, 280); }
                    else applyRemove();
                });
        });
    }

    function togglePin(id) {
        fetch(API + '/pin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id })
        })
            .then(function (r) { return r.json(); })
            .then(function (res) {
                if (res.code !== 200) { toast(res.message, 'error'); return; }
                toast(res.message);
                // 局部刷新：置顶改变排序，本地替换后重绘（置顶区/日期区重新分组）
                if (res.data) upsertNoteLocal(res.data);
                refreshAfterMutation();
            });
    }

    function toggleDistill(id) {
        fetch(API + '/distill', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id })
        })
            .then(function (r) { return r.json(); })
            .then(function (res) {
                if (res.code !== 200) { toast(res.message, 'error'); return; }
                toast(res.message);
                // 局部刷新：本地替换后重绘（经验库 tab 激活时一并刷新）
                if (res.data) upsertNoteLocal(res.data);
                refreshAfterMutation();
            });
    }

    // =========================================================================
    // 补记关联（后续验证）
    // =========================================================================
    function startLink(id) {
        var n = findNote(id);
        if (!n) return;
        state.linkedFrom = id;
        $('journal-link-title').textContent = (n.created_at.substring(0, 16)) + ' ' + (n.content || '').substring(0, 24) + '…';
        $('journal-link-banner').style.display = 'flex';
        $('journal-input').placeholder = '记录后续验证：后来怎么样了？';
        $('journal-input').focus();
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function cancelLink() {
        state.linkedFrom = '';
        $('journal-link-banner').style.display = 'none';
        $('journal-input').placeholder = '此刻的想法、心得、经验…写下来再说 ✍️';
    }

    // =========================================================================
    // 标签管理
    // =========================================================================
    function openNewTagDialog() {
        MDialog.prompt({
            title: '新建标签',
            message: '<p>标签名（不超过 8 个字）：</p>',
            placeholder: '如：灵感、读书、风控…',
            okText: '创建'
        }, function (val) {
            val = (val || '').trim();
            if (!val) { toast('标签名不能为空', 'error'); return false; }
            fetch(API + '/tag-save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: val })
            })
                .then(function (r) { return r.json(); })
                .then(function (res) {
                    if (res.code !== 200) { toast(res.message, 'error'); return; }
                    toast('标签已创建 🏷️');
                    // 局部刷新：把新标签并入本地标签表（计数由 refreshAfterMutation 重算）
                    if (res.data && res.data.name && !findTag(res.data.name)) {
                        state.tags.push({ name: res.data.name, color: res.data.color || '#999', count: 0 });
                    }
                    refreshAfterMutation();
                });
        });
    }

    var PALETTE = ['#4a90d9', '#7e57c2', '#e67e22', '#e74c3c', '#27ae60', '#95a5a6',
        '#16a085', '#d81b60', '#5c6bc0', '#f39c12', '#00897b', '#8d6e63'];

    function openRecolorDialog(tagName) {
        var swatches = PALETTE.map(function (c) {
            return '<span class="jcs-swatch" data-color="' + c + '" style="background:' + c + '"></span>';
        }).join('');
        var overlay = MDialog.show({
            title: '修改「' + tagName + '」颜色',
            type: 'info',
            message: '<div class="journal-color-grid">' + swatches + '</div>',
            showCancel: false,
            showClose: true,
            okText: '关闭',
            width: '360px'
        });
        Array.prototype.forEach.call(overlay.querySelectorAll('.jcs-swatch'), function (sw) {
            sw.addEventListener('click', function () {
                fetch(API + '/tag-save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: tagName, color: sw.getAttribute('data-color') })
                })
                    .then(function (r) { return r.json(); })
                    .then(function (res) {
                        if (res.code !== 200) { toast(res.message, 'error'); return; }
                        toast('标签颜色已更新 🎨');
                        MDialog.close(overlay);
                        // 局部刷新：就地改本地标签颜色后重渲染
                        var t = findTag(tagName);
                        if (t && res.data && res.data.color) t.color = res.data.color;
                        refreshAfterMutation();
                    });
            });
        });
    }

    function deleteTag(name) {
        MDialog.confirm('删除标签「' + name + '」？条目会保留，仅移除该标签。', function () {
            fetch(API + '/tag-delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name })
            })
                .then(function (r) { return r.json(); })
                .then(function (res) {
                    if (res.code !== 200) { toast(res.message, 'error'); return; }
                    var idx = state.filterTags.indexOf(name);
                    if (idx >= 0) state.filterTags.splice(idx, 1);
                    toast('标签已删除');
                    // 局部刷新：移除本地标签 + 镜像后端级联摘除各条目对该标签的引用
                    state.tags = state.tags.filter(function (t) { return t.name !== name; });
                    state.notes.forEach(function (n) {
                        if ((n.tags || []).indexOf(name) >= 0) {
                            n.tags = n.tags.filter(function (x) { return x !== name; });
                        }
                    });
                    refreshAfterMutation();
                });
        });
    }

    // =========================================================================
    // 导出
    // =========================================================================
    function exportNotes() {
        MDialog.show({
            title: '导出记录',
            type: 'info',
            message: '<p>将导出当前筛选条件下的 <b>' + filteredNotes().length + '</b> 条记录，选择格式：</p>',
            buttons: [
                {
                    text: '📄 Markdown', type: 'primary',
                    onClick: function () { doExport('md'); }
                },
                {
                    text: '🗃️ JSON', type: 'secondary',
                    onClick: function () { doExport('json'); }
                },
                { text: '取消', type: 'cancel' }
            ]
        });
    }

    function doExport(fmt) {
        var params = ['format=' + fmt];
        if (state.filterTags.length === 1) params.push('tag=' + encodeURIComponent(state.filterTags[0]));
        if (state.filterType !== 'all') params.push('type=' + state.filterType);
        if (state.keyword) params.push('keyword=' + encodeURIComponent(state.keyword));
        var days = daysFromFilter();
        if (days > 0) params.push('days=' + days);
        window.location.href = API + '/export?' + params.join('&');
    }

    // =========================================================================
    // 渲染：经验库
    // =========================================================================
    function renderWisdom() {
        var box = $('journal-wisdom-grid');
        var list = state.notes.filter(function (n) { return n.distilled && n.type === 'review'; });
        if (!list.length) {
            box.innerHTML = '<div class="journal-empty"><div class="je-icon">💡</div>' +
                '<div>还没有提炼过经验——去复盘一条记录，把教训变成财富</div></div>';
            return;
        }
        var idx = 0;
        box.innerHTML = list.map(function (n) {
            var lesson = (n.review && n.review.lesson) || n.content;
            var tag = findTag((n.tags || [])[0]);
            var color = tag ? tag.color : '#999';
            var delay = Math.min((idx++) * 60, 420);
            return '<div class="jws-card" data-id="' + escapeHtml(n.id) + '" style="animation-delay:' + delay + 'ms;">' +
                '<div class="jws-bar" style="background:' + color + '"></div>' +
                '<div class="jws-lesson">' + escapeHtml(lesson) + '</div>' +
                '<div class="jws-meta">' +
                '<span>' + (n.created_at || '').substring(0, 10) + '</span>' +
                (n.review && n.review.subject ? '<span>🎯 ' + escapeHtml(n.review.subject) + '</span>' : '') +
                '<button class="jws-link-btn" data-id="' + escapeHtml(n.id) + '">查看原复盘 →</button>' +
                '</div></div>';
        }).join('');
        Array.prototype.forEach.call(box.querySelectorAll('.jws-link-btn'), function (btn) {
            btn.addEventListener('click', function () { openEditDialog(btn.getAttribute('data-id')); });
        });
    }

    // =========================================================================
    // 渲染：统计看板
    // =========================================================================
    function renderStats() {
        var s = state.stats;
        if (!s) return;
        var cards = [
            { icon: '📝', num: s.total, label: '总记录', bg: 'linear-gradient(135deg,#eaf4fd,#f2f9ff)' },
            { icon: '🔄', num: s.review_count, label: '复盘数', bg: 'linear-gradient(135deg,#fff4e8,#fffaf2)' },
            { icon: '💡', num: s.distilled_count, label: '经验数', bg: 'linear-gradient(135deg,#e8f7ee,#f2fcf6)' },
            { icon: '🔥', num: s.streak_days, label: '连续记录(天)', bg: 'linear-gradient(135deg,#fdeeee,#fff7f7)', extra: '历史最佳 ' + s.best_streak_days + ' 天' },
            { icon: '📅', num: s.month_count, label: '本月记录', bg: 'linear-gradient(135deg,#f0edfd,#f8f7ff)' }
        ];
        $('journal-stats-cards').innerHTML = cards.map(function (c) {
            return '<div class="jsc-card" style="background:' + c.bg + ';">' +
                '<span class="jsc-icon">' + c.icon + '</span>' +
                '<div><div class="jsc-num">' + c.num + '</div>' +
                '<div class="jsc-label">' + c.label + '</div>' +
                (c.extra ? '<div class="jsc-extra">' + c.extra + '</div>' : '') +
                '</div></div>';
        }).join('');

        renderTagChart(s.tag_distribution);
        renderCalendar(s.calendar);
    }

    function renderTagChart(dist) {
        var el = $('journal-tag-chart');
        var data = dist.filter(function (d) { return d.count > 0; });
        if (!data.length) {
            el.innerHTML = '<div class="journal-chart-empty">暂无数据</div>';
            return;
        }
        if (!state.chartInstance) state.chartInstance = echarts.init(el);
        state.chartInstance.setOption({
            tooltip: { trigger: 'item', formatter: '{b}：{c} 条（{d}%）' },
            legend: { bottom: 0, textStyle: { fontSize: 11, color: '#777' } },
            series: [{
                type: 'pie',
                radius: ['42%', '68%'],
                center: ['50%', '44%'],
                itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
                label: { show: false },
                data: data.map(function (d) {
                    return { name: d.name, value: d.count, itemStyle: { color: d.color } };
                })
            }]
        });
    }

    function renderCalendar(cal) {
        var box = $('journal-calendar');
        if (!cal) { box.innerHTML = ''; return; }
        var html = '<div class="jcal-grid">';
        // 周标签行
        html += '<div class="jcal-week-labels">' +
            ['一', '二', '三', '四', '五', '六', '日'].map(function (w) {
                return '<span>周' + w + '</span>';
            }).join('') + '</div>';
        // 按周分列（GitHub 贡献图为横向周列，此处用行式更易读：每周一行）
        for (var w = 0; w < cal.weeks; w++) {
            html += '<div class="jcal-week">';
            for (var d = 0; d < 7; d++) {
                var day = cal.days[w * 7 + d];
                var cls = 'jcal-cell lv' + day.level + (day.future ? ' future' : '');
                html += '<span class="' + cls + '" data-date="' + day.date + '" data-count="' + day.count + '"></span>';
            }
            html += '</div>';
        }
        html += '</div><div class="jcal-legend">' +
            '<span class="jcal-legend-label">少</span>' +
            '<span class="jcal-cell lv0"></span><span class="jcal-cell lv1"></span>' +
            '<span class="jcal-cell lv2"></span><span class="jcal-cell lv3"></span><span class="jcal-cell lv4"></span>' +
            '<span class="jcal-legend-label">多</span></div>';
        box.innerHTML = html;

        // 悬停 tooltip
        Array.prototype.forEach.call(box.querySelectorAll('.jcal-cell[data-date]'), function (cell) {
            cell.addEventListener('mouseenter', function () {
                cell.setAttribute('title', cell.getAttribute('data-date') + ' · ' + cell.getAttribute('data-count') + ' 条');
            });
        });
    }

    // =========================================================================
    // Tab 切换
    // =========================================================================
    function switchTab(tab) {
        state.activeTab = tab;
        Array.prototype.forEach.call($('journal-tabs').querySelectorAll('.journal-tab'), function (b) {
            b.classList.toggle('active', b.getAttribute('data-tab') === tab);
        });
        $('pane-timeline').style.display = tab === 'timeline' ? '' : 'none';
        $('pane-wisdom').style.display = tab === 'wisdom' ? '' : 'none';
        $('pane-stats').style.display = tab === 'stats' ? '' : 'none';
        moveTabIndicator();
        if (tab === 'wisdom') renderWisdom();
        if (tab === 'stats') renderStats();
    }

    function moveTabIndicator() {
        var active = $('journal-tabs').querySelector('.journal-tab.active');
        var indicator = $('journal-tab-indicator');
        if (active && indicator) {
            indicator.style.width = active.offsetWidth + 'px';
            indicator.style.transform = 'translateX(' + active.offsetLeft + 'px)';
        }
    }

    // =========================================================================
    // 总渲染入口
    // =========================================================================
    function renderAll() {
        renderOverview();
        renderSidebar();
        renderComposerTags();
        renderTimeline(true);
        if (state.activeTab === 'wisdom') renderWisdom();
        if (state.activeTab === 'stats') renderStats();
    }

    // =========================================================================
    // 事件绑定与初始化
    // =========================================================================
    function bindEvents() {
        // 顶部 tab
        Array.prototype.forEach.call($('journal-tabs').querySelectorAll('.journal-tab'), function (b) {
            b.addEventListener('click', function () { switchTab(b.getAttribute('data-tab')); });
        });

        // 输入框自动增高
        var input = $('journal-input');
        input.addEventListener('input', function () {
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 180) + 'px';
        });
        input.addEventListener('focus', function () { $('journal-composer').classList.add('focused'); });
        input.addEventListener('blur', function () { $('journal-composer').classList.remove('focused'); });
        input.addEventListener('keydown', function (e) {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); submitNote(); }
        });

        // 复盘模式开关（平滑展开四格）
        $('journal-review-toggle').addEventListener('change', function () {
            $('journal-review-grid').classList.toggle('open', this.checked);
            if (this.checked) input.placeholder = '复盘叙述（可留白，重点填下面四格）…';
            else input.placeholder = '此刻的想法、心得、经验…写下来再说 ✍️';
        });

        // 提交
        $('journal-submit').addEventListener('click', submitNote);

        // 关联取消
        $('journal-link-cancel').addEventListener('click', cancelLink);

        // 类型 tab
        Array.prototype.forEach.call($('journal-type-tabs').querySelectorAll('.jtt'), function (b) {
            b.addEventListener('click', function () {
                state.filterType = b.getAttribute('data-type');
                Array.prototype.forEach.call($('journal-type-tabs').querySelectorAll('.jtt'), function (x) {
                    x.classList.toggle('active', x === b);
                });
                renderTimeline();
            });
        });

        // 时间筛选
        $('journal-days-filter').addEventListener('change', function () {
            state.filterDays = this.value;
            renderTimeline();
        });

        // 搜索（防抖）
        var searchTimer = null;
        $('journal-search').addEventListener('input', function () {
            var val = this.value;
            clearTimeout(searchTimer);
            searchTimer = setTimeout(function () {
                state.keyword = val.trim();
                renderTimeline();
            }, 250);
        });

        // 导出
        $('journal-export').addEventListener('click', exportNotes);

        // 窗口缩放：tab 指示条 + 图表自适应
        window.addEventListener('resize', function () {
            moveTabIndicator();
            if (state.chartInstance) state.chartInstance.resize();
        });
    }

    function init() {
        bindEvents();
        applyDeepLink();      // 其他模块跳转过来的预填关联（如热量页）
        renderTimeline(); // 先展示骨架屏
        loadAll(function () {
            moveTabIndicator();
            if (state.linkedFrom) $('journal-input').focus();
        });
    }

    /**
     * 跨模块深链：/journal?link=calorie:YYYY-MM-DD
     * 从热量页跳转过来时，预填关联来源（保存时写入 linked_from）、
     * 默认选中『减肥随笔』标签，实现两个模块数据互相引用。
     */
    function applyDeepLink() {
        var m = (location.search.match(/[?&]link=([^&]+)/) || [])[1];
        var link = m ? decodeURIComponent(m) : '';
        if (!/^calorie:\d{4}-\d{2}-\d{2}$/.test(link)) return;
        state.linkedFrom = link;
        if (state.composerTags.indexOf('减肥随笔') < 0) state.composerTags.push('减肥随笔');
        $('journal-link-title').textContent = '热量记录 ' + link.substring(8) + '（补记当日减肥心得）';
        $('journal-link-banner').style.display = 'flex';
        $('journal-input').placeholder = '记录这一天的减肥感悟、心情变化与心理历程……';
    }

    document.addEventListener('DOMContentLoaded', init);
})();
