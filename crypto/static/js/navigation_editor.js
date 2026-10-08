/* ================================================================
 * 导航自定义管理 —— 可视化编辑器（管理页 / 导航栏恢复弹窗共用）
 * ----------------------------------------------------------------
 * 在内存草稿上做全量 CRUD + 排序 + 显隐 + 分类移动，点"保存"一次性整份提交
 * （PUT /api/navigation/config）。导航栏直接拖拽走 CryptoNav.move，二者共用同一
 * Store 与写入队列；有草稿未保存时导航栏拖拽被禁用，避免互相覆盖。
 *
 * 排序以稳定 id 表达，保存前按「同级数组下标」重新规范化 order；隐藏/删除是显式
 * 状态，绝不会因"看起来为空"被恢复。全站最多一个活动编辑实例（实例化前缀隔离 DOM id）。
 * ================================================================ */
(function () {
    'use strict';

    var esc = function (s) { return (s == null ? '' : String(s)); };
    function iconChar(icon) {
        if (!icon) return '';
        if (icon.type === 'builtin') return (window.CryptoNav && CryptoNav.ICONS[icon.value]) || '';
        if (icon.type === 'text') return icon.value || '';
        return '';
    }
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }
    function clone(o) { return JSON.parse(JSON.stringify(o)); }
    function isExternal(u) { return !!u && /^https?:\/\//i.test(u); }
    var uid = (function () { var i = 0; return function () { i++; return 'custom-' + Date.now().toString(36) + '-' + i; }; })();

    var INST = 0;
    var current = null;   // 活动实例

    function CryptoNavEditor() {}

    // --------------------------------------------------- 挂载
    CryptoNavEditor.mount = function (container, opts) {
        opts = opts || {};
        var base = window.CryptoNav.config();
        if (!base) { container.textContent = '导航配置尚未加载…'; return null; }
        var inst = {
            id: 'nvs' + (++INST),
            container: container,
            opts: opts,
            draft: { settings: clone(base.settings), items: clone(base.items) },
            baseRevision: base.revision,
            dirty: false,
            selectedId: null,
            catalog: null
        };
        current = inst;
        inst.render = function () { renderAll(inst); };
        window.CryptoNav.on('config', function () {
            // 外部权威变化：无草稿则跟随刷新；有草稿提示冲突不覆盖
            if (!inst.dirty) { var b = window.CryptoNav.config(); inst.baseRevision = b.revision; inst.draft = { settings: clone(b.settings), items: clone(b.items) }; renderAll(inst); }
            else setStatus(inst, 'err', '检测到其他设备改动，当前草稿未受影响，可保存覆盖或取消后重载');
        });
        renderAll(inst);
        window.CryptoNav.getCatalog().then(function (c) { inst.catalog = c; if (inst.selectedId == null) renderDetail(inst); });
        return inst;
    };

    // 从导航栏「导航配置」按钮唤起的对话框
    CryptoNavEditor.open = function () {
        if (location.pathname.replace(/\/+$/, '') === '/navigation-settings') {
            var host = document.getElementById('nvsApp'); if (host) host.scrollIntoView();
            return;
        }
        var dlg = document.getElementById('navEditorDialog');
        if (!dlg) {
            dlg = el('dialog', 'nvs-dialog'); dlg.id = 'navEditorDialog';
            var inner = el('div', 'nvs-dialog-inner'); inner.id = 'navEditorMount';
            dlg.appendChild(inner);
            document.body.appendChild(dlg);
        }
        var mount = document.getElementById('navEditorMount');
        mount.innerHTML = '';
        CryptoNavEditor.mount(mount, { embedded: true });
        if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    };
    CryptoNavEditor.closeDialog = function () {
        var dlg = document.getElementById('navEditorDialog'); if (dlg && dlg.close) dlg.close();
    };

    // --------------------------------------------------- 草稿操作
    function byId(inst, id) { return inst.draft.items.find(function (n) { return n.id === id; }); }
    function topNodes(inst) {
        return inst.draft.items.filter(function (n) { return n.parent_id == null; })
            .sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
    }
    function childNodes(inst, gid) {
        return inst.draft.items.filter(function (n) { return n.parent_id === gid; })
            .sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
    }
    function normalizeOrders(inst) {
        topNodes(inst).forEach(function (n, i) { n.order = i; });
        inst.draft.items.filter(function (n) { return n.type === 'group'; }).forEach(function (g) {
            childNodes(inst, g.id).forEach(function (c, i) { c.order = i; });
        });
    }
    function markDirty(inst) {
        if (!inst.dirty) { inst.dirty = true; setStatus(inst, 'dirty', '有未保存的更改'); }
    }
    function moveSibling(inst, id, delta) {
        var node = byId(inst, id); if (!node) return;
        var list = node.parent_id == null ? topNodes(inst) : childNodes(inst, node.parent_id);
        var i = list.findIndex(function (n) { return n.id === id; });
        var j = i + delta;
        if (j < 0 || j >= list.length) return;
        list.splice(i, 1); list.splice(j, 0, node);
        normalizeOrders(inst); markDirty(inst); renderTree(inst); renderDetail(inst);
    }
    function removeNode(inst, id) {
        var node = byId(inst, id); if (!node) return;
        if (node.type === 'group') {
            var kids = childNodes(inst, id);
            var go = node.order || 0;
            inst.draft.items = inst.draft.items.filter(function (n) { return n.id !== id && n.parent_id !== id; });
            // 子项提升到该分类原顶层位置
            kids.forEach(function (k, i) { k.parent_id = null; k.order = go + i; });
            inst.draft.items = inst.draft.items.concat(kids);
        } else {
            inst.draft.items = inst.draft.items.filter(function (n) { return n.id !== id; });
        }
        normalizeOrders(inst);
        if (inst.selectedId === id) inst.selectedId = null;
        markDirty(inst); renderAll(inst);
    }
    function deleteGroupWithChildren(inst, id) {
        inst.draft.items = inst.draft.items.filter(function (n) { return n.id !== id && n.parent_id !== id; });
        normalizeOrders(inst); if (inst.selectedId === id) inst.selectedId = null;
        markDirty(inst); renderAll(inst);
    }
    function addLink(inst, fields) {
        fields = fields || {};
        var top = topNodes(inst);
        var node = {
            id: fields.id || uid(), type: 'link', parent_id: fields.parent_id || null,
            name: fields.name || '新链接', url: fields.url || '/', icon: fields.icon || null,
            order: (fields.parent_id == null ? top.length : childNodes(inst, fields.parent_id).length),
            visible: fields.visible !== false, target: fields.target || '_self',
            builtin_key: fields.builtin_key || null
        };
        inst.draft.items.push(node);
        normalizeOrders(inst); inst.selectedId = node.id; markDirty(inst); renderAll(inst);
        return node;
    }
    function addGroup(inst) {
        var node = { id: uid(), type: 'group', parent_id: null, name: '新分类', url: null,
            icon: null, order: topNodes(inst).length, visible: true, target: null, builtin_key: null };
        inst.draft.items.push(node); normalizeOrders(inst); inst.selectedId = node.id;
        markDirty(inst); renderAll(inst); return node;
    }
    function updateNode(inst, id, patch) {
        var node = byId(inst, id); if (!node) return;
        Object.keys(patch).forEach(function (k) { node[k] = patch[k]; });
        if ('parent_id' in patch) {
            var sibs = patch.parent_id == null ? topNodes(inst) : childNodes(inst, patch.parent_id);
            node.order = sibs.length;
        }
        normalizeOrders(inst); markDirty(inst); renderTree(inst); renderPreview(inst);
    }

    // --------------------------------------------------- 保存
    function save(inst) {
        normalizeOrders(inst);
        setStatus(inst, 'dirty', '正在保存…');
        window.CryptoNav.saveFull({ settings: inst.draft.settings, items: inst.draft.items },
            { base_revision: inst.baseRevision }).then(function (res) {
            if (res.status === 200 && res.body && res.body.success) {
                window.CryptoNav.applyConfirmed(res.body.config);
                inst.baseRevision = res.body.config.revision;
                inst.draft = { settings: clone(res.body.config.settings), items: clone(res.body.config.items) };
                inst.dirty = false;
                setStatus(inst, 'saved', '已保存并同步');
                (res.body.warnings || []).forEach(function (w) { window.CryptoNav.toast(w, 'warn'); });
                renderAll(inst);
            } else if (res.status === 409 && res.body && res.body.code === 'corrupt') {
                doRepair(inst);
            } else if (res.status === 409) {
                setStatus(inst, 'err', '版本冲突：其他设备已修改。将拉取最新，你的草稿保留。');
                window.CryptoNav.move && null;
                reloadKeepDraft(inst);
            } else {
                setStatus(inst, 'err', (res.body && res.body.error) || ('保存失败 (' + res.status + ')'));
            }
        });
    }
    function reloadKeepDraft(inst) {
        window.CryptoNav.saveFull; // no-op guard
        // 重新读取权威并更新 baseRevision，但保留草稿内容供用户再次保存
        window.CryptoNav.config();
        fetch('/api/navigation/config', { headers: { 'Accept': 'application/json' } }).then(function (r) { return r.json(); }).then(function (j) {
            if (j && j.success) { inst.baseRevision = j.config.revision; setStatus(inst, 'dirty', '已刷新基线，可再次保存覆盖'); }
        });
    }
    function doRepair(inst) {
        var fp = window.CryptoNav.corruptFingerprint();
        if (!confirm('检测到导航配置已损坏，是否用当前草稿覆盖修复？')) return;
        setStatus(inst, 'dirty', '修复中…');
        window.CryptoNav.saveFull({ settings: inst.draft.settings, items: inst.draft.items },
            { base_revision: 0, expected_fingerprint: fp }).then(function (res) {
            if (res.status === 200 && res.body && res.body.success) {
                window.CryptoNav.applyConfirmed(res.body.config);
                inst.baseRevision = res.body.config.revision; inst.dirty = false;
                setStatus(inst, 'saved', '已修复并保存');
            } else setStatus(inst, 'err', (res.body && res.body.error) || '修复失败');
        });
    }
    function resetDefault(inst) {
        if (!confirm('恢复默认布局将覆盖当前全部自定义（可再编辑）。确认继续？')) return;
        window.CryptoNav.resetDefault(inst.baseRevision).then(function (res) {
            if (res.status === 200 && res.body && res.body.success) {
                window.CryptoNav.applyConfirmed(res.body.config);
                inst.baseRevision = res.body.config.revision;
                inst.draft = { settings: clone(res.body.config.settings), items: clone(res.body.config.items) };
                inst.dirty = false; inst.selectedId = null; setStatus(inst, 'saved', '已恢复默认布局'); renderAll(inst);
            } else setStatus(inst, 'err', (res.body && res.body.error) || '恢复失败');
        });
    }
    function discard(inst) {
        var b = window.CryptoNav.config();
        inst.draft = { settings: clone(b.settings), items: clone(b.items) };
        inst.baseRevision = b.revision; inst.dirty = false; inst.selectedId = null;
        setStatus(inst, 'idle', '未做更改'); renderAll(inst);
    }

    // --------------------------------------------------- 导入 / 导出
    function exportJson(inst) {
        var payload = JSON.stringify({ schema_version: 1, settings: inst.draft.settings, items: inst.draft.items }, null, 2);
        var blob = new Blob([payload], { type: 'application/json' });
        var a = el('a'); a.href = URL.createObjectURL(blob); a.download = 'navigation-config.json';
        document.body.appendChild(a); a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500);
    }
    function importJson(inst) {
        var txt = prompt('粘贴导航配置 JSON（仅导入导航项，不含口令等数据）：');
        if (!txt) return;
        try {
            var obj = JSON.parse(txt);
            if (!obj || !Array.isArray(obj.items)) throw new Error('缺 items');
            inst.draft = { settings: obj.settings || clone(window.CryptoNav.config().settings), items: clone(obj.items) };
            normalizeOrders(inst); inst.dirty = true; inst.selectedId = null;
            setStatus(inst, 'dirty', '已导入草稿，保存后生效'); renderAll(inst);
        } catch (e) { alert('导入失败：' + e.message); }
    }

    // --------------------------------------------------- 渲染
    function renderAll(inst) { renderToolbar(inst); renderTree(inst); renderDetail(inst); renderPreview(inst); renderStatus(inst); }

    function renderToolbar(inst) {
        var bar = inst.container.querySelector('.nvs-toolbar'); if (!bar) { bar = el('div', 'nvs-toolbar'); inst.container.appendChild(bar); }
        bar.innerHTML = '';
        bar.appendChild(btn('＋ 新增链接', 'primary', function () { addLink(inst, { parent_id: null }); }));
        bar.appendChild(btn('＋ 新增分类', '', addGroup.bind(null, inst)));
        bar.appendChild(btn('📚 从内置页面添加', '', function () { addFromCatalog(inst); }));
        bar.appendChild(btn('⬆ 导入', '', function () { importJson(inst); }));
        bar.appendChild(btn('⬇ 导出', '', function () { exportJson(inst); }));
        bar.appendChild(btn('↺ 恢复默认', 'danger', function () { resetDefault(inst); }));
    }
    function btn(label, cls, fn) {
        var b = el('button', 'nvs-btn' + (cls ? ' ' + cls : ''), label);
        b.type = 'button'; b.addEventListener('click', fn); return b;
    }

    function addFromCatalog(inst) {
        if (!inst.catalog) { setStatus(inst, 'err', '内置目录加载中，请稍候'); return; }
        var sel = prompt('输入序号添加内置页面：\n' + inst.catalog.pages.map(function (p, i) { return (i + 1) + '. ' + p.name + ' (' + p.url + ')'; }).join('\n'));
        if (sel == null) return;
        var idx = parseInt(sel, 10) - 1;
        var pg = inst.catalog.pages[idx];
        if (!pg) { alert('无效序号'); return; }
        var used = inst.draft.items.some(function (n) { return n.builtin_key === pg.builtin_key; });
        if (used && !confirm('该页面已在导航中，仍要再添加一个入口？')) return;
        addLink(inst, { name: pg.name, url: pg.url, builtin_key: pg.builtin_key, target: '_self' });
    }

    function renderTree(inst) {
        var box = inst.container.querySelector('.nvs-tree'); if (!box) {
            var card = el('div', 'nvs-card'); card.appendChild(el('h2', null, '导航结构（含隐藏项）'));
            box = el('ul', 'nvs-tree'); card.appendChild(box); inst.container.querySelector('.nvs-col-left').appendChild(card);
        }
        box.innerHTML = '';
        if (!inst.draft.items.length) box.appendChild(el('li', 'nvs-empty', '（空）所有入口已删除，可用上方按钮新增，或恢复默认'));
        topNodes(inst).forEach(function (node) {
            if (node.parent_id == null) {
                box.appendChild(row(inst, node, false));
                if (node.type === 'group') childNodes(inst, node.id).forEach(function (c) { box.appendChild(row(inst, c, true)); });
            }
        });
    }

    function row(inst, node, isChild) {
        var li = el('li', 'nvs-row' + (isChild ? ' child' : '') + (node.type === 'group' ? ' grouphdr' : '') + (inst.selectedId === node.id ? ' selected' : ''));
        li.setAttribute('data-nav-id', node.id);
        var grip = el('span', 'nvs-grip', '⠿');
        li.appendChild(grip);
        var ic = iconChar(node.icon);
        li.appendChild(el('span', 'nvs-icon', ic || (node.type === 'group' ? '▸' : '·')));
        li.appendChild(el('span', 'nvs-name', node.name));
        if (node.type === 'link') {
            if (isExternal(node.url)) li.appendChild(el('span', 'nvs-tag external', '外链'));
            li.appendChild(el('span', 'nvs-url', node.url || ''));
        } else li.appendChild(el('span', 'nvs-tag', childNodes(inst, node.id).length + ' 子项'));
        if (node.builtin_key) li.appendChild(el('span', 'nvs-tag', '内置'));
        if (!node.visible) li.appendChild(el('span', 'nvs-tag hidden', '隐藏'));

        // 操作按钮：上移/下移/显隐/编辑/删除
        li.appendChild(mini('↑', function () { moveSibling(inst, node.id, -1); }));
        li.appendChild(mini('↓', function () { moveSibling(inst, node.id, 1); }));
        var eye = mini(node.visible ? '🙈' : '👁', function () { updateNode(inst, node.id, { visible: !node.visible }); renderTree(inst); });
        eye.title = node.visible ? '隐藏' : '显示';
        li.appendChild(eye);
        li.appendChild(mini('✎', function () { inst.selectedId = node.id; renderTree(inst); renderDetail(inst); }));
        var del = mini('🗑', function () {
            if (node.type === 'group' && childNodes(inst, node.id).length) {
                if (confirm('删除分类「' + node.name + '」？其子项将提升到该分类原顶层位置。\n（如需连同子项一并删除，请在详情表单里勾选。）')) removeNode(inst, node.id);
            } else if (confirm('从导航删除「' + node.name + '」？仅影响菜单，不删除路由或数据。')) removeNode(inst, node.id);
        });
        del.classList.add('del'); li.appendChild(del);
        return li;
    }
    function mini(txt, fn) { var b = el('button', 'nvs-mini-btn', txt); b.type = 'button'; b.addEventListener('click', fn); return b; }

    function renderDetail(inst) {
        var box = inst.container.querySelector('.nvs-detail'); if (!box) return;
        box.innerHTML = '';
        var card = el('div', 'nvs-card'); card.appendChild(el('h2', null, '详情编辑'));
        var form = el('div', 'nvs-form');
        var node = inst.selectedId ? byId(inst, inst.selectedId) : null;
        if (!node) { card.appendChild(el('div', 'nvs-empty', '选择左侧任一导航项进行编辑。')); box.appendChild(card); return; }

        form.appendChild(field('名称', textInput(node.name, function (v) { updateNode(inst, node.id, { name: v }); })));

        if (node.type === 'link') {
            form.appendChild(field('URL（站内以 / 开头，或 http/https 绝对地址）', textInput(node.url, function (v) { updateNode(inst, node.id, { url: v.trim() }); })));
            var tgtSel = select(['_self', '_blank'], node.target || '_self', function (v) { updateNode(inst, node.id, { target: v }); },
                { _self: '当前标签', _blank: '新标签' });
            form.appendChild(field('打开方式', tgtSel));
        } else {
            form.appendChild(el('div', 'nvs-empty', '分类为展开按钮，本身不跳转。'));
        }

        // 所属分类
        var groups = inst.draft.items.filter(function (n) { return n.type === 'group' && n.id !== node.id; });
        var parentOpts = [{ v: '', t: '（顶层）' }].concat(groups.map(function (g) { return { v: g.id, t: '分类：' + g.name }; }));
        var curParent = node.parent_id == null ? '' : node.parent_id;
        var pSel = selectRaw(parentOpts, curParent, function (v) {
            var pid = v || null;
            if (node.type === 'group') { alert('分类只能位于顶层'); renderDetail(inst); return; }
            updateNode(inst, node.id, { parent_id: pid }); renderTree(inst);
        });
        form.appendChild(field('所属分类', pSel));

        form.appendChild(field('图标', iconPicker(inst, node, function (icon) { updateNode(inst, node.id, { icon: icon }); renderTree(inst); })));

        var chk = el('label', 'nvs-check');
        var cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = node.visible !== false;
        cb.addEventListener('change', function () { updateNode(inst, node.id, { visible: cb.checked }); renderTree(inst); });
        chk.appendChild(cb); chk.appendChild(el('span', null, '在导航中显示'));
        form.appendChild(chk);

        if (node.type === 'group' && childNodes(inst, node.id).length) {
            var danger = el('div', 'nvs-form-actions');
            danger.appendChild(btn('删除分类及其全部子链接', 'danger', function () {
                if (confirm('删除分类「' + node.name + '」及其内 ' + childNodes(inst, node.id).length + ' 个链接？仅影响菜单，不删路由/数据。'))
                    deleteGroupWithChildren(inst, node.id);
            }));
            form.appendChild(danger);
        }

        var acts = el('div', 'nvs-form-actions');
        acts.appendChild(btn('应用并高亮', 'primary', function () { renderTree(inst); setStatus(inst, inst.dirty ? 'dirty' : 'idle', inst.dirty ? '有未保存的更改' : '详情已更新'); }));
        form.appendChild(acts);
        card.appendChild(form);
        box.appendChild(card);
    }

    function field(label, input) { var w = el('div'); w.appendChild(el('label', null, label)); w.appendChild(input); return w; }
    function textInput(val, fn) { var i = document.createElement('input'); i.type = 'text'; i.value = esc(val); i.addEventListener('input', function () { fn(i.value); }); return i; }
    function select(opts, val, fn, labels) {
        return selectRaw(opts.map(function (o) { return { v: o, t: (labels && labels[o]) || o }; }), val, fn);
    }
    function selectRaw(optList, val, fn) {
        var s = document.createElement('select');
        optList.forEach(function (o) { var op = el('option', null, o.t); op.value = o.v; if (String(o.v) === String(val)) op.selected = true; s.appendChild(op); });
        s.addEventListener('change', function () { fn(s.value); });
        return s;
    }
    function iconPicker(inst, node, fn) {
        var box = el('div', 'nvs-icon-grid');
        var keys = (window.CryptoNav.ICONS);
        var none = el('button', 'nvs-icon-opt' + (!node.icon ? ' sel' : ''), '无'); none.type = 'button';
        none.addEventListener('click', function () { box.querySelectorAll('.sel').forEach(function (x) { x.classList.remove('sel'); }); none.classList.add('sel'); fn(null); });
        box.appendChild(none);
        Object.keys(keys).forEach(function (k) {
            var sel = node.icon && node.icon.type === 'builtin' && node.icon.value === k;
            var b = el('button', 'nvs-icon-opt' + (sel ? ' sel' : ''), keys[k]); b.type = 'button'; b.title = k;
            b.addEventListener('click', function () { box.querySelectorAll('.sel').forEach(function (x) { x.classList.remove('sel'); }); b.classList.add('sel'); fn({ type: 'builtin', value: k }); });
            box.appendChild(b);
        });
        var txt = el('input'); txt.type = 'text'; txt.placeholder = '自定义文本图标'; txt.value = (node.icon && node.icon.type === 'text') ? node.icon.value : '';
        txt.addEventListener('input', function () { var v = txt.value.trim(); if (v) fn({ type: 'text', value: v }); });
        box.appendChild(txt);
        return box;
    }

    function renderPreview(inst) {
        var box = inst.container.querySelector('.nvs-preview-host'); if (!box) return;
        box.innerHTML = '';
        var card = el('div', 'nvs-card'); card.appendChild(el('h2', null, '预览'));
        var wrap = el('div', 'nvs-preview');
        wrap.appendChild(el('div', 'nvs-preview-cap', '桌面（可见项，按当前顺序）'));
        var bar = el('div', 'nvs-pv-bar');
        var maxVis = inst.draft.settings.desktop_max_visible || 6;
        var vis = topNodes(inst).filter(function (n) {
            if (!n.visible) return false;
            if (n.type === 'group') return childNodes(inst, n.id).some(function (c) { return c.visible; });
            return true;
        });
        vis.slice(0, maxVis).forEach(function (n) {
            var cls = 'nvs-pv-item' + (n.type === 'group' ? ' grp' : '');
            var t = (iconChar(n.icon) ? iconChar(n.icon) + ' ' : '') + n.name + (n.type === 'group' ? ' ▾' : '');
            var e = el('span', cls, t); if (!n.visible) e.style.opacity = .4; bar.appendChild(e);
        });
        if (vis.length > maxVis) bar.appendChild(el('span', 'nvs-pv-item', '更多 ▾ (' + (vis.length - maxVis) + ')'));
        wrap.appendChild(bar);
        card.appendChild(wrap);

        var range = el('div'); range.appendChild(el('label', null, '桌面顶层最多显示：' + maxVis));
        var rng = document.createElement('input'); rng.type = 'range'; rng.min = '3'; rng.max = '8'; rng.value = String(maxVis);
        rng.addEventListener('input', function () { inst.draft.settings.desktop_max_visible = parseInt(rng.value, 10); markDirty(inst); renderPreview(inst); renderToolbar(inst); });
        range.appendChild(rng); card.appendChild(range);
        box.appendChild(card);
    }

    function setStatus(inst, kind, msg) { inst._status = { kind: kind, msg: msg }; if (inst.container.querySelector('.nvs-statusbar')) renderStatus(inst); }
    function renderStatus(inst) {
        var sb = inst.container.querySelector('.nvs-statusbar'); if (!sb) return;
        var st = inst._status || { kind: 'idle', msg: '未做更改' };
        sb.innerHTML = '';
        var cls = st.kind === 'saved' ? 'saved' : (st.kind === 'err' ? 'err' : (st.kind === 'dirty' || st.kind === 'pending' ? 'dirty' : ''));
        sb.appendChild(el('span', cls, '● ' + st.msg));
        sb.appendChild(el('span', 'spacer'));
        var rev = el('span', null, '基线版本 r' + inst.baseRevision); sb.appendChild(rev);
        var saveBtn = btn(inst.dirty ? '💾 保存修改' : '已保存', 'primary', function () { if (inst.dirty) save(inst); });
        var disc = btn('放弃更改', '', function () { if (!inst.dirty || confirm('放弃未保存的更改？')) discard(inst); });
        sb.appendChild(saveBtn); sb.appendChild(disc);
    }

    // 页面离开守卫
    window.addEventListener('beforeunload', function (e) {
        if (current && current.dirty) { e.preventDefault(); e.returnValue = ''; }
    });

    window.CryptoNavEditor = CryptoNavEditor;
})();
