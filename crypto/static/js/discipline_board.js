/* ================================================================
   discipline_board.js —— 分析纪律看板 + 配置面板（批次11 P3）
   ----------------------------------------------------------------
   挂在任务计划页的 #discipline-panel 上（用户被闸门拦住的地方，闭环最短）。
   独立成文件，避免 task_plan.js（已 2000+ 行）继续膨胀。

   【设计取向】只做"如实呈现"，不做罚款与惩罚性展示——与任务计划模块
   长期主义 v1.1 的既有理念一致。三块核心内容：
     1. 合规率与断档热力：懈怠长什么样，什么时段最容易掉
     2. 补记率：诚实指标。闸门允许补记（堵死只会让人放弃功能），
        但补记率如实公开——让作弊被看见，比技术防作弊有意义
     3. 归因对比：有分析支撑 vs 无分析支撑的打卡命中率。
        这是整套纪律存在的唯一理由；如果差值为负，说明规则该改

   依赖：无（自带样式、不依赖 ECharts / MDialog，用原生 confirm 兜底）
   ================================================================ */
(function () {
    'use strict';

    if (window.DisciplineBoard) { return; }

    var BOARD_URL = '/plan/api/discipline/board';
    var CONFIG_URL = '/plan/api/discipline/config';
    var EXEMPT_URL = '/plan/api/discipline/exempt';
    var CHECKNOW_URL = '/plan/api/discipline/check-now';
    var MOUNT_ID = 'discipline-panel';

    var DAYS_OPTIONS = [7, 30, 90];
    var WEEK_CN = ['一', '二', '三', '四', '五', '六', '日'];

    var _days = 30;
    var _data = null;
    var _cfg = null;
    var _collapsed = false;

    /* ---------- 工具 ---------- */

    function esc(s) {
        return String(s === null || s === undefined ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function api(url, method, body) {
        var opts = { method: method || 'GET', headers: { 'Content-Type': 'application/json' } };
        if (body) { opts.body = JSON.stringify(body); }
        return fetch(url, opts).then(function (r) { return r.json(); });
    }

    function $(sel, root) { return (root || document).querySelector(sel); }

    /* ---------- 样式 ---------- */

    var CSS = [
        '.db-panel{background:var(--card-bg,#fff);border:1px solid var(--border-color,#eee);',
        'border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.05);padding:14px 20px;margin-bottom:20px;}',
        '.db-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}',
        '.db-title{font-size:.95rem;font-weight:700;flex:1;min-width:180px;}',
        '.db-days{display:flex;gap:4px;}',
        '.db-btn{border:1px solid var(--border-color,#ddd);background:#fff;border-radius:6px;',
        'padding:4px 10px;font-size:.78rem;cursor:pointer;color:var(--text-main,#333);white-space:nowrap;}',
        '.db-btn:hover:not(:disabled){border-color:var(--primary-color,#007bff);color:var(--primary-color,#007bff);}',
        '.db-btn:disabled{opacity:.5;cursor:not-allowed;}',
        '.db-btn.on{background:var(--primary-color,#007bff);border-color:var(--primary-color,#007bff);color:#fff;}',
        '.db-body{margin-top:12px;}',
        '.db-collapsed .db-body{display:none;}',
        /* KPI */
        '.db-kpis{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;}',
        '.db-kpi{flex:1;min-width:120px;border:1px solid var(--border-color,#eee);border-radius:10px;',
        'padding:10px 12px;background:#fcfdff;}',
        '.db-kpi .v{font-size:1.45rem;font-weight:800;line-height:1.2;font-variant-numeric:tabular-nums;}',
        '.db-kpi .l{font-size:.74rem;color:var(--text-muted,#777);margin-top:2px;}',
        '.db-good{color:#2b8a3e;}.db-bad{color:#c92a2a;}.db-warn{color:#e8590c;}.db-mut{color:#868e96;}',
        /* 归因 */
        '.db-attr{border:1px solid var(--border-color,#eee);border-radius:10px;padding:12px;margin-bottom:14px;',
        'background:linear-gradient(135deg,#fff,#f7fbff);}',
        '.db-attr h4{margin:0 0 8px;font-size:.86rem;}',
        '.db-attr-row{display:flex;align-items:center;gap:10px;margin-bottom:6px;font-size:.82rem;}',
        '.db-attr-row .nm{width:132px;flex-shrink:0;color:var(--text-muted,#777);}',
        '.db-bar{flex:1;height:14px;background:#f1f3f5;border-radius:7px;overflow:hidden;min-width:60px;}',
        '.db-bar>i{display:block;height:100%;border-radius:7px;}',
        '.db-attr-row .rt{width:120px;flex-shrink:0;text-align:right;font-variant-numeric:tabular-nums;}',
        '.db-verdict{margin-top:8px;padding:8px 10px;border-radius:8px;font-size:.8rem;line-height:1.6;}',
        /* 热力 */
        '.db-heat-wrap{overflow-x:auto;margin-bottom:14px;}',
        'table.db-heat{border-collapse:separate;border-spacing:2px;font-size:.66rem;}',
        'table.db-heat th{font-weight:600;color:var(--text-muted,#777);padding:0 2px;text-align:center;}',
        'table.db-heat td{width:22px;height:18px;text-align:center;border-radius:3px;background:#f1f3f5;',
        'color:#adb5bd;cursor:default;font-variant-numeric:tabular-nums;}',
        'table.db-heat td.na{background:transparent;}',
        /* 每日曲线 */
        '.db-daily{display:flex;align-items:flex-end;gap:3px;height:76px;margin-bottom:6px;overflow-x:auto;padding-bottom:2px;}',
        '.db-col{flex:1;min-width:14px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%;}',
        '.db-col>i{display:block;width:100%;border-radius:3px 3px 0 0;background:#51cf66;}',
        '.db-col.miss>i{background:#ffa8a8;}',
        '.db-col.none>i{background:#e9ecef;}',
        '.db-daily-x{display:flex;gap:3px;font-size:.62rem;color:var(--text-muted,#777);overflow-x:auto;margin-bottom:14px;}',
        '.db-daily-x>span{flex:1;min-width:14px;text-align:center;}',
        /* 配置 */
        '.db-cfg{border-top:1px dashed var(--border-color,#eee);padding-top:12px;}',
        '.db-cfg summary{cursor:pointer;font-size:.85rem;font-weight:600;color:var(--primary-color,#007bff);}',
        '.db-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;margin-top:12px;}',
        '.db-f{display:flex;flex-direction:column;gap:3px;}',
        '.db-f>label{font-size:.76rem;color:var(--text-muted,#777);}',
        '.db-f input[type=text],.db-f input[type=number],.db-f select{padding:5px 8px;font-size:.82rem;',
        'border:1px solid var(--border-color,#ddd);border-radius:6px;box-sizing:border-box;width:100%;}',
        '.db-f.chk{flex-direction:row;align-items:center;gap:6px;}',
        '.db-f.chk label{font-size:.82rem;color:var(--text-main,#333);}',
        '.db-cfg-acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center;}',
        '.db-note{font-size:.76rem;color:var(--text-muted,#777);line-height:1.6;margin:6px 0 0;}',
        '.db-digest{font-size:.8rem;color:var(--text-muted,#777);line-height:1.7;margin-bottom:14px;}',
        '.db-loading{text-align:center;padding:26px;color:var(--text-muted,#777);font-size:.85rem;}'
    ].join('');

    function ensureStyle() {
        if (document.getElementById('db-style')) { return; }
        var st = document.createElement('style');
        st.id = 'db-style';
        st.textContent = CSS;
        document.head.appendChild(st);
    }

    /* ---------- 骨架 ---------- */

    function skeleton() {
        return '<div class="db-head">' +
            '<span class="db-title">🧭 分析纪律 · 反懈怠看板</span>' +
            '<span class="db-days">' + DAYS_OPTIONS.map(function (d) {
                return '<button type="button" class="db-btn" data-days="' + d + '">近 ' + d + ' 天</button>';
            }).join('') + '</span>' +
            '<button type="button" class="db-btn" data-act="reload">↻ 刷新</button>' +
            '<button type="button" class="db-btn" data-act="toggle">▾ 折叠</button>' +
            '</div>' +
            '<div class="db-body"><div class="db-loading">加载中…</div></div>';
    }

    /* ---------- 渲染 ---------- */

    function rateCls(r) { return r >= 80 ? 'db-good' : (r >= 50 ? 'db-warn' : 'db-bad'); }

    function kpiHtml() {
        var s = _data.summary || {}, st = _data.streak || {}, bf = _data.backfill || {};
        var rate = Number(s.rate || 0);
        return '<div class="db-kpis">' +
            '<div class="db-kpi"><div class="v ' + rateCls(rate) + '">' + rate.toFixed(1) + '%</div>' +
            '<div class="l">小时合规率（' + (s.ok || 0) + '/' + ((s.ok || 0) + (s.missing || 0)) + ' 个已判定槽）</div></div>' +
            '<div class="db-kpi"><div class="v db-good">' + (st.current || 0) + '</div>' +
            '<div class="l">连续全合规天数</div></div>' +
            '<div class="db-kpi"><div class="v db-mut">' + (st.best || 0) + '</div>' +
            '<div class="l">历史最佳</div></div>' +
            '<div class="db-kpi"><div class="v ' + (bf.rate > 40 ? 'db-warn' : 'db-mut') + '">' +
            Number(bf.rate || 0).toFixed(1) + '%</div>' +
            '<div class="l">补记率（' + (bf.backfill || 0) + '/' + (bf.total || 0) + ' 条）· 诚实指标</div></div>' +
            '<div class="db-kpi"><div class="v ' + ((s.missing || 0) ? 'db-bad' : 'db-good') + '">' + (s.missing || 0) + '</div>' +
            '<div class="l">缺档小时数' + ((s.satisfied_later || 0) ? '（其中 ' + s.satisfied_later + ' 个已补记）' : '') + '</div></div>' +
            '</div>';
    }

    function attrHtml() {
        var a = _data.attribution || {};
        var w = a.with_analysis || {}, wo = a.without_analysis || {};
        var maxN = Math.max(w.total || 0, wo.total || 0, 1);
        var diff = a.rate_diff;
        var verdict, cls;
        if (diff === null || diff === undefined) {
            verdict = '样本还不足：需要「有分析」与「无分析」两组都有已回填实际涨跌的打卡，才能算出命中率差。继续按纪律做，几天后这里会自动出结论。';
            cls = 'background:#f8f9fa;color:#868e96;';
        } else if (diff > 0) {
            verdict = '📈 有分析支撑的打卡命中率高 <b>' + diff.toFixed(1) + '</b> 个百分点——这就是"先分析再打卡"值得坚持的证据。';
            cls = 'background:#ebfbee;color:#2b8a3e;';
        } else if (diff < 0) {
            verdict = '📉 有分析支撑的打卡命中率反而低 <b>' + Math.abs(diff).toFixed(1) + '</b> 个百分点。别急着否定纪律：先看样本量是否够大、分析是不是走了形式（比如全选"观望"）。数据是拿来改方法的，不是拿来自我否定的。';
            cls = 'background:#fff9db;color:#8a6d00;';
        } else {
            verdict = '➖ 两组命中率持平。样本还不够多，或分析质量有待提升——继续记录，让数据自己说话。';
            cls = 'background:#f8f9fa;color:#868e96;';
        }
        function row(name, o, color) {
            var n = o.total || 0;
            var pct = n ? (o.hit || 0) / n * 100 : 0;
            return '<div class="db-attr-row"><span class="nm">' + name + '</span>' +
                '<span class="db-bar"><i style="width:' + (n / maxN * 100).toFixed(1) + '%;background:' + color + '"></i></span>' +
                '<span class="rt">' + (n ? Number(o.rate || 0).toFixed(1) + '% (' + (o.hit || 0) + '/' + n + ')' : '无样本') + '</span></div>';
        }
        return '<div class="db-attr"><h4>🎯 归因对比：分析到底有没有用？</h4>' +
            row('有分析支撑', w, '#4dabf7') +
            row('无分析支撑', wo, '#ced4da') +
            '<div class="db-verdict" style="' + cls + '">' + verdict + '</div></div>';
    }

    function heatHtml() {
        var heat = _data.heatmap || [], total = _data.heatmap_total || [];
        if (!heat.length) { return ''; }
        var max = 1;
        heat.forEach(function (r) { r.forEach(function (v) { if (v > max) { max = v; } }); });

        var head = '<tr><th></th>' + Array.from({ length: 24 }, function (_, h) {
            return '<th>' + (h % 3 === 0 ? h : '') + '</th>';
        }).join('') + '</tr>';
        var rows = heat.map(function (r, wd) {
            var cells = r.map(function (v, h) {
                var t = (total[wd] && total[wd][h]) || 0;
                if (!t) { return '<td class="na" title="周' + WEEK_CN[wd] + ' ' + h + ':00 无已判定槽">·</td>'; }
                // 颜色深浅 = 该时段缺档占比，一眼看出"哪个时段最容易掉"
                var ratio = v / t;
                var alpha = v ? (0.25 + 0.75 * Math.min(1, v / max)) : 0;
                var bg = v ? 'background:rgba(224,49,49,' + alpha.toFixed(2) + ');color:#fff;' : 'background:#ebfbee;color:#2b8a3e;';
                return '<td style="' + bg + '" title="周' + WEEK_CN[wd] + ' ' + h + ':00 — 缺档 ' + v +
                    ' / 已判定 ' + t + '（' + (ratio * 100).toFixed(0) + '%）">' + (v || '·') + '</td>';
            }).join('');
            return '<tr><th>周' + WEEK_CN[wd] + '</th>' + cells + '</tr>';
        }).join('');
        return '<div class="db-heat-wrap"><table class="db-heat">' + head + rows + '</table>' +
            '<p class="db-note">24×7 断档热力：红=该时段缺档次数，绿=全部合规，灰点=该时段无已判定槽。' +
            '找出自己最容易掉的那几个小时，比笼统地"要更自律"有用得多。</p></div>';
    }

    function dailyHtml() {
        var daily = _data.daily || [];
        if (!daily.length) {
            return '<p class="db-note">还没有已判定的小时槽。纪律引擎只在生效时段（' +
                esc((_cfg && _cfg.active_hours) || '08:00-24:00') + '）内判定，且从启用那一刻起算，不追溯上线之前的时间。</p>';
        }
        var cols = daily.map(function (d) {
            var judged = (d.ok || 0) + (d.missing || 0);
            var h = judged ? Math.max(4, Math.round((d.ok || 0) / judged * 100)) : 0;
            var cls = judged ? ((d.missing || 0) ? 'miss' : '') : 'none';
            return '<div class="db-col ' + cls + '" title="' + esc(d.date) + '：合规 ' + (d.ok || 0) +
                ' / 缺档 ' + (d.missing || 0) + ' / 豁免 ' + (d.exempt || 0) + ' · ' + Number(d.rate || 0).toFixed(1) + '%">' +
                '<i style="height:' + (judged ? h : 2) + '%"></i></div>';
        }).join('');
        var xs = daily.map(function (d) {
            return '<span>' + esc(String(d.date || '').slice(5)) + '</span>';
        }).join('');
        return '<div class="db-daily">' + cols + '</div><div class="db-daily-x">' + xs + '</div>';
    }

    function digestHtml() {
        var dg = _data.digest || {};
        if (!dg.gap_count) {
            return '<div class="db-digest">🌱 近 ' + (dg.days || 7) + ' 天没有断档记录。</div>';
        }
        var tops = (dg.top_hours || []).map(function (h) { return h.hour + ':00（' + h.count + ' 次）'; }).join('、');
        return '<div class="db-digest">🔍 近 ' + (dg.days || 7) + ' 天断档画像：共 <b>' + dg.gap_count +
            '</b> 个缺口小时' + (dg.longest_gap ? '；最长连续断档 <b>' + esc(dg.longest_gap) + '</b>' : '') +
            (tops ? '；高发时段 <b>' + esc(tops) + '</b>' : '') + '</div>';
    }

    function cfgHtml() {
        var c = _cfg || {};
        var em = c.email || {}, br = c.browser || {};
        function chk(name, on, label) {
            return '<div class="db-f chk"><input type="checkbox" id="db-' + name + '"' + (on ? ' checked' : '') + '>' +
                '<label for="db-' + name + '">' + label + '</label></div>';
        }
        function txt(name, label, value, ph) {
            return '<div class="db-f"><label for="db-' + name + '">' + label + '</label>' +
                '<input type="text" id="db-' + name + '" value="' + esc(value === undefined || value === null ? '' : value) + '"' +
                (ph ? ' placeholder="' + esc(ph) + '"' : '') + '></div>';
        }
        function num(name, label, value, min, max) {
            return '<div class="db-f"><label for="db-' + name + '">' + label + '</label>' +
                '<input type="number" id="db-' + name + '" value="' + Number(value || 0) + '" min="' + min + '" max="' + max + '"></div>';
        }
        return '<details class="db-cfg"><summary>⚙️ 纪律规则与打扰强度</summary>' +
            '<div class="db-grid">' +
            chk('enabled', c.enabled !== false, '启用分析纪律（关闭后闸门放行、巡检停止）') +
            txt('active_hours', '生效时段（右端不含：24:00=午夜，08:00-24:00 即管到 23:59）', c.active_hours, '08:00-24:00') +
            num('required_count', '每小时要求的分析记录条数', c.required_count, 1, 50) +
            num('grace_minutes', '宽限期（分钟，槽结束后仍可补记）', c.grace_minutes, 0, 180) +
            '<div class="db-f"><label for="db-strict_mode">打卡闸门强度</label><select id="db-strict_mode">' +
            ['strict|硬阻断：无分析不给打卡', 'soft|宽松：放行但记为"无分析支撑"', 'off|关闭闸门：只提醒不拦']
                .map(function (o) {
                    var kv = o.split('|');
                    return '<option value="' + kv[0] + '"' + (c.strict_mode === kv[0] ? ' selected' : '') + '>' + kv[1] + '</option>';
                }).join('') + '</select></div>' +
            chk('require_cover_tracked', c.require_cover_tracked, '收紧：必须覆盖交易配置全部跟踪币种') +
            chk('em_enabled', em.enabled !== false, '邮件提醒总开关（关闭后缺口/汇总/日报全不发，台账与闸门照常）') +
            chk('em_on_gap', em.on_gap !== false, '邮件：缺口即时提醒') +
            num('em_merge_after', '邮件：连续 ≥N 个缺口合并为一封', em.merge_after, 1, 24) +
            txt('em_daily_report', '邮件：每日纪律日报时刻（留空=不发）', em.daily_report, '23:00') +
            chk('br_banner', br.banner !== false, '浏览器：贴顶横幅') +
            chk('br_sound', br.sound !== false, '浏览器：提示音') +
            chk('br_desktop_notify', br.desktop_notify !== false, '浏览器：桌面通知') +
            num('br_banner_after_minutes', '横幅延迟（整点后 N 分钟才打扰）', br.banner_after_minutes, 0, 59) +
            num('br_poll_seconds', '前端轮询周期（秒）', br.poll_seconds, 15, 600) +
            '</div>' +
            '<p class="db-note">提醒太密会在一周内被当成噪声关掉。<b>生效时段 + 横幅延迟 + 缺口合并 + 一次性豁免</b>四道闸门，' +
            '存在的意义都是同一件事：让这套机制能长期活下来，而不是第一周就被关掉。</p>' +
            '<div class="db-cfg-acts">' +
            '<button type="button" class="db-btn" data-act="save" style="background:var(--primary-color,#007bff);border-color:var(--primary-color,#007bff);color:#fff;">💾 保存规则</button>' +
            '<button type="button" class="db-btn" data-act="exempt2">😴 豁免 2 小时</button>' +
            '<button type="button" class="db-btn" data-act="exempt8">😴 豁免 8 小时</button>' +
            '<button type="button" class="db-btn" data-act="exempt0">取消豁免</button>' +
            '<button type="button" class="db-btn" data-act="checknow">▶ 立即跑一轮巡检</button>' +
            '<span class="db-note" id="db-cfg-msg" style="margin:0;flex:1;min-width:160px;"></span>' +
            '</div>' +
            '<p class="db-note" id="db-exempt-now"></p>' +
            '</details>';
    }

    function paint() {
        var body = $('.db-body', _root);
        if (!body) { return; }
        if (!_data) { body.innerHTML = '<div class="db-loading">暂无数据</div>'; return; }
        body.innerHTML =
            '<div style="font-size:.76rem;color:var(--text-muted,#777);margin-bottom:10px;">' +
            '统计区间 ' + esc(_data.start_date) + ' ~ ' + esc(_data.end_date) +
            ' · 规则：生效时段 ' + esc((_cfg && _cfg.active_hours) || '-') +
            '，每小时 ≥' + ((_cfg && _cfg.required_count) || 1) + ' 条，' +
            '闸门 ' + esc(modeCn((_cfg && _cfg.strict_mode) || 'strict')) + '</div>' +
            kpiHtml() +
            attrHtml() +
            digestHtml() +
            dailyHtml() +
            heatHtml() +
            cfgHtml();
        paintExempt();
        bindBody();
    }

    function modeCn(m) {
        return { strict: '硬阻断', soft: '宽松留痕', off: '关闭' }[m] || m;
    }

    function paintExempt() {
        var el = $('#db-exempt-now');
        if (!el) { return; }
        var st = (window.AnalysisDiscipline && window.AnalysisDiscipline.status()) || null;
        var until = st && st.exempt_until;
        el.innerHTML = until
            ? '😴 当前处于豁免中，截止 <b>' + esc(until) + '</b>：期间所有小时不追责、不提醒、闸门放行。'
            : '未设置豁免。出差或不盯盘时用豁免，别把整个功能关掉——有泄压阀，规则才活得下去。';
    }

    function cfgMsg(text, isErr) {
        var el = $('#db-cfg-msg');
        if (el) {
            el.textContent = text || '';
            el.style.color = isErr ? '#c92a2a' : '#2b8a3e';
        }
    }

    /* ---------- 交互 ---------- */

    var _root = null;

    function bindBody() {
        var body = $('.db-body', _root);
        if (!body) { return; }
        body.querySelectorAll('[data-act]').forEach(function (btn) {
            btn.addEventListener('click', function () { onAct(btn.dataset.act, btn); });
        });
    }

    function readCfgForm() {
        function val(id) { var e = document.getElementById('db-' + id); return e ? e.value : ''; }
        function checked(id) { var e = document.getElementById('db-' + id); return !!(e && e.checked); }
        function intVal(id, dflt) { var v = parseInt(val(id), 10); return isFinite(v) ? v : dflt; }
        var base = _cfg || {};
        return {
            enabled: checked('enabled'),
            active_hours: val('active_hours'),
            required_count: intVal('required_count', base.required_count || 1),
            require_cover_tracked: checked('require_cover_tracked'),
            grace_minutes: intVal('grace_minutes', base.grace_minutes === undefined ? 15 : base.grace_minutes),
            strict_mode: val('strict_mode'),
            email: {
                enabled: checked('em_enabled'),
                on_gap: checked('em_on_gap'),
                merge_after: intVal('em_merge_after', 2),
                daily_report: val('em_daily_report')
            },
            browser: {
                banner: checked('br_banner'),
                sound: checked('br_sound'),
                desktop_notify: checked('br_desktop_notify'),
                banner_after_minutes: intVal('br_banner_after_minutes', 20),
                poll_seconds: intVal('br_poll_seconds', 60)
            }
        };
    }

    function onAct(act, btn) {
        if (act === 'save') {
            btn.disabled = true;
            cfgMsg('保存中…');
            api(CONFIG_URL, 'POST', readCfgForm()).then(function (res) {
                btn.disabled = false;
                if (res && res.code === 200) {
                    _cfg = res.data || _cfg;
                    cfgMsg(res.message || '已保存', false);
                    if (window.AnalysisDiscipline) { window.AnalysisDiscipline.refresh(); }
                    load();   // 规则变了，看板口径随之变化，重算一次
                } else {
                    cfgMsg((res && res.message) || '保存失败', true);
                }
            }).catch(function (e) { btn.disabled = false; cfgMsg('保存失败: ' + e, true); });
            return;
        }
        if (act === 'checknow') {
            btn.disabled = true;
            cfgMsg('巡检中（会写台账并按规则发信）…');
            api(CHECKNOW_URL, 'POST').then(function (res) {
                btn.disabled = false;
                if (res && res.code === 200) {
                    var d = res.data || {};
                    cfgMsg('巡检完成：判定 ' + (d.judged || 0) + ' 槽（合格 ' + (d.satisfied || 0) +
                        ' / 缺口 ' + (d.missing || 0) + ' / 补齐 ' + (d.resolved || 0) + '），邮件 ' +
                        (d.email_sent || 0) + ' 封 · ' + (d.duration_ms || 0) + 'ms', false);
                    load();
                } else {
                    cfgMsg((res && res.message) || '巡检失败', true);
                }
            }).catch(function (e) { btn.disabled = false; cfgMsg('巡检失败: ' + e, true); });
            return;
        }
        if (act.indexOf('exempt') === 0) {
            var hours = parseInt(act.replace('exempt', ''), 10) || 0;
            if (hours > 0 && !window.confirm('确定豁免接下来 ' + hours + ' 小时？期间不追责、不提醒、打卡闸门放行。')) { return; }
            btn.disabled = true;
            api(EXEMPT_URL, 'POST', { hours: hours }).then(function (res) {
                btn.disabled = false;
                cfgMsg((res && res.message) || '操作失败', res && res.code === 200 ? false : true);
                if (res && res.code === 200) {
                    if (window.AnalysisDiscipline) { window.AnalysisDiscipline.refresh(); }
                    setTimeout(paintExempt, 1200);   // 等 discipline.js 轮询到新状态
                }
            }).catch(function (e) { btn.disabled = false; cfgMsg('操作失败: ' + e, true); });
        }
    }

    /* ---------- 加载 ---------- */

    function load() {
        var body = $('.db-body', _root);
        if (body) { body.innerHTML = '<div class="db-loading">加载中…</div>'; }
        api(BOARD_URL + '?days=' + _days, 'GET').then(function (res) {
            if (!res || res.code !== 200 || !res.data) {
                if (body) { body.innerHTML = '<div class="db-loading">' + esc((res && res.message) || '看板加载失败') + '</div>'; }
                return;
            }
            _data = res.data;
            _cfg = res.data.config || _cfg;
            paint();
        }).catch(function (e) {
            if (body) { body.innerHTML = '<div class="db-loading">看板加载失败: ' + esc(e) + '</div>'; }
        });
    }

    /* ---------- 初始化 ---------- */

    function init() {
        var host = document.getElementById(MOUNT_ID);
        if (!host) { return; }   // 不在任务计划页，静默跳过
        ensureStyle();
        host.className = 'db-panel';
        // 挂载点带 inline display:none（JS 未就绪时不露空框），这里接手后取消
        host.style.display = '';
        host.innerHTML = skeleton();
        _root = host;

        $('.db-head', host).addEventListener('click', function (e) {
            var btn = e.target.closest ? e.target.closest('[data-days],[data-act]') : null;
            if (!btn) { return; }
            if (btn.dataset.days) {
                _days = parseInt(btn.dataset.days, 10) || 30;
                host.querySelectorAll('[data-days]').forEach(function (b) {
                    b.classList.toggle('on', parseInt(b.dataset.days, 10) === _days);
                });
                load();
                return;
            }
            if (btn.dataset.act === 'reload') { load(); return; }
            if (btn.dataset.act === 'toggle') {
                _collapsed = !_collapsed;
                host.classList.toggle('db-collapsed', _collapsed);
                btn.textContent = _collapsed ? '▸ 展开' : '▾ 折叠';
            }
        });
        host.querySelectorAll('[data-days]').forEach(function (b) {
            b.classList.toggle('on', parseInt(b.dataset.days, 10) === _days);
        });
        load();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.DisciplineBoard = { reload: load };
})();
