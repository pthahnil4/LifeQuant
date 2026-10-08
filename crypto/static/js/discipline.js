/* ============================================================
 * discipline.js —— 分析纪律全站打扰层（批次11，L1/L2/L3）
 * ============================================================
 * 由 nav.html 全站注入，在任何页面都跑同一套节拍：
 *
 *   L1 导航角标   「📝 分析记录」右侧常驻倒计时；做完变 ✓。
 *                 最低打扰、最高频次——每小时抬眼就能看到还剩几分钟。
 *   L2 贴顶横幅   槽内已过 banner_after_minutes（默认 20 分钟）仍未分析才出现，
 *                 sticky 跟随滚动；同一小时槽只主动弹一次，可「本小时不再提醒」。
 *   L3 声音+桌面  横幅首次出现时一声短提示音 + Notification。
 *
 * 【为什么 L2/L3 要压到 20 分钟后】整点即打扰的提醒会在一周内被当成噪声关掉。
 * 给每小时开头留出一段自然的分析窗口，提醒只在"真的要漏了"时出现。
 *
 * 【浏览器策略约束】AudioContext 与 Notification 权限都要求用户手势，
 * 所以两者都在首次 click/keydown 时惰性初始化，绝不自动弹权限框。
 *
 * 【失败取向】status 接口异常/纪律关闭 → 摘掉角标与横幅、停止打扰，
 * 页面功能完全不受影响。纪律层绝不能成为页面的故障源。
 * ============================================================ */
