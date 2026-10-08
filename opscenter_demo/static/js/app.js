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
