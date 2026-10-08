/* ================================================================
 * 导航自定义管理 —— 前端核心（Store / 渲染 / 折叠 / 拖拽 / 保存 / 同步）
 * ----------------------------------------------------------------
 * 单一数据源：window.CryptoNav。顶部导航、"更多"、移动端抽屉、管理页编辑器
 * 全部经它读写同一份配置，绝不各持一套顺序。
 *
 * 首屏策略：HTML 只渲染内存默认（服务端已给，见 nav.html）→ 应用本机已确认
 * 快照 → 异步 GET 权威配置校准。GET 不写库；只有显式保存/拖拽/恢复才写。
 *
 * 排序即意图：主栏与"更多"是同一顶层序列的两个投影视图，缩放/字体变化只触发
 * 重新布局，不发写请求；隐藏/删除是持久化状态，不会被默认或校准偷偷撤销。
 * ================================================================ */
(function () {
    'use strict';

    var SNAPSHOT_KEY = 'cryptonav:snapshot:v1';
    var BROADCAST = 'cryptonav:change';
    var CALIBRATE_MS = 30000;
    var BOOT_TIMEOUT = 8000;
    var NAV_HDR = { 'Content-Type': 'application/json', 'X-Nav-Request': '1' };

    // 内置图标键 → 展示字符（纯文本，渲染用 textContent，绝不注入 HTML）
    var ICONS = {
        dashboard: '📊', trend: '🚀', star: '⭐', analysis: '📝', task: '⏰',
        alert: '🚨', instinct: '🧠', plan: '📋', journal: '✍️', calorie: '🔥',
        okx: '📖', api: '🔌', system: '🖥️', nav: '🧭', coin: '🪙', guide: '📚', wiki: '📚',
        expense: '💳', diary: '📔'
    };

    var state = {
        confirmed: null,        // 权威/已确认配置（含 items/settings/revision）
        calibrated: false,      // 是否已完成首次权威校准
        editable: true,
        corrupt: false,
        editing: false,         // 导航栏拖拽编辑态
        saveStatus: 'idle',     // idle|saving|pending|error
        drawerOpen: false,
        units: [],              // 当前渲染的顶层单元（含 children）
        catalog: null
    };

    var els = {};
    var listeners = {};
    var lastSnapshotRev = -1;

    // ----------------------------------------------------------- 小工具
    function emit(name, detail) {
        (listeners[name] || []).forEach(function (cb) { try { cb(detail); } catch (e) {} });
        try { document.dispatchEvent(new CustomEvent('nav:' + name, { detail: detail })); } catch (e) {}
    }
    function on(name, cb) { (listeners[name] = listeners[name] || []).push(cb); }

    function iconChar(icon) {
        if (!icon) return '';
        if (icon.type === 'builtin') return ICONS[icon.value] || '';
        if (icon.type === 'text') return icon.value || '';
        return '';
    }
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }
    function isExternal(url) { return !!url && /^https?:\/\//i.test(url); }
    function isBlank(node) { return node.target === '_blank' || isExternal(node.url); }
    function samePath(nodePath) {
        var cur = location.pathname.replace(/\/+$/, '') || '/';
        var np = (nodePath || '/').split('?')[0].replace(/\/+$/, '') || '/';
        return np === cur;
    }

    function cloneCfg(c) { return JSON.parse(JSON.stringify(c)); }

    // ----------------------------------------------------------- DOM 骨架获取
    function grabDom() {
        els.nav = document.querySelector('.main-nav');
        els.inner = document.querySelector('.nav-inner');
        els.links = document.getElementById('navLinks');
        els.more = document.getElementById('navMore');
        els.moreBtn = document.getElementById('navMoreBtn');
        els.moreMenu = document.getElementById('navMoreMenu');
        els.overlay = document.getElementById('navOverlay');
        els.drawer = document.getElementById('navDrawer');
        els.editBtn = document.getElementById('navEditToggle');
        els.menuBtn = document.getElementById('navMenuBtn');
        els.saveDotHost = els.editBtn;
        if (!els.links || !els.nav) return false;
        return true;
    }

    // ----------------------------------------------------------- 构建单元
    // items → 顶层单元数组：link / group(含可见 children)
    function buildUnits(cfg) {
        var items = (cfg && cfg.items) || [];
        var byId = {};
        items.forEach(function (n) { byId[n.id] = n; });
        var top = items.filter(function (n) { return n.parent_id == null; })
            .sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
        var units = [];
        top.forEach(function (n) {
            if (!n.visible) return;                     // 隐藏的顶层不展示
            if (n.type === 'link') {
                units.push({ kind: 'link', node: n, id: n.id });
            } else {
                var kids = items.filter(function (c) {
                    return c.parent_id === n.id && c.visible;
                }).sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
                if (kids.length) units.push({ kind: 'group', node: n, id: n.id, children: kids });
            }
        });
        return units;
    }

    function activeIds(cfg) {
        // 以真实 URL 命中当前页；返回 {linkId, parentId}
        var items = (cfg && cfg.items) || [];
        var links = items.filter(function (n) { return n.type === 'link' && n.url; })
            .sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
        for (var i = 0; i < links.length; i++) {
            var p = links[i].url.split('#')[0];
            if (isExternal(links[i].url)) { try { p = new URL(links[i].url).pathname; } catch (e) { p = '/'; } }
            if (samePath(p)) return { linkId: links[i].id, parentId: links[i].parent_id || null };
        }
        return { linkId: null, parentId: null };
    }

    // ----------------------------------------------------------- 链接 DOM
    function makeLinkEl(node, active, opts) {
        opts = opts || {};
        var a = el('a', 'nav-link');
        a.href = node.url || '#';
        a.setAttribute('data-nav-id', node.id);
        if (node.builtin_key === 'analysis' || (node.url || '').split('?')[0].replace(/\/+$/, '') === '/analysis') {
            a.setAttribute('data-nav-role', 'analysis');
        }
        var ic = iconChar(node.icon);
        var label = (ic ? ic + ' ' : '') + (node.name || '');
        a.textContent = label;
        if (isBlank(node)) {
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.appendChild(el('span', 'nav-ext-mark', '↗'));
        }
        if (active) a.classList.add('active');
        if (opts.draggable) {
            a.draggable = true;
            a.title = '拖拽可排序（编辑态）';
        }
        return a;
    }

    function makeHandle() { return el('span', 'nav-drag-handle', '⠿'); }

    function makeGroupEl(unit, activeChildId, opts) {
        opts = opts || {};
        var wrap = el('span', 'nav-group');
        wrap.setAttribute('data-nav-id', unit.id);
        var btn = el('button', 'nav-group-btn');
        btn.type = 'button';
        var ic = iconChar(unit.node.icon);
        btn.textContent = (ic ? ic + ' ' : '') + (unit.node.name || '');
        btn.appendChild(el('span', 'nav-caret', '▾'));
        if (activeChildId) wrap.classList.add('contains-active');
        if (opts.draggable) {
            wrap.draggable = true;
            btn.title = '拖拽可排序（编辑态）';
        }
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (state.editing) return;                    // 编辑态点按钮不误弹
            toggleGroupPopup(wrap, unit);
        });
        wrap.appendChild(btn);
        return wrap;
    }

    // ----------------------------------------------------------- 渲染主栏 + 更多
    function render() {
        if (!state.confirmed) return;
        var cfg = state.confirmed;
        state.units = buildUnits(cfg);
        var act = activeIds(cfg);
        state.activeId = act.linkId;
        state.activeParent = act.parentId;

        // 先全部铺到主栏测量
        els.links.innerHTML = '';
        els.moreMenu.innerHTML = '';
        if (els.more) els.more.hidden = true;

        var frag = document.createDocumentFragment();
        state.units.forEach(function (unit) {
            var nodeEl = renderUnit(unit, act);
            frag.appendChild(nodeEl);
            unit._el = nodeEl;
        });
        els.links.appendChild(frag);

        layout(act);
        emit('rendered', { activeId: act.linkId, config: cfg });
    }

    function renderUnit(unit, act) {
        if (unit.kind === 'link') {
            var a = makeLinkEl(unit.node, act.linkId === unit.id, { draggable: state.editing });
            if (state.editing) wireDrag(a, unit, null);
            return a;
        }
        var g = makeGroupEl(unit, act.linkId && act.parentId === unit.id ? act.linkId : null, { draggable: state.editing });
        g._unit = unit;
        if (state.editing) wireDrag(g, unit, null);
        return g;
    }

    // 依实测宽度 + desktop_max_visible 计算可见前缀，其余进"更多"
    var rafLayout = null;
    function scheduleLayout() {
        if (rafLayout) return;
        rafLayout = requestAnimationFrame(function () { rafLayout = null; layout(activeIds(state.confirmed)); });
    }
    function layout(act) {
        act = act || { linkId: state.activeId, parentId: state.activeParent };
        if (window.innerWidth < 768) { buildDrawer(); return; }
        if (state.editing) return;                        // 编辑态冻结折叠，避免拖动中跳位

        var units = state.units;
        var maxVis = (state.confirmed.settings && state.confirmed.settings.desktop_max_visible) || 6;

        // 复位到主栏
        units.forEach(function (u) { if (u._el && u._el.parentNode !== els.links) els.links.appendChild(u._el); });
        if (els.more) els.more.hidden = true;
        var avail = els.links.getBoundingClientRect().width;
        var gap = 2;
        function totalWidth() {
            var w = 0, c = els.links.children.length;
            for (var i = 0; i < c; i++) w += els.links.children[i].getBoundingClientRect().width;
            return w + gap * Math.max(0, c - 1);
        }
        if (units.length <= maxVis && totalWidth() <= avail + 0.5) return;

        // 需要"更多"：预留其按钮宽度
        if (els.more) els.more.hidden = false;
        var moreW = els.moreBtn ? els.moreBtn.getBoundingClientRect().width + gap : 90;
        var linkAvail = avail - moreW;

        // 逐个累加，取「同时满足数量与宽度」的最长前缀（不跳过放不下的项去显示后面的）
        var used = 0, keep = 0;
        for (var i = 0; i < units.length; i++) {
            var need = units[i]._el.getBoundingClientRect().width + (keep > 0 ? gap : 0);
            if (keep >= maxVis || used + need > linkAvail + 0.5) break;
            used += need; keep++;
        }
        // 溢出项移入更多
        els.moreMenu.innerHTML = '';
        units.slice(keep).forEach(function (u) {
            els.moreMenu.appendChild(unitToMore(u, act));
        });
        var hasActiveInMore = units.slice(keep).some(function (u) {
            return u.id === act.linkId || u.id === act.parentId ||
                (u.children || []).some(function (c) { return c.id === act.linkId; });
        });
        if (els.more) els.more.classList.toggle('has-active', hasActiveInMore);
    }

    function unitToMore(unit, act) {
        if (unit.kind === 'link') {
            var a = makeLinkEl(unit.node, act.linkId === unit.id, {});
            a.classList.add('nav-popup-item');
            return a;
        }
        var frag = document.createDocumentFragment();
        frag.appendChild(el('div', 'nav-more-section-label', (iconChar(unit.node.icon) ? iconChar(unit.node.icon) + ' ' : '') + (unit.node.name || '')));
        (unit.children || []).forEach(function (c) {
            var a = makeLinkEl(c, act.linkId === c.id, {});
            a.classList.add('nav-popup-item');
            frag.appendChild(a);
        });
        var box = el('div');
        box.appendChild(frag);
        return box;
    }

    // ----------------------------------------------------------- 分类浮层菜单
    var openPopup = null;
    function closePopup() {
        if (openPopup && openPopup.el.parentNode) openPopup.el.parentNode.removeChild(openPopup.el);
        if (openPopup && openPopup.wrap) openPopup.wrap.classList.remove('open');
        openPopup = null;
    }
    function toggleGroupPopup(wrap, unit) {
        if (openPopup && openPopup.wrap === wrap) { closePopup(); return; }
        closePopup();
        var pop = el('div', 'nav-popup');
        var act = activeIds(state.confirmed);
        (unit.children || []).forEach(function (c) {
            var a = makeLinkEl(c, act.linkId === c.id, {});
            a.classList.add('nav-popup-item');
            pop.appendChild(a);
        });
        els.overlay.appendChild(pop);
        var r = wrap.getBoundingClientRect();
        var pw = pop.getBoundingClientRect().width;
        var left = Math.min(Math.max(8, r.left), window.innerWidth - pw - 8);
        pop.style.left = left + 'px';
        pop.style.top = (r.bottom + 6) + 'px';
        wrap.classList.add('open');
        openPopup = { el: pop, wrap: wrap };
    }

    // ----------------------------------------------------------- 移动端抽屉
    function buildDrawer() {
        if (!els.drawer) return;
        closePopup();
        els.drawer.innerHTML = '';
        var head = el('div', 'nav-drawer-head');
        head.appendChild(el('strong', null, '导航'));
        var close = el('button', 'nav-drawer-close', '×');
        close.addEventListener('click', closeDrawer);
        head.appendChild(close);
        els.drawer.appendChild(head);
        var act = activeIds(state.confirmed);
        state.units.forEach(function (unit) {
            if (unit.kind === 'link') {
                var a = makeLinkEl(unit.node, act.linkId === unit.id, {});
                a.classList.add('nav-drawer-item');
                els.drawer.appendChild(a);
            } else {
                var g = el('div', 'nav-drawer-group');
                var lbl = el('div', 'nav-drawer-group-label');
                lbl.textContent = (iconChar(unit.node.icon) ? iconChar(unit.node.icon) + ' ' : '') + (unit.node.name || '');
                var arrow = el('span', null, '▾'); lbl.appendChild(arrow);
                var sub = el('div', 'nav-drawer-sub');
                (unit.children || []).forEach(function (c) {
                    var a = makeLinkEl(c, act.linkId === c.id, {});
                    a.classList.add('nav-drawer-item');
                    sub.appendChild(a);
                });
                if (act.parentId === unit.id) g.classList.add('open');
                lbl.addEventListener('click', function () { g.classList.toggle('open'); });
                g.appendChild(lbl); g.appendChild(sub);
                els.drawer.appendChild(g);
            }
        });
    }
    function openDrawer() {
        buildDrawer();
        els.drawer.classList.add('open');
        ensureScrim().classList.add('show');
        state.drawerOpen = true;
    }
    function closeDrawer() {
        if (els.drawer) els.drawer.classList.remove('open');
        var sc = document.querySelector('.nav-scrim'); if (sc) sc.classList.remove('show');
        state.drawerOpen = false;
    }
    function ensureScrim() {
        var sc = document.querySelector('.nav-scrim');
        if (!sc) {
            sc = el('div', 'nav-scrim');
            sc.addEventListener('click', closeDrawer);
            document.body.appendChild(sc);
        }
        return sc;
    }

    // ----------------------------------------------------------- 拖拽排序（编辑态）
    function wireDrag(nodeEl, unit, parentId) {
        nodeEl.addEventListener('dragstart', function (e) {
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/nav', JSON.stringify({ id: unit.id || unit.node.id, parent: parentId }));
            nodeEl.classList.add('dragging');
        });
        nodeEl.addEventListener('dragend', function () { nodeEl.classList.remove('dragging'); });
        nodeEl.addEventListener('dragover', function (e) {
            e.preventDefault(); e.dataTransfer.dropEffect = 'move';
        });
        nodeEl.addEventListener('drop', function (e) {
            e.preventDefault(); e.stopPropagation();
            var data;
            try { data = JSON.parse(e.dataTransfer.getData('text/nav')); } catch (x) { return; }
            if (!data || data.id === (unit.id || unit.node.id)) return;
            // 目标父级：拖到顶层单元上=顶层；(子项容器里调用时另传 parentId)
            var targetParent = null;
            var targetIsGroupChild = false;
            // 若目标单元是 group，且被拖的是 link → 放入该分类内
            if (unit.kind === 'group' && data.parent === null) { targetParent = unit.id; targetIsGroupChild = true; }
            else if (data.id) { targetParent = (unit.node.parent_id != null ? unit.node.parent_id : null); }
            submitMove(data.id, targetParent, unit.id);
        });
    }

    // 编辑态：给分类挂一个可投放子项的区域，支持把链接拖进/拖出
    function enableGroupDropZones() {
        state.units.forEach(function (u) {
            if (u.kind !== 'group') return;
            var btn = u._el && u._el.querySelector('.nav-group-btn');
            if (!btn) return;
            btn.addEventListener('dragover', function (e) { e.preventDefault(); });
            btn.addEventListener('drop', function (e) {
                e.preventDefault(); e.stopPropagation();
                var data; try { data = JSON.parse(e.dataTransfer.getData('text/nav')); } catch (x) { return; }
                if (!data) return;
                submitMove(data.id, u.id, (u.children[0] && u.children[0].id) || null);
            });
        });
    }

    // ----------------------------------------------------------- 保存状态机
    function toast(msg, kind) {
        var t = document.querySelector('.nav-toast');
        if (!t) { t = el('div', 'nav-toast'); document.body.appendChild(t); }
        t.className = 'nav-toast' + (kind ? ' ' + kind : '');
        t.textContent = msg;
        requestAnimationFrame(function () { t.classList.add('show'); });
        clearTimeout(t._h);
        t._h = setTimeout(function () { t.classList.remove('show'); }, 2600);
    }
    function setSaveStatus(s) {
        state.saveStatus = s;
        if (els.editBtn) els.editBtn.classList.toggle('has-pending', s === 'saving' || s === 'pending');
    }

    function apiFetch(url, opts) {
        var ctrl = ('AbortController' in window) ? new AbortController() : null;
        opts = opts || {};
        if (ctrl) opts.signal = ctrl.signal;
        var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, opts.timeout || 12000) : null;
        return fetch(url, opts).then(function (r) {
            if (timer) clearTimeout(timer);
            return r.json().then(function (j) { return { status: r.status, body: j }; })
                .catch(function () { return { status: r.status, body: null }; });
        }).catch(function (err) {
            if (timer) clearTimeout(timer);
            return { status: 0, body: null, networkError: String(err) };
        });
    }

    // 拖拽移动：乐观更新 → POST move → 用返回的权威配置替换
    function submitMove(itemId, parentId, beforeId) {
        if (!state.editable) { toast('导航配置只读，暂不可保存', 'warn'); return; }
        if (state.saveStatus === 'saving') return;
        var baseRev = state.confirmed.revision;
        // 乐观本地预览
        var optimistic = optimisticMove(baseRev, itemId, parentId, beforeId);
        setSaveStatus('saving');
        apiFetch('/api/navigation/move', {
            method: 'POST', headers: NAV_HDR,
            body: JSON.stringify({ base_revision: baseRev, item_id: itemId, parent_id: parentId, before_id: beforeId, mutation_id: 'nav-' + Date.now() })
        }).then(function (res) {
            if (res.status === 200 && res.body && res.body.success) {
                applyConfirmed(res.body.config); persistSnapshot(); setSaveStatus('idle'); toast('已同步', 'ok');
                broadcast();
            } else if (res.status === 409) {
                setSaveStatus('error');
                if (optimistic) render();                   // 回滚乐观预览
                toast('配置已被其他设备修改，请重试', 'err');
                fetchConfig(true);
            } else if (res.status === 0) {
                setSaveStatus('pending'); toast('保存结果待确认…', 'warn'); reconcileAfterUnknown(baseRev);
            } else {
                setSaveStatus('error'); if (optimistic) render();
                toast((res.body && res.body.error) || '保存失败', 'err');
            }
        });
    }

    // 纯前端乐观预览（不改权威，失败即 render 回滚）
    function optimisticMove(baseRev, itemId, parentId, beforeId) {
        try {
            var cfg = cloneCfg(state.confirmed);
            var items = cfg.items;
            var idx = items.findIndex(function (n) { return n.id === itemId; });
            if (idx < 0) return false;
            var moved = items.splice(idx, 1)[0];
            moved.parent_id = parentId || null;
            var sibs = items.filter(function (n) { return (n.parent_id || null) === (parentId || null); })
                .sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
            var at = beforeId ? sibs.findIndex(function (n) { return n.id === beforeId; }) : sibs.length;
            if (at < 0) at = sibs.length;
            sibs.splice(at, 0, moved);
            sibs.forEach(function (n, i) { n.order = i; });
            state.confirmed = cfg;                          // 仅内存预览
            render();
            return true;
        } catch (e) { return false; }
    }

    // 结果未知（断网/超时）时读回核对，不盲目重发
    function reconcileAfterUnknown(prevRev) {
        apiFetch('/api/navigation/config', { headers: { 'Accept': 'application/json' }, timeout: BOOT_TIMEOUT })
            .then(function (res) {
                if (res.status === 200 && res.body && res.body.success) {
                    applyConfirmed(res.body.config); persistSnapshot(); setSaveStatus('idle');
                    toast(res.body.config.revision > prevRev ? '已同步' : '服务器未记录本次改动', 'ok');
                } else { setSaveStatus('error'); toast('无法确认保存结果，请稍后重试', 'err'); }
            });
    }

    function applyConfirmed(cfg) {
        state.confirmed = cfg;
        state.units = [];
        render();
        emit('config', cfg);
    }

    // ----------------------------------------------------------- 快照 & 校准
    function persistSnapshot() {
        if (!state.confirmed) return;
        try { localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(state.confirmed)); lastSnapshotRev = state.confirmed.revision; }
        catch (e) {}
    }
    function loadSnapshot() {
        try {
            var raw = localStorage.getItem(SNAPSHOT_KEY);
            if (!raw) return null;
            var c = JSON.parse(raw);
            if (c && c.schema_version === 1 && Array.isArray(c.items)) return c;
        } catch (e) {}
        return null;
    }

    function fetchConfig(force) {
        return apiFetch('/api/navigation/config', { headers: { 'Accept': 'application/json' }, timeout: BOOT_TIMEOUT })
            .then(function (res) {
                if (res.status === 200 && res.body && res.body.success) {
                    var incoming = res.body.config;
                    state.editable = res.body.editable !== false;
                    state.corrupt = false;
                    // 有未保存编辑/拖拽中时不打断，仅记录
                    if (state.editing) { state._pendingExternal = incoming; return; }
                    if (force || !state.calibrated || incoming.revision !== (state.confirmed && state.confirmed.revision)) {
                        applyConfirmed(incoming); persistSnapshot();
                    }
                    state.calibrated = true;
                } else if (res.body && res.body.code === 'corrupt') {
                    state.corrupt = true; state.editable = false;
                    state.corruptFingerprint = res.body.fingerprint;
                    emit('corrupt', res.body);
                }
                // db_error/网络错：保留快照或默认，不改写
            });
    }

    function broadcast() {
        try { if ('BroadcastChannel' in window) { var bc = new BroadcastChannel(BROADCAST); bc.postMessage({ rev: state.confirmed && state.confirmed.revision }); bc.close(); } } catch (e) {}
        try { localStorage.setItem('cryptonav:ping', String(Date.now())); } catch (e) {}
    }

    function watchExternalChanges() {
        try {
            if ('BroadcastChannel' in window) {
                var bc = new BroadcastChannel(BROADCAST);
                bc.onmessage = function () { if (!state.editing) fetchConfig(false); };
            }
        } catch (e) {}
        window.addEventListener('storage', function (e) {
            if (e.key === 'cryptonav:ping' && !state.editing) fetchConfig(false);
        });
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden && !state.editing) fetchConfig(false);
        });
        setInterval(function () { if (!document.hidden && !state.editing) fetchConfig(false); }, CALIBRATE_MS);
    }

    // ----------------------------------------------------------- 编辑态开关
    function setEditing(on) {
        if (!state.editable && on) { toast(state.corrupt ? '导航配置已损坏，请到管理页修复' : '配置暂不可用，只读展示', 'warn'); return; }
        state.editing = !!on;
        els.nav.classList.toggle('nav-editing', state.editing);
        if (els.editBtn) {
            els.editBtn.classList.toggle('is-active', state.editing);
            els.editBtn.querySelector('.nav-edit-label').textContent = state.editing ? '完成整理' : '整理导航';
        }
        if (state.editing) {
            render(); enableGroupDropZones();
            toast('拖动条目调整顺序；点「导航配置」做增删改', 'warn');
        } else {
            if (state._pendingExternal) { applyConfirmed(state._pendingExternal); state._pendingExternal = null; }
            render();
        }
    }

    // ----------------------------------------------------------- 初始化
    // 首屏防闪：移除 nav.html 引导脚本给 <html> 打的 nav-booting 标记，揭示已渲染的导航。
    // 该标记使 CSS 在引导期隐藏服务端默认平铺兜底，避免「一排式→分类」变脸。
    function revealNav() {
        try { document.documentElement.classList.remove('nav-booting'); } catch (e) {}
    }

    function init() {
        if (!grabDom()) { revealNav(); return; }           // 拿不到 DOM 也要揭示，绝不永久隐藏

        var boot = {};
        try { boot = JSON.parse(document.getElementById('nav-bootstrap').textContent); } catch (e) {}
        // 优先级：本机已确认快照 > 服务端默认；快照存在即用快照，避免闪回默认平铺。
        state.confirmed = boot.defaultConfig || null;

        var snap = loadSnapshot();
        if (snap) { state.confirmed = snap; }              // 先用已确认快照，避免闪回默认
        render();
        revealNav();                                       // 首次渲染完成，立即揭示最终形态

        // 事件绑定
        if (els.moreBtn) els.moreBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (els.more.classList.contains('open')) { els.more.classList.remove('open'); }
            else { els.more.classList.add('open'); }
        });
        document.addEventListener('click', function () {
            closePopup();
            if (els.more) els.more.classList.remove('open');
        });
        if (els.editBtn) els.editBtn.addEventListener('click', function (e) { e.stopPropagation(); setEditing(!state.editing); });
        if (els.menuBtn) els.menuBtn.addEventListener('click', function (e) { e.stopPropagation(); openDrawer(); });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { closePopup(); closeDrawer(); if (state.editing) setEditing(false); }
        });

        var rz;
        window.addEventListener('resize', function () { clearTimeout(rz); rz = setTimeout(scheduleLayout, 120); });
        if ('ResizeObserver' in window && els.inner) { new ResizeObserver(scheduleLayout).observe(els.inner); }
        window.addEventListener('load', scheduleLayout);
        if (document.fonts && document.fonts.ready) { try { document.fonts.ready.then(scheduleLayout); } catch (e) {} }

        fetchConfig(false).then(function () { if (!state.calibrated) state.calibrated = true; scheduleLayout(); });
        watchExternalChanges();
    }

    // ----------------------------------------------------------- 公开 API（供编辑抽屉/页复用）
    window.CryptoNav = {
        ICONS: ICONS,
        ready: function () { return !!state.confirmed; },
        config: function () { return state.confirmed ? cloneCfg(state.confirmed) : null; },
        revision: function () { return state.confirmed ? state.confirmed.revision : 0; },
        isCorrupt: function () { return state.corrupt; },
        corruptFingerprint: function () { return state.corruptFingerprint || null; },
        applyConfirmed: function (cfg) { applyConfirmed(cfg); persistSnapshot(); broadcast(); },
        saveFull: function (payload, opts) {
            opts = opts || {};
            var body = { base_revision: opts.base_revision != null ? opts.base_revision : state.confirmed.revision,
                mutation_id: opts.mutation_id || ('nav-' + Date.now()), settings: payload.settings, items: payload.items };
            if (opts.expected_fingerprint) body.expected_fingerprint = opts.expected_fingerprint;
            return apiFetch('/api/navigation/config', { method: 'PUT', headers: NAV_HDR, body: JSON.stringify(body) });
        },
        move: function (itemId, parentId, beforeId) { return submitMove(itemId, parentId, beforeId); },
        resetDefault: function (baseRevision) {
            return apiFetch('/api/navigation/reset', { method: 'POST', headers: NAV_HDR,
                body: JSON.stringify({ base_revision: baseRevision, confirm: true, mutation_id: 'nav-reset-' + Date.now() }) });
        },
        getCatalog: function () {
            if (state.catalog) return Promise.resolve(state.catalog);
            return apiFetch('/api/navigation/catalog', { headers: { 'Accept': 'application/json' } }).then(function (r) {
                state.catalog = (r.body && r.body.success) ? { pages: r.body.pages, icons: r.body.icons } : { pages: [], icons: [] };
                return state.catalog;
            });
        },
        on: on,
        toast: toast,
        setEditing: setEditing,
        isEditing: function () { return state.editing; },
        openEditor: function () { if (window.CryptoNavEditor) return window.CryptoNavEditor.open(); window.location.href = '/navigation-settings'; },
        activeId: function () { return state.activeId; }
    };

    // navigation.js 由 nav.html 紧跟 <nav> 标记同步加载：此刻 nav DOM 与 nav-bootstrap
    // 均已解析就绪，立即初始化可在首屏绘制前完成渲染，最大限度缩短 nav-booting 隐藏期；
    // 若因异常排布导致 DOM 尚未就绪，再退回 DOMContentLoaded。
    if (document.getElementById('navLinks') && document.getElementById('nav-bootstrap')) {
        init();
    } else if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
