/**
 * OKX 交易操作能力清单页 —— 交互层
 * =================================
 * 文档正文由服务端渲染（crypto/capability_routes.py），本文件只负责四件事：
 *   1. 工具矩阵：把 data/_okx_list_tools.json 的解析结果渲染成可筛选表格
 *   2. 全文搜索：按小节过滤 + 关键词高亮 + 逐个跳转
 *   3. 目录：锚点跳转（自动展开目标小节）+ 滚动高亮
 *   4. 小节折叠 / 代码块复制
 * 全部为渐进增强：不执行 JS 时，文档正文、图示、模块统计仍然完整可读。
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
    function toast(msg) {
        var t = $('#cap-toast');
        if (!t) {
            t = document.createElement('div');
            t.id = 'cap-toast';
            t.style.cssText = 'position:fixed;left:50%;bottom:38px;transform:translateX(-50%);' +
                'background:rgba(38,51,63,.94);color:#e8f1f8;font-size:.8rem;padding:8px 16px;' +
                'border-radius:8px;z-index:9999;opacity:0;transition:opacity .18s;pointer-events:none';
            document.body.appendChild(t);
        }
        t.textContent = msg;
        t.style.opacity = '1';
        clearTimeout(t._h);
        t._h = setTimeout(function () { t.style.opacity = '0'; }, 1600);
    }

    /* =====================================================================
     * 1. 模块读写比条形图（宽度来自 data-*，避免在 style 属性里塞模板变量）
     * ===================================================================== */
    function drawBars() {
        $$('.cap-bar').forEach(function (bar) {
            var w = parseFloat(bar.getAttribute('data-w')) || 0;
            var r = parseFloat(bar.getAttribute('data-r')) || 0;
            bar.innerHTML = '<span class="bw"></span><span class="br"></span>';
            bar.children[0].style.width = w + '%';
            bar.children[1].style.width = r + '%';
        });
    }

    /* =====================================================================
     * 2. 工具矩阵命令表
     * ===================================================================== */
    var cmdState = { mod: '', kind: 'all', q: '' };

    function toolsData() {
        var el = $('#cap-tools-data');
        if (!el) return null;
        try { return JSON.parse(el.textContent); } catch (e) { return null; }
    }

    function buildCmdTable() {
        var body = $('#cap-cmd-body');
        var data = toolsData();
        if (!body || !data || !data.modules) return;
        var html = [];
        data.modules.forEach(function (m) {
            m.commands.forEach(function (c) {
                html.push('<tr data-mod="' + escapeHtml(m.name) + '" data-k="' + escapeHtml(c.kind) + '" ' +
                    'data-s="' + escapeHtml((m.name + ' ' + c.cli + ' ' + c.tool + ' ' + c.desc).toLowerCase()) + '">' +
                    '<td><code>okx ' + escapeHtml(c.cli) + '</code></td>' +
                    '<td><code>' + escapeHtml(c.tool) + '</code>' +
                    (c.tool === '-' ? '<div class="cap-req">仅 CLI 可用</div>' : '') + '</td>' +
                    '<td><span class="cap-tag cap-tag-' + (c.kind === 'W' ? 'w">写' : 'r">读') + '</span></td>' +
                    '<td>' + escapeHtml(c.desc) + '</td>' +
                    '<td class="cap-req">' + (c.req && c.req.length
                        ? '<b>' + escapeHtml(c.req.join(' ')) + '</b>' : '—') + '</td>' +
                    '</tr>');
            });
        });
        body.innerHTML = html.join('');
        applyCmdFilter();
    }

    function applyCmdFilter() {
        var rows = $$('#cap-cmd-body tr');
        var q = cmdState.q.toLowerCase().trim();
        var shown = 0;
        rows.forEach(function (tr) {
            var ok = (!cmdState.mod || tr.getAttribute('data-mod') === cmdState.mod) &&
                (cmdState.kind === 'all' || tr.getAttribute('data-k') === cmdState.kind) &&
                (!q || tr.getAttribute('data-s').indexOf(q) >= 0);
            tr.style.display = ok ? '' : 'none';
            if (ok) shown++;
        });
        var cnt = $('#cap-cmd-count');
        if (cnt) {
            var bits = [];
            if (cmdState.mod) bits.push('模块 ' + cmdState.mod);
            if (cmdState.kind !== 'all') bits.push(cmdState.kind === 'W' ? '仅写命令' : '仅读命令');
            if (q) bits.push('关键词「' + q + '」');
            cnt.textContent = shown + ' / ' + rows.length + ' 条' + (bits.length ? '　筛选：' + bits.join(' · ') : '');
        }
        var empty = $('#cap-matrix-empty');
        if (empty) {
            empty.style.display = shown ? 'none' : '';
            empty.textContent = shown ? '' : '当前筛选条件下没有命令。试试清空关键词，或点「清除模块筛选」。';
        }
        var tbl = $('#cap-matrix .cap-cmd-tbl');
        if (tbl) tbl.style.display = shown ? '' : 'none';
    }

    function initMatrix() {
        if (!$('#cap-matrix')) return;
        drawBars();
        buildCmdTable();

        $$('#cap-mods .cap-mod').forEach(function (card) {
            card.addEventListener('click', function () {
                var name = card.getAttribute('data-mod');
                var same = cmdState.mod === name;
                cmdState.mod = same ? '' : name;
                $$('#cap-mods .cap-mod').forEach(function (c) { c.classList.remove('sel'); });
                if (!same) card.classList.add('sel');
                applyCmdFilter();
            });
        });
        $$('#cap-kind-seg button').forEach(function (b) {
            b.addEventListener('click', function () {
                $$('#cap-kind-seg button').forEach(function (x) { x.classList.remove('on', 'war'); });
                b.classList.add('on');
                if (b.getAttribute('data-k') === 'W') b.classList.add('war');
                cmdState.kind = b.getAttribute('data-k');
                applyCmdFilter();
            });
        });
        var q = $('#cap-cmd-q');
        if (q) q.addEventListener('input', function () {
            clearTimeout(q._t); q._t = setTimeout(function () { cmdState.q = q.value; applyCmdFilter(); }, 120);
        });
        var rst = $('#cap-mod-reset');
        if (rst) rst.addEventListener('click', function () {
            cmdState.mod = '';
            $$('#cap-mods .cap-mod').forEach(function (c) { c.classList.remove('sel'); });
            if (q) { q.value = ''; cmdState.q = ''; }
            applyCmdFilter();
        });
    }

    /* =====================================================================
     * 3. 小节折叠
     * ===================================================================== */
    var sections = [];

    function secOf(node) {
        return node && node.closest ? node.closest('.cap-sec') : null;
    }
    function setFold(sec, fold) {
        sec.classList.toggle('is-fold', fold);
        var btn = $('.cap-sec-toggle', sec);
        if (btn) btn.textContent = fold ? '▸' : '▾';
        sec.setAttribute('data-folded', fold ? '1' : '0');
    }
    function initFold() {
        sections = $$('#cap-doc .cap-sec');
        sections.forEach(function (sec) {
            var btn = $('.cap-sec-toggle', sec);
            if (btn) btn.addEventListener('click', function () { setFold(sec, !sec.classList.contains('is-fold')); });
            var h = $('.cap-h2', sec);
            if (h) h.addEventListener('click', function () { setFold(sec, !sec.classList.contains('is-fold')); });
        });
        var fa = $('#cap-fold-all'), ua = $('#cap-unfold-all');
        if (fa) fa.addEventListener('click', function () { sections.forEach(function (s) { setFold(s, true); }); });
        if (ua) ua.addEventListener('click', function () { sections.forEach(function (s) { setFold(s, false); }); });
    }

    /* =====================================================================
     * 4. 全文搜索（小节过滤 + 命中高亮 + 逐个跳转）
     * ===================================================================== */
    var hits = [], hitIdx = -1, hitSec = 0, savedFold = null;

    function clearHits() {
        $$('mark.cap-hit').forEach(function (m) {
            var p = m.parentNode;
            p.replaceChild(document.createTextNode(m.textContent), m);
            p.normalize();
        });
        hits = []; hitIdx = -1; hitSec = 0;
    }

    // 目录同步过滤结果：命中小节之外的条目置灰，避免"点了跳不到"。
    // 一个都没命中时不置灰 —— 34 条全灰既没信息量，又像是目录坏了。
    function syncTocDim(on) {
        $$('#cap-toc a').forEach(function (a) {
            var h = document.getElementById((a.getAttribute('href') || '').slice(1));
            var s = h ? secOf(h) : null;
            a.classList.toggle('dim', !!on && !!s && s.classList.contains('cap-hidden'));
        });
    }

    function markIn(root, re, word) {
        var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode: function (n) {
                if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
                var p = n.parentNode;
                if (!p) return NodeFilter.FILTER_REJECT;
                var tag = p.nodeName;
                if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'MARK' || tag === 'BUTTON') {
                    return NodeFilter.FILTER_REJECT;
                }
                return re.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
            }
        });
        var nodes = [], n;
        while ((n = walker.nextNode())) { nodes.push(n); if (nodes.length > 400) break; }
        nodes.forEach(function (tn) {
            var frag = document.createDocumentFragment();
            var text = tn.nodeValue, last = 0;
            re.lastIndex = 0;
            var m, guard = 0;
            while ((m = re.exec(text)) && guard++ < 200) {
                if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
                var mk = document.createElement('mark');
                mk.className = 'cap-hit';
                mk.textContent = m[0];
                frag.appendChild(mk);
                last = m.index + m[0].length;
                if (!m[0].length) re.lastIndex++;
            }
            if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
            tn.parentNode.replaceChild(frag, tn);
        });
        return word;
    }

    function runSearch(raw) {
        var q = (raw || '').trim();
        var meta = $('#cap-q-meta'), nohit = $('#cap-nohit'), clearBtn = $('#cap-q-clear');
        clearHits();
        sections.forEach(function (s) { s.classList.remove('cap-hidden'); });
        if (clearBtn) clearBtn.style.display = q ? 'block' : 'none';

        if (q.length < 2) {
            if (meta) meta.textContent = '';
            if (nohit) nohit.style.display = 'none';
            syncTocDim();
            if (savedFold) { sections.forEach(function (s, i) { setFold(s, savedFold[i]); }); savedFold = null; }
            return;
        }
        // 搜索期间强制展开，否则折叠小节里的内容搜不到
        if (!savedFold) { savedFold = sections.map(function (s) { return s.classList.contains('is-fold'); }); }
        sections.forEach(function (s) { setFold(s, false); });

        var esc = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        var re = new RegExp(esc, 'gi');
        var visSec = 0;
        sections.forEach(function (s) {
            var hit = re.test(s.textContent);
            re.lastIndex = 0;
            if (hit) { visSec++; markIn(s, re, q); }
            s.classList.toggle('cap-hidden', !hit);
        });
        hits = $$('#cap-doc mark.cap-hit');
        hits.slice(300).forEach(function (m) {
            var p = m.parentNode; p.replaceChild(document.createTextNode(m.textContent), m); p.normalize();
        });
        hits = $$('#cap-doc mark.cap-hit');
        hitSec = visSec;
        syncTocDim(visSec > 0);
        if (nohit) nohit.style.display = hits.length ? 'none' : '';
        if (meta && !hits.length) {
            meta.innerHTML = '<b>0</b> 命中（' + sections.length + ' 个小节均无匹配）';
        }
        jump(0);   // 有命中时由 jump 统一写文案（含小节数），避免这里写完立刻被覆盖
    }

    function jump(i) {
        if (!hits.length) return;
        hitIdx = (i + hits.length) % hits.length;
        hits.forEach(function (m) { m.classList.remove('cur'); });
        var m = hits[hitIdx];
        m.classList.add('cur');
        m.scrollIntoView({ behavior: 'smooth', block: 'center' });
        var meta = $('#cap-q-meta');
        if (meta) {
            meta.innerHTML = '第 <b>' + (hitIdx + 1) + '</b> / ' + hits.length + ' 处命中 · ' +
                hitSec + ' 个小节　<span style="color:#98a4b1">回车下一个 · Shift+回车上一个 · Esc 清空</span>';
        }
    }

    function initSearch() {
        var q = $('#cap-q');
        if (!q) return;
        var timer = null;
        q.addEventListener('input', function () {
            clearTimeout(timer);
            timer = setTimeout(function () { runSearch(q.value); }, 180);
        });
        q.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); jump(hitIdx + (e.shiftKey ? -1 : 1)); }
            else if (e.key === 'Escape') { q.value = ''; runSearch(''); }
        });
        var c = $('#cap-q-clear');
        if (c) c.addEventListener('click', function () { q.value = ''; runSearch(''); q.focus(); });
        // 「/」聚焦搜索，符合文档站的肌肉记忆
        document.addEventListener('keydown', function (e) {
            if (e.key === '/' && document.activeElement !== q &&
                !/^(INPUT|TEXTAREA)$/.test((document.activeElement || {}).tagName || '')) {
                e.preventDefault(); q.focus(); q.select();
            }
        });
    }

    /* =====================================================================
     * 5. 目录：跳转（自动展开）+ 滚动高亮
     * ===================================================================== */
    function initToc() {
        var links = $$('#cap-toc a');
        if (!links.length) return;
        links.forEach(function (a) {
            a.addEventListener('click', function (e) {
                var id = a.getAttribute('href').slice(1);
                var h = document.getElementById(id);
                if (!h) return;
                e.preventDefault();
                var s = secOf(h);
                if (s && s.classList.contains('is-fold')) setFold(s, false);
                if (s && s.classList.contains('cap-hidden')) {
                    // 正处于过滤结果之外：先清空搜索再跳
                    var q = $('#cap-q');
                    if (q) { q.value = ''; runSearch(''); }
                }
                history.replaceState(null, '', '#' + id);
                h.scrollIntoView({ behavior: 'smooth', block: 'start' });
                setActive(id);
                // 窄屏目录是浮层，跳完就收起来，否则目录把正文又挡回去了
                var tocEl = $('#cap-toc');
                if (tocEl && tocEl.classList.contains('is-open') &&
                    window.matchMedia('(max-width: 1080px)').matches) {
                    setTocOpen(false);
                }
            });
        });
        initTocSwitch();

        function setActive(id) {
            links.forEach(function (a) { a.classList.toggle('on', a.getAttribute('href') === '#' + id); });
            keepTocVisible(links.filter(function (a) { return a.getAttribute('href') === '#' + id; })[0]);
        }

        // 目录项跟随高亮时，只挪「最近的可滚动祖先」的 scrollTop。绝不能用 scrollIntoView：
        // 窄屏（≤1080px）下 .cap-side 是 position:static 且整页铺开，它不再是滚动容器，
        // 那句会把 document 一起滚回目录区 —— 用户每滚一下就被弹回去，正文根本读不了。
        function keepTocVisible(a) {
            if (!a || !window.getComputedStyle) return;
            for (var el = a.parentNode; el && el !== document.body; el = el.parentNode) {
                var cs = window.getComputedStyle(el);
                if (!/(auto|scroll)/.test(cs.overflowY) || el.scrollHeight <= el.clientHeight + 4) continue;
                var ar = a.getBoundingClientRect(), sr = el.getBoundingClientRect();
                if (ar.top < sr.top) el.scrollTop -= (sr.top - ar.top) + 8;
                else if (ar.bottom > sr.bottom) el.scrollTop += (ar.bottom - sr.bottom) + 8;
                return;
            }
        }

        var heads = $$('#cap-doc .cap-h2, #cap-doc .cap-h3');
        // 滚动监听比 IntersectionObserver 更可控：始终取"已滚过顶部 90px 的最后一个标题"
        var raf = null;
        function spy() {
            raf = null;
            var cur = heads.length ? heads[0].id : null;
            for (var i = 0; i < heads.length; i++) {
                var h = heads[i];
                if (h.classList.contains('cap-hidden') || !h.offsetParent) continue;
                if (h.getBoundingClientRect().top <= 90) cur = h.id; else break;
            }
            if (cur) setActive(cur);
        }
        window.addEventListener('scroll', function () {
            if (!raf) raf = requestAnimationFrame(spy);
        }, { passive: true });
        spy();

        if (location.hash) {
            var h0 = document.getElementById(location.hash.slice(1));
            if (h0) {
                var s0 = secOf(h0);
                if (s0) setFold(s0, false);
                setTimeout(function () { h0.scrollIntoView({ block: 'start' }); setActive(location.hash.slice(1)); }, 40);
            }
        }
    }

    // 窄屏（≤1080px）下 34 条目录会堆成一堵墙挡在正文前，改成点一下才展开的浮层。
    // 开关状态只有这一处出口：漏同步按钮 class / aria 就会出现「目录已收起、箭头还朝上」
    function setTocOpen(open) {
        var toc = $('#cap-toc'), btn = $('#cap-toc-switch');
        if (!toc) return;
        toc.classList.toggle('is-open', !!open);
        if (btn) {
            btn.classList.toggle('is-open', !!open);
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        }
    }

    function initTocSwitch() {
        var btn = $('#cap-toc-switch');
        if (!btn || !$('#cap-toc')) return;
        btn.addEventListener('click', function () {
            setTocOpen(!$('#cap-toc').classList.contains('is-open'));
        });
    }

    /* =====================================================================
     * 6. 代码块复制
     * ===================================================================== */
    function initCopy() {
        $$('.cap-code-copy').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var pre = btn.parentNode.querySelector('pre');
                var text = (pre ? pre.innerText : '').replace(/^\$\s/gm, '');
                var done = function () {
                    btn.textContent = '已复制'; btn.classList.add('done');
                    setTimeout(function () { btn.textContent = '复制'; btn.classList.remove('done'); }, 1400);
                };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(done, function () { fallback(text, done); });
                } else { fallback(text, done); }
            });
        });
        function fallback(text, done) {
            var ta = document.createElement('textarea');
            ta.value = text; ta.style.cssText = 'position:fixed;top:-1000px';
            document.body.appendChild(ta); ta.select();
            try { document.execCommand('copy'); done(); } catch (e) { toast('复制失败，请手动选择'); }
            document.body.removeChild(ta);
        }
    }

    /* =====================================================================
     * 7. 表头点击排序（工具矩阵表）
     * ===================================================================== */
    function initCmdSort() {
        var tbl = $('#cap-cmd-body');
        if (!tbl) return;
        $$('#cap-matrix thead th').forEach(function (th, i) {
            th.style.cursor = 'pointer';
            th.title = '点击按此列排序';
            th.addEventListener('click', function () {
                var body = $('#cap-cmd-body');
                var rows = $$('tr', body);
                var asc = th.getAttribute('data-asc') !== '1';
                $$('#cap-matrix thead th').forEach(function (x) { x.removeAttribute('data-asc'); x.textContent = x.textContent.replace(/ [↑↓]$/, ''); });
                th.setAttribute('data-asc', asc ? '1' : '0');
                th.textContent = th.textContent.replace(/ [↑↓]$/, '') + (asc ? ' ↑' : ' ↓');
                rows.sort(function (a, b) {
                    var x = a.children[i].textContent.trim(), y = b.children[i].textContent.trim();
                    return (asc ? 1 : -1) * x.localeCompare(y, 'zh-Hans-CN', { numeric: true });
                });
                rows.forEach(function (r) { body.appendChild(r); });
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initFold();
        initSearch();
        initToc();
        initCopy();
        initMatrix();
        initCmdSort();
    });
})();
