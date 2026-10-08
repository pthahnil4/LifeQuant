/* ============================================================
 * analysis_quick.js —— 全站「一键批量快照并记录」弹窗（批次11 分析纪律）
 * ============================================================
 * 存在的理由：分析纪律把「先分析、后打卡」变成硬约束后，用户在打卡弹窗里
 * 被拦住时，必须能【就地】把分析做完，而不是被赶去另一个页面再回来。
 * 少一次跳转 = 少一次放弃。所以这套矩阵 UI 必须全站可用。
 *
 * 【为什么不用 MDialog】mdialog.js 并非所有页面都加载（index.html 在 body
 * 末尾才引入），而本模块由 nav.html 全站注入、必须在任何页面都能弹出。
 * 因此自建 overlay，零外部依赖。
 *
 * 【两种取数模式】
 *   live     当下：GET /api/task/analysis/snapshot_batch（含策略方向/ATR/BOLL）
 *   backfill 回溯：GET /plan/api/discipline/backfill-preview?slots=...
 *            只返回当时的真实价格，方向字段留空——历史时刻的策略方向无法在
 *            不引入未来数据的前提下诚实重建，宁缺毋假。
 *
 * 【落库统一出口】两种模式都走 POST /api/task/analysis/records_batch，
 * source 由服务端按 |now-ts| 自动判 live/backfill，前端不可指定（防伪造）。
 *
 * 用法：
 *   window.AnalysisQuick.open({ mode:'live', onSaved: function(recs){...} });
 *   window.AnalysisQuick.open({ mode:'backfill', slots:['2026-09-07 20'], onSaved:... });
 * ============================================================ */
