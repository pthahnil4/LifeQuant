/* =====================================================================
 * 实盘分析记录 —— 快照生成 / 个人判断录入 / 动态复盘窗口 / 命中率统计
 * 从 task.html 内联脚本抽离为共享模块，供「定时任务」Tab 与独立页 /analysis 复用。
 * 交互逻辑（计时、并发请求序号 token、AbortController 超时、动态复盘窗口）原样保留，勿改行为。
 * ===================================================================== */
(function () {
    /* ---- 局部工具：避免污染全局；两页各自提供 #toast 容器与 .task-toast 样式(style.css) ---- */
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function showToast(msg, type) {
        const t = document.getElementById('toast');
        if (!t) { return; }
        t.textContent = msg;
        t.className = 'task-toast toast-' + (type || 'info') + ' show';
        setTimeout(() => { t.classList.remove('show'); }, 3500);
    }

    let anaInited = false;
    let _anaAccount = '';      // 分析功能当前绑定的交易账号（与「交易配置」页下拉同源，随每次进入同步）

    let _anaSnapshot = null;   // 当前快照数据（保存时随表单提交）
    let _anaRecords = [];      // 当前列表数据（编辑/删除时用 id 索引）
    let _anaSnapSeq = 0;       // 单币快照请求序号：丢弃过期响应，防快速连点竞态
    let _anaSnapAbort = null;  // 在途请求句柄（关闭/重发时取消）
    let _anaSnapTimer = null;  // 超时定时器：防网络挂起永久卡加载
    let _anaSaveBusy = false;  // 单条保存防重复提交
    let _anaSnapTick = null;   // 单币快照实时计时器（每 0.5s 刷新已用秒数）
    let _anaBatchTick = null;  // 批量快照实时计时器
    let _anaReviewMult = {near: 4, far: 8};  // 复盘窗口倍数（由后端 stats 回传，前端标签据此渲染）
    // 命中率中性带配置（严格镜像后端 HIT_ATR_K/HIT_FLOOR_PCT/HIT_FAR_SCALE，由 stats 回传校准）
    let _anaHitCfg = {k: 0.3, floor: 0.1, farScale: Math.SQRT2};
    // 先触碰口径（批次16）：判断/方向 → 期望的路径结果；命中=期望类别与实际一致（镜像后端 _TOUCH_EXPECT/_DIR_TOUCH_EXPECT）
    const _JUDG_TOUCH = {rise: 'up', fall: 'down', watch: 'none'};
    const _DIR_TOUCH = {long: 'up', short: 'down'};
    const _TOUCH_LABEL = {up: '↑触', down: '↓触', none: '横盘', conflict: '双触'};

    // 中性带阈值 θ(%)：θ = max(地板, k × atr_pct × 窗口缩放)，远窗口按 √时长比放大（镜像后端 neutral_theta）
    function anaTheta(atrPct, isFar) {
        const scale = isFar ? _anaHitCfg.farScale : 1.0;
        return Math.max(_anaHitCfg.floor, _anaHitCfg.k * scale * (Number(atrPct) || 0));
    }

    // 把 '5m'/'15m'/'1H' 等周期串解析为分钟数（与后端 period_to_minutes 对齐）
    function _anaPeriodMin(p) {
        const m = /^\s*(\d+)\s*([mMhHwWdD])\s*$/.exec(String(p || ''));
        if (!m) return 0;
        const mult = {m: 1, h: 60, w: 10080, d: 1440}[m[2].toLowerCase()] || 0;
        return parseInt(m[1], 10) * mult;
    }

    // 分钟数格式化为简短时长标签：45→'45m'，60→'1H'，90→'1.5H'，120→'2H'
    function _anaFmtDur(min) {
        if (!min || min < 60) return (min || 0) + 'm';
        const h = min / 60;
        return (Number.isInteger(h) ? h : h.toFixed(1)) + 'H';
    }

    // 依据记录短周期算出近/远窗口（分钟 + 对应 DB 列）
    function _anaWindows(shortPeriod) {
        const sm = _anaPeriodMin(shortPeriod);
        if (sm <= 0) {
            return {near: {min: 60, col: 'price_1h', ts: 'ts_1h'},
                    far: {min: 240, col: 'price_4h', ts: 'ts_4h'}};
        }
        return {near: {min: sm * _anaReviewMult.near, col: 'price_1h', ts: 'ts_1h'},
                far: {min: sm * _anaReviewMult.far, col: 'price_4h', ts: 'ts_4h'}};
    }

    // 同步列表表头的窗口标签（倍数变化时）
    function _anaSyncReviewHeaders() {
        const nth = document.getElementById('ana-th-near');
        const fth = document.getElementById('ana-th-far');
        if (nth) nth.textContent = _anaReviewMult.near + '×短周期后';
        if (fth) fth.textContent = _anaReviewMult.far + '×短周期后';
    }

    // 价格精度显示规范：<10 保町3位 / 10~100 保町2位 / 100~1000 保町1位 / >1000 取整
    function anaFmtPrice(v) {
        const n = Number(v);
        if (!isFinite(n) || n <= 0) return '—';
        if (n < 10) return n.toFixed(3);
        if (n < 100) return n.toFixed(2);
        if (n < 1000) return n.toFixed(1);
        return Math.round(n).toString();
    }

    function anaDirBadge(dir, withPrev) {
        if (dir === 'long') return '<span class="badge badge-long">看多</span>';
        if (dir === 'short') return '<span class="badge badge-short">看空</span>';
        return '<span class="badge badge-watch">--</span>';
    }

    function anaJudgmentBadge(j) {
        if (j === 'rise') return '<span class="badge badge-long">涨</span>';
        if (j === 'fall') return '<span class="badge badge-short">跌</span>';
        return '<span class="badge badge-watch">观望</span>';
    }

    // 快照请求的账号参数：与币种列表同源（交易配置页选了哪个账号，快照就按哪个账号读配置）。
    // 独立页 /analysis 无账号下拉 → 传空 → 后端按运行/默认账号解析，仍与交易配置页默认口径一致。
    function _anaAccountParam(sep) {
        return _anaAccount ? (sep + 'account=' + encodeURIComponent(_anaAccount)) : '';
    }

    // 每次进入分析页/Tab 都重新拉取交易配置，同步币种下拉列表（在「交易配置」Tab
    // 增删保存后回到这里不会残留旧币种）；主/筛选两个下拉都整体重建，避免重复累积，
    // 并尽量保留上一次的选择。账号取「交易配置」页下拉当前值（task.html 有 #trading-account；
    // 独立页无此控件则留空由后端按默认账号解析），确保两页币种完全一致。
    function reloadAnalysisCoins() {
        const acctSel = document.getElementById('trading-account');
        _anaAccount = acctSel ? String(acctSel.value || '') : '';
        fetch('/api/task/config/trading' + _anaAccountParam('?')).then(r => r.json()).then(res => {
            if (res.code !== 200) { showToast('加载币种列表失败: ' + res.message, 'error'); return; }
            const coins = (res.data.currencies || []).map(c => c.instId).filter(Boolean);
            const sel = document.getElementById('ana-inst');
            const fSel = document.getElementById('ana-filter-inst');
            const cur = sel.value, curFilter = fSel.value;
            // 主选择器：整体重建
            sel.innerHTML = '';
            coins.forEach(id => {
                const opt = document.createElement('option');
                opt.value = id; opt.textContent = id;
                sel.appendChild(opt);
            });
            if (!coins.length) { sel.innerHTML = '<option value="">无已配置币种</option>'; }
            // 筛选选择器：保留首项「全部」，清空其余后按最新币种重建，避免重复累积
            Array.prototype.slice.call(fSel.options, 1).forEach(o => o.remove());
            coins.forEach(id => {
                const opt = document.createElement('option');
                opt.value = id; opt.textContent = id;
                fSel.appendChild(opt);
            });
            // 尽量保留上次选择（币种已被删除时自动回落到首项）
            if (coins.indexOf(cur) >= 0) { sel.value = cur; }
            if (curFilter === '' || coins.indexOf(curFilter) >= 0) { fSel.value = curFilter; }
        }).catch(e => showToast('加载币种列表失败: ' + e, 'error'));
    }

    function initAnalysisTab() {
        const firstTime = !anaInited;
        anaInited = true;
        reloadAnalysisCoins();
        if (firstTime) { loadAnalysisRecords(); }
    }

    function genAnalysisSnapshot() {
        const inst = document.getElementById('ana-inst').value;
        if (!inst) { showToast('请先选择币种', 'error'); return; }
        const btn = document.getElementById('ana-snap-btn');
        const tsEl = document.getElementById('ana-snap-ts');
        const reqId = ++_anaSnapSeq;
        if (_anaSnapAbort) { _anaSnapAbort.abort(); }   // 取消上一个在途请求
        btn.disabled = true;
        btn.textContent = '⏳ 快照生成中...';
        // 旧快照即刻作废：请求在途时点保存会提示重新生成，避免币种错配误存
        _anaSnapshot = null;
        document.getElementById('ana-snapshot-box').style.display = 'none';
        const t0 = performance.now();
        clearInterval(_anaSnapTick);
        tsEl.textContent = '分析中... 0s（首次需拉取K线，约数秒）';
        _anaSnapTick = setInterval(function() {
            if (reqId !== _anaSnapSeq) { clearInterval(_anaSnapTick); return; }
            tsEl.textContent = '分析中... ' + ((performance.now() - t0) / 1000).toFixed(0) + 's（拉K线+双周期计算）';
        }, 500);
        const ctrl = new AbortController();
        _anaSnapAbort = ctrl;
        clearTimeout(_anaSnapTimer);
        _anaSnapTimer = setTimeout(function() { ctrl.abort(); }, 120000);
        fetch('/api/task/analysis/snapshot?instId=' + encodeURIComponent(inst) + _anaAccountParam('&'),
              {signal: ctrl.signal})
            .then(r => r.json()).then(res => {
                if (reqId !== _anaSnapSeq) { return; }   // 已有更新的请求，丢弃过期响应
                clearInterval(_anaSnapTick);
                if (res.code !== 200) {
                    tsEl.textContent = '';
                    showToast('快照失败: ' + res.message, 'error');
                    return;
                }
                _anaSnapshot = res.data;
                renderAnalysisSnapshot(res.data);
                const secs = ((performance.now() - t0) / 1000).toFixed(1);
                tsEl.textContent = '快照时间：' + res.data.ts + ' · 耗时 ' + secs + 's';
                showToast('快照生成完成，耗时 ' + secs + 's', 'success');
            }).catch(e => {
                if (reqId !== _anaSnapSeq) { return; }
                clearInterval(_anaSnapTick);
                tsEl.textContent = '';
                showToast('快照失败: ' + (e && e.name === 'AbortError' ? '请求超时，请点击重试' : e), 'error');
            }).finally(function() {
                if (reqId === _anaSnapSeq) {
                    clearInterval(_anaSnapTick);
                    btn.disabled = false;
                    btn.textContent = '📸 生成快照';
                }
            });
    }

    function renderAnalysisSnapshot(d) {
        const dirCn = v => v === 'long' ? '看多' : (v === 'short' ? '看空' : '--');
        const dirCls = v => v === 'long' ? 'cc-green' : (v === 'short' ? 'cc-red' : '');
        const longText = dirCn(d.long_dir) +
            (d.long_dir_prev && d.long_dir_prev !== d.long_dir
                ? ' <span style="font-size:0.72rem;color:#999">(上一时段 ' + dirCn(d.long_dir_prev) + ')</span>'
                : (d.long_dir_prev ? ' <span style="font-size:0.72rem;color:#999">(与上一时段一致)</span>' : ''));
        document.getElementById('ana-snap-grid').innerHTML =
            '<div class="ana-snap-item"><div class="as-label">实时价格</div><div class="as-value">' + anaFmtPrice(d.price) + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">短周期方向 (' + escapeHtml(d.short_period || '') + ')</div><div class="as-value ' + dirCls(d.short_dir) + '">' + dirCn(d.short_dir) + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">长周期方向 (' + escapeHtml(d.long_period || '') + ')</div><div class="as-value ' + dirCls(d.long_dir) + '">' + longText + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">ATR%</div><div class="as-value">' + Number(d.atr_pct || 0).toFixed(2) + '%</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">BOLL 上轨</div><div class="as-value">' + anaFmtPrice(d.boll_upper) + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">BOLL 中轨</div><div class="as-value">' + anaFmtPrice(d.boll_middle) + '</div></div>' +
            '<div class="ana-snap-item"><div class="as-label">BOLL 下轨</div><div class="as-value">' + anaFmtPrice(d.boll_lower) + '</div></div>';
        document.getElementById('ana-snap-ts').textContent = '快照时间：' + d.ts;
        document.getElementById('ana-snapshot-box').style.display = '';
    }

    function hideAnalysisSnapshot() {
        document.getElementById('ana-snapshot-box').style.display = 'none';
        document.getElementById('ana-snap-ts').textContent = '';
        _anaSnapshot = null;
    }

    function saveAnalysisRecord() {
        if (_anaSaveBusy) { return; }
        if (!_anaSnapshot) { showToast('请先生成快照', 'error'); return; }
        const judgment = (document.querySelector('input[name="ana-judgment"]:checked') || {}).value || 'watch';
        // 结构价计划（可选，成对）：仅涨/跌可挂；镜像后端 _clean_plan_pair 基础校验
        const targetRaw = (document.getElementById('ana-target') || {}).value;
        const invalidRaw = (document.getElementById('ana-invalid') || {}).value;
        const tStr = String(targetRaw == null ? '' : targetRaw).trim();
        const iStr = String(invalidRaw == null ? '' : invalidRaw).trim();
        if ((tStr === '') !== (iStr === '')) { showToast('结构价需成对填写：目标价与失效价都要有', 'error'); return; }
        if (tStr && judgment === 'watch') { showToast('仅涨/跌判断可挂结构价计划', 'error'); return; }
        const planT = tStr ? Number(tStr) : null;
        const planI = iStr ? Number(iStr) : null;
        if (tStr && !(planT > 0)) { showToast('结构价必须大于 0', 'error'); return; }
        if (iStr && !(planI > 0)) { showToast('结构价必须大于 0', 'error'); return; }
        if (planT && planI && planT === planI) { showToast('目标价与失效价不能相同', 'error'); return; }
        const body = {
            instId: _anaSnapshot.instId,
            ts: _anaSnapshot.ts,
            price: _anaSnapshot.price,
            short_period: _anaSnapshot.short_period,
            long_period: _anaSnapshot.long_period,
            short_dir: _anaSnapshot.short_dir,
            long_dir: _anaSnapshot.long_dir,
            long_dir_prev: _anaSnapshot.long_dir_prev,
            atr_pct: _anaSnapshot.atr_pct,
            user_judgment: judgment,
            user_reason: document.getElementById('ana-reason').value.trim(),
            target_price: planT,
            invalid_price: planI
        };
        _anaSaveBusy = true;
        fetch('/api/task/analysis/records', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        }).then(r => r.json()).then(res => {
            showToast(res.message, res.code === 200 ? 'success' : 'error');
            if (res.code === 200) {
                // 用快照 + 返回的自增 id 在本地构造新记录，命中筛选则插到表首（不再整表重拉重建）
                const rec = {
                    id: (res.data && res.data.id), ts: _anaSnapshot.ts, inst_id: _anaSnapshot.instId,
                    price: _anaSnapshot.price, short_period: _anaSnapshot.short_period, long_period: _anaSnapshot.long_period,
                    short_dir: _anaSnapshot.short_dir, long_dir: _anaSnapshot.long_dir, long_dir_prev: _anaSnapshot.long_dir_prev,
                    atr_pct: _anaSnapshot.atr_pct, user_judgment: judgment, user_reason: body.user_reason,
                    price_1h: null, ts_1h: null, price_4h: null, ts_4h: null,
                    touch_near: null, touch_near_ts: null, touch_far: null, touch_far_ts: null,
                    target_price: planT, invalid_price: planI, plan_result: null, plan_r: null, plan_touch_ts: null
                };
                document.getElementById('ana-reason').value = '';
                if (document.getElementById('ana-target')) { document.getElementById('ana-target').value = ''; }
                if (document.getElementById('ana-invalid')) { document.getElementById('ana-invalid').value = ''; }
                hideAnalysisSnapshot();
                if (anaMatchesFilter(rec)) {
                    _anaRecords.unshift(rec);
                    anaInsertRowTop(rec);
                    refreshAnaStats();
                }
            }
        }).catch(e => showToast('保存失败: ' + e, 'error'))
          .finally(function() { _anaSaveBusy = false; });
    }

    // ============================================================
    // 批量快照矩阵：一键拉全部币种 → 逐行填判断/原因 → 一次性提交
    // ============================================================
    let _anaBatchItems = [];   // 当前矩阵的原始快照数据
    let _anaBatchSeq = 0;      // 批量请求序号：关闭/再次触发后丢弃在途响应
    let _anaBatchAbort = null; // 在途批量请求句柄（关闭/重发时取消）
    let _anaBatchTimer = null; // 超时定时器：防网络挂起按钮永久卡加载
    let _anaBatchSaveBusy = false; // 批量保存防重复提交

    function genBatchSnapshot(btn) {
        // 触发时再同步一次账号（下拉可能已切换但尚未重新进入分析 Tab）
        const _acctSel = document.getElementById('trading-account');
        if (_acctSel) { _anaAccount = String(_acctSel.value || ''); }
        const box = document.getElementById('ana-batch-box');
        const msgEl = document.getElementById('ana-batch-msg');
        const reqId = ++_anaBatchSeq;
        if (_anaBatchAbort) { _anaBatchAbort.abort(); }   // 取消上一个在途请求
        if (btn) { btn.disabled = true; btn.textContent = '⏳ 批量快照中...'; }
        box.style.display = '';
        document.getElementById('ana-batch-count').textContent = '0';
        document.getElementById('ana-batch-tbody').innerHTML =
            '<tr><td colspan="9" class="empty-state">加载中...</td></tr>';
        const t0 = performance.now();
        clearInterval(_anaBatchTick);
        msgEl.textContent = '正在串行拉取全部币种数据... 0s';
        _anaBatchTick = setInterval(function() {
            if (reqId !== _anaBatchSeq) { clearInterval(_anaBatchTick); return; }
            msgEl.textContent = '正在串行拉取全部币种数据... ' + ((performance.now() - t0) / 1000).toFixed(0) + 's（约数秒/币）';
        }, 500);
        const ctrl = new AbortController();
        _anaBatchAbort = ctrl;
        clearTimeout(_anaBatchTimer);
        _anaBatchTimer = setTimeout(function() { ctrl.abort(); }, 180000);
        fetch('/api/task/analysis/snapshot_batch' + _anaAccountParam('?'), {signal: ctrl.signal})
            .then(r => r.json()).then(res => {
                if (reqId !== _anaBatchSeq) { return; }   // 面板已关闭/已重新触发，丢弃过期响应
                clearInterval(_anaBatchTick);
                if (res.code !== 200) {
                    msgEl.textContent = '';
                    showToast('批量快照失败: ' + res.message, 'error');
                    return;
                }
                const items = res.data.items || [];
                const errors = res.data.errors || [];
                _anaBatchItems = items;
                renderBatchMatrix(items);
                document.getElementById('ana-batch-count').textContent = items.length;
                const secs = ((performance.now() - t0) / 1000).toFixed(1);
                let m = '成功 ' + items.length + ' 币种 · 总耗时 ' + secs + 's';
                if (items.length) { m += '（均 ' + (secs / items.length).toFixed(1) + 's/币）'; }
                if (errors.length) {
                    m += '，失败 ' + errors.length + ' 个（' + errors.map(e => e.instId || ('#' + e.index)).join('、') + '）';
                }
                msgEl.textContent = m;
                showToast('批量快照完成，总耗时 ' + secs + 's' + (errors.length ? '（部分失败）' : ''), errors.length ? 'error' : 'success');
            }).catch(e => {
                if (reqId !== _anaBatchSeq) { return; }
                clearInterval(_anaBatchTick);
                msgEl.textContent = '';
                showToast('批量快照失败: ' + (e && e.name === 'AbortError' ? '请求超时或已取消，请点击重试' : e), 'error');
            }).finally(function() {
                if (reqId === _anaBatchSeq) {
                    clearInterval(_anaBatchTick);
                    if (btn) { btn.disabled = false; btn.textContent = '🚀 一键批量快照'; }
                }
            });
    }

    function renderBatchMatrix(items) {
        const tbody = document.getElementById('ana-batch-tbody');
        if (!items.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="empty-state">无可用币种，请检查交易配置</td></tr>';
            return;
        }
        const dirCn = v => v === 'long' ? '看多' : (v === 'short' ? '看空' : '--');
        const dirCls = v => v === 'long' ? 'cc-green' : (v === 'short' ? 'cc-red' : '');
        tbody.innerHTML = items.map((d, i) => {
            const longText = dirCn(d.long_dir) +
                (d.long_dir_prev && d.long_dir_prev !== d.long_dir
                    ? ' <span style="font-size:0.72rem;color:#999">(上时段 ' + dirCn(d.long_dir_prev) + ')</span>'
                    : (d.long_dir_prev ? ' <span style="font-size:0.72rem;color:#999">(与上时段一致)</span>' : ''));
            return '<tr>' +
                '<td><input type="checkbox" class="ana-batch-pick" data-idx="' + i + '" checked></td>' +
                '<td style="white-space:nowrap"><strong>' + escapeHtml(d.instId) + '</strong></td>' +
                '<td>' + anaFmtPrice(d.price) + '</td>' +
                '<td><span class="' + dirCls(d.short_dir) + '">' + dirCn(d.short_dir) + '</span> ' +
                    '<span style="font-size:0.7rem;color:#999">' + escapeHtml(d.short_period || '') + '</span></td>' +
                '<td><span class="' + dirCls(d.long_dir) + '">' + longText + '</span> ' +
                    '<span style="font-size:0.7rem;color:#999">' + escapeHtml(d.long_period || '') + '</span></td>' +
                '<td>' + Number(d.atr_pct || 0).toFixed(2) + '%</td>' +
                '<td style="font-size:0.78rem;color:#555;white-space:nowrap">' +
                    anaFmtPrice(d.boll_upper) + ' / ' + anaFmtPrice(d.boll_middle) + ' / ' + anaFmtPrice(d.boll_lower) + '</td>' +
                '<td style="white-space:nowrap">' +
                    '<label><input type="radio" name="ana-batch-j-' + i + '" value="rise"> 涨</label> ' +
                    '<label><input type="radio" name="ana-batch-j-' + i + '" value="fall"> 跌</label> ' +
                    '<label><input type="radio" name="ana-batch-j-' + i + '" value="watch" checked> 观望</label></td>' +
                '<td><input type="text" class="config-input ana-batch-reason" data-idx="' + i + '" ' +
                    'placeholder="分析原因(可选)" style="width:100%;min-width:130px"></td>' +
                '</tr>';
        }).join('');
    }

    function toggleAllBatchRows(pick) {
        document.querySelectorAll('.ana-batch-pick').forEach(cb => { cb.checked = !!pick; });
    }

    function hideBatchSnapshot() {
        _anaBatchSeq++;                              // 作废在途请求（响应将被丢弃）
        if (_anaBatchAbort) { _anaBatchAbort.abort(); _anaBatchAbort = null; }
        const b = document.getElementById('ana-batch-btn');
        if (b) { b.disabled = false; b.textContent = '🚀 一键批量快照'; }
        document.getElementById('ana-batch-box').style.display = 'none';
        document.getElementById('ana-batch-tbody').innerHTML = '';
        document.getElementById('ana-batch-msg').textContent = '';
        _anaBatchItems = [];
    }

    function saveBatchSnapshots() {
        if (_anaBatchSaveBusy) { return; }
        if (!_anaBatchItems.length) { showToast('请先生成批量快照', 'error'); return; }
        const picked = [];
        document.querySelectorAll('.ana-batch-pick:checked').forEach(cb => {
            const idx = parseInt(cb.dataset.idx, 10);
            const d = _anaBatchItems[idx];
            if (!d) return;
            const radio = document.querySelector('input[name="ana-batch-j-' + idx + '"]:checked');
            const reasonEl = document.querySelector('.ana-batch-reason[data-idx="' + idx + '"]');
            picked.push({
                instId: d.instId, ts: d.ts, price: d.price,
                short_period: d.short_period, long_period: d.long_period,
                short_dir: d.short_dir, long_dir: d.long_dir, long_dir_prev: d.long_dir_prev,
                atr_pct: d.atr_pct, user_judgment: radio ? radio.value : 'watch',
                user_reason: reasonEl ? reasonEl.value.trim() : ''
            });
        });
        if (!picked.length) { showToast('请至少勾选一行要保存的记录', 'error'); return; }
        const msgEl = document.getElementById('ana-batch-msg');
        _anaBatchSaveBusy = true;
        msgEl.textContent = '正在批量保存（' + picked.length + ' 条）...';
        fetch('/api/task/analysis/records_batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({items: picked})
        }).then(r => r.json()).then(res => {
            showToast(res.message, res.code === 200 ? 'success' : 'error');
            if (res.code !== 200) { msgEl.textContent = ''; return; }
            const errors = (res.data && res.data.errors) || [];
            if (errors.length) {
                // 部分失败：保留矩阵供修正后重存，失败行明细展示在矩阵底部
                msgEl.textContent = res.message + '；失败行：' + errors.map(e => {
                    const it = _anaBatchItems[e.index];
                    return (it ? it.instId : ('第' + e.index + '行')) + '——' + e.message;
                }).join('；');
                showToast('部分行保存失败，请修正后重新勾选保存', 'error');
            } else {
                // 全量成功：用勾选行 + 返回 ids 在本地构造记录，命中筛选的插到表首（不再整表重拉重建）
                const ids = (res.data && res.data.ids) || [];
                const newRecs = picked.map(function(p, i) {
                    return {
                        id: ids[i], ts: p.ts, inst_id: p.instId, price: p.price,
                        short_period: p.short_period, long_period: p.long_period,
                        short_dir: p.short_dir, long_dir: p.long_dir, long_dir_prev: p.long_dir_prev,
                        atr_pct: p.atr_pct, user_judgment: p.user_judgment, user_reason: p.user_reason,
                        price_1h: null, ts_1h: null, price_4h: null, ts_4h: null,
                        touch_near: null, touch_near_ts: null, touch_far: null, touch_far_ts: null,
                        target_price: null, invalid_price: null, plan_result: null, plan_r: null, plan_touch_ts: null
                    };
                }).filter(function(r) { return r.id !== undefined; });
                hideBatchSnapshot();
                const add = newRecs.filter(anaMatchesFilter);
                if (add.length) {
                    _anaRecords = add.concat(_anaRecords);
                    const tbody = document.getElementById('ana-tbody');
                    if (!tbody.querySelector('tr[data-id]')) { tbody.innerHTML = ''; }
                    tbody.insertAdjacentHTML('afterbegin', add.map(anaRowHtml).join(''));
                    refreshAnaStats();
                }
            }
        }).catch(e => {
            msgEl.textContent = '';
            showToast('批量保存失败: ' + e, 'error');
        }).finally(function() {
            _anaBatchSaveBusy = false;
        });
    }

    function anaFilterParams() {
        const params = [];
        const inst = document.getElementById('ana-filter-inst').value;
        const judgment = document.getElementById('ana-filter-judgment').value;
        const start = document.getElementById('ana-filter-start').value;
        const end = document.getElementById('ana-filter-end').value;
        if (inst) params.push('instId=' + encodeURIComponent(inst));
        if (judgment) params.push('judgment=' + judgment);
        if (start) params.push('start=' + encodeURIComponent(start + ' 00:00:00'));
        if (end) params.push('end=' + encodeURIComponent(end + ' 23:59:59'));
        return params.length ? ('?' + params.join('&')) : '';
    }

    function loadAnalysisRecords() {
        const tbody = document.getElementById('ana-tbody');
        tbody.innerHTML = '<tr><td colspan="13" class="empty-state">加载中...（首次可能需回填复盘价格，稍慢）</td></tr>';
        fetch('/api/task/analysis/records' + anaFilterParams()).then(r => r.json()).then(res => {
            if (res.code !== 200) {
                tbody.innerHTML = '<tr><td colspan="13" class="empty-state">加载失败：' + escapeHtml(res.message || '') + '</td></tr>';
                return;
            }
            _anaRecords = res.data.records || [];
            renderAnalysisStats(res.data.stats);
            renderAnalysisTable(_anaRecords);
        }).catch(e => {
            tbody.innerHTML = '<tr><td colspan="13" class="empty-state">加载失败：' + escapeHtml(String(e)) + '</td></tr>';
        });
    }

    function renderAnalysisStats(stats) {
        const box = document.getElementById('ana-stats');
        if (!stats) { box.innerHTML = ''; renderAnaConfusion(null); return; }
        if (stats.near_mult) { _anaReviewMult.near = stats.near_mult; }
        if (stats.far_mult) { _anaReviewMult.far = stats.far_mult; }
        // 同步中性带配置：本地重算(computeStatsLocal)与结果列(anaResultCell)据此与后端对齐口径
        if (stats.hit_k !== undefined) { _anaHitCfg.k = stats.hit_k; }
        if (stats.hit_floor_pct !== undefined) { _anaHitCfg.floor = stats.hit_floor_pct; }
        if (stats.hit_far_scale !== undefined) { _anaHitCfg.farScale = stats.hit_far_scale; }
        _anaSyncReviewHeaders();
        const nLabel = _anaReviewMult.near + '×短周期';
        const fLabel = _anaReviewMult.far + '×短周期';
        const bucketText = b => b && (b.scored + b.open + b.pending) > 0
            ? (b.scored > 0 ? b.rate + '%' : '—')
              + ' <span style="font-size:0.72rem;color:#999;font-weight:400">(✓' + b.hit + ' ✗' + b.miss
              + ' ◔' + b.open + (b.pending ? ' ⏳' + b.pending : '') + ')</span>'
            : '—';
        const nearAvg = (stats.user.near && stats.user.near.chg_n > 0)
            ? ((stats.user.near.avg_pct > 0 ? '+' : '') + stats.user.near.avg_pct + '%') : '—';
        const plan = stats.plan || {total: 0, win: 0, loss: 0, open: 0, gap: 0, pending: 0, win_rate: 0, avg_r: 0};
        const decided = plan.win + plan.loss;
        const planText = plan.total > 0
            ? (decided > 0 ? plan.win_rate + '%' : '—')
              + ' <span style="font-size:0.72rem;color:#999;font-weight:400">(✓' + plan.win + ' ✗' + plan.loss
              + ' ◔' + (plan.open + plan.pending) + (plan.gap ? ' ⌛' + plan.gap : '')
              + ' · 均R ' + (decided > 0 ? Number(plan.avg_r).toFixed(2) : '—') + ')</span>'
            : '—';
        const cards = [
            ['记录总数', stats.total, 'cc-blue'],
            ['先触碰命中·个人 (' + nLabel + ')', bucketText(stats.user.near), 'cc-orange'],
            ['先触碰命中·个人 (' + fLabel + ')', bucketText(stats.user.far), 'cc-orange'],
            ['先触碰命中·策略 (' + nLabel + ')', bucketText(stats.strategy.near), 'cc-green'],
            ['先触碰命中·策略 (' + fLabel + ')', bucketText(stats.strategy.far), 'cc-green'],
            ['结构价计划兑现', planText, decided > 0 ? (plan.avg_r >= 0 ? 'cc-green' : 'cc-red') : 'cc-blue'],
            ['端点参考均涨 (' + nLabel + ')', nearAvg,
                (stats.user.near && stats.user.near.chg_n > 0 && stats.user.near.avg_pct >= 0) ? 'cc-green' : 'cc-red']
        ];
        box.innerHTML = cards.map(c =>
            '<div class="compare-card"><div class="cc-label">' + c[0] + '</div><div class="cc-value ' + c[2] + '">' + c[1] + '</div></div>'
        ).join('');
        renderAnaConfusion(stats);
    }

    // 混淆矩阵：行=你的判断，列=路径实际(先触上/先触下/全程未触/同根双触)，对角线判对(绿)、非对角判错(红)。
    // 内联样式渲染，兼容独立页(analysis.html .ana-stats)与 Tab 页(task.html .compare-cards)两套容器。
    function renderAnaConfusion(stats) {
        const box = document.getElementById('ana-confusion');
        if (!box) { return; }
        if (!stats || !stats.confusion) { box.innerHTML = ''; return; }
        const rows = [['rise', '判涨'], ['watch', '判观望'], ['fall', '判跌']];
        const cols = [['up', '↑先触'], ['down', '↓先触'], ['none', '横盘未触'], ['conflict', '双触']];
        const nLabel = _anaReviewMult.near + '×短周期';
        const fLabel = _anaReviewMult.far + '×短周期';
        function matrix(conf) {
            let h = '<table style="border-collapse:collapse;font-size:0.8rem;text-align:center">';
            h += '<tr><td style="padding:3px 8px"></td>' +
                cols.map(c => '<td style="padding:3px 8px;color:#999;font-weight:600">' + c[1] + '</td>').join('') + '</tr>';
            rows.forEach(function(r) {
                h += '<tr><td style="padding:3px 8px;color:#999;font-weight:600;text-align:right;white-space:nowrap">' + r[1] + '</td>';
                cols.forEach(function(c) {
                    const v = (conf[r[0]] && conf[r[0]][c[0]]) || 0;
                    const diag = _JUDG_TOUCH[r[0]] === c[0];
                    const bg = v === 0 ? '#fafafa' : (diag ? '#d4edda' : '#f8d7da');
                    const color = v === 0 ? '#ccc' : (diag ? '#155724' : '#721c24');
                    h += '<td style="padding:3px 8px;min-width:34px;border:1px solid #eee;background:' + bg +
                        ';color:' + color + ';font-weight:600">' + v + '</td>';
                });
                h += '</tr>';
            });
            return h + '</table>';
        }
        const k = (stats.hit_k !== undefined ? stats.hit_k : _anaHitCfg.k);
        const fs = Number(stats.hit_far_scale !== undefined ? stats.hit_far_scale : _anaHitCfg.farScale);
        box.innerHTML =
            '<div style="font-size:0.82rem;font-weight:700;color:#555;margin-bottom:8px">🔀 判断混淆矩阵（先触碰口径 · 定位你最常犯哪种误判）</div>' +
            '<div style="display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start">' +
            '<div><div style="font-size:0.78rem;font-weight:600;margin-bottom:5px;color:#777">近窗口(' + nLabel + ')</div>' + matrix(stats.confusion.near || {}) + '</div>' +
            '<div><div style="font-size:0.78rem;font-weight:600;margin-bottom:5px;color:#777">远窗口(' + fLabel + ')</div>' + matrix(stats.confusion.far || {}) + '</div>' +
            '<div style="font-size:0.74rem;color:#999;max-width:300px;line-height:1.7">' +
            '行=你的判断，列=窗口内 K 线路径<b>谁先触 P0±θ 带</b>（θ=' + k + '×ATR%，远窗口×' + fs.toFixed(2) + '）：' +
            '<b>横盘未触</b>=全程在带内；<b>双触</b>=同一根 bar 上下双触（无法定先后）。' +
            '涨/跌判断遇「横盘/双触」计<b>未兑现</b>不进命中率；观望的对角线是「横盘未触」。' +
            '<span style="color:#155724;font-weight:600">绿=判对</span>、<span style="color:#721c24;font-weight:600">红=判错</span>。</div>' +
            '</div>';
    }

    function anaReviewCell(rec, priceCol, tsCol, winMin) {
        const follow = rec[priceCol];
        const tip = (winMin ? (_anaFmtDur(winMin) + '后') : '') + (rec[tsCol] ? (' · 取价K线：' + rec[tsCol]) : '');
        if (follow === null || follow === undefined) return '<span class="ana-pending" title="' + tip + '">待回填</span>';
        const chg = rec.price > 0 ? (follow / rec.price - 1) * 100 : 0;
        const cls = chg > 0 ? 'cc-green' : (chg < 0 ? 'cc-red' : '');
        return '<div class="ana-review-cell" title="' + tip + '">' + anaFmtPrice(follow) +
            '<br><span class="' + cls + '">' + (chg > 0 ? '+' : '') + chg.toFixed(2) + '%</span></div>';
    }

    function anaTouchCell(rec) {
        // 先触碰判定（计分主口径）：窗口内 K 线路径谁先触 P0±θ 带判谁；
        // rise/fall：先触期望边✓、对侧✗、横盘/双触◔未兑现；watch：横盘✓、↑↓错失✗、双触◔
        const w = _anaWindows(rec.short_period);
        const expect = _JUDG_TOUCH[rec.user_judgment] || '';
        const parts = [];
        [['near', w.near.min, 'touch_near', 'touch_near_ts'], ['far', w.far.min, 'touch_far', 'touch_far_ts']].forEach(function(t) {
            const dur = _anaFmtDur(t[1]);
            const res = rec[t[2]];
            if (res !== 'up' && res !== 'down' && res !== 'none' && res !== 'conflict') {
                parts.push('<span class="ana-pending" title="窗口路径判定待回填">' + dur + '待</span>');
                return;
            }
            let verdict;
            if (expect === 'none') { verdict = res === 'none' ? 'hit' : (res === 'conflict' ? 'open' : 'miss'); }
            else if (res === 'none' || res === 'conflict') { verdict = 'open'; }
            else { verdict = res === expect ? 'hit' : 'miss'; }
            const cls = verdict === 'hit' ? 'ana-hit' : (verdict === 'miss' ? 'ana-miss' : 'ana-open');
            const icon = verdict === 'hit' ? '✓' : (verdict === 'miss' ? '✗' : '◔');
            const vtext = verdict === 'hit' ? '，判对' : (verdict === 'miss' ? '，判错' : '，未兑现不进命中率');
            parts.push('<span class="' + cls + '" title="' + dur + '：路径' + _TOUCH_LABEL[res] +
                (rec[t[3]] ? ('（首触 ' + rec[t[3]] + '）') : '') + vtext + '">' + dur + _TOUCH_LABEL[res] + icon + '</span>');
        });
        return '<div class="ana-review-cell">' + parts.join('<br>') + '</div>';
    }

    function anaPlanCell(rec) {
        // 结构价计划：先触目标=win(+R) / 先触失效=loss(-1R) / 观察中 / 无法观测；不受时间窗约束
        if (!rec.target_price || !rec.invalid_price) { return '<span class="ana-pending">未挂计划</span>'; }
        const tip = '目标 ' + anaFmtPrice(rec.target_price) + ' / 失效 ' + anaFmtPrice(rec.invalid_price) +
            (rec.plan_touch_ts ? (' · 触发 ' + rec.plan_touch_ts) : '');
        const r = rec.plan_result;
        if (r === 'win') { return '<span class="ana-hit" title="' + tip + '">✓ +' + (Number(rec.plan_r) || 0).toFixed(2) + 'R</span>'; }
        if (r === 'loss') { return '<span class="ana-miss" title="' + tip + '">✗ -1R</span>'; }
        if (r === 'gap') { return '<span class="ana-pending" title="' + tip + ' · 记录时刻K线超出可拉深度">无法观测</span>'; }
        if (r === 'open') { return '<span class="ana-open" title="' + tip + ' · 两端均未触，分析仍有效">观察中…</span>'; }
        return '<span class="ana-pending" title="' + tip + '">待结算</span>';
    }

    // 单行 HTML 构造器：全量渲染与行级局部刷新共用（data-id 供精准定位目标行）
    function anaRowHtml(r) {
        const w = _anaWindows(r.short_period);
        const longCell = anaDirBadge(r.long_dir) +
            (r.long_dir_prev && r.long_dir_prev !== r.long_dir
                ? '<div style="font-size:0.7rem;color:#999;margin-top:2px">上时段 ' + (r.long_dir_prev === 'long' ? '多' : '空') + '</div>' : '');
        return '<tr data-id="' + r.id + '">' +
            '<td style="text-align:center"><input type="checkbox" class="ana-row-pick" data-id="' + r.id + '" onclick="event.stopPropagation()"></td>' +
            '<td style="white-space:nowrap">' + escapeHtml(r.ts || '') + '</td>' +
            '<td>' + escapeHtml(r.inst_id || '') + '</td>' +
            '<td>' + anaFmtPrice(r.price) + '</td>' +
            '<td>' + anaDirBadge(r.short_dir) + ' <span style="font-size:0.7rem;color:#999">' + escapeHtml(r.short_period || '') + '</span></td>' +
            '<td>' + longCell + ' <span style="font-size:0.7rem;color:#999">' + escapeHtml(r.long_period || '') + '</span></td>' +
            '<td>' + anaJudgmentBadge(r.user_judgment) + '</td>' +
            '<td class="ana-reason-cell" title="' + escapeHtml(r.user_reason || '') + '">' + (escapeHtml(r.user_reason || '') || '—') + '</td>' +
            '<td>' + anaReviewCell(r, 'price_1h', 'ts_1h', w.near.min) + '</td>' +
            '<td>' + anaReviewCell(r, 'price_4h', 'ts_4h', w.far.min) + '</td>' +
            '<td>' + anaTouchCell(r) + '</td>' +
            '<td>' + anaPlanCell(r) + '</td>' +
            '<td><div class="action-btns">' +
                '<button class="m-btn m-btn-primary m-btn-sm" onclick="editAnalysisRecord(' + r.id + ')">✏️</button>' +
                '<button class="m-btn m-btn-danger m-btn-sm" onclick="deleteAnalysisRecord(' + r.id + ')">🗑</button>' +
            '</div></td>' +
            '</tr>';
    }

    function renderAnalysisTable(rows) {
        const tbody = document.getElementById('ana-tbody');
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="13" class="empty-state">暂无分析记录，先生成快照并保存</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map(anaRowHtml).join('');
    }

    /* ============================================================
     * 局部刷新：改动后仅更新受影响的行与统计卡，不再整表重拉重建
     * ============================================================ */

    // 本地重算命中率统计（严格镜像后端 analysis_record_repo.compute_stats 先触碰口径：
    // 判定结果直接读后端落库的 touch_* 字段，前端只重算判断→结果的映射，改动判断时无需拉取）
    function _emptyBucketL() {
        return {scored: 0, hit: 0, miss: 0, open: 0, pending: 0, rate: 0.0, avg_pct: 0.0, chg_n: 0};
    }
    function _emptyConfL() {
        return {rise: {up: 0, down: 0, none: 0, conflict: 0},
                watch: {up: 0, down: 0, none: 0, conflict: 0},
                fall: {up: 0, down: 0, none: 0, conflict: 0}};
    }
    // 镜像后端 _score_touch：方向判断 none/conflict=未兑现；观望 none=hit、up/down=miss、conflict=open
    function _scoreTouchL(b, key, touch, expectMap) {
        const exp = expectMap[key];
        if (exp === 'none') {
            if (touch === 'none') { b.hit += 1; }
            else if (touch === 'up' || touch === 'down') { b.miss += 1; }
            else { b.open += 1; }
        } else {
            if (touch === 'none' || touch === 'conflict') { b.open += 1; }
            else if (touch === exp) { b.hit += 1; }
            else { b.miss += 1; }
        }
    }
    function computeStatsLocal(records) {
        const stats = {
            total: records.length, score_mode: 'touch',
            near_mult: _anaReviewMult.near, far_mult: _anaReviewMult.far,
            hit_k: _anaHitCfg.k, hit_floor_pct: _anaHitCfg.floor, hit_far_scale: _anaHitCfg.farScale,
            user: {near: _emptyBucketL(), far: _emptyBucketL()},
            strategy: {near: _emptyBucketL(), far: _emptyBucketL()},
            confusion: {near: _emptyConfL(), far: _emptyConfL()},
            plan: {total: 0, win: 0, loss: 0, open: 0, gap: 0, pending: 0, win_rate: 0.0, avg_r: 0.0, _rn: 0, _rs: 0.0}
        };
        records.forEach(function(rec) {
            const price = Number(rec.price || 0);
            if (!(price > 0)) { return; }
            const judgment = rec.user_judgment || '';
            const sdir = rec.long_dir_prev || rec.long_dir || '';
            [['near', 'touch_near', 'price_1h'], ['far', 'touch_far', 'price_4h']].forEach(function(t) {
                const winKey = t[0];
                const touch = rec[t[1]];
                const settled = touch === 'up' || touch === 'down' || touch === 'none' || touch === 'conflict';
                const follow = Number(rec[t[2]] || 0);
                const chg = follow > 0 ? (follow / price - 1) * 100 : null;
                if (_JUDG_TOUCH[judgment]) {
                    const b = stats.user[winKey];
                    if (!settled) { b.pending += 1; }
                    else {
                        _scoreTouchL(b, judgment, touch, _JUDG_TOUCH);
                        if (chg !== null) { b.avg_pct += chg; b.chg_n += 1; }
                        stats.confusion[winKey][judgment][touch] += 1;
                    }
                }
                if (_DIR_TOUCH[sdir]) {
                    const b = stats.strategy[winKey];
                    if (!settled) { b.pending += 1; }
                    else { _scoreTouchL(b, sdir, touch, _DIR_TOUCH); }
                }
            });
            if (rec.target_price && rec.invalid_price) {
                const p = stats.plan;
                const res = rec.plan_result;
                if (res === 'win' || res === 'loss' || res === 'open' || res === 'gap') {
                    p[res] += 1; p.total += 1;
                    if ((res === 'win' || res === 'loss') && rec.plan_r !== null && rec.plan_r !== undefined) {
                        p._rs += Number(rec.plan_r); p._rn += 1;
                    }
                } else { p.pending += 1; p.total += 1; }
            }
        });
        ['user', 'strategy'].forEach(function(side) {
            ['near', 'far'].forEach(function(winKey) {
                const b = stats[side][winKey];
                b.scored = b.hit + b.miss;
                if (b.scored > 0) { b.rate = Math.round(b.hit / b.scored * 100 * 10) / 10; }
                if (b.chg_n > 0) { b.avg_pct = Math.round(b.avg_pct / b.chg_n * 1000) / 1000; }
                else { b.avg_pct = 0.0; }
            });
        });
        const plan = stats.plan;
        const decided = plan.win + plan.loss;
        if (decided > 0) { plan.win_rate = Math.round(plan.win / decided * 100 * 10) / 10; }
        if (plan._rn > 0) { plan.avg_r = Math.round(plan._rs / plan._rn * 1000) / 1000; }
        delete plan._rn; delete plan._rs;
        return stats;
    }

    function refreshAnaStats() { renderAnalysisStats(computeStatsLocal(_anaRecords)); }

    // 记录是否落在当前筛选条件内（镜像 anaFilterParams / 后端 query_records：字符串闭区间比较）
    function anaMatchesFilter(rec) {
        const inst = document.getElementById('ana-filter-inst').value;
        const judgment = document.getElementById('ana-filter-judgment').value;
        const start = document.getElementById('ana-filter-start').value;
        const end = document.getElementById('ana-filter-end').value;
        if (inst && rec.inst_id !== inst) { return false; }
        if (judgment && rec.user_judgment !== judgment) { return false; }
        if (start && (rec.ts || '') < start + ' 00:00:00') { return false; }
        if (end && (rec.ts || '') > end + ' 23:59:59') { return false; }
        return true;
    }

    function anaRowEl(id) { return document.querySelector('#ana-tbody tr[data-id="' + id + '"]'); }

    // 顶部插入新行（先清掉“暂无/加载中”占位行）
    function anaInsertRowTop(rec) {
        const tbody = document.getElementById('ana-tbody');
        if (!tbody.querySelector('tr[data-id]')) { tbody.innerHTML = ''; }
        tbody.insertAdjacentHTML('afterbegin', anaRowHtml(rec));
    }

    // 原地替换单行（判断/原因改动会连带结果列变化，整行重绘最稳妥）
    function anaUpdateRow(rec) {
        const tr = anaRowEl(rec.id);
        if (tr) { tr.outerHTML = anaRowHtml(rec); } else { anaInsertRowTop(rec); }
    }

    // 移除单行，全部删完则回落到空状态占位
    function anaRemoveRow(id) {
        const tr = anaRowEl(id);
        if (tr) { tr.remove(); }
        const tbody = document.getElementById('ana-tbody');
        if (!tbody.querySelector('tr[data-id]')) {
            tbody.innerHTML = '<tr><td colspan="13" class="empty-state">暂无分析记录，先生成快照并保存</td></tr>';
        }
    }

    // 结构价一键预填：boll=BOLL 轨按判断归边（看涨上轨为目标/下轨为失效，看跌反之）；
    // theta=快照价 ±θ 噪声带（近窗口 θ，降级选项；计划的失效价建议用真正的结构破位位）
    function anaPrefillPlan(mode) {
        if (!_anaSnapshot) { showToast('请先生成快照', 'error'); return; }
        const judgment = (document.querySelector('input[name="ana-judgment"]:checked') || {}).value || 'watch';
        if (judgment === 'watch') { showToast('先选「涨/跌」判断再预填结构价', 'info'); return; }
        const p0 = Number(_anaSnapshot.price || 0);
        if (!(p0 > 0)) { showToast('快照价格无效', 'error'); return; }
        let t = null, iv = null;
        if (mode === 'boll') {
            const up = Number(_anaSnapshot.boll_upper || 0), dn = Number(_anaSnapshot.boll_lower || 0);
            if (!(up > 0 && dn > 0)) { showToast('快照无 BOLL 轨数据', 'error'); return; }
            t = judgment === 'rise' ? up : dn;
            iv = judgment === 'rise' ? dn : up;
        } else {
            const th = anaTheta(Number(_anaSnapshot.atr_pct || 0), false);
            t = p0 * (1 + th / 100);
            iv = p0 * (1 - th / 100);
        }
        const fmt = v => String(Math.round(v * 10000) / 10000);
        document.getElementById('ana-target').value = fmt(t);
        document.getElementById('ana-invalid').value = fmt(iv);
    }

    function editAnalysisRecord(id) {
        const rec = _anaRecords.find(r => r.id === id);
        if (!rec) { showToast('记录不存在，请刷新', 'error'); return; }
        const j = rec.user_judgment || 'watch';
        const locked = rec.plan_result === 'win' || rec.plan_result === 'loss' || rec.plan_result === 'gap';
        MDialog.show({
            title: '✏️ 修改判断/原因/结构价（' + rec.inst_id + ' ' + rec.ts + '）',
            message: '<div style="display:flex;gap:14px;margin-bottom:10px;font-size:0.88rem">' +
                '<label><input type="radio" name="ana-edit-judgment" value="rise"' + (j === 'rise' ? ' checked' : '') + '> 涨</label>' +
                '<label><input type="radio" name="ana-edit-judgment" value="fall"' + (j === 'fall' ? ' checked' : '') + '> 跌</label>' +
                '<label><input type="radio" name="ana-edit-judgment" value="watch"' + (j === 'watch' ? ' checked' : '') + '> 观望</label>' +
                '</div>' +
                '<div style="display:flex;gap:10px;margin-bottom:8px;align-items:center;flex-wrap:wrap;font-size:0.84rem">' +
                '<label>目标价 <input type="number" id="ana-edit-target" step="any" min="0" style="width:110px"' +
                (locked ? ' disabled' : '') + ' value="' + (rec.target_price != null ? rec.target_price : '') + '"></label>' +
                '<label>失效价 <input type="number" id="ana-edit-invalid" step="any" min="0" style="width:110px"' +
                (locked ? ' disabled' : '') + ' value="' + (rec.invalid_price != null ? rec.invalid_price : '') + '"></label>' +
                '<span style="font-size:0.74rem;color:' + (locked ? '#dc3545' : '#999') + '">' +
                (locked ? '计划已终局，结构价锁定' : '仅涨/跌可挂；两端清空=取消计划；未终局改动后重新观察') + '</span>' +
                '</div>' +
                '<textarea id="ana-edit-reason" class="ana-reason-input" style="width:100%;min-height:80px;box-sizing:border-box">' +
                escapeHtml(rec.user_reason || '') + '</textarea>',
            okText: '保存修改',
            onOk: function() {
                const judgment = (document.querySelector('input[name="ana-edit-judgment"]:checked') || {}).value || 'watch';
                const reason = document.getElementById('ana-edit-reason').value.trim();
                // 结构价总是成对提交（后端据此判定为修改计划）；终局锁定时不送→后端保持原值
                const extra = locked ? {} : {
                    target_price: document.getElementById('ana-edit-target').value.trim() ? Number(document.getElementById('ana-edit-target').value.trim()) : null,
                    invalid_price: document.getElementById('ana-edit-invalid').value.trim() ? Number(document.getElementById('ana-edit-invalid').value.trim()) : null
                };
                fetch('/api/task/analysis/records/' + id, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(Object.assign({user_judgment: judgment, user_reason: reason}, extra))
                }).then(r => r.json()).then(res => {
                    showToast(res.message, res.code === 200 ? 'success' : 'error');
                    if (res.code === 200) {
                        // 只更新内存中这一条 + 原地重绘该行；若判断改动后不再命中筛选则移除该行
                        const rec = _anaRecords.find(r2 => r2.id === id);
                        if (rec) {
                            rec.user_judgment = judgment;
                            rec.user_reason = reason;
                            if (!locked && (rec.plan_result === null || rec.plan_result === undefined || rec.plan_result === 'open')) {
                                // 后端对改动过结构价的未终局计划重置为待重扫（touch/端点不受影响）
                                const changed = extra.target_price !== rec.target_price || extra.invalid_price !== rec.invalid_price;
                                rec.target_price = extra.target_price;
                                rec.invalid_price = extra.invalid_price;
                                if (changed) { rec.plan_result = null; rec.plan_r = null; rec.plan_touch_ts = null; }
                            }
                            if (anaMatchesFilter(rec)) { anaUpdateRow(rec); }
                            else { _anaRecords = _anaRecords.filter(r2 => r2.id !== id); anaRemoveRow(id); }
                            refreshAnaStats();
                        }
                    }
                }).catch(e => showToast('修改失败: ' + e, 'error'));
            }
        });
    }

    function deleteAnalysisRecord(id) {
        MDialog.danger('确定要删除这条分析记录吗？此操作不可恢复。', function() {
            fetch('/api/task/analysis/records/' + id, {method: 'DELETE'})
                .then(r => r.json()).then(res => {
                    showToast(res.message, res.code === 200 ? 'success' : 'error');
                    if (res.code === 200) {
                        // 本地删这一条 + 移除该行 + 本地重算统计（不再整表重拉）
                        _anaRecords = _anaRecords.filter(r2 => r2.id !== id);
                        anaRemoveRow(id);
                        refreshAnaStats();
                    }
                }).catch(e => showToast('删除失败: ' + e, 'error'));
        });
    }

    /* ---- 批量删除：表头勾选框全选/反选，工具条按钮删除已勾选记录 ---- */
    function toggleAllAnaRows(headCb) {
        const pick = headCb ? headCb.checked : true;
        document.querySelectorAll('#ana-tbody .ana-row-pick').forEach(c => { c.checked = pick; });
    }

    function batchDeleteAnalysisRecords(btn) {
        const ids = Array.from(document.querySelectorAll('#ana-tbody .ana-row-pick:checked'))
            .map(c => parseInt(c.dataset.id, 10)).filter(id => !isNaN(id));
        if (!ids.length) { showToast('请先勾选要删除的记录', 'info'); return; }
        MDialog.danger({
            title: '🗑 批量删除分析记录',
            message: '确定要删除勾选的 <b>' + ids.length + '</b> 条分析记录吗？此操作不可恢复。',
            okText: '确认删除',
            onOk: function() {
                if (btn) { btn.disabled = true; }
                fetch('/api/task/analysis/records_batch', {
                    method: 'DELETE',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ids: ids})
                }).then(r => r.json()).then(res => {
                    showToast(res.message, res.code === 200 ? 'success' : 'error');
                    if (res.code === 200) {
                        // 本地移除这些行 + 重算统计（不整表重拉）
                        const idset = {};
                        ids.forEach(function(id) { idset[id] = true; });
                        _anaRecords = _anaRecords.filter(r2 => !idset[r2.id]);
                        ids.forEach(anaRemoveRow);
                        refreshAnaStats();
                        const head = document.getElementById('ana-check-all');
                        if (head) { head.checked = false; }
                    }
                }).catch(e => showToast('批量删除失败: ' + e, 'error'))
                  .finally(function() { if (btn) { btn.disabled = false; } });
            }
        });
    }

    /* ---- 暴露给内联 onclick 与 switchTab 调用（其余保持 IIFE 私有） ---- */
    window.initAnalysisTab = initAnalysisTab;
    window.loadAnalysisRecords = loadAnalysisRecords;
    window.genAnalysisSnapshot = genAnalysisSnapshot;
    window.hideAnalysisSnapshot = hideAnalysisSnapshot;
    window.saveAnalysisRecord = saveAnalysisRecord;
    window.genBatchSnapshot = genBatchSnapshot;
    window.toggleAllBatchRows = toggleAllBatchRows;
    window.hideBatchSnapshot = hideBatchSnapshot;
    window.saveBatchSnapshots = saveBatchSnapshots;
    window.editAnalysisRecord = editAnalysisRecord;
    window.anaPrefillPlan = anaPrefillPlan;
    window.deleteAnalysisRecord = deleteAnalysisRecord;
    window.toggleAllAnaRows = toggleAllAnaRows;
    window.batchDeleteAnalysisRecords = batchDeleteAnalysisRecords;
    window.reloadAnalysisCoins = reloadAnalysisCoins;
})();