(function () {
    'use strict';

    if (window.AnalysisDiscipline) { return; }

    var STATUS_URL = '/plan/api/discipline/status';
    var EXEMPT_URL = '/plan/api/discipline/exempt';

    var SS_BANNER = 'aqd_banner_done_';   // 该小时槽横幅已主动弹过
    var SS_MUTED = 'aqd_banner_muted_';   // 用户点了「本小时不再提醒」

    var _st = null;            // 最近一次 status 载荷
    var _timer = null;         // 轮询定时器
    var _ticker = null;        // 1s 倒计时刷新
    var _banner = null;
    var _navLink = null;      // 主分析锚点（宽度变化作为导航重算折叠的观测量）
    var _fallback = null;     // 导航无分析入口时的独立倒计时胶囊
    var _pollSec = 60;
    var _audioCtx = null;
    var _gestureBound = false;
    var _lastLayoutWidth = -1;

    /* ---------- 小工具 ---------- */

    function ssGet(k) { try { return sessionStorage.getItem(k) || ''; } catch (e) { return ''; } }
    function ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* 隐私模式下静默 */ } }

    function esc(s) {
        return String(s === null || s === undefined ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    /** 服务端 'YYYY-MM-DD HH:MM:SS' → 本地 Date（按本地时区解析，项目全链路东八区本地时钟） */
    function parseServerNow(s) {
        var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(String(s || ''));
        if (!m) { return null; }
        return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
    }

    /** 本小时槽剩余秒数（以服务端时钟为基准，浏览器时间只用于走针） */
    function secondsLeft() {
        if (!_st || !_st.now) { return 0; }
        var base = parseServerNow(_st.now);
        if (!base) { return 0; }
        var elapsed = Math.floor((Date.now() - base.getTime()) / 1000);
        var inHour = base.getMinutes() * 60 + base.getSeconds() + elapsed;
        return Math.max(0, 3600 - inHour);
    }

    function mmss(sec) {
        var m = Math.floor(sec / 60), s = sec % 60;
        return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
    }

    /* ---------- 样式注入 ---------- */

    var CSS = [
        /* L1 导航角标 */
        '.aqd-badge{display:inline-block;margin-left:5px;padding:0 5px;border-radius:9px;',
        'font-size:.68rem;font-weight:700;line-height:16px;vertical-align:middle;',
        'background:#fff4e6;color:#d9480f;border:1px solid #ffd8a8;font-variant-numeric:tabular-nums;}',
        '.aqd-badge.ok{background:#ebfbee;color:#2b8a3e;border-color:#b2f2bb;}',
        '.aqd-badge.urgent{background:#fff5f5;color:#c92a2a;border-color:#ffc9c9;',
        'animation:aqd-pulse 1.2s ease-in-out infinite;}',
        '.aqd-badge.off{display:none;}',
        '@keyframes aqd-pulse{0%,100%{opacity:1}50%{opacity:.45}}',
        /* L2 贴顶横幅 */
        '.aqd-banner{position:sticky;top:0;z-index:8500;display:flex;align-items:center;gap:10px;',
        'flex-wrap:wrap;padding:9px 16px;background:#fff9db;border-bottom:1px solid #ffe066;',
        'font-size:.85rem;color:#664d03;box-shadow:0 2px 8px rgba(0,0,0,.06);}',
        '.aqd-banner.urgent{background:#fff5f5;border-bottom-color:#ffc9c9;color:#a61e1e;}',
        '.aqd-banner .aqd-txt{flex:1;min-width:200px;line-height:1.5;}',
        '.aqd-banner b{font-variant-numeric:tabular-nums;}',
        '.aqd-btn{border:1px solid rgba(0,0,0,.14);background:#fff;border-radius:6px;',
        'padding:4px 10px;font-size:.8rem;cursor:pointer;white-space:nowrap;color:inherit;}',
        '.aqd-btn:hover{border-color:currentColor;}',
        '.aqd-btn.pri{background:#fab005;border-color:#fab005;color:#3d2f00;font-weight:600;}',
        '.aqd-banner.urgent .aqd-btn.pri{background:#fa5252;border-color:#fa5252;color:#fff;}',
        '.aqd-x{border:0;background:transparent;font-size:1.15rem;cursor:pointer;',
        'line-height:1;padding:0 4px;color:inherit;opacity:.65;}',
        '.aqd-x:hover{opacity:1;}',
        /* 导航中无分析入口时的独立倒计时胶囊（不劫持导航、不强推恢复） */
        '.aqd-fallback{position:fixed;top:56px;right:14px;z-index:8600;display:inline-flex;align-items:center;gap:6px;',
        'padding:6px 10px;border-radius:22px;background:#fff4e6;color:#d9480f;border:1px solid #ffd8a8;',
        'font-size:.8rem;font-weight:700;text-decoration:none;box-shadow:0 4px 14px rgba(0,0,0,.1);',
        'font-variant-numeric:tabular-nums;}',
        '.aqd-fallback.ok{background:#ebfbee;color:#2b8a3e;border-color:#b2f2bb;}',
        '.aqd-fallback.urgent{background:#fff5f5;color:#c92a2a;border-color:#ffc9c9;animation:aqd-pulse 1.2s ease-in-out infinite;}',
        '.aqd-off{display:none !important;}'
    ].join('');

    function ensureStyle() {
        if (document.getElementById('aqd-style')) { return; }
        var st = document.createElement('style');
        st.id = 'aqd-style';
        st.textContent = CSS;
        document.head.appendChild(st);
    }

    /* ---------- L1 导航角标 ---------- */

    /** 收集导航中所有分析锚点：顶栏直达 / 更多菜单 / 移动端抽屉 / 分类浮层 */
    function findNavAnchors() {
        var sel = '#navLinks [data-nav-role="analysis"],' +
            '#navMoreMenu [data-nav-role="analysis"],' +
            '#navDrawer [data-nav-role="analysis"],' +
            '.nav-popup [data-nav-role="analysis"]';
        var list = [];
        try { document.querySelectorAll(sel).forEach(function (a) { list.push(a); }); } catch (e) { /* noop */ }
        return list;
    }

    /** 在某锚点内确保存在一个角标 span，并按给定态刷新（渲染后 DOM 重建会自动重挂） */
    function applyBadge(anchor, cls, text, title) {
        var b = null;
        for (var i = 0; i < anchor.children.length; i++) {
            if (anchor.children[i].classList && anchor.children[i].classList.contains('aqd-badge')) { b = anchor.children[i]; break; }
        }
        if (!b) { b = document.createElement('span'); anchor.appendChild(b); }
        b.className = cls;
        b.textContent = text;
        if (title) { b.title = title; }
    }

    /** 根据当前纪律状态算角标呈现（顶栏角标与独立胶囊共用） */
    function badgeState() {
        if (!_st || !_st.enabled || !_st.active) {
            return { off: true, ok: false, urgent: false, cls: 'aqd-badge off', text: '', title: '' };
        }
        var left = secondsLeft();
        if (_st.ok) {
            return { off: false, ok: true, urgent: false, cls: 'aqd-badge ok', text: '✓ ' + (_st.actual || 0),
                title: '本小时已分析 ' + (_st.actual || 0) + ' 条，打卡闸门已放行' };
        }
        return { off: false, ok: false, urgent: left <= 600, cls: 'aqd-badge' + (left <= 600 ? ' urgent' : ''), text: mmss(left),
            title: '本小时还没做分析记录，剩 ' + mmss(left) + '（要求 ' + (_st.required || 1) + ' 条）· 点击查看分析页' };
    }

    function removeFallback() {
        if (_fallback && _fallback.parentNode) { _fallback.parentNode.removeChild(_fallback); }
        _fallback = null;
    }

    function ensureFallback() {
        if (_fallback) { return _fallback; }
        var a = document.createElement('a');
        a.href = '/analysis';
        a.className = 'aqd-fallback';
        a.textContent = '📝';
        document.body.appendChild(a);
        _fallback = a;
        return a;
    }

    function paintBadge() {
        var s = badgeState();
        var anchors = findNavAnchors();
        if (anchors.length) {
            // 导航里有分析入口：角标挂进每个锚点（顶栏/抽屉/更多/浮层），撤掉独立胶囊
            removeFallback();
            _navLink = anchors[0];
            for (var i = 0; i < anchors.length; i++) { applyBadge(anchors[i], s.cls, s.text, s.title); }
            return;
        }
        // 导航里没有（折叠在未展开分类 / 被隐藏 / 被删除）：用固定胶囊显示同样倒计时，绝不强推恢复
        _navLink = null;
        if (s.off) { removeFallback(); return; }
        var f = ensureFallback();
        f.className = 'aqd-fallback' + (s.ok ? ' ok' : (s.urgent ? ' urgent' : ''));
        f.textContent = '📝 ' + s.text;
        f.title = s.title;
    }

    /** 角标/胶囊改变了导航条目宽度，通知 navigation.js 重新测量折叠 */
    function relayoutNav() {
        if (!_navLink) { return; }
        var w = _navLink.getBoundingClientRect().width;
        if (Math.abs(w - _lastLayoutWidth) < 1) { return; }
        _lastLayoutWidth = w;
        try { window.dispatchEvent(new Event('resize')); } catch (e) { /* 老浏览器忽略 */ }
    }

    /* ---------- L2 贴顶横幅 ---------- */

    function removeBanner() {
        if (_banner && _banner.parentNode) { _banner.parentNode.removeChild(_banner); }
        _banner = null;
    }

    function bannerWanted() {
        if (!_st || !_st.enabled || !_st.banner) { return false; }
        var slot = _st.hour_slot || '';
        if (!slot) { return false; }
        if (ssGet(SS_MUTED + slot)) { return false; }
        return true;
    }

    function paintBanner() {
        if (!bannerWanted()) { removeBanner(); return; }
        var slot = _st.hour_slot;
        var left = secondsLeft();
        var urgent = left <= 600;

        if (!_banner) {
            _banner = document.createElement('div');
            _banner.className = 'aqd-banner';
            _banner.innerHTML =
                '<span class="aqd-txt"></span>' +
                '<button type="button" class="aqd-btn pri" data-act="go">📸 一键分析并记录</button>' +
                '<button type="button" class="aqd-btn" data-act="page">📝 去分析页</button>' +
                '<button type="button" class="aqd-btn" data-act="exempt">😴 豁免 2 小时</button>' +
                '<button type="button" class="aqd-btn" data-act="mute">🔕 本小时不再提醒</button>' +
                '<button type="button" class="aqd-x" data-act="close" title="关闭">×</button>';
            var nav = document.querySelector('.main-nav');
            if (nav && nav.parentNode) { nav.parentNode.insertBefore(_banner, nav.nextSibling); }
            else { document.body.insertBefore(_banner, document.body.firstChild); }
            _banner.addEventListener('click', onBannerClick);
        }
        _banner.classList.toggle('urgent', urgent);
        _banner.querySelector('.aqd-txt').innerHTML =
            (urgent ? '⚠️ ' : '⏰ ') + '本小时（<b>' + esc(slot.slice(11) + ':00') + '</b>）还没做分析记录，' +
            '已过 <b>' + (_st.elapsed_minutes || 0) + '</b> 分钟，剩 <b>' + mmss(left) + '</b>。' +
            '未做分析时交易打卡会被拦住（要求 ' + (_st.required || 1) + ' 条）。';

        // 首次出现才响铃/推通知：同一槽内反复轮询不重复打扰
        if (!ssGet(SS_BANNER + slot)) {
            ssSet(SS_BANNER + slot, '1');
            beep();
            notify('分析纪律提醒', '本小时还没做分析记录，剩 ' + mmss(left) + '。打卡前需要先分析。');
        }
    }

    function onBannerClick(e) {
        var btn = e.target.closest ? e.target.closest('[data-act]') : null;
        if (!btn) { return; }
        var act = btn.dataset.act;
        if (act === 'close') { removeBanner(); return; }
        if (act === 'mute') {
            ssSet(SS_MUTED + (_st && _st.hour_slot || ''), '1');
            removeBanner();
            return;
        }
        if (act === 'page') { window.location.href = '/analysis'; return; }
        if (act === 'go') {
            if (!window.AnalysisQuick) { window.location.href = '/analysis'; return; }
            btn.disabled = true;
            window.AnalysisQuick.open({
                mode: 'live',
                title: '📸 补上本小时的分析记录',
                onSaved: function () { refresh(); }
            });
            // 弹窗可能被用户直接关掉，按钮必须恢复可点
            setTimeout(function () { if (btn) { btn.disabled = false; } }, 800);
            return;
        }
        if (act === 'exempt') {
            btn.disabled = true;
            fetch(EXEMPT_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hours: 2 })
            }).then(function (r) { return r.json(); }).then(function (res) {
                if (res && res.code === 200) {
                    removeBanner();
                    refresh();
                } else {
                    btn.disabled = false;
                    alert((res && res.message) || '豁免设置失败');
                }
            }).catch(function (e) {
                btn.disabled = false;
                alert('豁免设置失败: ' + e);
            });
        }
    }

    /* ---------- L3 声音 + 桌面通知 ---------- */

    function bindGesture() {
        if (_gestureBound) { return; }
        _gestureBound = true;
        var init = function () {
            // 浏览器要求用户手势后才能建 AudioContext / 申请通知权限
            try {
                var AC = window.AudioContext || window.webkitAudioContext;
                if (AC && !_audioCtx) { _audioCtx = new AC(); }
            } catch (e) { _audioCtx = null; }
            try {
                if (window.Notification && Notification.permission === 'default' &&
                    _st && _st.browser && _st.browser.desktop_notify) {
                    Notification.requestPermission();
                }
            } catch (e) { /* 不支持就跳过 */ }
            document.removeEventListener('click', init);
            document.removeEventListener('keydown', init);
        };
        document.addEventListener('click', init);
        document.addEventListener('keydown', init);
    }

    /** 两声短促上行音：够听见、不刺耳，也不需要任何音频资源文件 */
    function beep() {
        if (!_st || !_st.browser || !_st.browser.sound) { return; }
        if (!_audioCtx) { return; }
        try {
            if (_audioCtx.state === 'suspended') { _audioCtx.resume(); }
            var t = _audioCtx.currentTime;
            [880, 1174].forEach(function (freq, i) {
                var osc = _audioCtx.createOscillator();
                var gain = _audioCtx.createGain();
                osc.type = 'sine';
                osc.frequency.value = freq;
                var at = t + i * 0.16;
                gain.gain.setValueAtTime(0.0001, at);
                gain.gain.exponentialRampToValueAtTime(0.16, at + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.14);
                osc.connect(gain);
                gain.connect(_audioCtx.destination);
                osc.start(at);
                osc.stop(at + 0.16);
            });
        } catch (e) { /* 音频失败不影响其余提醒 */ }
    }

    function notify(title, body) {
        if (!_st || !_st.browser || !_st.browser.desktop_notify) { return; }
        try {
            if (!window.Notification || Notification.permission !== 'granted') { return; }
            var n = new Notification(title, { body: body, tag: 'aqd-' + (_st.hour_slot || '') });
            n.onclick = function () { try { window.focus(); } catch (e) { /* noop */ } n.close(); };
            setTimeout(function () { try { n.close(); } catch (e) { /* noop */ } }, 12000);
        } catch (e) { /* noop */ }
    }

    /* ---------- 轮询 ---------- */

    function paint() {
        paintBadge();
        relayoutNav();
        paintBanner();
    }

    function stopTicker() {
        if (_ticker) { clearInterval(_ticker); _ticker = null; }
    }

    function startTicker() {
        if (_ticker) { return; }
        _ticker = setInterval(function () {
            if (!_st || !_st.enabled) { return; }
            paintBadge();
            // 剩余时间在横幅上也显示，同步刷新
            if (_banner) { paintBanner(); }
        }, 1000);
    }

    function schedule() {
        if (_timer) { clearTimeout(_timer); }
        _timer = setTimeout(poll, Math.max(10, _pollSec) * 1000);
    }

    function poll() {
        fetch(STATUS_URL).then(function (r) { return r.json(); }).then(function (res) {
            var d = (res && res.data) || null;
            if (!d || !d.enabled) {
                // 纪律关闭：彻底静默，不留任何视觉残留
                _st = d || null;
                removeBanner();
                removeFallback();
                findNavAnchors().forEach(function (a) { applyBadge(a, 'aqd-badge off', '', ''); });
                stopTicker();
                schedule();
                return;
            }
            _st = d;
            _pollSec = (d.browser && d.browser.poll_seconds) || 60;
            bindGesture();
            paint();
            startTicker();
            schedule();
        }).catch(function () {
            // 接口不可达（重启中/网络抖动）：保持上一次状态，下个周期再试
            schedule();
        });
    }

    function refresh() {
        if (_timer) { clearTimeout(_timer); }
        poll();
    }

    function start() {
        ensureStyle();
        poll();
        // navigation.js 重绘（拖拽排序/校准/显隐变化）会重建导航 DOM，角标需随之重挂
        document.addEventListener('nav:rendered', function () { paintBadge(); relayoutNav(); });
        // 页面重新可见时立刻校准一次（挂后台的标签页定时器会被节流）
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) { refresh(); }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }

    window.AnalysisDiscipline = {
        refresh: refresh,
        status: function () { return _st; },
        hideBanner: removeBanner
    };
})();