(function () {
    'use strict';

    if (window.AnalysisQuick) { return; }   // 幂等：重复注入只生效一次

    var SNAP_BATCH_URL = '/api/task/analysis/snapshot_batch';
    var PREVIEW_URL = '/plan/api/discipline/backfill-preview';
    var SAVE_URL = '/api/task/analysis/records_batch';

    // 串行拉全部币种可能要十几秒；超时后必须能重试，绝不让按钮永久卡死
    var FETCH_TIMEOUT_MS = 180000;

    var _items = [];        // 当前矩阵原始数据（与行序一一对应）
    var _seq = 0;           // 请求序号：关闭/重发后丢弃在途响应
    var _abort = null;
    var _timer = null;
    var _tick = null;
    var _saving = false;
    var _overlay = null;
    var _opts = null;

    /* ---------- 小工具（自带，不依赖页面里的同名函数） ---------- */

    function esc(s) {
        return String(s === null || s === undefined ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function fmtPrice(v) {
        var n = Number(v);
        if (!isFinite(n) || n === 0) { return '--'; }
        if (n >= 1000) { return n.toFixed(1); }
        if (n >= 1) { return n.toFixed(3); }
        return n.toPrecision(4);
    }

    function getJSON(url, signal) {
        return fetch(url, { signal: signal }).then(function (r) { return r.json(); });
    }

    function postJSON(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        }).then(function (r) { return r.json(); });
    }

    /* ---------- 样式注入（全站一次性） ---------- */

    var CSS = [
        /* z-index 必须高于 MDialog 的 .m-dialog-overlay(90000)：本弹窗常在打卡弹窗内被唤起 */
        '.aq-mask{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:95000;',
        'display:flex;align-items:flex-start;justify-content:center;padding:4vh 12px;overflow:auto;}',
        '.aq-box{background:var(--card-bg,#fff);color:var(--text-main,#333);border-radius:12px;',
        'width:min(1080px,100%);box-shadow:0 12px 40px rgba(0,0,0,.25);overflow:hidden;}',
        '.aq-head{display:flex;align-items:center;gap:10px;padding:14px 18px;',
        'border-bottom:1px solid var(--border-color,#eee);}',
        '.aq-title{font-size:1.02rem;font-weight:700;flex:1;}',
        '.aq-x{border:0;background:transparent;font-size:1.3rem;cursor:pointer;',
        'color:var(--text-muted,#777);line-height:1;padding:2px 6px;}',
        '.aq-body{padding:14px 18px;}',
        '.aq-hint{font-size:.8rem;color:var(--text-muted,#777);margin:0 0 10px;line-height:1.6;}',
        '.aq-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;}',
        '.aq-msg{font-size:.8rem;color:var(--text-muted,#777);flex:1;min-width:180px;}',
        '.aq-msg.err{color:var(--danger-color,#dc3545);}',
        '.aq-btn{border:1px solid var(--border-color,#ddd);background:#fff;border-radius:6px;',
        'padding:6px 12px;font-size:.84rem;cursor:pointer;white-space:nowrap;}',
        '.aq-btn:hover:not(:disabled){border-color:var(--primary-color,#007bff);',
        'color:var(--primary-color,#007bff);}',
        '.aq-btn:disabled{opacity:.55;cursor:not-allowed;}',
        '.aq-btn.pri{background:var(--primary-color,#007bff);border-color:var(--primary-color,#007bff);color:#fff;}',
        '.aq-btn.pri:hover:not(:disabled){opacity:.9;color:#fff;}',
        '.aq-tbl-wrap{max-height:52vh;overflow:auto;border:1px solid var(--border-color,#eee);border-radius:8px;}',
        'table.aq-tbl{width:100%;border-collapse:collapse;font-size:.82rem;}',
        'table.aq-tbl th,table.aq-tbl td{padding:6px 8px;border-bottom:1px solid var(--border-color,#f0f0f0);',
        'text-align:left;vertical-align:middle;}',
        'table.aq-tbl th{position:sticky;top:0;background:#fafafa;z-index:1;font-weight:600;white-space:nowrap;}',
        'table.aq-tbl tr:last-child td{border-bottom:0;}',
        '.aq-empty{padding:26px;text-align:center;color:var(--text-muted,#777);}',
        '.aq-slot-tag{display:inline-block;padding:1px 6px;border-radius:4px;background:#eef4ff;',
        'color:#2f6fd0;font-size:.74rem;white-space:nowrap;}',
        '.aq-green{color:#2f9e44;}.aq-red{color:#e03131;}.aq-dim{color:#999;font-size:.72rem;}',
        '.aq-reason{width:100%;min-width:120px;padding:4px 6px;font-size:.8rem;',
        'border:1px solid var(--border-color,#ddd);border-radius:5px;box-sizing:border-box;}',
        '.aq-foot{padding:12px 18px;border-top:1px solid var(--border-color,#eee);',
        'display:flex;justify-content:flex-end;gap:8px;background:#fcfcfc;}'
    ].join('');

    function ensureStyle() {
        if (document.getElementById('aq-style')) { return; }
        var st = document.createElement('style');
        st.id = 'aq-style';
        st.textContent = CSS;
        document.head.appendChild(st);
    }

    /* ---------- DOM 骨架 ---------- */

    function buildSkeleton() {
        var mask = document.createElement('div');
        mask.className = 'aq-mask';
        mask.innerHTML =
            '<div class="aq-box" role="dialog" aria-modal="true">' +
            '  <div class="aq-head">' +
            '    <div class="aq-title" id="aq-title">📸 分析记录</div>' +
            '    <button type="button" class="aq-x" id="aq-close" title="关闭">×</button>' +
            '  </div>' +
            '  <div class="aq-body">' +
            '    <p class="aq-hint" id="aq-hint"></p>' +
            '    <div class="aq-bar">' +
            '      <button type="button" class="aq-btn" id="aq-all">全选</button>' +
            '      <button type="button" class="aq-btn" id="aq-none">全不选</button>' +
            '      <button type="button" class="aq-btn" id="aq-reload">↻ 重新取数</button>' +
            '      <span class="aq-msg" id="aq-msg"></span>' +
            '    </div>' +
            '    <div class="aq-tbl-wrap"><table class="aq-tbl">' +
            '      <thead id="aq-thead"></thead><tbody id="aq-tbody"></tbody>' +
            '    </table></div>' +
            '  </div>' +
            '  <div class="aq-foot">' +
            '    <button type="button" class="aq-btn" id="aq-cancel">取消</button>' +
            '    <button type="button" class="aq-btn pri" id="aq-save" disabled>💾 保存分析记录</button>' +
            '  </div>' +
            '</div>';
        document.body.appendChild(mask);

        // 点遮罩空白处关闭（点在对话框内不关）
        mask.addEventListener('mousedown', function (e) {
            if (e.target === mask) { close(); }
        });
        mask.querySelector('#aq-close').addEventListener('click', close);
        mask.querySelector('#aq-cancel').addEventListener('click', close);
        mask.querySelector('#aq-all').addEventListener('click', function () { pickAll(true); });
        mask.querySelector('#aq-none').addEventListener('click', function () { pickAll(false); });
        mask.querySelector('#aq-reload').addEventListener('click', function () { load(); });
        mask.querySelector('#aq-save').addEventListener('click', save);
        return mask;
    }

    function $(id) { return _overlay ? _overlay.querySelector('#' + id) : null; }

    function msg(text, isErr) {
        var el = $('aq-msg');
        if (!el) { return; }
        el.textContent = text || '';
        el.classList.toggle('err', !!isErr);
    }

    function setBusy(busy, text) {
        var save = $('aq-save'), reload = $('aq-reload');
        if (save) { save.disabled = busy || !_items.length; save.textContent = text || '💾 保存分析记录'; }
        if (reload) { reload.disabled = busy; }
    }

    function pickAll(on) {
        if (!_overlay) { return; }
        _overlay.querySelectorAll('.aq-pick').forEach(function (cb) { cb.checked = !!on; });
    }

    /* ---------- 矩阵渲染 ---------- */

    function headHtml() {
        var back = _opts && _opts.mode === 'backfill';
        var cols = back
            ? ['<th></th>', '<th>小时槽</th>', '<th>币种</th>', '<th>当时价格</th>', '<th>你的判断</th>', '<th>原因（可选）</th>']
            : ['<th></th>', '<th>币种</th>', '<th>价格</th>', '<th>短周期</th>', '<th>长周期</th>',
               '<th>ATR%</th>', '<th>你的判断</th>', '<th>原因（可选）</th>'];
        return '<tr>' + cols.join('') + '</tr>';
    }

    function dirCn(v) { return v === 'long' ? '看多' : (v === 'short' ? '看空' : '--'); }
    function dirCls(v) { return v === 'long' ? 'aq-green' : (v === 'short' ? 'aq-red' : 'aq-dim'); }

    function rowHtml(d, i) {
        var name = 'aq-j-' + i;
        var judge =
            '<td style="white-space:nowrap">' +
            '<label><input type="radio" name="' + name + '" value="rise"' + (d.user_judgment === 'rise' ? ' checked' : '') + '> 涨</label> ' +
            '<label><input type="radio" name="' + name + '" value="fall"' + (d.user_judgment === 'fall' ? ' checked' : '') + '> 跌</label> ' +
            '<label><input type="radio" name="' + name + '" value="watch"' + ((!d.user_judgment || d.user_judgment === 'watch') ? ' checked' : '') + '> 观望</label>' +
            '</td>';
        var reason = '<td><input type="text" class="aq-reason" data-idx="' + i + '" ' +
            'value="' + esc(d.user_reason || '') + '" placeholder="为什么这么判断"></td>';
        var pick = '<td><input type="checkbox" class="aq-pick" data-idx="' + i + '" checked></td>';

        if (_opts && _opts.mode === 'backfill') {
            return '<tr>' + pick +
                '<td><span class="aq-slot-tag">' + esc((d.hour_slot || d.ts || '').slice(0, 13)) + '</span></td>' +
                '<td style="white-space:nowrap"><strong>' + esc(d.instId) + '</strong></td>' +
                '<td>' + fmtPrice(d.price) + '</td>' + judge + reason + '</tr>';
        }
        var longText = dirCn(d.long_dir) +
            (d.long_dir_prev && d.long_dir_prev !== d.long_dir
                ? ' <span class="aq-dim">(上时段 ' + dirCn(d.long_dir_prev) + ')</span>' : '');
        return '<tr>' + pick +
            '<td style="white-space:nowrap"><strong>' + esc(d.instId) + '</strong></td>' +
            '<td>' + fmtPrice(d.price) + '</td>' +
            '<td><span class="' + dirCls(d.short_dir) + '">' + dirCn(d.short_dir) + '</span> ' +
            '<span class="aq-dim">' + esc(d.short_period || '') + '</span></td>' +
            '<td><span class="' + dirCls(d.long_dir) + '">' + longText + '</span> ' +
            '<span class="aq-dim">' + esc(d.long_period || '') + '</span></td>' +
            '<td>' + Number(d.atr_pct || 0).toFixed(2) + '%</td>' +
            judge + reason + '</tr>';
    }

    function render() {
        var thead = $('aq-thead'), tbody = $('aq-tbody');
        if (!thead || !tbody) { return; }
        thead.innerHTML = headHtml();
        if (!_items.length) {
            var span = (_opts && _opts.mode === 'backfill') ? 6 : 8;
            tbody.innerHTML = '<tr><td colspan="' + span + '" class="aq-empty">无可用数据，请检查交易配置或稍后重试</td></tr>';
            setBusy(false);
            return;
        }
        tbody.innerHTML = _items.map(rowHtml).join('');
        setBusy(false);
    }

    /* ---------- 取数 ---------- */

    function load() {
        var mode = (_opts && _opts.mode) || 'live';
        var reqId = ++_seq;
        if (_abort) { _abort.abort(); }
        _items = [];
        render();
        setBusy(true, '⏳ 取数中…');

        var url = mode === 'backfill'
            ? PREVIEW_URL + '?slots=' + encodeURIComponent(((_opts && _opts.slots) || []).join(','))
            : SNAP_BATCH_URL;
        if (mode === 'backfill' && !((_opts && _opts.slots) || []).length) {
            msg('未指定要回溯的小时槽', true);
            setBusy(false);
            return;
        }

        var t0 = performance.now();
        clearInterval(_tick);
        msg('正在串行拉取行情数据… 0s');
        _tick = setInterval(function () {
            if (reqId !== _seq) { clearInterval(_tick); return; }
            msg('正在串行拉取行情数据… ' + ((performance.now() - t0) / 1000).toFixed(0) + 's（约数秒/币，请勿关闭）');
        }, 500);

        var ctrl = new AbortController();
        _abort = ctrl;
        clearTimeout(_timer);
        _timer = setTimeout(function () { ctrl.abort(); }, FETCH_TIMEOUT_MS);

        getJSON(url, ctrl.signal).then(function (res) {
            if (reqId !== _seq) { return; }
            clearInterval(_tick);
            if (!res || res.code !== 200) {
                msg((res && res.message) || '取数失败', true);
                setBusy(false);
                return;
            }
            var secs = ((performance.now() - t0) / 1000).toFixed(1);
            if (mode === 'backfill') {
                var groups = (res.data && res.data.slots) || [];
                _items = [];
                groups.forEach(function (g) {
                    (g.items || []).forEach(function (it) {
                        it.hour_slot = g.hour_slot;
                        _items.push(it);
                    });
                });
                var miss = (res.data && res.data.errors) || [];
                msg('回溯取数完成：' + _items.length + ' 条 · 耗时 ' + secs + 's' +
                    (miss.length ? '；' + miss.length + ' 个币种历史K线不可用' : ''));
            } else {
                _items = (res.data && res.data.items) || [];
                var errs = (res.data && res.data.errors) || [];
                var m = '成功 ' + _items.length + ' 币种 · 耗时 ' + secs + 's';
                if (_items.length) { m += '（均 ' + (secs / _items.length).toFixed(1) + 's/币）'; }
                if (errs.length) {
                    m += '；失败 ' + errs.length + ' 个（' +
                        errs.map(function (e) { return e.instId || ('#' + e.index); }).join('、') + '）';
                }
                msg(m, !!errs.length);
            }
            render();
        }).catch(function (e) {
            if (reqId !== _seq) { return; }
            clearInterval(_tick);
            msg(e && e.name === 'AbortError' ? '请求超时或已取消，请点「重新取数」重试' : ('取数失败: ' + e), true);
            setBusy(false);
        });
    }

    /* ---------- 保存 ---------- */

    function collect() {
        var out = [];
        if (!_overlay) { return out; }
        _overlay.querySelectorAll('.aq-pick:checked').forEach(function (cb) {
            var i = parseInt(cb.dataset.idx, 10);
            var d = _items[i];
            if (!d) { return; }
            var radio = _overlay.querySelector('input[name="aq-j-' + i + '"]:checked');
            var reasonEl = _overlay.querySelector('.aq-reason[data-idx="' + i + '"]');
            out.push({
                instId: d.instId, ts: d.ts, price: d.price,
                short_period: d.short_period || '', long_period: d.long_period || '',
                short_dir: d.short_dir || '', long_dir: d.long_dir || '',
                long_dir_prev: d.long_dir_prev || '', atr_pct: d.atr_pct || 0,
                user_judgment: radio ? radio.value : 'watch',
                user_reason: reasonEl ? reasonEl.value.trim() : ''
            });
        });
        return out;
    }

    function save() {
        if (_saving) { return; }
        var picked = collect();
        if (!picked.length) { msg('请至少勾选一行要保存的记录', true); return; }
        _saving = true;
        setBusy(true, '⏳ 保存中…');
        msg('正在保存 ' + picked.length + ' 条分析记录…');

        postJSON(SAVE_URL, { items: picked }).then(function (res) {
            if (!res || res.code !== 200) {
                msg((res && res.message) || '保存失败', true);
                return;
            }
            var errs = (res.data && res.data.errors) || [];
            if (errs.length) {
                // 部分失败：保留矩阵供修正后重存（与分析页同一取向）
                msg(res.message + '；失败行：' + errs.map(function (e) {
                    var it = _items[e.index];
                    return (it ? it.instId : ('第' + e.index + '行')) + '——' + e.message;
                }).join('；'), true);
                return;
            }
            var ids = (res.data && res.data.ids) || [];
            var saved = picked.map(function (p, i) {
                return {
                    id: ids[i], ts: p.ts, inst_id: p.instId, price: p.price,
                    user_judgment: p.user_judgment, user_reason: p.user_reason,
                    hour_slot: String(p.ts || '').slice(0, 13)
                };
            });
            var cb = _opts && _opts.onSaved;
            close();
            if (typeof cb === 'function') {
                try { cb(saved); } catch (e) { /* 回调异常不影响已落库的结果 */ }
            }
        }).catch(function (e) {
            msg('保存失败: ' + e, true);
        }).finally(function () {
            _saving = false;
            setBusy(false);
        });
    }

    /* ---------- 生命周期 ---------- */

    function onKey(e) {
        if (e.key === 'Escape') { close(); }
    }

    function close() {
        _seq++;                                  // 作废在途请求
        if (_abort) { _abort.abort(); _abort = null; }
        clearInterval(_tick);
        clearTimeout(_timer);
        document.removeEventListener('keydown', onKey);
        if (_overlay && _overlay.parentNode) { _overlay.parentNode.removeChild(_overlay); }
        _overlay = null;
        _items = [];
        _saving = false;
    }

    function open(opts) {
        opts = opts || {};
        if (_overlay) { close(); }               // 同时只允许一个
        ensureStyle();
        _opts = {
            mode: opts.mode === 'backfill' ? 'backfill' : 'live',
            slots: opts.slots || [],
            onSaved: opts.onSaved,
            title: opts.title || '',
            hint: opts.hint || ''
        };
        _overlay = buildSkeleton();
        document.addEventListener('keydown', onKey);

        var back = _opts.mode === 'backfill';
        $('aq-title').textContent = _opts.title ||
            (back ? '🕰️ 回溯补记分析（用当时的真实价格）' : '📸 一键批量快照并记录');
        $('aq-hint').innerHTML = _opts.hint || (back
            ? '这些小时已经过去，价格取的是<b>当时的真实成交价</b>；策略方向无法在不引入未来数据的前提下重建，' +
              '因此留空——请只写<b>你现在的判断</b>。补记会被如实标记为 <code>backfill</code>，看板上会显示补记率。'
            : '一次拉取交易配置里的全部币种，逐行给出<b>你自己的</b>涨跌判断与原因，再一次性保存。' +
              '保存后本小时即视为已分析，打卡闸门自动放行。');
        load();
        return _overlay;
    }

    window.AnalysisQuick = {
        open: open,
        close: close,
        isOpen: function () { return !!_overlay; }
    };
})();
