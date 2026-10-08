/* ==========================================================================
   app.js · 全局交互（演示站）
   --------------------------------------------------------------------------
   仅承担纯前端交互：主题切换、移动端侧栏开合、刷新动效。
   无任何后端数据请求——演示阶段保持纯静态。
   ========================================================================== */
(function () {
  'use strict';

  var root = document.documentElement;

  /* ---- 主题切换（令牌驱动，localStorage 记忆） ---- */
  var THEME_KEY = 'opscenter_theme';
  var saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
  if (saved === 'light' || saved === 'dark') { root.setAttribute('data-theme', saved); }
  syncThemeIcon();

  var themeBtn = document.getElementById('themeToggle');
  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 忽略隐私模式 */ }
      syncThemeIcon();
    });
  }
  function syncThemeIcon() {
    if (!themeBtn) return;
    themeBtn.textContent = root.getAttribute('data-theme') === 'light' ? '☀️' : '🌙';
  }

  /* ---- 移动端侧栏开合 ---- */
  var sidebar = document.getElementById('sidebar');
  var scrim = document.getElementById('scrim');
  var navToggle = document.getElementById('navToggle');
  function closeNav() {
    if (sidebar) sidebar.classList.remove('is-open');
    if (scrim) scrim.classList.remove('is-visible');
  }
  if (navToggle) {
    navToggle.addEventListener('click', function (e) {
      e.stopPropagation();
      if (sidebar) sidebar.classList.toggle('is-open');
      if (scrim) scrim.classList.toggle('is-visible');
    });
  }
  if (scrim) scrim.addEventListener('click', closeNav);

  /* ---- 刷新按钮动效（演示：仅旋转，不请求数据） ---- */
  var refreshBtn = document.getElementById('refreshBtn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () {
      refreshBtn.style.transition = 'transform .6s cubic-bezier(.4,0,.2,1)';
      refreshBtn.style.transform = 'rotate(360deg)';
      setTimeout(function () {
        refreshBtn.style.transition = 'none';
        refreshBtn.style.transform = 'none';
      }, 620);
    });
  }

  /* ---- 卡片入场：滚动到视口再淡入（渐进增强，不支持则全部可见） ---- */
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.style.animationPlayState = 'running'; io.unobserve(en.target); }
      });
    }, { threshold: .08 });
    document.querySelectorAll('.fade-in').forEach(function (el) { io.observe(el); });
  }
})();

/* ==========================================================================
   P2 · 日志聚合查看器（仅当页面存在 #logApp 时激活，独立于其它页面）
   与后端只读 API 交互：级别/关键词/来源/币种过滤 + tail 增量向更早翻页 + 跟随。
   ========================================================================== */
(function () {
  'use strict';
  var app = document.getElementById('logApp');
  if (!app) { return; }

  var list = document.getElementById('logList');
  var statusEl = document.getElementById('logStatus');
  var moreBtn = document.getElementById('logMore');
  var qInput = document.getElementById('logQ');
  var coinInput = document.getElementById('logCoin');
  var srcSel = document.getElementById('logSource');
  var tailChk = document.getElementById('logTail');
  var chips = Array.prototype.slice.call(app.querySelectorAll('.chip[data-level]'));

  var state = { levels: {}, q: '', coin: '', source: '', nextBefore: null, hasMore: false, matched: 0 };
  var timer = null;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function lvlText(l) { return l === 'error' ? 'ERR' : (l === 'warn' ? 'WARN' : 'INFO'); }

  function lineHtml(r) {
    return '<div class="logline logline--' + esc(r.level) + '">' +
      '<span class="logline__ts">' + esc(r.ts || '—') + '</span>' +
      '<span class="logline__lvl">' + lvlText(r.level) + '</span>' +
      '<span class="logline__src">' + esc(r.src_name) + '</span>' +
      '<span class="logline__coin">' + esc(r.coin || '') + '</span>' +
      '<span class="logline__msg">' + esc(r.msg) + '</span>' +
      '</div>';
  }

  function buildUrl(before) {
    var levels = Object.keys(state.levels).filter(function (k) { return state.levels[k]; });
    var p = [];
    if (levels.length) { p.push('level=' + encodeURIComponent(levels.join(','))); }
    if (state.q) { p.push('q=' + encodeURIComponent(state.q)); }
    if (state.coin) { p.push('coin=' + encodeURIComponent(state.coin)); }
    if (state.source) { p.push('source=' + encodeURIComponent(state.source)); }
    p.push('limit=200');
    if (before != null) { p.push('before=' + encodeURIComponent(before)); }
    return '/logs/data?' + p.join('&');
  }

  function fetchPage(before, done) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', buildUrl(before), true);
    xhr.onload = function () {
      var res;
      try { res = JSON.parse(xhr.responseText); } catch (e) { res = null; }
      if (xhr.status === 200 && res && res.code === 0) { done(null, res.data); }
      else { done(new Error('bad response'), null); }
    };
    xhr.onerror = function () { done(new Error('network'), null); };
    xhr.send();
  }

  function setStatus() {
    var n = list.querySelectorAll('.logline').length;
    statusEl.textContent = '匹配 ' + state.matched + ' 条 · 已载入 ' + n +
      (state.hasMore ? '（可载入更早）' : '（已到最早）') +
      (tailChk && tailChk.checked ? ' · 跟随中' : '');
    moreBtn.style.display = state.hasMore ? '' : 'none';
  }

  function reload() {
    fetchPage(null, function (err, data) {
      if (err) { list.innerHTML = '<div class="empty">日志加载失败：' + esc(err.message) + '</div>'; return; }
      state.nextBefore = data.next_before;
      state.hasMore = data.has_more;
      state.matched = data.matched;
      list.innerHTML = data.records.length
        ? data.records.map(lineHtml).join('')
        : '<div class="empty"><span class="empty__icon">🔍</span>无匹配日志，试试放宽级别或关键词</div>';
      setStatus();
    });
  }

  function loadMore() {
    moreBtn.disabled = true;
    fetchPage(state.nextBefore, function (err, data) {
      moreBtn.disabled = false;
      if (err || !data.records.length) { state.hasMore = false; setStatus(); return; }
      state.nextBefore = data.next_before;
      state.hasMore = data.has_more;
      list.insertAdjacentHTML('beforeend', data.records.map(lineHtml).join(''));
      setStatus();
    });
  }

  chips.forEach(function (c) {
    c.addEventListener('click', function () {
      var lv = c.getAttribute('data-level');
      state.levels[lv] = !state.levels[lv];
      c.classList.toggle('is-on', !!state.levels[lv]);
      reload();
    });
  });
  if (qInput) { qInput.addEventListener('input', function () { state.q = qInput.value.trim(); reload(); }); }
  if (coinInput) { coinInput.addEventListener('input', function () { state.coin = coinInput.value.trim(); reload(); }); }
  if (srcSel) { srcSel.addEventListener('change', function () { state.source = srcSel.value; reload(); }); }
  if (moreBtn) { moreBtn.addEventListener('click', loadMore); }
  if (tailChk) {
    tailChk.addEventListener('change', function () {
      if (tailChk.checked) { timer = setInterval(reload, 5000); }
      else if (timer) { clearInterval(timer); timer = null; }
      setStatus();
    });
  }

  reload();
})();
